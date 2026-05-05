# Prompt 07 — Data Validation

Perform a complete integrity check between Postgres source and ClickHouse target.

For each of the 8 tables:
1. Row count: Postgres COUNT(*) vs ClickHouse count()
2. Numeric sum on one key column (e.g. SUM(total_amount) for orders)
3. Date range: MIN/MAX of the primary timestamp column
4. NULL check: count of NULLs in columns that used non-nullable Postgres types

Present results as a validation table:
| Table | PG rows | CH rows | Match? | Sum check | Date range match |

A successful migration should show:
- Row counts match exactly
- Numeric sums match within 0.01% (floating-point rounding)
- Date ranges match
- Zero unexpected NULLs in non-nullable columns
