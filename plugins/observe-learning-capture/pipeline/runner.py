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
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.classifier import Classifier, build_marker_candidate
from pipeline.dedupe import extract_existing_ids, is_duplicate
from pipeline.stage import append_candidates, read_pending
from pipeline.transcript import current_logical_turn, all_assistant_turns


def main() -> int:
    """Argv-parsing wrapper. Calls main_with_args with parsed values.

    Kept thin so tests can drive the pipeline via main_with_args without
    needing to monkey-patch sys.argv.

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
    return main_with_args(
        mode=args.mode,
        transcript=args.transcript,
        session_id=args.session_id,
        cwd=args.cwd,
    )


def main_with_args(
    mode: str,
    transcript: str,
    session_id: str,
    cwd: str,
) -> int:
    """Actual runner logic. Returns exit code.

    Bug 2 fix part 5: this function now runs the SDK auth precheck BEFORE
    constructing the Classifier (so missing/invalid keys produce a marker
    without ever touching the classifier path). The outer ``except`` block
    around the main pipeline ALSO emits a marker now — previously it
    logged to stderr and returned 0 silently, which violated the spec §9
    "log AND surface" mantra.

    Args:
        mode: ``"stop"`` (classify last assistant turn) or ``"session-end"``
            (concatenate all assistant turns and classify once).
        transcript: Path to the session JSONL transcript.
        session_id: Claude Code session UUID, used for marker provenance.
        cwd: Capture-time working directory, used for marker provenance.

    Returns:
        Exit code: 0 on success or handled failure (with marker emitted),
        1 only on fatal config load failure.
    """
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

    # CLI precheck before any classifier work begins. On failure the precheck
    # has ALREADY emitted a marker via direct append_candidates, so we just
    # return 0 cleanly — no further work. Replaces the SDK-era _auth_precheck
    # which validated ANTHROPIC_API_KEY against the API; we now rely on the
    # `claude` CLI to resolve subscription auth at invocation time, and the
    # only thing we can pre-check cheaply is whether the binary exists.
    if not _cli_precheck(pending_path, session_id, cwd):
        return 0

    try:
        classifier = Classifier(
            model=config.get("classifier_model", config.get("haiku_model", "claude-sonnet-4-5")),
            prompt_template_path=plugin_root / "prompts" / "classifier.md",
            observeie_md_path=destination_path,
            prompt_version=config["prompt_version"],
            # Inject pending_path so Classifier's internal marker writes
            # (e.g. cache-visibility sentinel) land in the same file the
            # runner writes to — and so tests using a tmp pending path
            # don't pollute ~/.claude/agents/.observeie-pending.md.
            pending_path=pending_path,
        )
        transcript_path = Path(transcript)

        # ------------------------------------------------------------------
        # Build turn text based on mode
        # ------------------------------------------------------------------
        if mode == "stop":
            turn = current_logical_turn(transcript_path)
            if turn is None:
                return 0
            turn_text = turn.text
            excerpt = turn.text[:200]
        else:
            # session-end mode: full session concatenated as one block.
            # WHY join with double newline: preserves inter-turn separation so
            # the classifier can reason about topic shifts across the convo.
            turn_text = "\n\n".join(
                t.text for t in all_assistant_turns(transcript_path)
            )
            if not turn_text:
                return 0
            excerpt = "(full session scan)"

        # ------------------------------------------------------------------
        # Classify
        # ------------------------------------------------------------------
        candidates = classifier.classify(
            turn_text=turn_text,
            session_id=session_id,
            cwd=cwd,
            excerpt=excerpt,
        )

        # ------------------------------------------------------------------
        # Dedup: filter candidates already merged into ObserveIE.md OR already
        # sitting in the pending queue. Without the pending check, the same
        # discovery can be staged twice in a long session before the merge
        # step runs — resulting in duplicate entries surviving to ObserveIE.md.
        # ------------------------------------------------------------------
        existing_ids = extract_existing_ids(destination_path)
        # Also collect IDs of candidates already waiting in the pending file.
        # WHY: a candidate staged in a prior turn this session has the same
        # id as one produced by the classifier this turn (id is deterministic
        # from fact text). Without this check, same-session re-triggers add
        # duplicates.
        pending_ids = {
            r.get("id") for r in read_pending(pending_path) if r.get("id")
        }
        already_known_ids = existing_ids | pending_ids
        novel = [c for c in candidates if not is_duplicate(c, already_known_ids)]

        # ------------------------------------------------------------------
        # Stage: append novel candidates to the pending file
        # ------------------------------------------------------------------
        append_candidates(pending_path, novel)
        return 0

    except Exception as exc:  # noqa: BLE001 — outer safety net; see below
        # Bug 2 fix part 5: outer catch now emits a marker via direct
        # append_candidates rather than logging+swallowing. Per spec §9
        # mantra: log AND surface — never silent. Previously this swallowed
        # unexpected runner errors (e.g. dedupe path failure, import error
        # after a broken upgrade) so the user had no signal anything broke.
        print(
            f"[observe-learning-capture] runner.py: unexpected runner error "
            f"for session={session_id}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        try:
            marker = build_marker_candidate(
                failure_reason=(
                    f"runner outer-catch: {type(exc).__name__}: {exc}"
                ),
                session_id=session_id,
                cwd=cwd,
                captured_at=datetime.now(timezone.utc),
            )
            append_candidates(pending_path, [marker])
        except Exception as marker_exc:  # noqa: BLE001 — last-ditch surface
            # If even the marker write fails (e.g. disk full, permission
            # denied), we still want stderr to record the original error
            # and the marker write failure. Don't re-raise — the hook
            # subshell exit code stays 0 so we don't pollute the user's
            # interactive output.
            print(
                f"[observe-learning-capture] runner.py: marker emission "
                f"also failed: {marker_exc}",
                file=sys.stderr,
            )
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


def _cli_precheck(
    pending_path: Path,
    session_id: str,
    cwd: str,
) -> bool:
    """Verify the `claude` binary is reachable on PATH. Returns True if OK.

    This is the subprocess-pivot replacement for `_auth_precheck`. The old
    precheck validated ANTHROPIC_API_KEY against the API via models.list;
    we no longer use the API directly, so the only cheap pre-check
    available at this layer is whether `claude` is invocable at all.

    Why we do NOT actually run `claude --version` (or similar) here:
        Every `claude` invocation costs subscription quota and adds
        wall-time latency to the hook subshell. A pre-flight version
        check would burn quota on every Stop hook even when no real
        classification is needed (e.g. when the prefilter would
        immediately reject the turn — though the prefilter actually
        runs in the bash hook script BEFORE this Python pipeline starts,
        so by the time we are here we are committed to a classifier
        call anyway). The deeper auth check (subscription valid, not
        rate-limited) is exercised by the first real classifier call,
        and any failure there produces a marker via the exception
        ladder in classifier.py the same way an explicit precheck
        failure would. Keeping the precheck cheap and structural
        avoids paying for the same check twice.

    Per spec section 9 mantra: log AND surface — never silent.

    Args:
        pending_path: File path where any failure marker should be appended.
        session_id: Session UUID, used for marker provenance.
        cwd: Capture-time working directory, used for marker provenance.

    Returns:
        True if `claude` is reachable on PATH; False otherwise. On False,
        a marker has already been appended to `pending_path` and the
        caller should short-circuit cleanly.
    """
    captured_at = datetime.now(timezone.utc)

    if shutil.which("claude") is None:
        marker = build_marker_candidate(
            failure_reason=(
                "`claude` binary not found on PATH. The hook subprocess could "
                "not invoke the CLI; check the hook environment's PATH or "
                "install Claude Code."
            ),
            session_id=session_id, cwd=cwd, captured_at=captured_at,
        )
        try:
            append_candidates(pending_path, [marker])
        except Exception as exc:  # noqa: BLE001 — last-ditch surface to stderr
            # If even the marker write fails (disk full, permissions), we
            # still log so the failure is visible in the hook's stderr/log.
            print(
                f"[observe-learning-capture] runner.py: cli-precheck marker "
                f"write failed: {exc}",
                file=sys.stderr,
            )
        print(
            "[observe-learning-capture] runner.py: `claude` binary not on PATH "
            "— skipping classifier; marker emitted",
            file=sys.stderr,
        )
        return False

    return True


if __name__ == "__main__":
    sys.exit(main())
