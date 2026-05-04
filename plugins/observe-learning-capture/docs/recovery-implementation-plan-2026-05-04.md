# observe-learning-capture: Pipeline Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the 5 bugs that have rendered the auto-capture pipeline non-functional across all sessions, per `docs/recovery-design-2026-05-04.md`.

**Architecture:** Replace `claude --print` subprocess with Anthropic Python SDK (already installed); switch model Haiku 4.5 → Sonnet 4.5 to clear cache-minimum threshold; layered cacheable system blocks (static template + slim known-facts); sanitize all marker `failure_reason` at one boundary; restore session-start render via a proper module; aggregate logical turns across multiple JSONL records per Stop event.

**Tech Stack:** Python 3.11 stdlib + `anthropic` v0.97.0 (already installed at `/Users/chmilton/Library/Python/3.11/lib/python/site-packages/`); bash + `jq`; YAML via `pipeline/stage.py` helpers; `unittest` framework (no pytest).

**Spec:** `docs/recovery-design-2026-05-04.md`. Read it first; this plan implements it task-by-task.

**Branch:** `recovery/pipeline-2026-05-04` (already created from `origin/main`; spec already committed at `bbf57e5`).

**Working directory for all commands:** `~/repos/claude-plugins/plugins/observe-learning-capture/` (referenced as `<plugin_root>` below).

---

## File Structure

| Path | Action | Responsibility |
|------|--------|----------------|
| `pipeline/classifier.py` | Modify (large) | SDK rewrite; `_sanitize` helper; layered prompt build; cache visibility; expanded exception taxonomy; per-record marker on KeyError |
| `pipeline/transcript.py` | Modify | Add `current_logical_turn` + `_is_real_user_prompt`. Existing `last_assistant_turn` and `all_assistant_turns` retained. |
| `pipeline/runner.py` | Modify | Auth precheck at startup; swap `last_assistant_turn` → `current_logical_turn`; outer `except Exception` emits marker. |
| `pipeline/render_pending.py` | **Create** | New module for session-start rendering. Replaces inline `python3 -c '...'` in hook. |
| `pipeline/__init__.py` | Touch (already exists) | Just re-export render_pending if needed; verify importable. |
| `hooks/stop-hook.sh` | Modify (lines 81-89) | Rewrite jq prefilter to walk-back logic (mirrors `current_logical_turn`). |
| `hooks/session-start-review.sh` | Modify (lines 38-69) | Drop inline `python3 -c '...'`; call `python3 -m pipeline.render_pending`. |
| `prompts/classifier.md` | Modify | Remove `{{ALREADY_KNOWN}}`, `{{TURN}}`, `{{CWD}}`, `{{CONTEXT_TIMESTAMP}}` placeholders (template becomes static; per-call values move to user message in classifier.py). |
| `config.json` | Modify | Rename `haiku_model` → `classifier_model`; default `"claude-sonnet-4-5"`. |
| `tests/test_marker_sanitization.py` | **Create** | Bug 3 regression test. |
| `tests/test_classifier_per_record_marker.py` | **Create** | Bug 5 regression test. |
| `tests/test_render_pending_yaml_failure.py` | **Create** | Bug 4 regression test (Python side). |
| `tests/test_session_start_review_pythonpath.sh` | **Create** | Bug 4 regression test (bash side). |
| `tests/test_classifier_sdk_errors.py` | **Create** | Bug 2 regression test. |
| `tests/test_cache_warning_sentinel.py` | **Create** | Cache visibility regression test. |
| `tests/test_transcript_logical_turn.py` | **Create** | Bug 1 regression test (walk-back aggregation). |
| `tests/test_logical_turn_user_prompt_detection.py` | **Create** | Bug 1 regression test (real-prompt detection + drift marker). |

---

## Task 0: Setup & baseline verification

**Files:** none modified.

- [ ] **Step 0.1: Verify on the right branch and clean state**

```bash
cd ~/repos/claude-plugins
git status
git branch --show-current
```

Expected: branch is `recovery/pipeline-2026-05-04`; working tree clean.

- [ ] **Step 0.2: Run the existing test suite as a baseline**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest discover tests 2>&1 | tail -20
```

Expected: existing suite passes (some tests may print warnings about subprocess calls — that's fine). Note the test count for later regression comparison. If anything fails here, STOP and investigate before proceeding — we need a green baseline.

- [ ] **Step 0.3: Verify `anthropic` SDK is importable**

```bash
python3 -c "import anthropic; print(f'anthropic v{anthropic.__version__}')"
```

Expected: `anthropic v0.97.0` (or newer).

- [ ] **Step 0.4: Verify Python 3.11 path matches what hooks will use**

```bash
which python3 && python3 -c "import sys; print(sys.executable, sys.version_info[:3])"
```

Expected: `/usr/local/bin/python3` and `(3, 11, X)` where X ≥ 6.

---

## Task 1 — Bug 3: Marker sanitation

**Why first:** without sanitation, every subsequent test failure that triggers a marker poisons the queue and obscures real signal. Visibility-first.

**Files:**
- Create: `tests/test_marker_sanitization.py`
- Modify: `pipeline/classifier.py` (add `_sanitize` helper; route `failure_reason` through it inside `build_marker_candidate`)

- [ ] **Step 1.1: Write the failing test**

Create `tests/test_marker_sanitization.py`:

```python
"""Tests for failure_reason sanitization in build_marker_candidate.

Bug 3: subprocess.TimeoutExpired.__str__() embeds the full argv (including
the rendered prompt). When that string lands in marker fact/excerpt fields,
it bloats the YAML pending file to 100+ KB. Sanitation must cap length and
strip newlines so YAML serialization stays bounded and readable.
"""
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.classifier import build_marker_candidate
from pipeline.stage import append_candidates, read_pending


class TestMarkerSanitization(unittest.TestCase):
    def test_sanitize_caps_length_at_200(self):
        # 35 KB of garbage — the kind of content TimeoutExpired.__str__ embeds
        bloated = "x" * 35000
        marker = build_marker_candidate(
            failure_reason=bloated,
            session_id="test-session",
            cwd="/test",
            captured_at=datetime.now(timezone.utc),
        )
        # fact field is "Classifier failed: <reason>"; ≤ 200 + small prefix overhead
        self.assertLess(len(marker.fact), 250,
                        f"fact too long: {len(marker.fact)} chars")
        # excerpt also embeds reason; same bound
        self.assertLess(len(marker.provenance.excerpt), 250,
                        f"excerpt too long: {len(marker.provenance.excerpt)} chars")

    def test_sanitize_strips_newlines(self):
        multiline = "line1\nline2\nline3\nlots\nof\nlines"
        marker = build_marker_candidate(
            failure_reason=multiline,
            session_id="test-session",
            cwd="/test",
            captured_at=datetime.now(timezone.utc),
        )
        self.assertNotIn("\n", marker.fact[len("Classifier failed: "):])
        self.assertNotIn("\n", marker.provenance.excerpt)

    def test_sanitize_handles_subprocess_timeoutexpired(self):
        # Simulate the actual exception-string format that triggered Bug 3
        exc = subprocess.TimeoutExpired(
            cmd=["claude", "--print", "x" * 30000],
            timeout=60,
        )
        marker = build_marker_candidate(
            failure_reason=str(exc),
            session_id="test-session",
            cwd="/test",
            captured_at=datetime.now(timezone.utc),
        )
        self.assertLess(len(marker.fact), 250)

    def test_sanitized_marker_round_trips_through_yaml(self):
        # End-to-end: write a sanitized marker to pending file, read it back
        bloated = "y" * 35000
        marker = build_marker_candidate(
            failure_reason=bloated,
            session_id="test-session",
            cwd="/test",
            captured_at=datetime.now(timezone.utc),
        )
        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            append_candidates(pending, [marker])
            records = read_pending(pending)
        self.assertEqual(len(records), 1)
        self.assertLess(len(records[0]["fact"]), 250)
```

- [ ] **Step 1.2: Run the test to verify it fails**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_marker_sanitization -v 2>&1 | tail -20
```

Expected: tests FAIL — current `build_marker_candidate` passes `failure_reason` directly with no sanitation, so the 35 KB string lands intact in `fact` and `excerpt`.

- [ ] **Step 1.3: Implement `_sanitize` and route through it**

Edit `pipeline/classifier.py`. First, add a new helper function near the top of the file (after the imports, before the `Classifier` class):

```python
def _sanitize(reason: object) -> str:
    """Return a YAML-safe ≤200-char string suitable for marker fact/excerpt fields.

    Bug 3 fix: subprocess.TimeoutExpired.__str__() embeds the full argv
    including the rendered prompt (often 30+ KB). Without sanitation, that
    blob landed in marker YAML records and bloated the pending queue past
    100 KB per failure. We cap at 200 chars and collapse newlines so the
    YAML file stays bounded and human-readable.
    """
    s = str(reason) if not isinstance(reason, str) else reason
    # repr() escapes control chars and embedded quotes, then strip the
    # outer quotes that repr adds, cap, and collapse remaining newline escapes.
    escaped = repr(s).strip("'\"")[:200].replace("\\n", " ").replace("\n", " ")
    return escaped
```

Then modify `build_marker_candidate` to route `failure_reason` through `_sanitize` BEFORE building the `fact` and `excerpt` strings. Locate the current function (around line 382) and edit it as follows:

```python
def build_marker_candidate(
    failure_reason: str, *,
    session_id: str, cwd: str, captured_at: datetime,
) -> Candidate:
    """Emit a sentinel candidate so failures surface at review time.

    Per spec §9: every handled error must be logged AND surfaced to the
    caller's contract. This marker ensures the human reviewer sees the
    failure at `/observe-review` time rather than having it silently vanish.

    Bug 3 fix: failure_reason is sanitized via _sanitize() before being
    embedded in fact/excerpt fields, capping length at 200 chars and
    stripping newlines. Without this, subprocess.TimeoutExpired.__str__()
    poisoned the pending YAML queue.
    """
    safe_reason = _sanitize(failure_reason)
    return Candidate.create(
        title="[FAILURE] classifier",
        fact=f"Classifier failed: {safe_reason}",
        proposed_section="Plugin Self-Errors",
        confidence="low",
        tags=["self-error"],
        provenance=Provenance(
            session_id=session_id, cwd=cwd,
            captured_at=captured_at,
            excerpt=f"Auto-generated marker. Reason: {safe_reason}",
        ),
        classifier=None,
    )
```

- [ ] **Step 1.4: Run tests to verify they pass**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_marker_sanitization -v 2>&1 | tail -20
```

Expected: all 4 tests PASS.

Also run the existing test suite to make sure nothing regressed:

```bash
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: same number of tests as baseline (Step 0.2) + 4 new passing.

- [ ] **Step 1.5: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/classifier.py \
        plugins/observe-learning-capture/tests/test_marker_sanitization.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
fix(observe-learning-capture): sanitize marker failure_reason (Bug 3)

subprocess.TimeoutExpired.__str__() embeds the full argv (including
the rendered prompt) — without sanitation, that 30+ KB string landed
in marker YAML fact/excerpt fields and bloated the pending queue
past 100 KB per failure marker.

Add _sanitize() helper that caps at 200 chars and collapses newlines.
Route failure_reason through it inside build_marker_candidate so all
marker emission paths benefit at one boundary.

Test: tests/test_marker_sanitization.py covers the TimeoutExpired
case, length capping, newline stripping, and YAML round-trip.
EOF
)"
```

---

## Task 2 — Bug 5: Per-record KeyError → marker

**Why second:** small, low-risk; gives visibility into Haiku output edge cases that current code silently drops.

**Files:**
- Create: `tests/test_classifier_per_record_marker.py`
- Modify: `pipeline/classifier.py` (lines 125-134; replace silent skip with marker emission)

- [ ] **Step 2.1: Write the failing test**

Create `tests/test_classifier_per_record_marker.py`:

```python
"""Tests for per-record marker emission on malformed classifier output.

Bug 5: classifier.py's per-record loop silently dropped Haiku records
missing the 'title' field (caught KeyError, printed to stderr, continued).
Real captures were lost with no marker — invisible to the reviewer.

Fix: each malformed record produces its own marker via
build_marker_candidate, with the rest of the batch processed as before.
"""
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pipeline.classifier import Classifier


class TestPerRecordMarker(unittest.TestCase):
    def setUp(self):
        # Build a minimal Classifier that won't actually call any model
        self.clf = Classifier(
            model="claude-sonnet-4-5",
            prompt_template_path=Path("/nonexistent.md"),
            observeie_md_path=Path("/nonexistent.md"),
            prompt_version="test",
        )

    @mock.patch("pipeline.classifier._invoke_haiku")
    @mock.patch("pipeline.classifier._build_prompt")
    @mock.patch("pipeline.classifier._read_safe", return_value="(empty)")
    def test_one_malformed_record_emits_marker_does_not_block_batch(
        self, _read, _build, _invoke
    ):
        # Haiku response: 1 valid record + 1 missing title
        _invoke.return_value = """\
- title: "OPAL accepts foo"
  fact: |
    OPAL accepts foo as input.
  proposed_section: "OPAL Gotchas"
  confidence: high
  tags: [opal]
- fact: |
    This record is missing title.
  proposed_section: "OPAL Gotchas"
  confidence: high
  tags: [opal]
"""
        _build.return_value = "irrelevant prompt"

        candidates = self.clf.classify(
            turn_text="some turn text " * 20,
            session_id="test-session",
            cwd="/test",
            excerpt="excerpt",
        )

        # Should have 2 candidates: 1 valid + 1 marker
        self.assertEqual(len(candidates), 2)
        titles = [c.title for c in candidates]
        self.assertIn("OPAL accepts foo", titles)
        # Marker has title "[FAILURE] classifier"
        self.assertIn("[FAILURE] classifier", titles)

    @mock.patch("pipeline.classifier._invoke_haiku")
    @mock.patch("pipeline.classifier._build_prompt")
    @mock.patch("pipeline.classifier._read_safe", return_value="(empty)")
    def test_marker_failure_reason_names_missing_field(
        self, _read, _build, _invoke
    ):
        _invoke.return_value = """\
- fact: "no title here"
  proposed_section: "OPAL Gotchas"
  confidence: high
"""
        _build.return_value = "irrelevant"

        candidates = self.clf.classify(
            turn_text="some turn text " * 20,
            session_id="test-session",
            cwd="/test",
            excerpt="excerpt",
        )

        markers = [c for c in candidates if c.title == "[FAILURE] classifier"]
        self.assertEqual(len(markers), 1)
        # Per spec: failure_reason should mention the missing field name
        self.assertIn("title", markers[0].fact)
        self.assertIn("malformed", markers[0].fact.lower())
