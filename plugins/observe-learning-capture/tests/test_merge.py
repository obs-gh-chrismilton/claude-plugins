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
