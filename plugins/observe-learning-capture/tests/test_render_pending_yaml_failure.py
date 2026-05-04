"""Tests for pipeline.render_pending — session-start review surface.

Bug 4: hooks/session-start-review.sh used inline `python3 -c '...'` with
sys.path.insert(0, os.environ.get("PWD", ".")). PWD wasn't reliably
exported to the inline Python subprocess, causing ModuleNotFoundError
and silently breaking the pending-review surface.

Fix: replace inline Python with a real module pipeline.render_pending,
called via `python3 -m pipeline.render_pending`. Real __file__ resolves
cleanly; standard module-import path; mockable from tests.

Module must also surface YAML parse failures explicitly (NOT swallow)
so that a poison-marker era pending file produces a visible RENDER
FAILED block instead of silent zero output.
"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


class TestRenderPending(unittest.TestCase):
    def test_empty_pending_file_produces_no_output(self):
        from pipeline import render_pending

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            pending.write_text("")  # empty
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = render_pending.main(pending_path=pending)
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().strip(), "")

    def test_missing_pending_file_produces_no_output(self):
        from pipeline import render_pending

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "does-not-exist.md"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = render_pending.main(pending_path=pending)
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().strip(), "")

    def test_valid_pending_file_renders_review_block(self):
        from pipeline import render_pending

        sample = """\
---
id: abcd1234
title: Test learning
fact: |
  This is a test fact.
proposed_section: OPAL Gotchas
confidence: high
tags: [opal]
provenance:
  session_id: test-session
  cwd: /test/cwd
  captured_at: 2026-05-04T10:00:00+00:00
  excerpt: test excerpt
"""
        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            pending.write_text(sample)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = render_pending.main(pending_path=pending)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("OBSERVE LEARNING CAPTURE", out)
            self.assertIn("PENDING REVIEW", out)
            self.assertIn("Test learning", out)
            self.assertIn("OPAL Gotchas", out)

    def test_malformed_yaml_produces_render_failed_block(self):
        # NOTE: We empirically verified that pipeline.stage.read_pending is
        # fault-tolerant — it catches per-record (ValueError, IndexError) and
        # logs to stderr rather than re-raising. So feeding genuinely malformed
        # YAML directly produces an empty-list return, not an exception.
        # (The plan's validator-finding appendix flagged this and authorized
        # mocking as one of two acceptable workarounds.)
        # We mock read_pending to raise, since the unit under test here is the
        # render module's exception-handling path — not stage.py's parser.
        from pipeline import render_pending

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            # File must exist and be non-empty so we reach the read_pending call.
            pending.write_text("this is not valid yaml: [\n  - broken: {{{{")
            buf = io.StringIO()
            with mock.patch(
                "pipeline.render_pending.read_pending",
                side_effect=ValueError("simulated YAML parse failure"),
            ), redirect_stdout(buf):
                rc = render_pending.main(pending_path=pending)
            self.assertEqual(rc, 0)  # never block session
            out = buf.getvalue()
            self.assertIn("RENDER FAILED", out)
            self.assertIn(str(pending), out)  # path must be surfaced


if __name__ == "__main__":
    unittest.main()