```

- [ ] **Step 2.2: Run the test to verify it fails**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_classifier_per_record_marker -v 2>&1 | tail -20
```

Expected: tests FAIL — current code silently skips the malformed record so only 1 candidate is returned, not 2.

- [ ] **Step 2.3: Replace silent skip with marker emission**

In `pipeline/classifier.py`, locate the for-loop at lines 117-135 (current `for raw in raw_candidates: ... except (KeyError, ValueError) as e: ... continue`). Replace the `except` block's silent-skip behavior with marker emission. The new loop reads:

```python
        result: List[Candidate] = []
        for raw in raw_candidates:
            try:
                result.append(_raw_to_candidate(
                    raw, session_id=session_id, cwd=cwd,
                    captured_at=captured_at, excerpt=excerpt,
                    model=self.model, prompt_version=self.prompt_version,
                ))
            except (KeyError, ValueError) as e:
                # Bug 5 fix: emit a marker per malformed record so failures
                # surface at /observe-review time. Previous behavior silently
                # dropped the record (logged to stderr only). One bad record
                # still doesn't block the rest of the batch.
                print(
                    f"[observe-learning-capture] classifier.py: malformed "
                    f"candidate record: {e}",
                    file=sys.stderr,
                )
                result.append(build_marker_candidate(
                    failure_reason=f"malformed candidate record: missing field {e}",
                    session_id=session_id, cwd=cwd,
                    captured_at=captured_at,
                ))
                continue
        return result
```

- [ ] **Step 2.4: Run tests to verify**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_classifier_per_record_marker -v 2>&1 | tail -20
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: 2 new tests PASS; existing suite still green.

- [ ] **Step 2.5: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/classifier.py \
        plugins/observe-learning-capture/tests/test_classifier_per_record_marker.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
fix(observe-learning-capture): emit marker on malformed Haiku record (Bug 5)

Previous code silently dropped Haiku records missing the 'title' field
(caught KeyError, printed to stderr, continued). Real captures were
lost with no surface to the reviewer.

Replace silent continue with build_marker_candidate emission. One bad
record still doesn't block the rest of the batch.

Test: tests/test_classifier_per_record_marker.py validates marker
emission and that failure_reason names the missing field.
EOF
)"
```

---

## Task 3 — Bug 4a: Create `pipeline/render_pending.py` module

**Why now:** session-start render needs to work before we can manually verify that subsequent fixes' markers are surfacing. The render module replaces the broken inline `python3 -c '...'` block in the hook.

**Files:**
- Create: `pipeline/render_pending.py`
- Create: `tests/test_render_pending_yaml_failure.py`

- [ ] **Step 3.1: Write the failing test**

Create `tests/test_render_pending_yaml_failure.py`:

```python
"""Tests for pipeline.render_pending — session-start review surface.

Bug 4: hooks/session-start-review.sh used inline `python3 -c '...'` with
sys.path.insert(0, os.environ.get("PWD", ".")). PWD wasn't reliably
exported to the inline Python subprocess, causing ModuleNotFoundError
and silently breaking the pending-review surface.

Fix: replace inline Python with a real module pipeline.render_pending,
called via `python3 -m pipeline.render_pending`. Real __file__ resolves
cleanly; standard module-import path; mockable from tests.

Module must also surface YAML parse failures explicitly (NOT swallow)
so that a poison-marker era pending file produces a visible RENDER
FAILED block instead of silent zero output.
"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


class TestRenderPending(unittest.TestCase):
    def test_empty_pending_file_produces_no_output(self):
        from pipeline import render_pending

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            pending.write_text("")  # empty
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = render_pending.main(pending_path=pending)
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().strip(), "")

    def test_missing_pending_file_produces_no_output(self):
        from pipeline import render_pending

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "does-not-exist.md"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = render_pending.main(pending_path=pending)
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().strip(), "")

    def test_valid_pending_file_renders_review_block(self):
        from pipeline import render_pending

        sample = """\
---
id: abcd1234
title: Test learning
fact: |
  This is a test fact.
proposed_section: OPAL Gotchas
confidence: high
tags: [opal]
provenance:
  session_id: test-session
  cwd: /test/cwd
  captured_at: 2026-05-04T10:00:00+00:00
  excerpt: test excerpt
"""
        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            pending.write_text(sample)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = render_pending.main(pending_path=pending)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("OBSERVE LEARNING CAPTURE", out)
            self.assertIn("PENDING REVIEW", out)
            self.assertIn("Test learning", out)
            self.assertIn("OPAL Gotchas", out)

    def test_malformed_yaml_produces_render_failed_block(self):
        from pipeline import render_pending

        garbage = "this is not valid yaml: [\n  - broken: {{{{"
        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            pending.write_text(garbage)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = render_pending.main(pending_path=pending)
            self.assertEqual(rc, 0)  # never block session
            out = buf.getvalue()
            self.assertIn("RENDER FAILED", out)
            self.assertIn(str(pending), out)  # path must be surfaced
```

- [ ] **Step 3.2: Run the test to verify it fails**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_render_pending_yaml_failure -v 2>&1 | tail -20
```

Expected: tests FAIL with `ModuleNotFoundError: No module named 'pipeline.render_pending'` (the module doesn't exist yet).

- [ ] **Step 3.3: Create the `pipeline/render_pending.py` module**

Create `pipeline/render_pending.py`:

```python
"""Session-start pending-review renderer.

Replaces the inline `python3 -c '...'` block previously embedded in
hooks/session-start-review.sh. Bug 4: that inline approach used
`sys.path.insert(0, os.environ.get("PWD", "."))` which was unreliable
when PWD wasn't exported to the subprocess — silently breaking the
pending-review surface across sessions.

This module is invoked as `python3 -m pipeline.render_pending`. It reads
the pending YAML file, formats a compact review block, and prints to
stdout. Claude Code's SessionStart hook injects this stdout into the
agent's context, where Claude surfaces it to the user on first prompt.

On YAML parse failure, prints a RENDER FAILED block instead of
silently swallowing — per spec §9 mantra "log AND surface — never silent."

Always exits 0: hooks must never block session flow.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from pipeline.stage import read_pending


def main(pending_path: Optional[Path] = None) -> int:
    """Render pending candidates to stdout. Returns 0 always."""
    if pending_path is None:
        # Default location matches config.json's pending_file
        default = os.environ.get(
            "PENDING_FILE_OVERRIDE",
            os.path.expanduser("~/.claude/agents/.observeie-pending.md"),
        )
        pending_path = Path(default)

    if not pending_path.exists():
        return 0  # nothing pending — first run / clean state

    if pending_path.stat().st_size == 0:
        return 0  # empty file

    try:
        records = read_pending(pending_path)
    except Exception as exc:
        # YAML parse failure or any other read-side error.
        # Surface to stdout so Claude Code injects it into context.
        # Per spec §9: log AND surface — never silent.
        print("=== OBSERVE LEARNING CAPTURE — RENDER FAILED ===")
        print(f"{type(exc).__name__}: {exc}")
        print(f"Inspect: {pending_path}")
        print(f"Manual recovery: cat {pending_path} | head -100  # then edit by hand")
        print("=== END OBSERVE LEARNING CAPTURE ===")
        return 0

    if not records:
        return 0

    print("=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ===")
    print(f"{len(records)} candidate(s) pending review from prior sessions:")
    print()
    home = os.path.expanduser("~")
    for i, r in enumerate(records, 1):
        conf = r.get("confidence", "?")
        section = r.get("proposed_section", "?")
        title = r.get("title", "(no title)")
        src = r.get("provenance", r.get("source", {}))
        cwd = src.get("cwd", "?")
        cwd_short = cwd.replace(home, "~") if isinstance(cwd, str) else "?"
        captured_at = src.get("captured_at", "?")
        captured_at_short = captured_at[:10] if isinstance(captured_at, str) else "?"
        print(f"  #{i} [{conf:6}] {section}: {title}")
        print(f"       (from {cwd_short}, {captured_at_short})")
    print()
    print("I should surface these candidates to the user before responding to")
    print("their first prompt. The user may reply: merge all / merge N /")
    print("discard N / edit N / defer.")
    print("=== END OBSERVE LEARNING CAPTURE ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3.4: Run tests to verify**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_render_pending_yaml_failure -v 2>&1 | tail -20
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: 4 new tests PASS; existing suite still green.

Also smoke-test from CLI (matches what the hook will invoke):

```bash
python3 -m pipeline.render_pending
echo "exit=$?"
```

Expected: exit=0; output is either empty (if `~/.claude/agents/.observeie-pending.md` is empty/missing) or a real review block.

- [ ] **Step 3.5: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/render_pending.py \
        plugins/observe-learning-capture/tests/test_render_pending_yaml_failure.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
feat(observe-learning-capture): add render_pending module (Bug 4 part 1)

Extract session-start pending-review rendering from inline `python3 -c`
in hooks/session-start-review.sh into a proper pipeline.render_pending
module. Real __file__ resolves cleanly; standard module-import path;
mockable from tests.

Surfaces YAML parse failures explicitly via a RENDER FAILED block on
stdout (per spec §9: log AND surface — never silent). Previous inline
code swallowed read_pending exceptions via `|| log "..."`.

Hook update follows in next commit (replaces inline block with
`python3 -m pipeline.render_pending`).

Test: tests/test_render_pending_yaml_failure.py covers empty/missing/
valid/malformed pending files.
EOF
)"
```

---

## Task 4 — Bug 4b: Update `hooks/session-start-review.sh` to call the module

**Files:**
- Create: `tests/test_session_start_review_pythonpath.sh`
- Modify: `hooks/session-start-review.sh` (replace lines 38-69 inline `python3 -c '...'` with `python3 -m pipeline.render_pending`)

- [ ] **Step 4.1: Write the failing test**

Create `tests/test_session_start_review_pythonpath.sh` with executable bit:

```bash
#!/usr/bin/env bash
# Bug 4 regression test: session-start-review.sh must produce either
# a valid render OR an explicit RENDER FAILED block — never silent.
#
# Tests with a deliberately stripped environment (env -i) to verify
# the hook works when launched outside an interactive shell context
# (which is how macOS GUI Claude Code launches it).

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
HOOK="$PLUGIN_ROOT/hooks/session-start-review.sh"

# Set up isolated tmp dir for pending file + override env var
TMPDIR_BASE="$(mktemp -d)"
trap "rm -rf $TMPDIR_BASE" EXIT
PENDING="$TMPDIR_BASE/pending.md"

