#!/usr/bin/env python3
"""CLI entry point for the Tableau -> Power BI Project converter.

The conversion logic lives in the `tableau_to_pbi` package. This file
just parses arguments and hands off. Keep it thin.

Usage
-----
    python script.py <input.twb | input.twbx> [--output ./out]

    # With a credentials file (JSON or XLSX):
    python script.py workbook.twbx --credentials credentials.json
    python script.py workbook.twbx --credentials credentials.xlsx --output ./out

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

Credentials file (--credentials)
---------------------------------
    Accepts a JSON or XLSX file that provides per-datasource connection
    overrides (server, database, port, schema) and credentials
    (username, password).

    Connection overrides are applied when building partition-M
    expressions in the SemanticModel — useful for promoting a workbook
    from a dev server to a prod server without editing the Tableau file.

    A ``credentials_manifest.json`` is written alongside the PBIP
    output.  It documents the effective connection parameters and
    username for each live data source so the user knows exactly what
    to enter in Power BI Desktop > Transform Data > Data source
    settings (or what to push via the Power BI REST API).

    JSON format:
        {
          "connections": [
            {"class": "sqlserver", "server": "prod.myco.com",
             "database": "AnalyticsDB", "username": "svc_pbi", "password": "..."},
            {"class": "snowflake",  "server": "acct.snowflakecomputing.com",
             "warehouse": "WH", "database": "DB", "username": "PBI", "password": "..."}
          ]
        }

    XLSX format: a sheet named "Credentials" (or first sheet) with
    column headers: class, server, database, port, schema, warehouse,
    username, password.
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
    ap.add_argument(
        "--credentials",
        metavar="CREDS_FILE",
        default=None,
        help=(
            "Path to a JSON or XLSX credentials file.  Connection fields "
            "(server, database, port, schema) override the values parsed "
            "from the Tableau workbook.  A credentials_manifest.json is "
            "written to the output folder documenting usernames and "
            "connection strings for PBI Desktop / REST API setup."
        ),
    )
    args = ap.parse_args()

    src = args.input or args.twbx or args.twb
    if not src:
        ap.error("Pass a .twb or .twbx path (positional or --twb/--twbx).")

    try:
        run(
            input_path=src,
            output=args.output,
            credentials_path=args.credentials,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())