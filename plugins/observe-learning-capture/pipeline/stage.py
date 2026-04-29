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
import re
import sys
import time
from pathlib import Path
from typing import Any, List

from pipeline.types import Candidate


# ---------------------------------------------------------------------------
# Lock configuration (C3)
# ---------------------------------------------------------------------------

# How long to spin-wait for LOCK_EX before falling back to a PID-suffixed file.
# Per spec §9: if lock fails after 2s, write to sibling file and log to stderr.
_LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_RETRY_INTERVAL = 0.05


def append_candidates(pending_file: Path, candidates: List[Candidate]) -> None:
    """Append candidates to the pending YAML file. Creates parent dirs.
    No-op on empty list. Uses POSIX flock with 2-second non-blocking
    acquisition timeout; on timeout, writes to a PID-suffixed sibling
    file per spec §9.

    Args:
        pending_file: Path to the `.pending.md` staging file.
        candidates: Candidates to append. Empty list is a no-op (no file created).
    """
    if not candidates:
        return

    # Create parent directories if they don't exist yet — this handles
    # first-run where ~/.claude/ subdir may not exist.
    pending_file.parent.mkdir(parents=True, exist_ok=True)

    # Try main file with timed flock (C3)
    if _try_write_with_lock(pending_file, candidates):
        return

    # Lock timeout — write to PID-suffixed fallback (spec §9)
    fallback = pending_file.parent / f"{pending_file.name}.{os.getpid()}"
    print(
        f"[observe-learning-capture] stage.py: lock timeout on {pending_file}; "
        f"falling back to {fallback}",
        file=sys.stderr,
    )
    _write_unlocked(fallback, candidates)


def _try_write_with_lock(path: Path, candidates: List[Candidate]) -> bool:
    """Attempt LOCK_EX with non-blocking retries until timeout (C3).

    Spins with LOCK_NB up to _LOCK_TIMEOUT_SECONDS, sleeping
    _LOCK_RETRY_INTERVAL between attempts. Returns True on success,
    False on timeout — caller should fall back to PID-suffixed sibling.

    Args:
        path: File to open for append and lock.
        candidates: Candidates to write once lock is acquired.

    Returns:
        True if lock was acquired and data was written; False on timeout.
    """
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    try:
        f = path.open("a", encoding="utf-8")
    except OSError as exc:
        # Can't even open the file — log and signal failure to caller.
        print(
            f"[observe-learning-capture] stage.py: cannot open {path}: {exc}",
            file=sys.stderr,
        )
        return False
    try:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break  # lock acquired
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_LOCK_RETRY_INTERVAL)
        for cand in candidates:
            f.write("---\n")
            f.write(_render_yaml(cand.to_yaml_record(), indent=0))
            f.write("\n")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            # Unlock failure is non-fatal — data is already written; OS
            # will release the lock when the fd closes in the finally block.
            pass
        return True
    finally:
        f.close()


