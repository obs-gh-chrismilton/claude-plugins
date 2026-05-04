"""Haiku-based classifier for learning candidates.

Invokes the `claude` CLI as a subprocess (no API key handling here — the
CLI manages auth). On any failure, emits a "marker candidate" so the
human sees the failure at next review (per spec §9 — log AND surface).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

import anthropic

from pipeline.stage import _parse_yaml_list
from pipeline.types import Candidate, ClassifierMeta, Provenance


def _sanitize(reason: object) -> str:
    """Return a YAML-safe ≤200-char string suitable for marker fact/excerpt fields.

    Bug 3 fix: subprocess.TimeoutExpired.__str__() embeds the full argv
    including the rendered prompt (often 30+ KB). Without sanitation, that
    blob landed in marker YAML records and bloated the pending queue past
    100 KB per failure. We cap at 200 chars and collapse newlines so the
    YAML file stays bounded and human-readable.
    """
    s = str(reason) if not isinstance(reason, str) else reason
    # repr() escapes control chars and embedded quotes, then strip the
    # outer quotes that repr adds, cap, and collapse remaining newline escapes.
    escaped = repr(s).strip("'\"")[:200].replace("\\n", " ").replace("\n", " ")
    return escaped


@dataclass
class Classifier:
    """Orchestrates Anthropic SDK invocations to produce Candidate objects.

    Attributes:
        model: Claude model ID (e.g. "claude-sonnet-4-5").
        prompt_template_path: Path to the static classifier prompt template.
        observeie_md_path: Path to ObserveIE.md — slim known-facts derived
            from this so Haiku/Sonnet avoids re-capturing known facts.
        prompt_version: Version label embedded in ClassifierMeta for each
            produced candidate, so prompts can be retro-evaluated later.
        pending_path: Where marker candidates are appended on failure.
            Injected (NOT hardcoded) so tests use a tmp path and never
            pollute the user's real ~/.claude/agents/.observeie-pending.md.
        _cache_call_count: Counter of consecutive calls observed with
            cache_read_input_tokens == 0. Reset on first cache hit.
        _cache_sentinel_path: One-shot marker file. When present, the
            cache-disabled warning has already been emitted and we skip
            re-emission. Deleted on first observed cache hit so the
            warning re-fires if the situation regresses.
    """

    model: str
    prompt_template_path: Path
    observeie_md_path: Path
    prompt_version: str = "1.0"
    pending_path: Path = field(default_factory=lambda: Path(
        os.path.expanduser("~/.claude/agents/.observeie-pending.md")
    ))
    _cache_call_count: int = 0
    _cache_sentinel_path: Path = field(default_factory=lambda: Path(
        os.path.expanduser("~/.claude/agents/.observe-cache-warned")
    ))

    def classify(
        self,
        turn_text: str,
        session_id: str,
        cwd: str,
        excerpt: Optional[str] = None,
    ) -> List[Candidate]:
        """Run classifier on turn_text. Returns 0+ candidates.

        Bug 2 fix: SDK-based, layered cacheable prompt.
        Bug 5 fix: per-record errors emit markers, not silent skips.

        On any classifier failure, emit a marker (per spec §9). Errors
        logged to stderr before marker emission for visibility.
        """
        captured_at = datetime.now(timezone.utc)
        excerpt = excerpt or turn_text[:200]

        try:
            slim_known_facts = _generate_slim_known_facts(self.observeie_md_path)
            static_template, slim_block, user_message = _build_prompt(
                template_path=self.prompt_template_path,
                turn_text=turn_text,
                slim_known_facts=slim_known_facts,
                cwd=cwd,
                captured_at=captured_at,
            )
            classifier_output, usage = _invoke_classifier(
                static_template=static_template,
                slim_known_facts=slim_block,
                user_message=user_message,
                model=self.model,
            )
            # Cache visibility check — surface a marker if cache silently no-ops.
            self._maybe_emit_cache_warning(usage, session_id, cwd, captured_at)
        except anthropic.AuthenticationError as exc:
            print(
                f"[observe-learning-capture] classifier.py: API key rejected "
                f"for session={session_id}: {exc}",
                file=sys.stderr,
            )
            return [build_marker_candidate(
                failure_reason=f"key rejected: {getattr(exc, 'status_code', '?')} from API",
                session_id=session_id, cwd=cwd, captured_at=captured_at,
            )]
        except anthropic.APIError as exc:
            print(
                f"[observe-learning-capture] classifier.py: SDK error "
                f"for session={session_id}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return [build_marker_candidate(
                failure_reason=f"{type(exc).__name__}: {exc}",
                session_id=session_id, cwd=cwd, captured_at=captured_at,
            )]
        except (RuntimeError, OSError) as exc:
            # Legacy compatibility — file read errors etc.
            print(
                f"[observe-learning-capture] classifier.py: classifier failed "
                f"for session={session_id}: {exc}",
                file=sys.stderr,
            )
            return [build_marker_candidate(
                failure_reason=str(exc),
                session_id=session_id, cwd=cwd, captured_at=captured_at,
            )]

        raw_candidates = parse_haiku_yaml_output(classifier_output)
        if not raw_candidates and not _is_empty_haiku_response(classifier_output):
            print(
                f"[observe-learning-capture] classifier.py: malformed yaml "
                f"from classifier for session={session_id}",
                file=sys.stderr,
            )
            return [build_marker_candidate(
                failure_reason=f"malformed yaml: {classifier_output[:200]}",
                session_id=session_id, cwd=cwd, captured_at=captured_at,
            )]

        result: List[Candidate] = []
        for raw in raw_candidates:
            try:
                result.append(_raw_to_candidate(
                    raw, session_id=session_id, cwd=cwd,
                    captured_at=captured_at, excerpt=excerpt,
                    model=self.model, prompt_version=self.prompt_version,
                ))
            except (KeyError, ValueError) as e:
                # Bug 5 fix: emit a marker per malformed record so failures
                # surface at /observe-review time. Previous behavior silently
                # dropped the record (logged to stderr only). One bad record
                # still doesn't block the rest of the batch.
                print(
                    f"[observe-learning-capture] classifier.py: malformed "
                    f"candidate record: {e}",
                    file=sys.stderr,
                )
                result.append(build_marker_candidate(
                    failure_reason=f"malformed candidate record: missing field {e}",
                    session_id=session_id, cwd=cwd,
                    captured_at=captured_at,
                ))
                continue
        return result

    def _maybe_emit_cache_warning(
        self,
        usage,
        session_id: str,
        cwd: str,
        captured_at: datetime,
    ) -> None:
        """Surface a marker if prompt cache silently no-ops.

        Per silent-failure-hunter review: if our prompt is below the model's
        cache minimum (1024 tokens for Sonnet 4.5), cache_control markers
        silently no-op — cache_creation_input_tokens=0, no error raised.
        Classifier "succeeds" but pays full input cost forever with no signal.

        Strategy: after 5 calls with cache_read_input_tokens consistently 0,
        emit a one-shot marker via sentinel file. Self-healing: sentinel is
        deleted on first observed cache_read>0 so the warning re-fires if
        the situation regresses.
        """
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

        if cache_read > 0:
            # Caching IS working — heal any prior warning sentinel
            if self._cache_sentinel_path.exists():
                try:
                    self._cache_sentinel_path.unlink()
                except OSError:
                    pass  # best-effort heal
            return

        # Cache not hitting on this call
        self._cache_call_count += 1
        if self._cache_call_count < 5:
            return  # not enough evidence yet

        if self._cache_sentinel_path.exists():
            return  # already warned; don't spam

        # Emit one-shot marker — uses injected pending_path, NOT hardcoded
        # (validator caught the hardcoded path was polluting tests' real
        # pending file and was an architectural violation).
        from pipeline.stage import append_candidates
        pending_path = self.pending_path
        marker = build_marker_candidate(
            failure_reason=(
                f"cache disabled: prefix below threshold "
                f"({self._cache_call_count} calls × 0 cache reads)"
            ),
            session_id=session_id, cwd=cwd, captured_at=captured_at,
        )
        try:
            append_candidates(pending_path, [marker])
            self._cache_sentinel_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_sentinel_path.touch()
        except Exception as exc:
            print(
                f"[observe-learning-capture] classifier.py: cache-warning "
                f"emission failed: {exc}",
                file=sys.stderr,
            )


def parse_haiku_yaml_output(output: str) -> List[dict[str, Any]]:
    """Parse Haiku's YAML response. Returns [] on empty list or malformed.

    Strips markdown fences (```yaml ... ```) if Haiku wraps its response,
    then splits the top-level YAML list into per-item documents and parses
    each via stage._parse_yaml_list.

    Args:
        output: Raw string from Haiku (stdout of the `claude` CLI call).

    Returns:
        List of dicts, one per candidate. Empty list on empty/malformed input.
    """
    output = output.strip()
    # Q8 fix: Haiku may respond with "null" or "~" to indicate no learnings found.
    # Treat these as "no candidates" rather than letting them fall through to the
    # YAML parser which would return an empty list with a spurious malformed-yaml
    # warning in the classifier (because "null" parses to None, not a list).
    if not output or output in ("[]", "null", "~"):
        return []
    # Strip markdown fences if Haiku wraps in ```yaml ... ```
    # WHY: the classifier prompt tells Haiku it MAY wrap; we normalize both forms.
    output = re.sub(r"^```(?:yaml)?\s*\n", "", output, flags=re.MULTILINE)
    output = re.sub(r"\n```\s*$", "", output, flags=re.MULTILINE)
    output = output.strip()
    if not output or output == "[]":
        return []
    # Split the top-level YAML list (`- item\n- item`) into per-item chunks,
    # then pass each through _parse_yaml_list wrapped in `---` doc delimiters.
    # Reuses stage._parse_yaml_list for the heavy lifting.
    docs = _split_haiku_list(output)
    if not docs:
        return []
    return _parse_yaml_list("---\n" + "\n---\n".join(docs))


def _split_haiku_list(yaml_list: str) -> List[str]:
    """Split a top-level YAML list (`- item\\n- item`) into per-item chunks.

    Each chunk is a mini-YAML-document for the item's fields, with the
    leading `- ` stripped and continuation lines de-indented by 2 spaces.

    Args:
        yaml_list: YAML text containing a top-level list (lines start with `- `).

    Returns:
        List of per-item YAML text blocks, ready to be rejoined with `---`.
    """
    items: List[str] = []
    current: List[str] = []
    for line in yaml_list.splitlines():
        if line.startswith("- "):
            # New top-level list entry — flush previous item if any.
            if current:
                items.append("\n".join(current))
            # Strip the leading `- ` and start a new item block.
            current = [line[2:]]
        elif current and (line.startswith("  ") or not line.strip()):
            # Continuation of current item — strip 2 spaces of list indentation.
            current.append(line[2:] if line.startswith("  ") else line)
        elif current:
            # Non-indented line that doesn't start a new entry — append as-is.
            current.append(line)
    if current:
        items.append("\n".join(current))
    return items


def _is_empty_haiku_response(raw_output: str) -> bool:
    """Return True if the raw Haiku response represents "no candidates found".

    Haiku may signal an empty result in several equivalent forms:
      - bare empty string
      - "[]" (bare YAML empty list)
      - "null" or "~" (YAML null)
      - "```yaml\\n[]\\n```" (fenced empty list — the common Haiku wrapping)
      - "```yaml\\n[]\\n```\\n\\nExplanatory prose..." (fenced empty list
        followed by an explanation — observed in T17 real-Haiku runs when
        Haiku knows the fact is already captured and explains why)

    WHY needed: the malformed-yaml guard in classify() compares against the
    RAW output string before fence stripping. Without this helper, a fenced
    "[]" followed by explanation prose triggers a spurious malformed-yaml
    marker because the raw string is neither empty nor literally "[]".

    Strategy: extract just the YAML content block (between/without fences)
    and check if its stripped first token is an empty-list signal. Any
    trailing prose after the closing fence is explanatory and is ignored.

    Args:
        raw_output: The raw stdout string from the `claude` CLI.

    Returns:
        True if the response is an empty-candidates signal; False otherwise.
    """
    stripped = raw_output.strip()
    if not stripped or stripped in ("[]", "null", "~"):
        return True

    # Extract just the fenced YAML block content (first code block, if any).
    # WHY first block: Haiku sometimes appends prose after the closing fence.
    # We only care about the YAML block — prose after it is not a parse error.
    fence_match = re.search(
        r"^```(?:yaml)?\s*\n(.*?)\n```",
        stripped,
        flags=re.DOTALL | re.MULTILINE,
    )
    if fence_match:
        yaml_content = fence_match.group(1).strip()
    else:
        # No fences — the entire output is the YAML content.
        yaml_content = stripped

    return not yaml_content or yaml_content in ("[]", "null", "~")


def _build_prompt(
    template_path: Path,
    turn_text: str,
    slim_known_facts: str,
    cwd: str,
    captured_at: datetime,
) -> tuple[str, str, str]:
    """Render the classifier prompt as a 3-tuple for layered cache structure.

    Returns:
        (static_template, slim_known_facts, user_message)

    The static template (cached system block 1) contains pure instruction
    content with no per-call placeholders. slim_known_facts (cached system
    block 2) is the bounded section-headers + id-list summary of
    ObserveIE.md. user_message wraps the per-call turn / cwd / timestamp.

    Bug 2 fix: previous version inlined ALREADY_KNOWN (30 KB ObserveIE.md)
    plus per-call values into one rendered string, which both bloated the
    prompt and (with the SDK rewrite) would have invalidated cache on
    every call due to per-call placeholders.
    """
    template = template_path.read_text(encoding="utf-8")
    user_message = (
        f"<turn>\n{turn_text}\n</turn>\n"
        f"<cwd>{cwd}</cwd>\n"
        f"<context_timestamp>{captured_at.isoformat()}</context_timestamp>"
    )
    return template, slim_known_facts, user_message


# Tolerant regex for ObserveIE.md dedup-key id markers.
# Real ObserveIE.md format is: `<!-- id:c4f9d2a1 captured:2026-05-01 ... -->`
# i.e. no space after `id:`, the 8-char hash is followed by ` captured:...`
# rather than `-->`. The regex accepts:
#   <!-- id:abcd1234 -->
#   <!-- id: abcd1234 -->
#   <!-- id:abcd1234 captured:2026-05-01 -->
#   <!--id:abcd1234-->
# Verified against /Users/chmilton/.claude/agents/ObserveIE.md format
# during validator review (executor agent confirmed real format).
_OBSERVEIE_ID_RE = re.compile(r"<!--\s*id:\s*([0-9a-f]{6,16})\b")


def _generate_slim_known_facts(observeie_md_path: Path) -> str:
    """Render a bounded slim summary of ObserveIE.md for the cached prompt block.

    Format:
        Section: <name>
          Known ids: id1, id2, id3
        Section: <name>
          Known ids: ...

    Bounded to id list (no body text) so the slim block stays sub-2KB
    regardless of ObserveIE.md growth. The deterministic post-classify
    dedupe in runner.py is the actual correctness gate; Haiku/Sonnet just
    needs section + id awareness to avoid obvious recapture attempts.

    On read failure, returns "(empty — ObserveIE.md unreadable)" so the
    classifier still runs (with no known-facts context) rather than
    crashing the pipeline.
    """
    if not observeie_md_path.exists():
        return "(empty — ObserveIE.md does not exist yet)"
    try:
        text = observeie_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"[observe-learning-capture] classifier.py: cannot read "
            f"ObserveIE.md for slim known-facts: {exc}",
            file=sys.stderr,
        )
        return "(empty — ObserveIE.md unreadable)"

    # Walk the file, tracking current section header and collecting ids.
    sections: dict[str, list[str]] = {}
    current_section: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("## "):
            current_section = line[3:].strip()
            sections.setdefault(current_section, [])
        elif current_section is not None:
            m = _OBSERVEIE_ID_RE.search(line)
            if m:
                sections[current_section].append(m.group(1))

    if not sections:
        return "(empty — no sections found in ObserveIE.md)"

    parts = []
    for section, ids in sections.items():
        parts.append(f"Section: {section}")
        if ids:
            parts.append(f"  Known ids: {', '.join(ids)}")
        else:
            parts.append("  Known ids: (none)")
    return "\n".join(parts)


def _invoke_classifier(
    static_template: str,
    slim_known_facts: str,
    user_message: str,
    model: str,
) -> tuple[str, object]:
    """Call the Anthropic SDK with layered cacheable system blocks.

    Returns (text_output, usage) where text_output is the first text block
    of the response and usage is the response.usage object (for cache
    visibility in callers).

    Bug 2 fix: replaces subprocess.run(['claude','--print',prompt],...).
    Eliminates: recursive Claude-Code-from-inside-Claude-Code invocation,
    60s subprocess ceiling, full ObserveIE.md re-processed every call.

    max_retries=0 so we own the retry budget; SDK's default 2 retries
    with exponential backoff would otherwise compound with timeout=120
    to ~6 min worst-case wall time (hook subshell may be reaped first).

    cache_control: ephemeral on both system blocks. Sonnet 4.5's 1024-token
    cache minimum lets the slim payload (~1KB) actually cache, unlike
    Haiku 4.5's 4096-token min which would silently no-op.

    NOTE: max_retries is an Anthropic() constructor arg, NOT a
    messages.create() kwarg. Validator caught this during plan review —
    passing max_retries to create() raises TypeError on first call.
    """
    # max_retries=0 on the client (constructor) — NOT on messages.create()
    client = anthropic.Anthropic(max_retries=0)  # auto-loads ANTHROPIC_API_KEY
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": static_template,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": slim_known_facts,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": user_message}],
        timeout=120,
    )
    # Defensive content extraction — handles thinking blocks, multi-block responses
    text_output = next(
        (b.text for b in response.content if getattr(b, "type", None) == "text"),
        "",
    )
    return text_output, response.usage


def _invoke_haiku(prompt: str, model: str) -> str:
    """Call the `claude` CLI with --model and --print. Returns stdout.

    Uses subprocess (not the SDK) so the CLI handles auth — no API key
    management needed here. Timeout is 60 seconds to avoid blocking the
    SessionStop hook indefinitely.

    Args:
        prompt: Fully rendered prompt string to pass as the last argument.
        model: Claude model ID string (e.g. "claude-haiku-4-5-20251001").

    Returns:
        stdout text from the `claude` CLI.

    Raises:
        RuntimeError: If the subprocess exits with a non-zero return code.
        OSError: If the subprocess cannot be started (e.g., `claude` not found).
        subprocess.TimeoutExpired: If the call exceeds 60 seconds.
    """
    proc = subprocess.run(
        ["claude", "--model", model, "--print", prompt],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exit={proc.returncode} stderr={proc.stderr[:300]}"
        )
    return proc.stdout


def _read_safe(path: Path) -> str:
    """Read a file, returning empty string on OSError (with stderr log).

    WHY: ObserveIE.md may not exist on first run. We don't want that to
    abort classification — Haiku will just see "(empty)" for already_known,
    which is correct behavior. The error is still logged so file-permission
    issues aren't silently swallowed (spec §9).

    Args:
        path: File to read.

    Returns:
        File contents as string, or "" if the file cannot be read.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"[observe-learning-capture] classifier.py: cannot read {path}: {exc}",
            file=sys.stderr,
        )
        return ""


