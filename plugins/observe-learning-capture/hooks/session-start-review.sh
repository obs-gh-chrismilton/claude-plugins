#!/usr/bin/env bash
# session-start-review.sh — SessionStart hook for observe-learning-capture.
#
# At every session start, if the pending file has any candidates, emit a
# review block on stdout. Claude Code injects stdout into the agent's
# context — Claude reads it and surfaces the candidates to the user
# on first prompt (per CLAUDE.md companion rule).
#
# Bug 4 fix: previous version used inline `python3 -c '...'` with
# sys.path.insert from `os.environ.get("PWD")` — fragile when PWD
# wasn't exported. Now calls the proper pipeline.render_pending module
# via `python3 -m`.
#
# Output: stdout. Logs to file. Always exit 0.
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

# Bug 4 fix: invoke the dedicated render module.
# - cd to plugin root so `python3 -m pipeline.render_pending` resolves the package.
# - PYTHONPATH belt-and-suspenders in case the cd is insufficient under unusual shells.
# - PENDING_FILE_OVERRIDE forwarded so tests can point the renderer at a fixture.
cd "$PLUGIN_ROOT" || { log "cd $PLUGIN_ROOT failed"; exit 0; }
PYTHONPATH="$PLUGIN_ROOT" PENDING_FILE_OVERRIDE="$PENDING_FILE" \
    python3 -m pipeline.render_pending 2>>"$LOG_FILE" \
    || log "render_pending invocation failed"

exit 0
