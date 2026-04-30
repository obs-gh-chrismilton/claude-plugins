# Observe Learning Capture — Claude Code Plugin

A Claude Code plugin that auto-captures Observe-platform-general learnings from conversation transcripts as they happen, stages them for human review, and merges approved candidates into `~/.claude/agents/ObserveIE.md` — so that knowledge discovered while working on one Observe customer benefits every future session in every customer.

## The Problem

Observe Integration Engineers work across multiple customer tenants daily. Each session may surface non-trivial Observe-platform knowledge — undocumented API behavior, OPAL syntax quirks, mutation signatures, cascade rules, error patterns — that would benefit *any* future IE session, *any* customer, dispatched by *any* agent.

Today this knowledge is captured ad-hoc:
- Sometimes the assistant updates `ObserveIE.md` at the end of a session (manual discipline, easily forgotten)
- Sometimes findings are buried in `/compact`-ed conversation transcripts
- Cross-customer propagation is nonexistent: a learning from a Vena session is invisible to a Tekion session

The result: IEs and the agents that assist them re-discover the same gotchas repeatedly. Knowledge does not compound.

## What It Does

Three Claude Code hooks plus a Python pipeline. Together they:

1. **Watch every Claude turn** for Observe-platform learnings via a cheap shell prefilter
2. **Classify** prefilter passes via Haiku, restricted by a prompt that explicitly rejects customer-specific facts
3. **Dedupe** candidates against ObserveIE.md *and* the existing pending queue (no same-session repeats)
4. **Stage** novel candidates in `~/.claude/agents/.observeie-pending.md` (YAML, append-only, flock-protected)
5. **Surface** pending candidates at next session start via a system-reminder block — Claude shows you the table, you reply `merge all` / `merge 1` / `discard 2` / `edit 1` / `defer`
6. **Merge** approved candidates into `~/.claude/agents/ObserveIE.md` with audit-log entries

The human is always the gatekeeper. The plugin proposes; the human disposes. False positives don't pollute global knowledge silently.

## How It Works

```
┌───────────────────────────────────────────────────────────────┐
│  Capture phase — during a Claude Code session                 │
│                                                               │
│  Claude finishes a turn                                       │
│         │                                                     │
│         ▼                                                     │
│  ┌─────────────────┐  Stop hook                               │
│  │ stop-hook.sh    │  vocab gate + discovery-verb gate        │
│  └─────────────────┘  (sub-100ms shell prefilter)             │
│         │ pass                                                │
│         ▼                                                     │
│  ┌─────────────────┐  classifier.py                           │
│  │  Haiku CLI      │  with ObserveIE.md as "already known"    │
│  └─────────────────┘  → 0+ candidates                         │
│         │                                                     │
│         ▼                                                     │
│  ┌─────────────────┐  dedupe.py                               │
│  │   hash check    │  vs. ObserveIE.md AND pending file       │
│  └─────────────────┘                                          │
│         │ novel                                               │
│         ▼                                                     │
│  ┌─────────────────┐  stage.py                                │
│  │ append YAML to  │  ~/.claude/agents/.observeie-pending.md  │
│  │ pending file    │  (flock + 2s timeout + PID fallback)     │
│  └─────────────────┘                                          │
│                                                               │
│  Backup: SessionEnd hook does a full-transcript scan          │
│  in case the prefilter false-negatived something              │
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│  Review phase — next session start (any cwd, any customer)    │
│                                                               │
│  ┌─────────────────────┐  SessionStart hook                   │
│  │ session-start-      │  reads pending YAML, emits           │
│  │ review.sh           │  system-reminder block on stdout     │
│  └─────────────────────┘                                      │
│         │                                                     │
│         ▼                                                     │
│  Claude (per CLAUDE.md Rule B) surfaces candidates BEFORE     │
│  responding to the user's first prompt:                       │
│                                                               │
│   📋 2 learning candidates pending review:                    │
│     #1 [high]  OPAL Gotchas: '7d' rejected, use '168h'        │
│     #2 [med]   Object Management: cascade-ordering deadlock   │
│                                                               │
│   Reply: merge all / merge 1, 3 / discard 2 / edit 1 / defer  │
│         │                                                     │
│         ▼                                                     │
│  User: "merge all"                                            │
│         │                                                     │
│         ▼                                                     │
│  ┌─────────────────┐  merge_cli.py                            │
│  │ merge_candidate │  → ~/.claude/agents/ObserveIE.md         │
│  │   per record    │  with <!-- id:XXX captured:DATE --> tag  │
│  │ remove_pending  │  + MERGE entry to plugin log             │
│  └─────────────────┘                                          │
└───────────────────────────────────────────────────────────────┘
```

