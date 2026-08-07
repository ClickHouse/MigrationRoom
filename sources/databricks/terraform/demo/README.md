# Databricks Terraform module — `demo` (Setup Path B, existing workspace)

This module provisions the MigrationRoom demo inside a Databricks workspace
you already have. It creates the Unity Catalog namespace, a dedicated
serverless SQL warehouse, a demo service principal with its own token, and
(optionally) an S3 staging path for the bulk-unload migration path — then
runs `setup_workload.py` to seed TPC-H.

If you don't have a workspace yet, see `../workspace/README.md` instead:
that module creates a fresh serverless workspace and chains into this one.

## What this module creates

| Resource | Purpose |
|---|---|
| `databricks_catalog.demo` | Unity Catalog catalog for the demo (`force_destroy=true`) |
| `databricks_schema.demo` | Schema inside the catalog; together they form `DATABRICKS_NAMESPACE` |
| `databricks_sql_endpoint.demo` | Serverless PRO SQL warehouse — serverless is required for the workload's materialized-view augmentation |
| `databricks_service_principal.demo` | Dedicated principal the playground authenticates as |
| `databricks_obo_token.demo` | 90-day on-behalf-of token for that principal |
| `databricks_grants.demo_catalog` / `databricks_grants.samples` | USE_CATALOG/USE_SCHEMA/SELECT on the demo catalog and on `samples` (the workload CTAS-copies from `samples.tpch`) |
| `databricks_permissions.warehouse_usage` | CAN_USE on the warehouse for the demo principal |
| `null_resource.setup_workload` | Runs `sources/databricks/scripts/setup_workload.py` against the new warehouse to seed TPC-H |
| *(optional, `enable_s3_staging=true`)* `aws_s3_bucket.staging` + lifecycle + public-access-block | Disposable staging bucket for staged Parquet unloads (7-day expiry) |
| *(optional)* `databricks_storage_credential.staging`, `aws_iam_role.staging`, `databricks_external_location.staging` | Unity Catalog external location backing the bucket |
| *(optional)* `aws_iam_user.staging_reader` + access key | Credentials ClickHouse Cloud's `s3()` table function uses to read the staged Parquet — separate from the Unity Catalog credential, which authenticates via an assumed role, not keys |

## Prerequisites

- An existing Databricks workspace with Unity Catalog enabled.
- A personal access token (PAT) for a **workspace admin** — used only to
  provision; the demo principal gets its own token as an output. Set as
  `databricks_token` in `terraform.tfvars`.
- `python3` and `pip` on the machine running `terraform` — the
  `null_resource.setup_workload` provisioner shells out to
  `setup_workload.py`.
- **Only if `enable_s3_staging=true`:** AWS credentials in the shell
  (`AWS_PROFILE`, or `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`) with
  permission to create S3 buckets, an IAM role, an IAM user, and policies.
  This module deliberately does **not** take a `databricks_account_id`
  variable — the `sts:ExternalId` for the IAM trust policy comes from
  `databricks_storage_credential.staging[0].aws_iam_role[0].external_id`,
  which the storage credential generates itself. (The account UUID is
  only needed by the `workspace` module, for the Databricks account API.)

## Usage

```bash
cd sources/databricks/terraform/demo
cp terraform.tfvars.example terraform.tfvars
# Edit: workspace_url, databricks_token

terraform init
terraform apply

# Capture the .env block:
terraform output -raw env_block >> ../../../../.env
```

To enable S3 staging, set `enable_s3_staging = true` (and optionally
`aws_region`) in `terraform.tfvars`, or pass
`-var=enable_s3_staging=true` on the `apply` line, with AWS credentials
already in the shell.

## The re-apply caveat (IAM propagation)

**This is stated plainly because it looks like a bug and isn't one.**
When `enable_s3_staging = true`, `databricks_external_location.staging`
validates itself by actually assuming the freshly-created IAM role. IAM is
eventually consistent — a role that was just created can fail to assume
for several seconds after `aws_iam_role_policy_attachment` reports done.
`time_sleep.iam_propagation` (30s) covers the common case, but if your
`apply` still fails on `databricks_external_location.staging`:

```bash
terraform apply
```

Just run it again. It will succeed once IAM has caught up. Do not delete
the role or the storage credential to "fix" this — nothing is
misconfigured, IAM propagation is just slower than 30 seconds sometimes.

The storage-credential/IAM ordering is inherently circular (the credential
needs the role's ARN, the role's trust policy needs the credential's
external ID), which is why the credential is created first with
`skip_validation = true` and a constructed ARN string rather than a normal
resource reference — there is no way to express this as a straight-line
dependency chain.

## Cost and teardown

A serverless SQL warehouse and (if enabled) S3 storage bill against the
partner's Databricks/AWS account, not against MigrationRoom. `auto_stop_mins`
defaults to 10 minutes of idle time before the warehouse suspends — an idle
warehouse still bills, so don't raise this casually.

```bash
terraform destroy
```

removes everything this module created. The catalog and schema are created
with `force_destroy = true` and the staging bucket with `force_destroy = true`,
so destroy won't wedge on leftover demo tables or objects.

## Serverless egress caveat

If the workspace has a network connectivity configuration (NCC) that
restricts serverless egress, the staging bucket's endpoint must be added
to the allowed list, or the serverless warehouse won't be able to read or
write staged Parquet through the external location. This is a workspace-
level setting outside this module's control — check with whoever manages
the workspace's network policy if `enable_s3_staging=true` and reads/writes
through the external location fail with a network error.

## Honesty about what's been verified

`terraform apply` for this module has **not** been run against a real
workspace as part of building it — there were no Databricks or AWS
credentials available in the environment that wrote it. What was verified
is that the configuration is syntactically and structurally valid:
`terraform fmt -check`, `terraform init -backend=false`, and
`terraform validate` all pass. Provider-side behavior (grants, the OBO
token, the storage-credential/IAM dance, serverless warehouse creation)
has not been exercised end-to-end.
