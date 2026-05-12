output "env_block" {
  description = "Paste this block into the project's .env file."
  sensitive   = true
  value = <<EOT

# ── Snowflake Source (provisioned by terraform) ──────────────
SNOWFLAKE_ACCOUNT=${var.snowflake_account}
SNOWFLAKE_USER=${snowflake_user.demo.name}
SNOWFLAKE_PASSWORD=${random_password.demo_user.result}
SNOWFLAKE_ROLE=${snowflake_account_role.demo.name}
SNOWFLAKE_WAREHOUSE=${snowflake_warehouse.demo.name}
EOT
}

output "summary" {
  description = "Human-readable summary of what was created."
  value = {
    warehouse = snowflake_warehouse.demo.name
    database  = snowflake_database.demo.name
    schema    = snowflake_schema.retail.name
    role      = snowflake_account_role.demo.name
    user      = snowflake_user.demo.name
  }
}
