variable "workspace_url" {
  description = "Databricks workspace URL, e.g. https://dbc-xxxxxxxx-xxxx.cloud.databricks.com. When chaining from the workspace module this is supplied automatically via workspace.auto.tfvars.json."
  type        = string
}

variable "databricks_token" {
  description = "Personal access token for a workspace admin. Alternative to databricks_client_id/secret — supply exactly one of the two. Used only to provision; the demo principal gets its own token as an output."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = var.databricks_token != "" || var.databricks_client_id != ""
    error_message = "Set either databricks_token, or databricks_client_id and databricks_client_secret."
  }
}

variable "databricks_client_id" {
  description = "Application ID of a service principal with workspace-admin on workspace_url. Alternative to databricks_token — supply exactly one of the two."
  type        = string
  default     = ""
}

variable "databricks_client_secret" {
  description = "OAuth secret matching databricks_client_id."
  type        = string
  sensitive   = true
  default     = ""
}

variable "catalog_name" {
  description = "Unity Catalog catalog to create for the demo workload. Not currently customizable: sources/databricks/scripts/setup_workload.sql hard-codes migration_demo.tpch in all of its statements, so changing this value only creates a second, ungranted catalog and leaves the seeded data behind in migration_demo. Templating the SQL is a tracked follow-up, not something to do at this merge gate."
  type        = string
  default     = "migration_demo"

  validation {
    condition     = var.catalog_name == "migration_demo"
    error_message = "catalog_name must stay \"migration_demo\": sources/databricks/scripts/setup_workload.sql hard-codes the migration_demo.tpch namespace in all 25 of its statements. Changing this variable does not retarget the seeding script — it creates and grants a second, empty catalog while the TPC-H data still lands in migration_demo.tpch, ungranted. Templating setup_workload.sql to honor this variable is a follow-up, not something to change here."
  }
}

variable "schema_name" {
  description = "Schema inside the catalog. Together with catalog_name this becomes DATABRICKS_NAMESPACE. Not currently customizable: sources/databricks/scripts/setup_workload.sql hard-codes migration_demo.tpch in all of its statements, so changing this value only creates a second, ungranted schema and leaves the seeded data behind in tpch. Templating the SQL is a tracked follow-up, not something to do at this merge gate."
  type        = string
  default     = "tpch"

  validation {
    condition     = var.schema_name == "tpch"
    error_message = "schema_name must stay \"tpch\": sources/databricks/scripts/setup_workload.sql hard-codes the migration_demo.tpch namespace in all 25 of its statements. Changing this variable does not retarget the seeding script — it creates and grants a second, empty schema while the TPC-H data still lands in migration_demo.tpch, ungranted. Templating setup_workload.sql to honor this variable is a follow-up, not something to change here."
  }
}

variable "warehouse_name" {
  description = "Name of the dedicated serverless SQL warehouse."
  type        = string
  default     = "migrationroom-demo"
}

variable "warehouse_size" {
  description = "SQL warehouse cluster size. 2X-Small is enough for TPC-H SF1."
  type        = string
  default     = "2X-Small"
}

variable "auto_stop_minutes" {
  description = "Idle minutes before the warehouse stops. Keep low — an idle warehouse bills."
  type        = number
  default     = 10
}

variable "service_principal_name" {
  description = "Display name of the service principal the playground authenticates as."
  type        = string
  default     = "migrationroom-demo-sp"
}

variable "run_workload_setup" {
  description = "Run sources/databricks/scripts/setup_workload.py after provisioning to create the TPC-H workload. Requires python3 and pip on the machine running terraform."
  type        = bool
  default     = true
}

variable "enable_s3_staging" {
  description = "Provision an S3 bucket, IAM role, Unity Catalog storage credential, and external location for the S3-staged migration path. Requires AWS credentials in the shell at apply time — that is a second cloud prerequisite, which is why this defaults to false. Migrations work without it via the direct batch path."
  type        = bool
  default     = false
}

variable "aws_region" {
  description = "AWS region for the staging bucket. Ignored when enable_s3_staging=false."
  type        = string
  default     = "us-east-1"
}