All future ObserveIE subagent dispatches in any customer dir now see the new knowledge.

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview) (the CLI)
- Python 3.11+ (stdlib only — no pip installs required)
- `jq` (transcript JSONL parsing in shell hooks)
- `claude` CLI on PATH (the same CLI that powers Claude Code — used for Haiku invocations)

The plugin is macOS-only at present (uses POSIX `fcntl.flock`). Linux works too in principle. Windows is unsupported.

## Installation

From this marketplace:

```
/plugin marketplace add obs-gh-chrismilton/claude-plugins
/plugin install observe-learning-capture@chris-plugins
```

Then add the companion rules to your `~/.claude/CLAUDE.md` so Claude knows how to handle the SessionStart pending-review context. Without these rules, the plugin's hook output is invisible to Claude. See [Companion Rules](#companion-rules-required-in-claudemd) below.

## Companion Rules (required in CLAUDE.md)

Two rules must land in your global `~/.claude/CLAUDE.md`. Without them the plugin still captures and stages, but Claude will not surface candidates at session start.

### Rule A — Cross-customer Observe knowledge must propagate

When you discover behavior, gotchas, mutation signatures, error patterns, OPAL syntax quirks, cascade rules, or platform constraints that are **NOT customer-specific** (would help any IE working any tenant), append them to the appropriate section of `~/.claude/agents/ObserveIE.md` BEFORE marking the task done.

This is a hard rule, same priority as the verification checklist. The `observe-learning-capture` plugin (when installed and operational) automates the capture, but you must still verify the appropriate section was updated as part of your end-of-task hand-off.

**Customer-specific facts** (tenant IDs, dataset names like `EchoNet/foo`, contacts, customer-named monitors) go to `~/Work/<Customer>/CLAUDE.md`, **NEVER** to ObserveIE.md.

**Non-Observe facts** (general programming patterns, framework knowledge, language quirks) go to neither — they belong in tier-1 docs or your own notes.

### Rule B — Pending-review handling

