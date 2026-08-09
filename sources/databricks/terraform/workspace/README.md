# Databricks Terraform module — `workspace` (Setup Path B, new environment)

This module creates a **serverless** Databricks workspace from nothing, then
chains into `../demo`, which provisions the MigrationRoom demo objects
inside it. Use this when you don't already have a Databricks workspace; if
you do, use `../demo` directly — see `../demo/README.md`.

## What this module creates

| Resource | Purpose |
|---|---|
| `databricks_mws_workspaces.demo` | A new workspace, `compute_mode = "SERVERLESS"` |
| `databricks_metastore.demo` *(optional, `create_metastore=true`)* | A Unity Catalog metastore |
| `databricks_metastore_assignment.demo` *(optional)* | Assigns that metastore to the new workspace |
| `databricks_mws_permission_assignment.provisioner_admin` | Makes the account service principal a workspace ADMIN, so the chained `demo` apply can authenticate against the new workspace |

Serverless is what makes this path prerequisite-free. A "classic" workspace
needs a cross-account IAM role, a root S3 bucket, and a VPC provisioned
before the workspace can even be created — that's the entire reason
Databricks demos are usually multi-step. `compute_mode = "SERVERLESS"`
skips all of it: no `credentials_id`, no `storage_configuration_id`, no
`aws` provider anywhere in this module.

## The one manual prerequisite

You need a Databricks account, and an account-level service principal with
the **account admin** role, created by hand in the account console
(`accounts.cloud.databricks.com` → Settings → Identity and access → Service
principals). Terraform authenticates *as* this service principal, so it
cannot also create it — that would be the tool provisioning its own
credential. This is "one console visit, then one command," not zero-touch,
and this README won't pretend otherwise.

Once created, generate an OAuth secret for the service principal and note
the application (client) ID and the account UUID (both visible in the
console under your profile).

**Why account admin specifically, not just workspace admin:** the chained
`demo` apply creates a Unity Catalog catalog
(`databricks_catalog.demo`), and `CREATE CATALOG` is a **metastore-level**
privilege, not a workspace-level one — `provisioner_admin` below only
grants workspace ADMIN. Nothing in this module grants a metastore
privilege explicitly, because doing so would need the metastore ID and
would be redundant if the assumption below already holds. The assumption
is: an account admin has implicit metastore-admin capability, and
metastore admins can create catalogs, so authenticating as an account
admin — which you must do anyway for this module's own provider block —
is what makes the later `CREATE CATALOG` succeed. A service principal
scoped to something narrower than account admin will authenticate fine
here and then fail, confusingly, when `demo` tries to create its catalog.

## Usage

```bash
cd sources/databricks/terraform/workspace
cp terraform.tfvars.example terraform.tfvars
# Edit: databricks_account_id, databricks_client_id, databricks_client_secret

terraform init
terraform apply
```

That provisions the workspace only. The recommended entry point is
`make databricks-provision-workspace` from the repo root, which runs this
module **and then** the `demo` module in one command — it reads this
module's `workspace_url` output and this module's own
`terraform.tfvars` (for the OAuth credentials) and writes them into
`sources/databricks/terraform/demo/workspace.auto.tfvars.json`, which
Terraform auto-loads. No copy-pasting a workspace URL between two applies.

## `create_metastore`

Defaults to `false`. Databricks auto-provisions one Unity Catalog metastore
per region for new accounts, and creating a second metastore in the same
region fails outright — so the safe default is to assume one already
exists and let workspace creation attach to it automatically.

To tell whether you need `create_metastore = true`: check the account
console's Catalog section for an existing metastore in your `aws_region`,
or just try the chained `demo` apply — if it fails because the workspace
has no metastore assigned, come back here, set `create_metastore = true`,
and re-apply.

## Cost and teardown

A serverless workspace bills on use — there's no separate "off" state to
leave it in, but nothing runs (and nothing bills) until `demo` provisions
warehouses and workloads inside it. This module has no idle-cost concerns
of its own.

```bash
terraform destroy
```

removes the workspace. Destroy `demo` first, or accept that destroying the
workspace takes everything `demo` created with it (catalogs, warehouses,
tokens — all of it lives inside the workspace).

## Cloud support

This module targets AWS only (`aws_region`, `databricks_mws_workspaces`
with `aws_region` set). GCP serverless workspaces use a `location`
attribute instead of `aws_region` and would need a different resource
shape; Azure workspaces are not created through
`databricks_mws_workspaces` at all — Azure Databricks workspaces are
provisioned through the `azurerm` provider, not the Databricks account
API. Neither is implemented here.

## Honesty about what's been verified

`terraform apply` has **not** been run against a real Databricks account
as part of building this module — there were no Databricks credentials
available in the environment that wrote it. What was verified is that the
configuration is syntactically and structurally valid: `terraform fmt
-check`, `terraform init -backend=false`, and `terraform validate` all
pass against provider `databricks/databricks` v1.124.0. Provider-side
behavior (serverless workspace creation, the permission assignment, the
optional metastore path) has not been exercised end-to-end.

The account-admin → implicit-metastore-admin → `CREATE CATALOG` chain
described above under "The one manual prerequisite" is **documented, not
verified against a live account** — the same lack-of-credentials caveat
applies to it as to everything else in this list.
