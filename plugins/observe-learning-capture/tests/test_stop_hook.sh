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

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
