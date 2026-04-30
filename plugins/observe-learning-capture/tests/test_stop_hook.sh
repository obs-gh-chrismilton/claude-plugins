#!/usr/bin/env bash
# Tests for stop-hook.sh prefilter logic.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/../hooks/stop-hook.sh"
PASS=0
FAIL=0

assert_prefilter() {
    local description="$1"
    local expected="$2"  # "pass" or "fail"
    local transcript="$3"

    local exit_code=0
    PREFILTER_ONLY=1 \
    CLAUDE_TRANSCRIPT_PATH="$transcript" \
    CLAUDE_SESSION_ID="test" \
    CLAUDE_PROJECT_DIR="/tmp/test" \
    bash "$HOOK" >/dev/null 2>&1 || exit_code=$?

    if [[ "$expected" == "pass" && $exit_code -eq 0 ]]; then
        echo "PASS: $description"
        PASS=$((PASS + 1))
    elif [[ "$expected" == "fail" && $exit_code -eq 1 ]]; then
        echo "PASS: $description"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $description (expected=$expected, got=$exit_code)"
        FAIL=$((FAIL + 1))
    fi
}

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

cat > "$TMPDIR/trivial.jsonl" <<'EOF'
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Sure, I'll do that."}]},"uuid":"a1","timestamp":"2026-04-29T11:00:00Z"}
EOF
assert_prefilter "trivial ack rejected" "fail" "$TMPDIR/trivial.jsonl"

cat > "$TMPDIR/discovery.jsonl" <<'EOF'
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Turns out OPAL rejects '7d' as a duration literal — must use '168h' instead. Verified by trying both in a filter() expression. The error message says 'expected duration literal' which is misleading because '7d' looks like one. Same goes for '14d' which must be '336h'."}]},"uuid":"a2","timestamp":"2026-04-29T11:00:00Z"}
EOF
assert_prefilter "Observe discovery passes" "pass" "$TMPDIR/discovery.jsonl"

cat > "$TMPDIR/generic.jsonl" <<'EOF'
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I read the file and notice it has many patterns related to error handling. The structure is straightforward — a try/except wrapping the main logic. There's no special configuration needed. Let me know if you'd like me to refactor it."}]},"uuid":"a3","timestamp":"2026-04-29T11:00:00Z"}
EOF
assert_prefilter "generic prose rejected" "fail" "$TMPDIR/generic.jsonl"

echo -n "" > "$TMPDIR/empty.jsonl"
assert_prefilter "empty transcript rejected" "fail" "$TMPDIR/empty.jsonl"

# ---------------------------------------------------------------------------
# stdin JSON input tests
#
# Verify the hook reads transcript_path from stdin JSON (the actual production
# contract Claude Code uses) rather than from env vars. These tests pipe JSON
# to stdin and deliberately do NOT set CLAUDE_TRANSCRIPT_PATH, confirming
# the stdin path is the one exercised.
# ---------------------------------------------------------------------------
echo
echo "=== stdin JSON input tests ==="

run_with_stdin() {
    local transcript="$1"
    # Build JSON matching the Claude Code hook input contract.
    local input='{"transcript_path":"'"$transcript"'","session_id":"test-stdin","cwd":"/tmp/test","hook_event_name":"Stop"}'
    local exit_code=0
    echo "$input" | PREFILTER_ONLY=1 bash "$HOOK" >/dev/null 2>&1 || exit_code=$?
    echo "$exit_code"
}

# Trivial ack: stdin JSON → prefilter should fail (exit 1)
ec=$(run_with_stdin "$TMPDIR/trivial.jsonl")
if [[ "$ec" == "1" ]]; then
    echo "PASS: stdin JSON trivial ack rejected"
    PASS=$((PASS + 1))
else
    echo "FAIL: stdin JSON trivial ack expected=1 got=$ec"
    FAIL=$((FAIL + 1))
fi

# Observe discovery: stdin JSON → prefilter should pass (exit 0)
ec=$(run_with_stdin "$TMPDIR/discovery.jsonl")
if [[ "$ec" == "0" ]]; then
    echo "PASS: stdin JSON Observe discovery passes"
    PASS=$((PASS + 1))
else
    echo "FAIL: stdin JSON Observe discovery expected=0 got=$ec"
    FAIL=$((FAIL + 1))
fi

# Generic prose: stdin JSON → prefilter should fail (exit 1)
ec=$(run_with_stdin "$TMPDIR/generic.jsonl")
if [[ "$ec" == "1" ]]; then
    echo "PASS: stdin JSON generic prose rejected"
    PASS=$((PASS + 1))
else
    echo "FAIL: stdin JSON generic prose expected=1 got=$ec"
    FAIL=$((FAIL + 1))
fi

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
