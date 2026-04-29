"""Dedup logic for the observe-learning-capture pipeline.

Two phases:
1. Hash-based exact dedup against ObserveIE.md — parses HTML comments
   emitted at merge time: ``<!-- id:XXXXXXXX captured:YYYY-MM-DD -->``
   (merge.py, T06, is responsible for writing those comments).
2. Tag-overlap near-duplicate warning — a review-time hint, not a hard
   block. Fires when the new candidate's tag set overlaps >50% of the
   smaller of the two tag sets being compared.

Why content-hash instead of fuzzy text match?
  The id is the 8-char SHA-256 prefix of the normalized fact text (see
  types._hash_fact). Using the id directly is O(1) dict lookup and is
  immune to minor whitespace/punctuation variants, because the same
  normalization was applied at creation time.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional, Set

from pipeline.types import Candidate


# ---------------------------------------------------------------------------
# Regex for the HTML comment format written by merge.py at promote time.
# Format: <!-- id:XXXXXXXX captured:YYYY-MM-DD -->
# The id group captures exactly 8 lowercase hex characters per spec §7.2.
# We're deliberately lenient with whitespace around the fields.
# ---------------------------------------------------------------------------
_ID_COMMENT_RE = re.compile(
    r"<!--\s*id:([0-9a-f]{8})\s+captured:[^\s>]+\s*-->"
)


def extract_existing_ids(observeie_md_path: Path) -> Set[str]:
    """Read ObserveIE.md and return the set of all candidate ids already
    merged into it, extracted from the HTML id comments.

    The dedup pipeline calls this once per session and caches the result.
    Returns an empty set (not an error) when the file is missing — the
    classifier still runs; we just have no prior knowledge to dedup against.

    Args:
        observeie_md_path: Absolute or relative Path to ObserveIE.md.

    Returns:
        Set of 8-char hex id strings found in the file's id comments.
        Empty set if the file does not exist or cannot be read.
    """
    if not observeie_md_path.exists():
        # Graceful degradation per spec §5.3: missing file is not an error.
        # Log-worthy for debugging, but not worth raising here.
        return set()
    try:
        content = observeie_md_path.read_text(encoding="utf-8")
    except OSError:
        # Permission denied, symlink loop, etc. — still a soft failure;
        # return empty rather than crashing the whole pipeline run.
        return set()
    # findall returns the captured group (the id) for each match.
    return set(_ID_COMMENT_RE.findall(content))


def is_duplicate(candidate: Candidate, existing_ids: Set[str]) -> bool:
    """Return True if this candidate's content hash is already present in
    ObserveIE.md (i.e. the fact has already been merged into the knowledge
    base).

    The check is intentionally exact: same normalized fact → same id →
    duplicate. This rejects re-submission of the identical learning without
    blocking near-variants (those are surfaced by near_duplicate_warning).

    Args:
        candidate: The newly-captured Candidate to check.
        existing_ids: The set returned by extract_existing_ids().

    Returns:
        True if candidate.id is in existing_ids, False otherwise.
    """
    return candidate.id in existing_ids


def near_duplicate_warning(
    candidate: Candidate,
    other_candidates: Iterable[Candidate],
) -> Optional[str]:
    """Return a human-readable warning if this candidate's tags overlap
    significantly with any other candidate in the current batch.

    "Significant" is defined as: the intersection of the two tag sets
    is strictly greater than 50% of the smaller tag set. This catches
    topical duplicates (same concept, slightly different wording) without
    triggering on broad shared tags like "opal" appearing across dozens
    of unrelated facts.

    The warning is stored in Candidate.dupe_warning and surfaced during
    human review — it is a hint, not a block. The reviewer can dismiss or
    consolidate as appropriate.

    Args:
        candidate: The candidate being tested for near-duplicate status.
        other_candidates: Iterable of existing candidates to compare against.
            Typically the other entries already in the current staging batch.

    Returns:
        A human-readable warning string if a near-duplicate is found, or
        None if the candidate is sufficiently distinct from all others.
    """
    # No tags → can't compute meaningful overlap; skip.
    if not candidate.tags:
        return None

    new_tag_set = {t.lower() for t in candidate.tags}

    for other in other_candidates:
        # Don't compare a candidate against itself (same id).
        if other.id == candidate.id:
            continue
        other_tags = {t.lower() for t in other.tags}
        if not other_tags:
            continue

        overlap = new_tag_set & other_tags
        # Threshold: >50% of the SMALLER of the two tag sets.
        # WHY: using the smaller set prevents a large tag set from always
        # triggering warnings against narrow-tagged entries. The "smaller
        # set" denominator keeps the comparison symmetric in spirit.
        smaller = min(len(new_tag_set), len(other_tags))
        if smaller > 0 and len(overlap) / smaller > 0.5:
            shared = ", ".join(sorted(overlap))
            return (
                f"Tags overlap with candidate {other.id} "
                f"(shared: {shared}). Consider merging or distinguishing."
            )

    return None
