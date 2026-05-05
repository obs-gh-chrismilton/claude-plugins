#!/usr/bin/env bash
# stop-hook.sh — Stop hook for observe-learning-capture plugin.
#
# Triggered: every Claude-turn end. Reads the most recent assistant turn
# from the session JSONL transcript, runs a cheap prefilter, and only
# invokes the Python classifier if the prefilter passes.
#
# Always exits 0 (hooks must not block session flow). Errors are logged.
#
# Input contract from Claude Code:
#   Claude Code passes hook input via stdin as JSON:
#     { session_id, transcript_path, cwd, hook_event_name, ... }
#   Env vars ($CLAUDE_TRANSCRIPT_PATH etc.) are NOT set by Claude Code —
#   they are supported here only as a fallback for direct test invocation.
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
# Read hook input — stdin JSON first, env-var fallback.
#
# Claude Code passes JSON to stdin: { session_id, transcript_path, cwd, ... }
# Env vars are a fallback for direct invocation during testing.
#
# Detection: if stdin is a terminal (fd 0 is a tty), we're running interactively
# or in an env-var-only test harness — skip stdin read and use env vars.
# When Claude Code fires the hook, stdin is always a pipe (non-tty), so
# the jq parse path is the one that runs in production.
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
    log "no transcript at transcript_path=$TRANSCRIPT — skip"
    # PREFILTER_ONLY mode: no transcript → prefilter fails (exit 1)
    [[ "${PREFILTER_ONLY:-0}" == "1" ]] && exit 1
    exit 0
fi

# ---------------------------------------------------------------------------
# Extract the CURRENT LOGICAL TURN text via jq (mirrors Python's
# pipeline.transcript.current_logical_turn — Bug 1d fix).
#
# WHY a walk-back, not just "last assistant record":
#   Modern Claude Code emits ~6 JSONL records per logical turn (interleaved
#   text / thinking / tool_use). The LAST assistant record is often a
#   tool_use chunk (no text after the text-block filter) or a 7-char ack
#   like "Saved." that fails Gate 1 (150-char minimum). Picking only the
#   last record causes the prefilter to false-reject substantive turns.
#
# Algorithm (mirrors current_logical_turn in pipeline/transcript.py):
#   1. Slurp all records into an array, then walk them in REVERSE.
#   2. For each assistant record, extract the joined text from its
#      .message.content (string-form OR array-of-blocks form), and
#      collect into a list.
#   3. Stop when we hit a real user prompt — defined as type == "user"
#      AND content is a STRING (not a list — list content == tool_result)
#      AND the string does NOT start with one of the injection prefixes
#      (slash commands, hook injections, bash IO captures, ide_selection,
#      this plugin's own SessionStart marker).
#   4. Re-reverse the collected texts so they read chronologically and
#      join with newlines. This is the "current logical turn" text.
#
# The injection-prefix list MUST stay in sync with _USER_INJECTION_PREFIXES
# in pipeline/transcript.py — drift here causes prefilter/classifier
# disagreement on where one logical turn ends and the next begins.
# ---------------------------------------------------------------------------
TURN_TEXT=$(jq -rsc '
    # Helper: extract joined text from an assistant message .content,
    # handling both string-form and array-of-blocks form.
    def assistant_text(content):
        if (content | type) == "string" then content
        else content
             | map(select(.type == "text") | .text)
             | join("\n")
        end;

    # Helper: is this record a real user-typed prompt (the boundary that
    # ends the current logical turn walk-back)?
    def is_real_user_prompt(rec):
        rec.type == "user"
        and (rec.message.content | type) == "string"
        and (rec.message.content
             | ltrimstr(" ") | ltrimstr("\t") | ltrimstr("\n")
             | test("^(/clear|/compact|/init|/cost|/help|/memory|<command-name>|<system-reminder>|<local-command-stdout>|<bash-input>|<bash-stdout>|<bash-stderr>|<ide_selection>|=== OBSERVE)")
             | not);

    # Walk records in reverse, accumulating assistant text until we hit
    # a real user prompt. Reduce produces a state object:
    #   { stopped: bool, texts: [collected texts in reverse order] }
    [.[]] | reverse
    | reduce .[] as $rec (
        {stopped: false, texts: []};
        if .stopped then .
        elif $rec.type == "assistant" then
            (assistant_text($rec.message.content)) as $t
            | if ($t | length) > 0
              then .texts += [$t]
              else .
              end
        elif is_real_user_prompt($rec) then
            .stopped = true
        else
            .   # tool_result / slash command / hook injection — skip past
        end
    )
    | .texts | reverse | join("\n")
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
for phrase in "turns out" "discovered" "it errors" "won't accept" \
              "surprisingly" "rejected" "deadlock" "doesn't cascade"; do
    if [[ "$TURN_TEXT" == *"$phrase"* ]]; then
        DISCOVERY_HIT=1
        break
    fi
done

shopt -u nocasematch  # Must precede regex block: [A-Z] requires uppercase under normal matching

# Additional regex-based discovery signals (only checked if phrase scan missed):
# WHY after shopt -u: [A-Z] would match lowercase under nocasematch, so
# delete[A-Z][a-zA-Z]*\( would spuriously match 'deletedataset(' — moving
# shopt -u here ensures CamelCase mutations like deleteDataset( are required.
if (( DISCOVERY_HIT == 0 )); then
    # HTTP 4xx/5xx error code mention — e.g. "HTTP 403" or "HTTP 500"
    if [[ "$TURN_TEXT" =~ HTTP[[:space:]]*[45][0-9][0-9] ]]; then
        DISCOVERY_HIT=1
    # GraphQL mutation pattern — e.g. deleteDataset( or deleteMonitor(
    elif [[ "$TURN_TEXT" =~ delete[A-Z][a-zA-Z]*\( ]]; then
        DISCOVERY_HIT=1
    fi
fi

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
log "prefilter passed — invoking classifier (session=$SESSION_ID)"
(
    cd "$PLUGIN_ROOT"
    python3 -m pipeline.runner \
        --mode "stop" \
        --transcript "$TRANSCRIPT" \
        --session-id "$SESSION_ID" \
        --cwd "$PROJECT_DIR" \
        2>>"$LOG_FILE"
) &

# Always exit 0 — hooks must not block session flow.
exit 0
