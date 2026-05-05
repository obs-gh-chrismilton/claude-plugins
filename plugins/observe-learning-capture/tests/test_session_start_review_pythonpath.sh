#!/usr/bin/env bash
# Bug 4 regression test: session-start-review.sh must produce either
# a valid render OR an explicit RENDER FAILED block — never silent.
#
# Tests with a deliberately stripped environment (env -i) to verify
# the hook works when launched outside an interactive shell context
# (which is how macOS GUI Claude Code launches it).

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/session-start-review.sh"

# Set up isolated tmp dir for pending file + override env var
TMPDIR_BASE="$(mktemp -d)"
trap "rm -rf $TMPDIR_BASE" EXIT
PENDING="$TMPDIR_BASE/pending.md"

# --- Test 1: empty pending file produces no output ---
> "$PENDING"
OUT=$(env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/local/bin" \
  PENDING_FILE_OVERRIDE="$PENDING" \
  bash "$HOOK" 2>&1)
if [[ -n "$OUT" ]]; then
    echo "FAIL test 1: empty pending should produce no output, got: $OUT"
    exit 1
fi
echo "PASS test 1: empty pending -> no output"

# --- Test 2: valid pending file produces review block ---
cat > "$PENDING" <<'YAML'
---
id: abcd1234
title: Test learning from session-start hook
fact: |
  This is a test fact for hook validation.
proposed_section: OPAL Gotchas
confidence: high
tags: [opal]
provenance:
  session_id: test-session
  cwd: /test/cwd
  captured_at: 2026-05-04T10:00:00+00:00
  excerpt: test excerpt
YAML
OUT=$(env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/local/bin" \
  PENDING_FILE_OVERRIDE="$PENDING" \
  bash "$HOOK" 2>&1)
if ! echo "$OUT" | grep -q "OBSERVE LEARNING CAPTURE"; then
    echo "FAIL test 2: valid pending should produce review block, got: $OUT"
    exit 1
fi
if ! echo "$OUT" | grep -q "Test learning from session-start hook"; then
    echo "FAIL test 2: valid pending should include title, got: $OUT"
    exit 1
fi
echo "PASS test 2: valid pending -> review block"

# --- Test 3: malformed pending produces RENDER FAILED block ---
# Use binary bytes that trigger UnicodeDecodeError in read_pending's
# read_text(encoding="utf-8"). The Step 4.1 plan-text fixture
# (`this is not valid yaml: [{{{{`) is actually parsed as a single
# scalar value by the hand-rolled YAML parser and does NOT raise —
# so we use genuinely-undecodable bytes to force the failure path.
printf '\xff\xfe\xfd\xfc\x00\x01\x02' > "$PENDING"
OUT=$(env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/local/bin" \
  PENDING_FILE_OVERRIDE="$PENDING" \
  bash "$HOOK" 2>&1)
if ! echo "$OUT" | grep -q "RENDER FAILED"; then
    echo "FAIL test 3: malformed pending should produce RENDER FAILED, got: $OUT"
    exit 1
fi
echo "PASS test 3: malformed pending -> RENDER FAILED block"

echo "All hook integration tests passed."
exit 0
