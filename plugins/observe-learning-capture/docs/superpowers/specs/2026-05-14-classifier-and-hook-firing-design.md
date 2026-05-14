# 2026-05-14 — Classifier env-strip, hook firing reliability, and skill audits

Status: draft pending implementation
Owner: Chris Milton
Scope: `pipeline/classifier.py`, `hooks/stop-hook.sh`, `commands/observe-capture.md`, `commands/observe-review.md`, new tests
Out of scope: Bug B (Claude Code `WebFetch` upstream leak), removing `ANTHROPIC_API_KEY` from user shell env.

---

## 1. Problem summary

Three distinct but related defects in the `observe-learning-capture` plugin cause the entire learning-capture pipeline to silently no-op across long stretches of real session activity:

1. **Auth leak at the Python subprocess boundary** — `pipeline/classifier.py::_invoke_classifier` invokes `claude -p` without an explicit `env=` kwarg. The subprocess inherits `ANTHROPIC_API_KEY` from the parent process, which takes precedence over the macOS keychain credential in `claude`'s auth-resolution chain. When the API tier has zero credit, every classifier call fails with "Credit balance is too low". The bash `stop-hook.sh` already unsets the env var before fork (line 33, added 2026-05-11), but the `/observe-capture` slash command and tests bypass that mitigation by invoking the runner directly.
2. **Misleading `"(no stderr)"` failure marker** — when `claude -p --output-format json` exits non-zero, error content commonly appears on stdout (structured error envelope) or in both streams. The current `_invoke_classifier` builds the `RuntimeError` message from stderr only, masking the real diagnostic.
3. **Hook fires but bails on a transcript-file-existence race** — `hooks/stop-hook.sh` checks `[[ ! -f "$TRANSCRIPT" ]]` *before* the retry loop. When Claude Code emits the Stop event slightly before flushing the JSONL transcript to disk, the file doesn't exist yet, the hook bails with `"no transcript at transcript_path=... — skip"`, and no retry happens. Log analysis from `~/.claude/logs/observe-learning-capture.log` over the last few weeks shows **92 "no transcript" bailouts vs 54 "prefilter passed" successes** — an ugly miss rate.

