#!/usr/bin/env bash
# stop-hook.sh — Stop hook for observe-learning-capture plugin.
#
# Triggered: every Claude-turn end. Reads the most recent assistant turn
# from the session JSONL transcript, runs a cheap prefilter, and only
# invokes the Python classifier if the prefilter passes.
#
# Always exits 0 (hooks must not block session flow). Errors are logged.
#
# Env from Claude Code:
#   $CLAUDE_TRANSCRIPT_PATH  — path to session JSONL
#   $CLAUDE_SESSION_ID       — session UUID
#   $CLAUDE_PROJECT_DIR      — current project dir (cwd)
#
# Debug env:
#   PREFILTER_ONLY=1 — exit 0 if prefilter would pass, 1 otherwise.
#   (Used by tests/test_stop_hook.sh to exercise the gate without
#   actually spawning the Python classifier.)

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
LOG_FILE="${HOME}/.claude/logs/observe-learning-capture.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [stop-hook] $*" >> "$LOG_FILE"
}

# ---------------------------------------------------------------------------
# Read transcript path from env
# ---------------------------------------------------------------------------
TRANSCRIPT="${CLAUDE_TRANSCRIPT_PATH:-}"
if [[ -z "$TRANSCRIPT" || ! -f "$TRANSCRIPT" ]]; then
    log "no transcript at \$CLAUDE_TRANSCRIPT_PATH=$TRANSCRIPT — skip"
    # PREFILTER_ONLY mode: no transcript → prefilter fails (exit 1)
    [[ "${PREFILTER_ONLY:-0}" == "1" ]] && exit 1
    exit 0
fi

# ---------------------------------------------------------------------------
# Extract last assistant turn text via jq.
#
# jq logic:
#   - Slurp all lines into an array.
#   - Select only records with .type == "assistant".
#   - For each, extract content:
#       - If .message.content is a string → use it directly.
#       - If it's an array → join all text blocks.
#   - Take the last element of that array ([-1]).
#   - If no assistant turn found, default to empty string.
# ---------------------------------------------------------------------------
TURN_TEXT=$(jq -rsc '
    [.[]
     | select(.type == "assistant")
     | .message.content
     | (if type == "string" then .
        else map(select(.type == "text") | .text) | join("\n")
        end)
    ][-1] // ""
' "$TRANSCRIPT" 2>/dev/null) || TURN_TEXT=""

if [[ -z "$TURN_TEXT" ]]; then
    # No assistant turn in this transcript yet — nothing to classify.
    [[ "${PREFILTER_ONLY:-0}" == "1" ]] && exit 1
    log "no assistant turn extracted — skip"
    exit 0
fi

# ---------------------------------------------------------------------------
# Gate 1: minimum character length.
#
# WHY: Very short turns (acknowledgements, one-liners) cannot contain the
# detail needed for a useful learning entry. The threshold is 150 chars —
# long enough to reject "Sure, I'll do that." but not so long that short
# explanatory turns are silently dropped.
# ---------------------------------------------------------------------------
TURN_LEN=${#TURN_TEXT}
if (( TURN_LEN < 150 )); then
    [[ "${PREFILTER_ONLY:-0}" == "1" ]] && exit 1
    exit 0
fi

# ---------------------------------------------------------------------------
# Gate 2: Observe vocabulary check.
#
# At least one Observe-domain term must appear in the text (case-insensitive).
# This prevents generic Python/bash discussions from reaching the classifier.
#
# nocasematch is scoped to this check and unset immediately after.
# ---------------------------------------------------------------------------
shopt -s nocasematch
VOCAB_HIT=0
for term in "OPAL" "Observe" "dataset" "datastream" "monitor" "worksheet" \
            "dashboard" "accelerat" "bookmark" "transform" "filedrop" \
            "poller" "pick_col" "make_col" "statsby" "timechart" \
            "deleteDataset" "deleteMonitor" "/v1/meta" "GraphQL" "observeinc"; do
    if [[ "$TURN_TEXT" == *"$term"* ]]; then
        VOCAB_HIT=1
        break
    fi
done

if (( VOCAB_HIT == 0 )); then
    shopt -u nocasematch
    [[ "${PREFILTER_ONLY:-0}" == "1" ]] && exit 1
    exit 0
fi

# ---------------------------------------------------------------------------
# Gate 3: Discovery verb / pattern check (AND-gate with vocab above).
#
# The turn must show a discovery — not just mention Observe. Generic
# "I read the Observe docs" passes Gate 2 but must fail Gate 3.
#
# IMPORTANT: 'requires' and 'signature' were explicitly REMOVED from this
# list (T01 review) — they are too generic and defeat the AND-gate semantics.
# E.g. "Observe requires auth" passes vocab+requires without any discovery.
#
# HTTP 4xx/5xx responses and GraphQL mutation patterns (delete*() calls) are
# additional signals for boundary/error discoveries.
# ---------------------------------------------------------------------------
DISCOVERY_HIT=0
for phrase in "turns out" "actually" "discovered" "it errors" "must be" \
              "won't accept" "cascade" "surprisingly" "rejected" \
              "deadlock" "doesn't cascade" "managed by" "non-blocking"; do
    if [[ "$TURN_TEXT" == *"$phrase"* ]]; then
        DISCOVERY_HIT=1
        break
    fi
done

# Additional regex-based discovery signals (only checked if phrase scan missed):
if (( DISCOVERY_HIT == 0 )); then
    # HTTP 4xx/5xx error code mention — e.g. "HTTP 403" or "HTTP 500"
    if [[ "$TURN_TEXT" =~ HTTP[[:space:]]*[45][0-9][0-9] ]]; then
        DISCOVERY_HIT=1
    # GraphQL mutation pattern — e.g. deleteDataset( or deleteMonitor(
    elif [[ "$TURN_TEXT" =~ delete[A-Z][a-zA-Z]*\( ]]; then
        DISCOVERY_HIT=1
    fi
fi

shopt -u nocasematch

if (( DISCOVERY_HIT == 0 )); then
    [[ "${PREFILTER_ONLY:-0}" == "1" ]] && exit 1
    exit 0
fi

# ---------------------------------------------------------------------------
# Prefilter passed. All three gates cleared.
# ---------------------------------------------------------------------------

# In test/debug mode: just report the pass/fail, don't spawn the classifier.
[[ "${PREFILTER_ONLY:-0}" == "1" ]] && exit 0

# ---------------------------------------------------------------------------
# Invoke the Python pipeline in the background.
#
# WHY background (&): the Stop hook must not delay the user's session
# end. The classifier calls the `claude` CLI (up to 60s timeout) — that
# wait must be invisible to the user.
#
# WHY cd "$PLUGIN_ROOT": python3 -m pipeline.runner resolves the module
# relative to cwd. Without this, the module won't be found unless the
# plugin root is already on PYTHONPATH.
#
# Errors from the background process are appended to $LOG_FILE for review.
# Per spec §9: always exit 0 from the hook — never block the session.
# ---------------------------------------------------------------------------
log "prefilter passed — invoking classifier (session=${CLAUDE_SESSION_ID:-unknown})"
(
    cd "$PLUGIN_ROOT"
    python3 -m pipeline.runner \
        --mode "stop" \
        --transcript "$TRANSCRIPT" \
        --session-id "${CLAUDE_SESSION_ID:-unknown}" \
        --cwd "${CLAUDE_PROJECT_DIR:-$PWD}" \
        2>>"$LOG_FILE"
) &

# Always exit 0 — hooks must not block session flow.
exit 0
