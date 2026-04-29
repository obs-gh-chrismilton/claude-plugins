#!/usr/bin/env bash
# session-end-scan.sh — SessionEnd hook for observe-learning-capture.
#
# Backup scan: invoked once at session end. Scans the entire session
# transcript with Haiku (one big call). Catches anything the Stop-hook
# prefilter false-negatived during the session.
#
# Env from Claude Code (same as stop-hook):
#   $CLAUDE_TRANSCRIPT_PATH, $CLAUDE_SESSION_ID, $CLAUDE_PROJECT_DIR

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
LOG_FILE="${HOME}/.claude/logs/observe-learning-capture.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [session-end-scan] $*" >> "$LOG_FILE"
}

TRANSCRIPT="${CLAUDE_TRANSCRIPT_PATH:-}"
if [[ -z "$TRANSCRIPT" || ! -f "$TRANSCRIPT" ]]; then
    log "no transcript — skip"
    exit 0
fi

log "running full-session scan (session=${CLAUDE_SESSION_ID:-unknown})"

# Synchronous (this is session end; user is leaving anyway).
cd "$PLUGIN_ROOT"
python3 -m pipeline.runner \
    --mode "session-end" \
    --transcript "$TRANSCRIPT" \
    --session-id "${CLAUDE_SESSION_ID:-unknown}" \
    --cwd "${CLAUDE_PROJECT_DIR:-$PWD}" \
    2>>"$LOG_FILE" || log "runner failed (non-fatal)"

exit 0
