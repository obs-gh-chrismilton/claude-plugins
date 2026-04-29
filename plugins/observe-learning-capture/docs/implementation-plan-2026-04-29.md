# `observe-learning-capture` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the `observe-learning-capture` plugin per the design spec at `plugins/observe-learning-capture/docs/design.md`. Plugin auto-captures Observe-platform learnings from Claude Code session transcripts, stages them for human review, and promotes approved candidates into `~/.claude/agents/ObserveIE.md` for cross-customer propagation.

**Architecture:** Three Claude Code hooks (Stop, SessionEnd, SessionStart) drive a Python pipeline (`types`, `transcript`, `dedupe`, `stage`, `merge`, `classifier`) that uses Haiku for classification and writes structured YAML candidates to `~/.claude/agents/.observeie-pending.md`. Surfaced at next session start; user approves conversationally.

**Tech Stack:** Python 3.11+ stdlib only (no pip installs). Bash 3.2+ shell scripts (macOS-friendly). `jq` for JSONL parsing in shell. `claude` CLI for Haiku invocation. Tests use stdlib `unittest`.

**Repo:** `~/repos/claude-plugins`, branch `feat/observe-learning-capture` (already created).

**Spec sections referenced:** §5 (Components), §7 (Schema), §9 (Error handling), §10 (Testing). When in doubt, the spec wins.

---

## Pre-flight checklist

- [ ] You're on branch `feat/observe-learning-capture`
- [ ] `~/repos/claude-plugins/plugins/observe-learning-capture/docs/design.md` exists (committed in `8c0ce5c`)
- [ ] `python3 --version` ≥ 3.11
- [ ] `jq --version` works
- [ ] `claude --version` works (Haiku invocations need it)
- [ ] You have not yet pushed the branch (do not push until Task 18)

---

## Task 1: Plugin scaffolding (no TDD — pure config)

**Skipping TDD because:** these are configuration manifests with no behavior to test. Verification happens in Task 13 (hooks register correctly) and Task 17 (end-to-end install).

**Files:**
- Create: `plugins/observe-learning-capture/.claude-plugin/plugin.json`
- Create: `plugins/observe-learning-capture/README.md`
- Create: `plugins/observe-learning-capture/config.json`
- Create: `plugins/observe-learning-capture/.gitignore`
- Create directory structure: `pipeline/`, `prompts/`, `tests/`, `tests/fixtures/`, `commands/`, `hooks/`

- [ ] **Step 1: Create directory structure**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
mkdir -p .claude-plugin pipeline prompts tests/fixtures commands hooks
```

- [ ] **Step 2: Write `.claude-plugin/plugin.json`**

```json
{
  "name": "observe-learning-capture",
  "description": "Auto-captures Observe-platform learnings from Claude Code sessions, stages them for review, promotes approved candidates into ObserveIE.md.",
  "author": {
    "name": "Chris Milton"
  }
}
```

- [ ] **Step 3: Write `README.md`**

```markdown
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

## Manual triggers

- `/observe-review` — review pending queue mid-session
- `/observe-capture` — force-capture last turn (bypass prefilter)
```

- [ ] **Step 4: Write `config.json`**

```json
{
  "destination_file": "~/.claude/agents/ObserveIE.md",
  "pending_file": "~/.claude/agents/.observeie-pending.md",
  "fallback_pending_file": "~/.claude/agents/.observeie-pending.fallback.md",
  "log_file": "~/.claude/logs/observe-learning-capture.log",
  "haiku_model": "claude-haiku-4-5-20251001",
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
      "turns out", "actually", "discovered", "it errors", "must be",
      "won't accept", "cascade", "signature", "requires", "surprisingly",
      "rejected", "deadlock", "doesn't cascade", "managed by", "non-blocking"
    ]
  },
  "session_end_scan_enabled": true,
  "stop_scan_enabled": true,
  "debug": false
}
```

- [ ] **Step 5: Write `.gitignore`**

```gitignore
__pycache__/
*.pyc
.pytest_cache/
tests/fixtures/tmp_*
```

- [ ] **Step 6: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/.claude-plugin/ \
        plugins/observe-learning-capture/README.md \
        plugins/observe-learning-capture/config.json \
        plugins/observe-learning-capture/.gitignore
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): scaffold plugin manifest and config"
```

---

## Task 2: `pipeline/types.py` — Candidate dataclass

**Files:**
- Create: `plugins/observe-learning-capture/pipeline/types.py`
- Create: `plugins/observe-learning-capture/tests/test_types.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_types.py`:

```python
"""Tests for pipeline.types — Candidate and Provenance dataclasses."""
import unittest
from datetime import datetime, timezone
from pipeline.types import Candidate, Provenance, ClassifierMeta


class TestProvenance(unittest.TestCase):
    def test_round_trip_to_dict(self):
        """Provenance round-trips through dict for YAML serialization."""
        p = Provenance(
            session_id="abc123",
            cwd="/Users/chmilton/Work/EchoNet",
            captured_at=datetime(2026, 4, 29, 11, 33, tzinfo=timezone.utc),
            excerpt="OPAL '7d' rejected; use '168h'.",
        )
        d = p.to_dict()
        self.assertEqual(d["session_id"], "abc123")
        self.assertEqual(d["captured_at"], "2026-04-29T11:33:00+00:00")
        # Round-trip
        p2 = Provenance.from_dict(d)
        self.assertEqual(p2.session_id, p.session_id)
        self.assertEqual(p2.captured_at, p.captured_at)


class TestCandidate(unittest.TestCase):
    def test_id_is_deterministic_hash_of_fact(self):
        """Same fact → same id (used for dedupe)."""
        c1 = Candidate.create(
            title="OPAL time literal",
            fact="OPAL rejects '7d'; use '168h'.",
            proposed_section="OPAL Gotchas",
            confidence="high",
            tags=["opal", "syntax"],
            provenance=_dummy_provenance(),
        )
        c2 = Candidate.create(
            title="Different title",
            fact="OPAL rejects '7d'; use '168h'.",  # same fact
            proposed_section="OPAL Gotchas",
            confidence="high",
            tags=["opal"],
            provenance=_dummy_provenance(),
        )
        self.assertEqual(c1.id, c2.id, "Same fact must produce same id")

    def test_id_normalizes_whitespace_and_case(self):
        """Hash normalization: same fact with different whitespace/case → same id."""
        c1 = Candidate.create(
            title="t1", fact="OPAL rejects '7d'; use '168h'.",
            proposed_section="X", confidence="high", tags=[],
            provenance=_dummy_provenance(),
        )
        c2 = Candidate.create(
            title="t2",
            fact="  OPAL  REJECTS '7d'; USE '168h'.  ",  # different case + whitespace
            proposed_section="X", confidence="high", tags=[],
            provenance=_dummy_provenance(),
        )
        self.assertEqual(c1.id, c2.id)

    def test_id_is_8_char_hex(self):
        c = Candidate.create(
            title="t", fact="some fact",
            proposed_section="X", confidence="high", tags=[],
            provenance=_dummy_provenance(),
        )
        self.assertEqual(len(c.id), 8)
        int(c.id, 16)  # must be valid hex

    def test_confidence_validation(self):
        with self.assertRaises(ValueError):
            Candidate.create(
                title="t", fact="f",
                proposed_section="X", confidence="medium-high",  # invalid
                tags=[], provenance=_dummy_provenance(),
            )

    def test_to_yaml_record(self):
        """Candidate serializes to the YAML schema in spec §7.1."""
        c = Candidate.create(
            title="OPAL time literal",
            fact="OPAL rejects '7d'; use '168h'.",
            proposed_section="OPAL Gotchas",
            confidence="high",
            tags=["opal", "syntax"],
            provenance=_dummy_provenance(),
            classifier=ClassifierMeta(
                model="claude-haiku-4-5-20251001",
                prompt_version="1.0",
                confidence_score=0.88,
            ),
        )
        record = c.to_yaml_record()
        # Required fields per spec §7.1
        for field in ["id", "title", "fact", "proposed_section", "confidence",
                      "tags", "source", "classifier"]:
            self.assertIn(field, record, f"missing required field {field}")
        self.assertEqual(record["confidence"], "high")
        self.assertEqual(record["source"]["session_id"], "test-session")


def _dummy_provenance():
    return Provenance(
        session_id="test-session",
        cwd="/tmp/test",
        captured_at=datetime(2026, 4, 29, 11, 33, tzinfo=timezone.utc),
        excerpt="test excerpt",
    )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_types -v
```

Expected: `ModuleNotFoundError: No module named 'pipeline.types'`

- [ ] **Step 3: Write `pipeline/__init__.py` and `tests/__init__.py`**

```bash
touch pipeline/__init__.py tests/__init__.py
```

- [ ] **Step 4: Implement `pipeline/types.py`**

```python
"""Dataclasses for the observe-learning-capture pipeline.

Canonical schema lives in docs/design.md §7.1. These types are the
in-memory representation; YAML serialization happens at the boundary
(stage.py for write, dedupe.py for read).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional


_VALID_CONFIDENCE = {"high", "medium", "low"}


@dataclass
class Provenance:
    """Where and when a candidate was captured.

    Source: spec §7.1 source.* fields.
    """

    session_id: str
    cwd: str
    captured_at: datetime
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize for YAML output. Datetime → ISO 8601 string."""
        return {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "captured_at": self.captured_at.isoformat(),
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Provenance:
        """Deserialize from YAML input. ISO string → datetime."""
        return cls(
            session_id=d["session_id"],
            cwd=d["cwd"],
            captured_at=datetime.fromisoformat(d["captured_at"]),
            excerpt=d["excerpt"],
        )


@dataclass
class ClassifierMeta:
    """Metadata about which model/prompt produced a candidate.

    Source: spec §7.1 classifier.* fields. Useful for retroactive
    re-evaluation if we change prompts later.
    """

    model: str
    prompt_version: str
    confidence_score: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "model": self.model,
            "prompt_version": self.prompt_version,
        }
        if self.confidence_score is not None:
            d["confidence_score"] = self.confidence_score
        return d


@dataclass
class Candidate:
    """A single learning candidate awaiting review.

    Created via `Candidate.create(...)` so the `id` (content hash) is
    computed consistently. Don't construct directly — use `create`.

    See spec §7.1 for the canonical field list.
    """

    id: str
    title: str
    fact: str
    proposed_section: str
    confidence: str
    tags: List[str]
    provenance: Provenance
    classifier: Optional[ClassifierMeta] = None
    dupe_warning: Optional[str] = None
    last_seen_at: Optional[datetime] = None

    @classmethod
    def create(
        cls,
        title: str,
        fact: str,
        proposed_section: str,
        confidence: str,
        tags: List[str],
        provenance: Provenance,
        classifier: Optional[ClassifierMeta] = None,
    ) -> Candidate:
        """Construct a Candidate, computing id from normalized fact."""
        if confidence not in _VALID_CONFIDENCE:
            raise ValueError(
                f"confidence must be one of {_VALID_CONFIDENCE}, got {confidence!r}"
            )
        cand_id = _hash_fact(fact)
        return cls(
            id=cand_id,
            title=title,
            fact=fact,
            proposed_section=proposed_section,
            confidence=confidence,
            tags=tags,
            provenance=provenance,
            classifier=classifier,
        )

    def to_yaml_record(self) -> dict[str, Any]:
        """Serialize to the YAML record shape in spec §7.1."""
        record: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "fact": self.fact,
            "proposed_section": self.proposed_section,
            "confidence": self.confidence,
            "tags": list(self.tags),
            "source": self.provenance.to_dict(),
        }
        if self.classifier is not None:
            record["classifier"] = self.classifier.to_dict()
        if self.dupe_warning is not None:
            record["dupe_warning"] = self.dupe_warning
        if self.last_seen_at is not None:
            record["last_seen_at"] = self.last_seen_at.isoformat()
        return record


def _hash_fact(fact: str) -> str:
    """Normalize and hash for dedup. Lowercase, collapse whitespace, strip
    trailing punctuation. 8-char hex prefix of sha256.

    Matches spec §5.3 (dedupe.py rules) — keep this in sync.
    """
    normalized = fact.lower().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"[.\!\?]+$", "", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_types -v
```

