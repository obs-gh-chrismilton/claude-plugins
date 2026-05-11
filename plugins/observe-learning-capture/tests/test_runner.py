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
            # Mock _cli_precheck to True so we exercise the no-turn early
            # return path (Task 10 wired the precheck before classifier
            # construction; without this mock the precheck would fail on
            # missing ANTHROPIC_API_KEY and emit its own marker, growing
            # the pending file before we ever reach the no-turn branch).
            with mock.patch("pipeline.runner._load_config", return_value=config), \
                 mock.patch("pipeline.runner._cli_precheck", return_value=True):
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
            # Mock _cli_precheck to True so missing ANTHROPIC_API_KEY in
            # the test env doesn't short-circuit before classify runs.
            with mock.patch("pipeline.runner._load_config", return_value=config), \
                 mock.patch("pipeline.runner._cli_precheck", return_value=True), \
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
            # Mock _cli_precheck to True so missing ANTHROPIC_API_KEY in
            # the test env doesn't short-circuit and append a marker to
            # pending (which would defeat the dedup-size assertion below).
            with mock.patch("pipeline.runner._load_config", return_value=config), \
                 mock.patch("pipeline.runner._cli_precheck", return_value=True), \
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


class TestRunnerOuterCatchEmitsMarker(unittest.TestCase):
    """Runner's outer ``except Exception`` previously logged + returned 0
    silently. Per spec §9 (log AND surface), the outer catch must ALSO
    emit a marker so unexpected failures surface at /observe-review time
    rather than being invisible.

    The test forces ``Classifier(...)`` construction to raise an
    unexpected RuntimeError, then asserts that:
      1. main_with_args returns exit code 0 (hook subshell stays clean).
      2. A `[FAILURE] classifier` marker is appended to the pending file.

    Migrated from the deleted test_classifier_sdk_errors.py during the
    2026-05-08 SDK→subprocess pivot. The only substantive change here:
    the precheck mock target moved from `_auth_precheck` to
    `_cli_precheck` to match the runner's new precheck name.
    """

    @mock.patch("pipeline.runner.Classifier")
    @mock.patch("pipeline.runner._cli_precheck", return_value=True)
    def test_unexpected_classifier_construction_error_emits_marker(
        self, _precheck, mock_clf_cls
    ):
        from pipeline import runner

        # Force Classifier(...) construction to blow up with an unexpected
        # exception. Outer except must catch it and emit a marker.
        mock_clf_cls.side_effect = RuntimeError(
            "classifier broken in unexpected way"
        )

        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "transcript.jsonl"
            # Minimal valid transcript: one user turn + one substantive
            # assistant turn so last_assistant_turn returns non-None even
            # though we never get that far (Classifier explodes first).
            transcript.write_text(
                '{"type":"user","message":{"content":"hello"},'
                '"uuid":"u1","timestamp":"2026-05-04T10:00:00Z"}\n'
                '{"type":"assistant","message":{"content":[{"type":"text",'
                '"text":"reply with substantive content " }]},'
                '"uuid":"a1","timestamp":"2026-05-04T10:00:01Z"}\n'
            )
            pending = Path(td) / "pending.md"
            destination = Path(td) / "ObserveIE.md"
            destination.write_text("")

            # Override config to point at temp paths so we don't pollute
            # the real ~/.claude/agents/.observeie-pending.md.
            with mock.patch(
                "pipeline.runner._load_config",
                return_value={
                    "destination_file": str(destination),
                    "pending_file": str(pending),
                    "classifier_model": "claude-sonnet-4-5",
                    "prompt_version": "test",
                },
            ):
                rc = runner.main_with_args(
                    mode="stop",
                    transcript=str(transcript),
                    session_id="test-session",
                    cwd="/test",
                )

            # IMPORTANT: assertions go INSIDE the tempfile context. Outside
            # the with-block, the temp dir (and pending file) is torn down,
            # so pending.exists() would always return False.
            self.assertEqual(rc, 0, "hook subshell must stay clean (rc=0)")
            self.assertTrue(pending.exists())
            content = pending.read_text()
            self.assertIn("[FAILURE] classifier", content)


if __name__ == "__main__":
    unittest.main()