def _raw_to_candidate(
    raw: dict[str, Any], *,
    session_id: str, cwd: str,
    captured_at: datetime, excerpt: str,
    model: str, prompt_version: str,
) -> Candidate:
    """Convert a parsed Haiku output dict into a Candidate object.

    Args:
        raw: Dict from parse_haiku_yaml_output() representing one candidate.
        session_id: Claude Code session identifier for provenance.
        cwd: Working directory at capture time.
        captured_at: UTC timestamp of this classification run.
        excerpt: Short conversation excerpt for provenance.
        model: Model ID string embedded in ClassifierMeta.
        prompt_version: Prompt version string embedded in ClassifierMeta.

    Returns:
        Candidate with id derived from the fact text (via Candidate.create).

    Raises:
        KeyError: If required fields (title, fact, proposed_section, confidence) are absent.
        ValueError: If confidence value is not in the allowed set.
    """
    # confidence_score is optional in Haiku output — convert to float when present.
    score = raw.get("classifier_confidence_score")
    return Candidate.create(
        title=raw["title"],
        fact=raw["fact"].strip(),
        proposed_section=raw["proposed_section"],
        confidence=raw["confidence"],
        tags=list(raw.get("tags", [])),
        provenance=Provenance(
            session_id=session_id, cwd=cwd,
            captured_at=captured_at, excerpt=excerpt,
        ),
        classifier=ClassifierMeta(
            model=model, prompt_version=prompt_version,
            confidence_score=float(score) if score is not None else None,
        ),
    )


