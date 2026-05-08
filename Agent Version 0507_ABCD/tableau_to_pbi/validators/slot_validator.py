"""Per-visual-type slot validators.

Each validator handles ONE PBI visual type that has known "won't render
without slot X" requirements. A validator's signature:

    validate(visual_type, ws, projections, ds_name, add_proj)

  ws         - parsed worksheet dict
  projections- the dict the report builder is about to attach to the
               visual JSON (mutated in place)
  ds_name    - the worksheet's resolved datasource name
  add_proj   - bound method ReportBuilder._add_proj. Validators use
               this to add a missing field (it goes through the model
               resolver and respects the same dropping rules as the
               normal projection path).

The dispatcher `validate_slots` runs the right validator for the visual
type. Anything not listed is a no-op.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List


# Common geographic tokens used to recognise a Location-eligible field
# from a worksheet's available shelves. Conservative on purpose — this
# fires only as a last resort, AFTER the normal slot mapping ran.
_GEO_TOKENS = (
    "country", "state", "province", "region",
    "city", "postcode", "zipcode", "zip",
    "location",
)


def _looks_geographic(field_name: str) -> bool:
    n = (field_name or "").lower()
    return any(tok in n for tok in _GEO_TOKENS)


def _validate_filled_map(
    ws: Dict[str, Any],
    projections: Dict[str, Any],
    ds_name: str,
    add_proj: Callable[..., None],
) -> List[str]:
    """filledMap is the most common 'looks broken' case.

    PBI requires the `Location` slot. When Tableau's worksheet emitted
    the country/region field on Details (because it was on the Detail
    shelf in twb) but not on Rows/Cols, the report builder leaves
    Location empty.

    Recovery order:
      1. Detail fields whose name looks geographic.
      2. Color encoding when it's a categorical (not a measure).
      3. Tooltip fields with a geographic name.
    A field promoted to Location stays in its original slot too — PBI
    is happy with the same field in two slots, and dropping it from
    Details could break tooltip behaviour the user authored.
    """
    notes: List[str] = []
    if "Location" in projections and projections["Location"].get("projections"):
        return notes  # Already populated.

    # 1) Look in detailFields.
    for f in ws.get("detailFields") or []:
        fname = (f.get("field") or "").strip()
        if fname and _looks_geographic(fname):
            add_proj(projections, "Location", f, ds_name)
            notes.append(
                f"filledMap.Location backfilled from Details: {fname}"
            )
            return notes

    # 2) Look at color encoding.
    color = ws.get("colorField") or {}
    if color.get("field") and _looks_geographic(color["field"]):
        add_proj(projections, "Location", color, ds_name)
        notes.append(
            f"filledMap.Location backfilled from Color: {color['field']}"
        )
        return notes

    # 3) Look in tooltip fields.
    for f in ws.get("tooltipFields") or []:
        fname = (f.get("field") or "").strip()
        if fname and _looks_geographic(fname):
            add_proj(projections, "Location", f, ds_name)
            notes.append(
                f"filledMap.Location backfilled from Tooltips: {fname}"
            )
            return notes

    return notes


def _validate_pie_or_donut(
    ws: Dict[str, Any],
    projections: Dict[str, Any],
    ds_name: str,
    add_proj: Callable[..., None],
) -> List[str]:
    """Pie / donut needs both a Category dimension and a value measure.
    A pie with only a value renders as one big circle (the symptom in
    Netflix's 'Movies and TV Shows Distribution'). When the category is
    sitting on the Color encoding (Tableau's classic pie-by-color
    pattern) but the report builder didn't promote it because rows+cols
    were empty, lift it into Category.
    """
    notes: List[str] = []
    # Category present? OK.
    if "Category" in projections and projections["Category"].get("projections"):
        return notes

    color = ws.get("colorField") or {}
    if not color.get("field"):
        return notes
    # Color encoding is categorical when its agg is empty/none. Measures
    # never make sense in the pie's Category slot.
    agg = (color.get("agg") or "").lower()
    if agg in ("", "none"):
        add_proj(projections, "Category", color, ds_name)
        notes.append(
            f"pieChart.Category backfilled from Color: {color['field']}"
        )
    return notes


# Dispatch table — visual_type -> validator function.
_VALIDATORS = {
    "filledMap":   _validate_filled_map,
    "shapeMap":    _validate_filled_map,
    "pieChart":    _validate_pie_or_donut,
    "donutChart":  _validate_pie_or_donut,
}


def validate_slots(
    visual_type: str,
    ws: Dict[str, Any],
    projections: Dict[str, Any],
    ds_name: str,
    add_proj: Callable[..., None],
) -> List[str]:
    """Public entry point — run the validator for `visual_type`, if any."""
    fn = _VALIDATORS.get(visual_type)
    if fn is None:
        return []
    return fn(ws, projections, ds_name, add_proj)
