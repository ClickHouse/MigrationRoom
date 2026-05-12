# Snowflake Terraform module (Setup Path B)

This module provisions a dedicated demo environment in your existing Snowflake
account and sets up the `MIGRATION_DEMO.RETAIL` workload (TPC-H tables +
Snowflake-specific augmentations), all in one `terraform apply`.

Creates:
- Warehouse `MIGRATION_DEMO_WH` (X-SMALL, auto-suspend 60s)
- Database `MIGRATION_DEMO`, schema `RETAIL`
- Role `MIGRATION_DEMO_ROLE` with USAGE on the warehouse + database + schema,
  SELECT on future tables, and IMPORTED PRIVILEGES on `SNOWFLAKE_SAMPLE_DATA`
- User `AI_MIGRATION_DEMO` with a random 32-char password
- Runs `setup_workload.py` to copy 8 TPC-H tables and apply 5 Snowflake-
  specific augmentations (VARIANT column, TIMESTAMP_TZ column, Clustering
  Key, Stream, Dynamic Table).

Teardown is one `terraform destroy`.

## Requirements

- `terraform` CLI ≥ 1.5
- `python3` with `snowflake-connector-python` installed (Terraform calls
  `python3 ../scripts/setup_workload.py` from a `null_resource`)
- A Snowflake account with ACCOUNTADMIN access (or equivalent)

## Usage

```bash
cd sources/snowflake/terraform
cp terraform.tfvars.example terraform.tfvars
# Edit: snowflake_account, admin_user, admin_password

terraform init
terraform apply
```

Expected runtime: ~1 minute (workload setup dominates, ~30s).

## Capture the credentials

```bash
terraform output -raw env_block
```

Append the output to the project root's `.env`, then follow Phase 1 of
[../GUIDE.md](../GUIDE.md).

## Teardown

```bash
terraform destroy
```

Drops the warehouse, database (and everything in it), role, and user.

## Notes

- The `null_resource.setup_workload` triggers on `filemd5()` of both
  `setup_workload.py` and `setup_workload.sql`, so editing either causes a
  re-run on the next `terraform apply`. To force a manual re-run:
  `terraform apply -replace=null_resource.setup_workload`.
- The demo user only has SELECT on the schema's tables. The setup script
  runs as the admin user so it can copy from `SNOWFLAKE_SAMPLE_DATA`.
