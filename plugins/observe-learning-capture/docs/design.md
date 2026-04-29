# `observe-learning-capture` — Design Spec

| Field | Value |
|---|---|
| **Author** | Chris Milton |
| **Date** | 2026-04-29 |
| **Status** | Draft — pending implementation |
| **Spec location** | `plugins/observe-learning-capture/docs/design.md` (lives with the plugin) |
| **Source brainstorm** | Conversation in `~/Work/EchoNet/`, 2026-04-29 |

---

## 1. Problem

Observe Integration Engineers (IEs) work across multiple customer tenants daily. Each session may surface non-trivial **Observe-platform-general** knowledge — undocumented API behavior, OPAL syntax quirks, mutation signatures, cascade-ordering rules, error patterns — that would benefit *any* future IE session, with *any* customer, dispatched by *any* agent variant.

Today this knowledge is captured ad-hoc:
- Sometimes the assistant updates `~/.claude/agents/ObserveIE.md` at the end of a session (manual discipline, easily forgotten).
- Sometimes findings are buried in conversation transcripts that get `/compact`-ed away.
- Cross-customer propagation is nonexistent: a learning from a Vena session is invisible to a Tekion session unless the assistant *happens* to remember and re-derive it.

The result: IEs and the agents that assist them re-discover the same gotchas repeatedly. Knowledge does not compound.

## 2. Goals

1. **Auto-capture** Observe-platform-general learnings from conversation transcripts as they happen, with no manual discipline required.
2. **Stage for review, don't auto-merge** — false positives must not silently pollute global agent knowledge.
3. **Survive `/compact`** — capture must run before the transcript is compressed away.
4. **Cross-customer propagation** — captures from `~/Work/Vena/` must be visible to sessions in `~/Work/Tekion/`. The destination is `~/.claude/agents/ObserveIE.md`, which is loaded by every ObserveIE subagent dispatch in any cwd.
5. **Cheap to run** — sub-cent per session in Haiku costs. Prefilter must skip turns with no learning candidates.
6. **Auditable** — every captured learning carries provenance (when, which session, source excerpt) so the human can verify before promoting.

## 3. Non-goals

- **No customer-specific facts.** Tenant IDs, dataset names, customer contacts, customer-specific quirks → those go to `~/Work/<Customer>/CLAUDE.md`, not into ObserveIE.md. The classifier is explicitly tuned to reject these.
- **No replacement for the `remember` plugin.** `remember` does session-replay (per-project, time-ordered narratives). This plugin does platform-knowledge-extraction (cross-project, fact-shaped, deduplicated). Different problem; both can coexist.
- **No multi-domain framework v1.** Plugin is hardcoded for Observe. Designed so a future fork (`snowflake-learning-capture`, etc.) is a copy-and-edit-prompts job, but no generalized config-driven pluggability ships in v1. (See §13.)
- **No automatic merge into ObserveIE.md.** The human is always the gatekeeper. The plugin proposes; the human disposes.

