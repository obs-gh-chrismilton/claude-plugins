"""Tests for cache visibility sentinel.

Per silent-failure-hunter review: if our prompt is below the model's
cache minimum (4096 tokens for Haiku, 1024 for Sonnet), cache_control
markers silently no-op (cache_creation_input_tokens=0, no error). Without
a visibility check, classifier "succeeds" but pays full input cost forever.

Fix: after N=5 calls, if cache_read_input_tokens has been 0 every time,
emit a one-shot marker via sentinel file ~/.claude/agents/.observe-cache-warned.
Self-healing: sentinel deleted on first observed cache_read>0.
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


class TestCacheWarning(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sentinel = Path(self._tmp.name) / ".observe-cache-warned"

    def tearDown(self):
        self._tmp.cleanup()

    def _make_classifier(self):
        from pipeline.classifier import Classifier
        # Inject test-isolated pending_path AND sentinel (both must be
        # tmp dir to prevent polluting the user's real ~/.claude state)
        pending = Path(self._tmp.name) / "pending.md"
        clf = Classifier(
            model="claude-sonnet-4-5",
            prompt_template_path=Path("/nonexistent.md"),
            observeie_md_path=Path("/nonexistent.md"),
            prompt_version="test",
            pending_path=pending,
        )
        clf._cache_call_count = 0
        clf._cache_sentinel_path = self.sentinel
        return clf

    def test_no_warning_under_threshold_calls(self):
        from pipeline.stage import append_candidates  # noqa
        clf = self._make_classifier()
        usage = mock.Mock(cache_read_input_tokens=0, cache_creation_input_tokens=0)
        # 4 calls — under N=5 threshold
        for _ in range(4):
            clf._maybe_emit_cache_warning(usage, "s", "/c", datetime.now(timezone.utc))
        self.assertFalse(self.sentinel.exists())

    def test_warning_emitted_at_threshold_when_cache_never_hits(self):
        clf = self._make_classifier()
        usage = mock.Mock(cache_read_input_tokens=0, cache_creation_input_tokens=0)
        # 5 calls with cache_read=0 every time → should fire
        for _ in range(5):
            clf._maybe_emit_cache_warning(usage, "s", "/c", datetime.now(timezone.utc))
        self.assertTrue(self.sentinel.exists(),
                        "sentinel should exist after 5 calls × 0 cache reads")

    def test_warning_not_re_emitted_after_sentinel_exists(self):
        clf = self._make_classifier()
        # Simulate prior warning already fired
        self.sentinel.touch()
        first_mtime = self.sentinel.stat().st_mtime

        usage = mock.Mock(cache_read_input_tokens=0, cache_creation_input_tokens=0)
        for _ in range(10):
            clf._maybe_emit_cache_warning(usage, "s", "/c", datetime.now(timezone.utc))
        # Sentinel should not have been re-touched
        self.assertEqual(self.sentinel.stat().st_mtime, first_mtime)

    def test_sentinel_self_heals_on_cache_hit(self):
        clf = self._make_classifier()
        # Pre-existing sentinel from prior warning
        self.sentinel.touch()
        usage = mock.Mock(cache_read_input_tokens=500, cache_creation_input_tokens=0)
        clf._maybe_emit_cache_warning(usage, "s", "/c", datetime.now(timezone.utc))
        self.assertFalse(self.sentinel.exists(),
                         "sentinel should be deleted on cache_read>0")
