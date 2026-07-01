# Migration Guide — BigQuery → ClickHouse Cloud

This guide walks you through a complete BigQuery → ClickHouse Cloud
migration using the **MigrationRoom** dashboard. The dashboard
orchestrates the work: you pick a source, click six step buttons in
order, and watch the AI agent do each step live. The agent has MCP
connections to your BigQuery dataset, your ClickHouse Cloud target, an
in-chat Python runtime, and the `migrationkit` Python library that
handles data movement.

**Demo workload:** `${BIGQUERY_PROJECT}.migration_demo` — TPC-H sample
tables (SF1, ~6M rows across 8 tables) loaded via the shared
[`workloads/tpch/`](../../workloads/tpch/) component, augmented with
BigQuery-native features:

| Source object | ClickHouse decision |
|---|---|
| `orders.order_metadata` (`STRUCT<...>`) | `JSON` column, or extract hot keys to typed columns |
| `lineitem.delivery_at` (`TIMESTAMP`) | `DateTime64(3, 'UTC')` with conversion at the source |
| `lineitem` `PARTITION BY DATE_TRUNC(l_shipdate, MONTH)` | `PARTITION BY toYYYYMM(l_shipdate)` |
| `lineitem` `CLUSTER BY l_orderkey, l_shipdate` | `ORDER BY (l_orderkey, l_shipdate)` |
| `daily_order_summary` (`MATERIALIZED VIEW`) | ClickHouse MV on `AggregatingMergeTree` + backfill |
| `customer.contact_addresses` (`ARRAY<STRUCT<...>>`) | `Array(Tuple(line String, city String, country String))` |

**Total time:** ~60 minutes including setup.
**Workflow:** the dashboard's six step buttons drive the migration. The
prompt files in [prompts/](prompts/) are what each button fires — you
don't need to paste them by hand.

---

## Phase 0 — BigQuery Setup (~10 min)

Pick one path. Both end with the same `migration_demo` dataset sitting
in your BigQuery project, with the TPC-H workload + augmentations
already applied.

### Prerequisites — `gcloud` CLI

Both paths assume you can authenticate to GCP. The smoothest way is
the `gcloud` CLI:

```bash
# macOS
brew install --cask google-cloud-sdk

# Linux (Debian/Ubuntu)
curl https://sdk.cloud.google.com | bash && exec -l $SHELL

# Verify
gcloud --version
```

Then sign in **once**:

```bash
gcloud auth login                              # opens browser, logs in your user
gcloud config set project <your-project-id>    # match what you'll put in .env / tfvars
gcloud auth application-default login          # needed by Terraform (Path B) and any
                                               # Python lib that uses ADC; safe to skip if
                                               # you exclusively use SA keys
```

