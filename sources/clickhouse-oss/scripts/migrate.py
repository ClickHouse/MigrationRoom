import os
import time
import clickhouse_connect

# ==========================================
# 1. Configuration & Credentials
# ==========================================

# OSS source
CH_OSS_HOST     = os.getenv("CH_OSS_HOST", "localhost")
CH_OSS_PORT     = int(os.getenv("CH_OSS_PORT", "8123"))
CH_OSS_USER     = os.getenv("CH_OSS_USER", "default")
CH_OSS_PASSWORD = os.getenv("CH_OSS_PASSWORD", "")
CH_OSS_DB       = os.getenv("CH_OSS_DB", "analytics")

# ClickHouse Cloud target
CH_HOST     = os.getenv("CLICKHOUSE_CLOUD_HOST", "")
CH_PORT     = int(os.getenv("CLICKHOUSE_CLOUD_PORT", "8443"))
CH_USER     = os.getenv("CLICKHOUSE_CLOUD_USER", "default")
CH_PASSWORD = os.getenv("CLICKHOUSE_CLOUD_PASSWORD", "")
CH_DB       = os.getenv("CLICKHOUSE_CLOUD_DATABASE", "analytics")

# ==========================================
# 2. Initialize Clients
# ==========================================

print("Connecting to Source (OSS)...")
source = clickhouse_connect.get_client(
    host=CH_OSS_HOST, port=CH_OSS_PORT,
    username=CH_OSS_USER, password=CH_OSS_PASSWORD,
    database=CH_OSS_DB, secure=False
)

print("Connecting to Target (Cloud)...")
target = clickhouse_connect.get_client(
    host=CH_HOST, port=CH_PORT,
    username=CH_USER, password=CH_PASSWORD,
    database=CH_DB,
    secure=True, verify=False # verify=False avoids macOS TLS chain errors
)

# ==========================================
# 3. Migration Worker Functions
# ==========================================

def migrate_full(table):
    """For tables < 500K rows: single batch."""
    print(f"\nMigrating {table} (Full transfer)...")
    t0 = time.time()
    
    result = source.query(f"SELECT * FROM {table}")
    if result.result_rows:
        target.insert(table, result.result_rows, column_names=result.column_names)
        
    print(f"✅ {table}: Migrated {len(result.result_rows)} rows in {time.time()-t0:.2f}s")


def migrate_by_month(table, time_col):
    """For tables 500K - 5M rows: chunk by month."""
    print(f"\nMigrating {table} (Chunked by month)...")
    
    months_result = source.query(f"SELECT DISTINCT toYYYYMM({time_col}) FROM {table} ORDER BY 1")
    months = [row[0] for row in months_result.result_rows]
    
    total_rows = 0
    for m in months:
        t0 = time.time()
        result = source.query(f"SELECT * FROM {table} WHERE toYYYYMM({time_col}) = {m}")
        if result.result_rows:
            target.insert(table, result.result_rows, column_names=result.column_names)
            total_rows += len(result.result_rows)
            print(f"  - {m}: Inserted {len(result.result_rows)} rows ({time.time()-t0:.2f}s)")
            
    print(f"✅ {table}: Migrated {total_rows} total rows")


def migrate_large_by_month(table, time_col, batch_size=200000):
    """For tables > 5M rows: chunk by month + LIMIT/OFFSET."""
    print(f"\nMigrating {table} (Chunked by month with limits)...")
    
    months_result = source.query(f"SELECT DISTINCT toYYYYMM({time_col}) FROM {table} ORDER BY 1")
    months = [row[0] for row in months_result.result_rows]
    
    total_rows = 0
    for m in months:
        offset = 0
        month_rows = 0
        while True:
            t0 = time.time()
            query = f"""
                SELECT * FROM {table} 
                WHERE toYYYYMM({time_col}) = {m} 
                ORDER BY {time_col} 
                LIMIT {batch_size} OFFSET {offset}
            """
            result = source.query(query)
            
            if not result.result_rows:
                break # No more data for this month
                
            target.insert(table, result.result_rows, column_names=result.column_names)
            
            rows_in_batch = len(result.result_rows)
            total_rows += rows_in_batch
            month_rows += rows_in_batch
            offset += batch_size
            
            print(f"  - {m} (offset {offset - batch_size}): Inserted {rows_in_batch} rows ({time.time()-t0:.2f}s)")
            
        print(f"  ✓ {m} complete: {month_rows} rows.")
        
    print(f"✅ {table}: Migrated {total_rows} total rows")


# ==========================================
# 4. Execute Pipeline
# ==========================================

if __name__ == "__main__":
    print("🚀 Starting Migration...")

    # Phase 1: Dimensions (< 500K)
    migrate_full("projects")
    
    # Phase 2: Small Facts (< 500K)
    migrate_full("conversions")
    
    # Phase 3: Medium Facts (500K - 5M)
    migrate_by_month("sessions", "started_at")
    
    # Phase 4: Large Facts (> 5M)
    migrate_large_by_month("pageviews", "timestamp", batch_size=200000)

    # Phase 5: AggregatingMergeTree Backfill (Direct target execution)
    # Binary state blobs cannot be copied via Python. 
    # We must construct them from the raw fact data directly on the target.
    print("\nExecuting daily_stats Materialized View backfill on target...")
    backfill_sql = """
        INSERT INTO analytics.daily_stats
        SELECT
            toDate(started_at) AS date,
            project_id,
            referrer_domain,
            device_type,
            country_code,
            uniqState(visitor_id) AS visitors,
            toUInt64(count()) AS sessions,
            sum(toUInt64(pageview_count)) AS pageviews,
            sum(toUInt64(is_bounce)) AS bounces,
            sum(toUInt64(duration_seconds)) AS total_duration
        FROM analytics.sessions
        GROUP BY date, project_id, referrer_domain, device_type, country_code;
    """
    
    t0 = time.time()
    target.command(backfill_sql)
    print(f"✅ daily_stats: Backfill complete ({time.time()-t0:.2f}s)")

    print("\n🎉 Migration finished successfully!")