Expected: `Ran 5 tests in 0.00Xs — OK`

- [ ] **Step 6: Commit**

```bash
cd ~/repos/claude-plugins
git add plugins/observe-learning-capture/pipeline/__init__.py \
        plugins/observe-learning-capture/pipeline/types.py \
        plugins/observe-learning-capture/tests/__init__.py \
        plugins/observe-learning-capture/tests/test_types.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): add Candidate/Provenance/ClassifierMeta types

Content-hash dedupe key normalizes whitespace, lowercase, trailing punctuation."
```

---

## Task 3: `pipeline/transcript.py` — JSONL turn extraction

**Files:**
- Create: `plugins/observe-learning-capture/pipeline/transcript.py`
- Create: `plugins/observe-learning-capture/tests/test_transcript.py`
- Create: `plugins/observe-learning-capture/tests/fixtures/sample_session.jsonl`

- [ ] **Step 1: Create test fixture (sample JSONL)**

Create `tests/fixtures/sample_session.jsonl` with 4 lines (one per JSON object):

```json
{"type":"user","message":{"role":"user","content":"Delete the orphan datastreams."},"uuid":"u1","timestamp":"2026-04-29T11:30:00Z"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I'll delete them."}]},"uuid":"a1","timestamp":"2026-04-29T11:30:05Z"}
{"type":"user","message":{"role":"user","content":"What happened?"},"uuid":"u2","timestamp":"2026-04-29T11:31:00Z"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Cascade-ordering deadlock on Tracing/Span — managed datasets reference each other. No force flag exists."}]},"uuid":"a2","timestamp":"2026-04-29T11:31:30Z"}
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_transcript.py`:

```python
"""Tests for pipeline.transcript — JSONL turn extraction."""
import unittest
from pathlib import Path

from pipeline.transcript import (
    last_assistant_turn,
    last_turn_pair,
    all_assistant_turns,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


class TestTranscript(unittest.TestCase):
    def test_last_assistant_turn_returns_most_recent(self):
        turn = last_assistant_turn(FIXTURE)
        self.assertIsNotNone(turn)
        self.assertIn("Cascade-ordering deadlock", turn.text)
        self.assertEqual(turn.uuid, "a2")

    def test_last_turn_pair_returns_user_then_assistant(self):
        user, assistant = last_turn_pair(FIXTURE)
        self.assertEqual(user.text, "What happened?")
        self.assertIn("Cascade-ordering", assistant.text)

    def test_all_assistant_turns(self):
        turns = list(all_assistant_turns(FIXTURE))
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0].uuid, "a1")
        self.assertEqual(turns[1].uuid, "a2")

    def test_missing_file_returns_none(self):
        self.assertIsNone(last_assistant_turn(Path("/nonexistent.jsonl")))

    def test_empty_file_returns_none(self, tmp_path=None):
        # Create empty file in /tmp
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as f:
            self.assertIsNone(last_assistant_turn(Path(f.name)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

```bash
python3 -m unittest tests.test_transcript -v
```

Expected: `ModuleNotFoundError: No module named 'pipeline.transcript'`

- [ ] **Step 4: Implement `pipeline/transcript.py`**

```python
"""Read Claude Code session transcript JSONL files.

Claude Code writes one JSON object per line. Each line is a "turn" —
either user, assistant, tool_use, tool_result, or system. We care about
user and assistant turns for learning capture.

The transcript path is provided to hooks via $CLAUDE_TRANSCRIPT_PATH.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Tuple


@dataclass
class Turn:
    """A single turn from the session transcript."""

    role: str  # "user" or "assistant"
    text: str  # extracted text content (concatenated for multi-block messages)
    uuid: str
    timestamp: str


def _iter_jsonl(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL file. Skips malformed lines
    (logged to stderr but not fatal — partial captures are still useful).
    """
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # Skip malformed line; don't blow up the whole hook
                    continue
    except OSError:
        return


def _extract_text(message_content: object) -> str:
    """Pull plain text from a message.content field.

    Claude Code stores assistant content as a list of blocks (text, tool_use,
    tool_result). User content is usually a string but can be a list. We
    concatenate all text blocks; non-text blocks are ignored.
    """
    if isinstance(message_content, str):
        return message_content
    if isinstance(message_content, list):
        texts = []
        for block in message_content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "\n".join(texts)
    return ""


def _record_to_turn(record: dict) -> Optional[Turn]:
    """Convert a JSONL record to a Turn, or None if it's not a user/assistant turn."""
    role = record.get("type")
    if role not in ("user", "assistant"):
        return None
    message = record.get("message", {})
    text = _extract_text(message.get("content"))
    if not text:
        return None
    return Turn(
        role=role,
        text=text,
        uuid=record.get("uuid", ""),
        timestamp=record.get("timestamp", ""),
    )


def last_assistant_turn(path: Path) -> Optional[Turn]:
    """Return the most recent assistant turn, or None if file missing/empty."""
    last: Optional[Turn] = None
    for record in _iter_jsonl(path):
        turn = _record_to_turn(record)
        if turn is not None and turn.role == "assistant":
            last = turn
    return last


def last_turn_pair(path: Path) -> Tuple[Optional[Turn], Optional[Turn]]:
    """Return (last_user_turn, last_assistant_turn).

    Useful for classifier context: "user asked X, assistant said Y."
    """
    user: Optional[Turn] = None
    assistant: Optional[Turn] = None
    for record in _iter_jsonl(path):
        turn = _record_to_turn(record)
        if turn is None:
            continue
        if turn.role == "user":
            user = turn
        elif turn.role == "assistant":
            assistant = turn
    return user, assistant


def all_assistant_turns(path: Path) -> Iterator[Turn]:
    """Yield every assistant turn in order. Used for SessionEnd full-scan."""
    for record in _iter_jsonl(path):
        turn = _record_to_turn(record)
        if turn is not None and turn.role == "assistant":
            yield turn
```

- [ ] **Step 5: Run test to verify it passes**

```bash
python3 -m unittest tests.test_transcript -v
```

Expected: `Ran 5 tests in 0.00Xs — OK`

- [ ] **Step 6: Commit**

```bash
git add plugins/observe-learning-capture/pipeline/transcript.py \
        plugins/observe-learning-capture/tests/test_transcript.py \
        plugins/observe-learning-capture/tests/fixtures/sample_session.jsonl
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): add transcript JSONL reader

last_assistant_turn / last_turn_pair / all_assistant_turns. Tolerant of
malformed lines and missing files."
```

---

## Task 4: `pipeline/dedupe.py` — content-hash dedup against ObserveIE.md

**Files:**
- Create: `plugins/observe-learning-capture/pipeline/dedupe.py`
- Create: `plugins/observe-learning-capture/tests/test_dedupe.py`
- Create: `plugins/observe-learning-capture/tests/fixtures/sample_observeie.md`

- [ ] **Step 1: Create test fixture (sample ObserveIE.md)**

Create `tests/fixtures/sample_observeie.md`:

```markdown
# ObserveIE

## OPAL Gotchas

- OPAL rejects '7d' as a time literal; use '168h'. <!-- id:a3f7e1c2 captured:2026-04-29 -->
- statsby requires explicit groupby() not bare column. <!-- id:b8c2d4e5 captured:2026-04-28 -->

## Object Management and Cleanup

- App uninstall does NOT cascade datasets. <!-- id:c1d2e3f4 captured:2026-04-28 -->
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_dedupe.py`:

```python
"""Tests for pipeline.dedupe."""
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.dedupe import (
    extract_existing_ids,
    is_duplicate,
    near_duplicate_warning,
)
from pipeline.types import Candidate, Provenance


FIXTURE = Path(__file__).parent / "fixtures" / "sample_observeie.md"


def _candidate(fact: str, tags=None) -> Candidate:
    return Candidate.create(
        title="t",
        fact=fact,
        proposed_section="X",
        confidence="high",
        tags=tags or [],
        provenance=Provenance(
            session_id="s", cwd="/tmp", excerpt="e",
            captured_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
        ),
    )


class TestDedupe(unittest.TestCase):
    def test_extract_existing_ids(self):
        ids = extract_existing_ids(FIXTURE)
        self.assertEqual(ids, {"a3f7e1c2", "b8c2d4e5", "c1d2e3f4"})

    def test_extract_existing_ids_missing_file(self):
        self.assertEqual(extract_existing_ids(Path("/nonexistent.md")), set())

    def test_is_duplicate_by_content(self):
        """Same fact text → recognized as duplicate of existing entry."""
        c = _candidate("OPAL rejects '7d' as a time literal; use '168h'")
        existing = extract_existing_ids(FIXTURE)
        self.assertTrue(is_duplicate(c, existing))

    def test_is_not_duplicate_for_novel_fact(self):
        c = _candidate("OPAL @\"...\" backtick contexts have parsing quirks")
        existing = extract_existing_ids(FIXTURE)
        self.assertFalse(is_duplicate(c, existing))

    def test_near_duplicate_warning_on_overlapping_tags(self):
        existing_candidates = [
            _candidate("Existing fact about OPAL", tags=["opal", "syntax"]),
        ]
        new = _candidate("Different fact also about OPAL syntax",
                         tags=["opal", "syntax", "literal"])
        warning = near_duplicate_warning(new, existing_candidates)
        self.assertIsNotNone(warning)
        self.assertIn("opal", warning.lower())

    def test_no_near_duplicate_when_tags_dont_overlap(self):
        existing = [_candidate("F1", tags=["k8s"])]
        new = _candidate("F2", tags=["billing"])
        self.assertIsNone(near_duplicate_warning(new, existing))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

```bash
python3 -m unittest tests.test_dedupe -v
```

Expected: ImportError on `pipeline.dedupe`.

- [ ] **Step 4: Implement `pipeline/dedupe.py`**

