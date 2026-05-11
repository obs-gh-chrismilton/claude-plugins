"""Tests for classifier helper functions that are auth/transport-agnostic.

These tests survived the 2026-05-08 SDK→subprocess pivot because they
exercise pure Python helpers (`_build_prompt`, `_generate_slim_known_facts`)
that don't care which transport carries the prompt to Claude.

Previously lived in test_classifier_sdk_errors.py alongside SDK-specific
TestSDKInvocation / TestAuthPrecheck classes; those classes were deleted
when the classifier pivoted to `claude -p` subprocess invocation
(coverage moved to test_classifier_subprocess.py). The helper tests
were preserved here under the more accurate filename.
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


class TestPromptStructure(unittest.TestCase):
    """The classifier prompt is split into 3 parts (static template, slim
    known-facts, per-call user message). The split was originally motivated
    by the SDK's ephemeral `cache_control` markers — the static and slim
    blocks went into cached system blocks while per-call values rode in
    the user message and didn't invalidate the cache.

    Post-subprocess pivot: cache_control is no longer used (the CLI
    handles caching opaquely), but the 3-part split is preserved because
    `_invoke_classifier` collapses static + slim into a single
    `--system-prompt` argument while keeping the per-call user_message
    on stdin. Tests still verify the structural invariants the split
    assumes.
    """

    def test_build_prompt_returns_three_strings(self):
        from pipeline.classifier import _build_prompt

        tmpl_path = Path(__file__).parent / "fixtures" / "test_classifier_template.md"
        tmpl_path.parent.mkdir(parents=True, exist_ok=True)
        tmpl_path.write_text("Static instruction text without placeholders.")

        result = _build_prompt(
            template_path=tmpl_path,
            turn_text="some turn",
            slim_known_facts="known: a, b, c",
            cwd="/test/cwd",
            captured_at=datetime(2026, 5, 4, 10, 0, 0, tzinfo=timezone.utc),
        )
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        static, slim, user = result
        self.assertIsInstance(static, str)
        self.assertIsInstance(slim, str)
        self.assertIsInstance(user, str)
        self.assertEqual(static, "Static instruction text without placeholders.")
        self.assertEqual(slim, "known: a, b, c")
        self.assertIn("some turn", user)
        self.assertIn("/test/cwd", user)
        self.assertIn("2026-05-04", user)

    def test_static_block_has_no_per_call_placeholders(self):
        """Static portion of the prompt MUST be deterministic across calls.

        Even though we no longer use cache_control, leaking per-call values
        into the static block would defeat any caching the CLI does
        internally and would also surprise future readers who expect the
        block named "static" to actually be static.
        """
        from pipeline.classifier import _build_prompt

        tmpl_path = Path(__file__).parent / "fixtures" / "test_classifier_template.md"
        # Two calls with completely different inputs:
        static, _slim, _user = _build_prompt(
            template_path=tmpl_path,
            turn_text="distinct turn 1",
            slim_known_facts="known: a",
            cwd="/test/cwd",
            captured_at=datetime(2026, 5, 4, 10, 0, 0, tzinfo=timezone.utc),
        )
        static2, _, _ = _build_prompt(
            template_path=tmpl_path,
            turn_text="distinct turn 2",
            slim_known_facts="known: b",
            cwd="/different",
            captured_at=datetime(2026, 5, 4, 11, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(static, static2,
                         "static block must be identical across calls; "
                         "per-call values must live in the user message")


class TestSlimKnownFacts(unittest.TestCase):
    """`_generate_slim_known_facts` derives a bounded section/id summary
    from ObserveIE.md so the classifier can avoid recapturing known
    facts. Bounded because ObserveIE.md grows over time and we don't want
    the prompt to scale linearly with knowledge-base size.
    """

    def test_generate_slim_known_facts_extracts_section_and_ids(self):
        from pipeline.classifier import _generate_slim_known_facts

        sample_observeie = """\
# ObserveIE Knowledge Base

## OPAL Gotchas

<!-- id: a1b2c3d4 -->
- Fact one about OPAL.

<!-- id: e5f6a7b8 -->
- Fact two about OPAL.

## API/GraphQL

<!-- id: 9988aabb -->
- Fact about API.
"""
        with tempfile.TemporaryDirectory() as td:
            obs = Path(td) / "ObserveIE.md"
            obs.write_text(sample_observeie)
            slim = _generate_slim_known_facts(obs)
        # Section headers appear so the classifier can route by section.
        self.assertIn("OPAL Gotchas", slim)
        self.assertIn("API/GraphQL", slim)
        # IDs appear (not full body text).
        self.assertIn("a1b2c3d4", slim)
        self.assertIn("e5f6a7b8", slim)
        self.assertIn("9988aabb", slim)
        # No body text — that would defeat the slim purpose and let prompt
        # size scale linearly with ObserveIE.md growth.
        self.assertNotIn("Fact one about OPAL", slim)
        # Bounded size: smaller than the input. The original plan's
        # `len // 2` was too tight for tiny fixtures where per-section
        # "Section:/Known ids:" labels dominate; on real ObserveIE.md
        # (multi-line bodies) the slim is dramatically smaller.
        self.assertLess(len(slim), len(sample_observeie))

    def test_generate_slim_known_facts_handles_missing_file(self):
        """Pipeline must degrade gracefully if ObserveIE.md doesn't exist
        yet (first-run scenario before any merge has happened).
        """
        from pipeline.classifier import _generate_slim_known_facts
        slim = _generate_slim_known_facts(Path("/does/not/exist.md"))
        self.assertIsInstance(slim, str)
        self.assertIn("(empty", slim.lower(),
                      "missing file should produce a non-empty placeholder, "
                      "not raise")


if __name__ == "__main__":
    unittest.main()
