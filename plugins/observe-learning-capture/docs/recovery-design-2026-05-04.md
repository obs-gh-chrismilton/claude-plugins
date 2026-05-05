# observe-learning-capture: Pipeline Recovery Design

**Date:** 2026-05-04
**Author:** Chris Milton (with structured agent review)
**Status:** Approved; awaiting implementation plan
**Supersedes:** none — this layers on top of `docs/design.md` (original plugin architecture)
**Related:** `docs/design.md` (original architecture, unchanged in spirit), `docs/implementation-plan-2026-04-29.md` (initial implementation plan, completed)

---

## 1. Background

The `observe-learning-capture` plugin auto-stages Observe-platform learnings from Claude Code sessions for human review, then promotes approved candidates into `~/.claude/agents/ObserveIE.md`. As of 2026-05-01, runtime telemetry showed the auto-capture pipeline was effectively non-functional across all sessions:

- Stop-hook prefilter rejected ~83% of substantive turns (86 of 103 hook fires logged "no assistant turn extracted" in a 24h window).
- The 17 turns that did pass the prefilter produced **zero** real candidates — every classifier subprocess invocation either timed out silently or returned empty.
- The pending YAML queue (`~/.claude/agents/.observeie-pending.md`) had ballooned to 133 KB with **one** record: a `[FAILURE] classifier` marker whose serialized failure_reason embedded the full subprocess argv (including a 35 KB rendered prompt).
- The session-start review hook's inline-Python module import was failing with `ModuleNotFoundError`, silently breaking the candidate-surfacing UX.

Root-cause analysis identified five distinct, independently-triggering bugs spanning bash hooks, Python pipeline code, and the classifier subprocess invocation pattern. This document specifies the fix.

---

## 2. Goals

1. Restore reliable per-Stop and per-SessionEnd capture of Observe-platform learnings.
2. Eliminate silent failure modes — every dropped capture must leave a visible breadcrumb (log AND surfaced marker), per the existing spec §9 mantra ("log AND surface — never silent").
3. Cut classifier latency below the timeout budget reliably (P99 < 60 s) by replacing the recursive `claude --print` subprocess with the Anthropic Python SDK + prompt caching + slim payload.
4. Stop poisoning the pending queue with marker bloat.
5. Restore the session-start review surface (currently silently broken on a subset of sessions).

## 3. Non-Goals (deferred)

- Changing the YAML pending-file format — the existing schema works.
- Re-tuning the classifier prompt content for accuracy. We're fixing infrastructure, not prompt-engineering.
- Adding new section taxonomies or new dedup heuristics.
- Adding an API key file / secret-management layer (decision: env var is sufficient).
- Backfilling captures from past sessions whose transcripts still exist on disk. (Possible follow-up, not blocking.)

## 4. Constraints

