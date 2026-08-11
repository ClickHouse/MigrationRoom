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
`demo` apply's workload-seeding step creates the Unity Catalog catalog via
plain SQL (`CREATE CATALOG IF NOT EXISTS`, run by `setup_workload.sql` —
see `../demo/README.md`; there is no `databricks_catalog` Terraform
resource in `demo` anymore), and `CREATE CATALOG` is a **metastore-level**
privilege either way, not a workspace-level one — `provisioner_admin`
below only grants workspace ADMIN. Nothing in this module grants a
metastore privilege explicitly, because doing so would need the metastore
ID and would be redundant if the assumption below already holds. The
assumption is: an account admin has implicit metastore-admin capability,
and metastore admins can create catalogs, so authenticating as an account
admin — which you must do anyway for this module's own provider block —
is what makes the later `CREATE CATALOG` succeed. A service principal
scoped to something narrower than account admin will authenticate fine
here and then fail, confusingly, when `demo` tries to create its catalog.
**Confirmed live** (see "Honesty about what's been verified" below): the
chained `demo` apply's `CREATE CATALOG` succeeded using the account-admin
identity's credentials, on the first attempt.

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

`terraform apply` **has** been run against a real Databricks account, via
`make databricks-provision-workspace` — and this module is the part of
the whole feature that applied cleanly on the first attempt, no code
changes needed. `databricks_mws_workspaces.demo` created a serverless
workspace in roughly 47 seconds; `databricks_mws_permission_assignment.provisioner_admin`
granted the provisioning service principal workspace ADMIN; and the
chained hand-off into `../demo` worked end-to-end — outputs captured,
`workspace.auto.tfvars.json` written by the merge script, and the `demo`
apply picked it up with no manual copy-pasting. `terraform fmt -check`,
`terraform init -backend=false`, and `terraform validate` all also pass
against provider `databricks/databricks` v1.124.0, as before.

What's still unexercised: the run took the `create_metastore = false`
path — the account already had a Unity Catalog metastore in the region —
so the `databricks_metastore` / `databricks_metastore_assignment` branch
(`create_metastore = true`) remains untested against a live account.
`terraform destroy` has **not** been run for this module either. And this
is one account, one region (`us-east-1`), one observation — not a
guarantee for other accounts, regions, or (per "Cloud support" above)
other clouds.

The account-admin → implicit-metastore-admin → `CREATE CATALOG` chain
described above under "The one manual prerequisite" **is now confirmed**,
not just documented: the chained `demo` apply's `CREATE CATALOG IF NOT
EXISTS` (run by `setup_workload.sql` against the workspace, using
credentials derived from this module's account-admin identity) succeeded
on the first attempt. The assumption held on this account; it hasn't been
tested against a service principal scoped to something narrower than
account admin, since that's precisely the configuration this README
recommends against.
