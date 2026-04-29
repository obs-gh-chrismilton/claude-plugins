"""Tests for pipeline.transcript — JSONL turn extraction."""
import unittest
from pathlib import Path

from pipeline.transcript import (
    last_assistant_turn,
    last_turn_pair,
    all_assistant_turns,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


class TestTranscript(unittest.TestCase):
    def test_last_assistant_turn_returns_most_recent(self):
        turn = last_assistant_turn(FIXTURE)
        self.assertIsNotNone(turn)
        self.assertIn("Cascade-ordering deadlock", turn.text)
        self.assertEqual(turn.uuid, "a2")

    def test_last_turn_pair_returns_user_then_assistant(self):
        user, assistant = last_turn_pair(FIXTURE)
        self.assertEqual(user.text, "What happened?")
        self.assertIn("Cascade-ordering", assistant.text)

    def test_all_assistant_turns(self):
        turns = list(all_assistant_turns(FIXTURE))
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0].uuid, "a1")
        self.assertEqual(turns[1].uuid, "a2")

    def test_missing_file_returns_none(self):
        self.assertIsNone(last_assistant_turn(Path("/nonexistent.jsonl")))

    def test_empty_file_returns_none(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as f:
            self.assertIsNone(last_assistant_turn(Path(f.name)))


if __name__ == "__main__":
    unittest.main()
