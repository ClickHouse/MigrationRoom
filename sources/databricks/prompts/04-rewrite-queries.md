# Step 4 — Rewrite the analytical queries for ClickHouse

Data is migrated and validated. Translate the partner's Databricks SQL to
ClickHouse SQL. **This step is chat-only — write no Python.** Use
`clickhousectl` `run_query` to check that each rewritten query runs and
returns sensible results.

## The queries

```sql
{olap_queries}
```

## Rewrite each one

For every query: show the original, the ClickHouse version, and a one-line
note on what changed and why. Then run the ClickHouse version and report
the row count returned.

## Databricks → ClickHouse dialect differences

| Databricks | ClickHouse | Note |
|---|---|---|
| `QUALIFY <pred>` | subquery + `WHERE` over the window | No `QUALIFY` in ClickHouse |
| `LATERAL VIEW explode(arr) AS x` | `ARRAY JOIN` or `arrayJoin(arr)` | `ARRAY JOIN` is usually clearer |
| `explode(m)` on a MAP | `arrayJoin(mapKeys(m))` + lookup | Maps explode to key/value pairs |
| `transform(arr, x -> f(x))` | `arrayMap(x -> f(x), arr)` | Argument order flips |
| `filter(arr, x -> p(x))` | `arrayFilter(x -> p(x), arr)` | Argument order flips |
| `aggregate(arr, 0, (a, x) -> a + x)` | `arraySum(arr)` / `arrayReduce('sum', arr)` | Prefer the specific function |
| `v:field`, `v:a.b`, `variant_get(v, '$.a')` | `JSONExtractString(v, 'a')`, `v.a` on a `JSON` column | Depends on whether you mapped to `JSON` or `String` |
| `named_struct('a', 1)` | `tuple(1)` / named `Tuple` | ClickHouse tuples are positional |
| `m['k']` on a MAP | `m['k']` | Same syntax; missing key returns the type default, not NULL |
| `try_divide(a, b)` | `if(b = 0, NULL, a / b)` | ClickHouse `/` by zero returns `inf`, not NULL |
| `try_cast(x AS t)` | `toTypeOrNull(x)` / `accurateCastOrNull` | |
| `ilike` | `ILIKE` | Supported |
| `date_trunc('month', d)` | `toStartOfMonth(d)` | ClickHouse also has `date_trunc` |
| `datediff(a, b)` | `dateDiff('day', b, a)` | Argument order flips |
| `catalog.schema.table` | `database.table` | Three levels collapse to two |
| `SEMI JOIN` / `ANTI JOIN` | `LEFT SEMI JOIN` / `LEFT ANTI JOIN` | Join kind precedes strictness in ClickHouse |
| Implicit `NULL` ordering | `NULLS FIRST` / `NULLS LAST` explicit | Defaults differ — state it |

## Rules

- Do not change the *meaning* of a query to make it run. If a rewrite
  can't be exact, say so and explain the difference.
- Where the schema decisions from step 1 make a query simpler (a hot
  VARIANT key extracted to a typed column, for instance), use the simpler
  form and point out the win.
- Keep the result shape identical — same columns, same order, same names —
  so step 5 can benchmark source against target fairly.

## When you're done

Present the rewritten set as a single SQL block the partner can copy, and
paste it into the dashboard's OLAP queries editor so step 5 benchmarks the
same statements.
