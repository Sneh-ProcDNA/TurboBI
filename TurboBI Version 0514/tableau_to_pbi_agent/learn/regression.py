"""Run the converter on every workbook in the corpus and snapshot the
resulting visual.json files into memory. Compare two snapshots to
verify only the changes the observer expected actually happened.

Why we don't keep a permanent gold output set: the user explicitly
asked for "current code is good, just don't break it" rather than
"freeze a hand-verified output." We materialize a baseline snapshot
ON DEMAND right before applying a candidate change, then re-snapshot
afterward and diff the two. The baseline is ephemeral.

Snapshot shape:

    {
        "<workbook_stem>": {
            "<page_id>/<visual_id>": {visual.json contents as dict},
            ...
        },
        ...
    }

We snapshot ONLY visual.json files. TMDL diffs would be noisy from
non-deterministic lineage tags etc., and the visual layer is what
data-rule changes (visual_rules.json, visual_picker) actually affect.
"""

from __future__ import annotations

import io
import json
import shutil
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .corpus import short_label

# NOTE: tableau_to_pbi.* is imported LAZILY inside _convert_one. The
# learner reloads those modules between baseline and re-snapshot to
# pick up edits to visual_rules.json / config.py / etc., and a
# top-level import would freeze the binding to the pre-edit module.


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# Snapshot type alias — one workbook -> {visual_path -> visual.json dict}
WbSnapshot = Dict[str, Dict[str, Any]]
CorpusSnapshot = Dict[str, WbSnapshot]


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def _convert_one(twbx_path: Path, work_root: Path) -> Path:
    """Run the deterministic converter on `twbx_path`, output beneath
    `work_root`. Stdout is silenced. Returns the PBIP root.

    Imports are intentionally deferred to call time — after the
    learner edits a data file, the runner clears tableau_to_pbi from
    sys.modules and we want THIS function to pick up the freshly
    re-imported chain. A module-level import would freeze the binding.
    """
    from tableau_to_pbi.converter import Converter, extract_twbx
    stem = twbx_path.stem
    extract_dir = work_root / f"_{stem}_extracted"
    output_dir  = work_root / f"{stem}_pbip"
    with redirect_stdout(io.StringIO()):
        twb_path, hypers = extract_twbx(str(twbx_path), str(extract_dir))
        Converter(
            twb_path,
            output=str(output_dir),
            stub_only=False,
            hyper_paths=hypers,
        ).run()
    return output_dir


def _read_visuals(pbip_root: Path) -> Dict[str, Any]:
    """Walk the PBIP and collect every visual.json into a dict keyed by
    `<page_id>/<visual_id>`."""
    out: Dict[str, Any] = {}
    report_dir = next(
        (p for p in pbip_root.glob("*.Report") if p.is_dir()),
        None,
    )
    if report_dir is None:
        return out
    pages_root = report_dir / "definition" / "pages"
    if not pages_root.exists():
        return out
    for vp in pages_root.glob("*/visuals/*/visual.json"):
        page_id   = vp.parent.parent.parent.name
        visual_id = vp.parent.name
        try:
            data = json.loads(vp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out[f"{page_id}/{visual_id}"] = data
    return out


def snapshot_corpus(
    workbooks: List[Path],
    work_root: Optional[Path] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> CorpusSnapshot:
    """Run converter on every workbook, return the in-memory snapshot.

    `work_root` defaults to a scratch dir under .backups so PBIP
    artifacts don't pollute the user's workspace. We delete it on the
    way out — only the in-memory snapshot lives on.
    """
    if work_root is None:
        work_root = _REPO_ROOT / "tableau_to_pbi_agent" / ".regression_scratch"
    if work_root.exists():
        shutil.rmtree(work_root, ignore_errors=True)
    work_root.mkdir(parents=True, exist_ok=True)

    snap: CorpusSnapshot = {}
    for wb in workbooks:
        label = short_label(wb)
        if on_progress:
            on_progress(label)
        try:
            pbip = _convert_one(wb, work_root)
            snap[label] = _read_visuals(pbip)
        except Exception as e:
            # Conversion failure on a corpus member is itself a
            # regression — record it as an empty snapshot with a sentinel
            # the differ will pick up.
            snap[label] = {"__conversion_error__": str(e)}

    shutil.rmtree(work_root, ignore_errors=True)
    return snap


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def diff_snapshots(
    before: CorpusSnapshot,
    after:  CorpusSnapshot,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return {workbook -> [change records]} for every visual whose JSON
    changed between snapshots. Records carry just enough context for
    the regression gate to classify each diff as expected or not."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    keys = sorted(set(before) | set(after))
    for k in keys:
        b = before.get(k, {})
        a = after.get(k, {})
        if "__conversion_error__" in a and "__conversion_error__" not in b:
            out[k] = [{"path": "__workbook__", "kind": "conversion_broke",
                       "error": a["__conversion_error__"]}]
            continue
        wb_changes: List[Dict[str, Any]] = []
        all_paths = set(b) | set(a)
        for vp in sorted(all_paths):
            if vp == "__conversion_error__":
                continue
            bv = b.get(vp)
            av = a.get(vp)
            if bv == av:
                continue
            wb_changes.append({
                "path":   vp,
                "before": bv,
                "after":  av,
                "kind":   _classify_change(bv, av),
            })
        if wb_changes:
            out[k] = wb_changes
    return out


def _classify_change(before: Any, after: Any) -> str:
    """Coarse change kind: visual_added / visual_removed / visualType_changed
    / other. Used by the regression gate to decide which observer
    expectation should match the diff."""
    if before is None and after is not None:
        return "visual_added"
    if before is not None and after is None:
        return "visual_removed"
    bt = (before or {}).get("visual", {}).get("visualType")
    at = (after  or {}).get("visual", {}).get("visualType")
    if bt != at:
        return "visualType_changed"
    return "other"


# ---------------------------------------------------------------------------
# Expectation matching
# ---------------------------------------------------------------------------

def classify_against_expectations(
    diffs: Dict[str, List[Dict[str, Any]]],
    expected: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Sort diffs into (matched, unexpected).

    `expected` is a list of records like:
        {workbook: "Dental_Pharma", visual_path: "<page>/<vid>",
         from: "tableEx", to: "card"}

    A diff matches if there's an `expected` row whose workbook AND
    visual_path AND from->to combination line up. Anything else lands
    in `unexpected` — the regression gate fails on a non-empty
    unexpected list.
    """
    matched: List[Dict[str, Any]] = []
    unexpected: List[Dict[str, Any]] = []

    # Index expectations by (workbook, visual_path) for O(1) lookup.
    exp_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for e in expected:
        exp_by_key[(e["workbook"], e["visual_path"])] = e

    for wb, changes in diffs.items():
        for ch in changes:
            key = (wb, ch.get("path", ""))
            exp = exp_by_key.get(key)
            if not exp:
                unexpected.append({"workbook": wb, **ch})
                continue
            # Verify the visualType change matches expectation.
            if ch["kind"] != "visualType_changed":
                unexpected.append({"workbook": wb, **ch})
                continue
            bt = (ch["before"] or {}).get("visual", {}).get("visualType")
            at = (ch["after"]  or {}).get("visual", {}).get("visualType")
            if bt != exp.get("from") or at != exp.get("to"):
                unexpected.append({
                    "workbook": wb, **ch,
                    "expected_from": exp.get("from"),
                    "expected_to":   exp.get("to"),
                })
                continue
            matched.append({"workbook": wb, **ch})

    return matched, unexpected
