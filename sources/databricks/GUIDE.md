# Migration Guide — Databricks → ClickHouse Cloud

This guide walks you through a complete Databricks → ClickHouse Cloud
migration using the **MigrationRoom** dashboard. The dashboard
orchestrates the work: you pick a source, click six step buttons in
order, and watch the AI agent do each step live. The agent has MCP
connections to your Databricks source (`databricks-mcp`, host port
`8008`, Compose profile `databricks`), your ClickHouse Cloud target,
an in-chat Python runtime, and the `migrationkit` Python library that
handles data movement.

**Demo workload:** `migration_demo.tpch` — TPC-H tables downsampled
from the `samples.tpch` catalog every Databricks workspace ships with.
`samples.tpch` is scale factor 1000 (~1 TB, ~6 billion `lineitem`
rows); `setup_workload.sql`'s predicates cut it to SF1-equivalent
cardinality (~6M `lineitem` rows), so this runs in minutes and the
eventual ClickHouse Cloud migration moves megabytes, not terabytes.
Eight Databricks-specific augmentations are layered on top so the agent
has to make real decisions instead of a mechanical type-for-type copy:

| Source object | ClickHouse decision |
|---|---|
| `orders.o_metadata` (VARIANT) | `JSON` column, or extract hot keys to typed columns |
| `lineitem.l_shipping_events` (`ARRAY<STRUCT>`) | `Nested(...)`, or `Array(Tuple(...))` |
| `lineitem.l_attributes` (`MAP<STRING, STRING>`) | `Map(String, String)` |
| `lineitem.l_committed_at` / `l_committed_at_ntz` (`TIMESTAMP` / `TIMESTAMP_NTZ`) | `DateTime64(6, 'UTC')` / `DateTime64(6)` |
| `lineitem` liquid clustering (`CLUSTER BY`) | `ORDER BY (...)` chosen from the actual queries |
| `lineitem` deletion vectors | No equivalent; `ReplacingMergeTree`, ClickPipes, or defer |
| `orders.o_orderyear` (`GENERATED ALWAYS AS`) | `MATERIALIZED` (stored) or `ALIAS` (computed) column |
| `daily_order_summary` (materialized view, serverless-only) | ClickHouse Materialized View on `AggregatingMergeTree` |

**Total time:** ~60 minutes including setup (estimate — see the
Honesty section below; this flow has not been timed end-to-end
against a live workspace).
**Workflow:** the dashboard's six step buttons drive the migration. The
prompt files in [prompts/](prompts/) are what each button fires — you
don't need to paste them by hand.

---

## Phase 0 — Databricks setup (~5–20 min depending on path, estimate)

Pick the entry point that matches what you already have. All three end
with the same `migration_demo.tpch` workload sitting in a Databricks
workspace and the four `DATABRICKS_*` variables in `.env`.

### New Databricks environment

You have no Databricks workspace at all. Terraform provisions a
**serverless** workspace, then chains straight into the demo-object
module below — one command, no copy-pasting a workspace URL between
two applies.

The one manual step: create a Databricks **account** (if you don't have
one) and an **account-admin service principal** inside it. Terraform
authenticates *as* that service principal, so it cannot also create it.

#### Where each value comes from

`terraform/workspace/terraform.tfvars` needs three values. All three
come from the **account console** at
**<https://accounts.cloud.databricks.com>** — not from inside a
workspace.

| tfvar | Where to get it |
|---|---|
| `databricks_account_id` | Account console → click the **down arrow next to your username** (upper right) → copy **Account ID** |
| `databricks_client_id` | The service principal's **application ID**, shown when you generate its secret (below) |
| `databricks_client_secret` | Generated once, alongside the client ID (below) |

You must be an **account admin** to reach any of this. If you created
the account, you already are — the original signup is the account owner
and is an account admin automatically.

**1. Get the account ID.** In the account console, click the down arrow
next to your username in the upper-right corner and copy **Account ID**.
This only appears in the account console — it is *not* displayed
anywhere inside a workspace, which is a common few-minutes-lost moment.

**2. Create the service principal.** Account console → sidebar →
**User management** → **Service principals** tab → **Add service
principal** → give it a name (e.g. `migrationroom-terraform`) → **Add**.

**3. Give it the account admin role.** Still under **User management** →
**Service principals**, click the service principal you just made →
**Roles** tab → enable the account admin role. Account admin (not
workspace admin) is required: the module calls the account API to create
a workspace, and the chained `demo` apply then needs metastore-level
`CREATE CATALOG`, which account admins hold implicitly.

