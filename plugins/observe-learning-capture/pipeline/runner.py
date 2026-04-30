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
from pathlib import Path

from pipeline.classifier import Classifier
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
        model=config["haiku_model"],
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


if __name__ == "__main__":
    sys.exit(main())
