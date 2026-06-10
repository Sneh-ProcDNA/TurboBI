"""Command-line entry point.

Usage:
    python -m tableau_to_pbi_agent <workbook.twbx>
    python -m tableau_to_pbi_agent <workbook.twbx> --skip-llm
    python -m tableau_to_pbi_agent <workbook.twbx> --credentials credentials.json
    python -m tableau_to_pbi_agent <workbook.twbx> --model claude-haiku-4-5-20251001

Without --skip-llm the orchestrator expects ANTHROPIC_API_KEY in the
environment. With --skip-llm the agent runs the converter twice but
without any new LLM calls (still picks up an existing hints sidecar) —
useful for re-applying previously learned hints after a code change.
"""

from __future__ import annotations

import argparse
import sys

from tableau_to_pbi._logging import configure_default

from .orchestrator import run_with_agent


def main(argv=None) -> int:
    # Route [TAG] lines through the structured logger (stderr, INFO+).
    # See tableau_to_pbi/_logging.py — TURBOBI_LOG_LEVEL overrides the
    # default level (DEBUG / INFO / WARNING / ERROR).
    configure_default()

    p = argparse.ArgumentParser(prog="tableau_to_pbi_agent")
    p.add_argument("input", help="Path to .twb or .twbx workbook.")
    p.add_argument("--model", default=None,
                   help="Override the Claude model id (default: Haiku 4.5).")
    p.add_argument("--skip-llm", action="store_true",
                   help="Run pipeline without any LLM calls.")
    p.add_argument(
        "--credentials",
        metavar="CREDS_FILE",
        default=None,
        help=(
            "Path to a JSON or XLSX credentials file. Connection fields "
            "(server, database, port, schema) override the values parsed "
            "from the workbook. A credentials_manifest.json is written "
            "to the output folder for PBI Desktop / REST API setup."
        ),
    )
    args = p.parse_args(argv)

    report = run_with_agent(
        args.input,
        model=args.model,
        skip_llm=args.skip_llm,
        credentials_path=args.credentials,
    )
    print()
    print("=" * 50)
    print(f"warnings before : {report['warnings_before']}")
    print(f"warnings after  : {report['warnings_after']}")
    print(f"hints added     : {report['hints_added']}")
    print(f"hints total     : {report['hints_total']}")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(main())