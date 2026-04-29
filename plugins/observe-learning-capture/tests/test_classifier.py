"""Tests for pipeline.classifier — Haiku invocation orchestration.

We mock the subprocess call to `claude` CLI. Real Haiku calls are only
exercised via end-to-end tests (Task 16).
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pipeline.classifier import (
    Classifier,
    parse_haiku_yaml_output,
    build_marker_candidate,
)


SAMPLE_YAML_OUTPUT = """\
- title: "OPAL '7d' rejected"
  fact: |
    OPAL rejects '7d'; use '168h'.
  proposed_section: "OPAL Gotchas"
  confidence: high
  tags: [opal, syntax]
  classifier_confidence_score: 0.88
"""

SAMPLE_EMPTY_OUTPUT = "[]"


class TestClassifierParser(unittest.TestCase):
    def test_parse_valid_yaml_output(self):
        cands = parse_haiku_yaml_output(SAMPLE_YAML_OUTPUT)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["title"], "OPAL '7d' rejected")

    def test_parse_empty_list(self):
        self.assertEqual(parse_haiku_yaml_output(SAMPLE_EMPTY_OUTPUT), [])

    def test_parse_malformed_returns_empty_with_marker(self):
        cands = parse_haiku_yaml_output("totally not yaml {{{")
        self.assertEqual(cands, [])


class TestClassifierEndToEnd(unittest.TestCase):
    @mock.patch("pipeline.classifier._invoke_haiku")
    def test_happy_path(self, mock_invoke):
        mock_invoke.return_value = SAMPLE_YAML_OUTPUT
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            obs.write_text("# ObserveIE\n", encoding="utf-8")
            c = Classifier(
                model="claude-haiku-4-5-20251001",
                prompt_template_path=_prompts_dir() / "classifier.md",
                observeie_md_path=obs,
            )
            cands = c.classify(
                turn_text="Some Observe platform conversation.",
                session_id="abc",
                cwd="/tmp/cwd",
            )
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].confidence, "high")
        self.assertEqual(cands[0].provenance.session_id, "abc")

    @mock.patch("pipeline.classifier._invoke_haiku")
    def test_haiku_failure_emits_marker_candidate(self, mock_invoke):
        mock_invoke.side_effect = RuntimeError("haiku timeout")
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            obs.write_text("", encoding="utf-8")
            c = Classifier(
                model="claude-haiku-4-5-20251001",
                prompt_template_path=_prompts_dir() / "classifier.md",
                observeie_md_path=obs,
            )
            cands = c.classify(
                turn_text="x", session_id="s", cwd="/c",
            )
        self.assertEqual(len(cands), 1)
        self.assertIn("self-error", cands[0].tags)


class TestMarkerCandidate(unittest.TestCase):
    def test_marker_carries_failure_reason(self):
        c = build_marker_candidate(
            failure_reason="haiku timeout",
            session_id="s", cwd="/c",
            captured_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
        )
        self.assertIn("self-error", c.tags)
        self.assertIn("haiku timeout", c.fact)


def _prompts_dir() -> Path:
    return Path(__file__).parent.parent / "prompts"


if __name__ == "__main__":
    unittest.main()
