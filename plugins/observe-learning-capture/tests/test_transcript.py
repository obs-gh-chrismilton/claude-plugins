"""Tests for pipeline.transcript — JSONL turn extraction.

Fixture: tests/fixtures/sample_session.jsonl
Turns in order: u1, a1, u2, a2, a3 (mixed text+tool_use), u3 (tool_result),
                a4 (tool-only, no text), a5 (text-only)

Expected behaviour:
- all_assistant_turns yields only text-bearing assistant turns: a1, a2, a3, a5.
  a4 is filtered out because _extract_text returns "" for tool-only content.
- last_turn_pair returns (u2, a5): u3 is a tool_result turn with no text,
  so _record_to_turn returns None for it, leaving u2 as the last user turn.
"""
import unittest
from pathlib import Path

from pipeline.transcript import (
    last_assistant_turn,
    last_turn_pair,
    all_assistant_turns,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


class TestTranscript(unittest.TestCase):
    # ------------------------------------------------------------------
    # Kept tests (with updated assertions to match expanded fixture)
    # ------------------------------------------------------------------

    def test_last_turn_pair_returns_user_then_assistant(self):
        """last_turn_pair returns (u2, a5) after fixture expansion.

        u3 contains only a tool_result block (no prose text) so
        _record_to_turn returns None for it; u2 remains the last user turn.
        a5 is the final text-bearing assistant turn.
        """
        user, assistant = last_turn_pair(FIXTURE)
        self.assertEqual(user.uuid, "u2")
        self.assertEqual(assistant.uuid, "a5")

    def test_missing_file_returns_none(self):
        """Non-existent path → all functions return None/empty without raising."""
        self.assertIsNone(last_assistant_turn(Path("/nonexistent.jsonl")))

    def test_empty_file_returns_none(self):
        """File that exists but is empty → last_assistant_turn returns None."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as f:
            self.assertIsNone(last_assistant_turn(Path(f.name)))

    # ------------------------------------------------------------------
    # New tests — fixture coverage for tool_use / mixed / tool_result
    # ------------------------------------------------------------------

    def test_mixed_text_and_tool_use_extracts_only_text(self):
        """Assistant turn with [text, tool_use] blocks → only the text block is kept."""
        turns = list(all_assistant_turns(FIXTURE))
        # a3 is the mixed-block turn
        a3 = next(t for t in turns if t.uuid == "a3")
        self.assertEqual(a3.text, "Let me check the dataset.")

    def test_tool_only_assistant_turn_filtered_out(self):
        """Assistant turn with only [tool_use] blocks (no text) must NOT be yielded."""
        turns = list(all_assistant_turns(FIXTURE))
        # a4 is tool-only — must be absent
        uuids = [t.uuid for t in turns]
        self.assertNotIn("a4", uuids)

    def test_tool_result_user_turn_filtered_out(self):
        """User turn containing only a tool_result block (no string prose) is filtered.

        u3 has tool_result content → _extract_text returns "" → _record_to_turn
        returns None → last_turn_pair should return u2 (the previous prose turn),
        not u3.
        """
        user, assistant = last_turn_pair(FIXTURE)
        self.assertEqual(user.uuid, "u2")
        self.assertEqual(assistant.uuid, "a5")

    def test_full_assistant_count(self):
        """After fixture expansion: exactly 4 text-bearing assistant turns yielded."""
        turns = list(all_assistant_turns(FIXTURE))
        self.assertEqual(len(turns), 4)
        self.assertEqual([t.uuid for t in turns], ["a1", "a2", "a3", "a5"])

    def test_last_assistant_turn_is_now_a5(self):
        """Most-recent text-bearing assistant turn is a5, which references deletePoller."""
        turn = last_assistant_turn(FIXTURE)
        self.assertEqual(turn.uuid, "a5")
        self.assertIn("deletePoller", turn.text)


if __name__ == "__main__":
    unittest.main()
