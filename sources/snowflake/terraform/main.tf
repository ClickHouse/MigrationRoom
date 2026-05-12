terraform {
  required_version = ">= 1.5.0"
  required_providers {
    snowflake = {
      source  = "Snowflake-Labs/snowflake"
      version = ">= 1.0.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5.0"
    }
  }
}

provider "snowflake" {
  account_name      = split("-", var.snowflake_account)[1]
  organization_name = split("-", var.snowflake_account)[0]
  user              = var.admin_user
  password          = var.admin_password
  role              = var.admin_role
}

resource "random_password" "demo_user" {
  length  = 32
  special = false
}

# Dedicated warehouse for the migration demo.
resource "snowflake_warehouse" "demo" {
  name           = "MIGRATION_DEMO_WH"
  warehouse_size = var.warehouse_size
  auto_suspend   = 60
  auto_resume    = true
  comment        = "Dedicated warehouse for AI Migration Assistant demo."
}

# Database + schema. The setup_workload.sql script does CREATE IF NOT EXISTS
# on both, so these are idempotent — Terraform owns the resource records, the
# script populates them with TPC-H tables.
resource "snowflake_database" "demo" {
  name    = "MIGRATION_DEMO"
  comment = "AI Migration Assistant demo: TPC-H + Snowflake-specific augmentations."
}

resource "snowflake_schema" "retail" {
  database = snowflake_database.demo.name
  name     = "RETAIL"
  comment  = "Augmented TPC-H tables (VARIANT, TIMESTAMP_TZ, Stream, Dynamic Table)."
}

# Role + user dedicated to the demo (limited blast radius).
resource "snowflake_account_role" "demo" {
  name    = "MIGRATION_DEMO_ROLE"
  comment = "Read access to MIGRATION_DEMO for the AI Migration Assistant demo."
}

resource "snowflake_grant_privileges_to_account_role" "demo_warehouse" {
  account_role_name = snowflake_account_role.demo.name
  privileges        = ["USAGE"]

  on_account_object {
    object_type = "WAREHOUSE"
    object_name = snowflake_warehouse.demo.name
  }
}

resource "snowflake_grant_privileges_to_account_role" "demo_database" {
  account_role_name = snowflake_account_role.demo.name
  privileges        = ["USAGE"]

  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.demo.name
  }
}

resource "snowflake_grant_privileges_to_account_role" "demo_schema" {
  account_role_name = snowflake_account_role.demo.name
  privileges        = ["USAGE"]

  on_schema {
    schema_name = "\"${snowflake_database.demo.name}\".\"${snowflake_schema.retail.name}\""
  }
}

resource "snowflake_grant_privileges_to_account_role" "demo_tables" {
  account_role_name = snowflake_account_role.demo.name
  privileges        = ["SELECT"]

  on_schema_object {
    future {
      object_type_plural = "TABLES"
      in_schema          = "\"${snowflake_database.demo.name}\".\"${snowflake_schema.retail.name}\""
    }
  }
}

# SNOWFLAKE_SAMPLE_DATA is shared with every account but the demo role needs
# explicit IMPORTED PRIVILEGES on it to copy tables from TPCH_SF1.
resource "snowflake_grant_privileges_to_account_role" "demo_sample_data" {
  account_role_name = snowflake_account_role.demo.name
  privileges        = ["IMPORTED PRIVILEGES"]

  on_account_object {
    object_type = "DATABASE"
    object_name = "SNOWFLAKE_SAMPLE_DATA"
  }
}

resource "snowflake_user" "demo" {
  name                 = "AI_MIGRATION_DEMO"
  login_name           = "AI_MIGRATION_DEMO"
  password             = random_password.demo_user.result
  default_role         = snowflake_account_role.demo.name
  default_warehouse    = snowflake_warehouse.demo.name
  default_namespace    = "${snowflake_database.demo.name}.${snowflake_schema.retail.name}"
  must_change_password = "false"
}

resource "snowflake_grant_account_role" "demo_to_user" {
  role_name = snowflake_account_role.demo.name
  user_name = snowflake_user.demo.name
}

# Run the workload setup script after the infrastructure exists.
# Uses the admin credentials so the script has permission to copy from
# SNOWFLAKE_SAMPLE_DATA into the demo schema.
resource "null_resource" "setup_workload" {
  depends_on = [
    snowflake_warehouse.demo,
    snowflake_schema.retail,
    snowflake_grant_privileges_to_account_role.demo_tables,
    snowflake_grant_privileges_to_account_role.demo_sample_data,
  ]

  provisioner "local-exec" {
    command     = "python3 ${path.module}/../scripts/setup_workload.py"
    interpreter = ["/bin/bash", "-c"]
    environment = {
      SNOWFLAKE_ACCOUNT   = var.snowflake_account
      SNOWFLAKE_USER      = var.admin_user
      SNOWFLAKE_PASSWORD  = var.admin_password
      SNOWFLAKE_ROLE      = var.admin_role
      SNOWFLAKE_WAREHOUSE = snowflake_warehouse.demo.name
    }
  }

  triggers = {
    setup_py  = filemd5("${path.module}/../scripts/setup_workload.py")
    setup_sql = filemd5("${path.module}/../scripts/setup_workload.sql")
  }
}
