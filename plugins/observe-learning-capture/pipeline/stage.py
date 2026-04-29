"""Append-only writer for the pending candidate queue.

Format: YAML list, one document per candidate, separated by `---`.
Append-only — never rewrites or reorders. Keeps `git diff` friendly if
the user version-controls their `~/.claude/`.

We write YAML manually (simple-shape only) to avoid pyyaml dependency.
The schema is restricted to scalars + lists + nested dicts of scalars,
which is safely round-trippable through hand-written code.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Any, List

from pipeline.types import Candidate


def append_candidates(pending_file: Path, candidates: List[Candidate]) -> None:
    """Append candidates to the pending YAML file. Creates parent dirs.
    No-op on empty list. Uses POSIX flock to avoid concurrent-write races
    (spec §9 error handling row).

    Args:
        pending_file: Path to the `.pending.md` staging file.
        candidates: Candidates to append. Empty list is a no-op (no file created).
    """
    if not candidates:
        return

    # Create parent directories if they don't exist yet — this handles
    # first-run where ~/.claude/ subdir may not exist.
    pending_file.parent.mkdir(parents=True, exist_ok=True)

    # Open for append; create if needed
    with pending_file.open("a", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except OSError:
            # Lock failed — proceed anyway. Worst case: malformed concat.
            # Acceptable per spec §9 fallback.
            pass
        for cand in candidates:
            f.write("---\n")
            f.write(_render_yaml(cand.to_yaml_record(), indent=0))
            f.write("\n")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def read_pending(pending_file: Path) -> List[dict[str, Any]]:
    """Parse pending YAML file. Returns list of records (dicts).
    Returns empty list if file missing.

    Args:
        pending_file: Path to the `.pending.md` staging file.

    Returns:
        List of dicts, one per `---` document in the file.
        Returns [] if the file does not exist or cannot be read.
    """
    if not pending_file.exists():
        return []
    try:
        content = pending_file.read_text(encoding="utf-8")
    except OSError:
        # Unreadable file — log context would go here in a real handler;
        # surface [] to caller per graceful-fallback rule.
        return []
    return _parse_yaml_list(content)


# ---------------------------------------------------------------------------
# YAML rendering
# ---------------------------------------------------------------------------
# Hand-rolled to avoid pyyaml dep. Handles only the shapes we emit (string,
# int, float, bool, None, list of scalars, list of dicts, dict of scalars).
# If we ever need more complex shapes, swap to ruamel.yaml or pyyaml — but
# document it as a dep change.


def _render_yaml(value: Any, indent: int) -> str:
    """Recursively render a value (dict, list, or scalar) as YAML lines.

    Args:
        value: The value to render. Supported types: dict, list, str,
               int, float, bool, None.
        indent: Current indentation level (each level adds 2 spaces).

    Returns:
        Multi-line string (no trailing newline) suitable for embedding
        between `---` document separators.
    """
    pad = "  " * indent
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                # Nested block — emit key on its own line, then recurse.
                out.append(f"{pad}{k}:")
                out.append(_render_yaml(v, indent + 1))
            elif isinstance(v, str) and ("\n" in v or len(v) > 80):
                # Multi-line or long string — YAML literal block scalar (`|`).
                # WHY: preserves newlines exactly; avoids quoting issues on
                # long excerpts that often contain colons, URLs, etc.
                out.append(f"{pad}{k}: |")
                for line in v.splitlines():
                    out.append(f"{pad}  {line}")
            else:
                out.append(f"{pad}{k}: {_scalar(v)}")
        return "\n".join(out)
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, (dict, list)):
                # Nested block inside a list — emit `-` then recurse.
                out.append(f"{pad}-")
                out.append(_render_yaml(item, indent + 1))
            else:
                out.append(f"{pad}- {_scalar(item)}")
        return "\n".join(out)
    # Bare scalar at top level (unusual but handle gracefully)
    return f"{pad}{_scalar(value)}"


def _scalar(v: Any) -> str:
    """Render a scalar value as a YAML-safe string token.

    Args:
        v: Scalar (str, int, float, bool, None).

    Returns:
        YAML token, quoted with double-quotes if the value contains
        characters that would confuse a YAML parser (colon, hash, etc.)
        or has surrounding whitespace.
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # Quote if contains chars that would confuse a YAML parser.
    # WHY: colons in values (e.g. ISO timestamps "2026-04-29T11:33:00+00:00")
    # are ambiguous in unquoted YAML; simpler to quote defensively.
    needs_quote = any(c in s for c in ":#&*!|>'\"%@`") or s.strip() != s
    if needs_quote:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


