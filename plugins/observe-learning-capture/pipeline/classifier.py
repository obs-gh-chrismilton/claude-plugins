"""Haiku-based classifier for learning candidates.

Invokes the `claude` CLI as a subprocess (no API key handling here — the
CLI manages auth). On any failure, emits a "marker candidate" so the
human sees the failure at next review (per spec §9 — log AND surface).
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from pipeline.stage import _parse_yaml_list
from pipeline.types import Candidate, ClassifierMeta, Provenance


@dataclass
class Classifier:
    """Orchestrates Haiku invocations to produce Candidate objects.

    Attributes:
        model: Claude model ID string (e.g. "claude-haiku-4-5-20251001").
        prompt_template_path: Path to the classifier.md prompt template.
        observeie_md_path: Path to ObserveIE.md — already-known content
            injected into the prompt so Haiku avoids re-capturing known facts.
        prompt_version: Version label embedded in ClassifierMeta for each
            produced candidate, so prompts can be retro-evaluated later.
    """

    model: str
    prompt_template_path: Path
    observeie_md_path: Path
    prompt_version: str = "1.0"

    def classify(
        self,
        turn_text: str,
        session_id: str,
        cwd: str,
        excerpt: Optional[str] = None,
    ) -> List[Candidate]:
        """Run Haiku on turn_text. Returns 0+ candidates.

        On Haiku failure, returns a single marker candidate (per spec §9).
        Errors logged to stderr before marker emission for visibility.

        Args:
            turn_text: Full conversation turn text to analyze.
            session_id: Claude Code session identifier for provenance.
            cwd: Working directory at capture time (identifies customer context).
            excerpt: Optional short excerpt; defaults to first 200 chars of turn_text.

        Returns:
            List of Candidate objects (possibly empty). On Haiku failure,
            returns a single marker Candidate with tag "self-error".
        """
        captured_at = datetime.now(timezone.utc)
        # Default excerpt: first 200 chars of the turn — enough to identify
        # context during review without bloating the staging YAML.
        excerpt = excerpt or turn_text[:200]

        try:
            already_known = _read_safe(self.observeie_md_path)
            prompt = _build_prompt(
                template_path=self.prompt_template_path,
                turn_text=turn_text,
                already_known=already_known,
                cwd=cwd,
                captured_at=captured_at,
            )
            haiku_output = _invoke_haiku(prompt, self.model)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            # Q1 fix: subprocess.TimeoutExpired inherits from subprocess.SubprocessError,
            # NOT from RuntimeError or OSError. Without this third clause a 60-second
            # Haiku hang would escape the handler and crash the SessionStop hook.
            # Catching subprocess.SubprocessError covers TimeoutExpired,
            # CalledProcessError, and any future subprocess exceptions cleanly.
            # WHY: spec §9 — log AND surface. Never silent.
            print(
                f"[observe-learning-capture] classifier.py: haiku invocation "
                f"failed for session={session_id}: {e}",
                file=sys.stderr,
            )
            return [
                build_marker_candidate(
                    failure_reason=str(e),
                    session_id=session_id, cwd=cwd,
                    captured_at=captured_at,
                )
            ]

        raw_candidates = parse_haiku_yaml_output(haiku_output)
        if not raw_candidates and not _is_empty_haiku_response(haiku_output):
            # Haiku returned something the parser couldn't parse —
            # non-empty/non-empty-list (after fence stripping), but no candidates extracted.
            # Log the first 200 chars for diagnostics; surface a marker.
            # WHY use _is_empty_haiku_response: Haiku may wrap "[]" in ```yaml fences.
            # haiku_output.strip() != "[]" would fire spuriously on "```yaml\n[]\n```".
            # The helper normalises both fenced and bare empty-list responses.
            print(
                f"[observe-learning-capture] classifier.py: malformed yaml "
                f"from haiku for session={session_id}",
                file=sys.stderr,
            )
            return [
                build_marker_candidate(
                    failure_reason=f"malformed yaml: {haiku_output[:200]}",
                    session_id=session_id, cwd=cwd,
                    captured_at=captured_at,
                )
            ]

        result: List[Candidate] = []
        for raw in raw_candidates:
            try:
                result.append(_raw_to_candidate(
                    raw, session_id=session_id, cwd=cwd,
                    captured_at=captured_at, excerpt=excerpt,
                    model=self.model, prompt_version=self.prompt_version,
                ))
            except (KeyError, ValueError) as e:
                # Skip malformed individual records but log so nothing is
                # silently dropped. WHY: one bad record shouldn't block
                # the rest of the batch.
                print(
                    f"[observe-learning-capture] classifier.py: skipped "
                    f"malformed candidate record: {e}",
                    file=sys.stderr,
                )
                continue
        return result


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
    already_known: str,
    cwd: str,
    captured_at: datetime,
) -> str:
    """Render the classifier prompt by substituting template placeholders.

    Args:
        template_path: Path to classifier.md prompt template.
        turn_text: Conversation text to analyze.
        already_known: Full content of ObserveIE.md (prevents re-capture).
        cwd: Working directory at capture time.
        captured_at: UTC timestamp for context.

    Returns:
        Fully rendered prompt string, ready to pass to the `claude` CLI.
    """
    template = template_path.read_text(encoding="utf-8")
    return (
        template
        .replace("{{TURN}}", turn_text)
        .replace("{{ALREADY_KNOWN}}", already_known or "(empty)")
        .replace("{{CWD}}", cwd)
        .replace("{{CONTEXT_TIMESTAMP}}", captured_at.isoformat())
    )


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

    Args:
        failure_reason: Human-readable description of what failed.
        session_id: Claude Code session identifier for provenance.
        cwd: Working directory at capture time.
        captured_at: UTC timestamp of the failure.

    Returns:
        Candidate with title "[FAILURE] classifier", section "Plugin Self-Errors",
        confidence "low", and tag "self-error".
    """
    return Candidate.create(
        title="[FAILURE] classifier",
        fact=f"Classifier failed: {failure_reason}",
        proposed_section="Plugin Self-Errors",
        confidence="low",
        tags=["self-error"],
        provenance=Provenance(
            session_id=session_id, cwd=cwd,
            captured_at=captured_at,
            # Excerpt includes the reason so reviewers don't need to check logs.
            excerpt=f"Auto-generated marker. Reason: {failure_reason}",
        ),
        classifier=None,
    )