```python
"""Dedup logic.

Two phases:
1. Hash-based exact dedup against ObserveIE.md (parses HTML comments
   we wrote at merge time: `<!-- id:XXXXXXXX captured:YYYY-MM-DD -->`)
2. Tag-overlap warning for "near duplicates" (review-time hint, not block)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional, Set

from pipeline.types import Candidate


# Matches the HTML comment we emit during merge. See merge.py and
# spec §7.2.
_ID_COMMENT_RE = re.compile(r"<!--\s*id:([0-9a-f]{8})\s+captured:[^\s>]+\s*-->")


def extract_existing_ids(observeie_md_path: Path) -> Set[str]:
    """Read ObserveIE.md and return the set of all candidate ids already
    merged into it (extracted from HTML id comments).

    Returns empty set if file missing — the classifier still runs, just
    without dedup against existing knowledge.
    """
    if not observeie_md_path.exists():
        return set()
    try:
        content = observeie_md_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(_ID_COMMENT_RE.findall(content))


def is_duplicate(candidate: Candidate, existing_ids: Set[str]) -> bool:
    """True if this candidate's id matches one already in ObserveIE.md."""
    return candidate.id in existing_ids


def near_duplicate_warning(
    candidate: Candidate,
    other_candidates: Iterable[Candidate],
) -> Optional[str]:
    """Return a human-readable warning if this candidate's tags overlap
    >50% with any other candidate. Used to populate `dupe_warning` field
    in the staged record so the human can review for redundancy.

    Returns None if no near-duplicate found.
    """
    if not candidate.tags:
        return None
    new_tag_set = set(t.lower() for t in candidate.tags)
    for other in other_candidates:
        if other.id == candidate.id:
            continue
        other_tags = set(t.lower() for t in other.tags)
        if not other_tags:
            continue
        overlap = new_tag_set & other_tags
        # >50% of the smaller set
        smaller = min(len(new_tag_set), len(other_tags))
        if smaller > 0 and len(overlap) / smaller > 0.5:
            shared = ", ".join(sorted(overlap))
            return (
                f"Tags overlap with candidate {other.id} "
                f"(shared: {shared}). Consider merging or distinguishing."
            )
    return None
```

- [ ] **Step 5: Run test to verify it passes**

```bash
python3 -m unittest tests.test_dedupe -v
```

Expected: `Ran 6 tests in 0.00Xs — OK`

- [ ] **Step 6: Commit**

```bash
git add plugins/observe-learning-capture/pipeline/dedupe.py \
        plugins/observe-learning-capture/tests/test_dedupe.py \
        plugins/observe-learning-capture/tests/fixtures/sample_observeie.md
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): add dedup against ObserveIE.md

Parses <!-- id:XXX --> HTML comments emitted by merge.py. Near-duplicate
warning emitted when tag overlap exceeds 50% of smaller tag set."
```

---

## Task 5: `pipeline/stage.py` — append candidates to pending file

**Files:**
- Create: `plugins/observe-learning-capture/pipeline/stage.py`
- Create: `plugins/observe-learning-capture/tests/test_stage.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_stage.py`:

```python
"""Tests for pipeline.stage — append YAML records to pending file."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.stage import append_candidates, read_pending
from pipeline.types import Candidate, Provenance, ClassifierMeta


def _candidate(fact: str = "test fact") -> Candidate:
    return Candidate.create(
        title="title",
        fact=fact,
        proposed_section="OPAL Gotchas",
        confidence="high",
        tags=["opal"],
        provenance=Provenance(
            session_id="s1", cwd="/tmp/cwd",
            captured_at=datetime(2026, 4, 29, 11, 33, tzinfo=timezone.utc),
            excerpt="excerpt",
        ),
        classifier=ClassifierMeta(
            model="claude-haiku-4-5-20251001",
            prompt_version="1.0", confidence_score=0.9,
        ),
    )


class TestStage(unittest.TestCase):
    def test_append_to_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            pending = Path(d) / ".pending.md"
            c = _candidate()
            append_candidates(pending, [c])
            self.assertTrue(pending.exists())
            records = read_pending(pending)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["id"], c.id)

    def test_append_preserves_existing_entries(self):
        with tempfile.TemporaryDirectory() as d:
            pending = Path(d) / ".pending.md"
            c1 = _candidate("first fact")
            c2 = _candidate("second fact")
            append_candidates(pending, [c1])
            append_candidates(pending, [c2])
            records = read_pending(pending)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["fact"], "first fact")
            self.assertEqual(records[1]["fact"], "second fact")

    def test_append_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as d:
            pending = Path(d) / "nonexistent" / ".pending.md"
            append_candidates(pending, [_candidate()])
            self.assertTrue(pending.exists())

    def test_append_empty_list_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            pending = Path(d) / ".pending.md"
            append_candidates(pending, [])
            self.assertFalse(pending.exists())

    def test_read_pending_returns_empty_for_missing_file(self):
        self.assertEqual(read_pending(Path("/nonexistent.md")), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m unittest tests.test_stage -v
```

Expected: ImportError on `pipeline.stage`.

- [ ] **Step 3: Implement `pipeline/stage.py`**

```python
"""Append-only writer for the pending candidate queue.

Format: YAML list, one document per candidate, separated by `---`.
Append-only — never rewrites or reorders. Keeps `git diff` friendly if
the user version-controls their `~/.claude/`.

We write YAML manually (simple-shape only) to avoid pyyaml dependency.
The schema is restricted to scalars + lists + nested dicts of scalars,
which is safely round-trippable through hand-written code.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Any, List

from pipeline.types import Candidate


def append_candidates(pending_file: Path, candidates: List[Candidate]) -> None:
    """Append candidates to the pending YAML file. Creates parent dirs.
    No-op on empty list. Uses POSIX flock to avoid concurrent-write races
    (spec §9 error handling row).
    """
    if not candidates:
        return

    pending_file.parent.mkdir(parents=True, exist_ok=True)

    # Open for append; create if needed
    with pending_file.open("a", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except OSError:
            # Lock failed — proceed anyway. Worst case: malformed concat.
            # Acceptable per spec §9 fallback.
            pass
        for cand in candidates:
            f.write("---\n")
            f.write(_render_yaml(cand.to_yaml_record(), indent=0))
            f.write("\n")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def read_pending(pending_file: Path) -> List[dict[str, Any]]:
    """Parse pending YAML file. Returns list of records (dicts).
    Returns empty list if file missing.
    """
    if not pending_file.exists():
        return []
    try:
        content = pending_file.read_text(encoding="utf-8")
    except OSError:
        return []
    return _parse_yaml_list(content)


# --- YAML rendering ----------------------------------------------------------
# Hand-rolled to avoid pyyaml dep. Handles only the shapes we emit (string,
# int, float, bool, None, list of scalars, list of dicts, dict of scalars).
# If we ever need more complex shapes, swap to ruamel.yaml or pyyaml — but
# document it as a dep change.


def _render_yaml(value: Any, indent: int) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{k}:")
                out.append(_render_yaml(v, indent + 1))
            elif isinstance(v, str) and ("\n" in v or len(v) > 80):
                out.append(f"{pad}{k}: |")
                for line in v.splitlines():
                    out.append(f"{pad}  {line}")
            else:
                out.append(f"{pad}{k}: {_scalar(v)}")
        return "\n".join(out)
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, (dict, list)):
                out.append(f"{pad}-")
                out.append(_render_yaml(item, indent + 1))
            else:
                out.append(f"{pad}- {_scalar(item)}")
        return "\n".join(out)
    return f"{pad}{_scalar(value)}"


def _scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # Quote if contains chars that would confuse a YAML parser
    needs_quote = any(c in s for c in ":#&*!|>'\"%@`") or s.strip() != s
    if needs_quote:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


# --- YAML parsing ------------------------------------------------------------
# Same restriction: only handles shapes we emit. Safe-list parser, not a
# full YAML implementation. If shapes change, this needs updating.


def _parse_yaml_list(content: str) -> List[dict[str, Any]]:
    """Parse a stream of `---`-separated YAML documents."""
    records: List[dict[str, Any]] = []
    docs = [d.strip() for d in content.split("---") if d.strip()]
    for doc in docs:
        try:
            parsed = _parse_yaml_block(doc.splitlines(), 0)[0]
            if isinstance(parsed, dict):
                records.append(parsed)
        except (ValueError, IndexError):
            # Tolerate malformed; surface in logs at higher level
            continue
    return records


def _parse_yaml_block(lines: list[str], base_indent: int) -> tuple[Any, int]:
    """Parse lines starting at base_indent. Returns (value, lines_consumed)."""
    result: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        stripped = line.lstrip()
        line_indent = len(line) - len(stripped)
        if line_indent < base_indent:
            break
        if line_indent > base_indent:
            i += 1
            continue
        if ":" not in stripped:
            i += 1
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        if not val:
            # Block — recurse
            sub_lines = lines[i + 1:]
            sub_value, consumed = _parse_yaml_subblock(sub_lines, base_indent + 2)
            result[key] = sub_value
            i += 1 + consumed
        elif val == "|":
            # Multi-line literal
            block_lines = []
            i += 1
            while i < len(lines):
                ln = lines[i]
                if ln.strip() and (len(ln) - len(ln.lstrip())) <= base_indent:
                    break
                block_lines.append(ln[base_indent + 2:] if len(ln) > base_indent + 2 else "")
                i += 1
            result[key] = "\n".join(block_lines).rstrip()
        else:
            result[key] = _parse_scalar(val)
            i += 1
    return result, i


def _parse_yaml_subblock(lines: list[str], indent: int) -> tuple[Any, int]:
    """Decide if subblock is a list or dict, parse accordingly."""
    if not lines:
        return {}, 0
    # Find first non-empty line at this indent
    first = None
    for line in lines:
        if line.strip():
            first = line
            break
    if first is None:
        return {}, 0
    stripped = first.lstrip()
    if stripped.startswith("- "):
        return _parse_yaml_list_block(lines, indent)
    return _parse_yaml_block(lines, indent)


def _parse_yaml_list_block(lines: list[str], indent: int) -> tuple[list, int]:
    """Parse a `- item\\n- item\\n...` block."""
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent < indent:
            break
        stripped = line.lstrip()
        if stripped.startswith("- "):
            val = stripped[2:].strip()
            result.append(_parse_scalar(val))
            i += 1
        else:
            i += 1
    return result, i


def _parse_scalar(s: str) -> Any:
    if s == "null" or s == "":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m unittest tests.test_stage -v
```

Expected: `Ran 5 tests in 0.00Xs — OK`

- [ ] **Step 5: Commit**

```bash
git add plugins/observe-learning-capture/pipeline/stage.py \
        plugins/observe-learning-capture/tests/test_stage.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): append-only stage to pending YAML

Hand-rolled YAML emitter/parser (no pyyaml dep). flock-protected appends.
Schema-restricted parser tolerates malformed entries gracefully."
```

---

## Task 6: `pipeline/merge.py` — promote approved → ObserveIE.md

**Files:**
- Create: `plugins/observe-learning-capture/pipeline/merge.py`
- Create: `plugins/observe-learning-capture/tests/test_merge.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_merge.py`:

```python
"""Tests for pipeline.merge — promote approved candidates into ObserveIE.md."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.merge import merge_candidate, remove_from_pending
from pipeline.stage import append_candidates, read_pending
from pipeline.types import Candidate, Provenance


def _candidate(fact: str = "Test fact about OPAL.",
               section: str = "OPAL Gotchas") -> Candidate:
    return Candidate.create(
        title="t", fact=fact, proposed_section=section,
        confidence="high", tags=["opal"],
        provenance=Provenance(
            session_id="s", cwd="/tmp", excerpt="e",
            captured_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
        ),
    )


class TestMerge(unittest.TestCase):
    def test_merge_appends_under_existing_section(self):
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            obs.write_text(
                "# ObserveIE\n\n## OPAL Gotchas\n\n- Existing fact.\n",
                encoding="utf-8",
            )
            c = _candidate("New OPAL fact")
            merge_candidate(c, obs)
            content = obs.read_text(encoding="utf-8")
            self.assertIn("- Existing fact.", content)
            self.assertIn("- New OPAL fact", content)
            self.assertIn(f"<!-- id:{c.id}", content)

    def test_merge_creates_section_if_missing(self):
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            obs.write_text("# ObserveIE\n\n## Other Section\n\n- thing.\n",
                           encoding="utf-8")
            c = _candidate("OPAL fact", section="OPAL Gotchas")
            merge_candidate(c, obs)
            content = obs.read_text(encoding="utf-8")
            self.assertIn("## OPAL Gotchas", content)
            self.assertIn("- OPAL fact", content)

    def test_merge_creates_file_if_missing(self):
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            c = _candidate("First fact ever")
            merge_candidate(c, obs)
            self.assertTrue(obs.exists())
            content = obs.read_text(encoding="utf-8")
            self.assertIn("## OPAL Gotchas", content)
            self.assertIn("- First fact ever", content)

    def test_remove_from_pending(self):
        with tempfile.TemporaryDirectory() as d:
            pending = Path(d) / ".pending.md"
            c1 = _candidate("fact one")
            c2 = _candidate("fact two")
            append_candidates(pending, [c1, c2])
            remove_from_pending(c1.id, pending)
            remaining = read_pending(pending)
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["fact"], "fact two")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m unittest tests.test_merge -v
```

Expected: ImportError on `pipeline.merge`.

- [ ] **Step 3: Implement `pipeline/merge.py`**

```python
"""Merge an approved candidate into ObserveIE.md, remove from pending.

