# AI Migration Assistant

A self-contained Docker Compose environment for hands-on AI-assisted database migration to ClickHouse Cloud. Partners use a chat interface backed by real MCP database connections to work through schema analysis, data migration, query rewriting, and performance optimisation — all with AI guidance.

![Architecture](docs/architecture.png)

> To regenerate the diagram after editing `docs/architecture.mmd`: `make diagram`

## Migration Sources

Three independent migration scenarios are included — each with its own dataset, guide, and pre-built LibreChat agent:

| Source | Dataset | Guide |
|---|---|---|
| **PostgreSQL → ClickHouse Cloud** | E-commerce platform (orders, events, sessions) | [sources/postgres/GUIDE.md](sources/postgres/GUIDE.md) |
| **ClickHouse OSS → ClickHouse Cloud** | Web analytics platform (pageviews, sessions, conversions) | [sources/clickhouse-oss/GUIDE.md](sources/clickhouse-oss/GUIDE.md) |
| **Snowflake → ClickHouse Cloud** | TPC-H + Snowflake-specific augmentations (VARIANT, TIMESTAMP_TZ, Stream, Dynamic Table, Clustering Key); partner-provided Snowflake account | [sources/snowflake/GUIDE.md](sources/snowflake/GUIDE.md) |

Want to contribute a new dataset or database engine? See **[docs/adding-a-source.md](docs/adding-a-source.md)**.

## What's included

**Local Docker Compose**
| Component | Purpose |
|---|---|
| PostgreSQL 16 | Source DB — pre-loaded e-commerce OLAP dataset |
| Postgres MCP server | Gives the AI agent full access to the Postgres source |
| ClickHouse OSS | Source DB — pre-loaded web analytics OLAP dataset |
| ClickHouse OSS MCP server | Gives the AI agent full access to the ClickHouse OSS source |
| Snowflake MCP server | Read-only access to a partner-provided Snowflake account (TPC-H + Snowflake-specific augmentations); opt-in via `make up-snowflake` |
| Migration Runner | In-chat Python sandbox — agent runs migration scripts directly, no local Python required |
| LibreChat | Provider-agnostic chat UI — bring your own API key |

The below components are part of the **ClickHouse Cloud**
| Component | Purpose |
|---|---|
| ClickHouse Cloud | Target DB — your Cloud service, ready for migration |
| ClickHouse Cloud MCP | Gives the AI agent read access to ClickHouse Cloud |
| ClickHouse Docs MCP | Lets the agent search ClickHouse docs on the fly |

## Prerequisites