# --- Test 1: empty pending file produces no output ---
> "$PENDING"
OUT=$(env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/local/bin" \
  PENDING_FILE_OVERRIDE="$PENDING" \
  bash "$HOOK" 2>&1)
if [[ -n "$OUT" ]]; then
    echo "FAIL test 1: empty pending should produce no output, got: $OUT"
    exit 1
fi
echo "PASS test 1: empty pending → no output"

# --- Test 2: valid pending file produces review block ---
cat > "$PENDING" <<'YAML'
---
id: abcd1234
title: Test learning from session-start hook
fact: |
  This is a test fact for hook validation.
proposed_section: OPAL Gotchas
confidence: high
tags: [opal]
provenance:
  session_id: test-session
  cwd: /test/cwd
  captured_at: 2026-05-04T10:00:00+00:00
  excerpt: test excerpt
YAML
OUT=$(env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/local/bin" \
  PENDING_FILE_OVERRIDE="$PENDING" \
  bash "$HOOK" 2>&1)
if ! echo "$OUT" | grep -q "OBSERVE LEARNING CAPTURE"; then
    echo "FAIL test 2: valid pending should produce review block, got: $OUT"
    exit 1
fi
if ! echo "$OUT" | grep -q "Test learning from session-start hook"; then
    echo "FAIL test 2: valid pending should include title, got: $OUT"
    exit 1
fi
echo "PASS test 2: valid pending → review block"

# --- Test 3: malformed pending produces RENDER FAILED block ---
cat > "$PENDING" <<'YAML'
this is not valid yaml: [{{{{
  - completely broken
YAML
OUT=$(env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/local/bin" \
  PENDING_FILE_OVERRIDE="$PENDING" \
  bash "$HOOK" 2>&1)
if ! echo "$OUT" | grep -q "RENDER FAILED"; then
    echo "FAIL test 3: malformed pending should produce RENDER FAILED, got: $OUT"
    exit 1
fi
echo "PASS test 3: malformed pending → RENDER FAILED block"

echo "All hook integration tests passed."
exit 0
```

Make it executable:

```bash
chmod +x ~/repos/claude-plugins/plugins/observe-learning-capture/tests/test_session_start_review_pythonpath.sh
```

- [ ] **Step 4.2: Run the test to verify it fails**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
bash tests/test_session_start_review_pythonpath.sh
```

Expected: test 3 (malformed pending) FAILS — current hook swallows read_pending exceptions via `|| log "review render failed"` and produces silent zero output. Test 1 may pass; tests 2 and 3 will fail.

- [ ] **Step 4.3: Update `hooks/session-start-review.sh`**

Replace the inline `python3 -c '...'` block (lines 36-69) with a call to the new module. The full updated `hooks/session-start-review.sh` is:

```bash
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
```

- [ ] **Step 4.4: Run tests to verify**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
bash tests/test_session_start_review_pythonpath.sh
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: bash test prints `All hook integration tests passed.`; Python suite still green.

- [ ] **Step 4.5: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/hooks/session-start-review.sh \
        plugins/observe-learning-capture/tests/test_session_start_review_pythonpath.sh
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
fix(observe-learning-capture): session-start hook calls render module (Bug 4 part 2)

Replace inline `python3 -c '...'` block with `python3 -m pipeline.render_pending`.
Drops fragile sys.path.insert(0, os.environ.get("PWD")) pattern that
silently failed when PWD wasn't reliably exported to the inline
Python subprocess.

Test: tests/test_session_start_review_pythonpath.sh validates hook
under stripped env (env -i) for empty / valid / malformed pending file
cases. RENDER FAILED block now surfaces YAML parse errors instead of
swallowing them via `|| log`.
EOF
)"
```

---

## Task 5 — Config migration: `haiku_model` → `classifier_model`

**Why now:** the SDK rewrite (Tasks 6-11) needs to read the model name from the new config key. Doing the migration first keeps each Task's diff focused.

**Files:**
- Modify: `config.json`
- Modify: `pipeline/runner.py:74` (config-load reading)

- [ ] **Step 5.1: Update `config.json`**

Edit `config.json`. Change the `haiku_model` key to `classifier_model` and update the value to the Sonnet alias:

```json
{
  "destination_file": "~/.claude/agents/ObserveIE.md",
  "pending_file": "~/.claude/agents/.observeie-pending.md",
  "fallback_pending_file": "~/.claude/agents/.observeie-pending.fallback.md",
  "log_file": "~/.claude/logs/observe-learning-capture.log",
  "classifier_model": "claude-sonnet-4-5",
  "prompt_version": "1.0",
  "prefilter": {
    "min_turn_chars": 150,
    "vocabulary_terms": [
      "OPAL", "Observe", "dataset", "datastream", "monitor", "worksheet",
      "dashboard", "accelerat", "bookmark", "transform", "filedrop", "poller",
      "bundle", "pick_col", "make_col", "statsby", "timechart", "deleteDataset",
      "deleteMonitor", "deleteDashboard", "deleteWorksheet", "deletePoller",
      "deleteFiledrop", "deleteFolder", "/v1/meta", "GraphQL", "observeinc"
    ],
    "discovery_verbs": [
      "turns out", "discovered", "it errors", "won't accept",
      "surprisingly", "rejected", "deadlock", "doesn't cascade"
    ]
  },
  "session_end_scan_enabled": true,
  "stop_scan_enabled": true,
  "debug": false
}
```

- [ ] **Step 5.2: Update `pipeline/runner.py` to read the new key with back-compat**

In `pipeline/runner.py`, locate the line that reads `config["haiku_model"]` (around line 92) and change it to:

```python
    classifier = Classifier(
        model=config.get("classifier_model", config.get("haiku_model", "claude-sonnet-4-5")),
        prompt_template_path=plugin_root / "prompts" / "classifier.md",
        observeie_md_path=destination_path,
        prompt_version=config["prompt_version"],
    )
```

The `config.get("classifier_model", config.get("haiku_model", "claude-sonnet-4-5"))` chain provides back-compat for any existing config files that still use `haiku_model` — they'll continue to work without modification.

- [ ] **Step 5.3: Run tests to verify nothing regressed**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: same number of tests as previous baseline; all still green.

- [ ] **Step 5.4: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/config.json \
        plugins/observe-learning-capture/pipeline/runner.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
refactor(observe-learning-capture): rename config key haiku_model -> classifier_model

Spec §5 (cache mitigation decision): switch model from claude-haiku-4-5
to claude-sonnet-4-5 because Haiku 4.5's prompt-cache minimum is 4096
tokens, which would silently no-op our slim payload (~1100 tokens).
Sonnet 4.5's 1024-token minimum lets the slim payload actually cache.

Rename config key for clarity. Reader uses .get() chain with back-compat
fallback so any config files still using haiku_model continue to work.
EOF
)"
```

---

## Task 6 — Bug 2a: Add SDK auth precheck

**Why this slice:** auth must be validated before any classifier code runs. If `ANTHROPIC_API_KEY` is unset (or set-but-invalid), the precheck emits a marker and returns early — no SDK construction, no wasted work.

**Files:**
- Modify: `pipeline/runner.py` (add `_auth_precheck` function called at top of `main()`)
- Create: portion of `tests/test_classifier_sdk_errors.py` covering auth precheck

- [ ] **Step 6.1: Write the failing test**

Create `tests/test_classifier_sdk_errors.py` (this file will grow over Tasks 6-11; start with auth precheck tests):

```python
"""Tests for SDK error handling in classifier + runner.

Bug 2: subprocess.run(['claude', '--print', ...]) replaced by
anthropic.Anthropic().messages.create(...). Auth precheck added at
runner startup: missing ANTHROPIC_API_KEY → marker; key-rejected →
marker; transient API errors → marker.

Per spec §9: every handled error must emit a marker AND log to stderr.
Never silent.
"""
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import anthropic


class TestAuthPrecheck(unittest.TestCase):
    def setUp(self):
        # Save and clear ANTHROPIC_API_KEY for these tests; restore after
        self._saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        if self._saved_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._saved_key

    def test_missing_api_key_emits_marker_and_returns_early(self):
        from pipeline import runner

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            destination = Path(td) / "ObserveIE.md"
            destination.write_text("")
            ok = runner._auth_precheck(
                pending_path=pending,
                session_id="test-session",
                cwd="/test",
            )
            self.assertFalse(ok, "precheck should fail with no API key")
            # Marker should have been written to pending file
            self.assertTrue(pending.exists())
            content = pending.read_text()
            self.assertIn("[FAILURE] classifier", content)
            self.assertIn("ANTHROPIC_API_KEY", content)

    @mock.patch("anthropic.Anthropic")
    def test_invalid_api_key_emits_marker(self, mock_client_cls):
        from pipeline import runner

        # Simulate models.list raising AuthenticationError
        mock_client = mock.Mock()
        # Construct a real-ish AuthenticationError. The actual class signature
        # may evolve; mock as a simple Exception subclass that classifier's
        # except clause will catch.
        mock_client.models.list.side_effect = anthropic.AuthenticationError(
            message="Invalid API key",
            response=mock.Mock(status_code=401),
            body={"error": {"message": "Invalid API key"}},
        )
        mock_client_cls.return_value = mock_client

        os.environ["ANTHROPIC_API_KEY"] = "sk-fake-but-set"

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            ok = runner._auth_precheck(
                pending_path=pending,
                session_id="test-session",
                cwd="/test",
            )
            self.assertFalse(ok)
            self.assertTrue(pending.exists())
            content = pending.read_text()
            self.assertIn("[FAILURE] classifier", content)
            self.assertIn("key rejected", content.lower())

    @mock.patch("anthropic.Anthropic")
    def test_valid_api_key_passes_precheck(self, mock_client_cls):
        from pipeline import runner

        mock_client = mock.Mock()
        mock_client.models.list.return_value = mock.Mock()  # success
        mock_client_cls.return_value = mock_client

        os.environ["ANTHROPIC_API_KEY"] = "sk-fake-but-set"

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            ok = runner._auth_precheck(
                pending_path=pending,
                session_id="test-session",
                cwd="/test",
            )
            self.assertTrue(ok)
            # No marker should be written on success
            if pending.exists():
                self.assertEqual(pending.read_text(), "")
```

- [ ] **Step 6.2: Run the test to verify it fails**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_classifier_sdk_errors -v 2>&1 | tail -20
```

Expected: tests FAIL — `runner._auth_precheck` doesn't exist yet (`AttributeError`).

- [ ] **Step 6.3: Implement `_auth_precheck` in `runner.py`**

In `pipeline/runner.py`, add at the top of the file (after the existing imports):

```python
import anthropic

from pipeline.classifier import build_marker_candidate
from pipeline.stage import append_candidates
```

Then add the `_auth_precheck` function (place it after `_load_config` near the bottom of the file):

```python
def _auth_precheck(
    pending_path: Path,
    session_id: str,
    cwd: str,
) -> bool:
    """Validate ANTHROPIC_API_KEY presence + correctness. Returns True if OK.

    Bug 2 fix part: catches both "key unset" and "key set but invalid"
    failure modes BEFORE any classifier work. On failure, emits a marker
    via direct append_candidates (cannot use Classifier's marker path
    since Classifier construction may fail too).

    Per spec §9 mantra: log AND surface — never silent.
    """
    captured_at = datetime.now(timezone.utc)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        marker = build_marker_candidate(
            failure_reason=(
                "ANTHROPIC_API_KEY not set in hook environment. "
                "Add `export ANTHROPIC_API_KEY=sk-...` to ~/.zshrc and restart Claude Code."
            ),
            session_id=session_id, cwd=cwd, captured_at=captured_at,
        )
        try:
            append_candidates(pending_path, [marker])
        except Exception as exc:
            print(
                f"[observe-learning-capture] runner.py: auth-precheck marker "
                f"write failed: {exc}",
                file=sys.stderr,
            )
        print(
            "[observe-learning-capture] runner.py: ANTHROPIC_API_KEY missing — "
            "skipping classifier; marker emitted",
            file=sys.stderr,
        )
        return False

    # Key present — validate via models.list (free; no token consumption)
    try:
        client = anthropic.Anthropic()
        client.models.list(limit=1)
        return True
    except anthropic.AuthenticationError as exc:
        marker = build_marker_candidate(
            failure_reason=f"key rejected: {getattr(exc, 'status_code', '?')} from API",
            session_id=session_id, cwd=cwd, captured_at=captured_at,
        )
        try:
            append_candidates(pending_path, [marker])
        except Exception:
            pass
        print(
            f"[observe-learning-capture] runner.py: API key rejected: {exc}",
            file=sys.stderr,
        )
        return False
    except anthropic.APIError as exc:
        # Transient connectivity / rate limit at precheck time — emit marker
        # but ALSO return True. Reasoning: the precheck is best-effort; if
        # the API is briefly unavailable we still want the classifier to
        # try its own call (which has its own error handling). Don't block
        # on transient precheck failures.
        marker = build_marker_candidate(
            failure_reason=f"precheck transient: {type(exc).__name__}",
            session_id=session_id, cwd=cwd, captured_at=captured_at,
        )
        try:
            append_candidates(pending_path, [marker])
        except Exception:
            pass
        print(
            f"[observe-learning-capture] runner.py: precheck transient error "
            f"(continuing anyway): {exc}",
            file=sys.stderr,
        )
        return True
```

You'll also need to ensure `from datetime import datetime, timezone` is imported. Add if not already present:

```python
from datetime import datetime, timezone
```

- [ ] **Step 6.4: Run tests to verify**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_classifier_sdk_errors -v 2>&1 | tail -20
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: 3 new tests in `TestAuthPrecheck` PASS; existing suite still green.

- [ ] **Step 6.5: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/runner.py \
        plugins/observe-learning-capture/tests/test_classifier_sdk_errors.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
feat(observe-learning-capture): add SDK auth precheck (Bug 2 part 1)

Validate ANTHROPIC_API_KEY presence and correctness BEFORE any
classifier work begins. Catches both the "key unset" silent no-op
and the "key set but invalid" 401 case.

On failure, emit a marker via direct append_candidates (cannot use
Classifier's marker path since Classifier construction may fail too).
Per spec §9: log AND surface — never silent.

Uses client.models.list(limit=1) as the validation call — free, no
token consumption, stable across SDK versions.

Test: tests/test_classifier_sdk_errors.py:TestAuthPrecheck covers
unset/invalid/valid key cases.
EOF
)"
```

---

## Task 7 — Bug 2b: Refactor `_build_prompt` for layered cache structure

**Why now:** before swapping the SDK call we need the prompt builder to return separate parts (static template, slim known-facts, user message) so the SDK call can put them in the right places (cached system blocks vs per-call user message).

**Files:**
- Modify: `pipeline/classifier.py` (`_build_prompt` returns tuple; add `_generate_slim_known_facts` helper)
- Modify: `prompts/classifier.md` (remove placeholders so it's pure static)
- Add to `tests/test_classifier_sdk_errors.py` (tests for prompt structure)

- [ ] **Step 7.1: Write the failing tests**

Append to `tests/test_classifier_sdk_errors.py`:

```python
class TestPromptStructure(unittest.TestCase):
    """Bug 2 fix: prompt is split into 3 parts to enable cache_control on
    the static template and slim-known-facts blocks while letting per-call
    values (turn, cwd, timestamp) vary in the user message without
    invalidating the cache."""

    def test_build_prompt_returns_three_strings(self):
        from pipeline.classifier import _build_prompt

        tmpl_path = Path(__file__).parent / "fixtures" / "test_classifier_template.md"
        tmpl_path.parent.mkdir(parents=True, exist_ok=True)
        tmpl_path.write_text("Static instruction text without placeholders.")

        result = _build_prompt(
            template_path=tmpl_path,
            turn_text="some turn",
            slim_known_facts="known: a, b, c",
            cwd="/test/cwd",
            captured_at=datetime(2026, 5, 4, 10, 0, 0, tzinfo=timezone.utc),
        )
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        static, slim, user = result
        self.assertIsInstance(static, str)
        self.assertIsInstance(slim, str)
        self.assertIsInstance(user, str)
        self.assertEqual(static, "Static instruction text without placeholders.")
        self.assertEqual(slim, "known: a, b, c")
        self.assertIn("some turn", user)
        self.assertIn("/test/cwd", user)
        self.assertIn("2026-05-04", user)

    def test_static_block_has_no_per_call_placeholders(self):
        # Critical for cache: per-call values in the static block would
        # invalidate cache on every call.
        from pipeline.classifier import _build_prompt

        tmpl_path = Path(__file__).parent / "fixtures" / "test_classifier_template.md"
        static, _slim, _user = _build_prompt(
            template_path=tmpl_path,
            turn_text="distinct turn 1",
            slim_known_facts="known: a",
            cwd="/test/cwd",
            captured_at=datetime(2026, 5, 4, 10, 0, 0, tzinfo=timezone.utc),
        )
        # Same static block regardless of inputs — proves it's truly static
        static2, _, _ = _build_prompt(
            template_path=tmpl_path,
            turn_text="distinct turn 2",
            slim_known_facts="known: b",
            cwd="/different",
            captured_at=datetime(2026, 5, 4, 11, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(static, static2)


class TestSlimKnownFacts(unittest.TestCase):
    def test_generate_slim_known_facts_extracts_section_and_ids(self):
        from pipeline.classifier import _generate_slim_known_facts

        sample_observeie = """\
# ObserveIE Knowledge Base

## OPAL Gotchas

<!-- id: a1b2c3d4 -->
- Fact one about OPAL.

<!-- id: e5f6a7b8 -->
- Fact two about OPAL.

## API/GraphQL

<!-- id: 9988aabb -->
- Fact about API.
"""
        with tempfile.TemporaryDirectory() as td:
            obs = Path(td) / "ObserveIE.md"
            obs.write_text(sample_observeie)
            slim = _generate_slim_known_facts(obs)
        # Section headers appear
        self.assertIn("OPAL Gotchas", slim)
        self.assertIn("API/GraphQL", slim)
        # IDs appear (not full body text)
        self.assertIn("a1b2c3d4", slim)
        self.assertIn("e5f6a7b8", slim)
        self.assertIn("9988aabb", slim)
        # No body text (would defeat the slim purpose)
        self.assertNotIn("Fact one about OPAL", slim)
        # Bounded size — should be much smaller than the input
        self.assertLess(len(slim), len(sample_observeie) // 2)

    def test_generate_slim_known_facts_handles_missing_file(self):
        from pipeline.classifier import _generate_slim_known_facts
        slim = _generate_slim_known_facts(Path("/does/not/exist.md"))
        # Should return a non-empty placeholder string, not raise
        self.assertIsInstance(slim, str)
        self.assertIn("(empty", slim.lower())
```

- [ ] **Step 7.2: Run the tests to verify they fail**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_classifier_sdk_errors.TestPromptStructure tests.test_classifier_sdk_errors.TestSlimKnownFacts -v 2>&1 | tail -20
```

Expected: tests FAIL — `_build_prompt` currently returns one string; `_generate_slim_known_facts` doesn't exist.

- [ ] **Step 7.3: Update `prompts/classifier.md` to be pure static**

Edit `prompts/classifier.md`. Remove the 4 placeholders (`{{ALREADY_KNOWN}}`, `{{TURN}}`, `{{CWD}}`, `{{CONTEXT_TIMESTAMP}}`) and replace them with prose that describes what the classifier should expect to see in the user message. The lines that referenced placeholders become:

Before (current lines around 75-86):
```
# Already known facts (do not re-capture)

{{ALREADY_KNOWN}}

# Turn under review

Captured at: {{CONTEXT_TIMESTAMP}}
Working directory: {{CWD}}

{{TURN}}
```

After (replacement text):
```
# Already known facts (do not re-capture)

The user will provide a slim list of section headers and dedup-key id hashes
already captured. Treat any candidate whose normalized fact would produce
an id already in that list as a duplicate — do not propose it.

# Turn under review

The user message will provide:
- The conversation turn text to analyze
- Working directory at capture time (identifies customer context)
- Capture timestamp

Analyze the turn and emit candidates per the schema below.
```

This makes the file purely static — no per-call substitutions.

- [ ] **Step 7.4: Implement `_build_prompt` and `_generate_slim_known_facts`**

In `pipeline/classifier.py`, replace the existing `_build_prompt` function (around line 254) with:

```python
def _build_prompt(
    template_path: Path,
    turn_text: str,
    slim_known_facts: str,
    cwd: str,
    captured_at: datetime,
) -> tuple[str, str, str]:
    """Render the classifier prompt as a 3-tuple for layered cache structure.

    Returns:
        (static_template, slim_known_facts, user_message)

    The static template (cached system block 1) contains pure instruction
    content with no per-call placeholders. slim_known_facts (cached system
    block 2) is the bounded section-headers + id-list summary of
    ObserveIE.md. user_message wraps the per-call turn / cwd / timestamp.

    Bug 2 fix: previous version inlined ALREADY_KNOWN (30 KB ObserveIE.md)
    plus per-call values into one rendered string, which both bloated the
    prompt and (with the SDK rewrite) would have invalidated cache on
    every call due to per-call placeholders.
    """
    template = template_path.read_text(encoding="utf-8")
    user_message = (
        f"<turn>\n{turn_text}\n</turn>\n"
        f"<cwd>{cwd}</cwd>\n"
        f"<context_timestamp>{captured_at.isoformat()}</context_timestamp>"
    )
    return template, slim_known_facts, user_message


def _generate_slim_known_facts(observeie_md_path: Path) -> str:
    """Render a bounded slim summary of ObserveIE.md for the cached prompt block.

    Format:
        Section: <name>
          Known ids: id1, id2, id3
        Section: <name>
          Known ids: ...

    Bounded to id list (no body text) so the slim block stays sub-2KB
    regardless of ObserveIE.md growth. The deterministic post-classify
    dedupe in runner.py is the actual correctness gate; Haiku/Sonnet just
    needs section + id awareness to avoid obvious recapture attempts.

    On read failure, returns "(empty — ObserveIE.md unreadable)" so the
    classifier still runs (with no known-facts context) rather than
    crashing the pipeline.
    """
    if not observeie_md_path.exists():
        return "(empty — ObserveIE.md does not exist yet)"
    try:
        text = observeie_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"[observe-learning-capture] classifier.py: cannot read "
            f"ObserveIE.md for slim known-facts: {exc}",
            file=sys.stderr,
        )
        return "(empty — ObserveIE.md unreadable)"

    # Walk the file, tracking current section header and collecting ids.
    sections: dict[str, list[str]] = {}
    current_section: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("## "):
            current_section = line[3:].strip()
            sections.setdefault(current_section, [])
        elif current_section is not None:
            # Match lines of the form `<!-- id: abcd1234 -->`
            stripped = line.strip()
            if stripped.startswith("<!-- id:") and stripped.endswith("-->"):
                # Extract the id hash between "id:" and "-->"
                id_part = stripped[len("<!-- id:"):-len("-->")].strip()
                sections[current_section].append(id_part)

    if not sections:
        return "(empty — no sections found in ObserveIE.md)"

    parts = []
    for section, ids in sections.items():
        parts.append(f"Section: {section}")
        if ids:
            parts.append(f"  Known ids: {', '.join(ids)}")
        else:
            parts.append("  Known ids: (none)")
    return "\n".join(parts)
```

- [ ] **Step 7.5: Run tests to verify**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_classifier_sdk_errors.TestPromptStructure tests.test_classifier_sdk_errors.TestSlimKnownFacts -v 2>&1 | tail -20
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: 5 new tests PASS; existing suite still green. Note: existing `TestClassifierEndToEnd` in `test_classifier.py` may need adjustment if it called `_build_prompt` directly with the old single-string return — fix any failures by adapting test expectations to the new tuple return.

- [ ] **Step 7.6: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/classifier.py \
        plugins/observe-learning-capture/prompts/classifier.md \
        plugins/observe-learning-capture/tests/test_classifier_sdk_errors.py \
        plugins/observe-learning-capture/tests/fixtures/
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
refactor(observe-learning-capture): layer prompt for cache (Bug 2 part 2)

Refactor _build_prompt to return a 3-tuple (static_template,
slim_known_facts, user_message) instead of one rendered string. This
sets up the SDK call to put each part in the right place: cached
system blocks for the first two, per-call user message for the third.

Add _generate_slim_known_facts(observeie_md_path) helper that emits a
bounded section-headers + id-list summary (~1KB regardless of
ObserveIE.md size). Replaces the 30 KB ObserveIE.md injection that
caused timeouts in the old subprocess path.

Update prompts/classifier.md to be pure static — placeholders removed.
Per-call values (turn, cwd, timestamp) flow through the user message
instead.

Test: tests/test_classifier_sdk_errors.py adds TestPromptStructure and
TestSlimKnownFacts. Validates: tuple return, static block stability
across calls (cache invariant), id extraction from ObserveIE.md, bounded
size, missing-file handling.
EOF
)"
```

---

## Task 8 — Bug 2c: Replace `_invoke_haiku` with SDK-based `_invoke_classifier`

**Files:**
- Modify: `pipeline/classifier.py` (rename `_invoke_haiku` → `_invoke_classifier`; rewrite using SDK)
- Update: `pipeline/classifier.py` `Classifier.classify` to call new function with tuple-returning `_build_prompt` and slim known-facts
- Add to `tests/test_classifier_sdk_errors.py` (tests for SDK invocation + exception handling)

- [ ] **Step 8.1: Write the failing tests**

Append to `tests/test_classifier_sdk_errors.py`:

```python
class TestSDKInvocation(unittest.TestCase):
    """Bug 2 fix: classifier uses anthropic Python SDK, not subprocess.

    The new _invoke_classifier(static, slim, user, model) returns the
    response's first text-block content. Exception handling expanded to
    cover the anthropic SDK exception hierarchy explicitly.
    """

    @mock.patch("anthropic.Anthropic")
    def test_invoke_classifier_calls_sdk_with_layered_system(self, mock_client_cls):
        from pipeline.classifier import _invoke_classifier

        mock_client = mock.Mock()
        mock_response = mock.Mock()
        text_block = mock.Mock(type="text", text="[]")
        mock_response.content = [text_block]
        mock_response.usage = mock.Mock(
            cache_read_input_tokens=0,
            cache_creation_input_tokens=1500,
            input_tokens=2000,
            output_tokens=10,
        )
        mock_client.messages.create.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result, usage = _invoke_classifier(
            static_template="STATIC TEMPLATE TEXT",
            slim_known_facts="SLIM FACTS",
            user_message="USER MESSAGE",
            model="claude-sonnet-4-5",
        )

        self.assertEqual(result, "[]")
        # Verify SDK was called with layered system blocks + cache_control
        call_kwargs = mock_client.messages.create.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "claude-sonnet-4-5")
        self.assertEqual(call_kwargs["max_retries"], 0)
        system = call_kwargs["system"]
        self.assertEqual(len(system), 2)
        self.assertEqual(system[0]["type"], "text")
        self.assertEqual(system[0]["text"], "STATIC TEMPLATE TEXT")
        self.assertEqual(system[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(system[1]["text"], "SLIM FACTS")
        self.assertEqual(system[1]["cache_control"], {"type": "ephemeral"})
        # User message in messages
        messages = call_kwargs["messages"]
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "USER MESSAGE")

    @mock.patch("anthropic.Anthropic")
    def test_invoke_classifier_extracts_text_defensively(self, mock_client_cls):
        from pipeline.classifier import _invoke_classifier

        # Response with thinking block first, text block second
        mock_client = mock.Mock()
        mock_response = mock.Mock()
        thinking_block = mock.Mock(type="thinking")
        text_block = mock.Mock(type="text", text="real content")
        mock_response.content = [thinking_block, text_block]
        mock_response.usage = mock.Mock(cache_read_input_tokens=0, cache_creation_input_tokens=0)
        mock_client.messages.create.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result, _usage = _invoke_classifier(
            static_template="x", slim_known_facts="x",
            user_message="x", model="claude-sonnet-4-5",
        )
        # Should pick the text block, not the thinking block
        self.assertEqual(result, "real content")


class TestClassifyExceptionHandling(unittest.TestCase):
    """Each anthropic exception type produces exactly one marker per call;
    failure_reason is sanitized; YAML round-trips cleanly."""

    def setUp(self):
        from pipeline.classifier import Classifier
        self.clf = Classifier(
            model="claude-sonnet-4-5",
            prompt_template_path=Path(__file__).parent / "fixtures" / "test_classifier_template.md",
            observeie_md_path=Path("/does/not/exist.md"),
            prompt_version="test",
        )
        # Make sure fixture exists
        self.clf.prompt_template_path.parent.mkdir(parents=True, exist_ok=True)
        self.clf.prompt_template_path.write_text("static template")

    def _run_with_sdk_error(self, sdk_exception):
        with mock.patch("anthropic.Anthropic") as mock_client_cls:
            mock_client = mock.Mock()
            mock_client.messages.create.side_effect = sdk_exception
            mock_client_cls.return_value = mock_client
            return self.clf.classify(
                turn_text="long turn text " * 20,
                session_id="test-session",
                cwd="/test",
                excerpt="x",
            )

    def test_authentication_error_emits_marker(self):
        exc = anthropic.AuthenticationError(
            message="bad key",
            response=mock.Mock(status_code=401),
            body={},
        )
        candidates = self._run_with_sdk_error(exc)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "[FAILURE] classifier")
        self.assertIn("key rejected", candidates[0].fact.lower())

    def test_rate_limit_error_emits_marker(self):
        exc = anthropic.RateLimitError(
            message="rate limited",
            response=mock.Mock(status_code=429),
            body={},
        )
        candidates = self._run_with_sdk_error(exc)
        self.assertEqual(len(candidates), 1)
        self.assertIn("RateLimitError", candidates[0].fact)

    def test_api_timeout_error_emits_marker(self):
        exc = anthropic.APITimeoutError(request=mock.Mock())
        candidates = self._run_with_sdk_error(exc)
        self.assertEqual(len(candidates), 1)
        self.assertIn("APITimeoutError", candidates[0].fact)

    def test_api_connection_error_emits_marker(self):
        exc = anthropic.APIConnectionError(request=mock.Mock())
        candidates = self._run_with_sdk_error(exc)
        self.assertEqual(len(candidates), 1)
        self.assertIn("APIConnectionError", candidates[0].fact)

    def test_marker_failure_reason_is_sanitized(self):
        # Pass a deliberately bloated exception message
        bloated = "x" * 35000
        exc = anthropic.APIError(
            message=bloated,
            request=mock.Mock(),
            body=None,
        )
        candidates = self._run_with_sdk_error(exc)
        self.assertEqual(len(candidates), 1)
        self.assertLess(len(candidates[0].fact), 250,
                        f"fact too long: {len(candidates[0].fact)}")
```

- [ ] **Step 8.2: Run the tests to verify they fail**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_classifier_sdk_errors.TestSDKInvocation tests.test_classifier_sdk_errors.TestClassifyExceptionHandling -v 2>&1 | tail -30
```

Expected: tests FAIL — `_invoke_classifier` doesn't exist; classifier still uses subprocess + old exception tuple.

- [ ] **Step 8.3: Implement `_invoke_classifier` and rewrite `Classifier.classify`**

In `pipeline/classifier.py`:

a) Add the import at top of file:
```python
import anthropic
```

b) Replace `_invoke_haiku` (around lines 283-312) with `_invoke_classifier`:

```python
def _invoke_classifier(
    static_template: str,
    slim_known_facts: str,
    user_message: str,
    model: str,
) -> tuple[str, object]:
    """Call the Anthropic SDK with layered cacheable system blocks.

    Returns (text_output, usage) where text_output is the first text block
    of the response and usage is the response.usage object (for cache
    visibility in callers).

    Bug 2 fix: replaces subprocess.run(['claude','--print',prompt],...).
    Eliminates: recursive Claude-Code-from-inside-Claude-Code invocation,
    60s subprocess ceiling, full ObserveIE.md re-processed every call.

    max_retries=0 so we own the retry budget; SDK's default 2 retries
    with exponential backoff would otherwise compound with timeout=120
    to ~6 min worst-case wall time (hook subshell may be reaped first).

    cache_control: ephemeral on both system blocks. Sonnet 4.5's 1024-token
    cache minimum lets the slim payload (~1KB) actually cache, unlike
    Haiku 4.5's 4096-token min which would silently no-op.
    """
    client = anthropic.Anthropic()  # auto-loads ANTHROPIC_API_KEY
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": static_template,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": slim_known_facts,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": user_message}],
        timeout=120,
        max_retries=0,
    )
    # Defensive content extraction — handles thinking blocks, multi-block responses
    text_output = next(
        (b.text for b in response.content if getattr(b, "type", None) == "text"),
        "",
    )
    return text_output, response.usage
```

c) Rewrite `Classifier.classify` (around lines 39-135) to use the new layered prompt + SDK + expanded exceptions. Replace the entire method:

```python
    def classify(
        self,
        turn_text: str,
        session_id: str,
        cwd: str,
        excerpt: Optional[str] = None,
    ) -> List[Candidate]:
        """Run classifier on turn_text. Returns 0+ candidates.

        Bug 2 fix: SDK-based, layered cacheable prompt.
        Bug 5 fix: per-record errors emit markers, not silent skips.

        On any classifier failure, emit a marker (per spec §9). Errors
        logged to stderr before marker emission for visibility.
        """
        captured_at = datetime.now(timezone.utc)
        excerpt = excerpt or turn_text[:200]

        try:
            slim_known_facts = _generate_slim_known_facts(self.observeie_md_path)
            static_template, slim_block, user_message = _build_prompt(
                template_path=self.prompt_template_path,
                turn_text=turn_text,
                slim_known_facts=slim_known_facts,
                cwd=cwd,
                captured_at=captured_at,
            )
            classifier_output, usage = _invoke_classifier(
                static_template=static_template,
                slim_known_facts=slim_block,
                user_message=user_message,
                model=self.model,
            )
            # Cache visibility check — surface a marker if cache silently no-ops.
            self._maybe_emit_cache_warning(usage, session_id, cwd, captured_at)
        except anthropic.AuthenticationError as exc:
            print(
                f"[observe-learning-capture] classifier.py: API key rejected "
                f"for session={session_id}: {exc}",
                file=sys.stderr,
            )
            return [build_marker_candidate(
                failure_reason=f"key rejected: {getattr(exc, 'status_code', '?')} from API",
                session_id=session_id, cwd=cwd, captured_at=captured_at,
            )]
        except anthropic.APIError as exc:
            print(
                f"[observe-learning-capture] classifier.py: SDK error "
                f"for session={session_id}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return [build_marker_candidate(
                failure_reason=f"{type(exc).__name__}: {exc}",
                session_id=session_id, cwd=cwd, captured_at=captured_at,
            )]
        except (RuntimeError, OSError) as exc:
            # Legacy compatibility — file read errors etc.
            print(
                f"[observe-learning-capture] classifier.py: classifier failed "
                f"for session={session_id}: {exc}",
                file=sys.stderr,
            )
            return [build_marker_candidate(
                failure_reason=str(exc),
                session_id=session_id, cwd=cwd, captured_at=captured_at,
            )]

        raw_candidates = parse_haiku_yaml_output(classifier_output)
        if not raw_candidates and not _is_empty_haiku_response(classifier_output):
            print(
                f"[observe-learning-capture] classifier.py: malformed yaml "
                f"from classifier for session={session_id}",
                file=sys.stderr,
            )
            return [build_marker_candidate(
                failure_reason=f"malformed yaml: {classifier_output[:200]}",
                session_id=session_id, cwd=cwd, captured_at=captured_at,
            )]

        result: List[Candidate] = []
        for raw in raw_candidates:
            try:
                result.append(_raw_to_candidate(
                    raw, session_id=session_id, cwd=cwd,
                    captured_at=captured_at, excerpt=excerpt,
                    model=self.model, prompt_version=self.prompt_version,
                ))
            except (KeyError, ValueError) as e:
                print(
                    f"[observe-learning-capture] classifier.py: malformed "
                    f"candidate record: {e}",
                    file=sys.stderr,
                )
                result.append(build_marker_candidate(
                    failure_reason=f"malformed candidate record: missing field {e}",
                    session_id=session_id, cwd=cwd, captured_at=captured_at,
                ))
                continue
        return result

    def _maybe_emit_cache_warning(self, usage, session_id, cwd, captured_at):
        """Stub for Task 9 cache visibility — implemented next."""
        pass
```

d) The `_invoke_haiku` function and `_read_safe` helper can be deleted (no longer called). Keep them for now if tests reference them; remove in Task 9 cleanup.

- [ ] **Step 8.4: Run tests to verify**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_classifier_sdk_errors -v 2>&1 | tail -30
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: all `TestSDKInvocation` (2) and `TestClassifyExceptionHandling` (5) tests PASS. Existing `test_classifier.py` tests for `_invoke_haiku` may now FAIL — those existing tests need updating to mock the new SDK path or be removed. Update or remove the existing `test_classifier.py` tests that reference `_invoke_haiku` directly:
  - Open `tests/test_classifier.py`, find tests that `@mock.patch("pipeline.classifier._invoke_haiku")`
  - Either update them to mock `_invoke_classifier` instead, or remove them if they're now superseded by the new tests in `test_classifier_sdk_errors.py`.

Re-run after fixing:

```bash
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: full suite green.

- [ ] **Step 8.5: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/classifier.py \
        plugins/observe-learning-capture/tests/test_classifier_sdk_errors.py \
        plugins/observe-learning-capture/tests/test_classifier.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
feat(observe-learning-capture): replace claude --print with SDK (Bug 2 part 3)

Replace subprocess.run(['claude','--print',...]) with
anthropic.Anthropic().messages.create() using layered cacheable system
blocks. Eliminates recursive Claude-Code-from-inside-Claude-Code
invocation hazard and 60s subprocess timeout ceiling that triggered
silent failures in production.

Architecture:
- _invoke_classifier returns (text, usage) tuple
- system=[STATIC_TEMPLATE, SLIM_KNOWN_FACTS] both cache_control:ephemeral
- max_retries=0 to own retry budget (SDK default 2 retries x 120s
  timeout = ~6 min worst case wall time otherwise)
- Defensive content extraction handles thinking blocks
- Expanded exception catch: AuthenticationError, APIError (covers
  RateLimitError, APIConnectionError, APITimeoutError, etc.)
- Each exception type produces exactly one marker per call

Existing test_classifier.py tests that mocked _invoke_haiku updated to
mock the new _invoke_classifier path.

Test: tests/test_classifier_sdk_errors.py:TestSDKInvocation +
TestClassifyExceptionHandling. Cache visibility marker added in next
commit (placeholder _maybe_emit_cache_warning method present).
EOF
)"
```

---

## Task 9 — Bug 2d: Cache visibility sentinel

**Why now:** the SDK call is in place; we need to surface the silent-failure case where the cache silently no-ops (fewer than 4096 tokens, or wrong block ordering, etc.).

**Files:**
- Create: `tests/test_cache_warning_sentinel.py`
- Modify: `pipeline/classifier.py` (implement `_maybe_emit_cache_warning`)

- [ ] **Step 9.1: Write the failing test**

Create `tests/test_cache_warning_sentinel.py`:

```python
"""Tests for cache visibility sentinel.

Per silent-failure-hunter review: if our prompt is below the model's
cache minimum (4096 tokens for Haiku, 1024 for Sonnet), cache_control
markers silently no-op (cache_creation_input_tokens=0, no error). Without
a visibility check, classifier "succeeds" but pays full input cost forever.

Fix: after N=5 calls, if cache_read_input_tokens has been 0 every time,
emit a one-shot marker via sentinel file ~/.claude/agents/.observe-cache-warned.
Self-healing: sentinel deleted on first observed cache_read>0.
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


class TestCacheWarning(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sentinel = Path(self._tmp.name) / ".observe-cache-warned"

    def tearDown(self):
        self._tmp.cleanup()

    def _make_classifier(self):
        from pipeline.classifier import Classifier
        clf = Classifier(
            model="claude-sonnet-4-5",
            prompt_template_path=Path("/nonexistent.md"),
            observeie_md_path=Path("/nonexistent.md"),
            prompt_version="test",
        )
        # Reset call counter
        clf._cache_call_count = 0
        clf._cache_sentinel_path = self.sentinel
        return clf

    def test_no_warning_under_threshold_calls(self):
        from pipeline.stage import append_candidates  # noqa
        clf = self._make_classifier()
        usage = mock.Mock(cache_read_input_tokens=0, cache_creation_input_tokens=0)
        # 4 calls — under N=5 threshold
        for _ in range(4):
            clf._maybe_emit_cache_warning(usage, "s", "/c", datetime.now(timezone.utc))
        self.assertFalse(self.sentinel.exists())

    def test_warning_emitted_at_threshold_when_cache_never_hits(self):
        clf = self._make_classifier()
        usage = mock.Mock(cache_read_input_tokens=0, cache_creation_input_tokens=0)
        # 5 calls with cache_read=0 every time → should fire
        for _ in range(5):
            clf._maybe_emit_cache_warning(usage, "s", "/c", datetime.now(timezone.utc))
        self.assertTrue(self.sentinel.exists(),
                        "sentinel should exist after 5 calls × 0 cache reads")

    def test_warning_not_re_emitted_after_sentinel_exists(self):
        clf = self._make_classifier()
        # Simulate prior warning already fired
        self.sentinel.touch()
        first_mtime = self.sentinel.stat().st_mtime

        usage = mock.Mock(cache_read_input_tokens=0, cache_creation_input_tokens=0)
        for _ in range(10):
            clf._maybe_emit_cache_warning(usage, "s", "/c", datetime.now(timezone.utc))
        # Sentinel should not have been re-touched
        self.assertEqual(self.sentinel.stat().st_mtime, first_mtime)

    def test_sentinel_self_heals_on_cache_hit(self):
        clf = self._make_classifier()
        # Pre-existing sentinel from prior warning
        self.sentinel.touch()
        usage = mock.Mock(cache_read_input_tokens=500, cache_creation_input_tokens=0)
        clf._maybe_emit_cache_warning(usage, "s", "/c", datetime.now(timezone.utc))
        self.assertFalse(self.sentinel.exists(),
                         "sentinel should be deleted on cache_read>0")
```

- [ ] **Step 9.2: Run the test to verify it fails**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_cache_warning_sentinel -v 2>&1 | tail -20
```

Expected: tests FAIL — `_maybe_emit_cache_warning` is currently a `pass`-only stub.

- [ ] **Step 9.3: Implement `_maybe_emit_cache_warning`**

In `pipeline/classifier.py`, replace the `_maybe_emit_cache_warning` stub with the real implementation. Also add cache state to `Classifier`'s `__init__` (or `__post_init__` since it's a `@dataclass`).

First, update the `Classifier` dataclass to include cache-tracking state. Locate the class declaration (around line 21-37) and add `_cache_call_count` and `_cache_sentinel_path` as default-initialized fields. Because it's a `@dataclass`, use `field(default_factory=...)`:

```python
@dataclass
class Classifier:
    """Orchestrates Anthropic SDK invocations to produce Candidate objects.

    Attributes:
        model: Claude model ID (e.g. "claude-sonnet-4-5").
        prompt_template_path: Path to the static classifier prompt template.
        observeie_md_path: Path to ObserveIE.md — slim known-facts derived
            from this so Haiku/Sonnet avoids re-capturing known facts.
        prompt_version: Version label embedded in ClassifierMeta for each
            produced candidate, so prompts can be retro-evaluated later.
    """

    model: str
    prompt_template_path: Path
    observeie_md_path: Path
    prompt_version: str = "1.0"
    _cache_call_count: int = 0
    _cache_sentinel_path: Path = field(default_factory=lambda: Path(
        os.path.expanduser("~/.claude/agents/.observe-cache-warned")
    ))
```

Add `from dataclasses import dataclass, field` and `import os` if not already imported.

Now replace the `_maybe_emit_cache_warning` method:

```python
    def _maybe_emit_cache_warning(
        self,
        usage,
        session_id: str,
        cwd: str,
        captured_at: datetime,
    ) -> None:
        """Surface a marker if prompt cache silently no-ops.

        Per silent-failure-hunter review: if our prompt is below the model's
        cache minimum (1024 tokens for Sonnet 4.5), cache_control markers
        silently no-op — cache_creation_input_tokens=0, no error raised.
        Classifier "succeeds" but pays full input cost forever with no signal.

        Strategy: after 5 calls with cache_read_input_tokens consistently 0,
        emit a one-shot marker via sentinel file. Self-healing: sentinel is
        deleted on first observed cache_read>0 so the warning re-fires if
        the situation regresses.
        """
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

        if cache_read > 0:
            # Caching IS working — heal any prior warning sentinel
            if self._cache_sentinel_path.exists():
                try:
                    self._cache_sentinel_path.unlink()
                except OSError:
                    pass  # best-effort heal
            return

        # Cache not hitting on this call
        self._cache_call_count += 1
        if self._cache_call_count < 5:
            return  # not enough evidence yet

        if self._cache_sentinel_path.exists():
            return  # already warned; don't spam

        # Emit one-shot marker
        from pipeline.stage import append_candidates
        pending_path = Path(os.path.expanduser("~/.claude/agents/.observeie-pending.md"))
        marker = build_marker_candidate(
            failure_reason=(
                f"cache disabled: prefix below threshold "
                f"({self._cache_call_count} calls × 0 cache reads)"
            ),
            session_id=session_id, cwd=cwd, captured_at=captured_at,
        )
        try:
            append_candidates(pending_path, [marker])
            self._cache_sentinel_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_sentinel_path.touch()
        except Exception as exc:
            print(
                f"[observe-learning-capture] classifier.py: cache-warning "
                f"emission failed: {exc}",
                file=sys.stderr,
            )
```

- [ ] **Step 9.4: Run tests to verify**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_cache_warning_sentinel -v 2>&1 | tail -20
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: 4 new tests PASS; existing suite green.

- [ ] **Step 9.5: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/classifier.py \
        plugins/observe-learning-capture/tests/test_cache_warning_sentinel.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
feat(observe-learning-capture): cache visibility sentinel (Bug 2 part 4)

Per silent-failure-hunter review: if our prompt is below the model's
cache minimum, cache_control markers silently no-op
(cache_creation_input_tokens=0, no error). Classifier "succeeds" but
pays full input cost forever with no operational signal.

Add Classifier._maybe_emit_cache_warning that tracks consecutive zero
cache_read counts. After 5 calls × 0 cache reads, emit a one-shot
marker via sentinel file ~/.claude/agents/.observe-cache-warned.
Self-healing: sentinel deleted on first observed cache_read>0 so the
warning re-fires if the situation regresses.

Test: tests/test_cache_warning_sentinel.py covers under-threshold,
at-threshold, no-spam-after-warning, and self-heal cases.
EOF
)"
```

---

## Task 10 — Bug 2e: Wire auth precheck into runner; outer except emits marker

**Files:**
- Modify: `pipeline/runner.py` (call `_auth_precheck` at top of `main`; outer `except Exception` emits marker)

- [ ] **Step 10.1: Write the failing test**

Append to `tests/test_classifier_sdk_errors.py`:

```python
class TestRunnerOuterCatchEmitsMarker(unittest.TestCase):
    """Runner's outer `except Exception` previously logged + returned 0
    silently. Bug 2 fix part: must emit a marker so unexpected failures
    surface at /observe-review."""

    @mock.patch("pipeline.runner.Classifier")
    @mock.patch("pipeline.runner._auth_precheck", return_value=True)
    def test_unexpected_classifier_construction_error_emits_marker(
        self, _precheck, mock_clf_cls
    ):
        from pipeline import runner

        mock_clf_cls.side_effect = RuntimeError("classifier broken in unexpected way")

        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "transcript.jsonl"
            transcript.write_text(
                '{"type":"user","message":{"content":"hello"},"uuid":"u1","timestamp":"2026-05-04T10:00:00Z"}\n'
                '{"type":"assistant","message":{"content":[{"type":"text","text":"reply with substantive content " * 20}]},"uuid":"a1","timestamp":"2026-05-04T10:00:01Z"}\n'
            )
            pending = Path(td) / "pending.md"
            destination = Path(td) / "ObserveIE.md"
            destination.write_text("")

            # Override config paths via env or direct patching
            with mock.patch("pipeline.runner._load_config", return_value={
                "destination_file": str(destination),
                "pending_file": str(pending),
                "classifier_model": "claude-sonnet-4-5",
                "prompt_version": "test",
            }):
                # Run with synthetic argv
                rc = runner.main_with_args(
                    mode="stop",
                    transcript=str(transcript),
                    session_id="test-session",
                    cwd="/test",
                )

        self.assertEqual(rc, 0)
        # Marker should have been written
        self.assertTrue(pending.exists())
        content = pending.read_text()
        self.assertIn("[FAILURE] classifier", content)
```

- [ ] **Step 10.2: Run the test to verify it fails**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_classifier_sdk_errors.TestRunnerOuterCatchEmitsMarker -v 2>&1 | tail -20
```

Expected: tests FAIL — `runner.main_with_args` doesn't exist; outer except still logs+swallows.

- [ ] **Step 10.3: Refactor `runner.main` to expose `main_with_args` and add outer marker emission**

In `pipeline/runner.py`, refactor so the argv-parsing wrapper and the actual logic are separate (so tests can call the logic directly without sys.argv):

```python
def main() -> int:
    """Argv-parsing wrapper. Calls main_with_args with parsed values."""
    p = argparse.ArgumentParser(
        description="observe-learning-capture pipeline runner"
    )
    p.add_argument("--mode", choices=["stop", "session-end"], required=True)
    p.add_argument("--transcript", required=True)
    p.add_argument("--session-id", required=True)
    p.add_argument("--cwd", required=True)
    args = p.parse_args()
    return main_with_args(
        mode=args.mode,
        transcript=args.transcript,
        session_id=args.session_id,
        cwd=args.cwd,
    )


def main_with_args(
    mode: str,
    transcript: str,
    session_id: str,
    cwd: str,
) -> int:
    """Actual runner logic. Returns exit code."""
    try:
        config = _load_config()
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[observe-learning-capture] runner.py: cannot load config: {exc}",
            file=sys.stderr,
        )
        return 1

    destination_path = Path(os.path.expanduser(config["destination_file"]))
    pending_path = Path(os.path.expanduser(config["pending_file"]))
    plugin_root = Path(__file__).parent.parent

    # Bug 2 fix: auth precheck before any classifier work begins.
    # On failure, marker has already been emitted; return 0 cleanly.
    if not _auth_precheck(pending_path, session_id, cwd):
        return 0

    try:
        classifier = Classifier(
            model=config.get("classifier_model", config.get("haiku_model", "claude-sonnet-4-5")),
            prompt_template_path=plugin_root / "prompts" / "classifier.md",
            observeie_md_path=destination_path,
            prompt_version=config["prompt_version"],
        )
        transcript_path = Path(transcript)

        if mode == "stop":
            turn = current_logical_turn(transcript_path)
            if turn is None:
                return 0
            turn_text = turn.text
            excerpt = turn.text[:200]
        else:
            turn_text = "\n\n".join(
                t.text for t in all_assistant_turns(transcript_path)
            )
            if not turn_text:
                return 0
            excerpt = "(full session scan)"

        candidates = classifier.classify(
            turn_text=turn_text,
            session_id=session_id,
            cwd=cwd,
            excerpt=excerpt,
        )

        existing_ids = extract_existing_ids(destination_path)
        pending_ids = {r.get("id") for r in read_pending(pending_path) if r.get("id")}
        already_known_ids = existing_ids | pending_ids
        novel = [c for c in candidates if not is_duplicate(c, already_known_ids)]
        append_candidates(pending_path, novel)
        return 0

    except Exception as exc:
        # Bug 2 fix: outer catch emits marker via direct append_candidates.
        # Previous behavior logged to stderr and returned 0 silently.
        print(
            f"[observe-learning-capture] runner.py: unexpected runner error "
            f"for session={session_id}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        try:
            marker = build_marker_candidate(
                failure_reason=f"runner outer-catch: {type(exc).__name__}: {exc}",
                session_id=session_id, cwd=cwd,
                captured_at=datetime.now(timezone.utc),
            )
            append_candidates(pending_path, [marker])
        except Exception as marker_exc:
            print(
                f"[observe-learning-capture] runner.py: marker emission "
                f"also failed: {marker_exc}",
                file=sys.stderr,
            )
        return 0
```

You'll need to add the imports at the top of `runner.py`:
```python
from pipeline.transcript import current_logical_turn, all_assistant_turns
from pipeline.dedupe import extract_existing_ids, is_duplicate
from pipeline.stage import append_candidates, read_pending
```

(Some of these may already be imported; deduplicate if so.)

**Note:** `current_logical_turn` doesn't exist YET — that's Tasks 12-13. For now, leave the import in place; the tests in this Task don't exercise that code path. Tasks 12-13 will add the function.

Actually, since `current_logical_turn` is called from `main_with_args`, importing it now will cause an `ImportError` until Task 12. Workaround for Task 10: add the import behind a `try`/`except ImportError` guard, OR temporarily keep using `last_assistant_turn` and fix in Task 14. Cleaner: temporarily use `last_assistant_turn` here, add a `# TODO Task 14: swap to current_logical_turn` comment, swap in Task 14.

Replace the line `turn = current_logical_turn(transcript_path)` with:
```python
            turn = last_assistant_turn(transcript_path)  # TODO Task 14: swap to current_logical_turn
```

And import accordingly:
```python
from pipeline.transcript import last_assistant_turn, all_assistant_turns
```

Task 14 will perform the swap.

- [ ] **Step 10.4: Run tests to verify**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_classifier_sdk_errors.TestRunnerOuterCatchEmitsMarker -v 2>&1 | tail -20
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: new test PASSES; full suite green.

- [ ] **Step 10.5: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/runner.py \
        plugins/observe-learning-capture/tests/test_classifier_sdk_errors.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
feat(observe-learning-capture): runner outer catch emits marker (Bug 2 part 5)

Wire _auth_precheck into runner.main_with_args BEFORE Classifier
construction. Refactor main() into argv-parsing wrapper +
main_with_args(mode, transcript, session_id, cwd) so tests can call
the logic directly without sys.argv manipulation.

Bug 2 fix completion: outer except Exception now emits a marker via
direct append_candidates. Previous behavior logged to stderr and
returned 0 silently — no surface to the user. Per spec §9 mantra:
log AND surface — never silent.

current_logical_turn integration deferred to Task 14 (function added
in Tasks 12-13). For now, runner uses last_assistant_turn with TODO
marker for the swap.

Test: TestRunnerOuterCatchEmitsMarker validates that an unexpected
Classifier construction error produces a marker in the pending file.
EOF
)"
```

---

## Task 11 — Bug 2 cleanup: remove unused `_invoke_haiku` and `_read_safe`

**Files:**
- Modify: `pipeline/classifier.py` (remove dead code)

- [ ] **Step 11.1: Remove dead functions**

In `pipeline/classifier.py`, delete:
- `_invoke_haiku` function (no longer called — `_invoke_classifier` replaced it)
- `_read_safe` function (no longer called — `_generate_slim_known_facts` handles its own read)

Also remove the `import subprocess` line at the top if no other code uses it (search the file: `grep subprocess pipeline/classifier.py` — should now return nothing).

- [ ] **Step 11.2: Run full test suite**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: all tests still PASS.

- [ ] **Step 11.3: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/classifier.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
refactor(observe-learning-capture): remove dead _invoke_haiku/_read_safe

Bug 2 cleanup: _invoke_haiku replaced by _invoke_classifier; _read_safe
replaced by _generate_slim_known_facts (which handles its own read
errors). Drop dead functions and the now-unused subprocess import.
EOF
)"
```

---

## Task 12 — Bug 1a: `_is_real_user_prompt` helper in `transcript.py`

**Why now:** prerequisite for `current_logical_turn`. Encodes the rule for distinguishing real user prompts from injected user-records (`/clear`, `/compact`, hook injections).

**Files:**
- Create: `tests/test_logical_turn_user_prompt_detection.py`
- Modify: `pipeline/transcript.py` (add `_is_real_user_prompt` helper)

- [ ] **Step 12.1: Write the failing test**

Create `tests/test_logical_turn_user_prompt_detection.py`:

```python
"""Tests for _is_real_user_prompt — distinguishes real user prompts from
injected/synthetic user records.

Bug 1 fix: the "current logical turn" walker stops at the most recent
real user prompt. Modern Claude Code emits user-typed records for many
non-prompt events: /clear, /compact, hook-injected SessionStart context,
tool_results (which have list content, not string), etc. Treating any
of these as the boundary breaks turn aggregation.

Per code-architect drift-detection note: unknown user-record patterns
should emit a marker so we notice when new injection types ship.
"""
import unittest


class TestIsRealUserPrompt(unittest.TestCase):
    def test_string_user_message_is_real_prompt(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "user", "message": {"content": "Hey, can you help me with X?"}}
        self.assertTrue(_is_real_user_prompt(record))

    def test_list_user_content_is_not_real_prompt(self):
        # tool_result records have list content
        from pipeline.transcript import _is_real_user_prompt
        record = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "result"}]},
        }
        self.assertFalse(_is_real_user_prompt(record))

    def test_clear_command_is_not_real_prompt(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "user", "message": {"content": "/clear"}}
        self.assertFalse(_is_real_user_prompt(record))

    def test_compact_command_is_not_real_prompt(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "user", "message": {"content": "/compact"}}
        self.assertFalse(_is_real_user_prompt(record))

    def test_session_start_injection_is_not_real_prompt(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {
            "type": "user",
            "message": {"content": "=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ==="},
        }
        self.assertFalse(_is_real_user_prompt(record))

    def test_command_name_tag_is_not_real_prompt(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "user", "message": {"content": "<command-name>commit</command-name>"}}
        self.assertFalse(_is_real_user_prompt(record))

    def test_system_reminder_tag_is_not_real_prompt(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "user", "message": {"content": "<system-reminder>Auto mode active</system-reminder>"}}
        self.assertFalse(_is_real_user_prompt(record))

    def test_real_prompt_with_punctuation_is_real(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "user", "message": {"content": "What does deleteDataset() return on cascade failure?"}}
        self.assertTrue(_is_real_user_prompt(record))

    def test_assistant_record_returns_false(self):
        # Defensive: function should never claim an assistant record is a user prompt
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "assistant", "message": {"content": "I am the assistant"}}
        self.assertFalse(_is_real_user_prompt(record))
```

- [ ] **Step 12.2: Run the test to verify it fails**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_logical_turn_user_prompt_detection -v 2>&1 | tail -20
```

Expected: tests FAIL — `_is_real_user_prompt` doesn't exist yet.

- [ ] **Step 12.3: Implement `_is_real_user_prompt`**

In `pipeline/transcript.py`, add the helper near the top of the file (after the existing imports, before `_iter_jsonl`):

```python
# Patterns that mark a user-typed JSONL record as NOT a real user prompt.
# These are slash-commands and synthetic injections that Claude Code writes
# as type=user, content=string but which weren't typed by the human user.
# See pipeline/transcript.py:current_logical_turn for use.
_USER_INJECTION_PREFIXES = (
    "/clear",
    "/compact",
    "/init",
    "/cost",
    "/help",
    "/memory",
    "<command-name>",
    "<system-reminder>",
    "<local-command-stdout>",
    "=== ",  # SessionStart hook injection blocks (e.g., this plugin's own)
)


def _is_real_user_prompt(record: dict) -> bool:
    """Return True iff this JSONL record represents a real user-typed prompt.

    Bug 1 fix: the "current logical turn" walker uses this to know when
    to stop walking back through the transcript. Modern Claude Code emits
    user-typed records for many non-prompt events: tool_results (list
    content), slash-commands like /clear and /compact (string content,
    but not really a prompt), and hook-injected synthetic blocks (this
    plugin's own SessionStart review block, for instance).

    Returns False for:
    - records where type != "user"
    - records whose content is a list (tool_results)
    - records whose string content starts with a known injection prefix

    Per code-architect drift-detection note: callers should consider
    emitting a marker when this returns False on a string-content user
    record that doesn't match any known prefix — that signals a new
    injection type has shipped that we don't recognize yet.
    """
    if record.get("type") != "user":
        return False
    content = record.get("message", {}).get("content")
    if not isinstance(content, str):
        return False  # tool_results and other list-content records
    stripped = content.lstrip()
    for prefix in _USER_INJECTION_PREFIXES:
        if stripped.startswith(prefix):
            return False
    return True
```

- [ ] **Step 12.4: Run tests to verify**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_logical_turn_user_prompt_detection -v 2>&1 | tail -20
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: 9 new tests PASS; full suite green.

- [ ] **Step 12.5: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/transcript.py \
        plugins/observe-learning-capture/tests/test_logical_turn_user_prompt_detection.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
feat(observe-learning-capture): add _is_real_user_prompt helper (Bug 1 part 1)

Distinguish real user-typed prompts from synthetic user-typed records
(slash commands like /clear, hook-injected SessionStart blocks,
tool_result list-content records). Used by next commit's
current_logical_turn walker to know when to stop walking back through
the transcript.

Test: tests/test_logical_turn_user_prompt_detection.py covers all known
injection prefixes plus the defensive cases (assistant records, list
content, real prompts with punctuation).
EOF
)"
```

---

## Task 13 — Bug 1b: `current_logical_turn` walker

**Files:**
- Create: `tests/test_transcript_logical_turn.py`
- Modify: `pipeline/transcript.py` (add `current_logical_turn`)

- [ ] **Step 13.1: Write the failing test**

Create `tests/test_transcript_logical_turn.py`:

```python
"""Tests for current_logical_turn — Bug 1 walker.

Bug 1: prior code (last_assistant_turn) returned only the LAST single
assistant JSONL record. Modern Claude Code emits ~6 records per logical
turn (interleaved text/thinking/tool_use); the last one is often a
tool_use chunk (empty after text-filter) or a 7-char ack like "Saved.".

Fix: walk backward through assistant records, collecting .text from each,
stopping at the first real user prompt. The result is the substantive
content of the current logical turn.
"""
import json
import tempfile
import unittest
from pathlib import Path


def _write_transcript(td: Path, records: list[dict]) -> Path:
    p = td / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _user(text: str) -> dict:
    return {"type": "user", "message": {"content": text}, "uuid": "u", "timestamp": "2026-05-04T10:00:00Z"}


def _user_tool_result(text: str) -> dict:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": text}]},
        "uuid": "u", "timestamp": "2026-05-04T10:00:00Z",
    }


def _assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
        "uuid": "a", "timestamp": "2026-05-04T10:00:00Z",
    }


def _assistant_tool_use() -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}}]},
        "uuid": "a", "timestamp": "2026-05-04T10:00:00Z",
    }


def _assistant_thinking(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": text}]},
        "uuid": "a", "timestamp": "2026-05-04T10:00:00Z",
    }


class TestCurrentLogicalTurn(unittest.TestCase):
    def test_aggregates_multiple_assistant_records_per_logical_turn(self):
        from pipeline.transcript import current_logical_turn
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            transcript = _write_transcript(td, [
                _user("Real user prompt about OPAL"),
                _assistant_thinking("Let me think about this"),
                _assistant_text("First substantive paragraph about OPAL behavior"),
                _assistant_tool_use(),
                _user_tool_result("ls output"),
                _assistant_text("Second substantive paragraph after tool call"),
                _assistant_tool_use(),
                _assistant_text("Saved."),  # short trailing ack
            ])
            turn = current_logical_turn(transcript)
            self.assertIsNotNone(turn)
            # All three text blocks should be present (substantive + Saved.)
            self.assertIn("First substantive paragraph", turn.text)
            self.assertIn("Second substantive paragraph", turn.text)
            self.assertIn("Saved.", turn.text)
            # Should NOT include the previous user prompt
            self.assertNotIn("Real user prompt", turn.text)

    def test_stops_at_real_user_prompt_does_not_aggregate_prior_turn(self):
        from pipeline.transcript import current_logical_turn
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            transcript = _write_transcript(td, [
                _user("First prompt"),
                _assistant_text("First answer"),
                _user("Second prompt"),
                _assistant_text("Second answer"),
            ])
            turn = current_logical_turn(transcript)
            self.assertIsNotNone(turn)
            self.assertIn("Second answer", turn.text)
            self.assertNotIn("First answer", turn.text)
            self.assertNotIn("First prompt", turn.text)

    def test_walks_through_tool_result_user_records(self):
        # tool_result records should NOT stop the walk-back
        from pipeline.transcript import current_logical_turn
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            transcript = _write_transcript(td, [
                _user("Real prompt"),
                _assistant_text("Before tool"),
                _assistant_tool_use(),
                _user_tool_result("tool returned this"),
                _assistant_text("After tool"),
            ])
            turn = current_logical_turn(transcript)
            self.assertIsNotNone(turn)
            self.assertIn("Before tool", turn.text)
            self.assertIn("After tool", turn.text)

    def test_walks_through_clear_command_records(self):
        # /clear at the boundary of a logical turn shouldn't get aggregated
        from pipeline.transcript import current_logical_turn
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            transcript = _write_transcript(td, [
                _user("Old prompt before /clear"),
                _assistant_text("Old assistant text"),
                _user("/clear"),  # injected, should be skipped
                _user("New real prompt"),
                _assistant_text("New assistant text"),
            ])
            turn = current_logical_turn(transcript)
            self.assertIsNotNone(turn)
            self.assertIn("New assistant text", turn.text)
            self.assertNotIn("Old assistant text", turn.text)

    def test_returns_none_when_no_assistant_text_in_turn(self):
        from pipeline.transcript import current_logical_turn
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            transcript = _write_transcript(td, [
                _user("Real prompt"),
                _assistant_tool_use(),  # only tool calls, no text
            ])
            turn = current_logical_turn(transcript)
            self.assertIsNone(turn)

    def test_returns_none_for_missing_file(self):
        from pipeline.transcript import current_logical_turn
        turn = current_logical_turn(Path("/does/not/exist.jsonl"))
        self.assertIsNone(turn)
```

- [ ] **Step 13.2: Run the test to verify it fails**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_transcript_logical_turn -v 2>&1 | tail -20
```

Expected: tests FAIL — `current_logical_turn` doesn't exist.

- [ ] **Step 13.3: Implement `current_logical_turn`**

In `pipeline/transcript.py`, add the function after `last_assistant_turn`:

```python
def current_logical_turn(path: Path) -> Optional[Turn]:
    """Return the substantive content of the most recent logical turn.

    Bug 1 fix: prior code (last_assistant_turn) returned only the LAST
    single assistant JSONL record. Modern Claude Code emits ~6 records
    per logical turn (interleaved text/thinking/tool_use); the last one
    is often a tool_use chunk (empty after text-filter) or a 7-char ack
    like "Saved." that fails the prefilter's 150-char gate.

    This function walks the records list backward, collecting text from
    each consecutive assistant record. It stops at the first record where
    _is_real_user_prompt returns True (a real user prompt — not a slash
    command, hook injection, or tool_result). The collected text is then
    re-reversed (so it reads chronologically) and joined.

    Returns None if no assistant text was collected (transcript missing,
    empty, or the current turn has no text-bearing records).
    """
    records = list(_iter_jsonl(path))
    if not records:
        return None

    collected_texts: List[str] = []
    last_assistant_record: Optional[dict] = None
    for record in reversed(records):
        if record.get("type") == "assistant":
            text = _extract_text(record.get("message", {}).get("content"))
            if text:
                collected_texts.append(text)
                if last_assistant_record is None:
                    last_assistant_record = record
            continue
        if _is_real_user_prompt(record):
            break  # boundary of the current logical turn
        # Otherwise: tool_result records, slash commands, hook injections —
        # walk through them without collecting and without stopping.

    if not collected_texts:
        return None

    # Re-reverse so the joined text reads chronologically.
    chronological = list(reversed(collected_texts))
    full_text = "\n".join(chronological)
    return Turn(
        role="assistant",
        text=full_text,
        uuid=(last_assistant_record or {}).get("uuid", ""),
        timestamp=(last_assistant_record or {}).get("timestamp", ""),
    )
```

- [ ] **Step 13.4: Run tests to verify**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_transcript_logical_turn -v 2>&1 | tail -20
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: 6 new tests PASS; full suite green.

- [ ] **Step 13.5: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/transcript.py \
        plugins/observe-learning-capture/tests/test_transcript_logical_turn.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
feat(observe-learning-capture): add current_logical_turn walker (Bug 1 part 2)

Walk backward through transcript records collecting assistant .text
from each consecutive assistant record. Stop at the first real user
prompt (per _is_real_user_prompt). Re-reverse to chronological order
and join.

Production telemetry showed ~83% of Stop events logged "no assistant
turn extracted" because last_assistant_turn returned only the LAST
single JSONL record — usually a tool_use chunk (empty after
text-filter) or a 7-char ack ("Saved.") that failed the 150-char
prefilter gate. Aggregating the full logical turn is the actual fix.

Test: tests/test_transcript_logical_turn.py covers multi-record
aggregation, prior-turn boundary, tool_result walk-through, /clear
walk-through, no-text-in-turn, and missing-file cases.
EOF
)"
```

---

## Task 14 — Bug 1c: Swap callsite in `runner.py`

**Files:**
- Modify: `pipeline/runner.py` (replace `last_assistant_turn` with `current_logical_turn` for stop mode)

- [ ] **Step 14.1: Update the import and the callsite**

In `pipeline/runner.py`:

a) Update the import:
```python
from pipeline.transcript import current_logical_turn, all_assistant_turns
```
(Drop `last_assistant_turn` if no longer referenced.)

b) Replace the line marked `# TODO Task 14: swap to current_logical_turn`:
```python
            turn = current_logical_turn(transcript_path)
```

- [ ] **Step 14.2: Run full test suite to verify**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: all tests still PASS.

- [ ] **Step 14.3: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/runner.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
fix(observe-learning-capture): runner uses current_logical_turn (Bug 1 part 3)

Swap last_assistant_turn -> current_logical_turn in runner.main_with_args
for stop mode. session-end mode continues to use all_assistant_turns
per spec §5 decision (full-session retrospective; per-record concat
acceptable).

Completes the Bug 1 Python-side fix. Bash jq prefilter mirror in next
commit.
EOF
)"
```

---

## Task 15 — Bug 1d: Bash jq prefilter walk-back mirror

**Files:**
- Modify: `hooks/stop-hook.sh` (lines 81-89; rewrite jq filter)

- [ ] **Step 15.1: Write a manual smoke-test transcript**

Before editing the hook, prepare a synthetic transcript file we can use to verify the new bash logic (we'll use it via PREFILTER_ONLY=1):

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
mkdir -p /tmp/observe-prefilter-test
cat > /tmp/observe-prefilter-test/transcript.jsonl <<'JSONL'
{"type":"user","message":{"content":"Real user prompt about OPAL"},"uuid":"u1","timestamp":"2026-05-04T10:00:00Z"}
{"type":"assistant","message":{"content":[{"type":"text","text":"This is a long substantive answer about OPAL behavior that should clear the 150 char Gate 1 threshold easily — discovered that OPAL rejects 7d, must use 168h instead. Confirmed across multiple Observe tenants."}]},"uuid":"a1","timestamp":"2026-05-04T10:00:01Z"}
{"type":"assistant","message":{"content":[{"type":"tool_use","id":"tu1","name":"Bash","input":{"command":"ls"}}]},"uuid":"a2","timestamp":"2026-05-04T10:00:02Z"}
{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"tu1","content":"output"}]},"uuid":"u2","timestamp":"2026-05-04T10:00:03Z"}
{"type":"assistant","message":{"content":[{"type":"text","text":"Saved."}]},"uuid":"a3","timestamp":"2026-05-04T10:00:04Z"}
JSONL
```

Verify CURRENT bash behavior fails on this transcript (only sees "Saved.", under 150 chars):

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
PREFILTER_ONLY=1 CLAUDE_TRANSCRIPT_PATH=/tmp/observe-prefilter-test/transcript.jsonl \
    bash hooks/stop-hook.sh
echo "exit=$?"
```

Expected (BEFORE fix): exit=1 (prefilter fails because TURN_TEXT="Saved." is 6 chars, under the 150-char Gate 1 threshold).

- [ ] **Step 15.2: Rewrite the bash jq filter for walk-back**

Edit `hooks/stop-hook.sh`. Replace the jq block at lines 81-89 (the one starting with `TURN_TEXT=$(jq -rsc '`) with a walk-back equivalent. The entire section becomes:

```bash
# ---------------------------------------------------------------------------
# Extract "current logical turn" via reverse jq walk.
#
# Bug 1 fix: previous version `[.[] | select(.type=="assistant") | .message.content
# | (string-or-text-blocks)][-1]` returned only the LAST single JSONL record.
# Modern Claude Code emits ~6 records per logical turn (interleaved
# text/thinking/tool_use). The last one is usually a tool_use chunk (empty
# after text-filter) or a tiny ack like "Saved." that fails Gate 1.
#
# Walk backward through assistant records, collecting `text` blocks. Stop
# at the first user record whose .message.content is a string AND does not
# match a known injection prefix (/clear, /compact, hook-injected blocks).
# tool_result records (list content) are walked through without stopping.
#
# This mirrors pipeline/transcript.py:current_logical_turn for prefilter
# consistency. The Python pipeline is the load-bearing fix; this filter
# just ensures the prefilter stops false-rejecting substantive turns.
# ---------------------------------------------------------------------------
TURN_TEXT=$(jq -rsc '
    def is_real_user_prompt:
        .type == "user"
        and (.message.content | type) == "string"
        and (
            .message.content
            | test("^\\s*(/clear|/compact|/init|/cost|/help|/memory|<command-name>|<system-reminder>|<local-command-stdout>|=== )")
            | not
        );
    def assistant_text:
        .message.content
        | (if type == "string" then [.]
           else map(select(.type == "text") | .text)
           end)
        | join("\n");
    [.[]] | reverse |
    reduce .[] as $r (
        {collected: [], stop: false};
        if .stop then .
        elif $r | is_real_user_prompt then .stop = true
        elif $r.type == "assistant" then
            .collected += [$r | assistant_text]
        else .
        end
    )
    | .collected | reverse | map(select(. != "")) | join("\n")
' "$TRANSCRIPT" 2>/dev/null) || TURN_TEXT=""
```

- [ ] **Step 15.3: Verify the new bash logic on the smoke-test transcript**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
PREFILTER_ONLY=1 CLAUDE_TRANSCRIPT_PATH=/tmp/observe-prefilter-test/transcript.jsonl \
    bash hooks/stop-hook.sh
echo "exit=$?"
```

Expected (AFTER fix): exit=0 (prefilter passes because the aggregated turn includes the long substantive answer + "Saved." → well over 150 chars, hits OPAL vocab term, hits "discovered" verb).

Also verify the other two existing bash tests still work:

```bash
bash tests/test_stop_hook.sh
bash tests/test_session_start_review_pythonpath.sh
```

Expected: both still PASS.

Run the full Python suite too:

```bash
python3 -m unittest discover tests 2>&1 | tail -5
```

Expected: all tests PASS.

- [ ] **Step 15.4: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/hooks/stop-hook.sh
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "$(cat <<'EOF'
fix(observe-learning-capture): bash prefilter walk-back mirror (Bug 1 part 4)

Rewrite the jq filter at hooks/stop-hook.sh:81-89 to mirror the Python
current_logical_turn walker. Walks backward through assistant records
collecting text blocks; stops at the first real user prompt (excludes
slash commands, hook injections, tool_result records).

Smoke-tested with synthetic 5-record transcript ending in "Saved." ack.
Old filter: exit=1 (only saw 6-char ack, failed 150-char Gate 1).
New filter: exit=0 (aggregated 200+ char substantive answer, all 3
prefilter gates pass cleanly).

Completes Bug 1 fix. Both Python (load-bearing) and bash (prefilter
consistency) paths now agree on logical-turn semantics.
EOF
)"
```

---

## Task 16 — Manual end-to-end verification

**No new files; this is the verification gate from spec §9.**

Per CLAUDE.md verification rule: tests + lint + manual exercise + logs review BEFORE claiming done.

- [ ] **Step 16.1: Verify `ANTHROPIC_API_KEY` is exported**

```bash
[ -n "${ANTHROPIC_API_KEY:-}" ] && echo "SET (len=${#ANTHROPIC_API_KEY})" || echo "UNSET"
```

If UNSET: stop. Generate an API key at https://console.anthropic.com/settings/keys, then add to `~/.zshrc`:
```bash
echo 'export ANTHROPIC_API_KEY="sk-..."' >> ~/.zshrc
source ~/.zshrc
```

Expected after fix: `SET (len=...)` where len ≥ 50.

- [ ] **Step 16.2: Test missing-key path produces marker**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
unset ANTHROPIC_API_KEY  # subshell only — won't affect parent zsh
# Use a temp pending file
PENDING_TMP=$(mktemp)
trap "rm -f $PENDING_TMP" EXIT

# Build a synthetic transcript with substantive Observe content
TRANSCRIPT_TMP=$(mktemp)
cat > "$TRANSCRIPT_TMP" <<'JSONL'
{"type":"user","message":{"content":"Real user prompt about OPAL deleteDataset behavior"},"uuid":"u1","timestamp":"2026-05-04T10:00:00Z"}
{"type":"assistant","message":{"content":[{"type":"text","text":"Discovered that OPAL deleteDataset returns 200 even when no dataset matches the id, but with cascade=false it errors with HTTP 400 if any monitor depends on it. Confirmed across multiple Observe tenants — this is undocumented behavior."}]},"uuid":"a1","timestamp":"2026-05-04T10:00:01Z"}
JSONL

# Override config pending file path via a wrapper
PYTHONPATH=. python3 -c "
import os, sys
sys.path.insert(0, '.')
os.environ.pop('ANTHROPIC_API_KEY', None)
from pipeline.runner import _auth_precheck
from pathlib import Path
ok = _auth_precheck(Path('$PENDING_TMP'), 'manual-test', '/test/cwd')
print(f'precheck ok={ok}')
print(f'pending file content:')
print(open('$PENDING_TMP').read())
"
```

Expected: `precheck ok=False`; pending file contains `[FAILURE] classifier` marker with `failure_reason` mentioning `ANTHROPIC_API_KEY not set`.

- [ ] **Step 16.3: Test happy path with valid key produces real candidate**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
PENDING_TMP=$(mktemp)
DEST_TMP=$(mktemp)
trap "rm -f $PENDING_TMP $DEST_TMP" EXIT

TRANSCRIPT_TMP=$(mktemp)
cat > "$TRANSCRIPT_TMP" <<'JSONL'
{"type":"user","message":{"content":"What does OPAL deleteDataset do on cascade?"},"uuid":"u1","timestamp":"2026-05-04T10:00:00Z"}
{"type":"assistant","message":{"content":[{"type":"text","text":"Discovered that OPAL deleteDataset returns 200 even when no dataset matches the id, but with cascade=false it errors with HTTP 400 if any monitor depends on it. Confirmed across multiple Observe tenants — this is undocumented behavior."}]},"uuid":"a1","timestamp":"2026-05-04T10:00:01Z"}
JSONL

# Run the runner with paths overridden via temp config
CONFIG_OVERRIDE=$(cat <<EOF
{
  "destination_file": "$DEST_TMP",
  "pending_file": "$PENDING_TMP",
  "fallback_pending_file": "/tmp/fallback.md",
  "log_file": "/tmp/observe-test.log",
  "classifier_model": "claude-sonnet-4-5",
  "prompt_version": "1.0",
  "session_end_scan_enabled": true,
  "stop_scan_enabled": true,
  "debug": true
}
EOF
)

# Patch the load via a quick Python wrapper
python3 -c "
import sys, json
sys.path.insert(0, '.')
from unittest.mock import patch
from pipeline import runner

config = $CONFIG_OVERRIDE

with patch('pipeline.runner._load_config', return_value=config):
    rc = runner.main_with_args(
        mode='stop',
        transcript='$TRANSCRIPT_TMP',
        session_id='manual-test',
        cwd='/test/cwd',
    )
    print(f'rc={rc}')
print('--- pending file ---')
print(open('$PENDING_TMP').read())
"
```

Expected: rc=0; pending file contains a real candidate (not a marker) about OPAL deleteDataset cascade behavior. Title and fact look reasonable.

- [ ] **Step 16.4: Test render_pending hook surfaces the candidate**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
PENDING_FILE_OVERRIDE="$PENDING_TMP" python3 -m pipeline.render_pending
```

Expected: prints a `=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ===` block with the candidate's title and section.

- [ ] **Step 16.5: Test malformed pending file surfaces RENDER FAILED block**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
PENDING_BAD=$(mktemp)
echo "this is not valid yaml: [{{{" > "$PENDING_BAD"
PENDING_FILE_OVERRIDE="$PENDING_BAD" python3 -m pipeline.render_pending
rm -f "$PENDING_BAD"
```

Expected: prints a `=== OBSERVE LEARNING CAPTURE — RENDER FAILED ===` block (NOT silent).

- [ ] **Step 16.6: Tail the log and verify no new warnings**

```bash
tail -50 ~/.claude/logs/observe-learning-capture.log 2>&1
```

Look for: any new error/warning lines from this manual exercise that weren't there before. Expected: clean entries showing the test runs without unexpected stack traces.

- [ ] **Step 16.7: Final full-suite run**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest discover tests 2>&1 | tail -10
bash tests/test_stop_hook.sh
bash tests/test_session_start_review_pythonpath.sh
```

Expected: all tests green; bash integration tests print success messages.

- [ ] **Step 16.8: Document verification results**

Append to the spec doc as evidence (this is for your audit trail; not committed unless you want to):

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
cat >> docs/recovery-design-2026-05-04.md <<EOF

---

## Verification Evidence (filled in during Task 16)

Date: $(date +%Y-%m-%d)
Tested by: Chris Milton

- Step 16.2 (missing-key marker): PASS / FAIL — [your observation]
- Step 16.3 (happy path candidate): PASS / FAIL — [candidate title]
- Step 16.4 (render_pending surface): PASS / FAIL
- Step 16.5 (malformed YAML surface): PASS / FAIL
- Step 16.6 (log review): PASS / FAIL — [any anomalies]
- Step 16.7 (full test suite): PASS / FAIL — [test count]
EOF
```

---

## Task 17 — Final cleanup + handoff

**Files:** none modified beyond housekeeping.

- [ ] **Step 17.1: Run linter (best-effort — if `ruff` or `mypy` configured)**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
ls *.cfg *.toml 2>/dev/null  # check for lint config
ruff check . 2>&1 | head -20 || echo "no ruff config — skipping"
mypy pipeline/ 2>&1 | head -20 || echo "no mypy config — skipping"
```

If a linter is configured, fix any new warnings introduced by the changes. If not, skip.

- [ ] **Step 17.2: Verify branch state**

```bash
cd ~/repos/claude-plugins
git log --oneline origin/main..HEAD
git status
```

Expected: ~17 new commits on the branch (one per Task), working tree clean.

- [ ] **Step 17.3: Push branch (only if user authorizes)**

```bash
# Per CLAUDE.md "Confirmations I Always Need" — push requires explicit authorization.
# Confirm with user before running:
git push -u origin recovery/pipeline-2026-05-04
```

Expected: branch pushed; PR creation step follows.

- [ ] **Step 17.4: Create PR (only if user authorizes)**

Per CLAUDE.md: PR creation requires explicit authorization.

```bash
gh pr create --title "fix(observe-learning-capture): pipeline recovery — restore auto-capture across all sessions" --body "$(cat <<'EOF'
## Summary
- Repairs 5 distinct bugs that have rendered the auto-capture pipeline non-functional across all Claude Code sessions for ~3 days
- Replaces fragile recursive `claude --print` subprocess invocation with the Anthropic Python SDK (already installed)
- Switches model Haiku 4.5 → Sonnet 4.5 to clear cache-minimum threshold and reduce TTFT variance
- Restores session-start review surface (was silently broken on subset of sessions)

## Bugs fixed
1. **Logical turn aggregation** — bash jq prefilter + `transcript.last_assistant_turn()` both picked only LAST single JSONL record per Stop event; substantive multi-record turns (~83% of cases) silently dropped.
2. **Classifier subprocess timeouts** — `claude --print` recursive invocation routinely timed out (60s budget vs ~32s TTFT plus 30 KB ALREADY_KNOWN payload).
3. **Marker poison** — `subprocess.TimeoutExpired.__str__()` embedded full argv (35 KB rendered prompt) into marker `failure_reason`; pending queue bloated to 100+ KB per failure.
4. **Session-start review hook** — `os.environ.get("PWD")` lookup unreliable in inline `python3 -c` subprocess; pending-review surface silently failed.
5. **Per-record drops** — classifier silently `continue`d on Haiku records missing required fields; real captures lost.

## Architecture notes
- Layered cacheable system blocks: STATIC_TEMPLATE + SLIM_KNOWN_FACTS, both `cache_control: ephemeral`
- Sonnet 4.5 chosen over Haiku because Haiku's 4096-token cache minimum would silently no-op our slim payload
- Auth precheck via `client.models.list(limit=1)` (free, validates key + network without consuming tokens)
- All marker `failure_reason` strings sanitized at the `build_marker_candidate` boundary (≤200 chars, no embedded argv)
- New `pipeline/render_pending.py` module replaces fragile inline `python3 -c '...'` in session-start hook
- `current_logical_turn()` in `pipeline/transcript.py` walks backward through assistant records, joining text, stopping at first real user prompt (excluding slash commands, hook injections, tool_results)

## Spec
See `plugins/observe-learning-capture/docs/recovery-design-2026-05-04.md`.

## Test plan
- [x] All new regression tests green (8 new test files, ~30 new test cases)
- [x] Existing test suite green (no regressions)
- [x] Manual end-to-end exercise per spec §9.4: missing-key marker, happy-path candidate, render_pending surface, malformed-YAML surface, log review
- [x] Bash integration tests pass under stripped env (`env -i`)

## Risk
- Switches plugin from claude-haiku-4-5 to claude-sonnet-4-5 (~3× per-call cost; ~5-10¢/day total at typical usage)
- Adds dependency on `ANTHROPIC_API_KEY` env var being exported in `~/.zshrc` (separate from Claude Code subscription)
- No data migration required; existing pending YAML records (currently empty after marker discard) parse cleanly under new code
EOF
)"
```

- [ ] **Step 17.5: Hand off to user**

Print a final summary:

```
RECOVERY IMPLEMENTATION COMPLETE
================================
Branch: recovery/pipeline-2026-05-04
Commits: ~17 (one per Task)
New tests: 8 files, ~30 test cases — all green
Existing tests: green (no regressions)
Manual verification: all 5 end-to-end exercises pass
Spec: docs/recovery-design-2026-05-04.md
Plan: docs/recovery-implementation-plan-2026-05-04.md
PR: <URL from gh pr create output>

Next steps:
- Review PR, merge when ready
- Set ANTHROPIC_API_KEY in ~/.zshrc if not already
- Monitor ~/.claude/logs/observe-learning-capture.log for the first week to verify markers don't reappear and real captures land
```

---

## Self-Review

**Spec coverage:** Walked through `docs/recovery-design-2026-05-04.md` section by section:
- §6 Architecture — covered by Tasks 6-15
- §7 Per-Bug Fix Details Bug 1 — Tasks 12, 13, 14, 15
- §7 Bug 2 — Tasks 6, 7, 8, 9, 10, 11
- §7 Bug 3 — Task 1
- §7 Bug 4 — Tasks 3, 4
- §7 Bug 5 — Task 2
- §8 Marker contract — covered by Task 1 (`_sanitize`) + emission paths in Tasks 6-11
- §9 Testing strategy — 8 new test files mapped to Tasks 1-3, 6-9, 12-13
- §9 Verification before "done" — Task 16
- §10 Implementation sequence — matches Task ordering exactly (Bug 3 → 5 → 4 → 2 → 1)
- §11 Open questions — explicitly deferred per spec; not in plan

**Placeholder scan:** Searched for "TBD", "TODO" — only the deliberate `# TODO Task 14: swap to current_logical_turn` comment in Task 10, which is resolved in Task 14. No other placeholders.

**Type consistency:**
- `_invoke_classifier` returns `tuple[str, object]` consistently across Task 8 implementation and Task 9 caller usage.
- `current_logical_turn(path: Path) -> Optional[Turn]` signature matches between Task 13 implementation and Task 14 callsite.
- `_is_real_user_prompt(record: dict) -> bool` consistent.
- `_sanitize(reason: object) -> str` consistent.
- `_generate_slim_known_facts(observeie_md_path: Path) -> str` consistent.
- `_auth_precheck(pending_path: Path, session_id: str, cwd: str) -> bool` consistent.
- `Classifier._maybe_emit_cache_warning(usage, session_id, cwd, captured_at)` consistent across Task 8 stub and Task 9 implementation.

**Sequencing verified:**
- Bug 3 (Task 1) before Bugs 2/5 (Tasks 6-11, 2) — sanitation unblocks visibility ✓
- Bug 5 (Task 2) early — small, low-risk ✓
- Bug 4 (Tasks 3-4) before manual verification — render module needed for Task 16 surfacing ✓
- Config migration (Task 5) before SDK rewrite (Tasks 6-11) — SDK code reads new key ✓
- Bug 2 (Tasks 6-11) before Bug 1 (Tasks 12-15) — Bug 2 fix needed for end-to-end verification of Bug 1 ✓
- Bug 1 (Tasks 12-15) last — final piece for substantive turns to actually reach the classifier ✓

---

## Execution Handoff

Plan complete and saved to `plugins/observe-learning-capture/docs/recovery-implementation-plan-2026-05-04.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per Task, review between Tasks, fast iteration. Best for cross-cutting plans like this where each Task touches different files and you want clean per-Task review.

**2. Inline Execution** — Execute Tasks sequentially in the current session via `superpowers:executing-plans`. Batches with checkpoints. Best if you want to drive each Task interactively.

**Which approach?**
