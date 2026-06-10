"""Golden snapshot tests.

Runs the converter on a small workbook corpus and asserts the output
hashes byte-match committed baselines. Catches accidental changes in
TMDL / report JSON output across refactors.

Corpus discovery:
  * Looks for ``.twbx`` files in ``Sample Dashboards/`` next to the
    repo root. The folder ships with the project but is not required
    — tests skip cleanly when a workbook is missing so the suite
    stays useful on machines that only have the source tree.

Update workflow:
  * To accept the current converter output as the new baseline, run
    with the env var ``UPDATE_SNAPSHOTS=1``. The tests then write
    fresh snapshot files instead of comparing.
  * Commit the updated snapshot files alongside the behavior change
    that justifies them. Reviewers see the snapshot diff in the PR
    and can sanity-check that the changes are intended.

The corpus is intentionally small (3 workbooks) so the full snapshot
run finishes in seconds. To run against the whole ``Sample Dashboards/``
folder, set ``SNAPSHOT_FULL_CORPUS=1`` — useful for big refactors but
too slow for the default unit-test pass.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import List

import pytest

from tableau_to_pbi.converter import Converter, extract_twbx
from tableau_to_pbi.tests.snapshot_helpers import (
    build_summary,
    collect_report_hashes,
    collect_semantic_model_hashes,
)


# Package root: tableau_to_pbi/tests/test_snapshot.py -> .../tableau_to_pbi
_PKG_ROOT = Path(__file__).resolve().parent.parent
# Repo root: tableau_to_pbi/ sits inside the project folder
_REPO_ROOT = _PKG_ROOT.parent
# Where committed snapshot files live.
_SNAPSHOT_DIR = _PKG_ROOT / "tests" / "snapshots"

# Default minimal corpus — fast enough for every test run.
_DEFAULT_CORPUS: List[str] = [
    "Sample Dashboards/UseCase2_test 3.twbx",
    "Sample Dashboards/DQM Dashboard.twbx",
    "Sample Dashboards/Netflix Movies and TV Shows Dashboard.twbx",
]


def _full_corpus() -> List[str]:
    """Every twbx in the Sample Dashboards folder. Opt-in via env var
    because running the whole set is slow."""
    dash_dir = _REPO_ROOT.parent / "Sample Dashboards"
    if not dash_dir.exists():
        return []
    return [
        f"Sample Dashboards/{p.name}"
        for p in sorted(dash_dir.glob("*.twbx"))
    ]


def _corpus() -> List[str]:
    if os.environ.get("SNAPSHOT_FULL_CORPUS") == "1":
        return _full_corpus()
    return _DEFAULT_CORPUS


def _resolve_corpus_path(rel: str) -> Path:
    """Resolve a corpus entry against the parent of the repo root.

    The ``Sample Dashboards/`` folder sits one level above the package
    root in the user's layout, so we search upward from the package.
    """
    candidate = _REPO_ROOT.parent / rel
    if candidate.exists():
        return candidate
    # Fall back to repo-root-relative for layouts that bundle dashboards
    # inside the project.
    inside = _REPO_ROOT / rel
    if inside.exists():
        return inside
    return candidate  # Non-existent — caller decides to skip


def _convert_to_tempdir(twbx_path: Path) -> Path:
    """Run the converter on ``twbx_path`` into a temporary directory.

    Returns the output directory containing the .pbip and its sibling
    Report / SemanticModel folders. Caller is responsible for cleanup
    (uses ``tempfile.mkdtemp`` so the path survives the function).
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="snap_"))
    stem = twbx_path.stem
    work_dir = tmpdir / f"_{stem}_extracted"
    twb_path, hypers = extract_twbx(str(twbx_path), str(work_dir))
    out_dir = tmpdir / f"{stem}_pbip"
    Converter(
        twb_path,
        str(out_dir),
        stub_only=False,
        hyper_paths=hypers,
        debug_ir=False,           # IR JSON adds noise we don't snapshot
        clean_output=True,
    ).run()
    return out_dir


def _capture(out_dir: Path, name: str) -> dict:
    """Build the full snapshot payload for one converted workbook."""
    return {
        "summary": build_summary(out_dir, name),
        "semantic_model": collect_semantic_model_hashes(out_dir, name),
        "report": collect_report_hashes(out_dir, name),
    }


def _snapshot_file(workbook_stem: str) -> Path:
    """Where the committed snapshot for a given workbook lives."""
    safe = workbook_stem.replace("/", "_").replace("\\", "_")
    return _SNAPSHOT_DIR / f"{safe}.json"


def _diff_payloads(expected: dict, actual: dict) -> List[str]:
    """Return a list of human-readable diff lines, or [] if identical.

    We don't render the full file contents — just enough to point a
    developer at what changed. ``UPDATE_SNAPSHOTS=1`` + a diff tool on
    the snapshot JSON is the recommended workflow for resolving.
    """
    lines: List[str] = []

    # Summary deltas
    exp_sum = expected.get("summary", {})
    act_sum = actual.get("summary", {})
    for key in sorted(set(exp_sum) | set(act_sum)):
        if exp_sum.get(key) != act_sum.get(key):
            lines.append(
                f"  summary.{key}: {exp_sum.get(key)!r} -> {act_sum.get(key)!r}"
            )

    # File-hash deltas, by category
    for category in ("semantic_model", "report"):
        exp = expected.get(category, {})
        act = actual.get(category, {})
        for path in sorted(set(exp) | set(act)):
            e, a = exp.get(path), act.get(path)
            if e is None:
                lines.append(f"  {category} ADDED: {path}")
            elif a is None:
                lines.append(f"  {category} REMOVED: {path}")
            elif e != a:
                lines.append(f"  {category} CHANGED: {path}")
    return lines


@pytest.mark.parametrize("workbook_rel", _corpus())
def test_snapshot(workbook_rel: str) -> None:
    """One parametrized test per corpus workbook.

    Each runs the converter end-to-end on the workbook, captures
    normalized hashes + a summary, and compares to the committed
    snapshot. Failure mode is a printable diff listing every file
    that changed plus every summary count that drifted.
    """
    src = _resolve_corpus_path(workbook_rel)
    if not src.exists():
        pytest.skip(f"corpus workbook not found: {workbook_rel}")

    out_dir = _convert_to_tempdir(src)
    try:
        actual = _capture(out_dir, src.stem)
    finally:
        # Best-effort cleanup. Windows occasionally holds handles, so
        # don't fail the test on cleanup error.
        shutil.rmtree(out_dir.parent, ignore_errors=True)

    snap_path = _snapshot_file(src.stem)

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(
            json.dumps(actual, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        pytest.skip(f"snapshot updated: {snap_path.name}")

    if not snap_path.exists():
        pytest.fail(
            f"no snapshot baseline for {src.stem}. "
            f"Run with UPDATE_SNAPSHOTS=1 to create one at {snap_path}."
        )

    expected = json.loads(snap_path.read_text(encoding="utf-8"))
    diffs = _diff_payloads(expected, actual)
    if diffs:
        msg = (
            f"Snapshot mismatch for {src.stem}:\n"
            + "\n".join(diffs[:50])  # cap output so failures stay readable
            + (
                f"\n  ... and {len(diffs) - 50} more"
                if len(diffs) > 50 else ""
            )
            + "\n\nIf the change is intentional, re-run with "
            "UPDATE_SNAPSHOTS=1 and commit the updated snapshot file."
        )
        pytest.fail(msg)
