variable "snowflake_account" {
  description = "Snowflake account identifier in the form ORGNAME-ACCOUNTNAME (e.g. ABCDEFG-XY12345)."
  type        = string
}

variable "admin_user" {
  description = "Snowflake user with ACCOUNTADMIN (or equivalent) to provision the demo environment."
  type        = string
}

variable "admin_password" {
  description = "Password for the admin user."
  type        = string
  sensitive   = true
}

variable "admin_role" {
  description = "Role of the admin user. Needs ACCOUNTADMIN to create warehouses + users + roles."
  type        = string
  default     = "ACCOUNTADMIN"
}

variable "warehouse_size" {
  description = "Size of the dedicated demo warehouse."
  type        = string
  default     = "X-SMALL"
}