- Docker Desktop 4.20+ (macOS/Windows) or Docker Engine + Docker Compose v2 (Linux) with at least 8 GB RAM
- `make` — pre-installed on macOS; on Debian/Ubuntu: `sudo apt-get install -y make`
- `git` — pre-installed on most systems; on Debian/Ubuntu: `sudo apt-get install -y git`
- `yq` — YAML processor used by `make setup`; on Debian/Ubuntu: `sudo snap install yq` or `sudo wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/local/bin/yq && sudo chmod +x /usr/local/bin/yq`
- An LLM API key (Anthropic Claude recommended, OpenAI also works)
- A [ClickHouse Cloud](https://clickhouse.cloud) account with a running service
- 10 GB free disk space


## Quick Start

```bash
git clone https://github.com/sishuoyang/ai-migration-assistant
cd ai-migration-assistant
make setup          # initialises submodules, injects agent skills, creates .env
```

Edit `.env` and add your LLM API key:
```bash
ANTHROPIC_API_KEY=sk-ant-...        # or OPENAI_API_KEY=sk-...
```

```bash
make up
```

Open **https://localhost** in your browser (accept the self-signed certificate warning).

> **First run:** Both databases seed on startup — allow 5–10 minutes total.
> - PostgreSQL generates ~10M rows (`DATASET_SIZE=medium`): `docker compose logs postgres -f`
> - ClickHouse OSS loads ~12.2M rows (fixed): `docker compose logs clickhouse-oss -f`

## Default credentials

| Service | Detail |
|---|---|
| LibreChat login | `admin@playground.local` / `playground` |
| Postgres host (internal) | `postgres:5432` |
| Postgres database | `ecommerce` |
| Postgres user / password | `playground` / `playground` |
| ClickHouse OSS host (internal) | `clickhouse-oss:8123` |
| ClickHouse OSS host (local) | `localhost:8123` |
| ClickHouse OSS user / password | `default` / *(empty)* |
| ClickHouse OSS database | `analytics` |
| ClickHouse Cloud | credentials from `.env` (`CLICKHOUSE_CLOUD_HOST/USER/PASSWORD`) |

## Using the playground

1. Sign in at **https://localhost** with `admin@playground.local` / `playground`.
2. **Pick the agent that matches your source.** The agent dropdown lists three pre-built agents, each with the right MCPs and migration prompt already attached — no model toggling or MCP-checkbox wrangling required:
   - `Postgres → ClickHouse Cloud`
   - `Snowflake → ClickHouse Cloud` (requires `make up-snowflake` for the source MCP to be reachable)
   - `ClickHouse OSS → ClickHouse Cloud`
3. Follow the guide for that source:
   - **Postgres:** [sources/postgres/GUIDE.md](sources/postgres/GUIDE.md)
   - **Snowflake:** [sources/snowflake/GUIDE.md](sources/snowflake/GUIDE.md)
   - **ClickHouse OSS:** [sources/clickhouse-oss/GUIDE.md](sources/clickhouse-oss/GUIDE.md)
4. Use the copy-paste prompts in the corresponding `prompts/` directory for each phase.

> **Want a different model per agent?** Set `AGENT_PROVIDER_<SOURCE>` / `AGENT_MODEL_<SOURCE>` in `.env` (e.g. `AGENT_MODEL_SNOWFLAKE=gpt-4o`) before first start, or use the LibreChat agent-settings UI to change a single agent's model afterward. To re-apply env changes, run `make reset-agent`.

## Dataset sizes

### PostgreSQL source (configurable)

| Size | Events | Seed time | RAM needed |
|---|---|---|---|
| `small` | 1M | ~1 min | 4 GB |
| `medium` (default) | 10M | 5–10 min | 8 GB |
| `large` | 30M | 20–30 min | 16 GB |

Set `DATASET_SIZE=small` in `.env` for faster startup on low-spec machines.

### ClickHouse OSS source (fixed)

| Table | Rows |
|---|---|
| `analytics.projects` | 1,000 |
| `analytics.sessions` | 2,000,000 |
| `analytics.pageviews` | 10,000,000 |
| `analytics.conversions` | 200,000 |
| `analytics.daily_stats` | pre-aggregated from sessions |

Seed time: ~3–5 minutes on first run.

## Enable LLM Tracing with Langfuse (optional)

LibreChat has native Langfuse support — add three lines to `.env` to get token usage,
latency, and full conversation traces for every AI interaction in the playground.

1. Sign up free at [cloud.langfuse.com](https://cloud.langfuse.com) → **Settings → API Keys**
2. Add to `.env`:
```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```
3. `docker compose restart librechat`

## Customising the Agent System Prompt

The agent's behaviour is controlled by a set of modular instruction files:

| File | Purpose |
|---|---|
| [librechat/clickhouse-cloud-instructions.md](librechat/clickhouse-cloud-instructions.md) | Base rules + best-practices, injected into `mcpServers.clickhousectl.serverInstructions` (shared by all three agents) |
| [librechat/sources/postgres-instructions.md](librechat/sources/postgres-instructions.md) | Postgres-specific rules, injected into `mcpServers.postgres-source.serverInstructions` |
| [librechat/sources/snowflake-instructions.md](librechat/sources/snowflake-instructions.md) | Snowflake-specific rules, injected into `mcpServers.snowflake-source.serverInstructions` |
| [librechat/sources/clickhouse-oss-instructions.md](librechat/sources/clickhouse-oss-instructions.md) | ClickHouse OSS-specific rules, injected into `mcpServers.clickhouse-oss-source.serverInstructions` |

Each pre-built agent attaches exactly the MCPs for its source, so it transparently receives only the relevant `serverInstructions` — no per-agent prompt is set. `make setup` rebuilds the injected blocks idempotently. Don't edit anything below the `--- Migration Rules (auto-injected …) ---` marker in `librechat.yaml`; the MCP-purpose blurb above the marker IS hand-editable.

**To add a new migration source:** see **[docs/adding-a-source.md](docs/adding-a-source.md)** for the full walkthrough — Docker service, MCP wiring, and system prompt authoring.

**To apply changes:**
```bash
# 1. Edit the instruction file(s)
$EDITOR librechat/sources/clickhouse-oss-instructions.md

# 2. Rebuild and inject into librechat.yaml
make setup

# 3. Reload the agent
docker compose restart librechat
```

## Commands

```bash
make setup               # first-time setup (submodules + agent skills + .env)
make up                  # start the playground (Postgres + ClickHouse OSS sources)
make up-snowflake        # also start the Snowflake source MCP (needs SNOWFLAKE_* in .env)
make snowflake-setup     # set up MIGRATION_DEMO.RETAIL workload in existing Snowflake (Path A)
make snowflake-provision # provision a fresh Snowflake demo env with Terraform (Path B)
make down                # stop without removing data
make reset               # destroy volumes and start fresh
make reset-agent         # delete + recreate the three pre-built agents (e.g. after changing AGENT_PROVIDER/AGENT_MODEL in .env)
make health              # check all services are healthy
make migration-status    # check progress of a running migration script (target row counts)
make logs                # tail all service logs
make diagram             # regenerate docs/architecture.png from docs/architecture.mmd
```

## Troubleshooting

**MCP servers not showing in LibreChat:**
Verify `interface.mcpServers.use: true` is set in `librechat/librechat.yaml`. Each pre-built agent attaches its MCPs automatically — if a picked agent has no MCPs in its panel, run `make reset-agent` to repair it.

**Agent not appearing in the dropdown:**
`docker compose logs librechat-init` should report `✅ Created '<Agent name>'` (or `already configured — skipping`). On the very first `make up`, init runs after LibreChat reports healthy — give it ~10 seconds, then refresh the page.

**Postgres seed takes too long:**
Set `DATASET_SIZE=small` in `.env`, run `make reset`.

**Out of memory during seed:**
Increase Docker Desktop memory to 8+ GB (Docker Desktop → Settings → Resources).

**Artifacts not rendering / Sandpack crashes with `crypto.subtle` error:**
Artifacts require a secure context. Access LibreChat via `https://` (port 443) through the nginx proxy — `http://` on port 3080 will not work for artifact rendering. Accept the self-signed certificate warning in your browser.

**ClickHouse OSS MCP (`clickhouse-oss-source`) not appearing:**
The MCP server uses `npx` on first start and may take 30–60 seconds to download packages.
Check logs: `docker compose logs clickhouse-oss-mcp -f`. If it fails, restart:
`docker compose restart clickhouse-oss-mcp`.

**ClickHouse OSS seed taking longer than expected:**
The init SQL loads 12.2M rows using `numbers()` — this is compute-bound, not I/O-bound.
On slower machines allow up to 10 minutes: `docker compose logs clickhouse-oss -f`.