Section routing:
- If `## proposed_section` heading exists (case-insensitive) → append bullet
  under it (right after the section header line, ahead of existing bullets,
  or at section end — we choose section-end for predictable diff).
- If section doesn't exist → append a new `## proposed_section` at file end.

Bullet format (matches spec §7.2):
    - {fact} <!-- id:{id} captured:{date} -->
"""
from __future__ import annotations

import re
from pathlib import Path

from pipeline.stage import read_pending
from pipeline.types import Candidate


_SECTION_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def merge_candidate(candidate: Candidate, observeie_md: Path) -> None:
    """Promote candidate into ObserveIE.md. Creates file/section as needed."""
    observeie_md.parent.mkdir(parents=True, exist_ok=True)

    bullet = (
        f"- {candidate.fact.strip()} "
        f"<!-- id:{candidate.id} "
        f"captured:{candidate.provenance.captured_at.date().isoformat()} -->"
    )

    if not observeie_md.exists():
        # Create with header + section
        new_content = (
            f"# ObserveIE\n\n## {candidate.proposed_section}\n\n{bullet}\n"
        )
        observeie_md.write_text(new_content, encoding="utf-8")
        return

    content = observeie_md.read_text(encoding="utf-8")

    section_idx = _find_section_index(content, candidate.proposed_section)

    if section_idx is None:
        # Append new section at file end
        if not content.endswith("\n"):
            content += "\n"
        content += f"\n## {candidate.proposed_section}\n\n{bullet}\n"
    else:
        # Insert at end of section (just before next heading or EOF)
        next_heading_idx = _find_next_heading_at_or_above(
            content, section_idx, level=2
        )
        insertion_point = next_heading_idx if next_heading_idx is not None else len(content)
        # Trim trailing whitespace before insertion
        before = content[:insertion_point].rstrip() + "\n"
        after = content[insertion_point:]
        content = f"{before}{bullet}\n\n{after}" if after else f"{before}{bullet}\n"

    observeie_md.write_text(content, encoding="utf-8")


def remove_from_pending(candidate_id: str, pending_file: Path) -> None:
    """Remove the candidate with matching id from the pending file.

    Rewrites the file (this is the only operation that's not append-only).
    """
    if not pending_file.exists():
        return
    records = read_pending(pending_file)
    remaining = [r for r in records if r.get("id") != candidate_id]
    if len(remaining) == len(records):
        return  # not found, no-op

    # Re-import here to avoid circular import
    from pipeline.stage import _render_yaml

    if not remaining:
        pending_file.write_text("", encoding="utf-8")
        return

    chunks = []
    for record in remaining:
        chunks.append("---\n" + _render_yaml(record, indent=0))
    pending_file.write_text("\n".join(chunks) + "\n", encoding="utf-8")


def _find_section_index(content: str, section_name: str) -> int | None:
    """Find the line-start index of `## {section_name}` (case-insensitive).
    Returns None if not found.
    """
    pattern = re.compile(
        rf"^##\s+{re.escape(section_name)}\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    m = pattern.search(content)
    return m.start() if m else None


def _find_next_heading_at_or_above(content: str, after_idx: int, level: int) -> int | None:
    """Find the next `##` (or higher-level) heading after `after_idx`.
    Returns line-start index or None.
    """
    pattern = re.compile(rf"^#{{1,{level}}}\s+", re.MULTILINE)
    for m in pattern.finditer(content, pos=after_idx + 1):
        return m.start()
    return None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m unittest tests.test_merge -v
```

Expected: `Ran 4 tests in 0.00Xs — OK`

- [ ] **Step 5: Commit**

```bash
git add plugins/observe-learning-capture/pipeline/merge.py \
        plugins/observe-learning-capture/tests/test_merge.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): merge approved candidates to ObserveIE.md

Routes by ## section heading; creates section/file as needed. Removes from
pending after merge (only non-append-only op in the pipeline)."
```

---

## Task 7: Classifier prompt template

**Files:**
- Create: `plugins/observe-learning-capture/prompts/classifier.md`
- Create: `plugins/observe-learning-capture/prompts/classifier-fewshot.md`

**Skipping TDD because:** prompt content is data, not behavior. Tested indirectly by `test_classifier.py` (Task 8) which mocks Haiku and asserts the prompt assembly.

- [ ] **Step 1: Write `prompts/classifier.md`**

```markdown
You are an Observe-platform learning extractor. Read the conversation excerpt
below and identify any **non-trivial, Observe-platform-general** facts that
would benefit any future Observe Integration Engineer (IE) regardless of
which customer they're working with.

# What to capture

Capture facts that:
- Describe undocumented or non-obvious Observe platform behavior (API, OPAL
  syntax, ingest rules, dataflow constraints, error patterns)
- Apply to ANY tenant (not specific to one customer's tenant ID, dataset,
  monitor, or contact)
- Are confirmed/observed (not speculation, not hypothesis-in-progress)
- Are NOT already in the "Already known" knowledge base below

# What to REJECT

Do NOT capture:
- Customer-specific facts (tenant IDs, dataset names like "EchoNet/foo",
  customer contacts, customer-specific quirks). These belong in per-customer
  CLAUDE.md, not here.
- General programming concepts (these aren't Observe-specific).
- Things already in "Already known" below.
- Speculation, plans, "we should try X next time" — only confirmed facts.
- Trivia findable in tier-1 Observe public docs (e.g., "OPAL has a `filter`
  verb"). Bias toward gotchas, undocumented behavior, mutation signatures,
  cascade rules, error messages.

# Output format

Return a YAML list. One document per candidate. Empty list if no candidates.

Example output:

```yaml
- title: "OPAL '7d' time literal rejected"
  fact: |
    OPAL rejects '7d' as a time literal in @"…" backtick contexts;
    use '168h' instead. Also '14d' → '336h'.
  proposed_section: "OPAL Gotchas"
  confidence: high
  tags: [opal, time-literals, syntax]
  classifier_confidence_score: 0.88
- title: "..."
  ...
```

Confidence guidance:
- `high`: directly observed in the conversation, with evidence
- `medium`: implied strongly by tool output or error messages
- `low`: plausibly true but not directly confirmed — be conservative

If there are no candidates, return an empty YAML document: just `[]` on its
own line.

---

# Already known (in ObserveIE.md, do NOT re-capture)

{{ALREADY_KNOWN}}

---

# Conversation excerpt to analyze

Captured at: {{CONTEXT_TIMESTAMP}}
Working directory: {{CWD}}

{{TURN}}

---

Now extract candidates. Return YAML only — no prose explanation, no markdown
fences. Empty list `[]` if no candidates.
```

- [ ] **Step 2: Write `prompts/classifier-fewshot.md`** (examples loaded conditionally for accuracy tuning)

```markdown
# Few-shot examples for the Observe learning classifier

## YES — capture these

### Example 1: undocumented API behavior
Conversation excerpt:
> "Tried calling deleteDatastream(id:42767020) — returned validation error
> 'cannot delete datastream with active poller'. Had to delete the poller
> first via deletePoller(id:...) then the datastream call succeeded."

Output:
```yaml
- title: "Datastreams with pollers cannot be deleted directly"
  fact: |
    deleteDatastream rejects datastreams with active pollers; the poller
    must be deleted first via deletePoller(id:).
  proposed_section: "Object Management and Cleanup"
  confidence: high
  tags: [delete, datastream, poller, cascade]
```

### Example 2: OPAL syntax quirk
Conversation excerpt:
> "OPAL filter accepts `time > now() - 168h` but rejects `time > now() - 7d`
> with 'expected duration literal'."

Output:
```yaml
- title: "OPAL 'Nd' time literals rejected"
  fact: |
    OPAL duration literals like '7d' are rejected; use hours instead
    ('168h'). Also '14d' → '336h'.
  proposed_section: "OPAL Gotchas"
  confidence: high
  tags: [opal, syntax, time-literals]
```

## NO — do NOT capture these

### Example 3: customer-specific
Conversation excerpt:
> "EchoNet's tenant ID is 153283189978 and the main billing dataset is
> at /usage/Billing."

Reasoning: tenant ID and dataset path are customer-specific. They belong
in `~/Work/EchoNet/CLAUDE.md`, not in ObserveIE.md.

Output: `[]`

### Example 4: speculation
Conversation excerpt:
> "I think we might be able to bypass the cascade-ordering by using
> deleteFolder, but I haven't verified yet."

Reasoning: "I think… haven't verified" → not confirmed. Only capture
confirmed facts.

Output: `[]`

### Example 5: tier-1 documented behavior
Conversation excerpt:
> "OPAL has a `filter` verb that takes a boolean expression."

Reasoning: this is in the public OPAL docs. Only capture non-obvious or
gotcha-shaped facts.

Output: `[]`
```

- [ ] **Step 3: Commit**

```bash
git add plugins/observe-learning-capture/prompts/
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): add classifier prompt + few-shot examples

Prompt explicitly rejects customer-specific facts, speculation, and
tier-1-documented behavior. Few-shot covers 2 yes-cases + 3 no-cases."
```

---

## Task 8: `pipeline/classifier.py` — Haiku invocation

**Files:**
- Create: `plugins/observe-learning-capture/pipeline/classifier.py`
- Create: `plugins/observe-learning-capture/tests/test_classifier.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_classifier.py`:

```python
"""Tests for pipeline.classifier — Haiku invocation orchestration.

We mock the subprocess call to `claude` CLI. Real Haiku calls are only
exercised via end-to-end tests (Task 16).
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pipeline.classifier import (
    Classifier,
    parse_haiku_yaml_output,
    build_marker_candidate,
)


SAMPLE_YAML_OUTPUT = """\
- title: "OPAL '7d' rejected"
  fact: |
    OPAL rejects '7d'; use '168h'.
  proposed_section: "OPAL Gotchas"
  confidence: high
  tags: [opal, syntax]
  classifier_confidence_score: 0.88
"""

SAMPLE_EMPTY_OUTPUT = "[]"


class TestClassifierParser(unittest.TestCase):
    def test_parse_valid_yaml_output(self):
        cands = parse_haiku_yaml_output(SAMPLE_YAML_OUTPUT)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["title"], "OPAL '7d' rejected")

    def test_parse_empty_list(self):
        self.assertEqual(parse_haiku_yaml_output(SAMPLE_EMPTY_OUTPUT), [])

    def test_parse_malformed_returns_empty_with_marker(self):
        cands = parse_haiku_yaml_output("totally not yaml {{{")
        self.assertEqual(cands, [])


