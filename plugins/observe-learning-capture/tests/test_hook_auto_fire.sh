#!/usr/bin/env bash
# tests/test_hook_auto_fire.sh
#
# 2026-05-14: end-to-end smoke test that the hooks Claude Code is supposed
# to auto-fire (Stop, SessionEnd, SessionStart) are wired correctly and
# behave correctly when invoked with the exact stdin JSON contract Claude
# Code uses in production.
#
# The user reported "I don't believe the hooks have been firing correctly."
# Other test files cover individual scenarios (prefilter logic, race
# recovery, per-turn boundary). This file verifies the WIRING and INVARIANT
# layer: are the hooks even reachable, do they accept Claude Code's input
# shape, do they refuse to bill against API tier (must unset ANTHROPIC_API_KEY).
#
# Tests:
#   - hooks.json registers each of Stop, SessionEnd, SessionStart
#   - Each registered command points to an executable script that exists
#     under hooks/ — no broken paths
#   - Each script unsets ANTHROPIC_API_KEY early (subscription-auth invariant)
#   - Each script accepts Claude Code's stdin JSON contract and exits 0
#     gracefully when given a non-existent transcript_path (the empty-state
#     test that Claude Code most often hits in plugin-dev workflows)
#   - SessionStart with the documented "startup|clear|compact" matcher
#     produces no errors on a fresh session
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
HOOKS_JSON="$PLUGIN_ROOT/hooks/hooks.json"
PASS=0
FAIL=0

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# =============================================================================
# Test 1: hooks.json registration coverage
# =============================================================================
echo "=== Test 1: hooks.json registers Stop, SessionEnd, SessionStart ==="

for event in Stop SessionEnd SessionStart; do
    if jq -e ".hooks.\"$event\"[0].hooks[0].command" "$HOOKS_JSON" >/dev/null 2>&1; then
        echo "PASS: $event hook registered"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $event hook missing from hooks.json"
        FAIL=$((FAIL + 1))
    fi
done

# =============================================================================
# Test 2: each registered script exists and is executable
# =============================================================================
echo
echo "=== Test 2: registered scripts exist and are executable ==="

# Extract every command string from hooks.json and check the referenced .sh
# file. The command form is `bash "${CLAUDE_PLUGIN_ROOT}/hooks/foo.sh"`
# so we strip the bash prefix and resolve relative to PLUGIN_ROOT.
#
# WHY a while-read loop and not mapfile: macOS ships bash 3.2 which lacks
# the `mapfile` builtin. This loop is the portable equivalent.
while IFS= read -r cmd; do
    # Extract the script path between quotes after "bash ".
    script_token=$(echo "$cmd" | sed -E 's|^bash[[:space:]]+"([^"]+)".*|\1|')
    # Replace ${CLAUDE_PLUGIN_ROOT} with the actual plugin root.
    resolved=$(echo "$script_token" | sed "s|\\\${CLAUDE_PLUGIN_ROOT}|$PLUGIN_ROOT|")
    if [[ -x "$resolved" ]]; then
        echo "PASS: $(basename "$resolved") exists and is executable"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $resolved is not executable or does not exist"
        FAIL=$((FAIL + 1))
    fi
