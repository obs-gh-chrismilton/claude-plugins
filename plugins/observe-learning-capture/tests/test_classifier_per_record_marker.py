"""Tests for per-record marker emission on malformed classifier output.

Bug 5: classifier.py's per-record loop silently dropped Haiku records
missing the 'title' field (caught KeyError, printed to stderr, continued).
Real captures were lost with no marker — invisible to the reviewer.

Fix: each malformed record produces its own marker via
build_marker_candidate, with the rest of the batch processed as before.
"""
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pipeline.classifier import Classifier


class TestPerRecordMarker(unittest.TestCase):
    def setUp(self):
        # Build a minimal Classifier that won't actually call any model
        self.clf = Classifier(
            model="claude-sonnet-4-5",
            prompt_template_path=Path("/nonexistent.md"),
            observeie_md_path=Path("/nonexistent.md"),
            prompt_version="test",
        )

    @mock.patch("pipeline.classifier._invoke_classifier")
    @mock.patch("pipeline.classifier._build_prompt")
    @mock.patch("pipeline.classifier._generate_slim_known_facts", return_value="(empty)")
    def test_one_malformed_record_emits_marker_does_not_block_batch(
        self, _slim, _build, _invoke
    ):
        # Classifier response: 1 valid record + 1 missing title.
        # Post-2026-05-08 pivot: _invoke_classifier returns just the text
        # string (no usage tuple) since the `claude -p` CLI does not
        # expose stable usage shape and the cache-warning sentinel that
        # consumed it was deleted.
        _invoke.return_value = """\
- title: "OPAL accepts foo"
  fact: |
    OPAL accepts foo as input.
  proposed_section: "OPAL Gotchas"
  confidence: high
  tags: [opal]
- fact: |
    This record is missing title.
  proposed_section: "OPAL Gotchas"
  confidence: high
  tags: [opal]
"""
        # Bug 2 part 2: _build_prompt now returns a 3-tuple
        # (static_template, slim_known_facts, user_message). The test
        # only cares that classify reaches _invoke_classifier — the prompt
        # content itself is irrelevant because _invoke_classifier is mocked.
        _build.return_value = ("static", "slim", "user")

        candidates = self.clf.classify(
            turn_text="some turn text " * 20,
            session_id="test-session",
            cwd="/test",
            excerpt="excerpt",
        )

        # Should have 2 candidates: 1 valid + 1 marker
        self.assertEqual(len(candidates), 2)
        titles = [c.title for c in candidates]
        self.assertIn("OPAL accepts foo", titles)
        # Marker has title "[FAILURE] classifier"
        self.assertIn("[FAILURE] classifier", titles)

    @mock.patch("pipeline.classifier._invoke_classifier")
    @mock.patch("pipeline.classifier._build_prompt")
    @mock.patch("pipeline.classifier._generate_slim_known_facts", return_value="(empty)")
    def test_marker_failure_reason_names_missing_field(
        self, _slim, _build, _invoke
    ):
        # Post-2026-05-08 pivot: _invoke_classifier returns just text.
        _invoke.return_value = """\
- fact: "no title here"
  proposed_section: "OPAL Gotchas"
  confidence: high
"""
        # Bug 2 part 2: _build_prompt now returns a 3-tuple.
        _build.return_value = ("static", "slim", "user")

        candidates = self.clf.classify(
            turn_text="some turn text " * 20,
            session_id="test-session",
            cwd="/test",
            excerpt="excerpt",
        )

        markers = [c for c in candidates if c.title == "[FAILURE] classifier"]
        self.assertEqual(len(markers), 1)
        # Per spec: failure_reason should mention the missing field name
        self.assertIn("title", markers[0].fact)
        self.assertIn("malformed", markers[0].fact.lower())
