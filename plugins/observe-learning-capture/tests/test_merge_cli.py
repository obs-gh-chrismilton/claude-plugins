"""Tests for pipeline.merge_cli — slash-command helper."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pipeline.merge_cli import _record_to_candidate, main
from pipeline.stage import append_candidates
from pipeline.types import Candidate, ClassifierMeta, Provenance


def _candidate(fact: str = "fact") -> Candidate:
    return Candidate.create(
        title="t", fact=fact, proposed_section="OPAL Gotchas",
        confidence="high", tags=["opal"],
        provenance=Provenance(
            session_id="s", cwd="/tmp",
            captured_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
            excerpt="e",
        ),
        classifier=ClassifierMeta(model="m", prompt_version="1.0", confidence_score=0.9),
    )


class TestMergeCli(unittest.TestCase):
    def test_record_to_candidate_round_trip(self):
        c1 = _candidate()
        record = c1.to_yaml_record()
        c2 = _record_to_candidate(record)
        self.assertEqual(c1.id, c2.id)
        self.assertEqual(c1.fact, c2.fact)

    def test_merge_cli_merge(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            obs = d / "ObserveIE.md"
            obs.write_text("# ObserveIE\n", encoding="utf-8")
            pending = d / ".pending.md"
            c = _candidate()
            append_candidates(pending, [c])

            config = {
                "destination_file": str(obs),
                "pending_file": str(pending),
            }
            with mock.patch("pipeline.merge_cli._load_config", return_value=config):
                with mock.patch.object(
                    sys, "argv", ["merge_cli.py", "--merge", c.id]
                ):
                    rc = main()
                    self.assertEqual(rc, 0)

            content = obs.read_text(encoding="utf-8")
            self.assertIn(f"<!-- id:{c.id}", content)
            # Pending should now be empty
            self.assertEqual(pending.read_text(encoding="utf-8").strip(), "")

    def test_merge_cli_discard(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            obs = d / "ObserveIE.md"
            pending = d / ".pending.md"
            c = _candidate("to be discarded")
            append_candidates(pending, [c])

            config = {
                "destination_file": str(obs),
                "pending_file": str(pending),
            }
            with mock.patch("pipeline.merge_cli._load_config", return_value=config):
                with mock.patch.object(
                    sys, "argv", ["merge_cli.py", "--discard", c.id]
                ):
                    rc = main()
                    self.assertEqual(rc, 0)

            self.assertFalse(obs.exists())  # never written
            self.assertEqual(pending.read_text(encoding="utf-8").strip(), "")

    def test_merge_cli_list(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            pending = d / ".pending.md"
            c1 = _candidate("first")
            c2 = _candidate("second")
            append_candidates(pending, [c1, c2])

            config = {
                "destination_file": str(d / "ObserveIE.md"),
                "pending_file": str(pending),
            }
            with mock.patch("pipeline.merge_cli._load_config", return_value=config):
                with mock.patch.object(
                    sys, "argv", ["merge_cli.py", "--list"]
                ):
                    rc = main()
                    self.assertEqual(rc, 0)

    def test_merge_cli_unknown_id_returns_1(self):
        with tempfile.TemporaryDirectory() as d:
            pending = Path(d) / ".pending.md"
            c = _candidate()
            append_candidates(pending, [c])
            config = {
                "destination_file": str(Path(d) / "Obs.md"),
                "pending_file": str(pending),
            }
            with mock.patch("pipeline.merge_cli._load_config", return_value=config):
                with mock.patch.object(
                    sys, "argv", ["merge_cli.py", "--merge", "deadbeef"]
                ):
                    rc = main()
                    self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
