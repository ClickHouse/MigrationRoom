terraform {
  required_version = ">= 1.5.0"
  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = ">= 1.124.0"
    }
  }
}

# Account-level provider. Note the host: the account API lives at
# accounts.cloud.databricks.com, not at a workspace URL.
provider "databricks" {
  host          = "https://accounts.cloud.databricks.com"
  account_id    = var.databricks_account_id
  client_id     = var.databricks_client_id
  client_secret = var.databricks_client_secret
}

# A serverless workspace needs NO prerequisite cloud resources — no
# cross-account IAM role, no root S3 bucket, no VPC. That is the entire
# reason the new-environment path is one command: the two arguments that
# would normally point at that IAM role and root bucket must be left
# unset entirely for compute_mode=SERVERLESS (the provider rejects them
# if set).
resource "databricks_mws_workspaces" "demo" {
  account_id     = var.databricks_account_id
  workspace_name = var.workspace_name
  aws_region     = var.aws_region
  compute_mode   = "SERVERLESS"
}

# Optional metastore. Most accounts already have one per region and a
# second in the same region is rejected, so this is opt-in.
resource "databricks_metastore" "demo" {
  count         = var.create_metastore ? 1 : 0
  name          = var.metastore_name
  region        = var.aws_region
  force_destroy = true
}

resource "databricks_metastore_assignment" "demo" {
  count        = var.create_metastore ? 1 : 0
  workspace_id = databricks_mws_workspaces.demo.workspace_id
  metastore_id = databricks_metastore.demo[0].id
}

# The account service principal terraform is authenticating as needs to be
# a workspace admin, otherwise the chained `demo` apply cannot reach the
# new workspace at all.
data "databricks_service_principal" "provisioner" {
  application_id = var.databricks_client_id
}

resource "databricks_mws_permission_assignment" "provisioner_admin" {
  workspace_id = databricks_mws_workspaces.demo.workspace_id
  principal_id = data.databricks_service_principal.provisioner.sp_id
  permissions  = ["ADMIN"]
}
