#!/usr/bin/env bash
# session-start-review.sh — SessionStart hook for observe-learning-capture.
#
# At every session start, if the pending file has any candidates, emit a
# system-reminder block on stdout. Claude Code will inject this into the
# agent's context — Claude reads it and surfaces the candidates to the
# user on first prompt (per CLAUDE.md companion rule from T15).
#
# Output goes to stdout; logs to file. Always exit 0.
#
# Debug env:
#   PENDING_FILE_OVERRIDE — override default pending file path (for tests).

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
LOG_FILE="${HOME}/.claude/logs/observe-learning-capture.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [session-start-review] $*" >> "$LOG_FILE"
}

PENDING_FILE="${PENDING_FILE_OVERRIDE:-${HOME}/.claude/agents/.observeie-pending.md}"

if [[ ! -f "$PENDING_FILE" ]]; then
    exit 0  # nothing pending — first run / clean state
fi

if [[ ! -s "$PENDING_FILE" ]]; then
    exit 0  # empty file
fi

log "pending file present — emitting review context"

# Render compact summary by parsing the YAML pending file via Python.
cd "$PLUGIN_ROOT"
PENDING_FILE="$PENDING_FILE" python3 -c '
import os, sys
from pathlib import Path
plugin_root = os.environ.get("PWD", ".")
sys.path.insert(0, plugin_root)
from pipeline.stage import read_pending

pending_path = Path(os.environ["PENDING_FILE"])
records = read_pending(pending_path)
if not records:
    sys.exit(0)

print("=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ===")
print(f"{len(records)} candidate(s) pending review from prior sessions:")
print()
for i, r in enumerate(records, 1):
    conf = r.get("confidence", "?")
    section = r.get("proposed_section", "?")
    title = r.get("title", "(no title)")
    src = r.get("source", {})
    cwd = src.get("cwd", "?")
    cwd_short = cwd.replace(os.path.expanduser("~"), "~")
    captured_at = src.get("captured_at", "?")[:10]
    print(f"  #{i} [{conf:6}] {section}: {title}")
    print(f"       (from {cwd_short}, {captured_at})")
print()
print("I should surface these candidates to the user before responding to")
print("their first prompt. The user may reply: merge all / merge N /")
print("discard N / edit N / defer.")
print("=== END OBSERVE LEARNING CAPTURE ===")
' 2>>"$LOG_FILE" || log "review render failed"

exit 0
