"""Dataclasses for the observe-learning-capture pipeline.

Canonical schema lives in docs/design.md §7.1. These types are the
in-memory representation; YAML serialization happens at the boundary
(stage.py for write, dedupe.py for read).

Design constraints:
- Candidate must be created via Candidate.create(...), never Candidate(...)
  directly, so that the `id` field is always derived from the fact content.
- The id is an 8-char hex prefix of the SHA-256 of the normalized fact,
  enabling deduplication against existing ObserveIE.md content.
- Confidence is restricted to {"high", "medium", "low"} per spec §5.2.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional


# Valid confidence levels per spec §5.2. Using a frozenset for O(1) lookup.
_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})


@dataclass
class Provenance:
    """Where and when a candidate was captured.

    Source: spec §7.1 source.* fields. Stored verbatim in the YAML
    staging file so reviewers can trace back to the originating session.

    Attributes:
        session_id: Claude Code session identifier (injected by SessionStart hook).
        cwd: Working directory at capture time (identifies the customer project).
        captured_at: UTC timestamp of capture. Always timezone-aware.
        excerpt: Short text excerpt from the conversation that triggered capture.
    """

    session_id: str
    cwd: str
    captured_at: datetime
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize for YAML output.

        datetime → ISO 8601 string (e.g. "2026-04-29T11:33:00+00:00").
        All other fields are plain strings.

        Returns:
            Dict suitable for yaml.dump().
        """
        return {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "captured_at": self.captured_at.isoformat(),
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Provenance:
        """Deserialize from YAML input.

        ISO 8601 string → timezone-aware datetime via fromisoformat().
        Python 3.11+ handles the "+00:00" suffix natively.

        Args:
            d: Dict as produced by to_dict() or loaded from YAML.

        Returns:
            Provenance instance with restored datetime.
        """
        return cls(
            session_id=d["session_id"],
            cwd=d["cwd"],
            captured_at=datetime.fromisoformat(d["captured_at"]),
            excerpt=d["excerpt"],
        )


@dataclass
class ClassifierMeta:
    """Metadata about which model/prompt produced a candidate.

    Source: spec §7.1 classifier.* fields. Stored alongside each candidate
    so we can retroactively re-evaluate if prompts change.

    Attributes:
        model: Model ID string (e.g. "claude-haiku-4-5-20251001").
        prompt_version: Version of the classifier prompt template used.
        confidence_score: Optional float [0.0–1.0] from the model's assessment.
    """

    model: str
    prompt_version: str
    confidence_score: Optional[float] = None

    def __post_init__(self) -> None:
        """Validate confidence_score is within [0.0, 1.0] when provided.

        Raises:
            ValueError: If confidence_score is outside the valid range.
        """
        # Guard against out-of-range floats that would slip through
        # the type system — the model may return scores like 1.05.
        if self.confidence_score is not None and not (0.0 <= self.confidence_score <= 1.0):
            raise ValueError(
                f"confidence_score must be in [0.0, 1.0], got {self.confidence_score}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the YAML classifier sub-object per spec §7.1.

        confidence_score is omitted when None to keep YAML compact.

        Returns:
            Dict with at least 'model' and 'prompt_version' keys.
        """
        d: dict[str, Any] = {
            "model": self.model,
            "prompt_version": self.prompt_version,
        }
        # Only include confidence_score if it was populated — avoids
        # null entries in YAML that would confuse downstream readers.
        if self.confidence_score is not None:
            d["confidence_score"] = self.confidence_score
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClassifierMeta":
        """Deserialize from a YAML classifier sub-object.

        Symmetric counterpart to to_dict(). confidence_score is optional
        in the dict — matches to_dict()'s omit-when-None behavior.

        Args:
            d: Dict as produced by to_dict() or loaded from YAML.

        Returns:
            ClassifierMeta instance with __post_init__ validation applied.
        """
        return cls(
            model=d["model"],
            prompt_version=d["prompt_version"],
            confidence_score=d.get("confidence_score"),
        )


@dataclass
class Candidate:
    """A single learning candidate awaiting human review.

    Use `Candidate.create(...)` — never call `Candidate(...)` directly.
    The `id` field is derived from the normalized fact text, ensuring
    stable deduplication across sessions (spec §5.3).

    Attributes:
        id: 8-char hex content hash of the normalized fact (see _hash_fact).
        title: Short human-readable label for the learning.
        fact: The actual learning text. This is what gets hashed for dedup.
        proposed_section: Target section in ObserveIE.md where this belongs.
        confidence: One of "high", "medium", "low" per spec §5.2.
        tags: Free-form topic tags for filtering/grouping.
        provenance: Capture context (session, cwd, timestamp, excerpt).
        classifier: Optional metadata about the model that produced this.
        dupe_warning: Set by dedupe.py when a near-match exists in ObserveIE.md.
        last_seen_at: Updated by dedupe.py when the same fact recurs.
    """

    id: str
    title: str
    fact: str
    proposed_section: str
    confidence: str
    tags: List[str]
    provenance: Provenance
    classifier: Optional[ClassifierMeta] = None
    dupe_warning: Optional[str] = None
    last_seen_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        """Validate confidence on every construction path.

        This guard runs whether the Candidate was built via create() or
        directly via cls(...) (e.g. from_yaml_record). Defense-in-depth:
        create() still pre-validates, but __post_init__ is the structural
        contract that can't be bypassed.

        Raises:
            ValueError: If confidence is not in the allowed set.
        """
        if self.confidence not in _VALID_CONFIDENCE:
            raise ValueError(
                f"confidence must be one of {sorted(_VALID_CONFIDENCE)}, "
                f"got {self.confidence!r}"
            )

    @classmethod
    def create(
        cls,
        title: str,
        fact: str,
        proposed_section: str,
        confidence: str,
        tags: List[str],
        provenance: Provenance,
        classifier: Optional[ClassifierMeta] = None,
    ) -> "Candidate":
        """Construct a Candidate, computing id from the normalized fact.

        This is the canonical constructor. The id is stable across calls
        with the same fact content (modulo normalization), enabling the
        dedupe pipeline to identify re-occurrences.

        Args:
            title: Short label (does NOT affect id — intentional).
            fact: The learning text. Normalized before hashing.
            proposed_section: Target ObserveIE.md section.
            confidence: Must be "high", "medium", or "low".
            tags: Topic tags (order not significant for id).
            provenance: Capture context.
            classifier: Optional model metadata.

        Returns:
            Candidate with id set to _hash_fact(fact).

        Raises:
            ValueError: If confidence is not in the allowed set.
        """
        # Validate confidence before constructing — fail fast so callers
        # can't accidentally persist an invalid record to the staging file.
        if confidence not in _VALID_CONFIDENCE:
            raise ValueError(
                f"confidence must be one of {sorted(_VALID_CONFIDENCE)}, "
                f"got {confidence!r}"
            )
        cand_id = _hash_fact(fact)
        return cls(
            id=cand_id,
            title=title,
            fact=fact,
            proposed_section=proposed_section,
            confidence=confidence,
            tags=[t.lower() for t in tags],  # normalize + defensive copy — canonical lowercase
            provenance=provenance,
            classifier=classifier,
        )

    def to_yaml_record(self) -> dict[str, Any]:
        """Serialize to the YAML record shape in spec §7.1.

        The `classifier` block is omitted when None — this happens for
        programmatically-injected candidates that bypass the Haiku
        classifier (e.g., marker candidates from error handling in §9).
        Records without a `classifier` block are still valid per §7.1.

        Optional fields (dupe_warning, last_seen_at) are included only
        when populated to keep YAML minimal.

        Returns:
            Dict with all required §7.1 fields. Ready for yaml.dump().
        """
        record: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "fact": self.fact,
            "proposed_section": self.proposed_section,
            "confidence": self.confidence,
            "tags": list(self.tags),       # copy to prevent mutation surprises
            "source": self.provenance.to_dict(),
        }
        # Include classifier block only when present — most candidates in
        # early pipeline stages won't have been classified yet.
        if self.classifier is not None:
            record["classifier"] = self.classifier.to_dict()
        # Dedupe annotations — set by dedupe.py, not present at creation time.
        if self.dupe_warning is not None:
            record["dupe_warning"] = self.dupe_warning
        if self.last_seen_at is not None:
            record["last_seen_at"] = self.last_seen_at.isoformat()
        return record

    @classmethod
    def from_yaml_record(cls, d: dict[str, Any]) -> "Candidate":
        """Reconstruct a Candidate from a YAML record (e.g., from the
        staging file). Bypasses `create()` because id is already known
        and confidence was validated at write time. __post_init__
        (added in C2) still validates structurally.

        Args:
            d: Dict as produced by to_yaml_record() or loaded from YAML.

        Returns:
            Candidate with all fields restored, including optional ones.

        Raises:
            ValueError: If the record carries an invalid confidence value.
        """
        # Reconstruct nested objects from their sub-dicts.
        provenance = Provenance.from_dict(d["source"])

        # classifier block is optional per spec §7.1 — absent for marker candidates.
        classifier: Optional[ClassifierMeta] = None
        if "classifier" in d:
            classifier = ClassifierMeta.from_dict(d["classifier"])

        # last_seen_at may be absent on first staging or omitted when None.
        last_seen_at: Optional[datetime] = None
        if d.get("last_seen_at") is not None:
            last_seen_at = datetime.fromisoformat(d["last_seen_at"])

        # Call cls(...) directly — id is already in the record and was
        # derived deterministically at write time; recomputing it would
        # fail if the fact text was ever normalized differently.
        return cls(
            id=d["id"],
            title=d["title"],
            fact=d["fact"],
            proposed_section=d["proposed_section"],
            confidence=d["confidence"],
            tags=[t.lower() for t in d.get("tags", [])],  # normalize + defensive copy — canonical lowercase
            provenance=provenance,
            classifier=classifier,
            dupe_warning=d.get("dupe_warning"),
            last_seen_at=last_seen_at,
        )


def _hash_fact(fact: str) -> str:
    """Normalize the fact text and return an 8-char hex content hash.

    Normalization steps (must match dedupe.py — keep in sync, spec §5.3):
      1. Strip leading/trailing whitespace.
      2. Lowercase the entire string.
      3. Collapse internal whitespace runs to a single space.
      4. Strip trailing sentence-ending punctuation (. ! ?).

    The result is the first 8 hex characters of the SHA-256 digest.
    8 chars = 32-bit address space; collision probability negligible for
    the expected corpus size (<10K entries lifetime).

    Args:
        fact: Raw fact text, as provided by the classifier or user.

    Returns:
        8-character lowercase hex string.
    """
    # Step 1 + 2: strip and lowercase
    normalized = fact.lower().strip()
    # Step 3: collapse whitespace
    normalized = re.sub(r"\s+", " ", normalized)
    # Step 4: strip trailing punctuation so "X." and "X" hash identically.
    # WHY: LLM output sometimes appends a trailing period; we don't want
    # that to create a spurious new id.
    normalized = re.sub(r"[.!?]+$", "", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