class TestClassifierEndToEnd(unittest.TestCase):
    @mock.patch("pipeline.classifier._invoke_haiku")
    def test_happy_path(self, mock_invoke):
        mock_invoke.return_value = SAMPLE_YAML_OUTPUT
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            obs.write_text("# ObserveIE\n", encoding="utf-8")
            c = Classifier(
                model="claude-haiku-4-5-20251001",
                prompt_template_path=_prompts_dir() / "classifier.md",
                observeie_md_path=obs,
            )
            cands = c.classify(
                turn_text="Some Observe platform conversation.",
                session_id="abc",
                cwd="/tmp/cwd",
            )
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].confidence, "high")
        self.assertEqual(
            cands[0].provenance.session_id, "abc"
        )

    @mock.patch("pipeline.classifier._invoke_haiku")
    def test_haiku_failure_emits_marker_candidate(self, mock_invoke):
        mock_invoke.side_effect = RuntimeError("haiku timeout")
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            obs.write_text("", encoding="utf-8")
            c = Classifier(
                model="claude-haiku-4-5-20251001",
                prompt_template_path=_prompts_dir() / "classifier.md",
                observeie_md_path=obs,
            )
            cands = c.classify(
                turn_text="x", session_id="s", cwd="/c",
            )
        self.assertEqual(len(cands), 1)
        self.assertIn("self-error", cands[0].tags)


class TestMarkerCandidate(unittest.TestCase):
    def test_marker_carries_failure_reason(self):
        c = build_marker_candidate(
            failure_reason="haiku timeout",
            session_id="s", cwd="/c",
            captured_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
        )
        self.assertIn("self-error", c.tags)
        self.assertIn("haiku timeout", c.fact)


def _prompts_dir() -> Path:
    return Path(__file__).parent.parent / "prompts"


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m unittest tests.test_classifier -v
```

Expected: ImportError on `pipeline.classifier`.

- [ ] **Step 3: Implement `pipeline/classifier.py`**

```python
"""Haiku-based classifier for learning candidates.

Invokes the `claude` CLI as a subprocess (no API key handling here — the
CLI manages auth). On any failure, emits a "marker candidate" so the
human sees the failure at next review (per spec §9 — log AND surface).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from pipeline.stage import _parse_yaml_list
from pipeline.types import Candidate, ClassifierMeta, Provenance


@dataclass
class Classifier:
    model: str
    prompt_template_path: Path
    observeie_md_path: Path
    prompt_version: str = "1.0"

    def classify(
        self,
        turn_text: str,
        session_id: str,
        cwd: str,
        excerpt: Optional[str] = None,
    ) -> List[Candidate]:
        """Run Haiku on turn_text. Returns 0+ candidates.

        On Haiku failure, returns a single marker candidate (per spec §9).
        """
        captured_at = datetime.now(timezone.utc)
        excerpt = excerpt or turn_text[:200]

        try:
            already_known = _read_safe(self.observeie_md_path)
            prompt = _build_prompt(
                template_path=self.prompt_template_path,
                turn_text=turn_text,
                already_known=already_known,
                cwd=cwd,
                captured_at=captured_at,
            )
            haiku_output = _invoke_haiku(prompt, self.model)
        except (RuntimeError, OSError) as e:
            return [
                build_marker_candidate(
                    failure_reason=str(e),
                    session_id=session_id, cwd=cwd,
                    captured_at=captured_at,
                )
            ]

        raw_candidates = parse_haiku_yaml_output(haiku_output)
        if not raw_candidates and haiku_output.strip() and haiku_output.strip() != "[]":
            # Haiku returned something but parser didn't recognize it
            return [
                build_marker_candidate(
                    failure_reason=f"malformed yaml: {haiku_output[:200]}",
                    session_id=session_id, cwd=cwd,
                    captured_at=captured_at,
                )
            ]

        result: List[Candidate] = []
        for raw in raw_candidates:
            try:
                result.append(_raw_to_candidate(
                    raw, session_id=session_id, cwd=cwd,
                    captured_at=captured_at, excerpt=excerpt,
                    model=self.model, prompt_version=self.prompt_version,
                ))
            except (KeyError, ValueError):
                continue  # skip individual malformed entries
        return result


def parse_haiku_yaml_output(output: str) -> List[dict[str, Any]]:
    """Parse Haiku's YAML response. Returns [] on empty list or malformed."""
    output = output.strip()
    if not output or output == "[]":
        return []
    # Strip markdown fences if Haiku wraps in ```yaml ... ```
    output = re.sub(r"^```(?:yaml)?\s*\n", "", output, flags=re.MULTILINE)
    output = re.sub(r"\n```\s*$", "", output, flags=re.MULTILINE)
    # Each candidate is a YAML doc starting with `- ` at root indent.
    # Reuse stage._parse_yaml_list for the heavy lifting — but Haiku
    # output is one list, not multiple ---separated docs. Wrap each
    # `- ` entry as its own doc.
    docs = _split_haiku_list(output)
    if not docs:
        return []
    return _parse_yaml_list("---\n" + "\n---\n".join(docs))


def _split_haiku_list(yaml_list: str) -> List[str]:
    """Split a top-level YAML list (`- item\\n- item`) into per-item chunks."""
    items: List[str] = []
    current: List[str] = []
    for line in yaml_list.splitlines():
        if line.startswith("- "):
            if current:
                items.append("\n".join(current))
            # Strip leading "- " and dedent rest of item
            current = [line[2:]]
        elif current and (line.startswith("  ") or not line.strip()):
            # Continuation of current item — strip 2 spaces of indent
            current.append(line[2:] if line.startswith("  ") else line)
        elif current:
            current.append(line)
    if current:
        items.append("\n".join(current))
    return items


def _build_prompt(
    template_path: Path,
    turn_text: str,
    already_known: str,
    cwd: str,
    captured_at: datetime,
) -> str:
    template = template_path.read_text(encoding="utf-8")
    return (
        template
        .replace("{{TURN}}", turn_text)
        .replace("{{ALREADY_KNOWN}}", already_known or "(empty)")
        .replace("{{CWD}}", cwd)
        .replace("{{CONTEXT_TIMESTAMP}}", captured_at.isoformat())
    )


def _invoke_haiku(prompt: str, model: str) -> str:
    """Call the `claude` CLI with --model and --print. Returns stdout."""
    proc = subprocess.run(
        ["claude", "--model", model, "--print", prompt],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exit={proc.returncode} stderr={proc.stderr[:300]}")
    return proc.stdout


def _read_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _raw_to_candidate(
    raw: dict[str, Any], *,
    session_id: str, cwd: str,
    captured_at: datetime, excerpt: str,
    model: str, prompt_version: str,
) -> Candidate:
    score = raw.get("classifier_confidence_score")
    return Candidate.create(
        title=raw["title"],
        fact=raw["fact"].strip(),
        proposed_section=raw["proposed_section"],
        confidence=raw["confidence"],
        tags=list(raw.get("tags", [])),
        provenance=Provenance(
            session_id=session_id, cwd=cwd,
            captured_at=captured_at, excerpt=excerpt,
        ),
        classifier=ClassifierMeta(
            model=model, prompt_version=prompt_version,
            confidence_score=float(score) if score is not None else None,
        ),
    )


def build_marker_candidate(
    failure_reason: str, *,
    session_id: str, cwd: str, captured_at: datetime,
) -> Candidate:
    """Emit a sentinel candidate so failures surface at review time."""
    return Candidate.create(
        title="[FAILURE] classifier",
        fact=f"Classifier failed: {failure_reason}",
        proposed_section="Plugin Self-Errors",
        confidence="low",
        tags=["self-error"],
        provenance=Provenance(
            session_id=session_id, cwd=cwd,
            captured_at=captured_at,
            excerpt=f"Auto-generated marker. Reason: {failure_reason}",
        ),
        classifier=None,
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m unittest tests.test_classifier -v
```

Expected: `Ran 5 tests in 0.0Xs — OK`

- [ ] **Step 5: Commit**

```bash
git add plugins/observe-learning-capture/pipeline/classifier.py \
        plugins/observe-learning-capture/tests/test_classifier.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): add Haiku-based classifier

Subprocess to claude CLI; mocked in tests. Failure emits marker candidate
so plugin self-errors surface at review (spec §9 log+surface)."
```

---

## Task 9: `hooks/stop-hook.sh` — prefilter + classifier invocation

**Files:**
- Create: `plugins/observe-learning-capture/hooks/stop-hook.sh`
- Create: `plugins/observe-learning-capture/tests/test_stop_hook.sh`

- [ ] **Step 1: Write the failing shell test**

Create `tests/test_stop_hook.sh`:

```bash
#!/usr/bin/env bash
# Tests for stop-hook.sh prefilter logic.
#
# Strategy: invoke the hook with PREFILTER_ONLY=1 (debug flag, see hook).
# That short-circuits before the classifier and just exits 0/1 based on
# whether the prefilter would have passed.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/../hooks/stop-hook.sh"
PASS=0
FAIL=0

assert_prefilter() {
    local description="$1"
    local expected="$2"  # "pass" or "fail"
    local transcript="$3"

    local exit_code=0
    PREFILTER_ONLY=1 \
    CLAUDE_TRANSCRIPT_PATH="$transcript" \
    CLAUDE_SESSION_ID="test" \
    CLAUDE_PROJECT_DIR="/tmp/test" \
    bash "$HOOK" >/dev/null 2>&1 || exit_code=$?

    if [[ "$expected" == "pass" && $exit_code -eq 0 ]]; then
        echo "PASS: $description"
        PASS=$((PASS + 1))
    elif [[ "$expected" == "fail" && $exit_code -eq 1 ]]; then
        echo "PASS: $description"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $description (expected=$expected, got=$exit_code)"
        FAIL=$((FAIL + 1))
    fi
}

# Fixtures
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# Trivial ack — should fail
cat > "$TMPDIR/trivial.jsonl" <<'EOF'
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Sure, I'll do that."}]},"uuid":"a1","timestamp":"2026-04-29T11:00:00Z"}
EOF
assert_prefilter "trivial ack rejected" "fail" "$TMPDIR/trivial.jsonl"

# Long Observe-platform discovery — should pass
cat > "$TMPDIR/discovery.jsonl" <<'EOF'
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Turns out OPAL rejects '7d' as a duration literal — must use '168h' instead. Verified by trying both in a filter() expression. The error message says 'expected duration literal' which is misleading because '7d' looks like one. Same goes for '14d' which must be '336h'."}]},"uuid":"a2","timestamp":"2026-04-29T11:00:00Z"}
EOF
assert_prefilter "Observe discovery passes" "pass" "$TMPDIR/discovery.jsonl"

# Long generic prose without Observe vocab — should fail
cat > "$TMPDIR/generic.jsonl" <<'EOF'
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I read the file and notice it has many patterns related to error handling. The structure is straightforward — a try/except wrapping the main logic. There's no special configuration needed. Let me know if you'd like me to refactor it."}]},"uuid":"a3","timestamp":"2026-04-29T11:00:00Z"}
EOF
assert_prefilter "generic prose rejected" "fail" "$TMPDIR/generic.jsonl"

# Empty transcript — should fail (no turn to classify)
echo -n "" > "$TMPDIR/empty.jsonl"
assert_prefilter "empty transcript rejected" "fail" "$TMPDIR/empty.jsonl"

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
```

```bash
chmod +x plugins/observe-learning-capture/tests/test_stop_hook.sh
```

- [ ] **Step 2: Run the shell test (it will fail because hook doesn't exist)**

```bash
bash plugins/observe-learning-capture/tests/test_stop_hook.sh
```

Expected: `bash: stop-hook.sh: No such file or directory`

- [ ] **Step 3: Implement `hooks/stop-hook.sh`**

```bash
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
#   PREFILTER_ONLY=1 — exit 0 if prefilter would pass, 1 otherwise. Used by
#                      tests/test_stop_hook.sh. No classifier invocation.

set -uo pipefail

# ---- Plugin paths -----------------------------------------------------------
HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
LOG_FILE="${HOME}/.claude/logs/observe-learning-capture.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [stop-hook] $*" >> "$LOG_FILE"
}

