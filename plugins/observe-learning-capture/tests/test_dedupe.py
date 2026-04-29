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
        # Ids must match the real SHA-256 content hashes written into the fixture.
        # See tests/fixtures/sample_observeie.md for the canonical values.
        ids = extract_existing_ids(FIXTURE)
        self.assertEqual(ids, {"31ef4a55", "c5df3252", "7f3d4ae2"})

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

    def test_no_warning_comparing_candidate_against_itself(self):
        """Self-comparison guard: candidate compared against [candidate] returns None."""
        c = _candidate("Some fact about OPAL", tags=["opal"])
        warning = near_duplicate_warning(c, [c])
        self.assertIsNone(warning)

    def test_no_warning_when_candidate_has_no_tags(self):
        """Tagless candidate cannot near-duplicate (early return)."""
        c = _candidate("Tagless fact", tags=[])
        existing = [_candidate("Other fact", tags=["opal", "syntax"])]
        self.assertIsNone(near_duplicate_warning(c, existing))

    def test_near_duplicate_works_with_mixed_case_tags(self):
        """Tag normalization (since T04 fix) means OPAL and opal both match.
        After Q4 fix, tags are stored lowercase; this test confirms that
        and that comparison would still work even if tags weren't normalized.
        """
        # Both candidates use canonical lowercase tags after normalization
        c1 = _candidate("Fact 1", tags=["OPAL", "Syntax"])  # input mixed case
        c2 = _candidate("Fact 2", tags=["opal", "syntax", "extra"])
        # After Candidate.create's normalization, c1.tags should be lowercase
        self.assertEqual(c1.tags, ["opal", "syntax"])
        # And the warning still fires
        warning = near_duplicate_warning(c1, [c2])
        self.assertIsNotNone(warning)


if __name__ == "__main__":
    unittest.main()
