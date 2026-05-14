"""Tests for subprocess-based classifier invocation.

This file replaces test_classifier_sdk_errors.py's SDK-specific
coverage after the auth pivot from the standalone Anthropic SDK
(ANTHROPIC_API_KEY) to the `claude` CLI subprocess (subscription auth
inherited from the user's Claude Code session).

Why a separate file rather than editing test_classifier_sdk_errors.py
in place: TDD discipline — write the new tests, watch them fail
against the SDK code, then flip the implementation. Keeping the new
tests in a separate file makes the diff easier to review and the
historical SDK tests easier to delete in one motion at the end.

What we cover here:
  - _invoke_classifier shells out to `claude -p` (NOT the Anthropic SDK).
  - The user message is passed on STDIN, NOT on argv (mitigation for
    macOS ARG_MAX = 256KB; long session-end concatenations exceed
    argv limits and produce OSError: argument list too long).
  - --model is passed explicitly to keep classifier on a cheap model
    (otherwise claude -p inherits the parent session's model — which
    in interactive Claude Code is currently Opus 4.7 1M, ~30x cost of
    Sonnet 4.5).
  - --output-format json + --no-session-persistence are always passed
    (machine-parseable output; no stray session records).
  - _cli_precheck (replacement for _auth_precheck) verifies that
    `claude` is reachable on PATH and exits 0 on a trivial invocation.
  - Subprocess-specific exceptions (FileNotFoundError, TimeoutExpired,
    CalledProcessError, JSONDecodeError) each route to a marker.
"""
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


# Reusable canned JSON shape that mirrors what `claude -p --output-format json`
# actually produced during the 2026-05-08 preflight (verified live):
#   {"type":"result","subtype":"success","is_error":false,...,
#    "result":"<assistant text>","stop_reason":"end_turn",...}
# Tests only need .result and the structural envelope.
def _claude_p_json(result_text: str) -> str:
    """Build a minimal JSON envelope matching `claude -p --output-format json`.

    Args:
        result_text: The assistant-text payload that lives at .result.

    Returns:
        JSON-encoded string ready to be returned as proc.stdout.
    """
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result_text,
        "stop_reason": "end_turn",
        "session_id": "test-session-uuid",
        "duration_ms": 1234,
    })


