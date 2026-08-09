variable "databricks_account_id" {
  description = "Databricks account UUID, from the account console (accounts.cloud.databricks.com) under your profile."
  type        = string
}

variable "databricks_client_id" {
  description = "Application ID of an account-level service principal with the account admin role. Created once by hand in the account console — it is the credential terraform authenticates with, so it cannot be provisioned here."
  type        = string
}

variable "databricks_client_secret" {
  description = "OAuth secret for the account-level service principal."
  type        = string
  sensitive   = true
}

variable "workspace_name" {
  description = "Name of the workspace to create."
  type        = string
  default     = "migrationroom-demo"
}

variable "aws_region" {
  description = "AWS region the serverless workspace runs in."
  type        = string
  default     = "us-east-1"
}

variable "create_metastore" {
  description = "Create a Unity Catalog metastore and assign it to the new workspace. Leave false unless you know the region has none: Databricks auto-provisions one per region for new accounts, and creating a second in the same region fails."
  type        = bool
  default     = false
}

variable "metastore_name" {
  description = "Name for the metastore when create_metastore=true."
  type        = string
  default     = "migrationroom-metastore"
}