When you see an `=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ===` block in your context (emitted by the plugin's SessionStart hook):

1. **Surface the candidate table to the user before responding to their first prompt.** Use this format:

   ```
   📋 N learning candidates pending review:
     #1 [high]  Section: "Title"  (cwd, date)
     #2 [med]   Section: "Title"  (cwd, date)

   Reply: merge all / merge 1, 3 / discard 2 / edit 1 / defer
   ```

2. **Wait for the user's response** before proceeding with their original prompt.
3. Parse loosely — `merge all`, `merge 1`, `discard 2`, `edit 1`, `defer`, or combinations.
4. For each merge: resolve the position number (#N) to the candidate's 8-char hash `id` from the YAML record. **Never pass the position number** — `merge_cli` requires the hash. Then run `python3 -m pipeline.merge_cli --merge {hash_id}` from the plugin directory. For discards: `--discard {hash_id}`. For edits: open the YAML record, let the user edit, then `--merge`.
5. After processing, confirm: "Merged N, discarded M, deferred K."
6. Then proceed with the user's original prompt.

**Hard constraints:**
- **NEVER auto-merge** without explicit user approval. The plugin proposes; the user disposes.
- If a candidate looks customer-specific, recommend `discard` or `edit` to redirect — per Rule A above.
- If user says `defer`, leave the queue alone — surfaces again next session.

## Manual Triggers

Two slash commands provide manual escape hatches:

- `/observe-review` — Review the pending queue mid-session (without waiting for next start)
- `/observe-capture` — Force-capture the last turn (bypasses the prefilter)

## Configuration

Defaults in `config.json` cover the common case. To override:

| Field | Default | Purpose |
|---|---|---|
| `destination_file` | `~/.claude/agents/ObserveIE.md` | Where merged learnings land |
| `pending_file` | `~/.claude/agents/.observeie-pending.md` | Staged candidates queue |
| `fallback_pending_file` | `.observeie-pending.fallback.md` | Used if main file lock-times-out |
| `log_file` | `~/.claude/logs/observe-learning-capture.log` | Plugin error + audit log |
| `haiku_model` | `claude-haiku-4-5-20251001` | Classifier model (point-in-time pin) |
| `prompt_version` | `1.0` | Tracks which prompt produced each candidate |
| `prefilter.min_turn_chars` | `150` | Skip turns shorter than this |
| `prefilter.vocabulary_terms` | 25 Observe-domain terms | Gate 1 — must contain at least one |
| `prefilter.discovery_verbs` | 8 discovery markers | Gate 2 — must contain at least one |

**Path expansion:** `~` in path values is expanded at runtime by consumers via `os.path.expanduser()` (Python) or `eval echo` (shell). Use `~/...` — do not pre-expand to absolute paths.

## Sample Workflow

A typical capture-and-review cycle:

```
You:    Delete the orphan datastreams in EchoNet.

Claude: [tries deleteDatastream(id:42767020), gets cascade error]
        Cascade-ordering deadlock on Tracing/Span — managed datasets
        reference each other. No force flag exists. Worked around by
        deleting the parent folder first via deleteFolder.

[Stop hook fires. Prefilter passes (Observe vocab + "deadlock" verb).
 Classifier runs. Pending file gets a new candidate.]

────────────────────────────────────────────────
Next day, you start a new session in ~/Work/Tekion:

[SessionStart hook fires. Pending file read. System-reminder injected.]

Claude: 📋 1 learning candidate pending review:
        #1 [high]  Object Management: cascade-ordering deadlock on
                   Tracing/* datastreams (EchoNet, 2026-04-29)

        Reply: merge all / discard 1 / edit 1 / defer

You:    merge all

Claude: [runs python3 -m pipeline.merge_cli --merge a3f7e1c2]
        Merged 1, discarded 0, deferred 0.
        Now what would you like to work on in Tekion?
```

The Tekion session — and every future session in any customer — now has access to that cascade-deadlock knowledge through ObserveIE.md.

## Cost

Each Stop hook that passes the prefilter triggers one Haiku call. Each call processes the most recent assistant turn (~1–3K tokens) plus the current ObserveIE.md (~5K tokens) plus the prompt template (~1K tokens).

- **~$0.007–$0.012 per Haiku call** at Haiku 4.5 pricing
- **~30% prefilter pass rate** in typical IE work
- **30-turn session × 30% pass × $0.01** ≈ **$0.09 per session**
- **+ SessionEnd backup scan** ≈ $0.05
- **= ~$0.10–$0.15 per session**

At ~5 IE sessions/day, that's **$0.50–$0.75/day** or roughly **$15–$25/month**.

## Troubleshooting

**Plugin installed but candidates never appear.**
- Check `~/.claude/logs/observe-learning-capture.log` for prefilter or classifier errors.
- Run the prefilter manually with `PREFILTER_ONLY=1` env to see if it's gating the turn.
- Verify `claude` CLI is on the hook's PATH and authenticated (`claude --version`).

**SessionStart fires but Claude doesn't surface candidates.**
- The `~/.claude/CLAUDE.md` Rule B is missing or not being loaded. Check `grep "Pending-review handling" ~/.claude/CLAUDE.md`.
- Verify the SessionStart hook is producing output: `bash plugins/observe-learning-capture/hooks/session-start-review.sh` should print the `=== OBSERVE LEARNING CAPTURE ===` block if pending candidates exist.

**`merge_cli` says "ID not found in pending."**
- The slash command may be passing the table position (#1) instead of the 8-char hash. Re-read CLAUDE.md Rule B step 4 — the position must be resolved to the YAML `id` field before passing to the CLI.

**Pending file has `[FAILURE] classifier` markers.**
- Real Haiku failures (timeout, auth, malformed response) emit marker candidates so they surface at review. Read the marker's `fact` field for the failure reason. Discard markers after diagnosing.

**Costs feel high.**
- Tighten `config.json`'s `prefilter.discovery_verbs` to drop your most-common false-positive triggers. The defaults already exclude `actually`, `must be`, `requires`, `signature`, etc. — verify your prefilter pass rate via a sample of log entries.

## Architecture

```
plugins/observe-learning-capture/
├── .claude-plugin/plugin.json     # Manifest
├── README.md                       # This file
├── config.json                     # Runtime config
├── docs/
│   ├── design.md                   # Full design spec (481 lines)
│   └── implementation-plan-2026-04-29.md  # TDD plan with 18 tasks
├── hooks/
│   ├── hooks.json                  # Stop, SessionEnd, SessionStart wiring
│   ├── stop-hook.sh                # Cheap shell prefilter + classifier invocation
│   ├── session-end-scan.sh         # Backup full-session scan
│   └── session-start-review.sh     # Surface pending candidates
├── pipeline/
│   ├── types.py                    # Candidate, Provenance, ClassifierMeta
│   ├── transcript.py               # JSONL turn extraction
│   ├── classifier.py               # Haiku CLI invocation
│   ├── dedupe.py                   # Content-hash dedup vs ObserveIE.md
│   ├── stage.py                    # Append-only YAML writer + parser
│   ├── merge.py                    # Promote approved → ObserveIE.md
│   ├── merge_cli.py                # CLI for slash commands
│   └── runner.py                   # Hook entry point
├── prompts/
│   ├── classifier.md               # Observe-flavored classification prompt
│   └── classifier-fewshot.md       # Yes/no examples
├── commands/
│   ├── observe-review.md           # /observe-review slash command
│   └── observe-capture.md          # /observe-capture slash command
└── tests/
    ├── fixtures/
    │   ├── sample_session.jsonl
    │   └── sample_observeie.md
    ├── test_types.py               # Candidate dataclass tests
    ├── test_transcript.py          # JSONL reader tests
    ├── test_classifier.py          # Mocked-Haiku classifier tests
    ├── test_dedupe.py              # Hash dedupe tests
    ├── test_stage.py               # YAML stage writer tests
    ├── test_merge.py               # Merge-to-ObserveIE.md tests
    ├── test_merge_cli.py           # Slash-command CLI tests
    ├── test_runner.py              # Runner orchestration tests
    ├── test_stop_hook.sh           # Shell prefilter tests
    └── test_e2e.py                 # End-to-end pipeline test
```

## Testing

```bash
cd plugins/observe-learning-capture
python3 -m unittest discover tests -v   # 76 Python tests
bash tests/test_stop_hook.sh             # 4 shell tests
```

The Python tests use stdlib `unittest` only (no pytest dependency).

## Schema

The pending file format (per design §7.1):

```yaml
---
- id: a3f7e1c2                    # SHA256(normalized fact), 8-char hex
  title: "OPAL '7d' time literal rejected"
  fact: |
    OPAL rejects '7d' as a time literal in @"…" backtick contexts;
    use '168h' instead. Also '14d' → '336h'.
  proposed_section: "OPAL Gotchas"
  confidence: high                # high | medium | low
  tags: [opal, time-literals, syntax]
  source:
    session_id: <uuid>
    cwd: /Users/chmilton/Work/EchoNet
    captured_at: 2026-04-29T11:33:00Z
    excerpt: "…1–3 lines from transcript…"
  classifier:
    model: claude-haiku-4-5-20251001
    prompt_version: "1.0"
    confidence_score: 0.88
```

After approval, the merged form in ObserveIE.md:

```markdown
- OPAL rejects '7d' as a time literal; use '168h' instead. <!-- id:a3f7e1c2 captured:2026-04-29 -->
```

The HTML comment is invisible in rendered markdown but greppable for tooling and parsed by `dedupe.py`.

## Risks and Limitations

- **v1 is Observe-only.** The classifier prompt is hardcoded for Observe-platform knowledge. A future fork (`snowflake-learning-capture`, etc.) is intentionally easy — copy the plugin, edit the prompt, change the destination file.
- **Heuristic prefilter** can have false negatives. The SessionEnd backup scan catches things the prefilter missed.
- **Hand-rolled YAML parser** (no pyyaml dependency). Restricted to the schemas this plugin emits. v1.1 candidate to swap for pyyaml if shapes expand.
- **Hash collisions**: 8-char hex = 4.3 billion namespace, ~50% birthday-paradox collision at ~65K facts. Negligible at expected corpus size (<10K lifetime). Force-capture via `/observe-capture` if you ever hit one.
- **Real Haiku non-determinism**: same input may produce slightly different fact text on repeat. Dedup catches exact-text repeats but rephrased duplicates may slip through. Manual `discard` is the escape hatch.

## Future Work (deferred to v1.1+)

- **Multi-domain framework**: shared `learning-capture-core` package, per-domain plugins reuse it
- **Auto-merge for high-confidence candidates**: tempting but risky — defer until track record shows <1% false-positive rate over months
- **Tripwire mode**: smart prefilter based on tool-call patterns instead of vocab gates
- **Cross-session conflict detection**: "candidate X contradicts existing fact Y in ObserveIE.md"
- **Web UI for queue review**: candidates as cards in a browser
- **Pyyaml swap**: replace the hand-rolled YAML emitter/parser if schema complexity grows

## Design and Plan

- **Design spec**: [`docs/design.md`](docs/design.md) — full architecture, schema, error handling, cost analysis (481 lines)
- **Implementation plan**: [`docs/implementation-plan-2026-04-29.md`](docs/implementation-plan-2026-04-29.md) — TDD plan with 18 tasks, exact code blocks, expected output for each step

## Author

Chris Milton — Observe Integration Engineer.

## License

Personal Claude Code plugin. Use at your own risk; not officially supported by Observe Inc.
