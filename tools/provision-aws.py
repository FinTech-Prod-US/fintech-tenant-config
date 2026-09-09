#!/usr/bin/env python3
"""
Provision AWS infrastructure for a tenant — SQS queues (with DLQs) and
S3 bucket with standard folder structure.

Queue names and S3 bucket names are auto-discovered by scanning the
tenant's env.json files, so the provisioner always matches what the
services actually reference. No hardcoded list needed.

Usage:
  # Dry-run (default — safe, prints plan only)
  python tools/provision-aws.py --tenant mcb --env qa

  # Apply
  python tools/provision-aws.py --tenant mcb --env qa --apply

  # Teardown (deletes queues + bucket — DESTRUCTIVE)
  python tools/provision-aws.py --tenant mcb --env qa --teardown

  # Override region
  python tools/provision-aws.py --tenant mcb --env qa --region us-east-1 --apply

  # LocalStack
  python tools/provision-aws.py --tenant mcb --env qa \
    --endpoint-url http://localhost:4566 --apply

What gets created:
  SQS: one main queue + one DLQ per discovered queue name
  S3:  one bucket (name taken from S3_BUCKET in env.json) with the
       folder prefixes listed under provision.s3_folders in metadata.yaml
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("ERROR: boto3 is required. Install with: pip install boto3", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


# ── Constants ──────────────────────────────────────────────────────────────────

# Keys whose values are SQS queue names (not URLs) — bare queue names
_BARE_QUEUE_KEY_PREFIXES = ("SQS_QUEUE_",)

# Regex to extract queue name from a full SQS URL
_SQS_URL_RE = re.compile(
    r"https://sqs\.[^/]+\.amazonaws\.com/\d+/([A-Za-z0-9_-]+)"
)

# S3 keys that hold a bucket name
_S3_BUCKET_KEYS = {"S3_BUCKET", "EIE_S3_BUCKET", "TAE_S3_BUCKET", "IMAGE_ARCHIVE_S3_BUCKET"}


# ── YAML / metadata helpers ────────────────────────────────────────────────────

def _simple_yaml_load(text: str) -> dict:
    """Minimal YAML parser for metadata.yaml when PyYAML is absent."""
    result: dict = {}
    stack: list[tuple[int, dict]] = [(0, result)]
    current_list_key: Optional[str] = None

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip())
        line = raw_line.strip()

        # Pop stack to current indent level
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
            current_list_key = None

        parent = stack[-1][1]

        if line.startswith("- "):          # list item
            val = line[2:].strip()
            if current_list_key and isinstance(parent.get(current_list_key), list):
                parent[current_list_key].append(val)
        elif ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if val == "":
                # It's a mapping key — push a new dict
                child: dict = {}
                parent[key] = child
                stack.append((indent + 2, child))
                current_list_key = None
            elif val in ("[]",):
                parent[key] = []
                current_list_key = key
            else:
                # Try to detect list start on next line
                parent[key] = val
                current_list_key = key
                # Pre-create list so list items can append
                # (overwritten if it stays a scalar)
    return result


def load_metadata(repo_root: Path, tenant: str) -> dict:
    path = repo_root / "tenants" / tenant / "metadata.yaml"
    if not path.exists():
        print(f"WARNING: metadata.yaml not found at {path}", file=sys.stderr)
        return {}
    text = path.read_text(encoding="utf-8")
    if _YAML_AVAILABLE:
        return yaml.safe_load(text) or {}
    return _simple_yaml_load(text)


# ── Discovery ──────────────────────────────────────────────────────────────────

def discover_resources(repo_root: Path, tenant: str) -> tuple[set[str], set[str]]:
    """
    Scan all env.json files for the tenant.
    Returns (queue_names, s3_bucket_names) — both are plain names (no URLs).
    DLQ references (names ending in _DLQ) are excluded from the main set;
    DLQs are created automatically alongside each main queue.
    """
    queue_names: set[str] = set()
    s3_buckets: set[str] = set()

    tenant_dir = repo_root / "tenants" / tenant
    for env_file in sorted(tenant_dir.rglob("env.json")):
        try:
            data = json.loads(env_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"WARNING: could not parse {env_file}: {exc}", file=sys.stderr)
            continue

        for key, value in data.items():
            if not isinstance(value, str):
                continue

            # SQS URL → extract queue name
            m = _SQS_URL_RE.match(value)
            if m:
                name = m.group(1)
                if not name.endswith("_DLQ"):
                    queue_names.add(name)
                continue

            # Bare SQS queue name (SQS_QUEUE_* keys)
            if any(key.startswith(pfx) for pfx in _BARE_QUEUE_KEY_PREFIXES):
                if value and not value.endswith("_DLQ"):
                    queue_names.add(value)
                continue

            # S3 bucket
            if key in _S3_BUCKET_KEYS and value:
                s3_buckets.add(value)

    return queue_names, s3_buckets


# ── SQS ───────────────────────────────────────────────────────────────────────

def provision_sqs(
    sqs,
    queue_names: set[str],
    dlq_max_receive: int,
    visibility_timeout: int,
    dry_run: bool,
) -> None:
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}SQS — {len(queue_names)} main queues + {len(queue_names)} DLQs:")

    for name in sorted(queue_names):
        dlq_name = f"{name}_DLQ"
        print(f"  {name}  +  {dlq_name}")

        if dry_run:
            continue

        # Create DLQ first
        try:
            dlq_resp = sqs.create_queue(
                QueueName=dlq_name,
                Attributes={"VisibilityTimeout": str(visibility_timeout)},
            )
            dlq_url = dlq_resp["QueueUrl"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "QueueAlreadyExists":
                dlq_url = sqs.get_queue_url(QueueName=dlq_name)["QueueUrl"]
                print(f"    {dlq_name}: already exists")
            else:
                raise

        dlq_attrs = sqs.get_queue_attributes(
            QueueUrl=dlq_url, AttributeNames=["QueueArn"]
        )
        dlq_arn = dlq_attrs["Attributes"]["QueueArn"]

        redrive_policy = json.dumps({
            "deadLetterTargetArn": dlq_arn,
            "maxReceiveCount": str(dlq_max_receive),
        })

        # Create main queue
        try:
            sqs.create_queue(
                QueueName=name,
                Attributes={
                    "VisibilityTimeout": str(visibility_timeout),
                    "RedrivePolicy": redrive_policy,
                },
            )
            print(f"    {name}: created")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "QueueAlreadyExists":
                print(f"    {name}: already exists")
            else:
                raise


def teardown_sqs(sqs, queue_names: set[str], dry_run: bool) -> None:
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}SQS TEARDOWN — deleting {len(queue_names) * 2} queues:")
    all_names = sorted(queue_names) + [f"{n}_DLQ" for n in sorted(queue_names)]
    for name in all_names:
        print(f"  DELETE {name}")
        if dry_run:
            continue
        try:
            url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
            sqs.delete_queue(QueueUrl=url)
            print(f"    deleted")
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("AWS.SimpleQueueService.NonExistentQueue", "QueueDoesNotExist"):
                print(f"    not found — skipped")
            else:
                raise


# ── S3 ────────────────────────────────────────────────────────────────────────

def provision_s3(
    s3,
    bucket_names: set[str],
    s3_folders: list[str],
    region: str,
    dry_run: bool,
) -> None:
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}S3 — {len(bucket_names)} bucket(s) with {len(s3_folders)} folders each:")

    for bucket in sorted(bucket_names):
        print(f"  bucket: {bucket}")
        for folder in s3_folders:
            print(f"    {folder}/")

        if dry_run:
            continue

        # Create bucket
        try:
            if region == "us-east-1":
                s3.create_bucket(Bucket=bucket)
            else:
                s3.create_bucket(
                    Bucket=bucket,
                    CreateBucketConfiguration={"LocationConstraint": region},
                )
            print(f"  bucket {bucket}: created")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                print(f"  bucket {bucket}: already exists")
            else:
                raise

        # Create folder placeholders
        for folder in s3_folders:
            key = f"{folder}/"
            s3.put_object(Bucket=bucket, Key=key, Body=b"")
        print(f"  folders created")


def teardown_s3(s3, bucket_names: set[str], dry_run: bool) -> None:
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}S3 TEARDOWN — deleting {len(bucket_names)} bucket(s) (objects first):")
    for bucket in sorted(bucket_names):
        print(f"  DELETE bucket: {bucket}")
        if dry_run:
            continue
        try:
            # Delete all objects first
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket):
                objects = page.get("Contents", [])
                if objects:
                    s3.delete_objects(
                        Bucket=bucket,
                        Delete={"Objects": [{"Key": o["Key"]} for o in objects]},
                    )
            s3.delete_bucket(Bucket=bucket)
            print(f"    deleted")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchBucket":
                print(f"    not found — skipped")
            else:
                raise


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Provision AWS SQS + S3 for a tenant")
    parser.add_argument("--tenant", required=True, help="Tenant name (matches tenants/<name>/)")
    parser.add_argument("--env", required=True, help="Environment (qa, dev, prod, etc.)")
    parser.add_argument("--region", help="AWS region (overrides metadata.yaml)")
    parser.add_argument("--endpoint-url", help="Override AWS endpoint (e.g. LocalStack)")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    parser.add_argument("--teardown", action="store_true", help="Delete all provisioned resources (DESTRUCTIVE)")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Path to fintech-tenant-config repo root",
    )
    args = parser.parse_args()

    dry_run = not args.apply and not args.teardown

    repo_root = args.repo_root
    metadata = load_metadata(repo_root, args.tenant)
    provision_cfg = metadata.get("provision", {}) or {}

    region = args.region or provision_cfg.get("aws_region") or "us-east-1"
    dlq_max_receive = int(provision_cfg.get("sqs_dlq_max_receive_count", 3))
    visibility_timeout = int(provision_cfg.get("sqs_visibility_timeout", 30))
    tenant_code = metadata.get("tenant_code") or args.tenant.upper()
    raw_folders = provision_cfg.get("s3_folders") or []
    s3_folders = [f.replace("{tenant_code}", tenant_code) for f in raw_folders]

    print(f"Tenant:       {args.tenant}")
    print(f"Environment:  {args.env}")
    print(f"Region:       {region}")
    print(f"Mode:         {'TEARDOWN' if args.teardown else ('APPLY' if args.apply else 'DRY-RUN')}")

    # Discover queues and S3 buckets from env.json files
    queue_names, s3_buckets = discover_resources(repo_root, args.tenant)

    # Merge additional queues declared explicitly in metadata.yaml
    additional_queues = provision_cfg.get("additional_sqs_queues") or []
    queue_names.update(q for q in additional_queues if q and not q.endswith("_DLQ"))

    if not queue_names and not s3_buckets:
        print("No SQS queues or S3 buckets discovered — nothing to provision.", file=sys.stderr)
        return 1

    # Build boto3 clients
    client_kwargs: dict = {"region_name": region}
    if args.endpoint_url:
        client_kwargs["endpoint_url"] = args.endpoint_url

    sqs = boto3.client("sqs", **client_kwargs)
    s3  = boto3.client("s3",  **client_kwargs)

    if args.teardown:
        teardown_sqs(sqs, queue_names, dry_run=False)
        teardown_s3(s3, s3_buckets, dry_run=False)
    else:
        provision_sqs(sqs, queue_names, dlq_max_receive, visibility_timeout, dry_run)
        provision_s3(s3, s3_buckets, s3_folders, region, dry_run)

    if dry_run:
        print("\nDry-run complete. Re-run with --apply to create resources.")
    else:
        print("\nDone.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
