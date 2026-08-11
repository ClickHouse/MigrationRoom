#!/bin/sh
# Preflights .env before `docker compose up` so a missing/incomplete .env
# fails fast with a useful message instead of letting LibreChat crash with
# an opaque Passport error:
#   error: There was an uncaught error: JwtStrategy requires a secret or key
#
# That happens when JWT_SECRET / JWT_REFRESH_SECRET / CREDS_KEY / CREDS_IV
# are missing from .env — which is exactly what happens if a guide's
# `terraform output -raw env_block >> .env` step runs before `make setup`:
# `>>` CREATES .env if it doesn't exist, so you end up with a .env
# containing only the appended Terraform block and none of the secrets
# `make setup`'s `cp .env.example .env` would have provided.
#
# Reads .env WITHOUT sourcing it: a value containing spaces, quotes, or
# shell metacharacters must never be executed, only pattern-matched. All
# checks are `grep -E '^VAR=<non-empty>'` against the raw file. Never
# prints a variable's value — names only.
#
# Called as a single recipe line from the Makefile's up / up-snowflake /
# up-bigquery / up-databricks targets. GNU Make 3.81 (macOS default) has
# no `.ONESHELL` and runs each recipe line in its own shell, so a
# multi-line inline shell block in the Makefile silently breaks — this
# script must stay invoked as one line: `@bash scripts/check-env.sh`.

set -eu

ENV_FILE="${MR_ENV_FILE:-.env}"

# has_nonempty_var VAR FILE — true if FILE has a line "VAR=<value>" where
# <value> contains at least one non-whitespace character. Anchored at
# start-of-line, so e.g. "^JWT_SECRET=" cannot match "JWT_REFRESH_SECRET="
# or a commented-out "# JWT_SECRET=...".
has_nonempty_var() {
    grep -Eq "^$1=.*[^[:space:]]" "$2" 2>/dev/null
}

if [ ! -f "$ENV_FILE" ]; then
    echo "❌ $ENV_FILE not found." >&2
    echo "" >&2
    echo "   Run: make setup" >&2
    echo "   That does 'cp .env.example .env', which seeds JWT_SECRET," >&2
    echo "   JWT_REFRESH_SECRET, CREDS_KEY, and CREDS_IV — the four secrets" >&2
    echo "   LibreChat needs just to boot. Without them it exits 1 at" >&2
    echo "   startup with:" >&2
    echo "     error: There was an uncaught error: JwtStrategy requires a secret or key" >&2
    exit 1
fi

missing=""
for var in JWT_SECRET JWT_REFRESH_SECRET CREDS_KEY CREDS_IV; do
    if ! has_nonempty_var "$var" "$ENV_FILE"; then
        missing="$missing $var"
    fi
done

if [ -n "$missing" ]; then
    echo "❌ $ENV_FILE is missing (or has an empty value for):$missing" >&2
    echo "" >&2
    echo "   These four are required for LibreChat to boot. Without them it" >&2
    echo "   exits 1 at startup with:" >&2
    echo "     error: There was an uncaught error: JwtStrategy requires a secret or key" >&2
    echo "" >&2
    echo "   Remedy:" >&2
    echo "   - Haven't run 'make setup' yet? Run it — it does 'cp .env.example" >&2
    echo "     .env', which seeds all four." >&2
    echo "   - Already ran 'make setup' but still see this? $ENV_FILE was likely" >&2
    echo "     created by a '>> $ENV_FILE' append (e.g. 'terraform output -raw" >&2
    echo "     env_block >> .env') that ran BEFORE 'make setup' — '>>' CREATES" >&2
    echo "     the file if it doesn't exist, so $ENV_FILE ended up containing only" >&2
    echo "     that appended block. Recover without losing it:" >&2
    echo "       cp $ENV_FILE ${ENV_FILE}.bak" >&2
    echo "       cp .env.example $ENV_FILE" >&2
    echo "       cat ${ENV_FILE}.bak >> $ENV_FILE   # re-append your appended block" >&2
    exit 1
fi

# --- Warn-only checks below: needed for the migration, not for boot. ---

llm_key_present=0
for var in ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_KEY BEDROCK_AWS_ACCESS_KEY_ID; do
    if has_nonempty_var "$var" "$ENV_FILE"; then
        llm_key_present=1
    fi
done
if [ "$llm_key_present" -eq 0 ]; then
    echo "⚠️  No LLM provider key set in $ENV_FILE (checked ANTHROPIC_API_KEY," >&2
    echo "   OPENAI_API_KEY, GOOGLE_KEY, BEDROCK_AWS_ACCESS_KEY_ID — any one is" >&2
    echo "   enough). The stack will still start, but the agent can't run any" >&2
    echo "   migration step until one is set." >&2
fi

ch_missing=""
for var in CLICKHOUSE_CLOUD_HOST CLICKHOUSE_CLOUD_PASSWORD; do
    if ! has_nonempty_var "$var" "$ENV_FILE"; then
        ch_missing="$ch_missing $var"
    fi
done
if [ -n "$ch_missing" ]; then
    echo "⚠️  $ENV_FILE is missing:$ch_missing — the ClickHouse Cloud target" >&2
    echo "   won't be reachable until these are set. The stack will still start." >&2
fi

exit 0
