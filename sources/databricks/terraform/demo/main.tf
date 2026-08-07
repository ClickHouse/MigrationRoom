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
resource "databricks_catalog" "demo" {
  name    = var.catalog_name
  comment = "MigrationRoom demo catalog — TPC-H with Databricks-specific augmentations."
  # Keeps `terraform destroy` from failing on a catalog that still has
  # tables in it. This is a throwaway demo catalog by construction.
  force_destroy = true
}

resource "databricks_schema" "demo" {
  catalog_name  = databricks_catalog.demo.name
  name          = var.schema_name
  comment       = "TPC-H SF1 copied from samples.tpch, plus Delta augmentations."
  force_destroy = true
}

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

# On-behalf-of token, so the playground authenticates as the service
# principal rather than as whoever ran terraform. Keeps DATABRICKS_TOKEN
# a machine credential and needs no change to DatabricksSource.
resource "databricks_obo_token" "demo" {
  application_id   = databricks_service_principal.demo.application_id
  comment          = "MigrationRoom playground"
  lifetime_seconds = 90 * 24 * 60 * 60
}

# ── Grants: read-only on the demo namespace + the samples catalog ─────
resource "databricks_grants" "demo_catalog" {
  catalog = databricks_catalog.demo.name
  grant {
    principal  = databricks_service_principal.demo.application_id
    privileges = ["USE_CATALOG", "USE_SCHEMA", "SELECT"]
  }
}

# The workload is built by CTAS out of `samples`, so the principal needs
# to read it. `samples` is a system catalog and always present.
resource "databricks_grants" "samples" {
  catalog = "samples"
  grant {
    principal  = databricks_service_principal.demo.application_id
    privileges = ["USE_CATALOG", "USE_SCHEMA", "SELECT"]
  }
}

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

  depends_on = [
    databricks_schema.demo,
    databricks_grants.samples,
  ]
}
