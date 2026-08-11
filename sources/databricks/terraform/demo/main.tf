terraform {
  required_version = ">= 1.5.0"
  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = ">= 1.124.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.11.0"
    }
  }
}

# Authenticates with either a PAT or OAuth M2M. Unset fields must be null,
# not "" — the provider treats an empty string as an attempt to use that
# auth method and then fails on ambiguity.
provider "databricks" {
  host          = var.workspace_url
  token         = var.databricks_token != "" ? var.databricks_token : null
  client_id     = var.databricks_client_id != "" ? var.databricks_client_id : null
  client_secret = var.databricks_client_secret != "" ? var.databricks_client_secret : null
}

# The AWS provider can't be lazy: it resolves credentials at configuration
# time even when every aws_* resource is count=0. Feed dummy creds when
# staging is off so the Databricks-only path needs no AWS setup at all.
provider "aws" {
  region                      = var.aws_region
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  access_key                  = var.enable_s3_staging ? null : "AKIAIOSFODNN7EXAMPLE"
  secret_key                  = var.enable_s3_staging ? null : "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
}

locals {
  namespace         = "${var.catalog_name}.${var.schema_name}"
  staging_role_name = "migrationroom-uc-staging"
}

# ── Unity Catalog namespace ──────────────────────────────────────────
# No databricks_catalog/databricks_schema resources here on purpose.
# Serverless workspaces use Default Storage, which has no storage-root
# URL — and storage_root is the only location argument this provider
# version (1.124.0) exposes on databricks_catalog. Trying to create the
# catalog through Terraform fails with "Metastore storage root URL does
# not exist" on Default Storage.
#
# Databricks' own guidance for serverless/Default-Storage workspaces is
# that a plain `CREATE CATALOG IF NOT EXISTS` (no MANAGED LOCATION)
# works fine — and sources/databricks/scripts/setup_workload.sql already
# runs exactly that (`CREATE CATALOG IF NOT EXISTS migration_demo` /
# `CREATE SCHEMA IF NOT EXISTS migration_demo.tpch`) before seeding any
# tables. So the catalog/schema are created by the SQL script via
# null_resource.setup_workload below, not by Terraform. This also works
# unchanged on a storage-rooted (non-Default-Storage) metastore, since
# `IF NOT EXISTS` doesn't care whether a location was supplied — so this
# one fix serves both the new-environment and existing-workspace paths.
#
# Consequence: `terraform destroy` no longer drops the catalog (there's
# no resource for it to destroy). See the Teardown section of
# ../../GUIDE.md and this module's README for the manual
# `DROP CATALOG migration_demo CASCADE;` needed to fully tear down.

# ── Serverless SQL warehouse ─────────────────────────────────────────
# Serverless matters beyond convenience: the workload's materialized-view
# augmentation only works on serverless compute.
resource "databricks_sql_endpoint" "demo" {
  name                      = var.warehouse_name
  cluster_size              = var.warehouse_size
  auto_stop_mins            = var.auto_stop_minutes
  enable_serverless_compute = true
  warehouse_type            = "PRO"
}

# ── Demo principal ───────────────────────────────────────────────────
resource "databricks_service_principal" "demo" {
  display_name          = var.service_principal_name
  allow_cluster_create  = false
  databricks_sql_access = true
  workspace_access      = true
}

# The demo service principal has no permission to use PATs by default —
# only account-admin identities (like the provisioner above) get that
# implicitly. databricks_obo_token needs the principal to hold CAN_USE
# on the workspace's "tokens" authorization object, or it fails with
# "does not have permission to use tokens."
#
# HAZARD: a databricks_permissions resource with authorization = "tokens"
# REPLACES the whole token-usage ACL, not just adds to it. Per the
# provider's own docs: "A given declaration of
# databricks_permissions.token_usage would OVERWRITE permissions to use
# PAT tokens from any existing groups with token usage permissions such
# as the `users` group. To avoid this, be sure to include any desired
# groups in additional access_control blocks." This module also runs
# against pre-existing workspaces (`make databricks-provision`), so
# without the second access_control block below, applying this would
# silently revoke every other user's ability to mint a PAT on someone's
# real workspace — a destructive side effect far outside this module's
# stated purpose. The `users` group is Databricks' built-in
# "everyone" group and is always present, so a literal group name is
# fine here — no data source needed.
resource "databricks_permissions" "token_usage" {
  authorization = "tokens"
  access_control {
    service_principal_name = databricks_service_principal.demo.application_id
    permission_level       = "CAN_USE"
  }
  access_control {
    group_name       = "users"
    permission_level = "CAN_USE"
  }
}

