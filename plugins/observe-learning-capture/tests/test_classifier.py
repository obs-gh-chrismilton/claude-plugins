"""Tests for pipeline.classifier — Haiku invocation orchestration.

We mock the subprocess call to `claude` CLI. Real Haiku calls are only
exercised via end-to-end tests (Task 16).
"""
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pipeline.classifier import (
    Classifier,
    parse_haiku_yaml_output,
    build_marker_candidate,
    _is_empty_haiku_response,
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


class TestClassifierEdgeCases(unittest.TestCase):
    @mock.patch("pipeline.classifier._invoke_haiku")
    def test_subprocess_timeout_emits_marker(self, mock_invoke):
        """Q1: subprocess.TimeoutExpired must be caught and produce a marker."""
        mock_invoke.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            obs.write_text("", encoding="utf-8")
            c = Classifier(
                model="m",
                prompt_template_path=_prompts_dir() / "classifier.md",
                observeie_md_path=obs,
            )
            cands = c.classify(turn_text="x", session_id="s", cwd="/c")
            self.assertEqual(len(cands), 1)
            self.assertIn("self-error", cands[0].tags)

    def test_inline_tags_parsed_as_list(self):
        """Q2: tags: [opal, syntax] must parse as ['opal', 'syntax'], not chars."""
        sample = """\
- title: "Test"
  fact: |
    Test fact.
  proposed_section: "X"
  confidence: high
  tags: [opal, syntax]
  classifier_confidence_score: 0.5
"""
        cands = parse_haiku_yaml_output(sample)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["tags"], ["opal", "syntax"])

    def test_inline_quoted_tags(self):
        """Inline tags with quotes parse correctly."""
        sample = """\
- title: "T"
  fact: f
  proposed_section: X
  confidence: high
  tags: ["opal", "syntax"]
"""
        cands = parse_haiku_yaml_output(sample)
        self.assertEqual(cands[0]["tags"], ["opal", "syntax"])

    def test_null_output_returns_empty(self):
        """Q8: 'null' from Haiku → empty list, not malformed marker."""
        self.assertEqual(parse_haiku_yaml_output("null"), [])
        self.assertEqual(parse_haiku_yaml_output("~"), [])

    def test_fence_stripping(self):
        """Haiku may wrap in ```yaml ... ``` — fences must be stripped."""
        fenced = """```yaml
- title: T
  fact: f
  proposed_section: X
  confidence: high
  tags: [opal]
```"""
        cands = parse_haiku_yaml_output(fenced)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["title"], "T")

    @mock.patch("pipeline.classifier._invoke_haiku")
    def test_happy_path_tags_round_trip(self, mock_invoke):
        """Q2 regression: full classify() with inline tags produces correct tag list."""
        mock_invoke.return_value = SAMPLE_YAML_OUTPUT
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            obs.write_text("", encoding="utf-8")
            c = Classifier(
                model="m",
                prompt_template_path=_prompts_dir() / "classifier.md",
                observeie_md_path=obs,
            )
            cands = c.classify(turn_text="x", session_id="s", cwd="/c")
            self.assertEqual(len(cands), 1)
            # Tags must be a real list of strings, NOT a character-decomposed list
            self.assertEqual(cands[0].tags, ["opal", "syntax"])


class TestEmptyHaikuResponse(unittest.TestCase):
    """Regression tests for _is_empty_haiku_response — added after T17 found
    that real Haiku returns fenced empty-list + trailing prose when it
    decides "this fact is already captured".
    """

    def test_bare_empty_list(self):
        self.assertTrue(_is_empty_haiku_response("[]"))
        self.assertTrue(_is_empty_haiku_response("  []  "))

    def test_bare_null(self):
        self.assertTrue(_is_empty_haiku_response("null"))
        self.assertTrue(_is_empty_haiku_response("~"))

    def test_empty_string(self):
        self.assertTrue(_is_empty_haiku_response(""))
        self.assertTrue(_is_empty_haiku_response("   \n  "))

    def test_fenced_empty_list(self):
        """Common Haiku wrapping — observed in T17 real-Haiku runs."""
        self.assertTrue(_is_empty_haiku_response("```yaml\n[]\n```"))
        self.assertTrue(_is_empty_haiku_response("```\n[]\n```"))

    def test_fenced_empty_list_with_trailing_prose(self):
        """T17 production finding: Haiku may explain why no candidates."""
        response = (
            "```yaml\n"
            "[]\n"
            "```\n"
            "\n"
            "This fact is already captured in the existing knowledge base."
        )
        self.assertTrue(
            _is_empty_haiku_response(response),
            "Fenced empty-list followed by explanation must be recognized "
            "as empty — otherwise classifier emits spurious self-error markers"
        )

    def test_real_candidate_is_not_empty(self):
        """A real candidate response must NOT register as empty."""
        response = (
            "```yaml\n"
            "- title: T\n"
            "  fact: f\n"
            "  proposed_section: X\n"
            "  confidence: high\n"
            "  tags: [opal]\n"
            "```"
        )
        self.assertFalse(_is_empty_haiku_response(response))

    @mock.patch("pipeline.classifier._invoke_haiku")
    def test_classify_with_fenced_empty_plus_prose_no_marker(self, mock_invoke):
        """End-to-end: a real-world dedup-style Haiku response must produce
        zero candidates, NOT a self-error marker. This is the bug T17 found.
        """
        mock_invoke.return_value = (
            "```yaml\n"
            "[]\n"
            "```\n"
            "\n"
            "This fact is already in the Already known section."
        )
        with tempfile.TemporaryDirectory() as d:
            obs = Path(d) / "ObserveIE.md"
            obs.write_text("# ObserveIE\n", encoding="utf-8")
            c = Classifier(
                model="m",
                prompt_template_path=_prompts_dir() / "classifier.md",
                observeie_md_path=obs,
            )
            cands = c.classify(turn_text="x", session_id="s", cwd="/c")
        self.assertEqual(cands, [],
            "Fenced-empty + prose must produce zero candidates, not a "
            "self-error marker — otherwise pending file fills with noise "
            "after every dedup-positive session."
        )


def _prompts_dir() -> Path:
    return Path(__file__).parent.parent / "prompts"


if __name__ == "__main__":
    unittest.main()
