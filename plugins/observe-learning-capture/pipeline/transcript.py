"""Read Claude Code session transcript JSONL files.

Claude Code writes one JSON object per line. Each line is a "turn" —
either user, assistant, tool_use, tool_result, or system. We care about
user and assistant turns for learning capture.

The transcript path is provided to hooks via $CLAUDE_TRANSCRIPT_PATH.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple


# Patterns that mark a user-typed JSONL record as NOT a real user prompt.
# These are slash-commands and synthetic injections that Claude Code writes
# as type=user, content=string but which weren't typed by the human user.
# See pipeline/transcript.py:current_logical_turn for use.
_USER_INJECTION_PREFIXES = (
    "/clear",
    "/compact",
    "/init",
    "/cost",
    "/help",
    "/memory",
    "<command-name>",
    "<system-reminder>",
    "<local-command-stdout>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<ide_selection>",
    # Tightened from bare "=== " to namespace on this plugin's own
    # SessionStart marker only; bare "=== " would misclassify legitimate
    # markdown banner rules like "=== Section ===" as injections.
    "=== OBSERVE",
)


def _is_real_user_prompt(record: dict) -> bool:
    """Return True iff this JSONL record represents a real user-typed prompt.

    Bug 1 fix: the "current logical turn" walker uses this to know when
    to stop walking back through the transcript. Modern Claude Code emits
    user-typed records for many non-prompt events: tool_results (list
    content), slash-commands like /clear and /compact (string content,
    but not really a prompt), and hook-injected synthetic blocks (this
    plugin's own SessionStart review block, for instance).

    Returns False for:
    - records where type != "user"
    - records whose content is a list (tool_results)
    - records whose string content starts with a known injection prefix

    Per code-architect drift-detection note: callers should consider
    emitting a marker when this returns False on a string-content user
    record that doesn't match any known prefix — that signals a new
    injection type has shipped that we don't recognize yet.
    """
    if record.get("type") != "user":
        return False
    content = record.get("message", {}).get("content")
    if not isinstance(content, str):
        return False  # tool_results and other list-content records
    stripped = content.lstrip()
    for prefix in _USER_INJECTION_PREFIXES:
        if stripped.startswith(prefix):
            return False
    return True


@dataclass
class Turn:
    """A single turn from the session transcript."""

    role: str  # "user" or "assistant"
    text: str  # extracted text content (concatenated for multi-block messages)
    uuid: str
    timestamp: str


def _iter_jsonl(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL file. Skips malformed lines
    and reports them to stderr; returns nothing on missing-file (silent)
    or read-error (logged to stderr).

    Per design §9: malformed-line and read-error cases are logged with
    full context but never raise — partial captures are still useful.
    """
    if not path.exists():
        # Silent return on missing file — expected at SessionStart before
        # Claude Code creates the transcript.
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    # Log the bad line with full context so it can be debugged,
                    # but don't abort — we still want whatever is parseable.
                    print(
                        f"[observe-learning-capture] transcript.py: skipping malformed JSON "
                        f"in {path}: {exc}",
                        file=sys.stderr,
                    )
                    continue
    except OSError as exc:
        # Log the I/O error (permissions, etc.) and abort the generator.
        # Caller receives an empty iterator; the hook degrades gracefully.
        print(
            f"[observe-learning-capture] transcript.py: cannot read {path}: {exc}",
            file=sys.stderr,
        )
        return


def _extract_text(message_content: object) -> str:
    """Pull plain text from a message.content field.

    Claude Code stores assistant content as a list of blocks (text, tool_use,
    tool_result). User content is usually a string but can be a list. We
    concatenate all text blocks; non-text blocks are ignored.
    """
    if isinstance(message_content, str):
        return message_content
    if isinstance(message_content, list):
        texts = []
        for block in message_content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "\n".join(texts)
    return ""


def _record_to_turn(record: dict) -> Optional[Turn]:
    """Convert a JSONL record to a Turn, or None if it's not a user/assistant turn."""
    role = record.get("type")
    if role not in ("user", "assistant"):
        return None
    message = record.get("message", {})
    text = _extract_text(message.get("content"))
    if not text:
        return None
    return Turn(
        role=role,
        text=text,
        uuid=record.get("uuid", ""),
        timestamp=record.get("timestamp", ""),
    )


def last_assistant_turn(path: Path) -> Optional[Turn]:
    """Return the most recent assistant turn, or None if file missing/empty."""
    last: Optional[Turn] = None
    for record in _iter_jsonl(path):
        turn = _record_to_turn(record)
        if turn is not None and turn.role == "assistant":
            last = turn
    return last


def current_logical_turn(path: Path) -> Optional[Turn]:
    """Return the substantive content of the most recent logical turn.

    Bug 1 fix: prior code (last_assistant_turn) returned only the LAST
    single assistant JSONL record. Modern Claude Code emits ~6 records
    per logical turn (interleaved text/thinking/tool_use); the last one
    is often a tool_use chunk (empty after text-filter) or a 7-char ack
    like "Saved." that fails the prefilter's 150-char gate.

    This function walks the records list backward, collecting text from
    each consecutive assistant record. It stops at the first record where
    _is_real_user_prompt returns True (a real user prompt — not a slash
    command, hook injection, or tool_result). The collected text is then
    re-reversed (so it reads chronologically) and joined.

    Returns None if no assistant text was collected (transcript missing,
    empty, or the current turn has no text-bearing records).
    """
    records = list(_iter_jsonl(path))
    if not records:
        return None

    collected_texts: List[str] = []
    last_assistant_record: Optional[dict] = None
    for record in reversed(records):
        if record.get("type") == "assistant":
            text = _extract_text(record.get("message", {}).get("content"))
            if text:
                collected_texts.append(text)
                if last_assistant_record is None:
                    last_assistant_record = record
            continue
        if _is_real_user_prompt(record):
            break  # boundary of the current logical turn
        # Otherwise: tool_result records, slash commands, hook injections —
        # walk through them without collecting and without stopping.

    if not collected_texts:
        return None

    # Re-reverse so the joined text reads chronologically.
    chronological = list(reversed(collected_texts))
    full_text = "\n".join(chronological)
    return Turn(
        role="assistant",
        text=full_text,
        uuid=(last_assistant_record or {}).get("uuid", ""),
        timestamp=(last_assistant_record or {}).get("timestamp", ""),
    )


def last_turn_pair(path: Path) -> Tuple[Optional[Turn], Optional[Turn]]:
    """Return (last_user_turn, last_assistant_turn).

    Useful for classifier context: "user asked X, assistant said Y."
    """
    user: Optional[Turn] = None
    assistant: Optional[Turn] = None
    for record in _iter_jsonl(path):
        turn = _record_to_turn(record)
        if turn is None:
            continue
        if turn.role == "user":
            user = turn
        elif turn.role == "assistant":
            assistant = turn
    return user, assistant


def all_assistant_turns(path: Path) -> Iterator[Turn]:
    """Yields every assistant turn in transcript order. Used for SessionEnd full-scan.

    Note: returns a generator — iterating it a second time yields nothing.
    Callers needing multiple passes: `turns = list(all_assistant_turns(path))`.
    """
    for record in _iter_jsonl(path):
        turn = _record_to_turn(record)
        if turn is not None and turn.role == "assistant":
            yield turn