> The details page shows only roles assigned **directly**. A role
> inherited through group membership is active but its toggle will not
> appear enabled — so don't rely on the toggles to audit this.

**4. Generate the OAuth secret.** With the service principal still
selected, open the **Credentials & secrets** tab and generate a secret.
Set a lifetime in days (maximum **730**). Copy **both** the secret and
the **client ID** before closing the dialog.

> **The secret is displayed exactly once.** If you lose it, generate a
> new one — a service principal can hold up to five. The client ID is
> the same value as the service principal's application ID.

The equivalent route from inside a workspace, if you prefer it: your
username → **Settings** → **Identity and access** → next to **Service
principals** click **Manage** → select it → **Secrets** tab →
**Generate secret**. That route can create the secret but *not* read the
account ID, so you still need step 1 in the account console.

> **Cloud support:** this path is AWS-only. The module targets
> `accounts.cloud.databricks.com` and creates an AWS serverless
> workspace with `aws_region`. On GCP the account console lives at a
> different host, and Azure has no equivalent `databricks_mws_workspaces`
> flow — use the *existing workspace* path below instead.

> **Databricks Free Edition cannot use this path at all.** Free Edition
> has "no access to the account console or account-level APIs" and is
> capped at one workspace and one metastore per account, so there is no
> account console to collect these three values from and nothing to
> create. Free Edition users should use the *workload only* path below.
> Free Edition is also non-commercial-use only.

#### Then run it

```bash
cd sources/databricks/terraform/workspace
cp terraform.tfvars.example terraform.tfvars
# Edit: databricks_account_id, databricks_client_id, databricks_client_secret

cd ../../../..
make databricks-provision-workspace
```

`make databricks-provision-workspace` runs the `workspace` module,
then the `demo` module against the workspace it just created, then
prints where to capture the `.env` block:

```bash
cd sources/databricks/terraform/demo && terraform output -raw env_block >> ../../../../.env
```

