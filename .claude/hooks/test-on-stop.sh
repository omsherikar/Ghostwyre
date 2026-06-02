#!/usr/bin/env bash
# Stop hook — run the fast test subset when Claude finishes a task and surface
# breakage immediately. Non-blocking: reports via systemMessage, never halts.
# No-op if pytest isn't installed or there's no test setup yet.
set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
command -v pytest >/dev/null 2>&1 || exit 0
[[ -d tests || -f pytest.ini || -f pyproject.toml ]] || exit 0

# Fast subset only: skip anything marked @pytest.mark.slow, stop at first failure.
out="$(pytest -q -x -m 'not slow' 2>&1)"
code=$?

# 0 = passed, 5 = no tests collected — both fine, stay silent.
[[ $code -eq 0 || $code -eq 5 ]] && exit 0

summary="$(printf '%s' "$out" | tail -n 15)"
jq -n --arg s "$summary" '{systemMessage: ("⚠️  Fast tests failing after this change:\n" + $s)}'
exit 0
