#!/usr/bin/env bash
# session-end-scan.sh — SessionEnd hook for observe-learning-capture.
#
# Backup scan: invoked once at session end. Scans the entire session
# transcript with Haiku (one big call). Catches anything the Stop-hook
# prefilter false-negatived during the session.
#
# Input contract from Claude Code:
#   Claude Code passes hook input via stdin as JSON:
#     { session_id, transcript_path, cwd, hook_event_name, ... }
#   Env vars are NOT set by Claude Code — supported only as fallback for
#   direct test/manual invocation.

set -uo pipefail

# Force subscription auth on the classifier subprocess (`claude -p`).
# See the matching comment in stop-hook.sh for the full rationale —
# short version: ANTHROPIC_API_KEY in env makes `claude -p` use the
# API key's tier-1 50K input-TPM rate limit, which trips on multi-call
# bursts; unsetting it forces keychain (MAX subscription) auth which
# has no per-minute ceiling.
unset ANTHROPIC_API_KEY

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
LOG_FILE="${HOME}/.claude/logs/observe-learning-capture.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [session-end-scan] $*" >> "$LOG_FILE"
}

# ---------------------------------------------------------------------------
# Read hook input — stdin JSON first, env-var fallback.
#
# Claude Code passes JSON to stdin: { session_id, transcript_path, cwd, ... }
# Env vars are a fallback for direct invocation during testing.
# ---------------------------------------------------------------------------
HOOK_INPUT=""
if [[ -t 0 ]]; then
    # No piped stdin — fall back directly to env vars (test/manual invocation).
    TRANSCRIPT="${CLAUDE_TRANSCRIPT_PATH:-}"
    SESSION_ID="${CLAUDE_SESSION_ID:-unknown}"
    PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
else
    HOOK_INPUT=$(cat)
    if [[ -n "$HOOK_INPUT" ]]; then
        TRANSCRIPT=$(printf '%s' "$HOOK_INPUT" | jq -r '.transcript_path // ""' 2>/dev/null)
        SESSION_ID=$(printf '%s' "$HOOK_INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null)
        PROJECT_DIR=$(printf '%s' "$HOOK_INPUT" | jq -r '.cwd // ""' 2>/dev/null)
    fi
    # Final fallback to env vars if JSON parse failed or fields are empty.
    TRANSCRIPT="${TRANSCRIPT:-${CLAUDE_TRANSCRIPT_PATH:-}}"
    SESSION_ID="${SESSION_ID:-${CLAUDE_SESSION_ID:-unknown}}"
    PROJECT_DIR="${PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-$PWD}}"
fi

if [[ -z "$TRANSCRIPT" || ! -f "$TRANSCRIPT" ]]; then
    log "no transcript — skip"
    exit 0
fi

log "running full-session scan (session=$SESSION_ID)"

# Synchronous (this is session end; user is leaving anyway).
cd "$PLUGIN_ROOT"
python3 -m pipeline.runner \
    --mode "session-end" \
    --transcript "$TRANSCRIPT" \
    --session-id "$SESSION_ID" \
    --cwd "$PROJECT_DIR" \
    2>>"$LOG_FILE" || log "runner failed (non-fatal)"

exit 0
