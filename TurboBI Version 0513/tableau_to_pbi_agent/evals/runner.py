"""Eval harness — run the agent over a list of workbooks and print a
before/after warnings table.

Usage:
    python -m tableau_to_pbi_agent.evals.runner workbook1.twbx workbook2.twbx
    python -m tableau_to_pbi_agent.evals.runner --skip-llm workbook1.twbx ...

The runner is intentionally tiny: no metrics framework, no scoring DSL.
Each row shows the deterministic baseline vs the post-agent state, so
you can eyeball whether a change to the prompt or context regressed
anything.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List

from ..orchestrator import run_with_agent


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("workbooks", nargs="+",
                   help="Paths to .twb / .twbx files.")
    p.add_argument("--skip-llm", action="store_true",
                   help="Run without LLM calls (for plumbing checks).")
    p.add_argument("--model", default=None)
    args = p.parse_args(argv)

    rows = []
    for wb in args.workbooks:
        if not Path(wb).exists():
            print(f"  [SKIP] {wb} not found")
            continue
        t0 = time.time()
        report = run_with_agent(wb, model=args.model,
                                skip_llm=args.skip_llm)
        dt = time.time() - t0
        rows.append({
            "workbook": Path(wb).name,
            "before":   report["warnings_before"],
            "after":    report["warnings_after"],
            "added":    report["hints_added"],
            "total":    report["hints_total"],
            "time_s":   round(dt, 1),
        })

    print()
    print("=" * 80)
    print(f"{'workbook':<40} {'before':>7} {'after':>7} "
          f"{'added':>6} {'total':>6} {'time_s':>7}")
    print("-" * 80)
    for r in rows:
        print(f"{r['workbook']:<40} {r['before']:>7} {r['after']:>7} "
              f"{r['added']:>6} {r['total']:>6} {r['time_s']:>7}")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
