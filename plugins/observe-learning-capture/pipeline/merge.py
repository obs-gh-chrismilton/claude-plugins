"""Merge an approved candidate into ObserveIE.md, remove from pending.

Section routing:
- If `## proposed_section` heading exists (case-insensitive) → append bullet
  under it (right after the section header line, ahead of existing bullets,
  or at section end — we choose section-end for predictable diff).
- If section doesn't exist → append a new `## proposed_section` at file end.

Bullet format (matches spec §7.2):
    - {fact} <!-- id:{id} captured:{date} -->

WHY the bullet format is locked: pipeline/dedupe.py uses a regex to parse
these comment annotations back out when checking for existing content.
Changing this format without updating dedupe.py will break deduplication.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.stage import read_pending
from pipeline.types import Candidate


def merge_candidate(candidate: Candidate, observeie_md: Path) -> None:
    """Promote a candidate bullet into ObserveIE.md. Creates file/section as needed.

    Reads the entire file, locates the target section (or creates one),
    appends the bullet at section-end, then writes the whole file back.
    This is an intentional read-modify-write: ObserveIE.md is not append-only
    (unlike the pending queue), and write-in-place would require more complex
    seek/splice logic with no benefit for a human-edited markdown file.

    Args:
        candidate: The approved Candidate to promote.
        observeie_md: Path to the destination ObserveIE.md file.

    Raises:
        OSError: If the file cannot be written (permission denied, etc.).
                 Not swallowed — caller must handle or let it propagate.
    """
    # Ensure parent directory exists — handles first-run where the target
    # directory may not yet exist (e.g. ~/.claude/ on a fresh install).
    observeie_md.parent.mkdir(parents=True, exist_ok=True)

    # C1 fix: sanitize HTML comment markers in fact text.
    # A fact that describes the plugin's own annotation format (e.g.
    # "the plugin emits <!-- id:abcd1234 ... -->") would otherwise inject a
    # spurious <!-- id:... --> token that dedupe.py's regex extracts as a
    # real id. Future candidates with that id would be spuriously deduped.
    # Zero-width space (U+200B) breaks the <!-- and --> token recognition
    # in dedupe.py's regex without changing how the text looks to a human
    # reader in rendered markdown.
    safe_fact = (
        candidate.fact.strip()
        .replace("<!--", "<​!--")   # zero-width space after < breaks <!--
        .replace("-->", "--​>")     # zero-width space before > breaks -->
    )

    # I1 fix: strip leading markdown list markers from the fact string.
    # If a candidate's fact starts with "- ", "* ", or "+ ", the rendered
    # bullet would be "- - text" (a double-bullet). Strip the leading marker
    # so the output is a clean single bullet.
    safe_fact = re.sub(r"^[-*+]\s+", "", safe_fact)

    # Bullet format per spec §7.2 — must stay in sync with dedupe.py regex.
    bullet = (
        f"- {safe_fact} "
        f"<!-- id:{candidate.id} "
        f"captured:{candidate.provenance.captured_at.date().isoformat()} -->"
    )

    if not observeie_md.exists():
        # First-ever write — create the file with top-level header + section.
        new_content = (
            f"# ObserveIE\n\n## {candidate.proposed_section}\n\n{bullet}\n"
        )
        observeie_md.write_text(new_content, encoding="utf-8")
        # C2 fix: spec §5.5 step 6 — log BEFORE return so every code path logs.
        _log_merge(candidate, observeie_md)
        return

    content = observeie_md.read_text(encoding="utf-8")

    section_idx = _find_section_index(content, candidate.proposed_section)

    if section_idx is None:
        # Target section doesn't exist yet — append it at file end.
        # Ensure we start on a new line before the new ## heading.
        if not content.endswith("\n"):
            content += "\n"
        content += f"\n## {candidate.proposed_section}\n\n{bullet}\n"
    else:
        # Section exists — insert bullet at end of section block (just before
        # the next same-or-higher-level heading, or at EOF).
        # WHY section-end (not section-start): keeps bullets in chronological
        # order within a section, which makes `git diff` easier to read and
        # avoids constant churn at the top of busy sections.
        next_heading_idx = _find_next_heading_at_or_above(
            content, section_idx, level=2
        )
        insertion_point = next_heading_idx if next_heading_idx is not None else len(content)

        # Trim trailing whitespace from the section block, then insert bullet.
        # WHY: a blank line before the next heading is conventional markdown
        # style, and rstrip+newline ensures exactly one blank line buffer.
        before = content[:insertion_point].rstrip() + "\n"
        after = content[insertion_point:]

        # Keep a blank line between the last bullet and the next heading (if any).
        if after:
            content = f"{before}{bullet}\n\n{after}"
        else:
            content = f"{before}{bullet}\n"

    observeie_md.write_text(content, encoding="utf-8")

    # C2 fix: spec §5.5 step 6 — write audit log entry after every successful merge.
    _log_merge(candidate, observeie_md)


def _log_merge(candidate: Candidate, observeie_md: Path) -> None:
    """Append a structured MERGE record to the audit log.

    Log path: ~/.claude/logs/observe-learning-capture.log

    Tolerant of log-write failures — logs to stderr and continues rather
    than raising, because an audit-log failure must not block a successful
    merge. This is the only deliberate exception to the "belt-and-suspenders,
    never silent" rule: we log the failure to stderr so it is visible to the
    user, but we do not propagate it to the caller (merge already succeeded).

    Args:
        candidate: The Candidate that was just merged.
        observeie_md: Path to the ObserveIE.md that received the merge.
    """
    log_path = Path(os.path.expanduser("~/.claude/logs/observe-learning-capture.log"))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (
            f"{timestamp} [INFO] component=merge "
            f"action=MERGE id={candidate.id} "
            f"section={candidate.proposed_section!r} "
            f"session={candidate.provenance.session_id} "
            f"target={observeie_md}\n"
        )
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        # Log failure to stderr but do not raise — merge already succeeded.
        # WHY: a broken log path (permissions, full disk) must not undo the
        # merge that has already been written to ObserveIE.md.
        print(
            f"[observe-learning-capture] merge.py: failed to write audit log: {exc}",
            file=sys.stderr,
        )


def remove_from_pending(candidate_id: str, pending_file: Path) -> None:
    """Remove the candidate with matching id from the pending staging file.

    This is the ONLY operation in the pipeline that rewrites (rather than
    appends to) the pending file. It is only called after a successful merge
    so the promoted candidate is not re-surfaced for review.

    WHY re-import _render_yaml here (not at module top):
    stage.py imports from pipeline.types; merge.py imports from pipeline.stage.
    If stage.py were to import from pipeline.merge, that would be a circular
    import. The deferred import here is an intentional pattern that keeps the
    dependency direction one-way: merge → stage → types.

    Args:
        candidate_id: The `id` field of the candidate to remove.
        pending_file: Path to the `.pending.md` staging file.

    Raises:
        OSError: If the file cannot be written after rewrite. Logged to stderr
                 with full context before re-raising so the caller sees both
                 the exception and the human-readable message.
    """
    if not pending_file.exists():
        # Nothing to do — file may have been cleaned up already.
        return

    records = read_pending(pending_file)
    remaining = [r for r in records if r.get("id") != candidate_id]

    if len(remaining) == len(records):
        # Candidate not found in pending — no-op; avoid unnecessary rewrite.
        return

    # Re-import here to avoid circular import (see docstring WHY above).
    from pipeline.stage import _render_yaml  # noqa: PLC0415

    try:
        if not remaining:
            # All candidates were removed — write an empty file rather than
            # deleting it, so the path stays valid for future appends.
            pending_file.write_text("", encoding="utf-8")
            return

        # Reconstruct the YAML document stream from the surviving records.
        # Each record gets a `---` boundary prefix, matching the append format
        # in stage.py so that subsequent reads parse identically.
        chunks = []
        for record in remaining:
            chunks.append("---\n" + _render_yaml(record, indent=0))
        pending_file.write_text("\n".join(chunks) + "\n", encoding="utf-8")
    except OSError as exc:
        # I4 fix: log full context before re-raising so the caller can see
        # what file failed and why. Silent-swallow would leave a ghost entry
        # in the pending queue (the merged candidate would re-surface at next
        # review), which is a correctness bug, not just a logging gap.
        print(
            f"[observe-learning-capture] merge.py: cannot write pending file "
            f"{pending_file}: {exc}",
            file=sys.stderr,
        )
        raise


def _find_section_index(content: str, section_name: str) -> int | None:
    """Find the line-start index of `## {section_name}` (case-insensitive).

    Only matches exact level-2 headings — `### Sub` would not match a
    `## Section` query, and `# Top` would not either.

    Args:
        content: Full text of ObserveIE.md.
        section_name: The section heading text to locate (without `## `).

    Returns:
        Character index of the start of the `##` line, or None if not found.
    """
    pattern = re.compile(
        rf"^##\s+{re.escape(section_name)}\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    m = pattern.search(content)
    return m.start() if m else None


def _find_next_heading_at_or_above(content: str, after_idx: int, level: int) -> int | None:
    """Find the next heading at level 1–`level` that appears after `after_idx`.

    Used to locate where the current section ends — the bullet must be
    inserted before the next same-or-higher-level heading to stay inside
    the correct section block.

    Args:
        content: Full text of ObserveIE.md.
        after_idx: Character index of the current section heading line.
                   The search begins strictly AFTER this position (pos + 1).
        level: Maximum heading level to treat as a section boundary.
               For level=2, matches `#` and `##` (but not `###`).

    Returns:
        Character index of the next boundary heading, or None if none found
        (meaning the current section runs to EOF).
    """
    # Match `#` through `#{level}` only — deeper headings belong inside the section.
    pattern = re.compile(rf"^#{{1,{level}}}\s+", re.MULTILINE)
    for m in pattern.finditer(content, pos=after_idx + 1):
        return m.start()
    return None
