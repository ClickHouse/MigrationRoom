import os
import json
import psycopg2
import psycopg2.extras
import clickhouse_connect
from datetime import datetime

# ==========================================
# 1. Configuration & Connections
# ==========================================

PG_HOST     = os.getenv("PG_HOST", "localhost")
PG_PORT     = int(os.getenv("PG_PORT", "5432"))
PG_USER     = os.getenv("PG_USER", "")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")
PG_DB       = os.getenv("PG_DB", "ecommerce")

CH_HOST     = os.getenv("CLICKHOUSE_CLOUD_HOST", "")
CH_PORT     = int(os.getenv("CLICKHOUSE_CLOUD_PORT", "8443"))
CH_USER     = os.getenv("CLICKHOUSE_CLOUD_USER", "default")
CH_PASSWORD = os.getenv("CLICKHOUSE_CLOUD_PASSWORD", "")
CH_DB       = os.getenv("CLICKHOUSE_CLOUD_DATABASE", "migration_target")

MIGRATION_PLAN = [
    {"table": "users",               "batch_key": "user_id",      "batch_size": 100000},
    {"table": "products",            "batch_key": "product_id",   "batch_size": 50000},
    {"table": "orders",              "batch_key": "order_id",     "batch_size": 100000},
    {"table": "order_items",         "batch_key": "item_id",      "batch_size": 250000},
    {"table": "inventory_snapshots", "batch_key": "snapshot_id",  "batch_size": 250000},
    {"table": "sessions",            "batch_key": "session_id",   "batch_size": 200000}, 
    {"table": "ad_impressions",      "batch_key": "impression_id","batch_size": 500000},
    {"table": "events",              "batch_key": "event_id",     "batch_size": 500000}
]

def get_pg_connection():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database=PG_DB
    )

def get_ch_connection():
    return clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, username=CH_USER, password=CH_PASSWORD,
        database=CH_DB, secure=True, verify=False 
    )

# ==========================================
# 2. Data Sanitization Rules
# ==========================================

def sanitize_row(row):
    clean_row = {}
    for key, val in row.items():
        if isinstance(val, dict):
            clean_row[key] = json.dumps(val, default=str)
        elif isinstance(val, list):
            clean_row[key] = ['' if x is None else str(x) for x in val]
        elif val is None:
            if key in ('device_type', 'country_code', 'category', 'subcategory', 'segment', 'referrer_source', 'referrer', 'page_url', 'ad_group', 'placement'):
                clean_row[key] = ''
            elif key in ('page_count', 'duration_seconds', 'user_id', 'creative_id', 'quantity_reserved', 'discount_pct'):
                clean_row[key] = 0
            elif key in ('ended_at', 'updated_at'):
                clean_row[key] = datetime(1970, 1, 1)
            elif key in ('clicked', 'converted'):
                clean_row[key] = False
            else:
                clean_row[key] = val
        else:
            clean_row[key] = val
    return clean_row

# ==========================================
# 3. Migration Execution
# ==========================================

def migrate_table_by_id(pg_cursor, ch_client, table_name, id_col, batch_size):
    print(f"\n--- Migrating {table_name} ---")
    
    if table_name == 'sessions':
        return migrate_table_by_offset(pg_cursor, ch_client, table_name, batch_size)
        
    # FIX: Use aliases and explicitly fetch from the dictionary
    pg_cursor.execute(f"SELECT MIN({id_col}) as min_id, MAX({id_col}) as max_id, COUNT(*) as total_rows FROM {table_name}")
    stats = pg_cursor.fetchone()
    
    min_id = stats['min_id']
    max_id = stats['max_id']
    total_rows = stats['total_rows']
    
    if not total_rows:
        print(f"Table {table_name} is empty. Skipping.")
        return

    print(f"Total rows: {total_rows:,} | ID Range: {min_id} - {max_id}")
    
    current_min = min_id
    batch_num = 1
    total_migrated = 0
    
    while current_min <= max_id:
        current_max = current_min + batch_size - 1
        
        pg_cursor.execute(
            f"SELECT * FROM {table_name} WHERE {id_col} >= %s AND {id_col} <= %s",
            (current_min, current_max)
        )
        rows = pg_cursor.fetchall()
        
        if rows:
            sanitized_data = [sanitize_row(row) for row in rows]
            column_names = list(sanitized_data[0].keys())
            data_to_insert = [[row[col] for col in column_names] for row in sanitized_data]
            
            ch_client.insert(table_name, data_to_insert, column_names=column_names)
            
            total_migrated += len(rows)
            print(f"  Batch {batch_num} | Migrated {len(rows):,} rows | ID {current_min} to {current_max}")
            
        current_min = current_max + 1
        batch_num += 1
        
    print(f"✅ {table_name} migration complete. Total migrated: {total_migrated:,}")

def migrate_table_by_offset(pg_cursor, ch_client, table_name, batch_size):
    # FIX: explicitly alias the count and fetch from dictionary
    pg_cursor.execute(f"SELECT COUNT(*) as total_rows FROM {table_name}")
    total_rows = pg_cursor.fetchone()['total_rows']
    
    print(f"Total rows: {total_rows:,} | Using LIMIT/OFFSET batching")
    
    offset = 0
    batch_num = 1
    total_migrated = 0
    
    while offset < total_rows:
        pg_cursor.execute(f"SELECT * FROM {table_name} LIMIT %s OFFSET %s", (batch_size, offset))
        rows = pg_cursor.fetchall()
        
        if rows:
            sanitized_data = [sanitize_row(row) for row in rows]
            column_names = list(sanitized_data[0].keys())
            data_to_insert = [[row[col] for col in column_names] for row in sanitized_data]
            
            ch_client.insert(table_name, data_to_insert, column_names=column_names)
            
            total_migrated += len(rows)
            print(f"  Batch {batch_num} | Migrated {len(rows):,} rows | Offset {offset}")
            
        offset += batch_size
        batch_num += 1
        
    print(f"✅ {table_name} migration complete. Total migrated: {total_migrated:,}")

# ==========================================
# 4. Validation
# ==========================================

def run_validation(ch_client):
    print("\n=== Post-Migration Validation (Row Counts) ===")
    
    queries = [f"SELECT '{t['table']}' AS table_name, COUNT(*) FROM {CH_DB}.{t['table']}" for t in MIGRATION_PLAN]
    union_query = " UNION ALL ".join(queries)
    
    results = ch_client.query(union_query)
    
    print(f"{'Table':<25} | {'ClickHouse Rows':<15}")
    print("-" * 43)
    for row in results.result_rows:
        # Check integer typing for clean formatting
        row_count = int(row[1]) 
        print(f"{row[0]:<25} | {row_count:<15,}")

# ==========================================
# 5. Main Execution
# ==========================================

if __name__ == "__main__":
    try:
        pg_conn = get_pg_connection()
        pg_cursor = pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ch_client = get_ch_connection()
        
        print("Starting E-Commerce Migration to ClickHouse Cloud...")
        print(f"Target Database: {CH_DB}")
        
        for plan in MIGRATION_PLAN:
            migrate_table_by_id(pg_cursor, ch_client, plan["table"], plan["batch_key"], plan["batch_size"])
            
        run_validation(ch_client)
        
    except Exception as e:
        print(f"Migration Failed: {e}")
    finally:
        if 'pg_cursor' in locals(): pg_cursor.close()
        if 'pg_conn' in locals(): pg_conn.close()