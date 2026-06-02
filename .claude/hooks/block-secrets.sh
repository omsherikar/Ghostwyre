#!/usr/bin/env bash
# PreToolUse hook — block file writes / commits that contain live secrets.
# Reads the tool-call JSON on stdin; denies via permissionDecision when a secret
# pattern is found. Matched against Write, Edit, and Bash tool calls.
set -uo pipefail

input="$(cat)"
tool="$(printf '%s' "$input" | jq -r '.tool_name // ""')"

path=""
payload=""
case "$tool" in
  Write)
    path="$(printf '%s' "$input" | jq -r '.tool_input.file_path // ""')"
    payload="$(printf '%s' "$input" | jq -r '.tool_input.content // ""')"
    ;;
  Edit)
    path="$(printf '%s' "$input" | jq -r '.tool_input.file_path // ""')"
    payload="$(printf '%s' "$input" | jq -r '.tool_input.new_string // ""')"
    ;;
  Bash)
    payload="$(printf '%s' "$input" | jq -r '.tool_input.command // ""')"
    ;;
  *)
    exit 0
    ;;
esac

deny() {
  jq -n --arg reason "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 0
}

# 1) Never write a real .env file (placeholder variants are fine).
if [[ -n "$path" ]]; then
  base="$(basename "$path")"
  case "$base" in
    .env.example|.env.sample|.env.template) : ;;
    .env|.env.*)
      deny "Refusing to write '$path' — .env files hold live secrets and must never be committed. Use .env.example with placeholder values." ;;
  esac
fi

# 2) Known token shapes: Slack (xox[bpars]-), Anthropic (sk-ant-), X bearer (run of A's).
if printf '%s' "$payload" | grep -Eq \
  'xox[bpars]-[0-9A-Za-z-]{10,}|sk-ant-[0-9A-Za-z_-]{20,}|AAAAAAAAAAAAAAAAAAAAAA[0-9A-Za-z%]{20,}'; then
  deny "A value matching a live secret (Slack / Anthropic / X token) was detected. Load it from the environment via pydantic-settings — never hardcode tokens."
fi

# 3) Generic hardcoded credential assignment with a real-looking value.
if printf '%s' "$payload" | grep -Eiq \
  '(api[_-]?key|secret|token|signing[_-]?secret|password)["'"'"' ]*[:=]["'"'"' ]*[0-9A-Za-z/+_-]{16,}'; then
  if ! printf '%s' "$payload" | grep -Eiq 'your[_-]|example|placeholder|xxxx|<[^>]+>|changeme|dummy|test[_-]?token|getenv|environ|settings\.'; then
    deny "A hardcoded credential assignment was detected. Load it from the environment (pydantic-settings) instead of embedding the value."
  fi
fi

exit 0
