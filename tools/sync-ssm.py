#!/usr/bin/env python3
"""
Sync tenant configuration files to AWS Systems Manager Parameter Store.

This is a one-way Git -> SSM sync. It never reads SSM back into Git.

Usage:
  # Dry-run
  python tools/sync-ssm.py --tenant mcb --env prod --region us-east-1 --dry-run

  # Apply
  python tools/sync-ssm.py --tenant mcb --env prod --region us-east-1

  # LocalStack
  python tools/sync-ssm.py --tenant mcb --env prod \
    --endpoint-url http://localhost:4566 --region us-east-1 --apply

Each service folder under tenants/{tenant}/{service}/ may contain:
  - env.json      -> SSM String parameters
  - secrets.json  -> SSM SecureString parameters

Parameters are written under:
  /fintech/{tenant}/{env}/{service}/config/{VAR}

Plain env.json values become SSM Type=String.
secrets.json values become SSM Type=SecureString with the KMS key specified by
--kms-key-id (default: alias/aws/ssm).

WARNING: Do not commit plaintext secrets.json to Git. Use SOPS or git-crypt to
encrypt it first. See README.md for the recommended workflow.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


DEFAULT_SERVICES = [
    "accs-server",
    "accs-ui",
    "api-aggregator-service",
    "business-rules-engine",
    "data-ingestion-service-express",
    "ecs-cashletter-service-engine",
    "ecs-cde-engine",
    "ecs-data-services-api-core",
    "ecs-dataqualityvalidator",
    "ecs-dupdetectengine",
    "ecs-exceptioningestionengine",
    "ecs-image-archive-engine",
    "ecs-image-quality-validator",
    "ecs-posting-eod-extract-engine",
    "ecs-taeengine",
    "file-data-enrichment-engine",
    "moov-io-accs",
    "moov-io-fed-accs",
]


import json
import shutil
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def find_sops() -> str | None:
    sops = shutil.which("sops")
    if sops:
        return sops
    # Common fallback locations on Windows when not on PATH
    candidates = [
        Path.home() / "tools" / "sops.exe",
        Path(r"C:\Program Files\sops\sops.exe"),
        Path(r"C:\ProgramData\chocolatey\bin\sops.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def load_json(path: Path) -> dict:
    """Load a JSON file, transparently decrypting with SOPS if it is encrypted."""
    raw_text = path.read_text(encoding="utf-8")
    raw_data = json.loads(raw_text)

    if "sops" in raw_data:
        sops_bin = find_sops()
        if not sops_bin:
            raise RuntimeError(
                f"{path} is SOPS-encrypted but 'sops' binary was not found in PATH. "
                "Install SOPS or decrypt the file before syncing."
            )
        result = subprocess.run(
            [sops_bin, "-d", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"SOPS decryption failed for {path}: {result.stderr.strip()}"
            )
        return json.loads(result.stdout)

    return raw_data


def put_parameter(
    client,
    name: str,
    value: str,
    param_type: str,
    kms_key_id: str | None,
    dry_run: bool,
) -> None:
    if dry_run:
        print(f"  [dry-run] would put {param_type}: {name}")
        return

    kwargs: dict = {
        "Name": name,
        "Value": value,
        "Type": param_type,
        "Overwrite": True,
        "DataType": "text",
    }
    if param_type == "SecureString" and kms_key_id:
        kwargs["KeyId"] = kms_key_id

    try:
        client.put_parameter(**kwargs)
        print(f"  put {param_type}: {name}")
    except ClientError as exc:
        print(f"  ERROR putting {name}: {exc}", file=sys.stderr)
        raise


def sync_service(
    client,
    tenant: str,
    env: str,
    service: str,
    service_dir: Path,
    kms_key_id: str | None,
    dry_run: bool,
) -> tuple[int, int]:
    base_path = f"/fintech/{tenant}/{env}/{service}/config"

    env_file = service_dir / "env.json"
    secrets_file = service_dir / "secrets.json"

    string_count = 0
    secure_count = 0

    if env_file.exists():
        for key, value in load_json(env_file).items():
            put_parameter(
                client,
                f"{base_path}/{key}",
                str(value),
                "String",
                kms_key_id,
                dry_run,
            )
            string_count += 1

    if secrets_file.exists():
        for key, value in load_json(secrets_file).items():
            put_parameter(
                client,
                f"{base_path}/{key}",
                str(value),
                "SecureString",
                kms_key_id,
                dry_run,
            )
            secure_count += 1

    return string_count, secure_count


def discover_services(tenant_dir: Path) -> list[str]:
    services = []
    if not tenant_dir.exists():
        return services
    for child in sorted(tenant_dir.iterdir()):
        if child.is_dir() and (child / "env.json").exists() or (child / "secrets.json").exists():
            services.append(child.name)
    return services


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync tenant config to SSM")
    parser.add_argument("--tenant", required=True, help="Tenant name (e.g. mcb)")
    parser.add_argument("--env", required=True, help="Environment name (e.g. prod)")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument("--endpoint-url", help="Optional SSM endpoint URL (for LocalStack)")
    parser.add_argument("--kms-key-id", default="alias/aws/ssm", help="KMS key for SecureString parameters")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Path to the tenant-config repo root",
    )
    parser.add_argument(
        "--service",
        action="append",
        help="Sync only this service (repeatable). Default: all services with config files.",
    )
    parser.add_argument(
        "--delete-orphans",
        action="store_true",
        help="Delete SSM parameters under the tenant prefix that are not in Git",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show changes without writing to SSM",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Required in addition to no --dry-run to apply changes",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print(
            "Refusing to apply: pass --dry-run to preview or --apply to execute.",
            file=sys.stderr,
        )
        return 1

    tenant_dir = args.repo_root / "tenants" / args.tenant
    services = args.service if args.service else discover_services(tenant_dir)
    if not services:
        print(f"No services found in {tenant_dir}", file=sys.stderr)
        return 1

    client_kwargs = {"region_name": args.region}
    if args.endpoint_url:
        client_kwargs["endpoint_url"] = args.endpoint_url

    client = boto3.client("ssm", **client_kwargs)

    print(f"Syncing tenant={args.tenant} env={args.env} region={args.region}")
    if args.endpoint_url:
        print(f"  endpoint={args.endpoint_url}")
    print(f"Services: {', '.join(services)}")
    if args.dry_run:
        print("Mode: DRY-RUN")
    else:
        print("Mode: APPLY")

    total_string = 0
    total_secure = 0
    for service in services:
        service_dir = tenant_dir / service
        if not service_dir.is_dir():
            print(f"Skipping {service}: directory not found", file=sys.stderr)
            continue
        print(f"\n{service}")
        s_count, sec_count = sync_service(
            client,
            args.tenant,
            args.env,
            service,
            service_dir,
            args.kms_key_id,
            args.dry_run,
        )
        total_string += s_count
        total_secure += sec_count

    print(f"\nSummary: {total_string} String, {total_secure} SecureString parameters")

    if args.delete_orphans and not args.dry_run:
        print("\nOrphan deletion not yet implemented in this version.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