# ---- Read most recent assistant turn ----------------------------------------
TRANSCRIPT="${CLAUDE_TRANSCRIPT_PATH:-}"
if [[ -z "$TRANSCRIPT" || ! -f "$TRANSCRIPT" ]]; then
    log "no transcript at \$CLAUDE_TRANSCRIPT_PATH=$TRANSCRIPT — skip"
    exit 0
fi

# Last assistant turn's text. Use jq to extract text blocks from the most
# recent assistant message. Tolerant of missing/malformed jq output.
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
    [[ "${PREFILTER_ONLY:-0}" == "1" ]] && exit 1
    log "no assistant turn extracted — skip"
    exit 0
fi

# ---- Prefilter --------------------------------------------------------------
# Heuristic: must be (a) >150 chars, (b) contain Observe vocab, (c) contain
# either a discovery verb, an OPAL block, an HTTP error code, or a GraphQL
# mutation name pattern.
TURN_LEN=${#TURN_TEXT}
if (( TURN_LEN < 150 )); then
    [[ "${PREFILTER_ONLY:-0}" == "1" ]] && exit 1
    exit 0
fi

# Vocab check (case-insensitive)
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

# Discovery verb / pattern check
DISCOVERY_HIT=0
for phrase in "turns out" "actually" "discovered" "it errors" "must be" \
              "won't accept" "cascade" "signature" "requires" "rejected" \
              "deadlock" "doesn't cascade"; do
    if [[ "$TURN_TEXT" == *"$phrase"* ]]; then
        DISCOVERY_HIT=1
        break
    fi
done

# Also count: HTTP 4xx/5xx, GraphQL mutation `delete*(`
if (( DISCOVERY_HIT == 0 )); then
    if [[ "$TURN_TEXT" =~ HTTP[[:space:]]*[45][0-9][0-9] ]]; then
        DISCOVERY_HIT=1
    elif [[ "$TURN_TEXT" =~ delete[A-Z][a-zA-Z]*\( ]]; then
        DISCOVERY_HIT=1
    fi
fi

shopt -u nocasematch

if (( DISCOVERY_HIT == 0 )); then
    [[ "${PREFILTER_ONLY:-0}" == "1" ]] && exit 1
    exit 0
fi

# Prefilter passed
[[ "${PREFILTER_ONLY:-0}" == "1" ]] && exit 0

# ---- Invoke classifier in background ----------------------------------------
# Detached so the user's session isn't blocked by Haiku latency.
log "prefilter passed — invoking classifier (session=$CLAUDE_SESSION_ID)"

(
    cd "$PLUGIN_ROOT"
    python3 -m pipeline.runner \
        --mode "stop" \
        --transcript "$TRANSCRIPT" \
        --session-id "${CLAUDE_SESSION_ID:-unknown}" \
        --cwd "${CLAUDE_PROJECT_DIR:-$PWD}" \
        2>>"$LOG_FILE"
) &

exit 0
```

```bash
chmod +x plugins/observe-learning-capture/hooks/stop-hook.sh
```

- [ ] **Step 4: Implement supporting `pipeline/runner.py` (CLI entry point invoked by hooks)**

Create `pipeline/runner.py`:

```python
"""CLI entry point for the pipeline. Invoked by hooks.

Modes:
- stop: classify the last assistant turn
- session-end: classify all assistant turns from the session
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pipeline.classifier import Classifier
from pipeline.dedupe import extract_existing_ids, is_duplicate
from pipeline.stage import append_candidates
from pipeline.transcript import last_assistant_turn, all_assistant_turns


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["stop", "session-end"], required=True)
    p.add_argument("--transcript", required=True)
    p.add_argument("--session-id", required=True)
    p.add_argument("--cwd", required=True)
    args = p.parse_args()

    config = _load_config()
    plugin_root = Path(__file__).parent.parent

    classifier = Classifier(
        model=config["haiku_model"],
        prompt_template_path=plugin_root / "prompts" / "classifier.md",
        observeie_md_path=Path(os.path.expanduser(config["destination_file"])),
        prompt_version=config["prompt_version"],
    )

    transcript_path = Path(args.transcript)

    if args.mode == "stop":
        turn = last_assistant_turn(transcript_path)
        if turn is None:
            return 0
        candidates = classifier.classify(
            turn_text=turn.text,
            session_id=args.session_id,
            cwd=args.cwd,
            excerpt=turn.text[:200],
        )
    else:  # session-end
        full_text = "\n\n".join(t.text for t in all_assistant_turns(transcript_path))
        if not full_text:
            return 0
        candidates = classifier.classify(
            turn_text=full_text,
            session_id=args.session_id,
            cwd=args.cwd,
            excerpt="(full session scan)",
        )

    # Dedup
    existing_ids = extract_existing_ids(
        Path(os.path.expanduser(config["destination_file"]))
    )
    novel = [c for c in candidates if not is_duplicate(c, existing_ids)]

    pending_file = Path(os.path.expanduser(config["pending_file"]))
    append_candidates(pending_file, novel)
    return 0


def _load_config() -> dict:
    plugin_root = Path(__file__).parent.parent
    config_path = plugin_root / "config.json"
    return json.loads(config_path.read_text())


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the shell test to verify prefilter behavior**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
bash tests/test_stop_hook.sh
```

Expected:
```
PASS: trivial ack rejected
PASS: Observe discovery passes
PASS: generic prose rejected
PASS: empty transcript rejected

Results: 4 passed, 0 failed
```

- [ ] **Step 6: Commit**

```bash
git add plugins/observe-learning-capture/hooks/stop-hook.sh \
        plugins/observe-learning-capture/pipeline/runner.py \
        plugins/observe-learning-capture/tests/test_stop_hook.sh
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): add Stop hook with prefilter + runner

Stop hook: cheap shell prefilter, runs classifier in background if pass.
runner.py: CLI entrypoint invoked by hooks. Backgrounded so user session
isn't blocked by Haiku latency."
```

---

## Task 10: `hooks/session-end-scan.sh` — full-session backup scan

**Files:**
- Create: `plugins/observe-learning-capture/hooks/session-end-scan.sh`

**Skipping TDD for the hook itself because:** it's a thin wrapper over runner.py (which has unit tests). The end-to-end test in Task 16 exercises this hook against a real-shaped transcript.

- [ ] **Step 1: Implement `hooks/session-end-scan.sh`**

```bash
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
```

```bash
chmod +x plugins/observe-learning-capture/hooks/session-end-scan.sh
```

- [ ] **Step 2: Commit**

```bash
git add plugins/observe-learning-capture/hooks/session-end-scan.sh
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): add SessionEnd backup-scan hook"
```

---

## Task 11: `hooks/session-start-review.sh` — surface pending candidates

**Files:**
- Create: `plugins/observe-learning-capture/hooks/session-start-review.sh`

- [ ] **Step 1: Implement `hooks/session-start-review.sh`**

```bash
#!/usr/bin/env bash
# session-start-review.sh — SessionStart hook for observe-learning-capture.
#
# At every session start, if the pending file has any candidates, emit a
# system-reminder block on stdout. Claude Code will inject this into the
# agent's context — Claude reads it and surfaces the candidates to the
# user on first prompt (per CLAUDE.md companion rule).
#
# Output goes to stdout; logs to file. Always exit 0.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
LOG_FILE="${HOME}/.claude/logs/observe-learning-capture.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [session-start-review] $*" >> "$LOG_FILE"
}

PENDING_FILE="${HOME}/.claude/agents/.observeie-pending.md"

if [[ ! -f "$PENDING_FILE" ]]; then
    exit 0  # nothing pending
fi

if [[ ! -s "$PENDING_FILE" ]]; then
    exit 0  # empty file
fi

log "pending file present — emitting review context"

# Render compact summary by parsing the YAML pending file via Python.
cd "$PLUGIN_ROOT"
python3 -c "
import os, sys
from pathlib import Path
sys.path.insert(0, '$PLUGIN_ROOT')
from pipeline.stage import read_pending

records = read_pending(Path('$PENDING_FILE'))
if not records:
    sys.exit(0)

print('=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ===')
print(f'{len(records)} candidate(s) pending review from prior sessions:')
print()
for i, r in enumerate(records, 1):
    conf = r.get('confidence', '?')
    section = r.get('proposed_section', '?')
    title = r.get('title', '(no title)')
    src = r.get('source', {})
    cwd = src.get('cwd', '?')
    cwd_short = cwd.replace(os.path.expanduser('~'), '~')
    captured_at = src.get('captured_at', '?')[:10]
    print(f'  #{i} [{conf:6}] {section}: {title}')
    print(f'       (from {cwd_short}, {captured_at})')
print()
print('I should surface these candidates to the user before responding to')
print('their first prompt. The user may reply: merge all / merge N /')
print('discard N / edit N / defer.')
print('=== END OBSERVE LEARNING CAPTURE ===')
" 2>>"$LOG_FILE" || log "review render failed"

exit 0
```

```bash
chmod +x plugins/observe-learning-capture/hooks/session-start-review.sh
```

- [ ] **Step 2: Manual smoke test**

```bash
# Set up a fake pending file
mkdir -p ~/.claude/agents
cat > ~/.claude/agents/.observeie-pending.md.test <<'EOF'
---
id: testid01
title: "Test fact"
fact: |
  Just a test fact.
proposed_section: "Test Section"
confidence: high
tags:
  - test
source:
  session_id: s1
  cwd: /tmp/foo
  captured_at: "2026-04-29T11:33:00+00:00"
  excerpt: "test excerpt"
EOF

PENDING_FILE_OVERRIDE=~/.claude/agents/.observeie-pending.md.test \
  bash plugins/observe-learning-capture/hooks/session-start-review.sh

# Cleanup
rm ~/.claude/agents/.observeie-pending.md.test
```

Expected: prints the `=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ===` block.

(The hook hardcodes the path; for the smoke test, we'd modify the hook to honor `PENDING_FILE_OVERRIDE` if set. Add this 2-line tweak to the hook before the smoke test:)

```bash
# In hooks/session-start-review.sh, replace:
#   PENDING_FILE="${HOME}/.claude/agents/.observeie-pending.md"
# With:
#   PENDING_FILE="${PENDING_FILE_OVERRIDE:-${HOME}/.claude/agents/.observeie-pending.md}"
```

- [ ] **Step 3: Apply the override-friendly tweak and commit**

```bash
# Edit hooks/session-start-review.sh to use ${PENDING_FILE_OVERRIDE:-...}
# (per Step 2 note above)

git add plugins/observe-learning-capture/hooks/session-start-review.sh
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): add SessionStart review-surfacer hook

Reads pending YAML, prints compact summary on stdout for Claude Code
to inject into agent context."
```

---

## Task 12: Slash commands

**Files:**
- Create: `plugins/observe-learning-capture/commands/observe-review.md`
- Create: `plugins/observe-learning-capture/commands/observe-capture.md`

**Skipping TDD because:** slash commands are markdown frontmatter + prose instructions to me. No code to test.

- [ ] **Step 1: Write `commands/observe-review.md`**

```markdown
---
description: Review pending Observe learning candidates from prior sessions
allowed-tools: ["Bash", "Read", "Edit", "Write"]
---

Read `~/.claude/agents/.observeie-pending.md`. If empty or missing, tell the
user "No pending candidates" and stop.

Otherwise, render the same review table the SessionStart hook produces:

```
📋 N learning candidates pending review:

  #1 [high]  OPAL Gotchas
             "OPAL rejects '7d'; use '168h'"  (EchoNet, 2026-04-29)
  #2 [med]   Object Management
             "Cascade-ordering deadlock..."   (EchoNet, 2026-04-29)
```

Then prompt: "Reply: `merge all` / `merge 1, 3` / `discard 2` / `edit 1` /
`defer`."

When the user replies:
- `merge N` or `merge all` → for each, run:
  `python3 -c "from pathlib import Path; from pipeline.merge import merge_candidate, remove_from_pending; from pipeline.stage import read_pending; ..."`
  (You can also exec `python3 -m pipeline.merge_cli --merge ID` if that
  helper exists — check Task 13.)
- `discard N` → call `remove_from_pending(id, pending_file)` only
- `edit N` → open the YAML record for editing, then re-stage
- `defer` → no action

Confirm completion with: "Merged N, discarded M, deferred K."
```

- [ ] **Step 2: Write `commands/observe-capture.md`**

```markdown
---
description: Force-capture the last turn into the pending queue (bypass prefilter)
allowed-tools: ["Bash"]
---

Run the classifier on the last assistant turn unconditionally, bypassing the
Stop-hook prefilter. Useful when you noticed a learning the prefilter would
have skipped.

```bash
python3 -m pipeline.runner \
  --mode stop \
  --transcript "${CLAUDE_TRANSCRIPT_PATH}" \
  --session-id "${CLAUDE_SESSION_ID}" \
  --cwd "$(pwd)"
```

Then read the pending file and report how many candidates were added.
```

- [ ] **Step 3: Commit**

```bash
git add plugins/observe-learning-capture/commands/
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): add /observe-review and /observe-capture"
```

---

## Task 13: `hooks/hooks.json` + merge CLI helper

**Files:**
- Create: `plugins/observe-learning-capture/hooks/hooks.json`
- Create: `plugins/observe-learning-capture/pipeline/merge_cli.py`

- [ ] **Step 1: Implement `hooks/hooks.json`**

```json
{
  "description": "observe-learning-capture hooks",
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/stop-hook.sh\""
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/session-end-scan.sh\""
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "matcher": "startup|clear|compact",
            "type": "command",
            "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/session-start-review.sh\""
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 2: Implement `pipeline/merge_cli.py`**

```python
"""CLI helper for the slash commands.

Usage:
    python3 -m pipeline.merge_cli --merge ID
    python3 -m pipeline.merge_cli --discard ID
    python3 -m pipeline.merge_cli --list
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.merge import merge_candidate, remove_from_pending
from pipeline.stage import read_pending
from pipeline.types import Candidate, ClassifierMeta, Provenance


def main() -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--merge", metavar="ID")
    g.add_argument("--discard", metavar="ID")
    g.add_argument("--list", action="store_true")
    args = p.parse_args()

    config = _load_config()
    pending_file = Path(os.path.expanduser(config["pending_file"]))
    destination_file = Path(os.path.expanduser(config["destination_file"]))

    records = read_pending(pending_file)

    if args.list:
        for r in records:
            print(f"{r['id']}\t{r['confidence']}\t{r['title']}")
        return 0

    target_id = args.merge or args.discard
    target = next((r for r in records if r.get("id") == target_id), None)
    if target is None:
        print(f"ID {target_id} not found in pending", file=sys.stderr)
        return 1

    if args.merge:
        # Reconstruct Candidate from record; merge.
        candidate = _record_to_candidate(target)
        merge_candidate(candidate, destination_file)
        remove_from_pending(target_id, pending_file)
        print(f"Merged {target_id} → {destination_file}")
    else:  # discard
        remove_from_pending(target_id, pending_file)
        print(f"Discarded {target_id}")
    return 0


def _record_to_candidate(record: dict) -> Candidate:
    src = record["source"]
    cls_meta = record.get("classifier") or {}
    return Candidate.create(
        title=record["title"],
        fact=record["fact"],
        proposed_section=record["proposed_section"],
        confidence=record["confidence"],
        tags=list(record.get("tags", [])),
        provenance=Provenance(
            session_id=src["session_id"],
            cwd=src["cwd"],
            captured_at=datetime.fromisoformat(src["captured_at"]),
            excerpt=src["excerpt"],
        ),
        classifier=ClassifierMeta(
            model=cls_meta.get("model", "unknown"),
            prompt_version=cls_meta.get("prompt_version", "1.0"),
            confidence_score=cls_meta.get("confidence_score"),
        ) if cls_meta else None,
    )


def _load_config() -> dict:
    plugin_root = Path(__file__).parent.parent
    return json.loads((plugin_root / "config.json").read_text())


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Commit**

```bash
git add plugins/observe-learning-capture/hooks/hooks.json \
        plugins/observe-learning-capture/pipeline/merge_cli.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(observe-learning-capture): wire hooks.json + add merge_cli helper

Stop, SessionEnd, SessionStart all wired. merge_cli supports --merge,
--discard, --list (used by /observe-review slash command)."
```

---

## Task 14: Add to marketplace catalog

**Files:**
- Modify: `.claude-plugin/marketplace.json` (repo root)

- [ ] **Step 1: Read current marketplace.json**

```bash
cat ~/repos/claude-plugins/.claude-plugin/marketplace.json
```

Confirm it lists `opal-optimizer` and `snowflake-brand`.

- [ ] **Step 2: Add new plugin entry**

Edit `.claude-plugin/marketplace.json`. The `plugins` array should now include a third entry:

```json
{
  "name": "chris-plugins",
  "owner": {
    "name": "Chris Milton"
  },
  "metadata": {
    "description": "Chris Milton's personal Claude Code plugins"
  },
  "plugins": [
    {
      "name": "opal-optimizer",
      "source": "./plugins/opal-optimizer",
      "description": "Autonomous OPAL query optimization loop for Observe monitors and queries"
    },
    {
      "name": "snowflake-brand",
      "source": "./plugins/snowflake-brand",
      "description": "Snowflake brand guidelines for designs, code, presentations, and marketing materials"
    },
    {
      "name": "observe-learning-capture",
      "source": "./plugins/observe-learning-capture",
      "description": "Auto-captures Observe-platform learnings from session transcripts; stages for review; promotes approved candidates into ObserveIE.md for cross-customer propagation"
    }
  ]
}
```

- [ ] **Step 3: Verify JSON is valid**

```bash
python3 -m json.tool ~/repos/claude-plugins/.claude-plugin/marketplace.json > /dev/null
```

Expected: no output (valid JSON).

- [ ] **Step 4: Commit**

```bash
git add .claude-plugin/marketplace.json
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "feat(marketplace): list observe-learning-capture plugin"
```

---

## Task 15: Companion rules in `~/.claude/CLAUDE.md`

**Files:**
- Modify: `~/.claude/CLAUDE.md` (NOT in the repo — global user config)

Two complementary rules land here in a single edit:

1. **Broader Observe-knowledge propagation rule** — discipline rule telling Claude that *when it learns something Observe-platform-general*, it should propagate it. (This is what the plugin automates, but the rule makes the human-side discipline explicit too.)
2. **SessionStart pending-review handler** — the rule that tells Claude how to handle the `=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ===` system-reminder block emitted by the plugin's SessionStart hook. Without it, the plugin's stdout would be invisible to Claude.

Both go into one combined section, inserted as a new top-level section.

- [ ] **Step 1: Read current global CLAUDE.md to find insertion point**

```bash
grep -n "## Confirmations I Always Need" ~/.claude/CLAUDE.md
```

Confirm it exists. We'll insert a new top-level section immediately before this line.

- [ ] **Step 2: Insert the combined rule section**

Use the Edit tool to insert this new section in `~/.claude/CLAUDE.md` immediately before the `## Confirmations I Always Need` line:

```markdown
---

## Observe Knowledge Propagation

Two complementary rules — one human-discipline, one plugin-handler. Both serve the same goal: Observe-platform learnings discovered in any session benefit every future session.

### Rule A — Cross-customer Observe knowledge must propagate

When you discover behavior, gotchas, mutation signatures, error patterns, OPAL syntax quirks, cascade rules, or platform constraints that are **NOT customer-specific** (would help any IE working any tenant), **append them to the appropriate section of `~/.claude/agents/ObserveIE.md` BEFORE marking the task done.**

This is a hard rule, same priority as the verification checklist. The `observe-learning-capture` plugin (when installed and operational) automates the capture, but you must still verify the appropriate section was updated as part of your end-of-task hand-off.

**Customer-specific facts** (tenant IDs, dataset names like `EchoNet/foo`, contacts, customer-named monitors) go to `~/Work/<Customer>/CLAUDE.md`, **NEVER** to ObserveIE.md.

**Non-Observe facts** (general programming patterns, framework knowledge, language quirks) go to neither — they belong in tier-1 docs or your own notes.

### Rule B — Pending-review handling

The `observe-learning-capture` plugin auto-stages Observe-platform learnings in `~/.claude/agents/.observeie-pending.md`. At every session start, its SessionStart hook emits an `=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ===` block into your context.

**When you see that block:**

1. Surface the candidate table to me **before responding to my first prompt**. Use this format:
   ```
   📋 N learning candidates pending review:
     #1 [high]  Section: "Title"  (cwd, date)
     #2 [med]   Section: "Title"  (cwd, date)

   Reply: `merge all` / `merge 1, 3` / `discard 2` / `edit 1` / `defer`
   ```
2. **Wait for my response** before proceeding with my original prompt.
3. Parse my response loosely — `merge all`, `merge 1`, `discard 2`, `edit 1`, `defer`, or combinations.
4. For each merge: run `python3 -m pipeline.merge_cli --merge ID` from the plugin directory. For discards: `--discard ID`. For edits: open the YAML record, let me edit, then `--merge`.
5. After processing, confirm: "Merged N, discarded M, deferred K."
6. Then proceed with my original prompt.

**Hard constraints:**
- **NEVER auto-merge** without my explicit approval. The plugin proposes; I dispose.
- If a candidate looks customer-specific, recommend `discard` or `edit` to redirect — per Rule A above.
- If I say `defer`, leave the queue alone — surfaces again next session.

**On `/observe-review` slash command:** same flow, but I invoked it explicitly mid-session.

**On `/observe-capture` slash command:** force-runs the classifier on the last turn (bypass prefilter); reports back how many new candidates were staged.

---
```

(The Edit tool call uses the existing `## Confirmations I Always Need` line as the `old_string` anchor and prepends the new section.)

- [ ] **Step 3: Verify the rule is in place**

```bash
grep -n "Observe Learning Capture" ~/.claude/CLAUDE.md
```

Expected: shows the section heading line.

- [ ] **Step 4: Note — this CLAUDE.md change is NOT committed to the plugin repo**

It's in the user's global `~/.claude/`, not in `~/repos/claude-plugins/`. The plugin's `docs/design.md §11` already documents the rule; this step lands the actual companion text where Claude will read it.

No commit step here. (If you version-control your `~/.claude/`, commit there separately.)

---

## Task 16: End-to-end integration test

**Files:**
- Create: `plugins/observe-learning-capture/tests/test_e2e.py`

- [ ] **Step 1: Write the e2e test**

Create `tests/test_e2e.py`:

```python
"""End-to-end test: synthesize a session transcript, run the runner with a
mocked classifier, verify pending file state and ObserveIE.md state after
simulated approval flow.

Does NOT actually call Haiku — that's mocked. The point is to verify the
glue between transcript → classifier → dedupe → stage → merge.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pipeline.classifier import Classifier
from pipeline.dedupe import extract_existing_ids, is_duplicate
from pipeline.merge import merge_candidate, remove_from_pending
from pipeline.stage import append_candidates, read_pending
from pipeline.transcript import last_assistant_turn
from pipeline.types import Candidate, ClassifierMeta, Provenance


SAMPLE_HAIKU_OUTPUT = """\
- title: "Cascade deadlock on Tracing/Span"
  fact: |
    deleteDatastream on Tracing/Span fails because managed datasets
    reference each other; no force flag exists.
  proposed_section: "Object Management and Cleanup"
  confidence: high
  tags: [delete, cascade, tracing]
  classifier_confidence_score: 0.91
"""


class TestEndToEnd(unittest.TestCase):
    @mock.patch("pipeline.classifier._invoke_haiku")
    def test_full_pipeline(self, mock_invoke):
        mock_invoke.return_value = SAMPLE_HAIKU_OUTPUT
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            transcript = tmp / "session.jsonl"
            transcript.write_text(
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{
                            "type": "text",
                            "text": (
                                "I tried deleteDatastream(id:42767020) and got "
                                "a cascade deadlock — the managed Tracing/* "
                                "datasets reference each other. No force flag."
                            ),
                        }],
                    },
                    "uuid": "a1",
                    "timestamp": "2026-04-29T11:33:00Z",
                }) + "\n",
                encoding="utf-8",
            )
            obs = tmp / "ObserveIE.md"
            obs.write_text("# ObserveIE\n", encoding="utf-8")
            pending = tmp / ".pending.md"
            prompt = (
                Path(__file__).parent.parent / "prompts" / "classifier.md"
            )

            # Stop-mode classify
            classifier = Classifier(
                model="m", prompt_template_path=prompt, observeie_md_path=obs,
            )
            turn = last_assistant_turn(transcript)
            self.assertIsNotNone(turn)
            cands = classifier.classify(
                turn_text=turn.text, session_id="s", cwd="/tmp",
            )
            self.assertEqual(len(cands), 1)

            # Dedup + stage
            existing = extract_existing_ids(obs)
            novel = [c for c in cands if not is_duplicate(c, existing)]
            append_candidates(pending, novel)

            records = read_pending(pending)
            self.assertEqual(len(records), 1)

            # Simulate approval — merge
            merge_candidate(novel[0], obs)
            remove_from_pending(novel[0].id, pending)

            content = obs.read_text(encoding="utf-8")
            self.assertIn("## Object Management and Cleanup", content)
            self.assertIn("deleteDatastream", content)
            self.assertIn(f"<!-- id:{novel[0].id}", content)

            # Pending should be empty now
            self.assertEqual(read_pending(pending), [])

            # Re-running with same Haiku output should now dedup
            existing_after = extract_existing_ids(obs)
            cands2 = classifier.classify(
                turn_text=turn.text, session_id="s2", cwd="/tmp",
            )
            novel2 = [c for c in cands2 if not is_duplicate(c, existing_after)]
            self.assertEqual(novel2, [], "Same fact must not re-stage after merge")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the e2e test**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest tests.test_e2e -v
```

Expected: `Ran 1 test in 0.0Xs — OK`

- [ ] **Step 3: Run the full test suite**

```bash
python3 -m unittest discover tests -v
```

Expected: ALL tests pass. Note the count.

- [ ] **Step 4: Commit**

```bash
git add plugins/observe-learning-capture/tests/test_e2e.py
git -c user.name="Chris Milton" -c user.email="chris.milton@observeinc.com" \
  commit -m "test(observe-learning-capture): add end-to-end pipeline test

Mocks Haiku, exercises transcript → classifier → dedupe → stage → merge.
Verifies dedup-after-merge prevents re-staging of same fact."
```

---

## Task 17: Verification per global CLAUDE.md checklist

This task is the "verification before claiming done" gate from `~/.claude/CLAUDE.md`. Don't claim plugin done until ALL four boxes check.

- [ ] **Step 1: Tests green**

```bash
cd ~/repos/claude-plugins/plugins/observe-learning-capture
python3 -m unittest discover tests -v 2>&1 | tail -20
bash tests/test_stop_hook.sh
```

Paste both outputs as evidence. Note test names that ran.

- [ ] **Step 2: Linter clean**

Python lint (ruff if installed, else stdlib `python -m py_compile`):

```bash
# If ruff is installed:
ruff check pipeline/ tests/

# Always works (stdlib):
python3 -m py_compile pipeline/*.py tests/*.py
```

Shell lint:

```bash
shellcheck hooks/*.sh tests/test_stop_hook.sh
```

If shellcheck not installed: `brew install shellcheck` (this IS a dep install — confirm with user before running).

Paste lint output (no errors expected).

- [ ] **Step 3: Manual exercise (the hard one)**

Install the plugin in your live Claude Code:

```bash
# Add the local repo as a dev marketplace (if not already)
claude  # then in Claude Code:
# /plugin marketplace add ~/repos/claude-plugins
# /plugin install observe-learning-capture@chris-plugins
```

Then in a fresh Claude Code session in `~/Work/EchoNet/`:

1. Have a conversation that includes a synthetic Observe learning, e.g.:
   "I just tried deleteDatastream(id:99999) and got a 'cannot delete with
   active poller' error — had to delete the poller first."
2. After Claude responds, wait ~5 seconds (background classifier).
3. Check `~/.claude/agents/.observeie-pending.md` — should contain a candidate.
4. Run `/exit` to fire SessionEnd.
5. Start a new Claude Code session in any cwd.
6. Verify Claude surfaces the candidate on first prompt per the CLAUDE.md rule.
7. Reply `merge all`. Verify ObserveIE.md is updated.
8. Verify pending file is empty.

Paste evidence: screenshot or copy/paste of:
- Pending file contents after step 3
- Session-start review prompt from step 6
- ObserveIE.md diff after step 7
- Empty pending file after step 8

- [ ] **Step 4: Logs reviewed**

```bash
tail -100 ~/.claude/logs/observe-learning-capture.log
```

Scan for any WARN/ERROR. Document any that appeared and whether they're benign (expected, e.g., empty transcript on cold start) or need fixing.

- [ ] **Step 5: Commit verification log if anything was tweaked**

If Step 3 surfaced bugs and you fixed them, commit those fixes. If everything worked first try, no commit needed for this task.

---

## Task 18: Open PR

- [ ] **Step 1: Push branch**

⚠️ **Per CLAUDE.md, this requires explicit user confirmation.** Confirm with user before running.

```bash
cd ~/repos/claude-plugins
git push -u origin feat/observe-learning-capture
```

- [ ] **Step 2: Open PR**

If `gh` is installed:

```bash
gh pr create --title "feat: observe-learning-capture plugin" --body "$(cat <<'EOF'
## Summary
- Auto-captures Observe-platform learnings from session transcripts
- Stages candidates in `~/.claude/agents/.observeie-pending.md`; surfaces at next session start for human review
- Merges approved candidates into `~/.claude/agents/ObserveIE.md` for cross-customer propagation
- Companion Hard Rule landed in `~/.claude/CLAUDE.md` (separate, not in this repo)

## Test plan
- [x] All Python unit tests pass (`python3 -m unittest discover tests -v`)
- [x] Shell prefilter tests pass (`bash tests/test_stop_hook.sh`)
- [x] End-to-end test (mocked Haiku) passes
- [x] Manual exercise — installed locally, ran a real session, verified pending file → SessionStart prompt → merge round-trip
- [x] No lint errors (ruff + shellcheck)

## Costs
~$0.10–$0.15 per session in Haiku tokens. ~$15–25/month at typical usage.

## Risks / limitations
- v1 is Observe-only. Multi-domain framework deferred (see `docs/design.md §13`).
- False positives possible — that's why review is required, not auto-merge.
- Prefilter is heuristic; may have false negatives. SessionEnd backup catches these.

## See also
- `plugins/observe-learning-capture/docs/design.md` — full design spec
- `plugins/observe-learning-capture/docs/implementation-plan-2026-04-29.md` — TDD plan
EOF
)"
```

If `gh` not installed: paste the same body manually into a GitHub PR.

- [ ] **Step 3: Update plan tracking**

After PR opens, mark task #29 (Implement auto-capture plugin) complete in TaskList.

---

## Self-review (against spec sections)

Cross-checked plan ↔ spec to verify coverage:

| Spec § | Topic | Plan task(s) covering it |
|---|---|---|
| §2 Goals | Auto-capture, stage-not-merge, /compact-survival, cross-customer, cheap, auditable | All ✅ — Tasks 8 (auto), 5+15 (stage), 13 (PreCompact via SessionEnd matcher), 6 (writes to global ObserveIE.md), 8 (prefilter cuts cost), 2 (provenance fields) |
| §4 Architecture | Diagram | Tasks 9, 10, 11 (the three hooks); 8 (classifier); 4-6 (pipeline) |
| §5.1 stop-hook prefilter | Vocab + discovery verbs | Task 9 step 3 ✅ |
| §5.2 classifier | Inputs, marker on failure | Task 8 ✅ |
| §5.3 dedupe | Hash + near-dup | Task 4 ✅ |
| §5.4 stage | YAML append, flock | Task 5 ✅ |
| §5.5 merge | Section routing, HTML comment | Task 6 ✅ |
| §5.6 /observe-review | | Task 12 ✅ |
| §5.7 /observe-capture | | Task 12 ✅ |
| §5.8 config.json | | Task 1 ✅ |
| §7 Schema | YAML record fields | Task 2 (types) + Task 5 (stage) ✅ |
| §8 Approval UX | merge/discard/edit/defer | Task 15 (CLAUDE.md rule) + Task 12 (slash command) ✅ |
| §9 Error handling | All 7 failure modes | Task 8 (Haiku/parse failures), Task 5 (flock), Task 11 (missing pending), Tasks 9/10 (transcript missing) ✅ |
| §10 Testing | Unit + integration + manual | Tasks 2-8 (units), Task 16 (integration), Task 17 (manual) ✅ |
| §11 CLAUDE.md companion rule | | Task 15 ✅ |
| §12 Cost analysis | | Acknowledged, no implementation needed |

**No gaps identified.**

---

## Execution Handoff

**Plan complete and saved to** `plugins/observe-learning-capture/docs/implementation-plan-2026-04-29.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Tasks 2-8 are independent enough that some could run in parallel.

**2. Inline Execution** — Execute tasks sequentially in this session using `superpowers:executing-plans`, batch execution with checkpoints.

**Which approach?**
