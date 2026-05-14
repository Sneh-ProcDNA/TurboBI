"""Hint sidecar I/O.

A "hint" tells the converter how to resolve a field that its own
deterministic resolver couldn't place. Shape on disk:

    {
        "<ds_name>": {
            "<field_name>": ["<table>", "<column>"],
            ...
        },
        ...
    }

The converter ignores hints unless its `_hint_lookup` validates the
(table, column) against the live model, so a stale hint pointing at a
column that no longer exists is silently dropped instead of crashing.

We keep one hints file per workbook, alongside the converter's _ir.json,
so re-runs pick up previously-learned hints without another LLM call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


HintMap = Dict[str, Dict[str, List[str]]]


def load(path: Path) -> HintMap:
    """Read a hints sidecar. Missing or malformed file -> empty dict."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: HintMap = {}
    for ds, mapping in data.items():
        if not isinstance(mapping, dict):
            continue
        ds_out: Dict[str, List[str]] = {}
        for field, loc in mapping.items():
            if (isinstance(loc, list) and len(loc) == 2
                    and all(isinstance(v, str) for v in loc)):
                ds_out[field] = [loc[0], loc[1]]
        if ds_out:
            out[ds] = ds_out
    return out


def save(path: Path, hints: HintMap) -> None:
    """Write hints atomically (write-tmp then rename) so a crash mid-write
    can't leave a partial file that load() would discard."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(hints, indent=2, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(path)


def merge(base: HintMap, new: HintMap) -> HintMap:
    """Layer `new` on top of `base` — new entries win."""
    out: HintMap = {ds: dict(m) for ds, m in base.items()}
    for ds, mapping in new.items():
        out.setdefault(ds, {}).update(mapping)
    return out


def add(hints: HintMap, ds_name: str,
        field: str, table: str, column: str) -> None:
    """Mutate `hints` in place to add one mapping."""
    hints.setdefault(ds_name, {})[field] = [table, column]


def hint_count(hints: HintMap) -> int:
    return sum(len(m) for m in hints.values())


def sidecar_path(output_dir: Path, twb_stem: str) -> Path:
    """Conventional location: alongside the PBIP output."""
    return output_dir / f"{twb_stem}_hints.json"
