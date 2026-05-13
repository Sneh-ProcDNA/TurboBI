"""Decide which Power BI visual type a worksheet should become.

Selection sources, in priority order:

    1. Geo  override - lat/lon or location fields beat anything else.
    2. Direct  mark  - explicit Tableau mark like 'Bar' / 'Line' / 'Pie'.
    3. Auto   rules  - mark='Automatic' is the common case in real
                       workbooks; we look at the shelf shape (how many
                       measures vs dimensions, date axis present, color
                       encoding present, etc.) to pick something
                       appropriate.
    4. Fallback      - tableEx, set in visual_rules.json.

All rules live in visual_rules.json so the user can tweak behavior
without touching Python.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import AGG_TABLE


_RULES_PATH = Path(__file__).parent / "visual_rules.json"

# Aggregation tokens that turn a field into a measure.
_MEASURE_AGGS = {agg for agg, info in AGG_TABLE.items() if info["is_measure"]}

# "Continuous" tokens that indicate a field acts as a continuous (measure-
# like) axis in Tableau — used for bar/column orientation detection. Bar
# orientation in Tableau follows the continuous axis: continuous on COLS
# = horizontal bars (PBI barChart), continuous on ROWS = vertical bars
# (PBI columnChart). Includes:
#   - All measure aggregations (sum, avg, cnt, cntd, min, max, ...)
#   - ``usr:`` — user-defined calc field reference (almost always
#     numeric in practice; the calc field's DAX result is a measure)
#   - ``attr:`` / table-calc prefixes — produce continuous values
_CONTINUOUS_AGGS = _MEASURE_AGGS | {
    "usr", "attr",
    "cum", "pcto", "pct", "dif", "pdif", "rnk",
    "wsum", "wavg", "wmin", "wmax",
    "fval", "fhi", "flo",
}

# Date-part aggregation tokens (yr/qr/mn/...). Tableau uses these on
# date columns dragged onto a shelf — even if the field doesn't have
# 'date' in its name, the agg token tells us it's behaving as a date.
# Includes both the date-PART tokens (yr/qr/mn — which integer year etc.)
# and the date-TRUNC tokens (ty/tqr/tmn/tmd/td — which return a real
# date value bucketed to year/quarter/month/etc.). `md` is Tableau's
# month-day continuous bucket. Without the trunc tokens, date-axis line
# charts whose Tableau shelf has e.g. `tmn:Date of Visit` would miss
# the date-axis rule and fall through to a generic table.
_DATE_AGGS = {
    "yr", "qr", "mn", "wk", "dy", "hr", "mi", "sd",
    "ty", "tqr", "tmn", "tmd", "td", "md",
}


# ---------------------------------------------------------------------------
# Rule loading (cached)
# ---------------------------------------------------------------------------

_rules_cache: Optional[Dict[str, Any]] = None


def _rules() -> Dict[str, Any]:
    global _rules_cache
    if _rules_cache is None:
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            _rules_cache = json.load(f)
    return _rules_cache


# ---------------------------------------------------------------------------
# Closest-match helper for unknown marks
# ---------------------------------------------------------------------------

_MARK_TO_GROUP: Dict[str, str] = {
    "barChart":       "bar",
    "columnChart":    "column",
    "lineChart":      "line",
    "areaChart":      "area",
    "scatterChart":   "scatter",
    "pieChart":       "pie",
    "donutChart":     "pie",
    "map":            "map",
    "filledMap":      "map",
    "treemap":        "treemap",
    "funnel":         "funnel",
    "waterfallChart": "waterfallChart",
    "tableEx":        "tableEx",
    "pivotTable":     "pivotTable",
    "card":           "card",
    "text":           "card",
    "multiRowCard":   "card",
    "lineClusteredColumnComboChart": "line",
}


def _closest_visual(mark: str, cfg: Dict[str, Any]) -> Optional[str]:
    """Find the closest supported visual for an unknown Tableau mark.

    Scoring:
        +3 per keyword that appears as a substring of the mark
        +1 per keyword that appears as a substring of any alias in mark_to_visual keys
    Returns the visual type of the best-matching group, or None.
    """
    keywords_cfg = cfg.get("keywords", {})
    default_group = cfg.get("default_group", "tableEx")
    if not keywords_cfg:
        return None

    mark_low = mark.lower()
    best_group: Optional[str] = None
    best_score = 0

    for group, keywords in keywords_cfg.items():
        score = 0
        for kw in keywords:
            kw_low = kw.lower()
            if kw_low in mark_low:
                score += 3
        if score > best_score:
            best_score = score
            best_group = group

    if not best_group or best_score == 0:
        best_group = default_group

    # Map the keyword group back to a concrete visual type.
    # First try the direct mapping from mark_to_visual using the group name itself.
    rules = _rules()
    direct = rules.get("mark_to_visual", {})
    if best_group in direct:
        return direct[best_group]

    # Otherwise map via _MARK_TO_GROUP.
    concrete = _MARK_TO_GROUP.get(best_group)
    if concrete and concrete in direct:
        return direct[concrete]

    # Absolute fallback.
    return rules.get("fallback_visual", "tableEx")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def pick_visual_type(ws: Dict[str, Any]) -> str:
    """Return the Power BI visualType for a parsed worksheet dict."""

    rules = _rules()

    # 1) Geo override — Tableau lat/lon flags beat any explicit mark.
    if ws.get("isGeo"):
        mark = (ws.get("markClass") or "").lower()
        if mark in ("polygon", "filled-map", "multipolygon"):
            return rules["geo_visuals"]["filled"]
        return rules["geo_visuals"]["scatter"]

    # 2) Direct mark mapping — we keep "automatic" out of this map so
    # it falls through to the auto_rules below.
    mark      = (ws.get("markClass") or "").lower()
    direct    = rules["mark_to_visual"]
    mapped = direct.get(mark) if mark and mark != "automatic" else None
    if mapped:
        # cardVisual is single-value; when the worksheet has 2+ text
        # encodings (e.g. UseCase2's Test KPI: 'Total Territories' +
        # 'Sum of Patients'), prefer multiRowCard so every value lands
        # as its own row. Same upgrade path applies to the auto-rule
        # cardVisual selection below.
        if mapped == "cardVisual" and len(ws.get("labelFields") or []) >= 2:
            return "multiRowCard"

        # Bar orientation: Tableau ``Bar`` mark renders horizontally
        # when the measure is on COLS and the dim on ROWS, vertically
        # when it's the other way around. PBI distinguishes via
        # visualType: ``barChart`` (horizontal) vs ``columnChart``
        # (vertical). The default ``Bar -> barChart`` mapping is wrong
        # for the vertical-bar case, so we override based on shelf
        # composition: measure on rows + dim on cols → columnChart.
        row_fields = ws.get("rowFields") or []
        col_fields = ws.get("colFields") or []
        # Continuous-axis detection: includes user calcs (``usr:``) and
        # table calcs since those produce continuous axes too. Without
        # them, the override misses the common case where a measure is
        # actually a calc-field reference (``usr:Calculation_xxx``).
        row_has_measure = any(
            (f.get("agg") or "").lower() in _CONTINUOUS_AGGS
            for f in row_fields
        )
        col_has_measure = any(
            (f.get("agg") or "").lower() in _CONTINUOUS_AGGS
            for f in col_fields
        )

        if mapped == "barChart":
            # Bar orientation in Tableau follows the continuous axis:
            #   * Continuous on COLS (measure on X) → horizontal bars
            #     (PBI ``barChart``).
            #   * Continuous on ROWS (measure on Y) → vertical bars
            #     (PBI ``columnChart``).
            # Default ``Bar`` mapping is ``barChart``; we override when
            # the measure is actually on the rows shelf.
            if row_has_measure and not col_has_measure:
                return "columnChart"
            # Otherwise (measure on cols, dim on rows): keep horizontal.

        if mapped == "scatterChart":
            # Scatter requires X and Y to both be numeric measures.
            # Tableau's ``Circle`` mark with a dimension on one axis
            # produces a categorical dot plot — PBI's scatterChart
            # rejects that and shows nothing. Route those to a
            # column/bar chart based on which shelf has the measure.
            measure_total = sum(1 for f in row_fields + col_fields
                                if (f.get("agg") or "").lower() in _CONTINUOUS_AGGS)
            dim_total = sum(1 for f in row_fields + col_fields
                            if (f.get("agg") or "").lower() not in _CONTINUOUS_AGGS
                            and f.get("field"))
            if measure_total < 2 and dim_total >= 1:
                if row_has_measure and not col_has_measure:
                    return "columnChart"
                if col_has_measure and not row_has_measure:
                    return "barChart"
                # No measure at all — fall back to a tableEx for the dim
                # list rather than emit a broken scatter.
                if measure_total == 0:
                    return "tableEx"

        return mapped

    # Extra rule: bubble chart (circle mark with size encoding). PBI's
    # native "bubble" is just a scatterChart with a Size well — same
    # visual type as a regular scatter, so this special case is a no-op
    # for the visual type itself. Kept as a hook in case we want to add
    # bubble-specific format-string overrides later.

    # 2b) Closest-match heuristic for unknown marks.
    if mark and mark != "automatic":
        closest = _closest_visual(mark, rules.get("closest_match", {}))
        if closest:
            return closest

    # 3) Auto rules — for mark='Automatic' (or unknown marks). Rules
    # are evaluated in priority order: explicit `priority` field first
    # (lower number = higher priority; defaults to 100), then the
    # original list order as a tiebreaker. This lets observers prepend
    # specific patterns (e.g. KPI variants) without worrying about
    # accidentally shadowing later catch-alls.
    facts = _shelf_facts(ws)
    auto = sorted(
        enumerate(rules["auto_rules"]),
        key=lambda iv: (int(iv[1].get("priority", 100)), iv[0]),
    )
    for _, rule in auto:
        if _matches(rule.get("when", {}), facts):
            picked = rule["visual"]
            # Auto-rule upgrade: 2+ text encodings → multiRowCard.
            if picked == "cardVisual" and len(ws.get("labelFields") or []) >= 2:
                return "multiRowCard"
            return picked

    # 4) Last-resort fallback.
    return rules.get("fallback_visual", "tableEx")


# ---------------------------------------------------------------------------
# Shelf-shape inspection
# ---------------------------------------------------------------------------

def _shelf_facts(ws: Dict[str, Any]) -> Dict[str, Any]:
    """Boil a worksheet down to a tiny dict the rules engine can match."""
    fields: List[Dict[str, Any]] = (
        list(ws.get("rowFields", [])) + list(ws.get("colFields", []))
    )

    measures   = [f for f in fields if (f.get("agg") or "").lower() in _MEASURE_AGGS]
    dimensions = [f for f in fields if (f.get("agg") or "").lower() not in _MEASURE_AGGS]

    has_date = any(_looks_like_date(f) for f in fields)

    # Encoding fields are dicts {field, agg}; treat as set when the
    # field name is non-empty.
    def _set(v: Any) -> bool:
        if isinstance(v, dict): return bool(v.get("field"))
        return bool(v)
    encoding = {
        "color":   _set(ws.get("colorField")),
        "size":    _set(ws.get("sizeField")),
        "label":   _set(ws.get("labelField")),
        "tooltip": bool(ws.get("tooltipFields")),
        "detail":  bool(ws.get("detailFields")),
    }

    return {
        "measure_count":   len(measures),
        "dimension_count": len(dimensions),
        "has_date":        has_date,
        "encoding":        encoding,
    }


def _looks_like_date(field: Dict[str, Any]) -> bool:
    agg = (field.get("agg") or "").lower()
    if agg in _DATE_AGGS:
        return True
    name = (field.get("field") or "").lower()
    return any(tok in name for tok in (
        "date", "year", "month", "quarter", "week", "day",
    ))


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------

def _matches(when: Dict[str, Any], facts: Dict[str, Any]) -> bool:
    """Apply every predicate in `when` against the worksheet facts.
    Returns True only if all predicates match."""
    for key, expected in when.items():
        if key == "has_date":
            if bool(expected) != bool(facts["has_date"]):
                return False
        elif key == "encoding":
            # Either a single shelf name or a list of shelf names.
            wanted = [expected] if isinstance(expected, str) else list(expected)
            if not all(facts["encoding"].get(s) for s in wanted):
                return False
        elif key in ("measure_count", "dimension_count"):
            if not _count_matches(facts[key], expected):
                return False
        else:
            # Unknown predicate name — ignore it rather than crashing.
            return False
    return True


def _count_matches(actual: int, spec: Any) -> bool:
    """Compare actual integer count against a spec like '>=2', '0', or 1.
    Numbers without an operator mean exact equality."""
    if isinstance(spec, (int, float)):
        return actual == int(spec)
    s = str(spec).strip()
    m = re.match(r"^\s*(==|>=|<=|>|<|!=)?\s*(\d+)\s*$", s)
    if not m:
        return False
    op, n = (m.group(1) or "=="), int(m.group(2))
    if op in ("==", "="): return actual == n
    if op == ">=":        return actual >= n
    if op == "<=":        return actual <= n
    if op == ">":         return actual >  n
    if op == "<":         return actual <  n
    if op == "!=":        return actual != n
    return False
