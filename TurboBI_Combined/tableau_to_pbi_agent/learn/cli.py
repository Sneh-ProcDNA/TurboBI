"""Command-line entry point for the learn-and-merge layer.

Usage:
    python -m tableau_to_pbi_agent.learn run
    python -m tableau_to_pbi_agent.learn rollback [--to <stamp>]
    python -m tableau_to_pbi_agent.learn status
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backup import BackupSession, list_sessions, session_for
from .corpus import discover
from .runner import run as run_pass


def _cmd_run(args) -> int:
    report = run_pass()
    print()
    print("=" * 70)
    print(f"corpus workbooks : {report['workbooks']}")
    accepted = sum(1 for r in report["passes"] if r.get("result") == "accepted")
    rejected = sum(1 for r in report["passes"] if r.get("result") == "rejected")
    noops    = sum(1 for r in report["passes"] if r.get("result") == "noop")
    errored  = sum(1 for r in report["passes"] if r.get("result") == "apply_error")
    print(f"observers run    : {len(report['passes'])}")
    print(f"  accepted       : {accepted}")
    print(f"  rejected       : {rejected}")
    print(f"  noop           : {noops}")
    print(f"  errored        : {errored}")
    print("=" * 70)
    return 1 if errored else 0


def _cmd_rollback(args) -> int:
    sessions = list_sessions()
    if not sessions:
        print("[ROLLBACK] no backup sessions to roll back to.")
        return 1
    target = args.to or sessions[-1]
    if target not in sessions:
        print(f"[ROLLBACK] session not found: {target}")
        print(f"           available: {sessions}")
        return 1
    sess = session_for(target)
    restored = sess.restore()
    print(f"[ROLLBACK] restored {len(restored)} file(s) from session {target}:")
    for p in restored:
        print(f"  - {p}")
    return 0


def _cmd_status(args) -> int:
    print("== Corpus ==")
    for wb in discover():
        print(f"  {wb}")
    print()
    print("== Backup sessions (oldest -> newest) ==")
    for s in list_sessions():
        print(f"  {s}")
    print()
    log = (
        Path(__file__).resolve().parent.parent / "learn_log.jsonl"
    )
    if log.exists():
        lines = log.read_text(encoding="utf-8").splitlines()
        print(f"== Last 5 log entries (of {len(lines)}) ==")
        for raw in lines[-5:]:
            try:
                rec = json.loads(raw)
                print(f"  [{rec.get('ts','?')}] "
                      f"{rec.get('observer','?')} -> {rec.get('result','?')}")
            except json.JSONDecodeError:
                print(f"  (malformed line)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tableau_to_pbi_agent.learn")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run",      help="Run all observers, autonomously merge if regression passes.")

    rb = sub.add_parser("rollback", help="Restore files from a past backup session.")
    rb.add_argument("--to", help="Session timestamp (default: latest).")

    sub.add_parser("status",   help="Show corpus, backup sessions, and recent log entries.")

    args = p.parse_args(argv)
    if   args.cmd == "run":      return _cmd_run(args)
    elif args.cmd == "rollback": return _cmd_rollback(args)
    elif args.cmd == "status":   return _cmd_status(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
