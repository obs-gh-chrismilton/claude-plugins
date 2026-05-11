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

    Scope filter (added 2026-05-11): only writes an audit entry when the
    merge target is inside the canonical production directory
    ($HOME/.claude/agents/). Test-fixture merges that target a temp
    directory (pytest's TemporaryDirectory) are skipped silently so they
    don't pollute the production log. Symptom this fixes: a single
    `unittest discover` run was writing 9+ MERGE audit lines per test
    invocation, indistinguishable from real production merges in the
    log, breaking any tail/grep-based production monitoring.

    Tolerant of log-write failures — logs to stderr and continues rather
    than raising, because an audit-log failure must not block a successful
    merge. This is the only deliberate exception to the "belt-and-suspenders,
    never silent" rule: we log the failure to stderr so it is visible to the
    user, but we do not propagate it to the caller (merge already succeeded).

    Args:
        candidate: The Candidate that was just merged.
        observeie_md: Path to the ObserveIE.md that received the merge.
    """
    # Scope check: only canonical production merges get audited.
    # The canonical dir is $HOME/.claude/agents/; anything else (typically
    # a tempfile.TemporaryDirectory() target from a test) is silently
    # skipped so the production log stays uncluttered.
    canonical_agents_dir = Path(os.path.expanduser("~/.claude/agents")).resolve()
    try:
        target_resolved = observeie_md.resolve()
    except OSError:
        # Path resolution failed (broken symlink, missing parents). Treat
        # as non-canonical to be safe — better to under-log than to write
        # noise when the target is malformed.
        return
    # is_relative_to was added in Python 3.9. We accept that minimum.
    if not target_resolved.is_relative_to(canonical_agents_dir):
        return

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
    appends to) the pending file. Called after a successful merge so the
    promoted candidate is not re-surfaced for review, and from the
    --discard path of merge_cli.

    Implementation note: text-surgery, not parse-and-reserialize.
    ============================================================
    Previous versions read the file via read_pending() (YAML parser) and
    rewrote the surviving records by re-rendering them with _render_yaml.
    That approach has two failure modes:

      1. Records the parser couldn't interpret (malformed YAML, unrecognized
         shapes) were silently dropped on rewrite. The parser already logs
         "skipped malformed YAML record" to stderr; the rewrite then made
         that drop permanent on disk -- data loss.
      2. Even valid records had their formatting normalized: inline lists
         (`tags: [a, b]`) became block lists (`tags:\\n  - a\\n  - b`),
         quote styles could change, indentation subtly differed. Not data
         loss but a `git diff` headache for users version-controlling
         `~/.claude/`.

    The fix is to do text-level surgery: split the file on the same
    document-boundary regex `_parse_yaml_list` uses (line-anchored `^---$`),
    detect which chunks contain a top-level `id: <target>` line, and rebuild
    the file from the chunks that DON'T match. Bytes of surviving chunks
    are preserved verbatim; malformed records pass through untouched.

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

    try:
        content = pending_file.read_text(encoding="utf-8")
    except OSError as exc:
        # Log and propagate — caller must know about read failures because
        # otherwise a subsequent merge would be operating on stale state.
        print(
            f"[observe-learning-capture] merge.py: cannot read pending file "
            f"{pending_file}: {exc}",
            file=sys.stderr,
        )
        raise

    # Walk the file line-by-line, grouping lines into "chunks" separated by
    # `---` boundary lines. A chunk consists of: optional separator line +
    # body lines. The first chunk (before the first `---`) has no separator.
    #
    # We avoid `re.split` because we want to preserve the EXACT separator
    # text (e.g. `---` vs `---   ` with trailing whitespace) and the exact
    # line endings around it.
    boundary_re = re.compile(r"^---\s*$")
    id_line_re = re.compile(rf"^id:\s+{re.escape(candidate_id)}\s*$")

    chunks: list[tuple[str | None, list[str]]] = []
    current_sep: str | None = None
    current_body: list[str] = []
    for line in content.split("\n"):
        if boundary_re.match(line):
            # Finalize the previous chunk before opening the new one.
            if current_sep is not None or current_body:
                chunks.append((current_sep, current_body))
            current_sep = line
            current_body = []
        else:
            current_body.append(line)
    # Don't forget the trailing chunk after the last `---`.
    if current_sep is not None or current_body:
        chunks.append((current_sep, current_body))

    # Filter: keep chunks whose body does NOT contain a top-level
    # `id: <candidate_id>` line. Top-level means line-start, no indent --
    # this matches the renderer's invariant that the canonical `id` lives
    # at indent 0 in the YAML mapping. Indented mentions (e.g. an `excerpt`
    # field that quotes another record's id) won't match by design.
    surviving: list[tuple[str | None, list[str]]] = []
    removed_count = 0
    for sep, body in chunks:
        has_target = any(id_line_re.match(line) for line in body)
        if has_target:
            removed_count += 1
            continue
        surviving.append((sep, body))

    if removed_count == 0:
        # No chunk matched — no-op. Skip rewrite entirely so file mtime and
        # any in-flight readers are undisturbed.
        return

    # Rebuild output: separator (if any) followed by body lines, then join
    # everything with `\n`. This preserves each surviving chunk's bytes
    # exactly (body line content + ordering), only altering chunks we
    # explicitly removed.
    out_lines: list[str] = []
    for sep, body in surviving:
        if sep is not None:
            out_lines.append(sep)
        out_lines.extend(body)
    new_content = "\n".join(out_lines)

    # If the original file ended with a newline and our rebuild lost it
    # (because `body` had no trailing empty element), restore it. This
    # keeps `wc -l` and `git diff` consistent across the round-trip.
    if content.endswith("\n") and not new_content.endswith("\n"):
        new_content += "\n"

    try:
        pending_file.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        # I4 fix preserved: log full context before re-raising so the caller
        # sees both the exception and the human-readable message. Silent-
        # swallow would leave a ghost entry that re-surfaces at /observe-review.
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
