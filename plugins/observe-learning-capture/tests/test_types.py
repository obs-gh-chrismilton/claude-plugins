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

    # ---- C1: from_yaml_record round-trip ----
    def test_from_yaml_record_round_trips(self):
        """Candidate → to_yaml_record → from_yaml_record reconstructs."""
        c1 = Candidate.create(
            title="OPAL time literal",
            fact="OPAL rejects '7d'.",
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
        record = c1.to_yaml_record()
        c2 = Candidate.from_yaml_record(record)
        self.assertEqual(c2.id, c1.id)
        self.assertEqual(c2.fact, c1.fact)
        self.assertEqual(c2.tags, c1.tags)
        self.assertEqual(c2.classifier.model, c1.classifier.model)
        self.assertEqual(c2.provenance.session_id, c1.provenance.session_id)

    def test_from_yaml_record_handles_missing_classifier(self):
        """Records without `classifier` block reconstruct cleanly."""
        c1 = Candidate.create(
            title="t", fact="f", proposed_section="X",
            confidence="medium", tags=[], provenance=_dummy_provenance(),
        )
        record = c1.to_yaml_record()
        self.assertNotIn("classifier", record)  # confirms omission per I3
        c2 = Candidate.from_yaml_record(record)
        self.assertIsNone(c2.classifier)

    # ---- C2: __post_init__ guards direct construction ----
    def test_direct_construction_with_bad_confidence_raises(self):
        """Even bypassing create(), __post_init__ catches bad confidence."""
        with self.assertRaises(ValueError):
            Candidate(
                id="abc12345",
                title="t",
                fact="f",
                proposed_section="X",
                confidence="bogus",  # invalid
                tags=[],
                provenance=_dummy_provenance(),
            )

    # ---- I1: tags defensive copy ----
    def test_tags_are_defensively_copied(self):
        """Caller mutating their tag list after creation must not affect candidate."""
        original_tags = ["opal", "syntax"]
        c = Candidate.create(
            title="t", fact="f", proposed_section="X",
            confidence="high", tags=original_tags,
            provenance=_dummy_provenance(),
        )
        original_tags.append("INJECTED")
        self.assertNotIn("INJECTED", c.tags)

    # ---- Q4: tag normalization at construction ----
    def test_tags_normalized_to_lowercase_at_creation(self):
        """Q4 fix: tags are stored canonical lowercase regardless of input case."""
        c = Candidate.create(
            title="t", fact="f", proposed_section="X",
            confidence="high", tags=["OPAL", "Syntax-Quirks"],
            provenance=_dummy_provenance(),
        )
        self.assertEqual(c.tags, ["opal", "syntax-quirks"])

    def test_from_yaml_record_normalizes_tags(self):
        """Defensive: even if YAML on disk had mixed case, deserializer normalizes."""
        record = {
            "id": "abc12345",
            "title": "t", "fact": "f",
            "proposed_section": "X",
            "confidence": "high",
            "tags": ["OPAL", "OPAL"],  # mixed case + dup
            "source": {
                "session_id": "s", "cwd": "/tmp",
                "captured_at": "2026-04-29T11:33:00+00:00",
                "excerpt": "e",
            },
        }
        c = Candidate.from_yaml_record(record)
        self.assertEqual(c.tags, ["opal", "opal"])


class TestClassifierMeta(unittest.TestCase):
    # ---- I2: ClassifierMeta.from_dict ----
    def test_from_dict_round_trips(self):
        m1 = ClassifierMeta(
            model="claude-haiku-4-5-20251001",
            prompt_version="1.0",
            confidence_score=0.88,
        )
        d = m1.to_dict()
        m2 = ClassifierMeta.from_dict(d)
        self.assertEqual(m2.model, m1.model)
        self.assertEqual(m2.confidence_score, m1.confidence_score)

    def test_from_dict_omits_optional_score(self):
        d = {"model": "m", "prompt_version": "1.0"}
        m = ClassifierMeta.from_dict(d)
        self.assertIsNone(m.confidence_score)

    # ---- I4: confidence_score range ----
    def test_confidence_score_above_one_raises(self):
        with self.assertRaises(ValueError):
            ClassifierMeta(
                model="m", prompt_version="1.0",
                confidence_score=1.5,
            )

    def test_confidence_score_below_zero_raises(self):
        with self.assertRaises(ValueError):
            ClassifierMeta(
                model="m", prompt_version="1.0",
                confidence_score=-0.1,
            )

    def test_confidence_score_at_bounds_ok(self):
        ClassifierMeta(model="m", prompt_version="1.0", confidence_score=0.0)
        ClassifierMeta(model="m", prompt_version="1.0", confidence_score=1.0)


def _dummy_provenance():
    return Provenance(
        session_id="test-session",
        cwd="/tmp/test",
        captured_at=datetime(2026, 4, 29, 11, 33, tzinfo=timezone.utc),
        excerpt="test excerpt",
    )


if __name__ == "__main__":
    unittest.main()
