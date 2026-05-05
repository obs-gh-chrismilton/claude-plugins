"""Tests for current_logical_turn — Bug 1 walker.

Bug 1: prior code (last_assistant_turn) returned only the LAST single
assistant JSONL record. Modern Claude Code emits ~6 records per logical
turn (interleaved text/thinking/tool_use); the last one is often a
tool_use chunk (empty after text-filter) or a 7-char ack like "Saved.".

Fix: walk backward through assistant records, collecting .text from each,
stopping at the first real user prompt. The result is the substantive
content of the current logical turn.
"""
import json
import tempfile
import unittest
from pathlib import Path


def _write_transcript(td: Path, records: list[dict]) -> Path:
    p = td / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _user(text: str) -> dict:
    return {"type": "user", "message": {"content": text}, "uuid": "u", "timestamp": "2026-05-04T10:00:00Z"}


def _user_tool_result(text: str) -> dict:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": text}]},
        "uuid": "u", "timestamp": "2026-05-04T10:00:00Z",
    }


def _assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
        "uuid": "a", "timestamp": "2026-05-04T10:00:00Z",
    }


def _assistant_tool_use() -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}}]},
        "uuid": "a", "timestamp": "2026-05-04T10:00:00Z",
    }


def _assistant_thinking(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": text}]},
        "uuid": "a", "timestamp": "2026-05-04T10:00:00Z",
    }


class TestCurrentLogicalTurn(unittest.TestCase):
    def test_aggregates_multiple_assistant_records_per_logical_turn(self):
        from pipeline.transcript import current_logical_turn
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            transcript = _write_transcript(td, [
                _user("Real user prompt about OPAL"),
                _assistant_thinking("Let me think about this"),
                _assistant_text("First substantive paragraph about OPAL behavior"),
                _assistant_tool_use(),
                _user_tool_result("ls output"),
                _assistant_text("Second substantive paragraph after tool call"),
                _assistant_tool_use(),
                _assistant_text("Saved."),  # short trailing ack
            ])
            turn = current_logical_turn(transcript)
            self.assertIsNotNone(turn)
            # All three text blocks should be present (substantive + Saved.)
            self.assertIn("First substantive paragraph", turn.text)
            self.assertIn("Second substantive paragraph", turn.text)
            self.assertIn("Saved.", turn.text)
            # Should NOT include the previous user prompt
            self.assertNotIn("Real user prompt", turn.text)

    def test_stops_at_real_user_prompt_does_not_aggregate_prior_turn(self):
        from pipeline.transcript import current_logical_turn
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            transcript = _write_transcript(td, [
                _user("First prompt"),
                _assistant_text("First answer"),
                _user("Second prompt"),
                _assistant_text("Second answer"),
            ])
            turn = current_logical_turn(transcript)
            self.assertIsNotNone(turn)
            self.assertIn("Second answer", turn.text)
            self.assertNotIn("First answer", turn.text)
            self.assertNotIn("First prompt", turn.text)

    def test_walks_through_tool_result_user_records(self):
        # tool_result records should NOT stop the walk-back
        from pipeline.transcript import current_logical_turn
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            transcript = _write_transcript(td, [
                _user("Real prompt"),
                _assistant_text("Before tool"),
                _assistant_tool_use(),
                _user_tool_result("tool returned this"),
                _assistant_text("After tool"),
            ])
            turn = current_logical_turn(transcript)
            self.assertIsNotNone(turn)
            self.assertIn("Before tool", turn.text)
            self.assertIn("After tool", turn.text)

    def test_walks_through_clear_command_records(self):
        # /clear at the boundary of a logical turn shouldn't get aggregated
        from pipeline.transcript import current_logical_turn
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            transcript = _write_transcript(td, [
                _user("Old prompt before /clear"),
                _assistant_text("Old assistant text"),
                _user("/clear"),  # injected, should be skipped
                _user("New real prompt"),
                _assistant_text("New assistant text"),
            ])
            turn = current_logical_turn(transcript)
            self.assertIsNotNone(turn)
            self.assertIn("New assistant text", turn.text)
            self.assertNotIn("Old assistant text", turn.text)

    def test_returns_none_when_no_assistant_text_in_turn(self):
        from pipeline.transcript import current_logical_turn
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            transcript = _write_transcript(td, [
                _user("Real prompt"),
                _assistant_tool_use(),  # only tool calls, no text
            ])
            turn = current_logical_turn(transcript)
            self.assertIsNone(turn)

    def test_returns_none_for_missing_file(self):
        from pipeline.transcript import current_logical_turn
        turn = current_logical_turn(Path("/does/not/exist.jsonl"))
        self.assertIsNone(turn)