def _write_unlocked(path: Path, candidates: List[Candidate]) -> None:
    """Write candidates to a fallback file without locking.

    Used when the main pending file lock cannot be acquired in time
    (spec §9 fallback path). OSError is logged to stderr rather than
    swallowed — the merge step must be aware of write failures.

    Args:
        path: Fallback file path (typically PID-suffixed sibling of main).
        candidates: Candidates to write.
    """
    try:
        with path.open("a", encoding="utf-8") as f:
            for cand in candidates:
                f.write("---\n")
                f.write(_render_yaml(cand.to_yaml_record(), indent=0))
                f.write("\n")
    except OSError as exc:
        print(
            f"[observe-learning-capture] stage.py: fallback write to {path} "
            f"failed: {exc}",
            file=sys.stderr,
        )


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
        # Intentionally silent: missing file is the steady-state first-run case.
        return []
    try:
        content = pending_file.read_text(encoding="utf-8")
    except OSError as exc:
        # I1: log full context so the caller/operator knows what went wrong.
        # WHY: silent swallow here would hide permission errors, broken mounts, etc.
        print(
            f"[observe-learning-capture] stage.py: cannot read {pending_file}: {exc}",
            file=sys.stderr,
        )
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
            if isinstance(v, list) and not v:
                # C2: empty list — emit inline `[]` so the parser sees a
                # known token, not an absent block. Without this guard the
                # empty list falls through to _scalar([]) → str([]) → "[]"
                # which then round-trips as the string "[]".
                out.append(f"{pad}{k}: []")
            elif isinstance(v, dict) and not v:
                # C2: same for empty dicts — emit inline `{}`.
                out.append(f"{pad}{k}: {{}}")
            elif isinstance(v, (dict, list)):
                # Non-empty nested block — emit key on its own line, then recurse.
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
        characters that would confuse a YAML parser (colon, hash, etc.),
        has surrounding whitespace, or looks like a YAML special token
        (number, bool, null) — the last condition prevents type-drift on
        round-trip (I3 fix: e.g. prompt_version "1.0" stays a string).
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        # Actual numeric type — render bare (no quotes).
        return str(v)
    s = str(v)
    # Quote if the value contains chars that could confuse a YAML parser.
    # WHY: colons in timestamps (e.g. "2026-04-29T11:33:00+00:00"), hashes
    # in comments, percent in URLs, etc. would all break unquoted parsing.
    needs_quote = any(c in s for c in ":#&*!|>'\"%@`") or s.strip() != s
    # I3: also quote strings that LOOK like numbers, booleans, or null.
    # Without this, _parse_scalar("1.0") returns float(1.0), so writing the
    # string "1.0" and reading it back yields a float — silent type drift.
    if re.match(r"^-?\d+(\.\d+)?$", s) or s in ("null", "true", "false", ""):
        needs_quote = True
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

    Splits ONLY on lines consisting of just `---` (per YAML spec).
    Naïve content.split("---") would corrupt records containing `---`
    inside scalar values (C1 fix).

    Args:
        content: Raw text of the pending file.

    Returns:
        List of dicts, one per non-empty document. Malformed documents
        are skipped and logged to stderr (I2 fix).
    """
    records: List[dict[str, Any]] = []
    # C1: line-anchored split — only a line that IS exactly `---` (optionally
    # with trailing whitespace) acts as a document boundary. This prevents
    # `---` embedded in scalar values from splitting mid-record.
    docs = [d.strip() for d in re.split(r"(?m)^---\s*$", content) if d.strip()]
    for doc_idx, doc in enumerate(docs):
        try:
            parsed = _parse_yaml_block(doc.splitlines(), 0)[0]
            if isinstance(parsed, dict):
                records.append(parsed)
        except (ValueError, IndexError) as exc:
            # I2: log with record index so the operator can locate the bad entry.
            # WHY: a corrupt entry should not block reading valid entries, but
            # silent drops make debugging impossible.
            print(
                f"[observe-learning-capture] stage.py: skipped malformed YAML "
                f"record (index {doc_idx}): {exc}",
                file=sys.stderr,
            )
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
            # I4: peek at the next non-empty line to decide whether this is
            # a genuinely empty value (None) or the start of a nested block.
            # Previous code always assumed nested block, returning {} for
            # `key:\n` — but an empty value should be None per YAML spec.
            next_indent = None
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    next_indent = len(lines[j]) - len(lines[j].lstrip())
                    break
            if next_indent is None or next_indent <= base_indent:
                # No nested content found — value is None
                result[key] = None
                i += 1
            else:
                # Nested block follows at deeper indentation
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
            # Inline value — handle empty-container markers before scalar parse.
            # C2: `[]` and `{}` written by _render_yaml must come back as the
            # correct empty Python container, not the string "[]" or "{}".
            if val == "[]":
                result[key] = []
            elif val == "{}":
                result[key] = {}
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
    # Quoted values are always returned as str — no further coercion.
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    # Try numeric coercions before falling back to string.
    # WHY: only unquoted tokens reach here; quoted numeric-looking strings
    # were already returned above, preventing I3-type float drift.
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s
