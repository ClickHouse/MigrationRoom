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
| `databricks_sql_endpoint.demo` | Serverless PRO SQL warehouse — serverless is required for the workload's materialized-view augmentation |
| `databricks_service_principal.demo` | Dedicated principal the playground authenticates as |
| `databricks_permissions.token_usage` | Grants the demo principal (and preserves the `users` group's) `CAN_USE` on PAT tokens — required before `databricks_obo_token.demo` can be created |
| `databricks_obo_token.demo` | 90-day on-behalf-of token for that principal |
| `databricks_grants.demo_catalog` | USE_CATALOG/USE_SCHEMA/SELECT on the demo catalog for the demo principal — only created when `run_workload_setup=true` (see below) |
| `databricks_permissions.warehouse_usage` | CAN_USE on the warehouse for the demo principal |
| `null_resource.setup_workload` | Only when `run_workload_setup=true`. Runs `sources/databricks/scripts/setup_workload.py` against the new warehouse to seed TPC-H — this is also what creates the `migration_demo.tpch` catalog/schema (see below), not a Terraform resource |
| *(optional, `enable_s3_staging=true`)* `aws_s3_bucket.staging` + lifecycle + public-access-block | Disposable staging bucket for staged Parquet unloads (7-day expiry) |
| *(optional)* `databricks_storage_credential.staging`, `aws_iam_role.staging`, `databricks_external_location.staging` | Unity Catalog external location backing the bucket |
| *(optional)* `aws_iam_user.staging_reader` + access key | Credentials ClickHouse Cloud's `s3()` table function uses to read the staged Parquet — separate from the Unity Catalog credential, which authenticates via an assumed role, not keys |

No `databricks_catalog`/`databricks_schema` resource here on purpose:
serverless workspaces use Default Storage, which has no storage-root URL
to hand to `databricks_catalog`'s `storage_root` argument (the only
location argument this provider version exposes). Databricks' own
guidance for Default Storage is that a plain `CREATE CATALOG IF NOT
EXISTS` needs no location, and `setup_workload.sql` already does exactly
that — so the catalog/schema are created by the SQL script, not by
Terraform. This works unchanged on a storage-rooted metastore too.
One consequence: `terraform destroy` no longer drops the catalog — see
"Cost and teardown" below.

No grant on `samples` either (a previous version of this module had
one): `samples` is a Databricks-managed system catalog, `MANAGE` on it
(needed to change its grants) isn't held
by anyone in a normal account, and Databricks documents `samples` as
readable without any grant. Nothing in this module's runtime path needs
an explicit grant on it — only `setup_workload.sql` references `samples`,
and that SQL runs as the provisioning identity, not the demo principal.

**`run_workload_setup` means more than "seed data."** Since the catalog
is created by `setup_workload.sql` and not by Terraform (above),
`run_workload_setup=false` means the catalog/schema never get created
*and* `databricks_grants.demo_catalog` is skipped (it's gated on the
same variable — otherwise it would fail trying to grant on a catalog
nothing created). If you set this to `false`, you are responsible for
creating `migration_demo.tpch` and granting the demo service principal
`USE_CATALOG`/`USE_SCHEMA`/`SELECT` on it yourself, before the
playground tries to use it — otherwise you end up with a service
principal that has a valid token and no access to anything.

## Prerequisites

> **Warning — shared/existing workspaces: this module can revoke other
> people's PAT access.** `databricks_permissions.token_usage` (created
> by this module — see "What this module creates" above) uses
> `authorization = "tokens"`, which **replaces the workspace's entire
> token-usage ACL**, not just adds to it. This module explicitly
> preserves `CAN_USE` for the demo service principal and for the
> built-in `users` group — but if any **other** principal (a specific
> user, or a group like `data-engineers`) currently holds `CAN_USE` or
> `CAN_MANAGE` on tokens in the target workspace, applying this module
> silently revokes it and deletes their active tokens. Terraform has no
> way to discover and preserve an ACL it wasn't told about.
>
> **Before running `terraform apply` against a workspace anyone else
> already uses**, check who currently has token access — workspace
> Settings → Advanced → Personal Access Tokens in the UI, or
> `GET /api/2.0/permissions/authorization/tokens` — and note any
> principal other than the `users` group. If you find one, add it as an
> additional `access_control` block in `databricks_permissions.token_usage`
> in `main.tf` before applying, the same way `users` is preserved there.

- An existing Databricks workspace with Unity Catalog enabled.
- A personal access token (PAT) for a **workspace admin** — used only to
  provision; the demo principal gets its own token as an output. Set as
  `databricks_token` in `terraform.tfvars`. Alternatively,
  `databricks_client_id`/`databricks_client_secret` (OAuth) for a
  service principal — this is how the chained call from `../workspace`
  authenticates, since a freshly created workspace has no PAT yet. Supply
  exactly one of the two.
- **If chaining from `../workspace`:** `setup_workload.sql` (run by
  `null_resource.setup_workload`) is what creates the `migration_demo`
  catalog and `tpch` schema, via `CREATE CATALOG IF NOT EXISTS` /
  `CREATE SCHEMA IF NOT EXISTS` — Terraform itself no longer creates
  them (see "What this module creates" above). That still requires
  `CREATE CATALOG`, a metastore-level privilege, on whichever identity
  the provisioner PAT (`databricks_token.provisioner`) runs as. The
  OAuth identity therefore needs to be the account admin service
  principal described in `../workspace/README.md` (account admin
  implies metastore-admin capability) — a principal scoped to only
  workspace-admin will authenticate but fail when the SQL script tries
  to create the catalog.
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

removes everything this module created. The staging bucket is created with
`force_destroy = true`, so destroy won't wedge on leftover objects in it.

**Regression: `terraform destroy` no longer removes the catalog.** Because
the `migration_demo` catalog/schema are created by `setup_workload.sql`
(see "What this module creates" above) rather than by a Terraform
resource, `terraform destroy` has nothing to target and leaves them
behind. Drop them by hand if you want a full teardown:

```sql
DROP CATALOG migration_demo CASCADE;
```

Run that against the workspace (e.g. via the SQL editor, or `databricks
sql`) as an identity with `MANAGE` on the catalog — the setup/provisioner
identity or an account/workspace admin both qualify.

## Serverless egress caveat

If the workspace has a network connectivity configuration (NCC) that
restricts serverless egress, the staging bucket's endpoint must be added
to the allowed list, or the serverless warehouse won't be able to read or
write staged Parquet through the external location. This is a workspace-
level setting outside this module's control — check with whoever manages
the workspace's network policy if `enable_s3_staging=true` and reads/writes
through the external location fail with a network error.

## Honesty about what's been verified

This module has now been applied against a real Databricks account (via
the chained `make databricks-provision-workspace` path) and reaches the
workload-seeding step; three defects it surfaced there are fixed in
`9777de2` and one more in `883ea3c`. A fifth error encountered on that
run was a local Python TLS trust-store problem, not a defect in this
module. For the verbatim errors, root causes, and fixes — including the
one that needs an action on your own machine — see
[`../../GUIDE.md`](../../GUIDE.md#troubleshooting)'s Troubleshooting
section and its "Honesty about what's been verified" section for exactly
how far the live run got (`setup_workload.sql` statement 15 of 26).
`terraform fmt -check`, `terraform init -backend=false`, and `terraform
validate` all still pass, but that was never the open question for this
module's live behavior.
