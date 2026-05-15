"""Parse converter stdout into structured warning records.

The converter emits human-readable lines like:

    [RESOLVE] 'Region (Dim!HCO)' (ds=federated.0y4y...) not found — field dropped from visual.
    [FILTER]  'Region' (ds=federated.0y4y...) not found — visual-level filter dropped.
    [DS]      worksheet datasourceRef 'foo' did not match any parsed datasource; falling back to first.

We pull those into typed dicts so the orchestrator can dispatch one
resolver per warning kind without re-implementing regex parsing
everywhere. Anything that doesn't match a known shape is skipped — we
do NOT want to LLM-resolve every line of stdout.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List


# Field-resolution failure (most common). The em-dash before "field
# dropped" is the standard separator in report.py's print(); fall back
# to a plain hyphen too just in case.
_RESOLVE_RE = re.compile(
    r"^\[(?P<kind>RESOLVE|FILTER)\]\s+'(?P<field>[^']+)'\s+"
    r"\(ds=(?P<ds>[^)]+)\)\s+not found"
)


# Datasource routing fallback — emitted when a worksheet's datasourceRef
# can't be matched to any parsed datasource.
_DS_RE = re.compile(
    r"^\[DS\]\s+worksheet datasourceRef '(?P<ref>[^']+)' did not match"
)


def parse(stdout: str) -> List[Dict[str, Any]]:
    """Walk a converter stdout dump and return one record per warning.

    Records carry a 'kind' field ('resolve' | 'filter' | 'ds') so the
    orchestrator can shard them to the right resolver. Duplicates
    (same field warned twice because it appears on multiple visuals)
    are deduped — one resolution applies everywhere.
    """
    seen_keys: set = set()
    out: List[Dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _RESOLVE_RE.match(line)
        if m:
            kind = "resolve" if m.group("kind") == "RESOLVE" else "filter"
            field = m.group("field").strip()
            ds = m.group("ds").strip()
            key = (kind, ds, field)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append({
                "kind":  kind,
                "field": field,
                "ds":    ds,
                "raw":   line,
            })
            continue

        m = _DS_RE.match(line)
        if m:
            ref = m.group("ref").strip()
            key = ("ds", ref)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append({
                "kind": "ds",
                "ref":  ref,
                "raw":  line,
            })
    return out


def summarize(warnings: List[Dict[str, Any]]) -> Dict[str, int]:
    """Tally warnings by kind. Used for before/after eval reporting."""
    out: Dict[str, int] = {}
    for w in warnings:
        out[w["kind"]] = out.get(w["kind"], 0) + 1
    return out
