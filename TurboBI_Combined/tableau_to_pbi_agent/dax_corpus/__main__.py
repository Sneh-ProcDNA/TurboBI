"""CLI for the DAX corpus + regression bed.

Usage:
    python -m tableau_to_pbi_agent.dax_corpus analyze
        Walk every .twbx under Sample Dashboards/, write
        corpus.jsonl + patterns.md next to this module.

    python -m tableau_to_pbi_agent.dax_corpus regen-tests
        Re-run the curated cases through the current translator and
        rewrite tableau_to_pbi/tests/test_dax_corpus.py with the
        captured outputs pinned. Diff the file to confirm only the
        expected cases moved.
"""

from __future__ import annotations

import sys


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = args[0]
    if cmd == "analyze":
        from .analyze import run
        return run()
    if cmd == "regen-tests":
        from .generate_tests import run
        return run()
    print(f"Unknown command: {cmd}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
