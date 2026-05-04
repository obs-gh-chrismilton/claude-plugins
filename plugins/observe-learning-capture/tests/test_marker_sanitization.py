"""Tests for failure_reason sanitization in build_marker_candidate.

Bug 3: subprocess.TimeoutExpired.__str__() embeds the full argv (including
the rendered prompt). When that string lands in marker fact/excerpt fields,
it bloats the YAML pending file to 100+ KB. Sanitation must cap length and
strip newlines so YAML serialization stays bounded and readable.
"""
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.classifier import build_marker_candidate
from pipeline.stage import append_candidates, read_pending


class TestMarkerSanitization(unittest.TestCase):
    def test_sanitize_caps_length_at_200(self):
        # 35 KB of garbage — the kind of content TimeoutExpired.__str__ embeds
        bloated = "x" * 35000
        marker = build_marker_candidate(
            failure_reason=bloated,
            session_id="test-session",
            cwd="/test",
            captured_at=datetime.now(timezone.utc),
        )
        # fact field is "Classifier failed: <reason>"; ≤ 200 + small prefix overhead
        self.assertLess(len(marker.fact), 250,
                        f"fact too long: {len(marker.fact)} chars")
        # excerpt also embeds reason; same bound
        self.assertLess(len(marker.provenance.excerpt), 250,
                        f"excerpt too long: {len(marker.provenance.excerpt)} chars")

    def test_sanitize_strips_newlines(self):
        multiline = "line1\nline2\nline3\nlots\nof\nlines"
        marker = build_marker_candidate(
            failure_reason=multiline,
            session_id="test-session",
            cwd="/test",
            captured_at=datetime.now(timezone.utc),
        )
        self.assertNotIn("\n", marker.fact[len("Classifier failed: "):])
        self.assertNotIn("\n", marker.provenance.excerpt)

    def test_sanitize_handles_subprocess_timeoutexpired(self):
        # Simulate the actual exception-string format that triggered Bug 3
        exc = subprocess.TimeoutExpired(
            cmd=["claude", "--print", "x" * 30000],
            timeout=60,
        )
        marker = build_marker_candidate(
            failure_reason=str(exc),
            session_id="test-session",
            cwd="/test",
            captured_at=datetime.now(timezone.utc),
        )
        self.assertLess(len(marker.fact), 250)

    def test_sanitized_marker_round_trips_through_yaml(self):
        # End-to-end: write a sanitized marker to pending file, read it back
        bloated = "y" * 35000
        marker = build_marker_candidate(
            failure_reason=bloated,
            session_id="test-session",
            cwd="/test",
            captured_at=datetime.now(timezone.utc),
        )
        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            append_candidates(pending, [marker])
            records = read_pending(pending)
        self.assertEqual(len(records), 1)
        self.assertLess(len(records[0]["fact"]), 250)