- Must remain auth-agnostic in Python: code calls `anthropic.Anthropic()` with no args; user manages `ANTHROPIC_API_KEY` in shell env.
- Must not introduce new pip dependencies (`anthropic` v0.97.0 already installed in user's site-packages).
- Must preserve existing CLI tools (`merge_cli.py`, `/observe-review`, `/observe-capture`) unchanged.
- Existing pending YAML records (written by the OLD classifier) must continue to parse correctly under the new code path. (Verified: `Candidate.from_yaml_record` at `pipeline/types.py:286` tolerates extra fields and missing optional blocks.)
- Hooks must continue to exit 0 always (never block session flow).

---

## 5. Decisions Audit Trail

Captured during structured brainstorm (2026-05-01 to 2026-05-04). Each decision was preceded by a 3-option proposal with tradeoffs.

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Q1 — Architectural target | **C: Replace `claude --print` with Anthropic Python SDK** | Per-Stop signal is genuinely useful (live capture catches discoveries while context is warm); recursive-CLI cancellation hazard isn't fixable inside option A (patch in place); option B (drop per-Stop) throws away half the design intent. |
| Q2 — Prompt size strategy | **C: Slim payload AND prompt caching** | Dedupe is the actual correctness mechanism; sending Haiku/Sonnet the full 30 KB is asking the model to do dedup work that's already done deterministically post-classify. Slimming respects layered design. |
| Q3 — API key source | **A: `ANTHROPIC_API_KEY` exported in `~/.zshrc`** | Standard SDK behavior; user generates separate API key from console.anthropic.com that bills against same Anthropic account as Claude Code subscription; plugin code stays auth-free. |
| Q4 — Scope | **A: All 5 bugs in one plan, sequenced** | Fixes are tightly entangled (Bug 3 sanitation unblocks visibility into Bugs 2/5; Bug 4 unblocks verification of Bug 1's surfacing). Implementation plan can still stage per-bug for review checkpoints. |
| Cache mitigation (raised by validator) | **A: Switch model from Haiku 4.5 to Sonnet 4.5** | Haiku 4.5's prompt-cache minimum is 4096 tokens; slim payload at ~1100 tokens would silently no-op. Sonnet 4.5's 1024-token minimum lets the slim payload actually cache. ~3× per-call cost is irrelevant at this volume (~5–10¢/day total). Sonnet's lower TTFT variance also reduces the operational fragility that originally triggered Bug 2. |
| `all_assistant_turns` (session-end mode) | **Leave per-record concat untouched** | Session-end's purpose is full-session retrospective; per-record concat is acceptable; minimizes diff. Logical-turn aggregation matters for Stop-mode classification, not for the SessionEnd retrospective scan. |

---

## 6. Architecture

### 6.1 Data flow

```
┌─────────────────────────────────────────────────────────────────────┐
│  Claude Code Stop event ─────► hooks/stop-hook.sh                   │
│                                  │                                  │
│                                  ▼                                  │
│                        [Prefilter — 3 gates]                        │
│  Gate 0 (NEW): aggregate "current logical turn" via reverse jq walk:│
│                from end of transcript, collect .text from each      │
│                consecutive assistant record, stop at first user     │
│                record where .message.content is a string AND text   │
│                does NOT match ^(/clear|/compact|=== |<command-name>)│
│  Gate 1: turn_text length ≥ 150 chars                               │
│  Gate 2: at least one Observe vocab term                            │
│  Gate 3: at least one discovery verb / HTTP 4xx-5xx / GraphQL       │
│          mutation pattern                                           │
│                                  │                                  │
│                                  ▼ if all 3 gates pass              │
│                python3 -m pipeline.runner --mode stop  (bg subshell)│
└──────────────────────────────────┼──────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  pipeline/runner.py  — STARTUP precheck added                       │
│    0a. Assert os.environ.get("ANTHROPIC_API_KEY"); on absence emit  │
│        marker via direct append_candidates and exit 0               │
│    0b. client.models.list(limit=1)  ◄── free auth probe; on         │
│        AuthenticationError emit marker and exit 0                   │
│    1. transcript.current_logical_turn(path)  ◄── NEW, replaces      │
│       last_assistant_turn for stop mode                             │
│    2. classifier.classify(turn_text, ...)                           │
│    3. dedupe (unchanged)                                            │
│    4. append_candidates                                             │
│    Outer except Exception: emits marker via direct append_candidates│
│       call (NOT just log+swallow as before)                         │
└──────────────────────────────────┼──────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  pipeline/classifier.py  ── SDK REWRITE ──                          │
│                                                                     │
│   client = anthropic.Anthropic()  # auto-loads ANTHROPIC_API_KEY    │
│   response = client.messages.create(                                │
│     model="claude-sonnet-4-5",                                      │
│     max_tokens=2048,                                                │
│     system=[                                                        │
│       {"type":"text", "text": STATIC_TEMPLATE,                      │
│        "cache_control": {"type":"ephemeral"}},   ◄── cached block 1 │
│       {"type":"text", "text": SLIM_KNOWN_FACTS,                     │
│        "cache_control": {"type":"ephemeral"}},   ◄── cached block 2 │
│     ],                                                              │
│     messages=[{"role":"user", "content": USER_MESSAGE}],            │
│     timeout=120,                                                    │
│     max_retries=0,        ◄── own retry budget                      │
│   )                                                                 │
│                                                                     │
│   yaml_output = next((b.text for b in response.content              │
│                       if b.type == "text"), "")                     │
│                                                                     │
│   # Cache visibility (one-shot via sentinel)                        │
│   if cache_warning_unfired and call_count >= 5:                     │
│     if response.usage.cache_read_input_tokens == 0:                 │
│       emit_one_shot_marker("cache disabled: prefix below threshold")│
│       touch sentinel  ──► ~/.claude/agents/.observe-cache-warned    │
│   elif sentinel_exists and response.usage.cache_read_input_tokens>0:│
│     unlink sentinel  ──► self-healing on first observed cache hit   │
│                                                                     │
│   except anthropic.AuthenticationError as e:                        │
│     marker(failure_reason=f"key rejected: {e.status_code}")         │
│   except anthropic.APIError as e:                                   │
│     marker(failure_reason=f"{type(e).__name__}: {_sanitize(e)}")    │
│                                                                     │
│   Per-record KeyError on Haiku output → marker (was: silent skip)   │
│   ALL marker failure_reason values routed through _sanitize()       │
└──────────────────────────────────┼──────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  hooks/session-start-review.sh ── PATCHED ──                        │
│   Drops inline `python3 -c '...'` entirely. Replaced with:          │
│     python3 -m pipeline.render_pending                              │
│   New module pipeline/render_pending.py:                            │
│     - Real __file__ resolves cleanly                                │
│     - Mockable from tests                                           │
│     - On read_pending YAML failure, prints                          │
│       === OBSERVE LEARNING CAPTURE — RENDER FAILED ===              │
│       block to stdout (Claude Code injects to context)              │
└─────────────────────────────────────────────────────────────────────┘
```

### 6.2 Layered prompt structure (cache-correct)

The prompt is restructured so that **only static / slowly-changing content** lives in cached system blocks; per-call values move into the user message. This is critical: any per-call placeholder in a cached block invalidates the cache on every call.

| Source | Destination | Cache behavior |
|--------|-------------|----------------|
| `prompts/classifier.md` (with `{{ALREADY_KNOWN}}`/`{{TURN}}`/`{{CWD}}`/`{{CONTEXT_TIMESTAMP}}` placeholders **removed**) | system block 1 | Cached `ephemeral`. Effectively never changes (template edits are rare and intentional). |
| Slim known-facts: section headers + flat list of all `id:` hashes already in ObserveIE.md (~1 KB; regenerated once per Classifier process; bounded — `id`s only, no body text) | system block 2 | Cached `ephemeral`. Invalidates only when ObserveIE.md is merged into. |
| Per-call: `<turn>{TURN_TEXT}</turn>\n<cwd>{CWD}</cwd>\n<context_timestamp>{ISO}</context_timestamp>` | user message content | Not cached; varies every call. |

**Note:** No `anthropic-beta` header required at SDK ≥ 0.30 (we have 0.97.0). Ephemeral cache is GA.

### 6.3 Components touched vs unchanged

**Touched:**
- `pipeline/classifier.py` — SDK rewrite; new `_sanitize`; layered prompt build; cache visibility sentinel; expanded exception taxonomy; per-record KeyError → marker.
- `pipeline/transcript.py` — new `current_logical_turn(path)` function. Existing `last_assistant_turn` and `all_assistant_turns` retained (used by session-end mode and possibly tests).
- `pipeline/runner.py` — startup auth precheck; `last_assistant_turn` → `current_logical_turn` swap at line 105; outer `except Exception` emits marker via direct append.
- `pipeline/render_pending.py` — NEW module replacing inline `python3 -c '...'` in session-start hook.
- `hooks/stop-hook.sh` — bash jq prefilter rewritten to walk-back logic for consistency with Python.
- `hooks/session-start-review.sh` — drops inline Python; calls `python3 -m pipeline.render_pending`.
- `config.json` — rename `haiku_model` → `classifier_model`; default value `"claude-sonnet-4-5"`. Read with back-compat fallback: `config.get("classifier_model", config.get("haiku_model", "claude-sonnet-4-5"))`.

**Unchanged:**
- `pipeline/stage.py` — existing PID-fallback path at `stage.py:56-62` already covers disk-full; no need for additional wrapping.
- `pipeline/dedupe.py`, `pipeline/merge.py`, `pipeline/merge_cli.py`, `pipeline/types.py` — surface and behavior preserved.
- `hooks/session-end-scan.sh` — same shape; same runner call; Bug 1's logical-turn change does not propagate here per session-end retrospective design.
- `commands/*.md` — slash commands unchanged.
- `prompts/classifier.md` — content edits required (placeholder removal) but file path stable; same template.

---

## 7. Per-Bug Fix Details

### Bug 1 — Logical turn aggregation
**Failure mode:** Bash jq prefilter at `hooks/stop-hook.sh:81-89` AND `transcript.last_assistant_turn()` at `transcript.py:102-109` both pick only the LAST single JSONL record per Stop event. Modern transcripts emit ~6 records per logical turn; the last one is usually a tool_use chunk (empty after text-filter) or a 7-char ack like "Saved." that fails the 150-char gate. Substantive turn content silently dropped.

**Fix locations:**
- `pipeline/transcript.py` — new `current_logical_turn(path: Path) -> Optional[Turn]`:
  - Iterates JSONL records into a list.
  - Walks backward from end of list.
  - Collects `_extract_text(record["message"]["content"])` from each record where `record["type"] == "assistant"`.
  - Stops when it encounters a record where `record["type"] == "user"` AND `_is_real_user_prompt(record)` returns True.
  - Returns a `Turn` with `text = "\n".join(reversed(collected))` (preserve chronological order).
  - Returns `None` if collected list is empty.
- `pipeline/transcript.py` — new helper `_is_real_user_prompt(record: dict) -> bool`:
  - Returns False if `record["message"]["content"]` is not a string (e.g., is a list of `tool_result` blocks).
  - Returns False if string matches any known-injection pattern: starts with `/clear`, `/compact`, `=== `, `<command-name>`, `<system-reminder>`.
  - Returns True otherwise.
  - On unknown pattern (string content not matching above and not matching a heuristic real-prompt regex like `^[^/=<]`), emit a marker with `failure_reason="unknown user-record kind: <first 80 chars>"` to surface drift over time.
- `pipeline/runner.py:105` — swap `last_assistant_turn(transcript_path)` → `current_logical_turn(transcript_path)` for `--mode stop`. `--mode session-end` continues to use `all_assistant_turns` per the decision in §5.
- `hooks/stop-hook.sh:81-89` — replace existing jq with reverse-walk equivalent. Approximate jq snippet:
  ```jq
  [.[]] | reverse |
  reduce .[] as $r (
    {collected: [], stop: false};
    if .stop then .
    elif $r.type == "assistant" then
      .collected += [
        ($r.message.content | (
          if type == "string" then [.]
          else map(select(.type == "text") | .text)
          end
        ))
      ] | flatten
    elif $r.type == "user" and ($r.message.content | type) == "string"
         and ($r.message.content | test("^(/clear|/compact|=== |<command-name>|<system-reminder>)") | not)
    then .stop = true
    else .
    end
  ) | .collected | reverse | join("\n")
  ```
  (Final form to be tightened during implementation; semantically: collect assistant text walking backward, stop at first real user prompt.)

**Regression test:** `tests/test_transcript_logical_turn.py` — synthetic transcript with 6 assistant records (3 thinking, 2 tool_use, 1 final "Saved.") preceded by a real user prompt. Asserts `current_logical_turn()` returns concatenation of substantive earlier text, not just "Saved."

### Bug 2 — Classifier subprocess → SDK rewrite
**Failure mode:** `pipeline/classifier.py:283-312` shells out to `claude --print` with 60s timeout and 30 KB ALREADY_KNOWN payload (full ObserveIE.md). Recursive Claude-Code-from-inside-Claude-Code invocation; routinely times out (Haiku TTFT ~32s on user's connection).

**Fix:** Rewrite `_invoke_haiku` → `_invoke_classifier` to use Anthropic Python SDK. Code shape per §6.1.

**Sub-changes:**
- `_build_prompt` returns a tuple `(static_template: str, slim_known_facts: str, user_message: str)` instead of one rendered string.
- `STATIC_TEMPLATE` loaded once per Classifier instance from `prompts/classifier.md` with all `{{...}}` placeholders removed (template edits to remove placeholders are part of this fix).
- `SLIM_KNOWN_FACTS` regenerated once per Classifier instance via a new helper that scans ObserveIE.md, extracts `## section` headers and `<!-- id: <hash> -->` markers (or whatever existing convention `extract_existing_ids` uses), and renders as: `Section: OPAL Gotchas\n  Known ids: a1b2c3d4, e5f6a7b8, ...\n\nSection: API/GraphQL\n  Known ids: ...`. Bounded to id list (no body text) so it stays sub-2KB regardless of ObserveIE.md growth.
- `client.models.list(limit=1)` auth probe at runner startup before Classifier construction.
- `max_retries=0` to own the retry budget (SDK default 2 × 120s = ~6 min worst-case wall time otherwise).
- Defensive content extraction: `next((b.text for b in response.content if b.type == "text"), "")`.

**Regression test:** `tests/test_classifier_sdk_errors.py` — monkeypatches `anthropic.Anthropic` to raise each of `AuthenticationError`, `RateLimitError`, `APITimeoutError`, `APIConnectionError`, `APIStatusError(500)`. Asserts: exactly one marker emitted per case; structured `failure_reason` ≤200 chars; no argv embedded; YAML round-trips cleanly.

### Bug 3 — Marker sanitation
**Failure mode:** `subprocess.TimeoutExpired.__str__()` embeds full argv into the marker's `failure_reason` field via `failure_reason=str(e)` at `classifier.py:90`. One marker → 100+ KB YAML record. Pending queue effectively dead.

**Fix:**
- New helper `_sanitize(reason: object) -> str` in `pipeline/classifier.py`:
  ```python
  def _sanitize(reason: object) -> str:
      """Return a YAML-safe ≤200-char string suitable for marker fact/excerpt fields."""
      s = str(reason) if not isinstance(reason, str) else reason
      # repr() to escape control chars and embedded quotes
      escaped = repr(s)
      # Strip the outer quotes that repr adds, cap length, collapse newlines
      escaped = escaped.strip("'\"")[:200].replace("\\n", " ")
      return escaped
  ```
- Apply sanitation **at the `build_marker_candidate` boundary**, not at every call site. The current function already builds both `fact` and `excerpt` from `failure_reason`; sanitize once at entry, both fields benefit.
- Spec for `failure_reason` content: `f"{type_short}: {structured detail}"`. Examples:
  - `timeout: 120s exceeded (turn=8421 chars, prompt~4087 tokens)`
  - `key rejected: 401 from API`
  - `cache disabled: prefix below threshold (5 calls × 0 cache reads)`
  - `malformed candidate record: missing field 'title'`
  - `unknown user-record kind: <first 80 chars of content>`
- Hard rule: `str(exception)` is NEVER passed directly to `failure_reason`. Always through structured construction first, then `_sanitize` at marker boundary.

**Regression test:** `tests/test_marker_sanitization.py` — passes mock `subprocess.TimeoutExpired` with 35 KB cmd argv to `build_marker_candidate`; asserts marker `failure_reason`, `fact`, and `excerpt` all ≤200 chars, no embedded newlines, YAML round-trips cleanly via `read_pending`.

### Bug 4 — Session-start review module-import
**Failure mode:** `hooks/session-start-review.sh:39-44` does `sys.path.insert(0, os.environ.get("PWD", "."))` inside an inline `python3 -c '...'` script. PWD after a bash `cd` is shell-internal; even when exported, edge cases (set -uo pipefail without set -e on cd failure; transient PWD inheritance issues on macOS) cause silent failure with `ModuleNotFoundError: No module named 'pipeline'`. Pending-review surface invisibly broken.

**Fix:**
- Replace the inline `python3 -c '...'` block entirely with a new module: `pipeline/render_pending.py`. Module contains the same logic that was inline (read `PENDING_FILE` env → call `read_pending` → format output to stdout).
- `hooks/session-start-review.sh` calls `python3 -m pipeline.render_pending` from `cd "$PLUGIN_ROOT"` working directory. Standard module-import path; no path manipulation inside the script. Same pattern that `stop-hook.sh:204-205` already uses successfully for `python3 -m pipeline.runner`.
- New module wraps `read_pending` in try/except. On YAML parse error, prints to stdout:
  ```
  === OBSERVE LEARNING CAPTURE — RENDER FAILED ===
  <ExceptionClass>: <message>
  Inspect: <PENDING_FILE path>
  Manual recovery: cat $PENDING_FILE | head -100  # then edit by hand
  === END OBSERVE LEARNING CAPTURE ===
  ```
  This block is injected into Claude Code's context by the SessionStart hook, surfacing the failure to the user immediately rather than silently swallowing.

**Regression test:** `tests/test_session_start_review_pythonpath.sh` — invokes the hook with `env -i HOME=$HOME PATH=/usr/bin:/bin bash session-start-review.sh`; asserts either a valid pending render OR a `RENDER FAILED` block on stdout. Also: `tests/test_render_pending_yaml_failure.py` — feeds a deliberately malformed pending YAML to `pipeline.render_pending`; asserts the `RENDER FAILED` block is emitted to stdout.

### Bug 5 — Per-record KeyError surfacing
**Failure mode:** `pipeline/classifier.py:125-134` catches `KeyError` on Haiku records missing the `title` field and silently `continue`s (logs to stderr only). Real captures lost; no marker; no surface.

**Fix:**
- Replace the `print(...stderr); continue` with `result.append(build_marker_candidate(failure_reason=f"malformed candidate record: missing field {e}", session_id=..., cwd=..., captured_at=...))`.
- `continue` to remaining records preserved — one bad record still doesn't block the batch.

**Regression test:** `tests/test_classifier_per_record_marker.py` — feeds Haiku response with two records: one valid, one missing `title`. Asserts: 2 candidates returned (1 valid candidate + 1 marker); marker has `tag: self-error` and `proposed_section: Plugin Self-Errors`.

---

## 8. Marker Contract / Error Handling

**Mantra (preserved from existing spec §9):** every handled error must produce a visible breadcrumb. Log AND surface. Never silent.

**Marker `failure_reason` format (canonical):**
```
{type_short}: {structured detail}
```
`type_short` is a brief failure category (`timeout`, `key rejected`, `cache disabled`, `malformed candidate record`, `unknown user-record kind`, etc.). `structured detail` is human-readable specifics. **Length cap:** 200 chars total (enforced via `_sanitize`). No newlines. No embedded argv.

**Marker emission paths (all five must hold):**
1. **Classifier-internal exceptions** (`AuthenticationError`, `APIError`, `OSError`, `subprocess.SubprocessError`, `RuntimeError`) → marker via `build_marker_candidate` with sanitized `failure_reason`.
2. **Runner outer `except Exception`** → marker via direct `append_candidates` call (cannot use Classifier's marker path since Classifier may not be constructable — e.g., import error).
3. **Cache-disabled detection** (5 calls × 0 cache reads) → one-shot marker via sentinel file `~/.claude/agents/.observe-cache-warned`. Sentinel deleted on first observed `cache_read_input_tokens > 0` (self-healing).
4. **Auth precheck failure** (env var unset; `models.list` raises `AuthenticationError`) → marker via direct `append_candidates`.
5. **Per-record Haiku output failure** (KeyError, ValueError) → marker per-record (Bug 5 fix).
6. **Unknown user-record kind** in `current_logical_turn` walker → marker for drift detection (Bug 1 fix).

**Marker write itself:** rely on existing `stage.py:56-62` PID-fallback path. No additional wrapping (validator confirmed PID-fallback covers disk-full; YAML serialization crash would raise BEFORE `append_candidates` is called, where the runner's outer except catches it and emits a marker via direct append). If PID-fallback ALSO fails, the message ends up in stderr → `$LOG_FILE`. (No additional grep-and-surface mechanism — explicitly out of scope per architect review.)

---

## 9. Testing Strategy

Per project coding standards (CLAUDE.md): **failing test before implementation** for each bug. The plugin has an existing `tests/` directory; new tests follow existing conventions.

**New test files (7 total):**

| File | Bug | Validates |
|------|-----|-----------|
| `tests/test_transcript_logical_turn.py` | 1 | `current_logical_turn` aggregates substantive text across multi-record assistant turn |
| `tests/test_logical_turn_user_prompt_detection.py` | 1 | `_is_real_user_prompt` correctly classifies `/clear`, `/compact`, hook injections vs real prompts; emits marker on unknown patterns |
| `tests/test_classifier_sdk_errors.py` | 2 | All Anthropic SDK exception types produce sanitized markers; YAML round-trips |
| `tests/test_marker_sanitization.py` | 3 | `_sanitize` caps at 200 chars, strips newlines, escapes control chars; oversized exceptions don't bloat YAML |
| `tests/test_session_start_review_pythonpath.sh` | 4 | Hook produces either valid render or `RENDER FAILED` block under stripped env |
| `tests/test_render_pending_yaml_failure.py` | 4 | Malformed pending YAML produces `RENDER FAILED` block on stdout |
| `tests/test_classifier_per_record_marker.py` | 5 | Per-record KeyError emits a marker, doesn't block batch |
| `tests/test_cache_warning_sentinel.py` | (new HIGH from review) | After 5 calls × 0 cache reads, sentinel file created and one-shot marker emitted; sentinel deleted on next cache_read>0 |

**Test conventions** (verified against existing `tests/`):
- Stdlib `unittest` framework — `class XxxTestCase(unittest.TestCase)` with `setUp`/`tearDown` and `mock.patch` for monkeypatching. No `pytest`.
- Anthropic SDK calls monkeypatched via `unittest.mock.patch("anthropic.Anthropic")` — no real network in any test.
- File I/O uses `tempfile.TemporaryDirectory()` (not `tmp_path` — that's pytest). Existing tests in `tests/test_classifier.py` and `tests/test_stage.py` are reference examples.
- Bash tests (`test_stop_hook.sh`, new `test_session_start_review_pythonpath.sh`) follow shell-script-with-exit-code-assertions convention; see existing `test_stop_hook.sh` as reference.
- Test runner: `cd <plugin_root> && python3 -m unittest discover tests` for Python tests; bash tests invoked individually.

**Verification before any "done" claim** (per CLAUDE.md):
1. All 7 new tests green.
2. Full existing suite (`tests/`) green — no regressions.
3. Lint clean — `ruff` / `mypy` per existing project config.
4. Manual end-to-end exercise:
   - `unset ANTHROPIC_API_KEY` → fire substantive Stop → expect "key missing" marker in pending file.
   - Set valid key → fire 5 substantive Stops → grep `~/.claude/logs/observe-learning-capture.log` for cache_read activity; verify pending file has real candidates (not markers).
   - Corrupt `prompts/classifier.md` (truncate to 0) → fire Stop → expect runner outer-catch marker.
   - Corrupt pending YAML (insert garbage) → trigger SessionStart → expect `RENDER FAILED` block in Claude Code context.
   - `/observe-review` slash command surfaces real candidate; `merge_cli --merge <id>` lands it in `ObserveIE.md`.
5. Logs reviewed (`~/.claude/logs/observe-learning-capture.log`) for any new warnings introduced.

---

## 10. Operational Notes for Implementer

**Implementation sequence (per architect review):**
1. **Bug 3 first** (marker sanitation) — without this, debugging Bugs 2 and 5 is impossible because failures still poison the queue.
2. **Bug 5** (per-record marker) — small, low-risk; gives visibility into Haiku output edge cases.
3. **Bug 4** (session-start render module) — unblocks ability to verify all subsequent fixes (you need to see surfaced markers at session start).
4. **Bug 2** (SDK rewrite + auth precheck + cache visibility) — largest change; depends on Bug 3 sanitation existing.
5. **Bug 1** (logical-turn aggregation) — last because end-to-end manual verification of all prior fixes requires substantive turns to actually reach the classifier.

**Slim payload regeneration:** Generate once per Classifier process instance, cache on the instance. Regenerate on `OSError` reading ObserveIE.md but DO NOT crash — emit marker with `failure_reason="known-facts regen failed: <sanitized>"` and skip the call (don't send empty slim payload, which would defeat caching).

**Migration:** No data migration needed. Old pending YAML records parse cleanly under new code (`Candidate.from_yaml_record` tolerates missing optional blocks — verified by validator). Existing pending file (currently empty after marker discard) starts fresh.

**Config-key migration:** `config.json` rename `haiku_model` → `classifier_model`. Read code uses `config.get("classifier_model", config.get("haiku_model", "claude-sonnet-4-5"))` for back-compat. Default value `"claude-sonnet-4-5"` (model alias, no dated suffix).

**Cost projection:** ~5–10¢/day at typical usage (~100 stop-mode calls + ~10 session-end-mode calls per day, Sonnet 4.5 pricing with effective caching). Under any reasonable usage; not material.

**Operational gotchas to remember:**
- The Anthropic SDK's `Anthropic()` constructor is purely lazy; no auth validation happens until first `.create()` call. The auth precheck via `models.list(limit=1)` is what catches the missing/invalid key case before classification work begins.
- Cache TTL is 5 minutes (`ephemeral`); cache benefit only realizes intra-session with rapid turns. Cross-session caching won't fire. This is acceptable — primary win is per-call latency reduction within a working session.
- Concurrent classifier invocations (e.g., user fires multiple Stops via parallel hook setups) all pay full cache-write cost — none can read what others are still writing. Not a concern at typical usage; flag if call rate increases.
- Bash race with concurrent transcript writes (jq parses a file Claude Code is appending to): `_iter_jsonl` already silently skips malformed lines. Tolerable; no marker emission added (per architect review — out of scope).

---

## 11. Open Questions / Follow-ups (deferred)

- Backfill captures from past session transcripts on disk — possible but non-blocking; standalone batch tool.
- Session-end mode also using `current_logical_turn` (per-logical-turn iteration) for symmetry — explicitly deferred per §5 decision.
- Cache TTL of 1h (vs default 5min) at 2× write cost — not yet justified by usage; could add later if cache hit rate proves disappointing.
- Distinguishing Pro/Max subscription billing from API key spend — out of scope; user generates separate API key per Q3 decision.

---

## Appendix A: Validator Findings Folded In

Round-1 + round-2 + final-validator agent reviews surfaced these corrections, all incorporated above:

1. ✅ Bug 1 fix lives in `transcript.py` (not just bash) — `current_logical_turn` is the actual load-bearing fix.
2. ✅ Exception tuple expanded to `anthropic.AuthenticationError` (separate) + `anthropic.APIError` (covers all subclasses).
3. ✅ Runner's outer `except Exception` emits a marker (was: log+swallow).
4. ✅ `max_retries=0` to own retry budget.
5. ✅ Auth precheck via `client.models.list(limit=1)` (not `models.retrieve(alias)` which may 404).
6. ✅ Cache visibility marker after N=5 calls; sentinel-file one-shot; self-healing on cache hit.
7. ✅ Model switched to `claude-sonnet-4-5` (Haiku 4.5's 4096-token cache min would silently no-op).
8. ✅ Prompt restructure: STATIC_TEMPLATE has placeholders REMOVED; per-call values move to user message.
9. ✅ `_sanitize` applied at `build_marker_candidate` boundary; covers `failure_reason`, `fact`, `excerpt`.
10. ✅ Single canonical max length (200 chars); 120-char number dropped.
11. ✅ Bug 4 fix moved out of inline `python3 -c` (where `__file__` is `'<string>'`) into new `pipeline/render_pending.py` module.
12. ✅ Cache-warning sentinel self-healing (delete on cache_read>0).
13. ✅ Drift-detection marker on unknown user-record patterns in `current_logical_turn`.
14. ✅ Config key migration: `haiku_model` → `classifier_model` with back-compat.
15. ✅ `all_assistant_turns` (session-end mode) explicitly left untouched.
16. ✅ Bash jq snippet drafted (semantic spec; final form during impl).
17. ✅ Test coverage expanded from 5 to 7 files (added user-prompt detection + cache-warning sentinel tests).

Explicitly **rejected** ideas (with rationale, all per architect review):
- ❌ Wrapping `append_candidates` in additional try/except — existing PID-fallback covers disk-full; YAML serialization crash caught upstream.
- ❌ `.in-flight-<session>.lock` files — false positives on normal force-quit make this fragile.
- ❌ Marker emission on bash JSONL parse race — `_iter_jsonl` already silently skips malformed lines; sufficient.
- ❌ Sonnet alternative: padding Haiku prompt with filler to reach 4096-token cache min — wastes tokens, obscures diagnostics, fights the platform.

---

## Verification Evidence

**Date:** 2026-05-05
**Verified by:** Chris Milton

| Step | Status | Notes |
|------|--------|-------|
| 16.1 `ANTHROPIC_API_KEY` exported in shell | ✅ | Added to `~/.zshrc` |
| 16.2 Missing-key path emits informative marker | ✅ | `_auth_precheck` returned False; pending file got `[FAILURE] classifier` marker with full remediation hint (mentions `ANTHROPIC_API_KEY` + `~/.zshrc` + restart) |
| 16.3 Happy-path real Sonnet call → real candidate | **deferred** | Decision: defer to organic post-deploy verification. Rationale: Claude Code's process-env was set BEFORE the new `ANTHROPIC_API_KEY` was added, so this session's hook subshells can't see it; restart required. Stronger signal anyway: real Stop event on a real OPAL conversation after reinstall, not synthetic-transcript exercise. The marker contract (every error path emits a marker) is the safety net if any SDK-call regression slipped through unit tests. |
| 16.4 `render_pending` happy path → review block on stdout | ✅ | Valid YAML pending file → `=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ===` block, exit 0 |
| 16.5 Malformed YAML → `RENDER FAILED` block on stdout | ✅ | Binary bytes (`\xff\xfe\xfd...`) → UnicodeDecodeError caught → `RENDER FAILED` block with class+message+recovery hint, exit 0 (NOT silent) |
| 16.6 Log review for new warnings | ✅ | No new errors from test runs; production hook log still shows old plugin behavior (expected — recovery branch not yet deployed) |
| 16.7 Full test suites green | ✅ | 133 Python tests + 7 bash stop-hook + 3 bash session-start subtests, all green |

**Verification verdict:** **5/7 steps PASS**, **1 deferred to organic post-deploy verification** (Step 16.3 — needs Claude Code restart to inherit new env var; better signal comes from real-world Stop events after deploy than from a synthetic-transcript SDK call). The marker contract is the safety net — if any SDK call regression survived unit tests, it'll surface as a marker on first real Stop event after the recovery branch ships.
