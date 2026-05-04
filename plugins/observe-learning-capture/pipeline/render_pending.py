"""Session-start pending-review renderer.

Replaces the inline `python3 -c '...'` block previously embedded in
hooks/session-start-review.sh. Bug 4: that inline approach used
`sys.path.insert(0, os.environ.get("PWD", "."))` which was unreliable
when PWD wasn't exported to the subprocess — silently breaking the
pending-review surface across sessions.

This module is invoked as `python3 -m pipeline.render_pending`. It reads
the pending YAML file, formats a compact review block, and prints to
stdout. Claude Code's SessionStart hook injects this stdout into the
agent's context, where Claude surfaces it to the user on first prompt.

On YAML parse failure, prints a RENDER FAILED block instead of
silently swallowing — per spec §9 mantra "log AND surface — never silent."

Always exits 0: hooks must never block session flow.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from pipeline.stage import read_pending


def main(pending_path: Optional[Path] = None) -> int:
    """Render pending candidates to stdout. Returns 0 always."""
    if pending_path is None:
        # Default location matches config.json's pending_file
        default = os.environ.get(
            "PENDING_FILE_OVERRIDE",
            os.path.expanduser("~/.claude/agents/.observeie-pending.md"),
        )
        pending_path = Path(default)

    if not pending_path.exists():
        return 0  # nothing pending — first run / clean state

    if pending_path.stat().st_size == 0:
        return 0  # empty file

    try:
        records = read_pending(pending_path)
    except Exception as exc:
        # YAML parse failure or any other read-side error.
        # Surface to stdout so Claude Code injects it into context.
        # Per spec §9: log AND surface — never silent.
        print("=== OBSERVE LEARNING CAPTURE — RENDER FAILED ===")
        print(f"{type(exc).__name__}: {exc}")
        print(f"Inspect: {pending_path}")
        print(f"Manual recovery: cat {pending_path} | head -100  # then edit by hand")
        print("=== END OBSERVE LEARNING CAPTURE ===")
        return 0

    if not records:
        return 0

    print("=== OBSERVE LEARNING CAPTURE — PENDING REVIEW ===")
    print(f"{len(records)} candidate(s) pending review from prior sessions:")
    print()
    home = os.path.expanduser("~")
    for i, r in enumerate(records, 1):
        conf = r.get("confidence", "?")
        section = r.get("proposed_section", "?")
        title = r.get("title", "(no title)")
        src = r.get("provenance", r.get("source", {}))
        cwd = src.get("cwd", "?")
        cwd_short = cwd.replace(home, "~") if isinstance(cwd, str) else "?"
        captured_at = src.get("captured_at", "?")
        captured_at_short = captured_at[:10] if isinstance(captured_at, str) else "?"
        print(f"  #{i} [{conf:6}] {section}: {title}")
        print(f"       (from {cwd_short}, {captured_at_short})")
    print()
    print("I should surface these candidates to the user before responding to")
    print("their first prompt. The user may reply: merge all / merge N /")
    print("discard N / edit N / defer.")
    print("=== END OBSERVE LEARNING CAPTURE ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
