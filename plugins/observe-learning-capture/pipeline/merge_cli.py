"""CLI helper for the slash commands.

Usage:
    python3 -m pipeline.merge_cli --merge ID
    python3 -m pipeline.merge_cli --discard ID
    python3 -m pipeline.merge_cli --list

This module is the thin glue layer between the /observe-review and
/observe-capture slash commands and the merge/stage pipeline modules.
It reads the config.json at the plugin root to locate the pending file
and destination ObserveIE.md, then delegates to merge.py / stage.py.

Error handling:
    - Config load failure → stderr + return 1.
    - Unknown candidate ID → stderr + return 1.
    - OSError during merge/remove_from_pending → propagated (logged by callee).
    - All non-fatal paths log to stderr for observability.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pipeline.merge import merge_candidate, remove_from_pending
from pipeline.stage import read_pending
from pipeline.types import Candidate


def main() -> int:
    """Entry point for merge_cli. Parses args and dispatches to merge, discard,
    or list. Returns 0 on success, 1 on user-visible error.

    Returns:
        int: Exit code — 0 for success, 1 for recoverable user-visible error.
    """
    p = argparse.ArgumentParser(
        description="Merge, discard, or list pending learn candidates."
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--merge", metavar="ID", help="Merge candidate ID into ObserveIE.md")
    g.add_argument("--discard", metavar="ID", help="Discard candidate ID from pending")
    g.add_argument("--list", action="store_true", help="List all pending candidates")
    args = p.parse_args()

    # Load plugin config — required for pending_file and destination_file paths.
    try:
        config = _load_config()
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[observe-learning-capture] merge_cli.py: cannot load config: {exc}",
            file=sys.stderr,
        )
        return 1

    pending_file = Path(os.path.expanduser(config["pending_file"]))
    destination_file = Path(os.path.expanduser(config["destination_file"]))

    # Read all pending records upfront — needed for all three operations.
    records = read_pending(pending_file)

    if args.list:
        # Print tab-separated: id, confidence, title — one per line.
        # WHY tab-separated: easy for shell scripts to parse via `awk -F'\t'`.
        for r in records:
            print(f"{r['id']}\t{r.get('confidence', '?')}\t{r.get('title', '?')}")
        return 0

    # Both --merge and --discard need to locate the candidate by ID.
    target_id = args.merge or args.discard
    target = next((r for r in records if r.get("id") == target_id), None)
    if target is None:
        print(f"ID {target_id} not found in pending", file=sys.stderr)
        return 1

    if args.merge:
        # Reconstruct the Candidate object and promote it into ObserveIE.md.
        candidate = _record_to_candidate(target)
        merge_candidate(candidate, destination_file)
        remove_from_pending(target_id, pending_file)
        print(f"Merged {target_id} → {destination_file}")
    else:
        # --discard: remove from pending without writing to ObserveIE.md.
        remove_from_pending(target_id, pending_file)
        print(f"Discarded {target_id}")

    return 0


def _record_to_candidate(record: dict) -> Candidate:
    """Reconstruct a Candidate from a YAML pending-file record.
    Delegates to Candidate.from_yaml_record() (added in T02). The id
    is preserved from the stored record — never recomputed — so the
    bullet written to ObserveIE.md matches the dedupe contract.
    """
    return Candidate.from_yaml_record(record)


def _load_config() -> dict:
    """Load config.json from the plugin root (parent of this file's directory).

    The plugin root is always one directory above pipeline/, so __file__'s
    parent.parent is stable regardless of how the module was invoked
    (python3 -m pipeline.merge_cli, or imported in tests).

    Returns:
        Parsed config dict with at minimum 'pending_file' and
        'destination_file' keys.

    Raises:
        OSError: If the config file cannot be read.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    plugin_root = Path(__file__).parent.parent
    return json.loads((plugin_root / "config.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    sys.exit(main())