If the region has no Unity Catalog metastore yet (rare, but possible
for a brand-new account), the chained `demo` apply fails outright
because there's nothing for the new workspace to attach to. Set
`create_metastore = true` in `terraform/workspace/terraform.tfvars`
and re-run — see
[terraform/workspace/README.md](terraform/workspace/README.md#create_metastore)
for how to tell in advance whether you need it. This is the most
likely first failure of this "one command" path.

See [terraform/workspace/README.md](terraform/workspace/README.md)
and [terraform/demo/README.md](terraform/demo/README.md) for what each
module creates and why account-admin (not workspace-admin) is required.

### Existing workspace, provision the demo objects

You already have a Databricks workspace with Unity Catalog. Terraform
creates a dedicated serverless SQL warehouse, a demo service principal
+ token, and (optionally) the S3 staging path, then seeds the workload
(which is what actually creates the catalog/schema — see
[terraform/demo/README.md](terraform/demo/README.md)) for you.

> **If this workspace has other users on it: read this before running
> `make databricks-provision`.** This module grants its demo service
> principal permission to use PATs by declaring the workspace's entire
> token-usage permission list, which **replaces** it rather than adding
> to it. It preserves access for the built-in `users` group, but if
> some other user or group already holds PAT access on this workspace,
> applying this module silently revokes it and deletes their active
> tokens. See the prominent warning in
> [terraform/demo/README.md](terraform/demo/README.md#prerequisites)
> for exactly what to check first and how to add an exception.

```bash
cd sources/databricks/terraform/demo
cp terraform.tfvars.example terraform.tfvars
# Edit: workspace_url, databricks_token
# (the workload setup script this module runs creates the catalog, and
# CREATE CATALOG is a metastore-level privilege, not a workspace-level
# one — see the README's Prerequisites for what the PAT's principal needs)

cd ../../../..
make databricks-provision
```

```bash
cd sources/databricks/terraform/demo && terraform output -raw env_block >> ../../../../.env
```

See [terraform/demo/README.md](terraform/demo/README.md) — in
particular the **re-apply caveat** if you set `enable_s3_staging =
true`: IAM propagation can make the first `apply` fail on
`databricks_external_location.staging`; running `terraform apply`
again succeeds once IAM catches up.

### Existing workspace, workload only (fully manual)

You want to do everything by hand instead of running Terraform. This
enumerates every step the two modules above automate.

This path needs **two identities**, mirroring the Terraform `demo`
module's design (`terraform/demo/main.tf`'s provisioner deliberately
runs the seeding script as the admin PAT, not as the demo principal's
read-only token): a **setup** principal with write access, used only
to provision and seed, and a **runtime** principal with read-only
access, whose token is the one that ends up in `.env`.

1. Create a Unity Catalog **catalog** and **schema**. The namespace
   must be **exactly** `migration_demo.tpch` — `setup_workload.sql`
   hard-codes that name in all 25 of its statements, so any other
   catalog/schema name fails at the first one.
2. Create a **serverless** SQL warehouse. Serverless is required for
   the `daily_order_summary` materialized-view augmentation — a
   classic warehouse works for everything else, but skips that one
   object (see the Troubleshooting entry below).
3. Create a **personal access token for the setup principal** — the
   identity you'll run step 7 as. It needs write access: `CREATE
   CATALOG` at the **metastore** (not workspace) level, plus `CREATE
   SCHEMA`, `CREATE TABLE`, and `MODIFY` (covers `ALTER TABLE`,
   `UPDATE`, `DELETE`) on the catalog from step 1. It also needs
   `USE CATALOG`, `USE SCHEMA`, and `SELECT` on the built-in `samples`
   catalog: step 7's `CREATE TABLE ... AS SELECT` runs **as this
   principal** and copies out of `samples.tpch`. An existing
   workspace-admin/account-admin PAT works fine here too, and usually
   needs no explicit grant on `samples` — account/workspace admins can
   already read it.
4. Grant `USE CATALOG`, `USE SCHEMA`, and `SELECT` on the demo catalog
   (only — it does not need `samples`, since it never runs the CTAS
   that reads from it) to a **separate, read-only principal**. This is
   the identity that ends up in `.env` for the playground to actually
   run as.
5. *(Optional, only for the S3-staged migration path)* create a Unity
   Catalog **external location** over your staging bucket with
   `WRITE FILES` granted.
6. Put the **setup** principal's token from step 3 in `.env` as
   `DATABRICKS_TOKEN` — temporarily, just for the next step:
   ```bash
   DATABRICKS_HOST=https://dbc-xxxxxxxx-xxxx.cloud.databricks.com
   DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/xxxxxxxxxxxxxxxx
   DATABRICKS_TOKEN=dapi................................
   DATABRICKS_NAMESPACE=migration_demo.tpch
   ```
7. Seed the workload:
   ```bash
   make databricks-setup
   ```
   `databricks-setup` installs `databricks-sql-connector`, then runs
   `sources/databricks/scripts/setup_workload.py`, which executes
   `setup_workload.sql` against your warehouse — the 8 TPC-H tables
   plus the eight augmentations above. **Once this succeeds, replace
   `DATABRICKS_TOKEN` in `.env` with the read-only principal's token
   from step 4** before starting the playground in Phase 1 — the
   runtime identity does not need, and should not have, write access.

---

## Phase 1 — Launch the playground (~5 min)

```bash
make up-databricks
```

`make up-databricks` regenerates `librechat.runtime.yaml` for the
`databricks` profile, pulls/builds images, and starts every service
including `databricks-mcp`. Default `make up` skips it — like
Snowflake and BigQuery, the Databricks MCP is profile-gated because it
needs account credentials in `.env`.

Check `docker compose ps` — `databricks-mcp` should show `healthy`
alongside the rest of the stack. Its healthcheck only probes that the
SSE endpoint responds; the actual Databricks connection is opened
lazily per tool call, so a healthy container does **not** by itself
prove your credentials are valid (see Troubleshooting).

Open **<https://localhost/dashboard/>** (accept the self-signed cert)
and sign in (`admin@playground.local` / `playground`). You'll land on
the **MigrationRoom** dashboard with the chat panel on the right.

In the **SETUP** card at the top:

- **Source**: pick `Databricks`. The chat panel auto-switches to the
  `Databricks → ClickHouse Cloud` agent.
- **Source database**: the `catalog.schema` pair, e.g.
  `migration_demo.tpch`.
- **Queries**: open **Edit · N OLAP** and confirm the OLAP queries are
  loaded (steps 1 and 4 use them, for schema design and rewrite).

---

## Phase 2 — Run the migration (~45 min, estimate)

The dashboard has **six step buttons** at the top of the **STEPS**
panel. Click each in order. All six are clickable at any time, so you
can re-fire a step (e.g. re-run validation after fixing the schema).

### Step 1 — Discover & Design Schema

Agent introspects the source via the `databricks-source` MCP
(`list_catalogs`, `list_schemas`, `list_tables`, `describe_table`,
`run_select_query` — never `run_python` for discovery), reads the OLAP
queries to drive `ORDER BY` choices, proposes the ClickHouse target
schema, and runs the DDL via `clickhousectl`.

**Watch in chat** for the agent's decisions: `Decimal(15, 2)` for
money, `Date32` for dates, `DateTime64(6, 'UTC')` vs `DateTime64(6)`
for the `TIMESTAMP` / `TIMESTAMP_NTZ` pair, `JSON` (not `String`) for
`o_metadata`, `Nested(...)` vs `Array(Tuple(...))` for
`l_shipping_events`, and an explicit call on `MATERIALIZED` vs `ALIAS`
for `o_orderyear`. **Confirm the target database name** when the agent
asks (default suggestion `migration_demo`).

### Step 2 — Migrate Data

Agent writes a short Python script using the `migrationkit` library,
dispatches it as a background job via `migration-runner`, issues
ONE `tail_python_job` to confirm `status=running`, then stops. The
dashboard's **Migration** tab streams live progress: rows/sec, ETA,
per-table progress bars, milestone events.

Large tables (over ~1M rows) can also take the S3-stage path if
`STAGING_S3_*` is set — the agent picks direct vs staged per table.

**This step is meant to look quiet in chat.** The agent dispatches and
stops by design (see [`docs/adding-a-source.md`](../../docs/adding-a-source.md)
on why polling isn't used); silence in the conversation while the
dashboard streams progress bars is expected, not stuck.

### Step 3 — Validate

Agent runs `Validator(...).validate()` — row count parity per table,
source vs target. Results land on the dashboard's **Validation** tab.
If anything mismatches the agent **stops and reports** in chat — fix
the schema and re-fire step 2, don't ask the agent to patch the target
by hand.

### Step 4 — Rewrite Queries

Agent translates each OLAP query from Databricks SQL to ClickHouse SQL
**in chat**. No script — this is a reasoning step. Walk through each
rewrite, push back on unfamiliar substitutions (`QUALIFY` → subquery,
`LATERAL VIEW explode` → `ARRAY JOIN`, `aggregate`/`filter` →
`arrayReduce`/`arrayFilter`, etc.).

### Step 5 — Benchmark

Agent runs `Benchmarker(...).benchmark(queries=[...])` — each query on
source and target, server-side timing on both. Results land on the
**Benchmark** tab as `source_ms / target_ms / speedup` per query.

Databricks timing comes from the SQL query-history REST API, with
`system.query.history` as a fallback; if neither is available
`server_ms` is `None` and wall-clock is shown instead — the agent
should say so rather than presenting wall time as server time. The
first query in a session also pays Databricks warehouse cold-start;
expect a throwaway `SELECT 1` before the real numbers.

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
made defensible choices: `Decimal` (not `Float`) for money, `Date32`
for dates, `JSON` (not a bare `String`) for `o_metadata`, a
`Nested`/`Map` decision for the nested lineitem columns, and a clear
call on the generated column and the materialized view.

**Row-count parity has one legitimate exception.** `lineitem` has
deletion vectors enabled, and `setup_workload.sql` deletes 500 rows
from it as part of seeding a real history to time-travel over. If a
partner (or another process) deletes more rows from the source table
**while step 2 is running**, the source count can legitimately drop
mid-migration — that's not a migration bug, and step 3's `Validator`
output should be read with that in mind rather than assumed to be
stale.

---

## Teardown and cost

Everything provisioned in Phase 0 bills against your own
Databricks/AWS account, not against MigrationRoom.

```bash
cd sources/databricks/terraform/demo && terraform destroy
cd ../workspace && terraform destroy   # only if you used the workspace path
```

Destroy `demo` before `workspace` — the workspace module has no
`demo` in scope and destroying it first takes everything `demo`
created (warehouse, tokens) down with it regardless of order,
but destroying in `demo` → `workspace` order lets each module report
what it removed cleanly.

**`terraform destroy` does not remove the `migration_demo` catalog.**
The catalog/schema are created by `setup_workload.sql` (`CREATE CATALOG
IF NOT EXISTS` / `CREATE SCHEMA IF NOT EXISTS`), not by a Terraform
resource — see `terraform/demo/README.md`'s "What this module creates"
for why. `terraform destroy` therefore has nothing to target and leaves
them behind. Drop them by hand for a full teardown:

```sql
DROP CATALOG migration_demo CASCADE;
```

Run that as the setup/provisioner identity or an account/workspace
admin — either has `MANAGE` on the catalog.

- The serverless SQL warehouse's `auto_stop_minutes` defaults to 10
  minutes of idle time before it suspends — an idle warehouse still
  bills, so don't raise this casually.
- If you enabled S3 staging, the staging bucket has a 7-day lifecycle
  expiry on staged objects, and the bucket is created with
  `force_destroy = true` so `terraform destroy` won't wedge on leftover
  objects in it.

---

## Troubleshooting

**`databricks-mcp` shows healthy but every tool call errors:**
Credentials are checked lazily, per call — not at container startup —
so a healthy container proves the SSE endpoint responds, not that
`DATABRICKS_HOST` / `DATABRICKS_HTTP_PATH` / `DATABRICKS_TOKEN` are
valid. Check `docker compose logs databricks-mcp`, fix `.env`, then
`docker compose --profile databricks restart databricks-mcp`.

**Chained `demo` apply fails because the workspace has no metastore:**
Some regions/accounts have no Unity Catalog metastore auto-provisioned
yet. Set `create_metastore = true` in
`terraform/workspace/terraform.tfvars` and re-apply — see
[terraform/workspace/README.md](terraform/workspace/README.md#create_metastore)
for how to check in advance whether you need it. This is the most
likely first failure of the "one command" new-environment path.

**`setup_workload.sql` fails on the hand-written `orders` table (`CREATE
OR REPLACE TABLE migration_demo.tpch.orders`):**
That DDL's column types were written without a live workspace to test
against and may not exactly match `samples.tpch.orders` on yours. Run
`DESCRIBE TABLE samples.tpch.orders`, compare against the column list
in `setup_workload.sql`, adjust the mismatched types, then re-run
`make databricks-setup`.

**First query of the session is slow:**
A stopped SQL warehouse takes 30s+ to serve its first statement. This
is warehouse cold-start, not a ClickHouse-vs-Databricks comparison —
run a throwaway `SELECT 1` before benchmarking, or note that query 1's
number includes cold-start.

**`VARIANT` column rejected / type doesn't exist:**
Needs DBSQL 2024.35+ or DBR 15.3+. Upgrade the SQL warehouse's channel
to Current, or use a newer runtime, then re-run `make databricks-setup`.

**Staged unload refused:**
The S3 path needs a Unity Catalog **external location** over the
staging bucket with `WRITE FILES` granted. `unload_to_s3` raises an
error saying exactly that; either provision the external location
(`enable_s3_staging=true` in the `demo` module) or let the agent fall
back to the direct path for that table.

**`terraform apply` on the `demo` module fails once on
`databricks_external_location.staging`, then succeeds on retry:**
Expected, not a bug. The external location validates itself by
assuming the freshly created IAM role, and IAM is eventually
consistent — a role that was just created can fail to assume for a
few seconds even after Terraform reports the attachment done. Just
run `terraform apply` again; don't delete the role or the storage
credential.

**`daily_order_summary` (and sample query 7) missing:**
The materialized-view augmentation is `@optional` in
`setup_workload.sql` — it needs a serverless SQL warehouse, and is
silently skipped on a classic one, which is a perfectly reasonable
demo environment otherwise. If it's absent, drop sample query 7 (and
its rewrite in `expected_ch_queries.sql`) rather than treating the
setup as broken.

**Serverless egress control blocking the staging bucket:**
If the workspace has a network connectivity configuration (NCC)
restricting serverless egress, the staging bucket's endpoint must be
added to the allowed list, or the serverless warehouse can't read or
write staged Parquet through the external location. This is a
workspace-level network setting outside Terraform's control here —
check with whoever manages the workspace's NCC.

---

## Honesty about what's been verified

No live migration has been run end-to-end against a real Databricks
workspace as part of building this source — that needs a workspace, a
SQL warehouse, and a token, none of which exist in the environment
this guide was written in. `terraform apply` has **not** been run for
either Terraform module; `terraform validate` proves the configuration
is well-formed, not that an apply succeeds. See each module's README
(["workspace"](terraform/workspace/README.md),
["demo"](terraform/demo/README.md)) for the specific attributes most
likely to need adjustment on first contact with a real account.

Likewise, `make up-databricks` — Phase 1's very first command — has
not actually been run in this environment: the `databricks-mcp` image
has not been built, and none of the Compose services have been booted
under the `databricks` profile. What's been verified is static: the
Dockerfile, compose service definition, and profile gating are
consistent with the other sources' equivalents, and `docker compose
config` accepts them. Whether the image builds cleanly and the
container reaches `healthy` has not been exercised.
