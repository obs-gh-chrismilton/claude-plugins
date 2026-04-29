"""End-to-end test: synthesize a session transcript, run the runner with a
mocked classifier, verify pending file state and ObserveIE.md state after
simulated approval flow.

Does NOT actually call Haiku — that's mocked. The point is to verify the
glue between transcript → classifier → dedupe → stage → merge.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pipeline.classifier import Classifier
from pipeline.dedupe import extract_existing_ids, is_duplicate
from pipeline.merge import merge_candidate, remove_from_pending
from pipeline.stage import append_candidates, read_pending
from pipeline.transcript import last_assistant_turn
from pipeline.types import Candidate, ClassifierMeta, Provenance


SAMPLE_HAIKU_OUTPUT = """\
- title: "Cascade deadlock on Tracing/Span"
  fact: |
    deleteDatastream on Tracing/Span fails because managed datasets
    reference each other; no force flag exists.
  proposed_section: "Object Management and Cleanup"
  confidence: high
  tags: [delete, cascade, tracing]
  classifier_confidence_score: 0.91
"""


class TestEndToEnd(unittest.TestCase):
    @mock.patch("pipeline.classifier._invoke_haiku")
    def test_full_pipeline(self, mock_invoke):
        """Full happy path: transcript → classifier → dedupe → stage → merge."""
        mock_invoke.return_value = SAMPLE_HAIKU_OUTPUT
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            transcript = tmp / "session.jsonl"
            transcript.write_text(
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{
                            "type": "text",
                            "text": (
                                "I tried deleteDatastream(id:42767020) and got "
                                "a cascade deadlock — the managed Tracing/* "
                                "datasets reference each other. No force flag."
                            ),
                        }],
                    },
                    "uuid": "a1",
                    "timestamp": "2026-04-29T11:33:00Z",
                }) + "\n",
                encoding="utf-8",
            )
            obs = tmp / "ObserveIE.md"
            obs.write_text("# ObserveIE\n", encoding="utf-8")
            pending = tmp / ".pending.md"
            prompt = (
                Path(__file__).parent.parent / "prompts" / "classifier.md"
            )

            # Stop-mode classify (mocked Haiku output)
            classifier = Classifier(
                model="m", prompt_template_path=prompt, observeie_md_path=obs,
            )
            turn = last_assistant_turn(transcript)
            self.assertIsNotNone(turn)
            cands = classifier.classify(
                turn_text=turn.text, session_id="s", cwd="/tmp",
            )
            self.assertEqual(len(cands), 1, "classifier should produce 1 candidate")

            # Dedup + stage
            existing = extract_existing_ids(obs)
            novel = [c for c in cands if not is_duplicate(c, existing)]
            self.assertEqual(len(novel), 1, "first capture should be novel")
            append_candidates(pending, novel)

            records = read_pending(pending)
            self.assertEqual(len(records), 1)

            # Simulate approval — merge
            merge_candidate(novel[0], obs)
            remove_from_pending(novel[0].id, pending)

            content = obs.read_text(encoding="utf-8")
            self.assertIn("## Object Management and Cleanup", content)
            self.assertIn("deleteDatastream", content)
            self.assertIn(f"<!-- id:{novel[0].id}", content)

            # Pending should be empty now
            self.assertEqual(read_pending(pending), [])

            # Re-running with same Haiku output should now dedup
            existing_after = extract_existing_ids(obs)
            cands2 = classifier.classify(
                turn_text=turn.text, session_id="s2", cwd="/tmp",
            )
            novel2 = [c for c in cands2 if not is_duplicate(c, existing_after)]
            self.assertEqual(novel2, [], "Same fact must not re-stage after merge")

    @mock.patch("pipeline.classifier._invoke_haiku")
    def test_haiku_returns_empty_no_candidates_staged(self, mock_invoke):
        """If Haiku returns [] (no candidates), pipeline silently no-ops."""
        mock_invoke.return_value = "[]"
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            obs = tmp / "ObserveIE.md"
            obs.write_text("", encoding="utf-8")
            pending = tmp / ".pending.md"
            prompt = Path(__file__).parent.parent / "prompts" / "classifier.md"

            classifier = Classifier(
                model="m", prompt_template_path=prompt, observeie_md_path=obs,
            )
            cands = classifier.classify(
                turn_text="some text", session_id="s", cwd="/tmp",
            )
            self.assertEqual(cands, [])

            existing = extract_existing_ids(obs)
            novel = [c for c in cands if not is_duplicate(c, existing)]
            append_candidates(pending, novel)

            self.assertFalse(pending.exists(),
                "Empty candidate list should not create a pending file")

    @mock.patch("pipeline.classifier._invoke_haiku")
    def test_haiku_failure_emits_marker_to_pending(self, mock_invoke):
        """Haiku timeout/failure → marker candidate staged so user sees it."""
        import subprocess
        mock_invoke.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            obs = tmp / "ObserveIE.md"
            obs.write_text("", encoding="utf-8")
            pending = tmp / ".pending.md"
            prompt = Path(__file__).parent.parent / "prompts" / "classifier.md"

            classifier = Classifier(
                model="m", prompt_template_path=prompt, observeie_md_path=obs,
            )
            cands = classifier.classify(
                turn_text="some text", session_id="s", cwd="/tmp",
            )
            self.assertEqual(len(cands), 1)
            self.assertIn("self-error", cands[0].tags)

            # Marker is staged like any other candidate
            existing = extract_existing_ids(obs)
            novel = [c for c in cands if not is_duplicate(c, existing)]
            append_candidates(pending, novel)

            records = read_pending(pending)
            self.assertEqual(len(records), 1)
            self.assertIn("self-error", records[0]["tags"])


if __name__ == "__main__":
    unittest.main()