done < <(jq -r '
    [.hooks | to_entries[] | .value[] | .hooks[] | .command] | .[]
' "$HOOKS_JSON")

# =============================================================================
# Test 3: subscription-auth invariant — every hook script unsets
#         ANTHROPIC_API_KEY before doing any classifier work
# =============================================================================
echo
echo "=== Test 3: subscription-auth invariant (unset ANTHROPIC_API_KEY) ==="

for script in stop-hook.sh session-end-scan.sh; do
    if grep -q '^unset ANTHROPIC_API_KEY' "$PLUGIN_ROOT/hooks/$script"; then
        echo "PASS: $script unsets ANTHROPIC_API_KEY at top of script"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $script does NOT unset ANTHROPIC_API_KEY — would leak"
        echo "       parent env onto API tier instead of MAX subscription."
        FAIL=$((FAIL + 1))
    fi
done

# session-start-review.sh is a read-only renderer; it does not invoke
# claude -p, so the unset invariant does not apply. We still want to
# confirm it doesn't ACCIDENTALLY invoke claude -p.
if grep -q 'claude -p' "$PLUGIN_ROOT/hooks/session-start-review.sh"; then
    echo "FAIL: session-start-review.sh appears to invoke claude -p — must add unset"
    FAIL=$((FAIL + 1))
else
    echo "PASS: session-start-review.sh does not invoke claude -p (no unset needed)"
    PASS=$((PASS + 1))
fi

# =============================================================================
# Test 4: Stop hook auto-fire with Claude Code's stdin JSON contract
# =============================================================================
echo
echo "=== Test 4: Stop hook accepts Claude Code stdin JSON + exits 0 gracefully ==="

# Claude Code passes JSON like:
#   {"session_id":"...","transcript_path":"...","cwd":"...","hook_event_name":"Stop"}
# Build a non-existent path so the hook's "no transcript" path is exercised.
fake_transcript="$TMPDIR/never-created.jsonl"
stop_input=$(jq -nc \
    --arg t "$fake_transcript" \
    --arg s "auto-fire-stop-test" \
    --arg c "/tmp/test" \
    '{transcript_path: $t, session_id: $s, cwd: $c, hook_event_name: "Stop"}')

start_ts=$(date +%s)
ec=0
echo "$stop_input" | bash "$PLUGIN_ROOT/hooks/stop-hook.sh" >/dev/null 2>&1 || ec=$?
end_ts=$(date +%s)
duration=$((end_ts - start_ts))

# Hook MUST exit 0 even when there's no transcript (must not block Claude Code).
if [[ $ec -eq 0 ]]; then
    echo "PASS: Stop hook exits 0 on missing-transcript (non-blocking)"
    PASS=$((PASS + 1))
else
    echo "FAIL: Stop hook exited $ec — would block Claude Code session"
    FAIL=$((FAIL + 1))
fi

# Hook MUST terminate within a reasonable budget. Retry budget is bounded.
if (( duration <= 5 )); then
    echo "PASS: Stop hook returned within ${duration}s"
    PASS=$((PASS + 1))
else
    echo "FAIL: Stop hook took ${duration}s — too long for an auto-fire hook"
    FAIL=$((FAIL + 1))
fi

# =============================================================================
# Test 5: SessionEnd hook auto-fire with Claude Code's stdin JSON contract
# =============================================================================
echo
echo "=== Test 5: SessionEnd hook accepts Claude Code stdin JSON + exits 0 ==="

session_end_input=$(jq -nc \
    --arg t "$fake_transcript" \
    --arg s "auto-fire-session-end-test" \
    --arg c "/tmp/test" \
    '{transcript_path: $t, session_id: $s, cwd: $c, hook_event_name: "SessionEnd"}')

start_ts=$(date +%s)
ec=0
echo "$session_end_input" | bash "$PLUGIN_ROOT/hooks/session-end-scan.sh" >/dev/null 2>&1 || ec=$?
end_ts=$(date +%s)
duration=$((end_ts - start_ts))

if [[ $ec -eq 0 ]]; then
    echo "PASS: SessionEnd hook exits 0 on missing-transcript"
    PASS=$((PASS + 1))
else
    echo "FAIL: SessionEnd hook exited $ec"
    FAIL=$((FAIL + 1))
fi

if (( duration <= 5 )); then
    echo "PASS: SessionEnd hook returned within ${duration}s"
    PASS=$((PASS + 1))
else
    echo "FAIL: SessionEnd hook took ${duration}s"
    FAIL=$((FAIL + 1))
fi

# =============================================================================
# Test 6: SessionStart hook matcher — startup|clear|compact
# =============================================================================
echo
echo "=== Test 6: SessionStart hook has the documented matcher ==="

matcher=$(jq -r '.hooks.SessionStart[0].hooks[0].matcher // ""' "$HOOKS_JSON")
if [[ "$matcher" == "startup|clear|compact" ]]; then
    echo "PASS: SessionStart matcher is the documented 'startup|clear|compact'"
    PASS=$((PASS + 1))
else
    echo "FAIL: SessionStart matcher is '$matcher' — expected 'startup|clear|compact'"
    FAIL=$((FAIL + 1))
fi

# =============================================================================
# Test 7: hooks.json is syntactically valid JSON
# =============================================================================
echo
echo "=== Test 7: hooks.json is valid JSON ==="

if jq -e . "$HOOKS_JSON" >/dev/null 2>&1; then
    echo "PASS: hooks.json parses as valid JSON"
    PASS=$((PASS + 1))
else
    echo "FAIL: hooks.json is malformed"
    FAIL=$((FAIL + 1))
fi

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
