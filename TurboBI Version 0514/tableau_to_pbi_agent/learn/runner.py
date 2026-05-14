"""Orchestrate the full learn-and-merge pass.

Flow:

  1. Snapshot the corpus (run converter on every workbook, capture
     every visual.json into memory).
  2. Build per-workbook facts: parsed worksheets + the just-captured
     visuals. This is what observers consume.
  3. For each observer, collect ChangeProposals.
  4. For each proposal:
       a) Open a BackupSession and capture every file_to_modify.
       b) Call apply().
       c) Reload Python modules whose data files we just edited
          (visual_picker caches visual_rules.json on import).
       d) Re-snapshot the corpus.
       e) Diff before/after; classify diffs against expected_diffs.
       f) If unexpected list is empty -> keep, log success.
          Else -> restore from backup, log failure with first few
          unexpected diffs.

Each pass is logged to learn_log.jsonl so re-runs are auditable.
"""

from __future__ import annotations

import importlib
import io
import json
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .backup     import BackupSession
from .corpus     import discover, short_label
from .observer_base import ChangeProposal, Observer
from .observers  import CardObserver, KpiDimLabelObserver
from .regression import (
    snapshot_corpus,
    diff_snapshots,
    classify_against_expectations,
)


_LOG_PATH = (
    Path(__file__).resolve().parent.parent / "learn_log.jsonl"
)


# ---------------------------------------------------------------------------
# Corpus facts: parser output + current visual snapshot per workbook
# ---------------------------------------------------------------------------

def _parse_worksheets(twbx_path: Path) -> List[Dict[str, Any]]:
    """Parse a twbx (extracts the .twb if needed). Silences the parser's
    chatty hyper-binding output — the runner has its own logging.

    Lazy imports: see regression._convert_one for why."""
    from tableau_to_pbi.converter import extract_twbx
    from tableau_to_pbi.parser    import TWBParser
    extract_dir = twbx_path.parent / f"_{twbx_path.stem}_extracted"
    with redirect_stdout(io.StringIO()):
        twb_path, _hypers = extract_twbx(str(twbx_path), str(extract_dir))
        p = TWBParser(twb_path)
        p.parse()
    return list(p.worksheets)


def _build_corpus_facts(
    workbooks: List[Path],
    snapshot: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Pair each workbook's parsed worksheets with its visual snapshot."""
    facts: List[Dict[str, Any]] = []
    for wb in workbooks:
        label = short_label(wb)
        try:
            ws_list = _parse_worksheets(wb)
        except Exception as e:
            print(f"  [WARN] parse failed for {wb.name}: {e}")
            ws_list = []
        facts.append({
            "workbook_label": label,
            "workbook_path":  wb,
            "worksheets":     ws_list,
            "visuals":        snapshot.get(label, {}),
        })
    return facts


# ---------------------------------------------------------------------------
# Module reload — visual_picker caches the rules JSON on first import
# ---------------------------------------------------------------------------

def _reload_converter_modules() -> None:
    """Drop converter modules so the next snapshot picks up our edits."""
    for mod_name in list(sys.modules):
        if mod_name == "tableau_to_pbi" or mod_name.startswith("tableau_to_pbi."):
            del sys.modules[mod_name]
    # Also clear visual_picker's cached rules dict directly when the
    # module was already imported above (defensive for future caches
    # that survive sys.modules deletion).
    try:
        vp = importlib.import_module("tableau_to_pbi.visual_picker")
        if hasattr(vp, "_rules_cache"):
            vp._rules_cache = None
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------

def run(observers: List[Observer] | None = None,
        verbose: bool = True) -> Dict[str, Any]:
    if observers is None:
        observers = [CardObserver(), KpiDimLabelObserver()]

    workbooks = discover()
    if not workbooks:
        print("[LEARN] no workbooks found in corpus.")
        return {"workbooks": 0, "passes": []}

    print(f"[LEARN] corpus: {len(workbooks)} workbook(s)")
    for wb in workbooks:
        print(f"        - {wb.relative_to(_repo())}")

    print("\n[LEARN] baseline snapshot...")
    t0 = time.time()
    before = snapshot_corpus(
        workbooks,
        on_progress=(lambda l: print(f"        baseline: {l}")) if verbose else None,
    )
    print(f"[LEARN] baseline done in {time.time()-t0:.1f}s — "
          f"{sum(len(v) for v in before.values())} visuals captured")

    facts = _build_corpus_facts(workbooks, before)

    log_records: List[Dict[str, Any]] = []
    for obs in observers:
        proposals = obs.observe(facts)
        if not proposals:
            print(f"\n[LEARN] {obs.name}: nothing to do")
            log_records.append({
                "ts": _now(), "observer": obs.name,
                "result": "noop",
            })
            continue

        for prop in proposals:
            print(f"\n[LEARN] {obs.name}: {prop.summary}")
            print(f"        files: "
                  f"{[str(p.relative_to(_repo())) for p in prop.files_to_modify]}")
            print(f"        expected diffs: {len(prop.expected_diffs)}")

            session = BackupSession()
            for f in prop.files_to_modify:
                session.capture(f)

            try:
                prop.apply()
            except Exception as e:
                print(f"        [FAIL] apply raised: {e}")
                session.restore()
                log_records.append({
                    "ts": _now(), "observer": obs.name,
                    "result": "apply_error", "error": str(e),
                    "backup": session.stamp,
                })
                continue

            _reload_converter_modules()

            print("        re-snapshotting corpus...")
            t1 = time.time()
            after = snapshot_corpus(workbooks, on_progress=None)
            print(f"        re-snapshot done in {time.time()-t1:.1f}s")

            diffs = diff_snapshots(before, after)
            matched, unexpected = classify_against_expectations(
                diffs, prop.expected_diffs,
            )

            if unexpected:
                print(f"        [REJECT] {len(unexpected)} unexpected "
                      f"change(s) — rolling back")
                for u in unexpected[:5]:
                    print(f"          - {u['workbook']} {u.get('path','?')} "
                          f"kind={u.get('kind','?')}")
                session.restore()
                _reload_converter_modules()
                log_records.append({
                    "ts": _now(), "observer": obs.name,
                    "result": "rejected",
                    "matched":     len(matched),
                    "unexpected":  len(unexpected),
                    "backup":      session.stamp,
                    "first_unexpected": unexpected[:5],
                })
            else:
                print(f"        [ACCEPT] {len(matched)} expected "
                      f"change(s) verified, no unexpected diffs")
                # Adopt 'after' as the new baseline so subsequent
                # observers in the same pass diff against it.
                before = after
                log_records.append({
                    "ts": _now(), "observer": obs.name,
                    "result":      "accepted",
                    "matched":     len(matched),
                    "files":       [str(p) for p in prop.files_to_modify],
                    "backup":      session.stamp,
                })

    _append_log(log_records)
    return {"workbooks": len(workbooks), "passes": log_records}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now().isoformat(timespec="seconds")


def _repo() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _append_log(records: List[Dict[str, Any]]) -> None:
    if not records:
        return
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")
