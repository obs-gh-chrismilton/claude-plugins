#!/usr/bin/env bash
# tests/test_hook_missing_file_race.sh
#
# 2026-05-14 regression test for the "transcript file does not yet exist at
# Stop-hook fire time" race that has been silently dropping ~92 captures in
# the user's recent session history (per ~/.claude/logs/observe-learning-capture.log
# analysis).
#
# Scenario:
#   Claude Code emits the Stop hook at assistant-turn end. In some plugin-
#   development workflows (and in certain timing-edge scenarios), the JSONL
#   transcript file is created slightly AFTER the Stop event reaches the hook.
#   The current stop-hook.sh checks `[[ ! -f "$TRANSCRIPT" ]]` BEFORE the retry
#   loop, so the hook bails immediately with "no transcript at transcript_path
#   = ... -- skip" and the classifier never runs.
#
# Existing test_stop_hook.sh covers the "file exists but content not flushed
# yet" race via test on $TMPDIR/race.jsonl. This file covers the orthogonal
# "file does not exist YET" race -- different code path, different fix surface.
#
# Expected behavior AFTER the fix:
#   The hook treats missing-file the same as empty-file: sleep $RETRY_DELAY,
#   retry; up to $MAX_RETRIES times. If the file appears with substantive
#   content within the retry window, prefilter passes (exit 0). If the file
#   never appears, the hook still exits 0 (or 1 under PREFILTER_ONLY) gracefully
#   -- never hangs, never blocks the session.
#
# Two cases:
#   Case A -- file appears late: confirm retry recovers it.
#   Case B -- file never appears: confirm graceful skip (no hang, no crash).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/../hooks/stop-hook.sh"
PASS=0
FAIL=0

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "=== Case A: transcript file appears AFTER hook fires (race recovery) ==="

# Path that does NOT exist at hook fire time. Background writer will create it
# with substantive Observe-domain content after a short delay.
late_transcript="$TMPDIR/late.jsonl"

# Sanity-check: the file truly does not exist at this point.
if [[ -e "$late_transcript" ]]; then
    echo "FAIL: precondition violation -- $late_transcript should not exist yet"
    exit 1
fi

# Background writer: after 300ms, create the transcript with substantive
# content that will pass all three prefilter gates (length >= 150, Observe
# vocab present, discovery verb present).
#
# WHY 300ms: the hook's first existence check at t=0 must see the file
# missing; retry budget is MAX_RETRIES * RETRY_DELAY = 3 * 200ms = 600ms,
# so the writer's content lands comfortably inside the retry window.
(
    sleep 0.3
    cat > "$late_transcript" <<'JSONL_EOF'
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Turns out OPAL rejects '7d' as a duration literal -- must use '168h' instead. Verified by trying both in a filter() expression. The error message says 'expected duration literal' which is misleading because '7d' looks like one. Same goes for '14d' which must be '336h'."}]},"uuid":"late1","timestamp":"2026-05-14T12:00:00Z"}
JSONL_EOF
) &
writer_pid=$!

# Hook fires at t=0; transcript does not exist yet.
case_a_exit=0
PREFILTER_ONLY=1 \
CLAUDE_TRANSCRIPT_PATH="$late_transcript" \
CLAUDE_SESSION_ID="missing-file-race-A" \
CLAUDE_PROJECT_DIR="/tmp/test" \
bash "$HOOK" >/dev/null 2>&1 || case_a_exit=$?

# Reap the background writer.
wait "$writer_pid" 2>/dev/null || true

if [[ $case_a_exit -eq 0 ]]; then
    echo "PASS: hook recovered after transcript file appeared during retry window"
    PASS=$((PASS + 1))
else
    echo "FAIL: hook did NOT recover (expected=0 got=$case_a_exit) -- retry loop"
    echo "       must cover missing-file case, not just empty-file case."
    FAIL=$((FAIL + 1))
fi

echo
echo "=== Case B: transcript file NEVER appears (graceful skip) ==="

# Path that will never exist throughout the test.
absent_transcript="$TMPDIR/absent.jsonl"

# Sanity-check: still does not exist.
if [[ -e "$absent_transcript" ]]; then
    echo "FAIL: precondition violation -- $absent_transcript should not exist"
    exit 1
fi

# Hook fires; nothing will ever create the file.
# Behavior contract: the hook must terminate within roughly
# (MAX_RETRIES + 1) * RETRY_DELAY plus jq overhead -- bounded.
# Under PREFILTER_ONLY=1 this should exit 1 (skipped). Under normal mode
# it would exit 0 (fire-and-forget; no work done) -- we use PREFILTER_ONLY
# here so we get a deterministic non-zero on skip.
start_ts=$(date +%s)
case_b_exit=0
PREFILTER_ONLY=1 \
CLAUDE_TRANSCRIPT_PATH="$absent_transcript" \
CLAUDE_SESSION_ID="missing-file-race-B" \
CLAUDE_PROJECT_DIR="/tmp/test" \
bash "$HOOK" >/dev/null 2>&1 || case_b_exit=$?
end_ts=$(date +%s)
duration=$((end_ts - start_ts))

# Must exit 1 (PREFILTER_ONLY semantics: "no content / no transcript -> fail").
if [[ $case_b_exit -eq 1 ]]; then
    echo "PASS: hook gracefully skipped absent transcript (exit 1 under PREFILTER_ONLY)"
    PASS=$((PASS + 1))
else
    echo "FAIL: hook returned $case_b_exit when transcript never existed (expected 1)"
    FAIL=$((FAIL + 1))
fi

# Hard bound: must terminate within 5 seconds. This guards against any retry
# loop becoming an infinite waiter.
if (( duration < 5 )); then
    echo "PASS: hook terminated within ${duration}s on absent transcript"
    PASS=$((PASS + 1))
else
    echo "FAIL: hook ran ${duration}s on absent transcript -- bounded retry budget"
    echo "       seems to be missing or too generous."
    FAIL=$((FAIL + 1))
fi

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
