#!/usr/bin/env python3
"""CLI entry point for the Tableau -> Power BI Project converter.

The conversion logic lives in the `tableau_to_pbi` package. This file
just parses arguments and hands off. Keep it thin.

Usage
-----
    python script.py <input.twb | input.twbx> [--output ./out]

    # Long-form flags (kept for backward compat with old invocations):
    python script.py --twb  workbook.twb        [--output ./out]
    python script.py --twbx workbook.twbx       [--output ./out]

Behavior depends on the file extension:

    .twb   -> visuals-only mode. Data model is a single placeholder
              table; visual queries come out empty but the page layout
              and visual types are preserved.

    .twbx  -> full mode. A real TMDL semantic model is built from the
              workbook's <object-graph> metadata, and visuals are wired
              to those tables and columns.
"""

import argparse
from pathlib import Path

from tableau_to_pbi import run


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Convert Tableau .twb/.twbx to Power BI .pbip.",
    )
    # Positional input (preferred) plus legacy --twb / --twbx flags.
    ap.add_argument("input", nargs="?",
                    help="Path to a .twb or .twbx file")
    ap.add_argument("--twb",  help="(legacy) .twb workbook")
    ap.add_argument("--twbx", help="(legacy) .twbx packaged workbook")
    ap.add_argument("--output", help="Output folder (default: <name>_pbip)")
    args = ap.parse_args()

    src = args.input or args.twbx or args.twb
    if not src:
        ap.error("Pass a .twb or .twbx path (positional or --twb/--twbx).")

    try:
        run(input_path=src, output=args.output)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
