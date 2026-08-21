#!/usr/bin/env python3
"""
Export existing SSM parameters into this repo's tenant-first JSON structure.

Usage:
  python tools/export-from-ssm.py --tenant mcb --env prod --region us-east-1

This reads SSM parameters under the legacy path:
  /fintech/{env}/{service}/config/{VAR}

and writes them as:
  tenants/{tenant}/{service}/env.json      (String parameters)
  tenants/{tenant}/{service}/secrets.json  (SecureString parameters)

WARNING: secrets.json will contain plaintext secrets. Do not commit it without
encrypting with SOPS first. See README.md for the SOPS workflow.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import boto3


LEGACY_PREFIX = "/fintech"
TENANT_PARAM_PATTERN = re.compile(
    r"^/fintech/(?P<env>[^/]+)/(?P<service>[^/]+)/(?P<scope>[^/]+)/(?P<name>.+)$"
)


def normalize_service_name(name: str) -> str:
    """Map legacy SSM service segment to repo folder name."""
    # Some SSM paths use shortened forms; keep canonical kebab-case.
    return name.lower()


def fetch_parameters(client, path: str) -> list[dict]:
    params: list[dict] = []
    paginator = client.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=path, Recursive=True, WithDecryption=True):
        params.extend(page.get("Parameters", []))
    return params


def group_by_service(parameters: list[dict], env: str) -> dict[str, dict[str, dict]]:
    """Group SSM parameters by service, separating String and SecureString."""
    groups: dict[str, dict[str, dict]] = {}

    for param in parameters:
        name = param["Name"]
        match = TENANT_PARAM_PATTERN.match(name)
        if not match:
            print(f"Skipping non-matching path: {name}")
            continue
        param_env = match.group("env")
        service_raw = match.group("service")
        scope = match.group("scope")
        var_name = match.group("name")

        if param_env != env:
            continue

        service = normalize_service_name(service_raw)
        service_dir = groups.setdefault(service, {"env": {}, "secrets": {}, "scopes": set()})
        service_dir["scopes"].add(scope)

        value = param["Value"]
        param_type = param["Type"]

        if param_type == "SecureString":
            service_dir["secrets"][var_name] = value
        else:
            service_dir["env"][var_name] = value

    return groups


def write_service_files(tenant_dir: Path, service: str, data: dict) -> None:
    service_path = tenant_dir / service
    service_path.mkdir(parents=True, exist_ok=True)

    env_file = service_path / "env.json"
    secrets_file = service_path / "secrets.json"

    with env_file.open("w", encoding="utf-8") as f:
        json.dump(data["env"], f, indent=2, sort_keys=True)
        f.write("\n")

    if data["secrets"]:
        with secrets_file.open("w", encoding="utf-8") as f:
            json.dump(data["secrets"], f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"  wrote {env_file} and {secrets_file} (secrets present)")
    else:
        print(f"  wrote {env_file}")


def write_metadata(tenant_dir: Path, tenant: str, env: str, services: list[str]) -> None:
    import yaml

    meta = {
        "tenant": tenant,
        "environment": env,
        "description": f"Configuration for {tenant} ({env})",
        "services": sorted(services),
        "ssm_prefix": f"/fintech/{tenant}/{env}",
    }
    meta_file = tenant_dir / "metadata.yaml"
    with meta_file.open("w", encoding="utf-8") as f:
        yaml.safe_dump(meta, f, sort_keys=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export SSM parameters to tenant config files")
    parser.add_argument("--tenant", required=True, help="Tenant name (e.g. mcb)")
    parser.add_argument("--env", required=True, help="Environment name (e.g. prod)")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument("--endpoint-url", help="Optional SSM endpoint URL (for LocalStack)")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Path to the tenant-config repo root",
    )
    args = parser.parse_args()

    client_kwargs = {"region_name": args.region}
    if args.endpoint_url:
        client_kwargs["endpoint_url"] = args.endpoint_url

    client = boto3.client("ssm", **client_kwargs)

    legacy_path = f"{LEGACY_PREFIX}/{args.env}"
    print(f"Fetching parameters from {legacy_path} in {args.region}...")
    parameters = fetch_parameters(client, legacy_path)
    print(f"Found {len(parameters)} parameters")

    groups = group_by_service(parameters, args.env)
    print(f"Grouped into {len(groups)} services")

    tenant_dir = args.repo_root / "tenants" / args.tenant
    tenant_dir.mkdir(parents=True, exist_ok=True)

    for service, data in sorted(groups.items()):
        write_service_files(tenant_dir, service, data)

    write_metadata(tenant_dir, args.tenant, args.env, list(groups.keys()))

    print("\nExport complete.")
    print(f"Tenant directory: {tenant_dir}")
    print("REMINDER: encrypt secrets.json with SOPS before committing to Git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
