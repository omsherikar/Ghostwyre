#!/usr/bin/env bash
# PostToolUse hook — auto-format Python files after Claude writes/edits them,
# so formatting noise never shows up in review. No-op if ruff isn't installed.
set -uo pipefail

input="$(cat)"
file="$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_response.filePath // ""')"

[[ "$file" == *.py ]] || exit 0
[[ -f "$file" ]] || exit 0
command -v ruff >/dev/null 2>&1 || exit 0

ruff format "$file" >/dev/null 2>&1 || true
ruff check --fix "$file" >/dev/null 2>&1 || true
exit 0