## 4. Architecture overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  Claude Code session in ~/Work/<Customer>/                          │
│                                                                     │
│  ┌─────────────┐   Stop hook fires after every Claude turn         │
│  │   Claude    │       │                                            │
│  │  finishes   │───────┴───┐                                        │
│  │  responding │           ▼                                        │
│  └─────────────┘   ┌─────────────────┐                             │
│                    │ stop-prefilter  │  cheap shell/grep:          │
│                    │     .sh         │  "is this turn worth        │
│                    └─────────────────┘   classifying?"              │
│                              │ pass                                 │
│                              ▼                                      │
│                    ┌─────────────────┐                             │
│                    │  classifier.py  │  Haiku call with            │
│                    │                 │  Observe-flavored prompt    │
│                    └─────────────────┘                             │
│                              │ candidate(s)                         │
│                              ▼                                      │
│                    ┌─────────────────┐                             │
│                    │  dedupe.py      │  hash check vs              │
│                    │                 │  existing ObserveIE.md      │
│                    └─────────────────┘                             │
│                              │ novel candidate(s)                   │
│                              ▼                                      │
│                    ┌─────────────────┐                             │
│                    │   stage.py      │  append YAML record to      │
│                    └─────────────────┘  ~/.claude/agents/          │
│                                          .observeie-pending.md     │
│                                                                     │
│  Also: SessionEnd hook does a comprehensive scan as backup,         │
│  in case Stop-prefilter has false negatives or session crashes      │
│  before Stop fires.                                                 │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  NEXT Claude Code session (any cwd, any customer)                   │
│                                                                     │
│  SessionStart hook reads .observeie-pending.md                      │
│       │                                                             │
│       ▼ injects "📋 N learning candidates pending" into context    │
│                                                                     │
│  On user's first prompt:                                            │
│    Claude shows candidate table → user replies merge/edit/discard   │
│       │                                                             │
│       ▼ merge.py promotes approved candidates into ObserveIE.md     │
│         drops them from the pending file                            │
│                                                                     │
│  All future ObserveIE subagent dispatches now see the new knowledge │
└─────────────────────────────────────────────────────────────────────┘
```

## 5. Components

```
plugins/observe-learning-capture/
├── .claude-plugin/
│   └── plugin.json              # plugin manifest
├── README.md                    # user-facing docs
├── config.json                  # destination paths, debug flag, model
├── docs/
│   └── design.md                # this file
├── hooks/
│   ├── hooks.json               # Stop, SessionEnd, SessionStart registrations
│   ├── stop-hook.sh             # prefilter (inline) + classifier invocation
│   ├── session-end-scan.sh      # full-session backup scan
│   └── session-start-review.sh  # surfaces pending candidates
├── pipeline/
│   ├── classifier.py            # Haiku call + prompt assembly
│   ├── dedupe.py                # hash check against ObserveIE.md
│   ├── stage.py                 # append to .observeie-pending.md
│   ├── merge.py                 # promote approved → ObserveIE.md
│   ├── transcript.py            # extract recent turn(s) from session JSONL
│   └── types.py                 # dataclasses for Candidate, ProvenanceMeta
├── prompts/
│   ├── classifier.md            # Observe-flavored classification prompt
│   └── classifier-fewshot.md    # examples of yes/no candidates
├── commands/
│   ├── observe-review.md        # /observe-review — manual queue review
│   └── observe-capture.md       # /observe-capture — force-capture last turn
└── tests/
    ├── conftest.py
    ├── fixtures/                # sample transcripts, expected outputs
    ├── test_prefilter.sh        # bats or shell-based tests
    ├── test_classifier.py       # mock Haiku, verify prompt assembly
    ├── test_dedupe.py
    ├── test_stage.py
    ├── test_merge.py
    └── test_transcript.py
```

### 5.1 `hooks/stop-hook.sh` — prefilter + classifier invocation

Triggered: every Claude-turn end. Single shell entry point invoked by Claude Code's hooks.json. Internally:

1. Reads turn metadata from environment variables Claude Code provides to hooks: `CLAUDE_SESSION_ID`, `CLAUDE_PROJECT_DIR`, `CLAUDE_TRANSCRIPT_PATH` (path to the session JSONL).
2. Runs the prefilter heuristic inline (pure shell, no subprocess except `jq`).
3. If prefilter passes → invokes `python3 pipeline/classifier.py` with the session metadata.
4. Always exits 0 (hooks must not block session flow).

Prefilter logic (heuristic, not LLM-based — runs inline before Haiku invocation):
- Read the last assistant turn from `$CLAUDE_TRANSCRIPT_PATH` (the JSONL file Claude Code passes to every hook via env).
- **Pass to classifier IF any of:**
  - Turn contains substring matches for Observe-platform vocabulary: `OPAL`, `Observe`, `dataset`, `datastream`, `monitor`, `worksheet`, `dashboard`, `accelerat`, `bookmark`, `transform`, `filedrop`, `poller`, `bundle`, `pick_col`, `make_col`, `statsby`, `timechart`, `deleteDataset`, `deleteMonitor`, etc.
  - **AND** turn is more than ~150 chars long (filters trivial acks).
  - **AND** turn contains either: a discovered API behavior verb (`turns out`, `actually`, `discovered`, `it errors`, `must be`, `won't accept`, `cascade`, `signature`, `requires`, `surprisingly`); OR an OPAL block (fenced code with `filter`, `make_col`, `statsby`, etc.); OR an HTTP error code in 4xx/5xx; OR a GraphQL mutation name pattern.
