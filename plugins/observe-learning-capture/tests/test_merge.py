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

    def test_merge_bullet_parses_through_dedupe(self):
        """Format-contract test: merge writes a bullet that dedupe.py reads back."""
        from pipeline.dedupe import extract_existing_ids
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            c = _candidate("Some platform fact")
            merge_candidate(c, obs)
            ids = extract_existing_ids(obs)
            self.assertIn(c.id, ids,
                "Bullet written by merge.py must be parseable by dedupe.py's regex")

    def test_merge_sanitizes_html_comments_in_fact(self):
        """C1: fact containing <!-- id:fake1234 captured:2026-01-01 --> must NOT
        be parseable by dedupe.py as containing 'fake1234'.
        """
        from pipeline.dedupe import extract_existing_ids
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            c = _candidate(
                "The plugin emits <!-- id:fake1234 captured:2026-01-01 --> as id annotation"
            )
            merge_candidate(c, obs)
            ids = extract_existing_ids(obs)
            self.assertIn(c.id, ids)
            self.assertNotIn("fake1234", ids,
                "Embedded HTML comment in fact must not be extracted as a real id")

    def test_merge_strips_leading_bullet_marker_from_fact(self):
        """I1: fact starting with '- ' must not produce '- - text' bullet."""
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            c = _candidate("- already-a-bullet text")
            merge_candidate(c, obs)
            content = obs.read_text(encoding="utf-8")
            self.assertNotIn("- - already-a-bullet", content)
            self.assertIn("- already-a-bullet", content)

    def test_merge_writes_audit_log(self):
        """C2: spec §5.5 step 6 — audit log entry written."""
        import os
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            c = _candidate("Audit test fact")
            # merge_candidate writes to the real ~/.claude/logs/ path.
            # This is acceptable: the log is append-only and never read by
            # the test suite as a control path. We only verify it was written.
            merge_candidate(c, obs)
            real_log = Path(os.path.expanduser("~/.claude/logs/observe-learning-capture.log"))
            self.assertTrue(real_log.exists(),
                "Audit log file should exist after merge")
            content = real_log.read_text(encoding="utf-8")
            self.assertIn(f"id={c.id}", content)
            self.assertIn("action=MERGE", content)


if __name__ == "__main__":
    unittest.main()