class TestSubprocessInvocation(unittest.TestCase):
    """The core invocation contract: shell out to `claude -p`, pass user
    message via STDIN, parse .result from the JSON envelope.
    """

    @mock.patch("pipeline.classifier.subprocess.run")
    def test_invoke_classifier_strips_anthropic_env_vars(self, mock_run):
        """Subprocess MUST be launched with env= that excludes
        ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN.

        WHY this test exists (2026-05-14 bug A):
        `claude -p` walks an auth-resolution chain where env vars take
        precedence over the macOS keychain credential. The parent Claude
        Code session is on the user's MAX subscription (resolved via
        keychain at session start). If we let the subprocess inherit
        ANTHROPIC_API_KEY from os.environ, `claude -p` re-resolves auth,
        the env var wins, and the call silently bills against an API tier
        the user does not want billed — or fails with "Credit balance is
        too low" if that tier has zero balance.

        Hard rule from global CLAUDE.md (Anthropic API Key Policy):
        "Anthropic model invocations must go through my MAX subscription
        via the parent Claude Code session's auth context (macOS keychain
        credential), NEVER through the ANTHROPIC_API_KEY env var."

        The bash hook (`stop-hook.sh` line 33) already does
        `unset ANTHROPIC_API_KEY` before forking the Python pipeline.
        This test guards the Python-side defense in depth so that the
        /observe-capture slash command (which does NOT shell-unset) and
        any future caller cannot reintroduce the leak.
        """
        from pipeline.classifier import _invoke_classifier

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=_claude_p_json("[]"),
            stderr="",
        )

        # Force both vars into the environment for the duration of this test
        # so we can verify the subprocess.run call strips them out.
        with mock.patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-fake-test-only",
            "ANTHROPIC_AUTH_TOKEN": "fake-auth-token-test-only",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }, clear=False):
            _invoke_classifier(
                static_template="STATIC",
                slim_known_facts="SLIM",
                user_message="USER MESSAGE",
                model="claude-sonnet-4-5",
            )

        called_args, called_kwargs = mock_run.call_args
        env = called_kwargs.get("env")
        self.assertIsNotNone(
            env,
            "subprocess.run must be called with an explicit env= kwarg "
            "so we can guarantee ANTHROPIC_* vars are stripped.",
        )
        self.assertNotIn(
            "ANTHROPIC_API_KEY", env,
            "ANTHROPIC_API_KEY must be stripped from subprocess env "
            "(would force `claude -p` onto API auth instead of keychain).",
        )
        self.assertNotIn(
            "ANTHROPIC_AUTH_TOKEN", env,
            "ANTHROPIC_AUTH_TOKEN must also be stripped — same auth-leak "
            "risk as ANTHROPIC_API_KEY.",
        )
        # PATH must survive — without it the subprocess can't locate `claude`.
        self.assertIn(
            "PATH", env,
            "non-sensitive env (PATH) must pass through to the subprocess.",
        )

    @mock.patch("pipeline.classifier.subprocess.run")
    def test_invoke_classifier_calls_claude_p_with_expected_flags(self, mock_run):
        """The subprocess must be `claude -p ... --system-prompt ... --model ...
        --output-format json --no-session-persistence` (in some order).
        """
        from pipeline.classifier import _invoke_classifier

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=_claude_p_json("[]"),
            stderr="",
        )

        result = _invoke_classifier(
            static_template="STATIC TEMPLATE TEXT",
            slim_known_facts="SLIM FACTS",
            user_message="USER MESSAGE",
            model="claude-sonnet-4-5",
        )

        # Return value is the unwrapped text (not a tuple any more — usage is
        # unrecoverable with subprocess and we accept blind operation per the
        # 2026-05-08 design committee verdict).
        self.assertEqual(result, "[]")

        # Inspect the actual subprocess.run call.
        self.assertEqual(mock_run.call_count, 1)
        called_args, called_kwargs = mock_run.call_args
        argv = called_args[0] if called_args else called_kwargs.get("args")
        self.assertIsNotNone(argv, "subprocess.run must be called with an argv list")

        # Mandatory pieces of the argv contract:
        self.assertEqual(argv[0], "claude", "must invoke the `claude` CLI")
        self.assertIn("-p", argv, "must use --print / -p mode for non-interactive output")
        self.assertIn("--system-prompt", argv)
        self.assertIn("--model", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("--no-session-persistence", argv,
                      "must NOT leave stray session records on disk")

        # The model token immediately follows --model.
        model_idx = argv.index("--model")
        self.assertEqual(argv[model_idx + 1], "claude-sonnet-4-5")

        # output-format must be json so .result can be parsed.
        fmt_idx = argv.index("--output-format")
        self.assertEqual(argv[fmt_idx + 1], "json")

    @mock.patch("pipeline.classifier.subprocess.run")
    def test_user_message_passed_on_stdin_not_argv(self, mock_run):
        """R2 mitigation: user_message goes via stdin, not -p arg.

        macOS ARG_MAX is ~256KB; a long session-end concatenation will exceed
        argv limits and raise OSError: argument list too long. Passing on
        stdin avoids the entire argv-size problem and is also more shell-safe.
        """
        from pipeline.classifier import _invoke_classifier

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=_claude_p_json("[]"),
            stderr="",
        )

        long_message = "long turn text " * 5000  # ~75 KB — well under ARG_MAX
        # but enough to verify we're not putting it through argv.
        _invoke_classifier(
            static_template="x",
            slim_known_facts="y",
            user_message=long_message,
            model="claude-sonnet-4-5",
        )

        called_args, called_kwargs = mock_run.call_args
        argv = called_args[0] if called_args else called_kwargs.get("args")

        # The user message MUST NOT appear as a standalone argv token. Note we
        # do allow `claude -p` with an empty positional; the prompt is fed via
        # stdin. Some integration variants accept `-p <prompt>` directly, but
        # for argv-size safety we only use the stdin form.
        self.assertNotIn(long_message, argv,
                         "user_message must NOT be on argv (ARG_MAX risk)")

        # The stdin payload must contain the user message.
        self.assertEqual(called_kwargs.get("input"), long_message,
                         "user_message must be passed via subprocess.run(input=...)")

    @mock.patch("pipeline.classifier.subprocess.run")
    def test_invoke_classifier_uses_static_and_slim_in_system_prompt(self, mock_run):
        """The static template and slim known-facts blocks must both appear
        in the --system-prompt argument value.

        Cache_control granularity is unrecoverable with `claude -p` (the CLI
        manages its own caching opaquely), so we collapse both blocks into a
        single system prompt rather than trying to preserve the SDK's
        two-block layered structure that no longer has any effect.
        """
        from pipeline.classifier import _invoke_classifier

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=_claude_p_json("[]"),
            stderr="",
        )

        _invoke_classifier(
            static_template="STATIC TEMPLATE TEXT",
            slim_known_facts="SLIM FACTS BLOCK",
            user_message="x",
            model="claude-sonnet-4-5",
        )

        called_args, _ = mock_run.call_args
        argv = called_args[0]
        sys_idx = argv.index("--system-prompt")
        system_text = argv[sys_idx + 1]
        self.assertIn("STATIC TEMPLATE TEXT", system_text)
        self.assertIn("SLIM FACTS BLOCK", system_text)


