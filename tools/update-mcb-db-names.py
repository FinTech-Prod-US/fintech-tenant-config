import json
from pathlib import Path

TENANT = "mcb"
TENANT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "tenants" / TENANT

DB_NAME_PRIMARY = "mcb_check_payment_platform"
DB_NAME_BUSINESS_RULES = "mcb_business_rules"
DB_NAME_DUP_DETECT = "mcb_dup_detect"
DB_NAME_ACCS = "mcb_check_payment_platform"

# Map service folder name -> required DB name variables
SERVICE_DB_VARS = {
    "api-aggregator-service": ["DB_NAME_BUSINESS_RULES"],
    "business-rules-engine": ["DB_NAME_PRIMARY", "DB_NAME_BUSINESS_RULES"],
    "data-ingestion-service-express": ["DB_NAME_PRIMARY"],
    "file-data-enrichment-engine": ["DB_NAME_PRIMARY", "DB_NAME_BUSINESS_RULES", "DB_NAME_ACCS"],
    "ecs-dupdetectengine": ["DB_NAME_DUP_DETECT"],
    "ecs-exceptioningestionengine": ["DB_NAME_PRIMARY"],
    "ecs-taeengine": ["DB_NAME_PRIMARY"],
    "ecs-posting-eod-extract-engine": ["DB_NAME_PRIMARY"],
    "ecs-cashletter-service-engine": ["DB_NAME_PRIMARY", "DB_NAME_BUSINESS_RULES"],
    "ecs-image-archive-engine": ["DB_NAME_PRIMARY", "DB_NAME_BUSINESS_RULES"],
    "ecs-data-services-api-core": ["DB_NAME_PRIMARY", "DB_NAME_BUSINESS_RULES"],
    "ecs-dataqualityvalidator": ["DB_NAME_PRIMARY"],  # no direct DB, but used as default?
    "ecs-cde-engine": ["DB_NAME_PRIMARY"],
    "ecs-image-quality-validator": ["DB_NAME_PRIMARY"],
    "accs-server": ["DB_NAME_PRIMARY", "DB_NAME_BUSINESS_RULES"],
    "accs-ui": [],  # frontend, no DB
    "moov-io-accs": [],
    "moov-io-fed-accs": [],
}

DB_VAR_VALUES = {
    "DB_NAME_PRIMARY": DB_NAME_PRIMARY,
    "DB_NAME_BUSINESS_RULES": DB_NAME_BUSINESS_RULES,
    "DB_NAME_DUP_DETECT": DB_NAME_DUP_DETECT,
    "DB_NAME_ACCS": DB_NAME_ACCS,
}

for svc_dir in sorted(TENANT_CONFIG_DIR.iterdir()):
    if not svc_dir.is_dir():
        continue
    svc = svc_dir.name
    env_file = svc_dir / "env.json"
    if not env_file.exists():
        continue

    data = json.loads(env_file.read_text(encoding="utf-8"))
    required = SERVICE_DB_VARS.get(svc, [])

    changed = False
    for var in required:
        expected = DB_VAR_VALUES[var]
        if var not in data or data[var] != expected:
            data[var] = expected
            changed = True
            print(f"  updated {svc}/env.json: {var}={expected}")

    # Remove stale non-prefixed DB names that are not in required list
    for var in list(data.keys()):
        if var.startswith("DB_NAME_") and var not in required:
            del data[var]
            changed = True
            print(f"  removed {svc}/env.json: {var}")

    if changed:
        env_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    else:
        print(f"  ok {svc}/env.json")

print("Done")
