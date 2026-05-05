# Prompt 01 — Schema Discovery

Using the **postgres-source** MCP server, explore the ecommerce database:

1. List all tables in the public schema with row counts
2. For each table show: column names, data types, nullable flag, default values, constraints
3. List all indexes (B-tree, GIN, partial, unique)
4. Identify fact tables vs dimension tables from foreign key relationships
5. Highlight Postgres-specific features (JSONB, arrays, ENUMs, SERIAL, TIMESTAMPTZ) that will need special handling during migration

Produce a migration inventory table:
| Table | Rows | Special Features | Migration Notes |
