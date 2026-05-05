"""Tests for _is_real_user_prompt — distinguishes real user prompts from
injected/synthetic user records.

Bug 1 fix: the "current logical turn" walker stops at the most recent
real user prompt. Modern Claude Code emits user-typed records for many
non-prompt events: /clear, /compact, hook-injected SessionStart context,
tool_results (which have list content, not string), etc. Treating any
of these as the boundary breaks turn aggregation.

Per code-architect drift-detection note: unknown user-record patterns
should emit a marker so we notice when new injection types ship.
"""
import unittest


class TestIsRealUserPrompt(unittest.TestCase):
    def test_string_user_message_is_real_prompt(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "user", "message": {"content": "Hey, can you help me with X?"}}
        self.assertTrue(_is_real_user_prompt(record))

    def test_list_user_content_is_not_real_prompt(self):
        # tool_result records have list content
        from pipeline.transcript import _is_real_user_prompt
        record = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "result"}]},
        }
        self.assertFalse(_is_real_user_prompt(record))

    def test_clear_command_is_not_real_prompt(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "user", "message": {"content": "/clear"}}
        self.assertFalse(_is_real_user_prompt(record))

    def test_compact_command_is_not_real_prompt(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "user", "message": {"content": "/compact"}}
        self.assertFalse(_is_real_user_prompt(record))

    def test_session_start_injection_is_not_real_prompt(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {
            "type": "user",
            "message": {"content": "=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ==="},
        }
        self.assertFalse(_is_real_user_prompt(record))

    def test_command_name_tag_is_not_real_prompt(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "user", "message": {"content": "<command-name>commit</command-name>"}}
        self.assertFalse(_is_real_user_prompt(record))

    def test_system_reminder_tag_is_not_real_prompt(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "user", "message": {"content": "<system-reminder>Auto mode active</system-reminder>"}}
        self.assertFalse(_is_real_user_prompt(record))

    def test_real_prompt_with_punctuation_is_real(self):
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "user", "message": {"content": "What does deleteDataset() return on cascade failure?"}}
        self.assertTrue(_is_real_user_prompt(record))

    def test_assistant_record_returns_false(self):
        # Defensive: function should never claim an assistant record is a user prompt
        from pipeline.transcript import _is_real_user_prompt
        record = {"type": "assistant", "message": {"content": "I am the assistant"}}
        self.assertFalse(_is_real_user_prompt(record))
