#!/usr/bin/env python3
"""
Validate tenant configuration files before they are synced to SSM.

Checks:
  - env.json and secrets.json are valid JSON
  - No duplicate keys across env.json and secrets.json
  - All required variables for each service are present
  - No placeholder values like "<set-in-ssm>" or empty strings
  - Secrets files are not obviously plaintext if SOPS is configured

Usage:
  python tools/validate.py --tenant mcb
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


REQUIRED_VARS = {
    "api-aggregator-service": ["NODE_ENV", "PORT", "DB_HOST", "DB_PORT", "DB_USER", "DB_NAME_BUSINESS_RULES"],
    "business-rules-engine": ["NODE_ENV", "PORT", "DB_HOST", "DB_PORT", "DB_USER", "DB_NAME_BUSINESS_RULES", "DB_NAME_PRIMARY"],
    "data-ingestion-service-express": ["NODE_ENV", "PORT", "DB_HOST", "DB_PORT", "DB_USER", "DB_NAME_PRIMARY"],
    "file-data-enrichment-engine": ["NODE_ENV", "PORT", "DB_HOST", "DB_PORT", "DB_USER", "DB_NAME_PRIMARY", "DB_NAME_BUSINESS_RULES", "DB_NAME_ACCS"],
    "ecs-taeengine": ["DB_NAME_PRIMARY"],
}


PLACEHOLDER_PATTERNS = ["<", ">", "TODO", "FIXME", "set-in-ssm"]


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_service(tenant_dir: Path, service: str) -> list[str]:
    errors: list[str] = []
    service_dir = tenant_dir / service
    env_file = service_dir / "env.json"
    secrets_file = service_dir / "secrets.json"

    env_vars: dict = {}
    secrets_vars: dict = {}

    if env_file.exists():
        try:
            env_vars = load_json(env_file)
        except json.JSONDecodeError as exc:
            errors.append(f"{service}/env.json: invalid JSON - {exc}")
    else:
        errors.append(f"{service}: env.json not found")

    if secrets_file.exists():
        try:
            raw_text = secrets_file.read_text(encoding="utf-8")
            raw_data = json.loads(raw_text)
            if "sops" not in raw_data:
                errors.append(
                    f"{service}/secrets.json: plaintext secrets are not allowed in Git. "
                    "Encrypt with SOPS before committing."
                )
            secrets_vars = load_json(secrets_file)
        except json.JSONDecodeError as exc:
            errors.append(f"{service}/secrets.json: invalid JSON - {exc}")

    duplicates = set(env_vars.keys()) & set(secrets_vars.keys())
    if duplicates:
        errors.append(f"{service}: duplicate keys in env.json and secrets.json: {', '.join(sorted(duplicates))}")

    all_vars = {**env_vars, **secrets_vars}

    required = REQUIRED_VARS.get(service, [])
    missing = [k for k in required if k not in all_vars]
    if missing:
        errors.append(f"{service}: missing required keys: {', '.join(missing)}")

    for key, value in all_vars.items():
        if value is None or value == "":
            errors.append(f"{service}/{key}: empty value")
            continue
        str_value = str(value)
        for pattern in PLACEHOLDER_PATTERNS:
            if pattern in str_value:
                errors.append(f"{service}/{key}: contains placeholder '{pattern}'")
                break

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate tenant config")
    parser.add_argument("--tenant", required=True, help="Tenant name")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Path to the tenant-config repo root",
    )
    args = parser.parse_args()

    tenant_dir = args.repo_root / "tenants" / args.tenant
    if not tenant_dir.exists():
        print(f"Tenant directory not found: {tenant_dir}", file=sys.stderr)
        return 1

    services = sorted(
        d.name
        for d in tenant_dir.iterdir()
        if d.is_dir() and ((d / "env.json").exists() or (d / "secrets.json").exists())
    )

    all_errors: list[str] = []
    for service in services:
        all_errors.extend(validate_service(tenant_dir, service))

    if all_errors:
        print(f"Validation failed for tenant {args.tenant}:")
        for err in all_errors:
            print(f"  - {err}")
        return 1

    print(f"Validation passed for tenant {args.tenant} ({len(services)} services)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
