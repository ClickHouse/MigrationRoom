#!/usr/bin/env bash
# Builds serverInstructions in librechat.yaml for each MCP server:
#
#   clickhousectl.serverInstructions =
#       librechat/clickhouse-cloud-instructions.md                       (base, all sources)
#     + agent-skills/skills/clickhouse-best-practices/AGENTS.md          (best practices)
#
#   <id>-source.serverInstructions = <existing blurb in librechat.yaml>
#     + librechat/sources/<id>-instructions.md                           (source-specific rules)
#
# The source-MCP blurb is preserved (it's MCP-purpose config, not migration rules);
# the rules are appended below a marker so re-runs are idempotent.
#
# To add a new source:
#   1. Create librechat/sources/<id>-instructions.md
#   2. Add mcpServers.<id>-source entry in librechat/librechat.yaml
#   3. Run: make setup
set -euo pipefail

BASE_FILE="librechat/clickhouse-cloud-instructions.md"
SOURCES_DIR="librechat/sources"
SKILLS_FILE="agent-skills/skills/clickhouse-best-practices/AGENTS.md"
LOCAL_SKILLS="${HOME}/.agents/skills/clickhouse-best-practices/AGENTS.md"
LIBRECHAT_YAML="librechat/librechat.yaml"
RULES_MARKER="--- Migration Rules (auto-injected, do not edit below) ---"

# ── Resolve agent-skills path ────────────────────────────────────────────────
if [ -f "$SKILLS_FILE" ]; then
    echo "Using agent-skills from submodule: $SKILLS_FILE"
elif [ -f "$LOCAL_SKILLS" ]; then
    echo "Using agent-skills from local install: $LOCAL_SKILLS"
    SKILLS_FILE="$LOCAL_SKILLS"
else
    echo "⚠️  ClickHouse agent-skills not found."
    echo "   Expected: $SKILLS_FILE"
    echo "   Fallback: $LOCAL_SKILLS"
    echo "   Run: make setup  (clones agent-skills automatically)"
    echo "   Continuing without best practices."
    SKILLS_FILE=""
fi

# ── Check dependencies ───────────────────────────────────────────────────────
if ! command -v yq &>/dev/null; then
    echo "⚠️  yq not found — serverInstructions not updated."
    echo "   macOS:  brew install yq"
    echo "   Linux:  snap install yq  OR  wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/local/bin/yq && chmod +x /usr/local/bin/yq"
    exit 0
fi

if [ ! -f "$BASE_FILE" ]; then
    echo "❌ Base prompt not found: $BASE_FILE"
    exit 1
fi
if [ ! -f "$LIBRECHAT_YAML" ]; then
    echo "❌ librechat.yaml not found: $LIBRECHAT_YAML"
    exit 1
fi

# ── 1. Inject base + best-practices into clickhousectl ───────────────────────
echo
echo "Injecting clickhousectl.serverInstructions:"
echo "  base:           $BASE_FILE ($(wc -l < "$BASE_FILE") lines)"
if [ -n "$SKILLS_FILE" ]; then
    echo "  best-practices: $SKILLS_FILE ($(wc -l < "$SKILLS_FILE") lines)"
    export YQ_VALUE="$(cat "$BASE_FILE")"$'\n\n'"$(cat "$SKILLS_FILE")"
else
    export YQ_VALUE="$(cat "$BASE_FILE")"
fi
yq -i '.mcpServers.clickhousectl.serverInstructions = strenv(YQ_VALUE)' "$LIBRECHAT_YAML"
echo "  ✅ clickhousectl ($(printf '%s' "$YQ_VALUE" | wc -c) chars)"

# ── 2. Inject each source's rules into its source MCP ────────────────────────
echo
echo "Injecting per-source rules:"
if [ ! -d "$SOURCES_DIR" ]; then
    echo "  (no $SOURCES_DIR directory — skipping)"
    exit 0
fi

shopt -s nullglob
for src_file in "$SOURCES_DIR"/*-instructions.md; do
    source_id=$(basename "$src_file" | sed 's/-instructions\.md$//')
    mcp_key="${source_id}-source"

    # Confirm the corresponding MCP is declared in librechat.yaml. Without
    # this entry the yq assignment would silently create an orphan key that
    # LibreChat ignores — fail loudly instead.
    if ! yq -e ".mcpServers.\"${mcp_key}\"" "$LIBRECHAT_YAML" >/dev/null 2>&1; then
        echo "  ❌ $src_file: no .mcpServers.${mcp_key} entry in $LIBRECHAT_YAML"
        echo "     Add an entry for the ${mcp_key} MCP server, or rename the file."
        exit 1
    fi

    # Read current serverInstructions (the MCP-purpose blurb), strip any
    # previously appended rules block, then re-append. This keeps re-runs
    # idempotent without growing the field on every build.
    export MCP_KEY="$mcp_key"
    blurb=$(yq -r '.mcpServers[strenv(MCP_KEY)].serverInstructions // ""' "$LIBRECHAT_YAML")
    blurb_only="${blurb%%${RULES_MARKER}*}"
    # Trim trailing whitespace/newlines from the blurb so the marker spacing
    # is consistent.
    blurb_only="${blurb_only%$'\n'}"
    blurb_only="${blurb_only%$'\n'}"

    rules=$(cat "$src_file")
    export YQ_VALUE="${blurb_only}"$'\n\n'"${RULES_MARKER}"$'\n\n'"${rules}"
    yq -i '.mcpServers[strenv(MCP_KEY)].serverInstructions = strenv(YQ_VALUE)' "$LIBRECHAT_YAML"
    echo "  ✅ ${mcp_key} ← ${src_file} ($(wc -l < "$src_file") lines, total $(printf '%s' "$YQ_VALUE" | wc -c) chars)"
done

echo
echo "✅ All serverInstructions injected."
