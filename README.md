# Fintech Tenant Configuration

Git-backed source of truth for per-tenant, per-service environment configuration
synced to AWS Systems Manager Parameter Store (SSM).

## Repository layout

```text
.
├── README.md
├── schema/
│   └── required-env-vars.json      # canonical required keys per service
├── tenants/
│   └── mcb/
│       ├── metadata.yaml           # tenant metadata and SSM prefix
│       ├── accs-server/
│       │   ├── env.json            # plain String parameters
│       │   └── secrets.json        # SecureString parameters (encrypt before commit)
│       ├── api-aggregator-service/
│       │   ├── env.json
│       │   └── secrets.json
│       └── ...
└── tools/
    ├── export-from-ssm.py          # one-time export from legacy SSM paths
    ├── sync-ssm.py               # Git -> SSM sync
    └── validate.py                 # validate tenant config before sync
```

## Parameter path convention

New tenant-first SSM path:

```text
/fintech/{tenant}/{env}/{service}/config/{VAR}
```

Example:

```text
/fintech/mcb/prod/api-aggregator-service/config/DB_NAME_BUSINESS_RULES
```

This replaces the legacy service-first path:

```text
/fintech/prod/{service}/config/{VAR}
```

## Security: handling secrets

**Do not commit plaintext `secrets.json` to Git.**

This repository uses [Mozilla SOPS](https://github.com/getsops/sops) to encrypt
`secrets.json` files with AWS KMS. The committed file contains ciphertext; the
cleartext is only visible when SOPS decrypts it.

### SOPS workflow

Install SOPS and ensure AWS credentials are available. The `.sops.yaml`
file in this repo already points to the dedicated KMS key:

```yaml
arn:aws:kms:us-east-1:127214157504:key/5e9cc23a-74da-4161-b8b0-9e7bbf360b98
(alias/fintech-tenant-config)
```

Edit a secrets file:

```bash
sops tenants/mcb/api-aggregator-service/secrets.json
```

SOPS decrypts on open and re-encrypts on save.

**CI/automation:** `tools/sync-ssm.py` automatically decrypts SOPS-encrypted
`secrets.json` before writing to SSM, provided the `sops` binary is on PATH or
in one of the common fallback locations (`~/tools/sops.exe`,
`C:\Program Files\sops\sops.exe`, Chocolatey bin).

For local development and LocalStack testing, plaintext `secrets.json` is
acceptable. Before committing to Git, either:

1. Encrypt with SOPS, or
2. Leave `secrets.json` out of Git (add to `.gitignore`) and inject secrets
   through a separate secure mechanism.

## Workflow

### 1. Validate

```bash
python tools/validate.py --tenant mcb
```

### 2. Dry-run sync

```bash
python tools/sync-ssm.py --tenant mcb --env prod --region us-east-1 --dry-run
```

### 3. Apply sync

```bash
python tools/sync-ssm.py --tenant mcb --env prod --region us-east-1 --apply
```

### 4. Render env files on EC2

The `fintech-ec2-deployment` `render-env.sh` script fetches parameters for the
current tenant and writes `.env` files:

```bash
TENANT=mcb ENV=prod bash scripts/render-env.sh
```

## LocalStack testing

Start LocalStack and run the sync against it. Use `us-east-1` to match the
production AWS region and the LocalStack container default:

```bash
python tools/sync-ssm.py \
  --tenant mcb \
  --env prod \
  --endpoint-url http://localhost:4566 \
  --region us-east-1 \
  --apply
```

Then verify:

```bash
aws --endpoint-url http://localhost:4566 ssm get-parameters-by-path \
  --path /fintech/mcb/prod --recursive --region us-east-1
```

## Adding a new tenant

1. Copy `tenants/mcb` to `tenants/<new-tenant>`.
2. Update `tenants/<new-tenant>/metadata.yaml`.
3. Change database names, queue URLs, and other tenant-specific values.
4. Run `validate.py` and `sync-ssm.py`.

## Required environment variables per service

See `schema/required-env-vars.json`. This is used by `validate.py` to ensure every
tenant has the same set of configuration keys.