- **Fail (exit 1) IF:**
  - Turn is shorter than 150 chars.
  - Turn is purely tool-result echoing without prose (heuristic: ratio of code/quoted lines to prose lines).
  - Turn contains the literal phrase "I'll do that" or "Sure" as 80%+ of its prose content.

Performance budget: <50ms. No subprocess except `jq` for transcript extraction.

### 5.2 `pipeline/classifier.py` — Haiku call

Inputs:
- The last assistant turn (and optionally the preceding user prompt for context).
- The current `~/.claude/agents/ObserveIE.md` content (so Haiku knows what's already known and won't re-propose).
- Session metadata: `session_id`, `cwd`, ISO timestamp.

Process:
1. Load `prompts/classifier.md` template, substitute `{{TURN}}`, `{{ALREADY_KNOWN}}`, `{{CONTEXT_TIMESTAMP}}`.
2. Call Haiku via `claude` CLI subprocess (or anthropic SDK if available locally).
3. Parse Haiku response — must be a YAML list (possibly empty).
4. For each candidate, attach provenance fields and emit.

Returns: `list[Candidate]` (possibly empty).

Failure modes (per Chris's "log AND surface" rule):
- Haiku call fails → log to `~/.claude/logs/observe-learning-capture.log` with full context AND emit a marker candidate `{title: "[FAILURE] classifier", fact: "Haiku call failed: <error>", confidence: low, tags: [self-error]}` so the failure is visible at next review.
- Haiku returns malformed YAML → log AND emit a marker candidate with the raw response for debugging.
- ObserveIE.md unreadable → fall back to empty `ALREADY_KNOWN` and log warning.

### 5.3 `pipeline/dedupe.py` — content-hash dedup

For each candidate from the classifier:
1. Compute SHA256 of `fact` field (after light normalization: lowercase, collapse whitespace, strip punctuation).
2. Check against:
   - Existing fact-hashes in ObserveIE.md (precomputed when read by classifier).
   - Existing pending-file entries.
3. If hash matches existing → skip (or in pending, update `last_seen_at` and add new provenance to a list).
4. If novel → pass to stage.

Also emits a `near-duplicate` warning if the candidate's `tags` overlap >50% with an existing candidate but the hash differs (review-time hint, not a block).

### 5.4 `pipeline/stage.py` — append to pending file

Pending file: `~/.claude/agents/.observeie-pending.md`.

Format: YAML front-matter list (one document per candidate). Append-only — never rewrite, never reorder. Keeps file simple and `git diff`-friendly if user version-controls their `~/.claude/`.

```yaml
---
- id: a3f7e1c2
  title: "OPAL '7d' time literal rejected"
  fact: |
    OPAL rejects '7d' as a time literal in @"…" backtick contexts;
    use '168h' instead. Also '14d' → '336h'.
  proposed_section: "OPAL Gotchas"
  confidence: high
  tags: [opal, time-literals, syntax]
  source:
    session_id: b63cc30c-7e49-4edc-8845-0425e57def85
    cwd: /Users/chmilton/Work/EchoNet
    captured_at: 2026-04-29T11:33:00Z
    excerpt: |
      I tried `time > now() - 7d` and got "expected duration literal".
      Switched to `168h` and it parsed.
  classifier:
    model: claude-haiku-4-5-20251001
    prompt_version: 1.0
    confidence_score: 0.88
- id: ...
  ...
```

### 5.5 `pipeline/merge.py` — promote approved → ObserveIE.md

Triggered by user-side approval action (handled by my conversational layer, see §8).

Per-candidate merge:
1. Read ObserveIE.md.
2. Find heading matching `proposed_section` (case-insensitive, `##` and `###` both considered).
3. If heading exists → append `- {fact}` as a bullet under it (with HTML comment carrying `id` for future re-dedup).
4. If heading doesn't exist → append a new `## {proposed_section}` section at end of file with the bullet.
5. Remove candidate from `.observeie-pending.md`.
6. Log to `~/.claude/logs/observe-learning-capture.log`: `MERGE id={id} section={section} session={session_id}`.

The merged bullet looks like:

```markdown
- OPAL rejects '7d' as a time literal; use '168h' instead. <!-- id:a3f7e1c2 captured:2026-04-29 -->
```

Clean to read; HTML comment is invisible in rendered markdown but greppable for tooling.

### 5.6 `commands/observe-review.md` — manual review trigger

User types `/observe-review` mid-session → I read `.observeie-pending.md`, render the candidate table, and start the same approval flow as session-start. Useful when the user wants to clear the queue without waiting for next session.

### 5.7 `commands/observe-capture.md` — force-capture last turn

User types `/observe-capture` → I run `pipeline/classifier.py` against the last turn unconditionally (bypassing prefilter). Useful when the user notices "hey that was a learning the prefilter probably missed."

### 5.8 `config.json` — runtime configuration

Lives at `plugins/observe-learning-capture/config.json`. Defaults shipped; user can override by editing.

```json
{
  "destination_file": "~/.claude/agents/ObserveIE.md",
  "pending_file": "~/.claude/agents/.observeie-pending.md",
  "log_file": "~/.claude/logs/observe-learning-capture.log",
  "haiku_model": "claude-haiku-4-5-20251001",
  "prompt_version": "1.0",
  "prefilter": {
    "min_turn_chars": 150,
    "vocabulary_terms": ["OPAL", "Observe", "dataset", "datastream", "..."],
    "discovery_verbs": ["turns out", "actually", "discovered", "..."]
  },
  "session_end_scan_enabled": true,
  "stop_scan_enabled": true,
  "debug": false
}
```

A future `snowflake-learning-capture` plugin would fork this plugin, edit destination/prompt, and that's it. v1 does not generalize beyond per-config customization.

## 6. Data flow — end-to-end scenarios

### Scenario A: Real-time capture during EchoNet session

1. User: "OK delete the rest of the orphan datastreams."
2. Claude does work, hits cascade-ordering deadlock on `Tracing/Span`, learns the rule.
3. Claude finishes the turn explaining the deadlock.
4. **Stop hook fires** → `stop-prefilter.sh`:
   - Turn contains `Tracing/Span`, `cascade`, `datastream`, length 800 chars, has "discovered" verb. **Pass.**
5. **`stop-classify.sh` runs**:
   - Calls `classifier.py` with the turn + current ObserveIE.md.
   - Haiku returns one candidate: `{title: "Cascade-ordering deadlock on Tracing datastreams", fact: "...", proposed_section: "Object Management and Cleanup", confidence: high, tags: [delete, cascade, tracing]}`.
6. `dedupe.py`: hash not present in ObserveIE.md (the cleanup learnings *are* there from prior session, but this is a *new* aspect). Novel.
7. `stage.py`: appends to `~/.claude/agents/.observeie-pending.md`.
8. Hook exits silently. User sees no UI change. Total added latency: ~1–3s (Haiku call), runs after my response is already on screen.

### Scenario B: SessionEnd backup catches what Stop missed

1. Long session, prefilter false-negatived a turn (e.g., learning was in a tool result that prefilter heuristic didn't recognize).
2. User runs `/exit`.
3. **SessionEnd hook fires** → `session-end-scan.sh`:
   - Reads the entire session transcript.
   - Calls `classifier.py` once with the full transcript + ObserveIE.md + current pending file (so it doesn't re-propose what Stop already staged).
   - Haiku returns the missed candidate.
4. `dedupe.py` + `stage.py` as before.

### Scenario C: Next session start — review

1. User starts a new session in `~/Work/Tekion/` (different customer).
2. **SessionStart hook fires** → `session-start-review.sh`:
   - Reads `.observeie-pending.md`.
   - If non-empty, emits to stdout (which Claude Code injects as system-reminder context):
     ```
     === OBSERVE LEARNING CAPTURE — PENDING REVIEW ===
     2 candidate(s) pending review from prior sessions:
       #1 [high] OPAL Gotchas: '7d' rejected, use '168h' (EchoNet, 2026-04-29)
       #2 [high] Object Management: Cascade-ordering deadlock on Tracing/Span (EchoNet, 2026-04-29)
     I should surface these on the next user prompt before responding to it.
     ```
3. User: "Help me check Tekion's metric ingest."
4. Claude (me) sees the system reminder and, **before** addressing the prompt:
   ```
   📋 Two learning candidates pending review from earlier:

   #1 [high]  OPAL Gotchas
              "OPAL rejects '7d'; use '168h'"  (EchoNet, 2026-04-29)
   #2 [high]  Object Management and Cleanup
              "Cascade-ordering deadlock on Tracing/* datastreams..."  (EchoNet, 2026-04-29)

   Reply: `merge all` / `merge 1` / `discard 2` / `defer` (keep in queue) / `edit 1` (open the candidate for editing).
   ```
5. User: `merge all`.
6. I run `merge.py` for each, commit the result if `~/.claude` is git-tracked, then proceed with the Tekion task.

### Scenario D: User-initiated review mid-session

1. Mid-session, user types `/observe-review`.
2. Same flow as scenario C steps 4–6, just on demand.

### Scenario E: Force-capture

1. User notices a learning the prefilter would skip ("the API actually returns 422 not 400 for this case").
2. User types `/observe-capture`.
3. Slash command runs `classifier.py` against the last turn unconditionally.
4. Result staged as normal. Surfaces at next review (or `/observe-review`).

## 7. Schema details (canonical)

### 7.1 Pending file: `~/.claude/agents/.observeie-pending.md`

YAML list. One document per candidate. Append-only. No reordering.

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | SHA256(normalized fact) truncated to 8 chars |
| `title` | string | yes | ≤80 chars, used in review UI |
| `fact` | string (block) | yes | The learning itself, prose, 1–5 lines |
| `proposed_section` | string | yes | Heading in ObserveIE.md (e.g., "OPAL Gotchas") |
| `confidence` | enum | yes | `high`/`medium`/`low` |
| `tags` | list[string] | yes | normalized lowercase, hyphenated |
| `source.session_id` | string | yes | Claude Code session UUID |
| `source.cwd` | string | yes | absolute path |
| `source.captured_at` | string | yes | ISO 8601 UTC |
| `source.excerpt` | string (block) | yes | 1–3 lines from transcript |
| `classifier.model` | string | yes | e.g., `claude-haiku-4-5-20251001` |
| `classifier.prompt_version` | string | yes | semver of prompt template |
| `classifier.confidence_score` | float | no | model's self-rating, 0.0–1.0 |
| `dupe_warning` | string | no | populated by dedupe.py if near-duplicate |
| `last_seen_at` | string | no | populated if same id captured again |

### 7.2 Merged form in ObserveIE.md

Plain markdown bullet, HTML-comment carrying `id`. Example:

```markdown
## OPAL Gotchas

- OPAL rejects '7d' as a time literal; use '168h' instead. Also '14d' → '336h'. <!-- id:a3f7e1c2 captured:2026-04-29 -->
- ...
```

The HTML comment is what `dedupe.py` reads to know "this fact is already in ObserveIE.md."

## 8. Approval UX (detailed)

The approval flow is implemented partly in shell (the SessionStart hook surfaces context) and partly in Claude's behavior (me reading the context and acting on it). The plugin **does not** ship its own UI — it relies on Claude reading the system-reminder context and the user's CLAUDE.md rule (see §11) telling Claude how to handle it.

User reply grammar (matched conversationally, not a strict parser):
- `merge all` — merge every pending candidate
- `merge 1, 3` — merge specific candidates by review-table number
- `discard 2` — drop candidate #2 from pending without merging
- `discard all` — empty the queue
- `edit 1` — drop user into editing the candidate (I open the YAML, user edits, then merges)
- `defer` — leave the queue alone, surface again next session
- `defer 1, merge 2` — combinations

## 9. Error handling and silent-failure prevention

Per `~/.claude/CLAUDE.md` "graceful fallback ≠ silent failure":

| Failure mode | Detection | Surface | Recovery |
|---|---|---|---|
| Hook directory bootstrap missing | Hook script first line | `mkdir -p` then continue (don't fail like `remember` does) | N/A |
| Transcript file unreadable | `transcript.py` | Log + skip this turn (Stop hook only); SessionEnd will retry | Wait for next trigger |
| Haiku CLI missing | `classifier.py` import-time check | Log + emit marker candidate with `tags: [self-error, haiku-missing]` | User sees marker at review |
| Haiku returns malformed output | `classifier.py` parser | Log full response, emit marker candidate with raw response | User reviews |
| ObserveIE.md unreadable | `classifier.py`, `merge.py` | Log + classifier proceeds with empty `ALREADY_KNOWN`; merge fails loudly | User notified at review |
| Pending file unwritable | `stage.py` | Log + emit to a fallback location `~/.claude/.observeie-pending.fallback.md` | Auto-recover next session if main file becomes writable |
| Dedupe hash collision (vanishingly rare) | `dedupe.py` | Treat as duplicate, log warning | User force-captures via `/observe-capture` if needed |
| Concurrent writes to pending file from two simultaneous sessions | `stage.py` | Use POSIX `flock` on the file before append; if lock fails after 2s, write to `~/.claude/agents/.observeie-pending.<pid>.md` and log | Next merge step picks up all sibling pending files |
| Concurrent reads from two SessionStart hooks | `session-start-review.sh` | Read-only operation, no race. Both sessions surface same candidates; first to merge wins; second's merge fails gracefully (id no longer in pending) and that's fine | N/A |

All errors logged to `~/.claude/logs/observe-learning-capture.log` with structured format:

```
2026-04-29T11:33:00Z [ERROR] component=classifier session=b63cc30c... reason="haiku call failed: timeout" detail=...
```

Log rotation: tail-based, max 10MB then rotate to `.1`, `.2`, etc.; oldest deleted at 5 generations.

## 10. Testing approach (TDD)

Per global CLAUDE.md "strict TDD always":

### 10.1 Unit tests (Python)

- `test_classifier.py`: mock Haiku, verify prompt assembly, parse correct/malformed responses, marker-candidate emission on failure
- `test_dedupe.py`: verify hash normalization, ObserveIE.md scanning, near-duplicate detection
- `test_stage.py`: verify YAML append, ID uniqueness, no rewriting of existing entries
- `test_merge.py`: verify section creation/append, HTML-comment id insertion, removal from pending
- `test_transcript.py`: verify JSONL parsing, last-turn extraction, multi-turn extraction for SessionEnd

### 10.2 Integration tests (shell + Python)

- `test_prefilter.sh`: feed sample transcripts, assert pass/fail per heuristic rule
- End-to-end test: synthesize a session JSONL → run Stop hook → run SessionStart hook → assert pending file state and ObserveIE.md state after simulated user approval

### 10.3 Manual verification checklist (per CLAUDE.md verification rule)

Before declaring done:
1. ✅ Tests green — pytest + bats output pasted
2. ✅ Linter clean — ruff for Python, shellcheck for shell
3. ✅ Manual exercise — install plugin, run a real EchoNet session that contains a known learning, verify it lands in `.observeie-pending.md`. Restart, verify SessionStart prompt appears. Approve, verify ObserveIE.md updated.
4. ✅ Logs reviewed — no warnings introduced

## 11. Companion: global CLAUDE.md rule

A new "Hard Rule" must land in `~/.claude/CLAUDE.md` so Claude knows to handle the SessionStart system-reminder properly:

> **Observe learning candidates pending review.** When SessionStart context contains `=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ===`, surface the candidate table to the user **before** responding to their first prompt. Use the review grammar (`merge all` / `merge N` / `discard N` / `edit N` / `defer`). After approval, run the merge step (or invoke `/observe-merge` if the slash command exists), then proceed with the user's prompt. Customer-specific facts must NEVER be merged into ObserveIE.md — those go to `~/Work/<Customer>/CLAUDE.md`. If a candidate seems customer-specific, recommend `discard` or `edit` to redirect.

This rule lands in the same commit pass as the plugin. Without it, the plugin's SessionStart context would be ignored.

## 12. Cost analysis

Per-Stop scan (when prefilter passes):
- Input tokens: last turn (~500–3000) + ObserveIE.md (~5000) + prompt template (~1000) ≈ 6500–9000 tokens
- Output tokens: candidates list (~100–500 tokens)
- At Haiku 4.5 pricing (~$1/M input, $5/M output): **~$0.007–$0.012 per scan**

Per session estimate:
- 30-turn session, ~30% prefilter pass rate → ~9 Haiku calls
- 9 × $0.01 ≈ **$0.09/session**
- Plus SessionEnd backup: 1 × full-session scan (~10–30K tokens) ≈ **$0.05**

**~$0.10–$0.15 per session.** At ~5 sessions/day → **$0.50–$0.75/day**, or ~$15–25/month. Acceptable.

If costs become a concern, two levers:
- Tighten the prefilter (drop pass rate to 15%)
- Skip Haiku ObserveIE.md inclusion when transcript is small; use a cheaper "is this Observe-related at all" yes/no Haiku call as a second-stage filter

## 13. Future work (explicitly out of scope for v1)

- **Generic framework** (multi-domain, config-driven): support `snowflake-learning-capture` as a sibling plugin via shared `learning-capture-core` package. Defer until second domain is needed.
- **Auto-merge for high-confidence candidates**: tempting but risky. Defer until track record shows <1% false-positive rate over months.
- **`tripwire` mode** (option 4 from brainstorming): smart filter based on tool-call patterns. Defer; v1 uses dumb prefilter.
- **Cross-session conflict detection**: "candidate X contradicts existing fact Y in ObserveIE.md". Hard problem. Defer.
- **Web UI for queue review**: candidates as cards in a browser. Defer; conversational review is fine.
- **Confidence-weighted auto-discard**: drop low-confidence candidates after N sessions if not approved. Defer.

## 14. References

- Brainstorm conversation: `~/Work/EchoNet/` session, 2026-04-29 (transcript path: `~/.claude/projects/-Users-chmilton-Work-EchoNet/<uuid>.jsonl`)
- Sister plugins (layout reference): `~/repos/claude-plugins/plugins/opal-optimizer/`
- Destination file: `~/.claude/agents/ObserveIE.md`
- Companion rule: `~/.claude/CLAUDE.md` "Observe learning candidates" section (added in same PR)
- Anthropic Claude Code hooks docs: <https://docs.claude.com/en/docs/claude-code/hooks>

---

**Status:** Draft. Awaiting user review before implementation plan.
