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


class TestStageEdgeCases(unittest.TestCase):
    def test_fact_with_triple_dash_does_not_corrupt_record(self):
        """C1: '---' inside a scalar value must not split records."""
        with tempfile.TemporaryDirectory() as d:
            pending = Path(d) / ".pending.md"
            c1 = _candidate("Use --- for markdown horizontal rules in OPAL")
            c2 = _candidate("second fact unrelated")
            append_candidates(pending, [c1, c2])
            records = read_pending(pending)
            self.assertEqual(len(records), 2)
            self.assertIn("Use ---", records[0]["fact"])
            self.assertEqual(records[1]["fact"], "second fact unrelated")

    def test_empty_tags_round_trip_correctly(self):
        """C2: tags=[] must survive write-read as []."""
        with tempfile.TemporaryDirectory() as d:
            pending = Path(d) / ".pending.md"
            c = Candidate.create(
                title="t", fact="fact", proposed_section="X",
                confidence="high", tags=[],  # empty
                provenance=Provenance(
                    session_id="s", cwd="/tmp",
                    captured_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
                    excerpt="e",
                ),
            )
            append_candidates(pending, [c])
            records = read_pending(pending)
            self.assertEqual(records[0]["tags"], [])

    def test_full_round_trip_through_from_yaml_record(self):
        """S1 + I3: write a candidate, read records, reconstruct via from_yaml_record.
        Verifies all fields including prompt_version (string), confidence_score (float),
        cwd (path-like string), proposed_section (with space).
        """
        with tempfile.TemporaryDirectory() as d:
            pending = Path(d) / ".pending.md"
            c1 = _candidate("Test fact for round-trip")
            append_candidates(pending, [c1])
            records = read_pending(pending)
            c2 = Candidate.from_yaml_record(records[0])
            # Strict equality on all fields
            self.assertEqual(c2.id, c1.id)
            self.assertEqual(c2.title, c1.title)
            self.assertEqual(c2.fact, c1.fact)
            self.assertEqual(c2.proposed_section, c1.proposed_section)
            self.assertEqual(c2.confidence, c1.confidence)
            self.assertEqual(c2.tags, c1.tags)
            self.assertEqual(c2.provenance.session_id, c1.provenance.session_id)
            self.assertEqual(c2.provenance.cwd, c1.provenance.cwd)
            self.assertEqual(c2.provenance.captured_at, c1.provenance.captured_at)
            # I3: prompt_version must remain str, not become float
            self.assertEqual(c2.classifier.prompt_version, c1.classifier.prompt_version)
            self.assertIsInstance(c2.classifier.prompt_version, str)
            self.assertEqual(c2.classifier.confidence_score, c1.classifier.confidence_score)

    def test_empty_value_key_parses_as_none(self):
        """I4: a key with empty value (e.g., 'session_id:\\n') parses as None, not {}."""
        from pipeline.stage import _parse_yaml_list
        content = "---\nkey1: value1\nkey2:\nkey3: value3\n"
        records = _parse_yaml_list(content)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["key1"], "value1")
        self.assertIsNone(records[0]["key2"])
        self.assertEqual(records[0]["key3"], "value3")

    def test_inline_list_parses_correctly(self):
        """Q2 fix: stage._parse_yaml_block handles inline [a, b] lists."""
        from pipeline.stage import _parse_yaml_list
        content = """---
key: value
items: [a, b, c]
quoted: ["x", "y"]
empty: []
"""
        records = _parse_yaml_list(content)
        self.assertEqual(records[0]["items"], ["a", "b", "c"])
        self.assertEqual(records[0]["quoted"], ["x", "y"])
        self.assertEqual(records[0]["empty"], [])


if __name__ == "__main__":
    unittest.main()