Combined impact: cross-customer Observe-platform learnings (the entire raison d'être of this plugin) are not being captured. The user's hard rule from global `CLAUDE.md` — "Observe-platform learnings must propagate to `~/.claude/agents/ObserveIE.md` before task completion" — silently relies on a pipeline that is mostly inert.

---

## 2. Fixes (narrowly scoped)

### 2.1 `pipeline/classifier.py` — strip Anthropic env vars at subprocess boundary

In `_invoke_classifier`, build `subprocess_env` once, strip `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`, and pass it via `env=`:

```python
subprocess_env = os.environ.copy()
for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
    subprocess_env.pop(var, None)

proc = subprocess.run(
    argv,
    input=user_message,
    capture_output=True,
    text=True,
    timeout=_CLAUDE_P_TIMEOUT_SECONDS,
    check=False,
    env=subprocess_env,
)
```

Rationale: defense in depth. The bash hook already does `unset ANTHROPIC_API_KEY`, but the slash command, tests, and any future caller will not. Stripping at the subprocess boundary covers every path.

### 2.2 `pipeline/classifier.py` — surface both stdout and stderr on non-zero exit

Replace the stderr-only error message with one that interleaves both streams when present:

```python
if proc.returncode != 0:
    truncated_stderr = (proc.stderr or "").strip()[:300]
    truncated_stdout = (proc.stdout or "").strip()[:300]
    parts: list[str] = []
    if truncated_stderr:
        parts.append(f"stderr: {truncated_stderr}")
    if truncated_stdout:
        parts.append(f"stdout: {truncated_stdout}")
    diagnostic = " | ".join(parts) or "(no output)"
    raise RuntimeError(f"claude -p exited {proc.returncode}: {diagnostic}")
```

Rationale: under `--output-format json`, `claude -p` writes structured error envelopes to stdout. Stderr-only error messages produce the misleading `"(no stderr)"` marker the user has been seeing.

### 2.3 `pipeline/classifier.py` — update module docstring

The current docstring claims subprocess pivot is sufficient for keychain auth. That's wrong without env-stripping. Add a paragraph that names the env-strip as the actual mechanism.

### 2.4 `hooks/stop-hook.sh` — extend retry loop to cover transcript-file existence

Move the `[[ ! -f "$TRANSCRIPT" ]]` check *inside* the retry loop so that missing-file races resolve on retry, identical to how empty-extraction races already do. Pseudocode:

```bash
RETRY_COUNT=0
TURN_TEXT=""
while (( RETRY_COUNT <= MAX_RETRIES )); do
    if [[ -n "$TRANSCRIPT" && -f "$TRANSCRIPT" ]]; then
        TURN_TEXT=$(extract_turn_text "$TRANSCRIPT")
        [[ -n "$TURN_TEXT" ]] && break
    fi
    (( RETRY_COUNT++ ))
    (( RETRY_COUNT <= MAX_RETRIES )) && sleep "$RETRY_DELAY"
done

if [[ -z "$TURN_TEXT" ]]; then
    log "no transcript or no assistant turn after $RETRY_COUNT retry/ies — skip"
    [[ "${PREFILTER_ONLY:-0}" == "1" ]] && exit 1
    exit 0
fi
```

Rationale: log analysis shows 92 "no transcript" misses recently. Most of those are likely flush races resolvable with the same retry budget already in place for content extraction.

### 2.5 `commands/observe-capture.md` — add `unset ANTHROPIC_API_KEY` and accurate count reporting

The slash command currently invokes `python3 -m pipeline.runner` directly without unsetting the env var. With fix 2.1 in place, the Python boundary catches this, but a belt-and-suspenders unset at the shell level provides observability and parity with the bash hook. Also: compute candidate count as `(post_count − pre_count)` instead of just "read and count".

### 2.6 `commands/observe-review.md` — minor clarification only

Add a note about how to deal with legacy `[FAILURE] classifier` markers (bulk discard via `--discard` per hash).

---

## 3. Tests (TDD — written before any implementation change)

### 3.1 `tests/test_classifier_subprocess.py` — three new tests

- `test_invoke_classifier_strips_anthropic_env_vars` — set both vars in `os.environ` via a context manager, mock `subprocess.run`, assert the `env=` kwarg is a dict missing both vars and still containing `PATH`.
- `test_nonzero_exit_message_includes_stdout` — mock `subprocess.run` returning `returncode=1`, `stdout="Credit balance is too low"`, `stderr=""`, call `_invoke_classifier`, assert the raised `RuntimeError`'s string contains `"Credit balance is too low"`.
- `test_nonzero_exit_message_includes_both_streams_when_present` — both stdout and stderr non-empty, assert both appear in the error string.

### 3.2 `tests/test_hook_transcript_race.sh` (new) — bash integration test

Drive `stop-hook.sh` with `PREFILTER_ONLY=1`:
- Case A: transcript file does not exist at fire time, gets created after ~100ms. With the fix in place, the hook returns 0 (prefilter passed) instead of 1 (skipped).
- Case B: transcript never exists. After `MAX_RETRIES` cycles the hook returns 1 (skipped) — fail-safe behavior preserved.

### 3.3 `tests/test_hook_per_turn.sh` (new) — fire-cadence smoke test

Generate a synthetic transcript with three distinct assistant turns separated by real user prompts. Drive the hook three times with `PREFILTER_ONLY=1`. Confirm each invocation extracts only the latest logical turn (boundary detection works) and that all three pass the prefilter. This proves the Stop hook is designed to capture *each* turn end, not just the last one.

---

## 4. Autoresearch iteration phase

After core fixes land and tests pass:
- Define an eval set under `evals/` of representative assistant turns drawn from recent real sessions (with PII scrubbed).
- Use `autoresearch` (top-level skill) to A/B prompt-template variations and possibly classifier-model selection.
- Keep changes that improve precision/recall without inflating cost; revert regressions via git.
- Time-box to ~30 minutes of agent time; report cost/quality deltas.

---

## 5. Hand-off package shape

Per global `CLAUDE.md` end-of-task hand-off rule:
1. Files changed (one line each).
2. Verification evidence: test names that ran green, lint output, log file snippet showing a real classifier success after fix, screenshot if any UI involved (n/a here).
3. Commit message draft (no `Co-Authored-By` line).
4. PR description draft (will push to `obs-gh-chrismilton/claude-plugins`).
5. Follow-up TODOs: anything noticed during autoresearch that wasn't fixed.

---

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Env stripping breaks tests that *want* the API key passed through | Only `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are stripped; everything else (incl. `PATH`, `HOME`, `CLAUDE_*`) passes through unchanged. Verified by the new test. |
| Retry loop extension adds latency to every Stop hook | Worst case is `MAX_RETRIES * RETRY_DELAY` = 4 × 0.2s = 800ms backgrounded after the user's turn end — the user does not see this wait because the classifier is already backgrounded with `&`. Bounded. |
| Autoresearch eats subscription quota on a failing prompt | Time-boxed and run only after fixes are green; eval set kept small (<20 turns); cost printed at the end of each iteration. |
| GitHub push exposes session-specific data | Spec doc and tests reference scrubbed fixtures only; no real transcript content committed. |

---

## 7. Approval status

User approved the high-level design in conversation 2026-05-14. This document is the durable record. Implementation proceeds via `superpowers:test-driven-development`.
