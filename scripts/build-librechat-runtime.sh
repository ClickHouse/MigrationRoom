#!/usr/bin/env bash
# Generates librechat/librechat.runtime.yaml from librechat/librechat.yaml
# by removing MCP servers for sources whose Compose profile isn't active.
#
# LibreChat opens an SSE session to every entry in `mcpServers` at boot and
# retries forever when the host is unreachable — so leaving snowflake-source
# / bigquery-source declared while their containers aren't running produces
# an endless "Transport error (transient, will reconnect)" loop in the log.
#
# Active profiles come from $COMPOSE_PROFILES (comma- or space-separated),
# which Compose itself reads natively. The Makefile exports it for each
# `up*` target so the same value drives `docker compose`, this script, and
# the librechat-init container.
#
# Optional sources (gated by Compose profile -> only included when profile is active):
#   snowflake-source -> profile "snowflake"
#   bigquery-source  -> profile "bigquery"
# All other MCPs are unconditional.

set -euo pipefail

SOURCE_FILE="librechat/librechat.yaml"
RUNTIME_FILE="librechat/librechat.runtime.yaml"

if [ ! -f "$SOURCE_FILE" ]; then
    echo "❌ Source librechat config not found: $SOURCE_FILE" >&2
    exit 1
fi
if ! command -v yq &>/dev/null; then
    echo "⚠️  yq not found — copying $SOURCE_FILE -> $RUNTIME_FILE unfiltered." >&2
    echo "   Install: brew install yq  (macOS)  /  snap install yq  (Linux)" >&2
    cp "$SOURCE_FILE" "$RUNTIME_FILE"
    exit 0
fi

profiles_csv="${COMPOSE_PROFILES:-}"
# Normalize "snowflake bigquery" / "snowflake,bigquery" -> ",snowflake,bigquery,"
normalized=",$(echo "$profiles_csv" | tr ' ' ',' | tr -s ','),"

is_active() {
    local profile="$1"
    [[ "$normalized" == *",${profile},"* ]]
}

# Start from a clean copy of the source.
cp "$SOURCE_FILE" "$RUNTIME_FILE"

removed=()
kept=()

drop_mcp() {
    local mcp_key="$1"
    local host="$2"
    MCP_KEY="$mcp_key" HOST="$host" \
        yq -i 'del(.mcpServers[env(MCP_KEY)]) | .mcpSettings.allowedDomains |= map(select(. != env(HOST)))' \
        "$RUNTIME_FILE"
}

# snowflake-source: MCP key "snowflake-source", host points at the shim
if is_active snowflake; then
    kept+=("snowflake-source")
else
    drop_mcp "snowflake-source" "snowflake-source-shim"
    # snowflake-source itself is also in allowedDomains historically — strip it too
    HOST="snowflake-source" yq -i '.mcpSettings.allowedDomains |= map(select(. != env(HOST)))' "$RUNTIME_FILE"
    removed+=("snowflake-source")
fi

# bigquery-source
if is_active bigquery; then
    kept+=("bigquery-source")
else
    drop_mcp "bigquery-source" "bigquery-source"
    removed+=("bigquery-source")
fi

echo "Runtime librechat config: $RUNTIME_FILE"
echo "  Active profiles: ${profiles_csv:-<none>}"
if [ ${#kept[@]} -gt 0 ]; then
    echo "  Kept optional MCPs:    ${kept[*]}"
fi
if [ ${#removed[@]} -gt 0 ]; then
    echo "  Removed optional MCPs: ${removed[*]}"
fi