class TestSubprocessErrorHandling(unittest.TestCase):
    """Each plausible subprocess-layer failure must produce a marker per
    spec §9 (log AND surface). Errors must NOT escape to caller silently.
    """

    def setUp(self):
        from pipeline.classifier import Classifier
        # Use a fresh tmpdir per test so prompt template / pending paths don't
        # cross-pollute. The classifier is constructed once here.
        self._tmp = tempfile.TemporaryDirectory()
        tmpl = Path(self._tmp.name) / "classifier.md"
        tmpl.write_text("static template")
        self.clf = Classifier(
            model="claude-sonnet-4-5",
            prompt_template_path=tmpl,
            observeie_md_path=Path(self._tmp.name) / "ObserveIE.md",
            prompt_version="test",
            pending_path=Path(self._tmp.name) / "pending.md",
        )

    def tearDown(self):
        self._tmp.cleanup()

    @mock.patch("pipeline.classifier.subprocess.run")
    def test_file_not_found_emits_marker(self, mock_run):
        """`claude` binary not on PATH → FileNotFoundError → marker."""
        mock_run.side_effect = FileNotFoundError(
            2, "No such file or directory", "claude"
        )
        candidates = self.clf.classify(
            turn_text="long substantive turn text " * 20,
            session_id="s", cwd="/c", excerpt="e",
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "[FAILURE] classifier")
        self.assertIn("self-error", candidates[0].tags)
        # Failure reason should reference the binary or the FileNotFoundError
        # so a human can diagnose ("claude not on PATH" or similar).
        self.assertTrue(
            "claude" in candidates[0].fact.lower()
            or "not found" in candidates[0].fact.lower(),
            f"failure reason should mention the missing binary: {candidates[0].fact}",
        )

    @mock.patch("pipeline.classifier.subprocess.run")
    def test_timeout_emits_marker(self, mock_run):
        """`claude -p` exceeded subprocess timeout → TimeoutExpired → marker."""
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["claude", "-p"], timeout=120
        )
        candidates = self.clf.classify(
            turn_text="long substantive turn text " * 20,
            session_id="s", cwd="/c", excerpt="e",
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "[FAILURE] classifier")
        self.assertIn("timeout", candidates[0].fact.lower())

    @mock.patch("pipeline.classifier.subprocess.run")
    def test_nonzero_exit_emits_marker(self, mock_run):
        """`claude -p` returned non-zero exit (auth not resolved, internal error,
        rate limit on subscription, etc.) → marker.

        Note: subprocess.run with check=False returns a CompletedProcess with
        a non-zero returncode; it does NOT raise CalledProcessError unless
        check=True is passed. The classifier should inspect returncode and
        treat anything non-zero as a failure regardless of which error class
        the CLI uses to communicate it.
        """
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout="",
            stderr="Not logged in. Please run /login.",
        )
        candidates = self.clf.classify(
            turn_text="long substantive turn text " * 20,
            session_id="s", cwd="/c", excerpt="e",
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "[FAILURE] classifier")
        # Marker should carry stderr context so the user can diagnose.
        self.assertTrue(
            "exit" in candidates[0].fact.lower()
            or "not logged in" in candidates[0].fact.lower()
            or "1" in candidates[0].fact,
            f"failure reason should reference the exit code or stderr: {candidates[0].fact}",
        )

    @mock.patch("pipeline.classifier.subprocess.run")
    def test_invalid_json_output_emits_marker(self, mock_run):
        """If `claude -p --output-format json` returns non-JSON stdout
        (CLI bug, partial output, version skew), produce a marker rather
        than crashing the runner.
        """
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="this is not json {{{",
            stderr="",
        )
        candidates = self.clf.classify(
            turn_text="long substantive turn text " * 20,
            session_id="s", cwd="/c", excerpt="e",
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "[FAILURE] classifier")
        # The failure reason should hint at JSON parsing.
        self.assertTrue(
            "json" in candidates[0].fact.lower()
            or "parse" in candidates[0].fact.lower(),
            f"failure reason should mention JSON parse error: {candidates[0].fact}",
        )

    @mock.patch("pipeline.classifier.subprocess.run")
    def test_nonzero_exit_message_includes_stdout(self, mock_run):
        """When `claude -p` exits non-zero with the error on STDOUT (not
        stderr), the resulting marker's failure reason MUST include that
        stdout content. Otherwise the failure surfaces as a misleading
        "(no stderr)" annotation that hides the real diagnostic.

        WHY this matters (2026-05-14 bug A symptom):
        Under `--output-format json`, `claude -p` commonly writes its
        error envelope to STDOUT (not stderr). The user observed every
        marker reading `Classifier failed: claude -p exited 1: (no stderr)`
        because the implementation only captured stderr in the error
        message — stdout was discarded for failure cases. The actual
        message "Credit balance is too low" (which would have made the
        root cause instantly diagnosable) was lost.
        """
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout="Credit balance is too low",
            stderr="",
        )
        candidates = self.clf.classify(
            turn_text="long substantive turn text " * 20,
            session_id="s", cwd="/c", excerpt="e",
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "[FAILURE] classifier")
        self.assertIn(
            "Credit balance",
            candidates[0].fact,
            f"marker's fact must surface stdout-side error content "
            f"so the user can diagnose without grepping the parent terminal: "
            f"got {candidates[0].fact!r}",
        )

    @mock.patch("pipeline.classifier.subprocess.run")
    def test_nonzero_exit_message_includes_both_streams_when_present(self, mock_run):
        """When BOTH stdout and stderr are non-empty on a non-zero exit,
        both must appear in the failure reason. Some `claude -p` failures
        emit structured JSON on stdout AND a human-readable line on stderr;
        we want both signals reaching the user's review queue.
        """
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=2,
            stdout='{"is_error": true, "result": "rate limited"}',
            stderr="429 Too Many Requests",
        )
        candidates = self.clf.classify(
            turn_text="long substantive turn text " * 20,
            session_id="s", cwd="/c", excerpt="e",
        )
        self.assertEqual(len(candidates), 1)
        fact = candidates[0].fact
        # Sanitization truncates fact to ~200 chars; both signals must
        # remain visible inside that budget. We assert substrings that
        # are short enough to fit even after _sanitize.
        self.assertIn(
            "rate limited", fact,
            f"stdout content must appear in marker fact: {fact!r}",
        )
        self.assertIn(
            "429", fact,
            f"stderr content must appear in marker fact: {fact!r}",
        )

    @mock.patch("pipeline.classifier.subprocess.run")
    def test_marker_failure_reason_is_sanitized(self, mock_run):
        """Bug 3 fix invariant: failure_reason length is capped — even when the
        underlying error message includes a 30+KB blob (e.g. an embedded
        rendered prompt from TimeoutExpired's argv repr).
        """
        # Deliberately bloat the stderr so an unsanitized fact would
        # exceed 200 chars.
        bloated = "x" * 35000
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=2,
            stdout="",
            stderr=bloated,
        )
        candidates = self.clf.classify(
            turn_text="long substantive turn text " * 20,
            session_id="s", cwd="/c", excerpt="e",
        )
        self.assertEqual(len(candidates), 1)
        self.assertLess(
            len(candidates[0].fact), 250,
            f"failure_reason must be sanitized (≤200 chars + prefix); "
            f"got {len(candidates[0].fact)}",
        )


