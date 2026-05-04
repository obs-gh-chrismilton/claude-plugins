"""CLI entry point for the observe-learning-capture pipeline.

Invoked by hooks (stop-hook.sh, session-end-scan.sh) after the shell
prefilter passes. Orchestrates: transcript parse → Haiku classification →
dedup against ObserveIE.md → append to pending staging file.

Modes
-----
stop
    Classify the single most-recent assistant turn.
    Used by stop-hook.sh on every session stop.

session-end
    Classify all assistant turns concatenated.
    Used by session-end-scan.sh for a full session retrospective.

Exit codes
----------
0   Success (including "no candidates to write" — that is not an error).
1   Fatal config error (bad JSON, missing file). Logged to stderr.
    Any other internal exception is caught, logged, and returns 0 so
    the hook's background subshell doesn't produce a stray non-zero exit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from pipeline.classifier import Classifier, build_marker_candidate
from pipeline.dedupe import extract_existing_ids, is_duplicate
from pipeline.stage import append_candidates, read_pending
from pipeline.transcript import last_assistant_turn, all_assistant_turns


def main() -> int:
    """Parse args, run the pipeline, return exit code.

    Returns:
        0 on success (including empty candidate list).
        1 on config load failure (OSError / JSONDecodeError).
    """
    p = argparse.ArgumentParser(
        description="observe-learning-capture pipeline runner"
    )
    p.add_argument(
        "--mode",
        choices=["stop", "session-end"],
        required=True,
        help="stop: classify last turn; session-end: classify full session.",
    )
    p.add_argument(
        "--transcript",
        required=True,
        help="Path to the session JSONL transcript file.",
    )
    p.add_argument(
        "--session-id",
        required=True,
        help="Claude Code session UUID for provenance metadata.",
    )
    p.add_argument(
        "--cwd",
        required=True,
        help="Project directory at capture time (identifies customer context).",
    )
    args = p.parse_args()

    # Config load: fail fast with exit 1 so stop-hook.sh's background subshell
    # logs a clear error rather than cascading into a confusing import failure.
    try:
        config = _load_config()
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[observe-learning-capture] runner.py: cannot load config: {exc}",
            file=sys.stderr,
        )
        return 1

    # Resolve file paths with expanduser so ~/... paths work from any cwd.
    # WHY: config.json stores "~/.claude/..." — the plugin is invoked from
    # varying directories by hooks, so we must expand explicitly (Python's
    # Path() does NOT expand ~ automatically).
    destination_path = Path(os.path.expanduser(config["destination_file"]))
    pending_path = Path(os.path.expanduser(config["pending_file"]))

    plugin_root = Path(__file__).parent.parent

    classifier = Classifier(
        model=config.get("classifier_model", config.get("haiku_model", "claude-sonnet-4-5")),
        prompt_template_path=plugin_root / "prompts" / "classifier.md",
        observeie_md_path=destination_path,
        prompt_version=config["prompt_version"],
    )

    transcript_path = Path(args.transcript)

    # ------------------------------------------------------------------
    # Build turn text based on mode
    # ------------------------------------------------------------------
    if args.mode == "stop":
        # Stop mode: single most-recent assistant turn
        turn = last_assistant_turn(transcript_path)
        if turn is None:
            # No assistant turn yet (e.g. hook fired at session start before
            # Claude has responded). Not an error — just nothing to classify.
            return 0
        turn_text = turn.text
        excerpt = turn.text[:200]
    else:
        # session-end mode: full session concatenated as one block.
        # WHY join with double newline: preserves inter-turn separation so
        # Haiku can reason about topic shifts across the conversation.
        turn_text = "\n\n".join(t.text for t in all_assistant_turns(transcript_path))
        if not turn_text:
            return 0
        excerpt = "(full session scan)"

    # ------------------------------------------------------------------
    # Classify
    # ------------------------------------------------------------------
    try:
        candidates = classifier.classify(
            turn_text=turn_text,
            session_id=args.session_id,
            cwd=args.cwd,
            excerpt=excerpt,
        )
    except Exception as exc:  # noqa: BLE001 — belt-and-suspenders; classifier already logs
        # Per spec §9: classifier.py already logs+surfaces via marker candidate
        # on expected failures. This outer catch handles truly unexpected paths
        # (e.g. import errors after a broken upgrade) without crashing the hook.
        print(
            f"[observe-learning-capture] runner.py: unexpected classifier error "
            f"for session={args.session_id}: {exc}",
            file=sys.stderr,
        )
        return 0

    # ------------------------------------------------------------------
    # Dedup: filter candidates already merged into ObserveIE.md OR already
    # sitting in the pending queue. Without the pending check, the same
    # discovery can be staged twice in a long session before the merge step
    # runs — resulting in duplicate entries that survive into ObserveIE.md.
    # ------------------------------------------------------------------
    existing_ids = extract_existing_ids(destination_path)
    # Also collect IDs of candidates already waiting in the pending file.
    # WHY: a candidate staged in a prior turn this session has the same id
    # as one produced by the classifier this turn (id is deterministic from
    # fact text). Without this check, same-session re-triggers add duplicates.
    pending_ids = {r.get("id") for r in read_pending(pending_path) if r.get("id")}
    already_known_ids = existing_ids | pending_ids
    novel = [c for c in candidates if not is_duplicate(c, already_known_ids)]

    # ------------------------------------------------------------------
    # Stage: append novel candidates to the pending file
    # ------------------------------------------------------------------
    append_candidates(pending_path, novel)

    return 0


def _load_config() -> dict:
    """Load config.json from the plugin root directory.

    Returns:
        Parsed config dict.

    Raises:
        OSError: If config.json cannot be read.
        json.JSONDecodeError: If config.json is malformed.
    """
    # __file__ is pipeline/runner.py — parent.parent is the plugin root.
    plugin_root = Path(__file__).parent.parent
    config_path = plugin_root / "config.json"
    return json.loads(config_path.read_text(encoding="utf-8"))


def _auth_precheck(
    pending_path: Path,
    session_id: str,
    cwd: str,
) -> bool:
    """Validate ANTHROPIC_API_KEY presence + correctness. Returns True if OK.

    Bug 2 fix part: catches both "key unset" and "key set but invalid"
    failure modes BEFORE any classifier work. On failure, emits a marker
    via direct append_candidates (cannot use Classifier's marker path
    since Classifier construction may fail too).

    Per spec section 9 mantra: log AND surface — never silent.

    Args:
        pending_path: File path where any failure marker should be appended.
        session_id: Session UUID, used for marker provenance.
        cwd: Capture-time working directory, used for marker provenance.

    Returns:
        True if the key is present AND validates against the API; False
        otherwise (caller should short-circuit before constructing the
        classifier). Transient API errors at precheck time return True so
        the classifier still gets a chance — its own error handling will
        emit a marker if the call ultimately fails.
    """
    captured_at = datetime.now(timezone.utc)

    # Branch 1: key not set in env at all. The hook environment may not
    # inherit the user's shell exports, so this is a common failure mode.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        marker = build_marker_candidate(
            failure_reason=(
                "ANTHROPIC_API_KEY not set in hook environment. "
                "Add `export ANTHROPIC_API_KEY=sk-...` to ~/.zshrc and restart Claude Code."
            ),
            session_id=session_id, cwd=cwd, captured_at=captured_at,
        )
        try:
            append_candidates(pending_path, [marker])
        except Exception as exc:  # noqa: BLE001 — last-ditch surface to stderr
            # If even the marker write fails, we still log so the failure is
            # visible somewhere (stderr is captured by the hook framework).
            print(
                f"[observe-learning-capture] runner.py: auth-precheck marker "
                f"write failed: {exc}",
                file=sys.stderr,
            )
        print(
            "[observe-learning-capture] runner.py: ANTHROPIC_API_KEY missing — "
            "skipping classifier; marker emitted",
            file=sys.stderr,
        )
        return False

    # Branch 2: key present — validate it via models.list (free; no token
    # consumption; stable across SDK versions). Catches the common "key
    # rotated and stale value still in env" failure mode.
    try:
        client = anthropic.Anthropic()
        client.models.list(limit=1)
        return True
    except anthropic.AuthenticationError as exc:
        marker = build_marker_candidate(
            failure_reason=f"key rejected: {getattr(exc, 'status_code', '?')} from API",
            session_id=session_id, cwd=cwd, captured_at=captured_at,
        )
        try:
            append_candidates(pending_path, [marker])
        except Exception:  # noqa: BLE001 — best-effort marker write
            pass
        print(
            f"[observe-learning-capture] runner.py: API key rejected: {exc}",
            file=sys.stderr,
        )
        return False
    except anthropic.APIError as exc:
        # Transient connectivity / rate limit at precheck time — emit marker
        # but ALSO return True. Reasoning: the precheck is best-effort; if
        # the API is briefly unavailable we still want the classifier to
        # try its own call (which has its own error handling). Don't block
        # on transient precheck failures.
        marker = build_marker_candidate(
            failure_reason=f"[non-blocking] precheck transient: {type(exc).__name__}",
            session_id=session_id, cwd=cwd, captured_at=captured_at,
        )
        try:
            append_candidates(pending_path, [marker])
        except Exception:  # noqa: BLE001 — best-effort marker write
            pass
        print(
            f"[observe-learning-capture] runner.py: precheck transient error "
            f"(continuing anyway): {exc}",
            file=sys.stderr,
        )
        return True


if __name__ == "__main__":
    sys.exit(main())
