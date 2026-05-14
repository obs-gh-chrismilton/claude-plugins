#!/usr/bin/env bash
# tests/test_hook_per_turn.sh
#
# 2026-05-14: verifies the user-expressed invariant that the Stop hook
# triggers on the END OF EACH ASSISTANT TURN and that, on each firing, the
# logical-turn extractor returns ONLY the latest turn's text -- not the
# full session's worth of accumulated assistant prose.
#
# Why this matters:
#   The user's intuition was that "the capture skill only captures the last
#   agent turn, so we need the skill to trigger at the end of each turn
#   automatically." The Stop event in hooks.json is wired exactly for that
#   per-turn cadence; the missing safety net was a regression test proving
#   that the turn-boundary detection in extract_turn_text() actually returns
#   per-turn slices.
#
# Mechanism:
#   We grow a transcript file three times, simulating three successive
#   assistant turns separated by real user prompts. Each turn has DIFFERENT
#   prefilter verdicts when evaluated in isolation:
#
#       Turn 1 ALONE:    PASSES prefilter (substantial Observe discovery)
#       Turn 2 ALONE:    FAILS  prefilter (trivial ack, < 150 chars)
#       Turn 3 ALONE:    FAILS  prefilter (generic prose, no Observe vocab)
#
#   If extract_turn_text() were incorrectly concatenating ALL assistant text
#   in the file, turns 2 and 3 would inherit turn 1's substantive content and
#   pass prefilter every time -- which is what a session-end-scan does, NOT
#   what the per-turn Stop hook should do. By asserting pass/fail/fail at
#   the three snapshots, we prove the boundary detection works.
#
# Note on user-record requirements:
#   stop-hook.sh's jq walks records in reverse until it hits a real user
#   prompt -- defined as type == "user" AND content is a string AND content
#   does NOT start with an injection prefix (slash commands, hook injections,
#   tool_results, etc.). We must emit user records with PLAIN-STRING content
#   to terminate the walk at the expected turn boundary.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/../hooks/stop-hook.sh"
PASS=0
FAIL=0

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

transcript="$TMPDIR/per_turn.jsonl"

# -----------------------------------------------------------------------------
# Helper: run hook in PREFILTER_ONLY mode, return exit code (0 = pass).
# -----------------------------------------------------------------------------
run_hook() {
    local ec=0
    PREFILTER_ONLY=1 \
    CLAUDE_TRANSCRIPT_PATH="$transcript" \
    CLAUDE_SESSION_ID="per-turn-test" \
    CLAUDE_PROJECT_DIR="/tmp/test" \
    bash "$HOOK" >/dev/null 2>&1 || ec=$?
    echo "$ec"
}

assert_verdict() {
    local description="$1"
    local expected="$2"   # "pass" or "fail"
    local actual_code; actual_code=$(run_hook)

    local actual="?"
    [[ "$actual_code" == "0" ]] && actual="pass"
    [[ "$actual_code" == "1" ]] && actual="fail"

    if [[ "$actual" == "$expected" ]]; then
        echo "PASS: $description (got $actual)"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $description (expected $expected, got $actual / exit=$actual_code)"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== Per-turn boundary detection ==="

# -----------------------------------------------------------------------------
# Snapshot 1: assistant turn 1 only -- substantive Observe discovery.
# Expected: prefilter PASSES because turn 1 alone is substantive.
# -----------------------------------------------------------------------------
cat > "$transcript" <<'JSONL'
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Turns out OPAL rejects '7d' as a duration literal -- must use '168h' instead. Verified by trying both in a filter() expression. The error message says 'expected duration literal' which is misleading because '7d' looks like one. Same goes for '14d' which must be '336h'."}]},"uuid":"a1","timestamp":"2026-05-14T12:00:00Z"}
JSONL
assert_verdict "Snapshot 1: assistant turn 1 alone passes prefilter" "pass"

# -----------------------------------------------------------------------------
# Snapshot 2: appended user1 prompt + assistant turn 2 (trivial ack).
#
# The turn-boundary detector walks backward from end. It should stop at the
# user1 prompt and return ONLY assistant 2's text -- a 32-char ack that fails
# Gate 1 (min 150 chars). If the detector were incorrectly aggregating across
# the user prompt, it would also include assistant 1's substantive text and
# the prefilter would pass.
# -----------------------------------------------------------------------------
cat >> "$transcript" <<'JSONL'
{"type":"user","message":{"role":"user","content":"Could you also check the monitor delete cascade?"},"uuid":"u1","timestamp":"2026-05-14T12:01:00Z"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Sure, I'll do that."}]},"uuid":"a2","timestamp":"2026-05-14T12:02:00Z"}
JSONL
assert_verdict "Snapshot 2: trivial ack turn 2 alone fails prefilter (boundary held)" "fail"

# -----------------------------------------------------------------------------
# Snapshot 3: appended user2 prompt + assistant turn 3 (generic prose, no
# Observe vocab). Should still fail prefilter on turn 3 alone -- proving
# the walker stops at the most recent user prompt and doesn't drag prior
# substantive turn-1 content forward.
# -----------------------------------------------------------------------------
cat >> "$transcript" <<'JSONL'
{"type":"user","message":{"role":"user","content":"Now write me a story about kittens."},"uuid":"u2","timestamp":"2026-05-14T12:03:00Z"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Once upon a time there were many kittens who lived in a meadow. They played all day long and never tired. The flowers around them swayed in the breeze, and butterflies danced. It was a perfectly idyllic scene, the kind of scene a child might paint in pastel watercolors."}]},"uuid":"a3","timestamp":"2026-05-14T12:04:00Z"}
JSONL
assert_verdict "Snapshot 3: generic prose turn 3 alone fails prefilter (no Observe vocab)" "fail"

# -----------------------------------------------------------------------------
# Sanity check: the Stop hook is registered for the "Stop" event in hooks.json
# (i.e. fires once per assistant turn end -- not just at session end).
# We assert the hooks.json file actually wires Stop, so a refactor that
# inadvertently demotes the hook to SessionEnd-only would fail this test.
# -----------------------------------------------------------------------------
echo
echo "=== Hook registration check ==="
hooks_json="$HERE/../hooks/hooks.json"
if [[ ! -f "$hooks_json" ]]; then
    echo "FAIL: hooks.json missing at $hooks_json"
    FAIL=$((FAIL + 1))
elif jq -e '.hooks.Stop[0].hooks[0].command' "$hooks_json" >/dev/null 2>&1; then
    echo "PASS: hooks.json registers a Stop-event handler"
    PASS=$((PASS + 1))
else
    echo "FAIL: hooks.json does not register a Stop-event handler -- per-turn"
    echo "       cadence relies on this registration."
    FAIL=$((FAIL + 1))
fi

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