class TestCliPrecheck(unittest.TestCase):
    """`_cli_precheck` is the replacement for `_auth_precheck`. It verifies
    that the `claude` binary is reachable and that a trivial invocation
    succeeds (which transitively confirms subscription auth).

    Strategy: rather than ACTUALLY running `claude --version` (which would
    cost subscription quota and depend on a working network), we just check
    that `which claude` resolves. The deeper auth check is exercised by
    the first real classifier call — failures there produce a marker the
    same way an explicit precheck failure would.
    """

    def test_cli_precheck_passes_when_claude_on_path(self):
        from pipeline import runner

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            # shutil.which is the cleanest way to test this. Patch it to a
            # known truthy value to simulate `claude` being installed.
            with mock.patch("pipeline.runner.shutil.which", return_value="/fake/claude"):
                ok = runner._cli_precheck(
                    pending_path=pending,
                    session_id="test-session",
                    cwd="/test",
                )
            self.assertTrue(ok)
            # No marker should be written on success. (The file may or may
            # not exist — append_candidates creates it on first write — but
            # if it exists it must be empty.)
            if pending.exists():
                self.assertEqual(pending.read_text(), "")

    def test_cli_precheck_fails_when_claude_missing(self):
        """Binary not on PATH → marker emitted, return False."""
        from pipeline import runner

        with tempfile.TemporaryDirectory() as td:
            pending = Path(td) / "pending.md"
            destination = Path(td) / "ObserveIE.md"
            destination.write_text("")
            with mock.patch("pipeline.runner.shutil.which", return_value=None):
                ok = runner._cli_precheck(
                    pending_path=pending,
                    session_id="test-session",
                    cwd="/test",
                )
            self.assertFalse(ok, "precheck must fail when claude is not on PATH")
            self.assertTrue(pending.exists())
            content = pending.read_text()
            self.assertIn("[FAILURE] classifier", content)
            # Failure reason should reference claude/PATH so a user can fix it.
            self.assertTrue(
                "claude" in content.lower() and "path" in content.lower(),
                f"marker should mention `claude` and PATH: {content}",
            )


if __name__ == "__main__":
    unittest.main()
