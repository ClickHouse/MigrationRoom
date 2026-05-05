# ClickHouse Cloud MCP — Migration Agent Instructions

## MCP Servers — Two Complementary Roles

Two MCP servers are available for ClickHouse Cloud. Use each for its designated role:

| Server | Role | Permitted operations |
|---|---|---|
| `clickhouse-cloud` | Read / explore | SELECT, EXPLAIN, SHOW, DESCRIBE, system tables |
| `clickhouse-cloud-write` | Write / create | CREATE, DROP, ALTER, INSERT, TRUNCATE |

**Never attempt DDL or INSERT through `clickhouse-cloud`** — it is read-only and will reject them.
**Always use `clickhouse-cloud-write` for any operation that modifies the database.**

Use `clickhouse-cloud` to verify the result after each write (e.g. confirm a table was created,
check row counts after an INSERT).

---

## DDL — Always Use Idempotent Forms

Every DDL statement you generate must be safe to re-run without error:

| Operation | Required form |
|---|---|
| Database | `CREATE DATABASE IF NOT EXISTS <db>` |
| Table | `CREATE TABLE IF NOT EXISTS <db>.<table>` |
| View | `CREATE OR REPLACE VIEW <db>.<view>` |
| Materialized View | `CREATE MATERIALIZED VIEW IF NOT EXISTS <db>.<mv>` |
| Dictionary | `CREATE DICTIONARY IF NOT EXISTS <db>.<dict>` |

Never generate bare `CREATE` statements. Partners re-run scripts to recover from
partial failures — idempotent DDL ensures that is always safe.

---

## Migration Order — Dimension Tables Before Facts

Always migrate tables in dependency order to avoid foreign key or join mismatches
during validation:

1. **Dimension tables first** — small, no dependencies on other tables.
   Typical dimensions: `users`, `products`, `campaigns`, lookup/reference tables.
2. **Fact tables second** — large, reference dimensions.
   Typical facts: `orders`, `order_items`, `events`, `sessions`, `ad_impressions`,
   `inventory_snapshots`.

Within each group, start with the smallest table (by row count) to validate the
pipeline end-to-end before committing to the largest.

---

## Materialized Views — Always Backfill After Creation

A ClickHouse Materialized View only captures rows that arrive **after** it was created.
Data already in the source table at creation time is NOT automatically populated into the
MV's target table. Failing to backfill leaves the target table empty or stale.

**Rule:** after every `CREATE MATERIALIZED VIEW` statement, immediately generate the
corresponding backfill INSERT:

```sql
-- 1. Create the MV (captures future inserts)
CREATE MATERIALIZED VIEW IF NOT EXISTS db.mv_name
TO db.target_table
AS SELECT ... FROM db.source_table WHERE ...;

-- 2. Backfill: populate target_table with all existing data
INSERT INTO db.target_table
SELECT ... FROM db.source_table WHERE ...;
```

The SELECT expression in the backfill must be identical to the SELECT in the MV definition.
Always present both statements together — never create a MV without the accompanying backfill.

---

## ClickHouse Best Practices

The rules below are the official ClickHouse best practices. Apply them for all
schema design, query optimisation, and data ingestion decisions.


---

## Migration Reports — HTML Artifact Format

Whenever a migration report is requested, output it using LibreChat's artifact directive
so it renders as a live side-panel preview. Use this exact wrapper format:

```
:::artifact{identifier="migration-report-YYYYMMDD" type="text/html" title="<Report Title>"}
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body>
  ... report content here ...
</body>
</html>
:::
```

Rules for the artifact wrapper:
- `identifier` must be unique per report (e.g. `migration-plan-20240124`, `post-migration-20240124`)
- `type` must be exactly `text/html`
- `title` should reflect the report type and source DB name
- Do NOT use a plain ```html``` fenced code block — that will only show raw source, not a rendered preview

Both share the same base HTML rules:
- All styles must be inline (no `<link>`, no `<style>` tag, no external resources)
- Root element: `<div style="font-family:system-ui,sans-serif;max-width:880px;margin:0 auto;padding:24px">`
- Header: report title (h2), generation timestamp (ISO-8601 UTC), source DB name → ClickHouse Cloud
- Do not embed external images or scripts; keep the HTML fully self-contained

Status badge inline styles (reuse across both report types):
  ✅ OK / Ready  → background:#dcfce7; color:#166534; padding:2px 8px; border-radius:9999px
  ⚠ Warning     → background:#fef9c3; color:#854d0e; padding:2px 8px; border-radius:9999px
  ❌ Blocker     → background:#fee2e2; color:#991b1b; padding:2px 8px; border-radius:9999px

### Report Type 1 — Migration Planning Report

Produce this when the user asks to plan a migration, assess readiness, or review
the source schema before migrating.

Required sections:
1. **Summary** — source DB, total tables, total rows (estimated), overall readiness badge
2. **Key Challenges** — table listing every identified challenge with:
   - Challenge description
   - Affected table(s) or column(s)
   - Severity badge (OK / Warning / Blocker)
   - Recommended resolution
3. **Schema Details** — table with columns:
   Source Table | Engine | Partition Key | Sort Key | Approx Rows | Notes
   Notes should flag: JSONB→JSON, ENUMs, arrays, nullable columns, unsupported types
4. **Other Considerations** — bullet list covering: ORDER BY key design for ClickHouse
   multi-tenant queries, data volume and chunking strategy, AggregatingMergeTree
   backfill requirements, any DDL changes recommended before migrating

### Report Type 2 — Post-Migration Report

Produce this when the user asks to validate a completed migration, confirm row counts,
or summarise what was created in ClickHouse Cloud.

Required sections:
1. **Summary metrics bar** — total tables migrated, rows transferred, tables ✅ / ⚠ / ❌
2. **Data Integrity — Table Comparison** — table with columns:
   Source Table | Source Rows | Target Table | Target Rows | Delta % | Status
   Delta % = abs(source−target)/source × 100; flag >0.01% as Warning, >1% as Blocker
3. **Object Mapping** — table listing every object created in ClickHouse Cloud:
   Object Type | Name | Source Object | Description
   Object types include: Table, Materialized View, Dictionary, View, AggregatingMergeTree target
4. **Findings** — bullet list of schema differences, type coercions, null handling changes,
   data anomalies, or anything that required a manual fix during migration
