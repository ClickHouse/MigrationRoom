output "env_block" {
  description = "Paste this block into the project's .env file."
  sensitive   = true
  value       = <<EOT

# ── Databricks Source (provisioned by terraform) ──────────────
DATABRICKS_HOST=${var.workspace_url}
DATABRICKS_HTTP_PATH=${databricks_sql_endpoint.demo.odbc_params[0].path}
DATABRICKS_TOKEN=${databricks_obo_token.demo.token_value}
DATABRICKS_NAMESPACE=${local.namespace}
${var.enable_s3_staging ? "STAGING_S3_BUCKET=${aws_s3_bucket.staging[0].bucket}\nSTAGING_S3_REGION=${var.aws_region}\nSTAGING_S3_PREFIX=migrationkit\nSTAGING_S3_ACCESS_KEY_ID=${aws_iam_access_key.staging_reader[0].id}\nSTAGING_S3_SECRET_ACCESS_KEY=${aws_iam_access_key.staging_reader[0].secret}" : "# (no S3 staging — set enable_s3_staging=true to provision it)"}
EOT
}

output "summary" {
  description = "Human-readable summary of what was created."
  value = {
    workspace_url     = var.workspace_url
    catalog           = var.catalog_name
    schema            = var.schema_name
    namespace         = local.namespace
    warehouse         = databricks_sql_endpoint.demo.name
    warehouse_path    = databricks_sql_endpoint.demo.odbc_params[0].path
    service_principal = databricks_service_principal.demo.display_name
    staging_bucket    = var.enable_s3_staging ? aws_s3_bucket.staging[0].bucket : null
    workload_seeded   = var.run_workload_setup
  }
}
