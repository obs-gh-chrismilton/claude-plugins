"""Classifier for learning candidates.

Invokes the local `claude` CLI as a subprocess so the call inherits the
user's Claude Code subscription auth (no ANTHROPIC_API_KEY needed). On
any failure, emits a "marker candidate" so the human sees the failure
at next review (per spec §9 — log AND surface).

Why subprocess and not the Anthropic Python SDK
-----------------------------------------------
The previous implementation imported `anthropic` and called
`Anthropic().messages.create(...)` with `cache_control` markers on
layered system blocks. That required an ANTHROPIC_API_KEY in the hook
subprocess environment. macOS launchd does NOT inherit the user's
interactive shell exports into hook subprocesses, so the key was
unavailable and every classifier invocation across a 4-day window
emitted a `[FAILURE] classifier` marker instead of doing real work.

Pivoting to `claude -p` lets the call inherit the user's existing MAX
subscription credentials from the surrounding Claude Code session via
the macOS keychain. Trade-offs (accepted by design committee on
2026-05-08):
  - Cache-control markers are not exposed by the CLI; we lose the
    fine-grained per-block ephemeral-cache strategy the SDK supported.
  - Token usage data is not consistently extractable in a stable shape,
    so `_invoke_classifier` returns just text (no usage tuple).
  - Per-call quota draws from the same 5-hour rolling window as the
    user's interactive Claude Code usage.
"""
from __future__ import annotations

