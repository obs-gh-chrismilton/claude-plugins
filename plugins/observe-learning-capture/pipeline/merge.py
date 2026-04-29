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

import re
from pathlib import Path

from pipeline.stage import read_pending
from pipeline.types import Candidate


# Matches any `#` heading at level 1-6 (used to detect next-section boundaries)
_SECTION_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


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

    # Bullet format per spec §7.2 — must stay in sync with dedupe.py regex.
    bullet = (
        f"- {candidate.fact.strip()} "
        f"<!-- id:{candidate.id} "
        f"captured:{candidate.provenance.captured_at.date().isoformat()} -->"
    )

    if not observeie_md.exists():
        # First-ever write — create the file with top-level header + section.
        new_content = (
            f"# ObserveIE\n\n## {candidate.proposed_section}\n\n{bullet}\n"
        )
        observeie_md.write_text(new_content, encoding="utf-8")
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
        OSError: If the file cannot be written after rewrite. Not swallowed.
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
