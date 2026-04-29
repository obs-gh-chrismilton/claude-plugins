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