def build_marker_candidate(
    failure_reason: str, *,
    session_id: str, cwd: str, captured_at: datetime,
) -> Candidate:
    """Emit a sentinel candidate so failures surface at review time.

    Per spec §9: every handled error must be logged AND surfaced to the
    caller's contract. This marker ensures the human reviewer sees the
    failure at `/observe-review` time rather than having it silently vanish.

    Bug 3 fix: failure_reason is sanitized via _sanitize() before being
    embedded in fact/excerpt fields, capping length at 200 chars and
    stripping newlines. Without this, subprocess.TimeoutExpired.__str__()
    poisoned the pending YAML queue.

    Args:
        failure_reason: Human-readable description of what failed.
        session_id: Claude Code session identifier for provenance.
        cwd: Working directory at capture time.
        captured_at: UTC timestamp of the failure.

    Returns:
        Candidate with title "[FAILURE] classifier", section "Plugin Self-Errors",
        confidence "low", and tag "self-error".
    """
    safe_reason = _sanitize(failure_reason)
    return Candidate.create(
        title="[FAILURE] classifier",
        fact=f"Classifier failed: {safe_reason}",
        proposed_section="Plugin Self-Errors",
        confidence="low",
        tags=["self-error"],
        provenance=Provenance(
            session_id=session_id, cwd=cwd,
            captured_at=captured_at,
            # Excerpt includes the reason so reviewers don't need to check logs.
            excerpt=f"Auto-generated marker. Reason: {safe_reason}",
        ),
        classifier=None,
    )