# On-behalf-of token, so the playground authenticates as the service
# principal rather than as whoever ran terraform. Keeps DATABRICKS_TOKEN
# a machine credential and needs no change to DatabricksSource.
resource "databricks_obo_token" "demo" {
  depends_on       = [databricks_permissions.token_usage]
  application_id   = databricks_service_principal.demo.application_id
  comment          = "MigrationRoom playground"
  lifetime_seconds = 90 * 24 * 60 * 60
}

# ── Grants: read-only on the demo namespace ───────────────────────────
# depends_on is required (not implicit) because `catalog` below is a
# plain var, not a reference to a databricks_catalog resource — the
# catalog itself is created by setup_workload.sql, not by Terraform (see
# the comment above). Without this, Terraform could try to grant on a
# catalog that doesn't exist yet.
#
# count mirrors null_resource.setup_workload's: the catalog only comes
# into existence as a side effect of that SQL script running. With
# run_workload_setup = false there is no null_resource instance, the
# depends_on above would be satisfied vacuously (Terraform doesn't wait
# on zero instances), and this grant would otherwise run against a
# catalog nothing ever created — failing with a confusing
# catalog-not-found error instead of skipping cleanly. Gating on the
# same variable makes that failure mode impossible instead of just
# less likely.
resource "databricks_grants" "demo_catalog" {
  count      = var.run_workload_setup ? 1 : 0
  depends_on = [null_resource.setup_workload]
  catalog    = var.catalog_name
  grant {
    principal  = databricks_service_principal.demo.application_id
    privileges = ["USE_CATALOG", "USE_SCHEMA", "SELECT"]
  }
}

# No grant on `samples` here — there used to be one, but it's gone.
# `samples` is a Databricks-managed system catalog; MANAGE on it
# (needed to change its grants) isn't held
# by anyone in a normal account, and Databricks documents `samples` as
# readable without any grant at all — the resource was both unattainable
# and unnecessary. Nothing in this module's runtime path needs it: only
# setup_workload.sql references `samples`, and that SQL runs as the
# provisioning identity (databricks_token.provisioner), not as
# databricks_service_principal.demo. Neither DatabricksSource
# (docker/migration-runner/migrationkit/sources/databricks.py) nor any of
# the six prompts references `samples`.

resource "databricks_permissions" "warehouse_usage" {
  sql_endpoint_id = databricks_sql_endpoint.demo.id
  access_control {
    service_principal_name = databricks_service_principal.demo.application_id
    permission_level       = "CAN_USE"
  }
}

# ── Optional: S3 staging for the bulk-unload path ────────────────────
resource "random_id" "staging_suffix" {
  count       = var.enable_s3_staging ? 1 : 0
  byte_length = 4
}

