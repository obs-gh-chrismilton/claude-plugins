# observe-learning-capture

Auto-captures Observe-platform-general learnings from Claude Code session
transcripts, stages them in `~/.claude/agents/.observeie-pending.md`, surfaces
them at next session start for review, and merges approved candidates into
`~/.claude/agents/ObserveIE.md`.

## How it works

1. Stop hook fires after every Claude turn → cheap shell prefilter → if pass,
   Haiku classifier proposes candidates → dedupe → stage to pending file.
2. SessionEnd hook does a full-transcript backup scan in case the prefilter
   missed something.
3. SessionStart hook reads pending file → emits system-reminder context →
   Claude surfaces candidates conversationally on first user prompt.
4. User replies `merge all` / `merge N` / `discard N` / `edit N` / `defer`.

See `docs/design.md` for the full design.

## Cost

~$0.10–$0.15 per session in Haiku tokens. ~$15–25/month at typical usage.

## Configuration

Edit `config.json` to override defaults: destination file paths, model,
prefilter rules, debug flag.

**Path expansion:** `~` in path values is expanded at runtime by consumers
via `os.path.expanduser()` (Python) or `eval echo` (shell). Use `~/...` —
do NOT pre-expand to absolute paths.

## Manual triggers

- `/observe-review` — review pending queue mid-session
- `/observe-capture` — force-capture last turn (bypass prefilter)
