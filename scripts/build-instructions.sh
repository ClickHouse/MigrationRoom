#!/usr/bin/env bash
# Builds the clickhouse-cloud serverInstructions in librechat.yaml by combining:
#   1. librechat/clickhouse-cloud-instructions.md  — base rules (generic, all sources)
#   2. librechat/sources/*.md                      — source-specific rules (one file per source)
#   3. agent-skills/skills/clickhouse-best-practices/AGENTS.md — ClickHouse best practices
#
# To add a new source: create librechat/sources/<source-name>-instructions.md
# Run via: make setup  (or manually: bash scripts/build-instructions.sh)
set -euo pipefail

TEMPLATE_FILE="librechat/clickhouse-cloud-instructions.md"
SOURCES_DIR="librechat/sources"
SKILLS_FILE="agent-skills/skills/clickhouse-best-practices/AGENTS.md"
LOCAL_SKILLS="${HOME}/.agents/skills/clickhouse-best-practices/AGENTS.md"
LIBRECHAT_YAML="librechat/librechat.yaml"

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

if [ ! -f "$TEMPLATE_FILE" ]; then
    echo "❌ Template not found: $TEMPLATE_FILE"
    exit 1
fi

# ── Collect source-specific instructions ────────────────────────────────────
SOURCES_CONTENT=""
if [ -d "$SOURCES_DIR" ]; then
    for f in "$SOURCES_DIR"/*.md; do
        [ -f "$f" ] || continue
        source_id=$(basename "$f" | sed 's/-instructions\.md$//')
        echo "  source:       $f ($(wc -l < "$f") lines)"
        SOURCES_CONTENT+=$'\n\n---\n\n'
        SOURCES_CONTENT+="## Source-Specific Rules: ${source_id}"$'\n\n'
        SOURCES_CONTENT+="$(cat "$f")"$'\n'
    done
fi

# ── Combine base + sources + agent skills ────────────────────────────────────
echo "Building serverInstructions from:"
echo "  base:         $TEMPLATE_FILE ($(wc -l < "$TEMPLATE_FILE") lines)"

if [ -n "$SKILLS_FILE" ]; then
    echo "  best-practices: $SKILLS_FILE ($(wc -l < "$SKILLS_FILE") lines)"
    export YQ_VALUE="$(cat "$TEMPLATE_FILE")${SOURCES_CONTENT}$(cat "$SKILLS_FILE")"
else
    export YQ_VALUE="$(cat "$TEMPLATE_FILE")${SOURCES_CONTENT}"
fi

yq -i '.mcpServers.clickhouse-cloud.serverInstructions = strenv(YQ_VALUE)' "$LIBRECHAT_YAML"

TOTAL_CHARS=${#YQ_VALUE}
echo "✅ serverInstructions injected ($TOTAL_CHARS chars total)"
