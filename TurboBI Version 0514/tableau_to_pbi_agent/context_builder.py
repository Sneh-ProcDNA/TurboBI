"""Build the compact, cache-friendly context Claude needs to resolve a
field-resolution warning.

Token strategy:

  * **No XML.** The twb is parser-cleaned into the IR JSON dicts; we
    pass those to Claude directly. A workbook that's 500 KB of XML
    becomes ~30 KB of IR JSON, and we only need a tiny slice of that.
  * **Per-datasource snapshot, system-cached.** The full table -> column
    list goes once into a cached system message. Every warning for that
    datasource reuses the cache.
  * **Per-warning user turn is tiny.** It carries only the missing
    field, the worksheet's wsColumns hint, and 1-2 sibling fields from
    the same worksheet. Typical user turn: ~250 input tokens.

The model snapshot is built once per workbook in `model_snapshot()`;
per-warning slices come from `warning_context()`.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Model snapshot (cached as system prompt)
# ---------------------------------------------------------------------------

def model_snapshot(
    datasources: List[Dict[str, Any]],
    model_tables: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compact mapping {ds_name: {ds_caption, tables: {table: [columns]}}}.

    Drops everything that isn't load-bearing for resolution: data types,
    DAX expressions, hidden flags, source columns, relationships. Just
    names. Claude only needs to answer "which (table, column) is this
    field referring to?"; types and aliases don't change that.
    """
    out: Dict[str, Any] = {}
    for ds in datasources:
        ds_name = ds.get("name", "")
        if not ds_name:
            continue
        tables: Dict[str, List[str]] = {}
        for t in model_tables:
            if t.get("datasource") != ds_name:
                continue
            tname = t.get("name", "")
            if not tname:
                continue
            cols = sorted({c.get("name", "")
                           for c in (t.get("columns") or [])
                           if c.get("name")})
            measures = sorted({m.get("name", "")
                               for m in (t.get("measures") or [])
                               if m.get("name")})
            tables[tname] = list(cols) + [f"{m} (measure)" for m in measures]
        if tables:
            out[ds_name] = {
                "caption": ds.get("caption", ""),
                "tables":  tables,
            }
    return out


def render_snapshot_for_system(snapshot: Dict[str, Any]) -> str:
    """Pretty-print the snapshot as JSON for the system prompt. Stable
    key order so prompt caching survives across runs."""
    return json.dumps(snapshot, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Per-warning context (small, sent in the user turn)
# ---------------------------------------------------------------------------

def warning_context(
    warning: Dict[str, Any],
    worksheets: List[Dict[str, Any]],
    snapshot: Dict[str, Any],
    sibling_limit: int = 6,
    datasources: Optional[List[Dict[str, Any]]] = None,
    calc_index: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Build the JSON object we hand Claude for one warning.

    Returns None when the datasource isn't in the snapshot — the
    converter logged the warning but the model has no candidates, and a
    Claude call won't help.

    `datasources` (parser-level dicts) is optional but very valuable:
    Tableau calc-fields appear in warnings as their internal IDs
    (`Calculation_<long_id>`) while the model holds them under their
    user-authored caption ('Sort and Order By Calculation'). The
    Tableau-name → caption bridge lets Claude pick the right measure
    when those IDs come through.
    """
    ds_name = warning.get("ds", "")
    field   = warning.get("field", "")
    if not ds_name or not field:
        return None
    if ds_name not in snapshot:
        return None

    # Tableau-side caption / role lookup. Cheap (linear scan) but each
    # warning only does this once and the column lists are O(100s).
    caption: Optional[str] = None
    role: Optional[str]    = None
    datatype: Optional[str] = None
    if datasources:
        for ds in datasources:
            if ds.get("name") != ds_name:
                continue
            for col in ds.get("columns") or []:
                if col.get("name") == field or col.get("id") == field:
                    caption  = col.get("caption")
                    role     = col.get("role")
                    datatype = col.get("dataType") or col.get("datatype")
                    break
            break

    # Fallback for worksheet-local calc fields — the parser doesn't
    # surface them, but calc_index pulled them straight from the twb
    # XML. This is what unlocks Calculation_<id> warnings whose
    # caption is just an alias for an existing model column.
    if calc_index and not caption:
        info = calc_index.get(field)
        if info:
            caption  = info.get("caption")
            role     = info.get("role")
            datatype = info.get("datatype")
            formula  = info.get("formula")
        else:
            formula = None
    else:
        formula = None

    # Find a worksheet that references this field — its wsColumns and
    # sibling fields are the strongest disambiguation hint we have.
    chosen_ws: Optional[Dict[str, Any]] = None
    for ws in worksheets:
        if ws.get("datasourceRef") != ds_name:
            continue
        if _ws_uses_field(ws, field):
            chosen_ws = ws
            break
    if chosen_ws is None:
        # Fall back to any worksheet on the same datasource.
        for ws in worksheets:
            if ws.get("datasourceRef") == ds_name:
                chosen_ws = ws
                break

    siblings: List[Dict[str, str]] = []
    ws_columns_hint: Optional[str] = None
    if chosen_ws is not None:
        ws_columns = chosen_ws.get("wsColumns") or {}
        # Direct lookup: the parser may have a (Object!Suffix) raw form
        # registered for this field's canonical key.
        from .canonical import canonical_name
        canon = canonical_name(field)
        ws_columns_hint = ws_columns.get(canon) or ws_columns.get(field)

        seen_field: set = set()
        for f in _iter_ws_fields(chosen_ws):
            fname = (f.get("field") or "").strip()
            if not fname or fname == field or fname in seen_field:
                continue
            seen_field.add(fname)
            entry: Dict[str, str] = {"field": fname}
            if f.get("agg"):
                entry["agg"] = f.get("agg")
            siblings.append(entry)
            if len(siblings) >= sibling_limit:
                break

    out: Dict[str, Any] = {
        "field":      field,
        "kind":       warning.get("kind", "resolve"),
        "datasource": ds_name,
        # Tableau's disambiguator (e.g. 'Region (Dim!HCO)') when present
        # — by far the strongest signal for picking the right table.
        "wsColumnsRaw": ws_columns_hint,
        # Worksheet that owns the field, if we found one. Helps Claude
        # spot context like "this is on a map worksheet about HCPs".
        "worksheet":  (chosen_ws or {}).get("name"),
        "siblings":   siblings,
    }
    # Add Tableau-side metadata when present — these are the highest-
    # signal hints for ID-style references like Calculation_xxx.
    if caption:
        out["caption"] = caption
    if role:
        out["role"] = role
    if datatype:
        out["dataType"] = datatype
    if formula:
        # Worksheet-local calcs whose formula is e.g. '[REGION]' or
        # 'RIGHT([Current Sprint],2)' tell Claude exactly which column
        # the calc aliases. Limited to 200 chars by build_calc_index.
        out["formula"] = formula
    return out


def _ws_uses_field(ws: Dict[str, Any], field: str) -> bool:
    for f in _iter_ws_fields(ws):
        if (f.get("field") or "").strip() == field:
            return True
    return False


def _iter_ws_fields(ws: Dict[str, Any]):
    for k in ("rowFields", "colFields", "detailFields", "tooltipFields"):
        for f in ws.get(k) or []:
            yield f
    for k in ("colorField", "sizeField", "labelField"):
        f = ws.get(k) or {}
        if f.get("field"):
            yield f
    for f in ws.get("filters") or []:
        if f.get("field"):
            yield f