# ---------------------------------------------------------------------------
# YAML parsing
# ---------------------------------------------------------------------------
# Same restriction: only handles shapes we emit. Safe-list parser, not a
# full YAML implementation. If shapes change, this needs updating.


def _parse_yaml_list(content: str) -> List[dict[str, Any]]:
    """Parse a stream of `---`-separated YAML documents.

    Args:
        content: Raw text of the pending file.

    Returns:
        List of dicts, one per non-empty document. Malformed documents
        are silently skipped (logged at higher level when integrated).
    """
    records: List[dict[str, Any]] = []
    docs = [d.strip() for d in content.split("---") if d.strip()]
    for doc in docs:
        try:
            parsed = _parse_yaml_block(doc.splitlines(), 0)[0]
            if isinstance(parsed, dict):
                records.append(parsed)
        except (ValueError, IndexError):
            # Tolerate malformed; surface in logs at higher level.
            # WHY: a corrupt entry should not block reading valid entries.
            continue
    return records


def _parse_yaml_block(lines: list[str], base_indent: int) -> tuple[Any, int]:
    """Parse a block of lines as a YAML mapping starting at base_indent.

    Args:
        lines: List of raw text lines to parse.
        base_indent: Expected indentation level for keys in this block.

    Returns:
        (dict, lines_consumed) — the parsed dict and how many lines were
        consumed. Lines with deeper indentation are handled by recursive
        calls; lines with shallower indentation signal end of block.
    """
    result: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            # Skip blank lines
            i += 1
            continue
        stripped = line.lstrip()
        line_indent = len(line) - len(stripped)
        if line_indent < base_indent:
            # Back-indented — this block is done
            break
        if line_indent > base_indent:
            # Over-indented line with no parent key — skip it.
            # Shouldn't happen in well-formed output but be tolerant.
            i += 1
            continue
        if ":" not in stripped:
            # Not a key:value line — skip
            i += 1
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        if not val:
            # Empty value → nested block follows on the next lines.
            sub_lines = lines[i + 1:]
            sub_value, consumed = _parse_yaml_subblock(sub_lines, base_indent + 2)
            result[key] = sub_value
            i += 1 + consumed
        elif val == "|":
            # Multi-line literal block scalar — collect lines until
            # we see a line that's not indented deeper than base.
            block_lines = []
            i += 1
            while i < len(lines):
                ln = lines[i]
                if ln.strip() and (len(ln) - len(ln.lstrip())) <= base_indent:
                    # Back to base or less — literal block is done
                    break
                # Strip the block scalar's indentation prefix
                if len(ln) > base_indent + 2:
                    block_lines.append(ln[base_indent + 2:])
                elif ln.strip():
                    block_lines.append(ln.lstrip())
                else:
                    block_lines.append("")
                i += 1
            result[key] = "\n".join(block_lines).rstrip()
        else:
            result[key] = _parse_scalar(val)
            i += 1
    return result, i


def _parse_yaml_subblock(lines: list[str], indent: int) -> tuple[Any, int]:
    """Decide if subblock is a list or dict, parse accordingly.

    Args:
        lines: Lines remaining after the parent key line.
        indent: Expected indentation for this sub-block's content.

    Returns:
        (value, lines_consumed) — either a dict or list, and line count.
    """
    if not lines:
        return {}, 0
    # Find first non-empty line to detect whether this is a list or map
    first = None
    for line in lines:
        if line.strip():
            first = line
            break
    if first is None:
        return {}, 0
    stripped = first.lstrip()
    if stripped.startswith("- "):
        return _parse_yaml_list_block(lines, indent)
    return _parse_yaml_block(lines, indent)


def _parse_yaml_list_block(lines: list[str], indent: int) -> tuple[list, int]:
    """Parse a `- item\\n- item\\n...` block at the given indent.

    Args:
        lines: Lines to parse (starting from the first `- ` entry).
        indent: Expected indentation for `- ` markers.

    Returns:
        (list, lines_consumed)
    """
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent < indent:
            # Back-indented — list done
            break
        stripped = line.lstrip()
        if stripped.startswith("- "):
            val = stripped[2:].strip()
            result.append(_parse_scalar(val))
            i += 1
        else:
            i += 1
    return result, i


def _parse_scalar(s: str) -> Any:
    """Parse a YAML scalar token back to a Python value.

    Handles: null, true/false booleans, ints, floats, quoted strings,
    and bare strings. Inverse of _scalar() for the shapes we emit.

    Args:
        s: Raw token string from a YAML line.

    Returns:
        Python value: None, bool, int, float, or str.
    """
    if s == "null" or s == "":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    # Strip double-quote wrapping that _scalar() adds for special chars.
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    # Try numeric coercions before falling back to string.
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s
