"""Regression test: _log_merge must only write audit entries for canonical
production merges, not test-fixture merges into temp directories.

Background: _log_merge writes to ~/.claude/logs/observe-learning-capture.log
regardless of merge target. Tests that exercise merge_candidate() with a
tempfile.TemporaryDirectory() target produce MERGE audit lines in the
user's real production log file, polluting it with thousands of
test-fixture session=s entries (observed in this session 2026-05-11,
9 spurious entries from one test-suite run).

The fix: skip the audit log write when the merge target is outside the
canonical production directory ($HOME/.claude/agents/). Tests use temp
paths and don't need audit logs; production merges use $HOME/.claude/agents/
ObserveIE.md and DO need them.
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pipeline.types import Candidate, Provenance


def _make_test_candidate(fact_text: str = "test fact for log scope") -> Candidate:
    """Build a simple Candidate suitable for exercising merge_candidate's
    audit-log path. The fact text is varied so candidate.id is unique
    per call.
    """
    return Candidate.create(
        title="Test title",
        fact=fact_text,
        proposed_section="OPAL Gotchas",
        confidence="high",
        tags=["test"],
        provenance=Provenance(
            session_id="test-session",
            cwd="/test",
            captured_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc),
            excerpt="excerpt",
        ),
    )


class TestLogMergeScope(unittest.TestCase):
    """The audit log is reserved for production merges. Tests that target
    a tempdir must not pollute it.
    """

    def test_log_merge_skips_when_target_outside_canonical_agents_dir(self):
        """Merge target inside a tempdir (not ~/.claude/agents/) must NOT
        produce an audit log entry. The whole point of the fix.
        """
        from pipeline.merge import _log_merge

        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            fake_home.mkdir()
            fake_log_dir = fake_home / ".claude" / "logs"
            fake_log_dir.mkdir(parents=True)
            fake_log_path = fake_log_dir / "observe-learning-capture.log"

            # Target is inside the tempdir (NOT under fake_home/.claude/agents/).
            non_canonical_target = Path(td) / "ObserveIE.md"

            with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
                _log_merge(_make_test_candidate(), non_canonical_target)

            # The log file must NOT have been created. _log_merge is the
            # only thing that would have created it under fake_home, so
            # its non-existence is a clean signal.
            self.assertFalse(
                fake_log_path.exists(),
                f"Audit log was created for non-canonical target. Contents: "
                f"{fake_log_path.read_text() if fake_log_path.exists() else '(none)'}",
            )

    def test_log_merge_writes_when_target_is_in_canonical_dir(self):
        """Merge target inside $HOME/.claude/agents/ MUST produce an audit
        log entry. The fix must not regress the production logging behavior.
        """
        from pipeline.merge import _log_merge

        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            fake_home.mkdir()
            fake_log_dir = fake_home / ".claude" / "logs"
            fake_log_dir.mkdir(parents=True)
            fake_log_path = fake_log_dir / "observe-learning-capture.log"

            canonical_dir = fake_home / ".claude" / "agents"
            canonical_dir.mkdir(parents=True)
            canonical_target = canonical_dir / "ObserveIE.md"
            canonical_target.write_text("# ObserveIE\n", encoding="utf-8")

            candidate = _make_test_candidate()

            with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
                _log_merge(candidate, canonical_target)

            self.assertTrue(
                fake_log_path.exists(),
                "Audit log MUST be created for canonical-dir merge target",
            )
            log_content = fake_log_path.read_text(encoding="utf-8")
            self.assertIn("action=MERGE", log_content)
            self.assertIn(candidate.id, log_content)
            self.assertIn(str(canonical_target), log_content)

    def test_log_merge_handles_nested_canonical_target(self):
        """A target nested DEEPER inside $HOME/.claude/agents/ (e.g. a
        per-customer subdirectory) still counts as canonical and logs.
        """
        from pipeline.merge import _log_merge

        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            fake_home.mkdir()
            fake_log_dir = fake_home / ".claude" / "logs"
            fake_log_dir.mkdir(parents=True)
            fake_log_path = fake_log_dir / "observe-learning-capture.log"

            # Nested target: ~/.claude/agents/customer-X/ObserveIE.md
            nested_dir = fake_home / ".claude" / "agents" / "customer-X"
            nested_dir.mkdir(parents=True)
            nested_target = nested_dir / "ObserveIE.md"
            nested_target.write_text("# ObserveIE\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
                _log_merge(_make_test_candidate(), nested_target)

            self.assertTrue(
                fake_log_path.exists(),
                "Audit log MUST also be written for nested canonical targets",
            )


if __name__ == "__main__":
    unittest.main()
