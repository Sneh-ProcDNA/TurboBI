"""Discover the .twbx workbooks that act as the regression corpus.

The corpus is whatever .twbx files we can find under known directories
relative to the repo root:

    .                       (the workspace cwd — top-level user files)
    Sample Dashboards/       (user-curated test set)

Anything new the user drops into either folder is automatically picked
up on the next learner run. A `corpus_ignore.txt` file (one filename
per line) lets the user opt specific files out without deleting them.
"""

from __future__ import annotations

from pathlib import Path
from typing import List


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _ignore_set() -> set:
    ignore_path = _REPO_ROOT / "corpus_ignore.txt"
    if not ignore_path.exists():
        return set()
    return {
        line.strip()
        for line in ignore_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def discover() -> List[Path]:
    """Return every .twbx (sorted) found under known corpus locations."""
    out: List[Path] = []
    ignore = _ignore_set()
    for sub in (_REPO_ROOT, _REPO_ROOT / "Sample Dashboards"):
        if not sub.exists():
            continue
        for p in sorted(sub.glob("*.twbx")):
            if p.name in ignore:
                continue
            out.append(p)
    return out


def short_label(path: Path) -> str:
    """Compact 'workbook stem' identifier used as a key in regression
    snapshots and log lines."""
    return path.stem.replace(" ", "_")
