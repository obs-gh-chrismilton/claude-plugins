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
from datetime import datetime
from pathlib import Path

from pipeline.merge import merge_candidate, remove_from_pending
from pipeline.stage import read_pending
from pipeline.types import Candidate, ClassifierMeta, Provenance


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

    Prefers Candidate.from_yaml_record() (added in T02 fix) as the primary
    deserializer. The manual fallback path is defensive — it handles any future
    case where from_yaml_record is absent (e.g., very old plugin version loaded
    against a newer test environment), but in normal operation from_yaml_record
    is always present.

    Args:
        record: Dict as produced by Candidate.to_yaml_record() and read back
                from the pending staging file via stage.read_pending().

    Returns:
        Candidate instance with all fields restored.
    """
    # Primary path: use the canonical deserializer on Candidate (T02 fix).
    # WHY prefer this: it is the single source of truth for deserialization
    # logic and keeps field mapping in one place (types.py, not here).
    if hasattr(Candidate, "from_yaml_record"):
        return Candidate.from_yaml_record(record)

    # Defensive fallback: manual reconstruction mirroring types.py logic.
    # This branch should never be hit in a correctly installed plugin.
    src = record["source"]
    cls_meta = record.get("classifier") or {}
    return Candidate.create(
        title=record["title"],
        fact=record["fact"],
        proposed_section=record["proposed_section"],
        confidence=record["confidence"],
        tags=list(record.get("tags", [])),
        provenance=Provenance(
            session_id=src["session_id"],
            cwd=src["cwd"],
            captured_at=datetime.fromisoformat(src["captured_at"]),
            excerpt=src["excerpt"],
        ),
        classifier=ClassifierMeta(
            model=cls_meta.get("model", "unknown"),
            prompt_version=cls_meta.get("prompt_version", "1.0"),
            confidence_score=cls_meta.get("confidence_score"),
        ) if cls_meta else None,
    )


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
