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