import json as _json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

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
    """Orchestrates `claude` CLI subprocess invocations to produce Candidate
    objects from assistant turn text.

    Attributes:
        model: Claude model ID (e.g. "claude-sonnet-4-5"). Passed via
            `--model` so the classifier doesn't inherit whatever model
            the parent Claude Code session is running on (interactive
            sessions on Opus 4.7 1M would 30x the per-call cost).
        prompt_template_path: Path to the static classifier prompt template.
        observeie_md_path: Path to ObserveIE.md — slim known-facts derived
            from this so the classifier avoids re-capturing known facts.
        prompt_version: Version label embedded in ClassifierMeta for each
            produced candidate, so prompts can be retro-evaluated later.
        pending_path: Where marker candidates are appended on failure.
            Injected (NOT hardcoded) so tests use a tmp path and never
            pollute the user's real ~/.claude/agents/.observeie-pending.md.
    """

    model: str
    prompt_template_path: Path
    observeie_md_path: Path
    prompt_version: str = "1.0"
    pending_path: Path = field(default_factory=lambda: Path(
        os.path.expanduser("~/.claude/agents/.observeie-pending.md")
    ))

    def classify(
        self,
        turn_text: str,
        session_id: str,
        cwd: str,
        excerpt: Optional[str] = None,
    ) -> List[Candidate]:
        """Run classifier on turn_text. Returns 0+ candidates.

        Subprocess pivot (2026-05-08): _invoke_classifier shells out to
        `claude -p` instead of using the Anthropic SDK. Exception ladder
        is structured around subprocess error types (FileNotFoundError,
        TimeoutExpired, non-zero exit, JSONDecodeError) instead of
        anthropic.* exceptions.

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
            classifier_output = _invoke_classifier(
                static_template=static_template,
                slim_known_facts=slim_block,
                user_message=user_message,
                model=self.model,
            )
        except FileNotFoundError as exc:
            # `claude` binary not on PATH at invocation time. Distinguish from
            # generic OSError so the user-actionable failure_reason is clear.
            print(
                f"[observe-learning-capture] classifier.py: `claude` binary "
                f"not found for session={session_id}: {exc}",
                file=sys.stderr,
            )
            return [build_marker_candidate(
                failure_reason=(
                    "`claude` binary not found on PATH; the hook subprocess "
                    "could not invoke the CLI"
                ),
                session_id=session_id, cwd=cwd, captured_at=captured_at,
            )]
        except subprocess.TimeoutExpired as exc:
            # `claude -p` exceeded the subprocess timeout. The exception's
            # __str__ embeds argv (which embeds the full prompt) — _sanitize
            # in build_marker_candidate caps that to 200 chars.
            print(
                f"[observe-learning-capture] classifier.py: `claude -p` timeout "
                f"for session={session_id} after {exc.timeout}s",
                file=sys.stderr,
            )
            return [build_marker_candidate(
                failure_reason=f"claude -p timeout after {exc.timeout}s",
                session_id=session_id, cwd=cwd, captured_at=captured_at,
            )]
        except _json.JSONDecodeError as exc:
            # `claude -p --output-format json` returned non-JSON stdout. Most
            # likely cause: CLI version skew or a CLI-side error that didn't
            # use the documented JSON envelope. Log and surface.
            print(
                f"[observe-learning-capture] classifier.py: invalid JSON from "
                f"`claude -p` for session={session_id}: {exc}",
                file=sys.stderr,
            )
            return [build_marker_candidate(
                failure_reason=f"json parse error from claude -p: {exc}",
                session_id=session_id, cwd=cwd, captured_at=captured_at,
            )]
        except subprocess.SubprocessError as exc:
            # Catches subprocess.SubprocessError subclasses we did NOT match
            # above (e.g. CalledProcessError if a downstream caller switches
            # to check=True; we currently use check=False and inspect
            # returncode manually inside _invoke_classifier).
            print(
                f"[observe-learning-capture] classifier.py: subprocess error "
                f"for session={session_id}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return [build_marker_candidate(
                failure_reason=f"{type(exc).__name__}: {exc}",
                session_id=session_id, cwd=cwd, captured_at=captured_at,
            )]
        except (RuntimeError, OSError) as exc:
            # _invoke_classifier raises RuntimeError when the CLI returns a
            # non-zero exit code (the failure mode for "Not logged in" and
            # subscription-rate-limit cases). OSError covers any I/O issue
            # outside the subprocess itself (template read failure, etc.).
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

# NOTE: `_maybe_emit_cache_warning` removed on 2026-05-08 alongside the SDK
# pivot. The sentinel-file infrastructure assumed visibility into
# `response.usage.cache_read_input_tokens` from the Anthropic SDK; the
# `claude -p` CLI does not expose that data in a stable, parseable shape,
# so cache visibility is no longer measurable from this layer. The CLI
# manages its own cache opaquely. We accept blind operation per the design
# committee on 2026-05-08 (the SDK's per-block cache strategy was already
# being defeated by the CLI's full-context loading anyway, so the warning
# wouldn't have provided actionable signal even if measurable).


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


# Per-call subprocess timeout (seconds). Scoped to a module constant so the
# value is greppable and a future tuning change touches one site.
#
# WHY 120: the SDK path documented retries+timeout compounding to ~6min
# worst-case; we no longer control retries (the CLI handles its own), so
# the simpler model is "wait at most 2 minutes for the CLI to produce
# output, otherwise emit a TimeoutExpired marker and move on." The hook
# subshell on macOS is not bounded as aggressively as the SDK comment
# suggested — `&`-detached background processes survive the parent's
# completion — so 120s is comfortably within budget.
_CLAUDE_P_TIMEOUT_SECONDS = 120


def _invoke_classifier(
    static_template: str,
    slim_known_facts: str,
    user_message: str,
    model: str,
) -> str:
    """Invoke `claude -p` as a subprocess and return the response text.

    The command line we build is:

        claude -p \
            --system-prompt <STATIC + SLIM joined> \
            --model <model> \
            --output-format json \
            --no-session-persistence

    The user_message is passed via STDIN (NOT argv) — see R2 from the
    2026-05-08 risk audit. macOS ARG_MAX is ~256KB; SessionEnd-mode runs
    concatenate ALL assistant turns into user_message and can easily blow
    that limit, producing OSError: argument list too long. Stdin has no
    such limit and is also more shell-safe.

    Args:
        static_template: The static portion of the classifier prompt
            (instructions, output schema, etc.). Combined with
            slim_known_facts into a single --system-prompt argument.
        slim_known_facts: The bounded section/id summary derived from
            ObserveIE.md so the classifier can avoid re-capturing
            already-known facts.
        user_message: The per-call payload — wrapped turn text plus the
            cwd/timestamp metadata produced by `_build_prompt`. Passed
            on stdin.
        model: Claude model identifier (e.g. "claude-sonnet-4-5"). Passed
            via --model. WHY explicit: without --model, claude -p inherits
            the parent session's model which could be Opus 4.7 1M (~30x
            the per-call cost of Sonnet 4.5).

    Returns:
        The unwrapped assistant-text payload from the JSON envelope's
        `.result` field. May be an empty string if the model produced
        no text output.

    Raises:
        FileNotFoundError: If the `claude` binary is not on PATH at
            invocation time.
        subprocess.TimeoutExpired: If the CLI did not return within
            _CLAUDE_P_TIMEOUT_SECONDS.
        json.JSONDecodeError: If `claude -p --output-format json`
            returned something that is not parseable JSON. Propagates
            so the caller's exception ladder can emit a marker.
        RuntimeError: If `claude -p` returned a non-zero exit code. The
            error message includes the exit code and the truncated
            stderr so a human reader can diagnose (e.g. "Not logged in").

    Caching note:
        Cache visibility / control is unrecoverable through `claude -p`.
        The CLI loads CLAUDE.md, project memory, and other surrounding
        context regardless of our prompt structure, so the layered
        per-block cache strategy the SDK supported is moot. We collapse
        static + slim into a single --system-prompt argument and accept
        opaque caching. See classifier.py module docstring for context.
    """
    # Collapse the two cacheable system blocks into one. The SDK path used
    # two ephemeral-cached blocks but the CLI doesn't expose cache_control,
    # so the structural distinction no longer buys anything; one block is
    # simpler and avoids any risk of the CLI mishandling repeated --system
    # flags (the CLI accepts --system-prompt as a single string argument).
    system_prompt = f"{static_template}\n\n{slim_known_facts}"

    argv = [
        "claude",
        "-p",
        "--system-prompt", system_prompt,
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
    ]

    # NOTE on quota cost: each call here counts against the user's MAX
    # subscription 5-hour rolling window (shared with their interactive
    # Claude Code usage). Per the 2026-05-08 user direction, this is
    # acceptable; no per-day rate limiter is enforced at this layer.
    proc = subprocess.run(
        argv,
        input=user_message,           # R2: pass via stdin to dodge ARG_MAX
        capture_output=True,
        text=True,
        timeout=_CLAUDE_P_TIMEOUT_SECONDS,
        check=False,                  # we inspect returncode below
    )

    if proc.returncode != 0:
        # Truncate stderr aggressively — the CLI may emit verbose context
        # (auth failure messages with installation hints, etc.) and we want
        # a marker that is YAML-safe and human-readable. The marker layer's
        # _sanitize() also caps to 200 chars, but trimming here keeps the
        # raised exception message itself bounded.
        truncated_stderr = (proc.stderr or "").strip()[:300]
        raise RuntimeError(
            f"claude -p exited {proc.returncode}: {truncated_stderr or '(no stderr)'}"
        )

    # The CLI's JSON envelope is documented to put the assistant text at
    # `.result`. We do not defensively probe other shapes — if the schema
    # ever changes, the JSONDecodeError path or a KeyError will surface
    # via the exception ladder rather than silently returning "".
    parsed = _json.loads(proc.stdout)
    return parsed.get("result", "")


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
