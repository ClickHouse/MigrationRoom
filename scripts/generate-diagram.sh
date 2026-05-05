#!/usr/bin/env bash
# Generates docs/architecture.png from docs/architecture.mmd
# Requires Node.js (npx is used to run mermaid-cli without a global install)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
MMD_FILE="$ROOT_DIR/docs/architecture.mmd"
PNG_FILE="$ROOT_DIR/docs/architecture.png"

if [ ! -f "$MMD_FILE" ]; then
    echo "❌ Source not found: $MMD_FILE"
    exit 1
fi

if ! command -v npx &>/dev/null; then
    echo "❌ npx not found — install Node.js from https://nodejs.org"
    exit 1
fi

echo "Generating architecture diagram..."
npx -y @mermaid-js/mermaid-cli \
    --input  "$MMD_FILE" \
    --output "$PNG_FILE" \
    --backgroundColor white \
    --width  1400 \
    --scale  2

echo "✅ Diagram saved: $PNG_FILE"