> **Can I skip gcloud?** Yes — neither the playground nor the agent
> ever calls `gcloud`. The runtime only needs a service-account JSON
> key referenced by `BIGQUERY_KEY_FILE`. Without gcloud you'll create
> the SA + key in the [GCP console](https://console.cloud.google.com/iam-admin/serviceaccounts)
> (IAM → Service accounts → Add key → JSON), and Terraform (Path B)
> will need `GOOGLE_APPLICATION_CREDENTIALS` pointed at an existing
> key instead of using ADC.

### Path A — Existing GCP project

```bash
# 1. Create a service account + key with gcloud (skip if you already have one):
PROJECT_ID=my-gcp-sandbox
SA_EMAIL=migration-demo-sa@${PROJECT_ID}.iam.gserviceaccount.com

gcloud iam service-accounts create migration-demo-sa \
  --project=${PROJECT_ID} --display-name="Migration demo"

for role in roles/bigquery.dataEditor roles/bigquery.jobUser; do
  gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SA_EMAIL}" --role="${role}"
done

mkdir -p secrets
gcloud iam service-accounts keys create secrets/gcp-key.json \
  --iam-account=${SA_EMAIL}

# 2. Set BIGQUERY_PROJECT, BIGQUERY_DATASET, BIGQUERY_LOCATION, and
#    BIGQUERY_KEY_FILE=secrets/gcp-key.json in .env.

# 3. Activate a venv so the loader's pip installs don't touch system Python.
python3 -m venv .venv && source .venv/bin/activate

# 4. Generate TPC-H SF1 .tbl files locally (one-time, ~30 s).
make tpch-data

# 5. Load + augment into BigQuery.
set -a; source .env; set +a
make tpch-load-bigquery
```

### Path B — Fresh demo environment via Terraform

Terraform's Google provider authenticates against [Application Default
Credentials](https://cloud.google.com/docs/authentication/application-default-credentials),
so the `gcloud auth application-default login` from the prereqs block
is what makes `terraform apply` work without juggling key files.

```bash
# (Prereqs above: gcloud installed + `gcloud auth application-default login` done.)
cd sources/bigquery/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in existing_project_id (attach mode)
                                               # OR leave null + set billing_account_id (greenfield)
terraform init
terraform apply
terraform output -no-color -raw env_block 2>/dev/null \
  | grep -E '^(#|[A-Z_]+=|$)' >> ../../../.env
```

Provisions a BigQuery dataset, a dedicated service account with
least-privilege roles, writes the SA key to `./secrets/gcp-key.json`,
and optionally creates a GCS staging bucket. Then run `make tpch-data`
and `make tpch-load-bigquery` as in Path A to populate the workload.

> **gcloud roles needed**:
> - **Attach mode**: BigQuery Admin + IAM Admin on the target project
>   (Terraform creates the SA and key).
> - **Greenfield mode**: Project Creator + Billing Account User on the
>   billing account (Terraform creates the project + enables the
>   BigQuery / Storage APIs first).

---

## Phase 1 — Launch the playground (~5 min)

```bash
make up-bigquery
```

`make up-bigquery` runs `make up` plus the profile-gated
`bigquery-source` MCP container. Default `make up` skips BigQuery
because the upstream toolbox crashes without valid credentials.

Open **<https://localhost/dashboard/>** (accept the self-signed cert)
and sign in (`admin@playground.local` / `playground`). You'll land on
the **MigrationRoom** dashboard with the chat panel on the right.

In the **SETUP** card at the top:

- **Source**: pick `BigQuery`. The chat panel auto-switches to the
  `BigQuery → ClickHouse Cloud` agent.
- **Source database**: pick `migration_demo` (or whatever dataset you
  populated in Phase 0).
- **Queries**: open **Edit · N OLAP** and confirm the OLAP queries are
  loaded (steps 4 and 5 use them for rewrite + benchmark).

> **Different model?** Set `AGENT_PROVIDER_BIGQUERY` /
> `AGENT_MODEL_BIGQUERY` in `.env` and run `make reset-agent`, or
> change the model in the LibreChat agent-settings panel.

---

## Phase 2 — Run the migration (~45 min)

The dashboard has **six step buttons** at the top of the **STEPS**
panel. Click each in order. Every click fires a prompt to the agent
and tracks progress live; you don't paste prompts by hand. All six
buttons are clickable at any time, so you can re-fire a step (e.g. to
re-run validation after fixing the schema).

### Step 1 — Discover & Design Schema

Agent introspects the source via `bigquery-source` MCP
(`bigquery-list-dataset-ids`, `bigquery-list-table-ids`,
`bigquery-get-table-info`, `INFORMATION_SCHEMA.PARTITIONS`), reads
your OLAP queries to drive `ORDER BY` choices, proposes the
ClickHouse target schema, and runs the DDL via `clickhousectl`.

**Watch in chat** for the agent's decisions: `Decimal(P,S)` for money,
`DateTime64(3, 'UTC')` for the timezone column, `JSON` (not `String`)
for the `STRUCT` column, `Array(Tuple(...))` for the
`ARRAY<STRUCT<...>>` column, BigQuery `PARTITION BY DATE_TRUNC(...)` →
ClickHouse `PARTITION BY toYYYYMM(...)`. **Confirm the target database
name** when the agent asks (default suggestion `migration_demo`).

### Step 2 — Migrate Data

Agent writes a short Python script using the `migrationkit` library,
dispatches it as a background job via `migration-runner`, issues
ONE `tail_python_job` to confirm `status=running`, then stops. The
dashboard's **Migration** tab streams live progress: rows/sec, ETA,
per-table progress bars, milestone events.

BigQuery → ClickHouse Cloud large tables can also take the GCS-stage
path (`EXPORT DATA` to GCS Parquet → `INSERT FROM gcs()`); the agent
picks the right path per table based on row count and whether
`STAGING_GCS_*` env vars are set.

You don't need to do anything during this step except watch the bars
fill. The chat will be quiet — that's intentional.

### Step 3 — Validate

Agent runs `Validator(...).validate()` — row count parity per table,
source vs target. Results land on the dashboard's **Validation** tab
(one row per table: source rows / target rows / matched). If anything
mismatches the agent **stops and reports** in chat — fix the schema
and re-fire step 2, don't ask the agent to patch the target by hand.

### Step 4 — Rewrite Queries

Agent translates each OLAP query from BigQuery SQL to ClickHouse SQL
**in chat**. No script — this is a reasoning step. Walk through each
rewrite, push back on unfamiliar function substitutions
(`DATE_TRUNC(MONTH, x)` → `toStartOfMonth(x)`, `ARRAY_AGG` →
`groupArray`, `STRUCT` accessor → tuple element access, etc.).

### Step 5 — Benchmark

Agent runs `Benchmarker(...).benchmark(queries=[...])` — each query
on source and target, server-side timing on both. Results land on the
**Benchmark** tab as `source_ms / target_ms / speedup` per query.

BigQuery timing comes from the `QueryJob.ended - QueryJob.started`
delta (network-neutral); ClickHouse from the `X-ClickHouse-Summary`
HTTP header.

### Step 6 — Optimize

Agent proposes ClickHouse-Cloud-specific optimizations for the
slowest queries: Materialized Views on `AggregatingMergeTree`,
Projections, codec adjustments. Iterate in chat — once you apply an
optimization, re-fire step 5 to confirm the speedup.

---

## Validation

Compare the agent's final state against:

- **Schema:** [queries/expected_ch_schema.sql](queries/expected_ch_schema.sql)
- **Queries:** [queries/expected_ch_queries.sql](queries/expected_ch_queries.sql)
- **Checklist:** [../../docs/migration-checklist.md](../../docs/migration-checklist.md)

Bit-for-bit identity isn't expected — what matters is that the agent
made defensible choices: `Decimal` (not `Float`) for money,
`DateTime64(3, 'UTC')` for the augmented timezone-aware column, `JSON`
(not `String`) for the BigQuery STRUCT column, `Array(Tuple(...))` for
the nested `contact_addresses`, a Materialized View on
`AggregatingMergeTree` replacing the BigQuery MV.

---

## Troubleshooting

**BigQuery MCP container restarts / shows unhealthy:**
The toolbox image needs a service-account key. Confirm
`BIGQUERY_KEY_FILE` in `.env` points at a JSON file that exists on
disk and that the SA has BigQuery Data Viewer (read) on the dataset.
Check `docker compose logs bigquery-source`.

**Migration runner can't reach BigQuery from inside the container:**
The `migration-runner` container mounts the same SA key at
`/secrets/gcp-key.json` and sets `GOOGLE_APPLICATION_CREDENTIALS` to
that path. If you edited `BIGQUERY_KEY_FILE` after `make up`, restart
the runner: `docker compose restart migration-runner`.

**Step 2 looks quiet in chat:**
Expected — the agent dispatches the migration as a background job and
stops. Watch the dashboard's **Migration** tab for live row counts
and throughput.

**Step 3 reports a mismatch:**
The agent will stop and surface the mismatched table(s) in chat.
Don't ask it to patch the target with manual `INSERT … SELECT … FROM
…` — that bypasses the Migrator and hides the underlying bug. Fix
the schema (most common cause: case-sensitivity, or a column the
agent left out) and re-fire step 2.

**Costs unexpectedly high after a few runs:**
BigQuery bills per byte scanned, not per query. The agent's `SELECT *`
discovery queries scan whole tables. To cap spend during repeated
demos, set a per-project quota in the GCP console (BigQuery →
Settings → Query usage). The TPC-H SF1 workload is ~1 GB, so a
typical demo session scans < 10 GB, well under BigQuery's 1 TB/mo
free tier.
