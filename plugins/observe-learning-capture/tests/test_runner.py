"""Tests for pipeline.runner — CLI orchestration glue."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TestRunner(unittest.TestCase):
    def test_config_load_failure_returns_1(self):
        """Config file missing/malformed → main() returns 1 with stderr log."""
        from pipeline.runner import _load_config
        # _load_config raises by design; main() should catch and return 1.
        # We test main() with an invalid config_path indirectly.
        with mock.patch("pipeline.runner._load_config") as mock_cfg:
            mock_cfg.side_effect = json.JSONDecodeError("test", "", 0)
            with mock.patch.object(
                sys, "argv",
                ["runner.py", "--mode", "stop", "--transcript", "/tmp/x",
                 "--session-id", "s", "--cwd", "/tmp"]
            ):
                from pipeline.runner import main
                rc = main()
                self.assertEqual(rc, 1)

    def test_stop_mode_no_turn_returns_0_no_stage(self):
        """If transcript has no assistant turn, runner returns 0 and stages nothing."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            transcript = d / "session.jsonl"
            transcript.write_text("", encoding="utf-8")  # empty
            obs = d / "ObserveIE.md"
            obs.write_text("# ObserveIE\n", encoding="utf-8")
            pending = d / ".pending.md"

            config = {
                "destination_file": str(obs),
                "pending_file": str(pending),
                "haiku_model": "m",
                "prompt_version": "1.0",
            }
            with mock.patch("pipeline.runner._load_config", return_value=config):
                with mock.patch.object(
                    sys, "argv",
                    ["runner.py", "--mode", "stop",
                     "--transcript", str(transcript),
                     "--session-id", "s", "--cwd", "/tmp"]
                ):
                    from pipeline.runner import main
                    rc = main()
                    self.assertEqual(rc, 0)
            self.assertFalse(pending.exists(), "no candidates should mean no pending file")

    def test_stop_mode_stages_novel_candidate(self):
        """Successful classify → novel candidate appended to pending."""
        from pipeline.types import Candidate, Provenance
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            transcript = d / "session.jsonl"
            transcript.write_text(
                json.dumps({
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [
                        {"type": "text", "text": "OPAL platform discovery."}
                    ]},
                    "uuid": "a1", "timestamp": "2026-04-29T11:00:00Z",
                }) + "\n", encoding="utf-8")
            obs = d / "ObserveIE.md"
            obs.write_text("# ObserveIE\n", encoding="utf-8")
            pending = d / ".pending.md"

            mock_candidate = Candidate.create(
                title="t", fact="novel fact", proposed_section="X",
                confidence="high", tags=["opal"],
                provenance=Provenance(
                    session_id="s", cwd="/tmp",
                    captured_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
                    excerpt="e",
                ),
            )

            config = {
                "destination_file": str(obs),
                "pending_file": str(pending),
                "haiku_model": "m",
                "prompt_version": "1.0",
            }
            with mock.patch("pipeline.runner._load_config", return_value=config), \
                 mock.patch("pipeline.classifier.Classifier.classify",
                            return_value=[mock_candidate]):
                with mock.patch.object(
                    sys, "argv",
                    ["runner.py", "--mode", "stop",
                     "--transcript", str(transcript),
                     "--session-id", "s", "--cwd", "/tmp"]
                ):
                    from pipeline.runner import main
                    rc = main()
                    self.assertEqual(rc, 0)
            self.assertTrue(pending.exists())

    def test_dedup_includes_pending_file(self):
        """Q9 fix: dedup checks BOTH destination AND pending file."""
        from pipeline.types import Candidate, Provenance
        from pipeline.stage import append_candidates
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            transcript = d / "session.jsonl"
            transcript.write_text(
                json.dumps({
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [
                        {"type": "text", "text": "Some assistant text."}
                    ]},
                    "uuid": "a1", "timestamp": "2026-04-29T11:00:00Z",
                }) + "\n", encoding="utf-8")
            obs = d / "ObserveIE.md"
            obs.write_text("# ObserveIE\n", encoding="utf-8")
            pending = d / ".pending.md"

            # Stage a candidate first
            existing = Candidate.create(
                title="t", fact="already pending fact", proposed_section="X",
                confidence="high", tags=["opal"],
                provenance=Provenance(
                    session_id="s", cwd="/tmp",
                    captured_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
                    excerpt="e",
                ),
            )
            append_candidates(pending, [existing])
            pending_size_before = pending.stat().st_size

            # Classifier returns the SAME fact again
            duplicate = Candidate.create(
                title="t2", fact="already pending fact",  # same fact → same id
                proposed_section="X", confidence="high", tags=["opal"],
                provenance=Provenance(
                    session_id="s2", cwd="/tmp",
                    captured_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
                    excerpt="e",
                ),
            )

            config = {
                "destination_file": str(obs),
                "pending_file": str(pending),
                "haiku_model": "m",
                "prompt_version": "1.0",
            }
            with mock.patch("pipeline.runner._load_config", return_value=config), \
                 mock.patch("pipeline.classifier.Classifier.classify",
                            return_value=[duplicate]):
                with mock.patch.object(
                    sys, "argv",
                    ["runner.py", "--mode", "stop",
                     "--transcript", str(transcript),
                     "--session-id", "s", "--cwd", "/tmp"]
                ):
                    from pipeline.runner import main
                    main()

            # File should NOT have grown — duplicate caught by pending-file dedup
            pending_size_after = pending.stat().st_size
            self.assertEqual(pending_size_before, pending_size_after,
                "Duplicate candidate (same id as already-pending) must not be re-staged")


if __name__ == "__main__":
    unittest.main()
