"""Deterministic resolver for trivial calc-field aliases.

A calc field whose formula is just `[COLUMN]` or `[COLUMN (TABLE)]` is a
pure rename — there's no ambiguity to send to an LLM. This resolver
handles those for zero tokens, leaving only the genuinely uncertain
ones for Claude.

Patterns we crack:

    [REGION]                              -> column 'REGION' anywhere
    [REGION (DIM_REGION)]                 -> column 'REGION' in table whose
                                             name contains 'DIM_REGION'
    //comment\\n[REGION]                  -> same as above (Tableau's
                                             default formula prefix)
    [X (Final_Plc_Deployment_Tracker)]    -> column 'X' in
                                             FINAL_PLC_DEPLOYMENT_TRACKER

Anything more complex (arithmetic, IF/THEN, conditional, function calls)
is left for the LLM.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple


# A formula that is JUST a single column reference, possibly preceded
# by Tableau's '// comment' lines and whitespace.
_SIMPLE_REF_RE = re.compile(
    r"^\s*(?://[^\r\n]*[\r\n]+\s*)*"          # optional // comment lines
    r"\[(?P<inner>[^\[\]]+)\]"                  # the [token]
    r"\s*$"
)

# Inside the brackets, a "(Table)" disambiguator at the end.
_TABLE_HINT_RE = re.compile(r"^(?P<col>.+?)\s*\((?P<tbl>[^()]+)\)\s*$")


# Tableau emits these tokens INSIDE the suffix to mean 'this column is a
# derived object', not 'this column lives in a table named X'. They look
# like table hints to the regex but should be ignored as such.
_NON_TABLE_SUFFIXES = {"group", "groups", "set", "sets", "bin", "bins",
                       "parameter", "parameters", "calculation"}


def resolve_via_formula(
    field: str,
    formula: str,
    ds_name: str,
    snapshot: Dict[str, Any],
    prefer_table: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """Try to map a calc-field's formula to (table, column) via pattern.

    `prefer_table` is the worksheet's primary table (as inferred by the
    report builder's `_primary_table_for_ws`). When the formula is a
    no-suffix reference like `[REGION]` and REGION exists in multiple
    tables, the resolver prefers the worksheet's primary table over
    arbitrary insertion order. This is exactly the failure mode the
    hint sidecars revealed: TEST_NAME exists in two tables and the
    resolver was picking the bigger one when the worksheet was about
    the smaller one.

    Returns None when the formula isn't a trivial alias OR when the
    column it points at can't be found in the model snapshot for the
    given datasource. Returning None means "send this one to the LLM".
    """
    if not formula or not snapshot or ds_name not in snapshot:
        return None
    m = _SIMPLE_REF_RE.match(formula)
    if not m:
        return None
    inner = m.group("inner").strip()

    table_hint: Optional[str] = None
    col_token: str = inner
    h = _TABLE_HINT_RE.match(inner)
    if h:
        col_token  = h.group("col").strip()
        suffix     = h.group("tbl").strip()
        # Drop suffixes that are Tableau-internal markers, not table
        # names. '[Region (group)]' means 'the group named Region',
        # which is a derived field whose source column we still want
        # to match by name alone.
        if suffix.lower() not in _NON_TABLE_SUFFIXES:
            table_hint = suffix

    tables = snapshot[ds_name].get("tables", {}) or {}

    # When a table hint is present, restrict the search to tables whose
    # name contains the hint (case-insensitive). Tableau capitalises
    # tokens differently from the model name, so substring match is
    # more forgiving than equality.
    if table_hint:
        hint_l = table_hint.lower()
        candidate_tables = [t for t in tables
                            if hint_l in t.lower() or t.lower() in hint_l]
        if not candidate_tables:
            candidate_tables = list(tables.keys())
    else:
        candidate_tables = list(tables.keys())

    # When several candidate tables remain (no hint, or hint matched
    # multiple) and the worksheet declared a primary table, sort the
    # candidates so the primary table is checked first. This biases
    # the per-pass scans below toward the table the worksheet is
    # actually about.
    if prefer_table and prefer_table in candidate_tables and len(candidate_tables) > 1:
        candidate_tables = (
            [prefer_table] +
            [t for t in candidate_tables if t != prefer_table]
        )

    # Prefer an exact column-name match within candidate tables, then
    # case-insensitive, then with underscores/spaces normalized.
    target = col_token
    target_norm = _norm(target)

    # Pass 1: exact
    for tbl in candidate_tables:
        cols = tables[tbl]
        if target in cols:
            return (tbl, target)

    # Pass 2: case-insensitive
    target_l = target.lower()
    for tbl in candidate_tables:
        for col in tables[tbl]:
            if col.lower() == target_l:
                return (tbl, col)

    # Pass 3: ignore underscores/spaces/parens
    for tbl in candidate_tables:
        for col in tables[tbl]:
            if _norm(col) == target_norm:
                return (tbl, col)

    return None


def _norm(s: str) -> str:
    """Lowercase, drop spaces / underscores / parens — used for the
    most permissive match attempt only."""
    return re.sub(r"[\s_()]+", "", s or "").lower()


def resolve_all_via_formula(
    warnings: list,
    snapshot: Dict[str, Any],
    calc_index: Dict[str, Dict[str, Any]],
    worksheets: Optional[list] = None,
) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """Walk every field-resolution warning and resolve the trivial ones.

    `worksheets` (the parser's IR list) is optional but valuable: when
    provided, we compute each warning's "primary table" using the same
    voting heuristic the report builder uses, then pass it as a tie-
    breaker to `resolve_via_formula`. This is the fix for the
    multi-table-same-column case the user's hint sidecars exposed:
    formulas like `[TEST_NAME]` (no suffix) bound to the wrong table
    when TEST_NAME existed on more than one.

    Returns the same {(ds, field): (table, column)} shape the LLM
    resolver returns, so the orchestrator can merge both results.
    """
    out: Dict[Tuple[str, str], Tuple[str, str]] = {}
    primary_by_ws = _primary_table_index(snapshot, worksheets or [])

    for w in warnings:
        if w.get("kind") not in ("resolve", "filter"):
            continue
        field = w.get("field", "")
        ds    = w.get("ds", "")
        info  = calc_index.get(field) or {}
        formula = info.get("formula") or ""
        if not formula:
            continue

        # First worksheet on the same datasource that uses this field
        # wins — sufficient because Tableau scopes calc-field
        # references to a single datasource anyway.
        prefer_table: Optional[str] = None
        if worksheets:
            for ws in worksheets:
                if ws.get("datasourceRef") != ds:
                    continue
                if not _ws_uses_field(ws, field):
                    continue
                prefer_table = primary_by_ws.get(ws.get("name", ""))
                break

        loc = resolve_via_formula(field, formula, ds, snapshot,
                                  prefer_table=prefer_table)
        if loc:
            out[(ds, field)] = loc
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ws_uses_field(ws: Dict[str, Any], field: str) -> bool:
    """True when `ws` references `field` on any shelf or encoding."""
    for k in ("rowFields", "colFields", "detailFields", "tooltipFields"):
        for f in ws.get(k) or []:
            if (f.get("field") or "").strip() == field:
                return True
    for k in ("colorField", "sizeField", "labelField"):
        f = ws.get(k) or {}
        if (f.get("field") or "").strip() == field:
            return True
    for f in ws.get("filters") or []:
        if (f.get("field") or "").strip() == field:
            return True
    return field in (ws.get("wsColumns") or {})


def _primary_table_index(
    snapshot: Dict[str, Any],
    worksheets: list,
) -> Dict[str, str]:
    """Compute {worksheet_name: primary_table} via a lightweight
    re-implementation of the report builder's voting algorithm.

    We can't import ReportBuilder here without dragging in the model
    dependency, so this operates purely on the parser-level worksheet
    dicts plus the snapshot's table->columns map. Suffix matches grant
    +5 votes; columns that exist in exactly one table grant +1.
    """
    out: Dict[str, str] = {}
    suffix_re = re.compile(r"\(\s*[^) !]+!\s*([^)]+)\s*\)")

    for ws in worksheets:
        name = ws.get("name", "")
        if not name:
            continue
        ds = ws.get("datasourceRef", "")
        if not ds or ds not in snapshot:
            continue
        tables = snapshot[ds].get("tables", {}) or {}
        if not tables:
            continue

        votes: Dict[str, int] = {}
        def _vote(tbl: str, w: int) -> None:
            if tbl:
                votes[tbl] = votes.get(tbl, 0) + w

        names: list = []
        for k in ("rowFields", "colFields", "detailFields", "tooltipFields"):
            names.extend((f.get("field") or "") for f in (ws.get(k) or []))
        for k in ("colorField", "sizeField", "labelField"):
            f = ws.get(k) or {}
            if f.get("field"):
                names.append(f["field"])
        for f in ws.get("filters") or []:
            if f.get("field"):
                names.append(f["field"])
        for raw in (ws.get("wsColumns") or {}).values():
            names.append(raw)

        for fname in names:
            if not fname:
                continue
            m = suffix_re.search(fname)
            if m:
                suffix = m.group(1).strip().lower()
                for tbl in tables:
                    if suffix in tbl.lower():
                        _vote(tbl, 5)
                        break
                continue
            owners = [t for t, cols in tables.items()
                      if fname in cols
                      or _norm(fname) in (_norm(c) for c in cols)]
            if len(owners) == 1:
                _vote(owners[0], 1)

        if votes:
            out[name] = max(votes.items(), key=lambda kv: kv[1])[0]
    return out
