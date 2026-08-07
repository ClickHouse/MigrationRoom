output "workspace_url" {
  description = "URL of the created workspace. Feeds the demo module's workspace_url variable."
  value       = databricks_mws_workspaces.demo.workspace_url
}

output "workspace_id" {
  description = "Numeric workspace ID."
  value       = databricks_mws_workspaces.demo.workspace_id
}

output "summary" {
  description = "Human-readable summary of what was created."
  value = {
    workspace_name    = databricks_mws_workspaces.demo.workspace_name
    workspace_url     = databricks_mws_workspaces.demo.workspace_url
    aws_region        = var.aws_region
    compute_mode      = "SERVERLESS"
    metastore_created = var.create_metastore
  }
}
