# Prompt 04 — Data Migration

Migrate data from each Postgres table to ClickHouse Cloud using the postgresql() table function.
No intermediate files — it's a direct INSERT ... SELECT from Postgres.

> **Before you start — network reachability:**
> ClickHouse Cloud executes the `postgresql()` function from its cloud environment, so it
> needs to reach your Postgres instance over the internet. The local Docker hostname
> `postgres:5432` is not reachable from Cloud.
>
> Choose one of these options and substitute the correct host in all queries below:
>
> **Option A — Tunnel (quickest):**
> ```bash
> ngrok tcp 5432   # gives you something like 0.tcp.ngrok.io:12345
> ```
> Use `0.tcp.ngrok.io:12345` (your actual ngrok address) as the host:port.
>
> **Option B — Export to S3/GCS then import:**
> ```sql
> -- In Postgres: COPY users TO PROGRAM 'aws s3 cp - s3://bucket/users.csv'
> -- In ClickHouse: INSERT INTO migration_target.users SELECT * FROM s3('s3://bucket/users.csv', ...)
> ```

Migration order (respects dimension→fact dependency):
1. users
2. products
3. sessions
4. events      ← largest (10M rows) — batch by month if needed
5. orders
6. order_items
7. ad_impressions
8. inventory_snapshots

For each table:
1. Show the INSERT ... SELECT statement before executing
2. Execute on clickhouse-cloud
3. Verify: compare count() in ClickHouse vs COUNT(*) in Postgres

Postgres connection details:
  DB: ecommerce, User: playground, Password: playground
  Host: <your-ngrok-host:port>  (replace with actual address from Option A above)

Example pattern (using ngrok tunnel):
```sql
INSERT INTO migration_target.events
SELECT event_id, user_id, session_id, event_type, page_url, referrer,
       properties, device_type, country_code, created_at
FROM postgresql('<ngrok-host:port>', 'ecommerce', 'events', 'playground', 'playground');
```

For events (10M rows), batch by month to limit memory:
```sql
INSERT INTO migration_target.events
SELECT ... FROM postgresql('<ngrok-host:port>', 'ecommerce', 'events', 'playground', 'playground')
WHERE created_at >= '2024-01-01' AND created_at < '2024-02-01';
-- repeat for each month
```
