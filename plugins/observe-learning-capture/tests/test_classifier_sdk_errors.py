"""Tests for SDK error handling in classifier + runner.

Bug 2: subprocess.run(['claude', '--print', ...]) replaced by
anthropic.Anthropic().messages.create(...). Auth precheck added at
runner startup: missing ANTHROPIC_API_KEY -> marker; key-rejected ->
marker; transient API errors -> marker.

Per spec section 9: every handled error must emit a marker AND log to
stderr. Never silent.
"""
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import anthropic


class TestAuthPrecheck(unittest.TestCase):
    """Cover the three auth-precheck outcomes:
    - ANTHROPIC_API_KEY missing entirely (env unset)
    - ANTHROPIC_API_KEY present but rejected by the API (401)
    - ANTHROPIC_API_KEY present and accepted (success)
    """

    def setUp(self):
        # Save and clear ANTHROPIC_API_KEY for these tests; restore in tearDown.
        # WHY: these tests assert behavior that depends on env presence; we
        # cannot rely on the executor's shell having (or not having) the var.
        self._saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        if self._saved_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._saved_key

    def test_missing_api_key_emits_marker_and_returns_early(self):
        from pipeline import runner

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            destination = Path(td) / "ObserveIE.md"
            destination.write_text("")
            ok = runner._auth_precheck(
                pending_path=pending,
                session_id="test-session",
                cwd="/test",
            )
            self.assertFalse(ok, "precheck should fail with no API key")
            # Marker should have been written to pending file
            self.assertTrue(pending.exists())
            content = pending.read_text()
            self.assertIn("[FAILURE] classifier", content)
            self.assertIn("ANTHROPIC_API_KEY", content)

    @mock.patch("anthropic.Anthropic")
    def test_invalid_api_key_emits_marker(self, mock_client_cls):
        from pipeline import runner

        # Simulate models.list raising AuthenticationError. The actual class
        # signature requires a response object (not just status_code), but
        # mock.Mock(status_code=401) duck-types as the right shape under
        # anthropic SDK 0.97.0 (verified via preflight).
        mock_client = mock.Mock()
        mock_client.models.list.side_effect = anthropic.AuthenticationError(
            message="Invalid API key",
            response=mock.Mock(status_code=401),
            body={"error": {"message": "Invalid API key"}},
        )
        mock_client_cls.return_value = mock_client

        os.environ["ANTHROPIC_API_KEY"] = "sk-fake-but-set"

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            ok = runner._auth_precheck(
                pending_path=pending,
                session_id="test-session",
                cwd="/test",
            )
            self.assertFalse(ok)
            self.assertTrue(pending.exists())
            content = pending.read_text()
            self.assertIn("[FAILURE] classifier", content)
            self.assertIn("key rejected", content.lower())

    @mock.patch("anthropic.Anthropic")
    def test_valid_api_key_passes_precheck(self, mock_client_cls):
        from pipeline import runner

        mock_client = mock.Mock()
        mock_client.models.list.return_value = mock.Mock()  # success
        mock_client_cls.return_value = mock_client

        os.environ["ANTHROPIC_API_KEY"] = "sk-fake-but-set"

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            ok = runner._auth_precheck(
                pending_path=pending,
                session_id="test-session",
                cwd="/test",
            )
            self.assertTrue(ok)
            # No marker should be written on success
            if pending.exists():
                self.assertEqual(pending.read_text(), "")