resource "aws_s3_bucket" "staging" {
  count         = var.enable_s3_staging ? 1 : 0
  bucket        = "migrationroom-databricks-${random_id.staging_suffix[0].hex}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "staging" {
  count                   = var.enable_s3_staging ? 1 : 0
  bucket                  = aws_s3_bucket.staging[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Staged Parquet is disposable — expire it so a forgotten demo doesn't bill.
resource "aws_s3_bucket_lifecycle_configuration" "staging" {
  count  = var.enable_s3_staging ? 1 : 0
  bucket = aws_s3_bucket.staging[0].id
  rule {
    id     = "expire-staged-parquet"
    status = "Enabled"
    filter {
      prefix = "migrationkit/"
    }
    expiration {
      days = 7
    }
  }
}

# The storage credential is created FIRST, with skip_validation, pointing at
# a role ARN built as a string. This is what breaks the circular dependency:
# the IAM role's trust policy needs the credential's external ID, and the
# credential needs the role's ARN. Validation is skipped because the role
# genuinely does not exist yet at this point.
resource "databricks_storage_credential" "staging" {
  count           = var.enable_s3_staging ? 1 : 0
  name            = local.staging_role_name
  skip_validation = true
  aws_iam_role {
    role_arn = "arn:aws:iam::${data.aws_caller_identity.current[0].account_id}:role/${local.staging_role_name}"
  }
  comment = "MigrationRoom staging bucket access (managed by terraform)"
}

data "aws_caller_identity" "current" {
  count = var.enable_s3_staging ? 1 : 0
}

data "databricks_aws_unity_catalog_assume_role_policy" "staging" {
  count          = var.enable_s3_staging ? 1 : 0
  aws_account_id = data.aws_caller_identity.current[0].account_id
  role_name      = local.staging_role_name
  external_id    = databricks_storage_credential.staging[0].aws_iam_role[0].external_id
}

data "databricks_aws_unity_catalog_policy" "staging" {
  count          = var.enable_s3_staging ? 1 : 0
  aws_account_id = data.aws_caller_identity.current[0].account_id
  bucket_name    = aws_s3_bucket.staging[0].bucket
  role_name      = local.staging_role_name
}

resource "aws_iam_policy" "staging" {
  count  = var.enable_s3_staging ? 1 : 0
  name   = "${local.staging_role_name}-policy"
  policy = data.databricks_aws_unity_catalog_policy.staging[0].json
}

resource "aws_iam_role" "staging" {
  count              = var.enable_s3_staging ? 1 : 0
  name               = local.staging_role_name
  assume_role_policy = data.databricks_aws_unity_catalog_assume_role_policy.staging[0].json
}

resource "aws_iam_role_policy_attachment" "staging" {
  count      = var.enable_s3_staging ? 1 : 0
  role       = aws_iam_role.staging[0].name
  policy_arn = aws_iam_policy.staging[0].arn
}

# IAM is eventually consistent: the external location validates by actually
# assuming the role, which fails for a few seconds after role creation.
resource "time_sleep" "iam_propagation" {
  count           = var.enable_s3_staging ? 1 : 0
  depends_on      = [aws_iam_role_policy_attachment.staging]
  create_duration = "30s"
}

resource "databricks_external_location" "staging" {
  count           = var.enable_s3_staging ? 1 : 0
  name            = "migrationroom-staging"
  url             = "s3://${aws_s3_bucket.staging[0].bucket}/migrationkit"
  credential_name = databricks_storage_credential.staging[0].name
  comment         = "MigrationRoom staged Parquet unloads"
  depends_on      = [time_sleep.iam_propagation]
}

resource "databricks_grants" "staging_location" {
  count             = var.enable_s3_staging ? 1 : 0
  external_location = databricks_external_location.staging[0].id
  grant {
    principal  = databricks_service_principal.demo.application_id
    privileges = ["READ_FILES", "WRITE_FILES", "CREATE_EXTERNAL_TABLE"]
  }
}

# An access key for ClickHouse Cloud's s3() table function, which reads the
# staged Parquet. ClickHouse authenticates with keys, not an assumed role,
# so this is separate from the Unity Catalog credential above.
resource "aws_iam_user" "staging_reader" {
  count = var.enable_s3_staging ? 1 : 0
  name  = "migrationroom-databricks-staging-reader"
}

resource "aws_iam_user_policy" "staging_reader" {
  count = var.enable_s3_staging ? 1 : 0
  name  = "read-staged-parquet"
  user  = aws_iam_user.staging_reader[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.staging[0].arn,
          "${aws_s3_bucket.staging[0].arn}/*",
        ]
      },
    ]
  })
}

resource "aws_iam_access_key" "staging_reader" {
  count = var.enable_s3_staging ? 1 : 0
  user  = aws_iam_user.staging_reader[0].name
}

# ── Workload ─────────────────────────────────────────────────────────
# The workload script needs a PAT and needs write privileges (CREATE
# CATALOG), so it runs as the provisioning identity, not as the demo
# service principal. One hour is ample and limits the blast radius if the
# state file leaks.
resource "databricks_token" "provisioner" {
  count            = var.run_workload_setup ? 1 : 0
  comment          = "MigrationRoom workload setup (short-lived)"
  lifetime_seconds = 3600
}

# Runs the same script as `make databricks-setup`, against the warehouse
# just created. Triggers on the warehouse id so a recreated warehouse
# re-seeds.
resource "null_resource" "setup_workload" {
  count = var.run_workload_setup ? 1 : 0

  triggers = {
    warehouse_id = databricks_sql_endpoint.demo.id
    namespace    = local.namespace
  }

  provisioner "local-exec" {
    working_dir = "${path.module}/../../../.."
    command     = <<-EOT
      python3 -m pip install --quiet -r sources/databricks/scripts/requirements.txt
      python3 sources/databricks/scripts/setup_workload.py
    EOT
    environment = {
      DATABRICKS_HOST      = var.workspace_url
      DATABRICKS_HTTP_PATH = databricks_sql_endpoint.demo.odbc_params[0].path
      DATABRICKS_TOKEN     = databricks_token.provisioner[0].token_value
    }
  }

  # No depends_on needed: the catalog/schema this SQL creates
  # (`CREATE CATALOG IF NOT EXISTS` / `CREATE SCHEMA IF NOT EXISTS` in
  # setup_workload.sql) aren't Terraform resources anymore — see the
  # comment above databricks_sql_endpoint.demo. The warehouse and
  # provisioner token dependencies are already implicit through the
  # interpolations in `environment` above.
}
