#!/usr/bin/env python3
"""
Add tenant-prefixed DB name env vars to MCB tenant config.

This is a one-time migration helper. It reads the exported env.json files
and adds the new DB_NAME_* variables introduced by the tenant DB unification.

For MCB we keep the legacy DB names (no prefix) to maintain backward
compatibility. New tenants can use prefixed names like <tenant>_check_payment_platform.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TENANT_DIR = REPO_ROOT / "tenants" / "mcb"

# Map service -> {var: value}
ADDITIONS = {
    "accs-server": {
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
    "api-aggregator-service": {
        "DB_NAME_BUSINESS_RULES": "business_rules",
    },
    "business-rules-engine": {
        "DB_NAME_BUSINESS_RULES": "business_rules",
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
    "data-ingestion-service-express": {
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
    "file-data-enrichment-engine": {
        "DB_NAME_PRIMARY": "check_payment_platform",
        "DB_NAME_BUSINESS_RULES": "business_rules",
        "DB_NAME_ACCS": "check_payment_platform",
    },
    "ecs-cashletter-service-engine": {
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
    "ecs-cde-engine": {
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
    "ecs-data-services-api-core": {
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
    "ecs-dataqualityvalidator": {
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
    "ecs-dupdetectengine": {
        "DB_NAME_DUP_DETECT": "dup_detect",
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
    "ecs-exceptioningestionengine": {
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
    "ecs-image-archive-engine": {
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
    "ecs-image-quality-validator": {
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
    "ecs-posting-eod-extract-engine": {
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
    "ecs-taeengine": {
        "DB_NAME_PRIMARY": "check_payment_platform",
    },
}


def update_env_json(service_dir: Path, additions: dict) -> bool:
    env_file = service_dir / "env.json"
    if not env_file.exists():
        return False

    with env_file.open("r", encoding="utf-8") as f:
        data = json.load(f)

    changed = False
    for key, value in additions.items():
        if key not in data:
            data[key] = value
            changed = True

    if changed:
        with env_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")

    return changed


def main() -> int:
    for service, additions in sorted(ADDITIONS.items()):
        service_dir = TENANT_DIR / service
        if not service_dir.is_dir():
            print(f"SKIP: {service} directory not found")
            continue
        if update_env_json(service_dir, additions):
            print(f"UPDATED: {service}/env.json")
        else:
            print(f"NO CHANGE: {service}/env.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
