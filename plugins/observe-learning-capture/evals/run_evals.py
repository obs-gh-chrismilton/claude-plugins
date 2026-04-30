"""Eval runner for the observe-learning-capture classifier.

Reads evals/evals.json, runs each eval through the real classifier (real
Haiku call), grades against the expected assertions, and prints summary
+ a JSON result file the autoresearch loop consumes for results.tsv.

Usage:
    python3 -m evals.run_evals [--prompt <path>] [--out <path>] [--quiet]

The --prompt arg lets autoresearch swap in a candidate prompt without
overwriting the production prompts/classifier.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make pipeline importable regardless of cwd
PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from pipeline.classifier import Classifier  # noqa: E402


def grade_one(case: dict, candidates: list) -> tuple[bool, str]:
    """Return (passed, reason). Reason is empty on pass, descriptive on fail."""
    expected = case["expected"]
    n = len(candidates)
    min_c = expected.get("min_candidates", 0)
    max_c = expected.get("max_candidates", 99)

    if not (min_c <= n <= max_c):
        return False, f"candidate count {n} outside [{min_c},{max_c}]"

    # NO cases: count check is the only assertion.
    if expected.get("min_candidates", 0) == 0 and expected.get("max_candidates", 0) == 0:
        return True, ""

    # YES cases: at least one candidate must match the assertions.
    if "any_fact_contains" in expected:
        terms = expected["any_fact_contains"]
        match = any(
            all(t.lower() in c.fact.lower() for t in terms)
            for c in candidates
        )
        if not match:
            # Looser: any single term match counts (terms are alternatives, not all required)
            looser = any(
                any(t.lower() in c.fact.lower() for t in terms)
                for c in candidates
            )
            if not looser:
                return False, f"no candidate fact mentions any of {terms}"

    if "any_section_matches" in expected:
        pattern = expected["any_section_matches"]
        match = any(
            re.search(pattern, c.proposed_section, re.IGNORECASE)
            for c in candidates
        )
        if not match:
            sections = [c.proposed_section for c in candidates]
            return False, f"no candidate section matches /{pattern}/i (got {sections})"

    if "any_tags_include" in expected:
        wanted = [t.lower() for t in expected["any_tags_include"]]
        all_tags = {t.lower() for c in candidates for t in c.tags}
        # At least one wanted tag must appear in any candidate
        if not any(w in all_tags for w in wanted):
            return False, f"no candidate has any tag from {wanted} (got {sorted(all_tags)})"

    return True, ""


def run_one(case: dict, classifier: Classifier, observeie_md_temp: Path) -> dict:
    """Run one eval. Returns a result dict for aggregation."""
    # Build a minimal ObserveIE.md context for this case (the classifier loads
    # it as {{ALREADY_KNOWN}} during prompt assembly).
    observeie_md_temp.write_text(
        case.get("already_known_excerpt", "# ObserveIE\n"), encoding="utf-8"
    )

    start = time.time()
    try:
        candidates = classifier.classify(
            turn_text=case["input_turn"],
            session_id=f"eval-{case['id']}",
            cwd="/tmp/eval",
            excerpt=case["input_turn"][:200],
        )
    except Exception as exc:
        return {
            "id": case["id"],
            "name": case["name"],
            "passed": False,
            "reason": f"classifier crashed: {exc}",
            "n_candidates": 0,
            "duration_s": time.time() - start,
        }

    duration_s = time.time() - start

    # Filter out marker candidates (self-error) — those are pipeline diagnostics, not real candidates
    real_candidates = [c for c in candidates if "self-error" not in c.tags]

    passed, reason = grade_one(case, real_candidates)
    return {
        "id": case["id"],
        "name": case["name"],
        "passed": passed,
        "reason": reason,
        "n_candidates": len(real_candidates),
        "n_markers": len(candidates) - len(real_candidates),
        "duration_s": duration_s,
        "category": case["category"],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--prompt", default=str(PLUGIN_ROOT / "prompts" / "classifier.md"),
                   help="Path to the classifier prompt template to evaluate")
    p.add_argument("--evals", default=str(PLUGIN_ROOT / "evals" / "evals.json"))
    p.add_argument("--out", default=str(PLUGIN_ROOT / "evals" / "last_result.json"))
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    evals = json.loads(Path(args.evals).read_text())

    # Use a tempdir-based ObserveIE.md so the classifier's existing-knowledge
    # context is per-eval (each case sets its own already_known_excerpt).
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="eval-"))
    observeie_md = tmpdir / "ObserveIE.md"

    classifier = Classifier(
        model="claude-haiku-4-5-20251001",
        prompt_template_path=Path(args.prompt),
        observeie_md_path=observeie_md,
    )

    if not args.quiet:
        print(f"Running {len(evals)} evals against {Path(args.prompt).name}...")
        print()

    results = []
    started = datetime.now(timezone.utc)
    for case in evals:
        r = run_one(case, classifier, observeie_md)
        results.append(r)
        if not args.quiet:
            mark = "✓" if r["passed"] else "✗"
            extra = f" — {r['reason']}" if r["reason"] else ""
            print(f"  {mark} {r['id']:30} ({r['n_candidates']} cand, {r['duration_s']:.1f}s){extra}")

    n_pass = sum(1 for r in results if r["passed"])
    n = len(results)
    pass_rate = n_pass / n if n else 0.0
    total_duration = sum(r["duration_s"] for r in results)
    duration_mean = total_duration / n if n else 0.0

    summary = {
        "started_at": started.isoformat(),
        "pass_rate": pass_rate,
        "n_pass": n_pass,
        "n_total": n,
        "duration_s_mean": duration_mean,
        "duration_s_total": total_duration,
        "tokens_mean": None,  # Haiku CLI doesn't easily expose token counts; null for now
        "results": results,
        "prompt_path": args.prompt,
    }

    Path(args.out).write_text(json.dumps(summary, indent=2))

    if not args.quiet:
        print()
        print(f"Pass rate: {n_pass}/{n} = {pass_rate:.1%}")
        print(f"Mean duration: {duration_mean:.1f}s per eval")
        print(f"Total wall: {total_duration:.1f}s")
        print(f"Result written to: {args.out}")

    return 0 if pass_rate == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
