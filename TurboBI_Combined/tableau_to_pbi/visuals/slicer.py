"""Slicer visual + title formatting helpers.

A "placeholder slicer" is what we emit for every Tableau filter zone
that didn't resolve to a chart visual — a single-column slicer bound
to either a model column or a parameter table. The slicer module owns:

  * ``build_placeholder_slicer`` — the visual JSON construction. Pure
    except for an optional ``project_field`` callback that delegates
    field-binding back to the ReportBuilder (which owns the worksheet-
    aware field resolver).
  * ``slicer_mode_objects`` — Tableau widget mode -> PBI single/multi
    dropdown / list selection.
  * ``title_object`` — visualContainerObjects.title bag, with optional
    background sibling. Shared with chart visuals that want the same
    Tableau-style title styling.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import SCHEMA
from ..utils import hex_id
from .helpers import (
    color_expr,
    expr_lit,
    normalize_font_size,
    safe_font_family,
)


def slicer_mode_objects(mode: str) -> Dict[str, Any]:
    """Translate Tableau filter-widget ``mode`` into PBI slicer settings.

    Tableau widget modes (lowercase, from the ``<zone mode='...'>`` attr).
    These are the actual tokens Tableau emits — verified by surveying
    the ``mode='...'`` attribute frequency across our workbook corpus:

        compact          -> Single Value (Dropdown)        — most common
        checkdropdown    -> Multiple Values (Dropdown)
        dropdown         -> Single Value (Dropdown)         — alternate
        radiolist        -> Single Value (List)             — radio buttons
        vscroll          -> Multiple Values (List)
        checklist        -> Multiple Values (List)
        custom-list      -> Multiple Values (Custom List)
        none             -> default UI (treat as multi list)
        range / fixed / typeinlist / pattern / readonly / datetime
                         -> not a list/dropdown — leave PBI default

    PBI slicer settings used:
        ``objects.data[0].properties.mode``  = ``'Dropdown'`` | ``'Basic'``
            (Basic = list. Dropdown = collapsed dropdown control.)
        ``objects.general[0].properties.singleSelect``  = true | false
            (false = multi-select; true = single-select)

    Returns ``{}`` when the mode doesn't map to a list/dropdown
    control — e.g. a range slider — so PBI Desktop falls back to its
    default.
    """
    m = (mode or "").strip().lower()
    if not m:
        return {}

    # Tokens that mean "single value selection" in Tableau.
    single_modes = {
        "compact",          # most common — Single Value (Dropdown)
        "dropdown",         # alternate single dropdown
        "radiolist",        # Single Value (List)
        # Older / less common forms that may still appear:
        "single-dropdown", "radio",
        "single-list", "single-value-list",
    }
    # Tokens that mean "multiple values" in Tableau.
    multi_modes = {
        "checkdropdown", "multiple-dropdown",
        "checklist", "vscroll",
        "compact-list", "custom-list",
    }
    # Tokens whose UI is a collapsed dropdown control (not a list).
    dropdown_modes = {
        "compact", "dropdown",
        "checkdropdown", "multiple-dropdown",
        "single-dropdown",
    }

    is_single   = m in single_modes
    is_multi    = m in multi_modes
    is_dropdown = m in dropdown_modes
    if not (is_multi or is_single or is_dropdown):
        return {}

    objects: Dict[str, Any] = {}
    general_props: Dict[str, Any] = {}
    # Single wins over multi when both could match (single is the
    # more specific intent — Tableau never marks a checklist as
    # single, so the only ambiguity is when a token is in neither).
    if is_single:
        general_props["singleSelect"] = expr_lit("true")
    elif is_multi:
        general_props["singleSelect"] = expr_lit("false")
    if general_props:
        objects["general"] = [{"properties": general_props}]

    if is_dropdown:
        objects["data"] = [{
            "properties": {"mode": expr_lit("'Dropdown'")},
        }]
    return objects


def title_object(
    text: str,
    style: Optional[Dict[str, Any]] = None,
    enabled: bool = True,
) -> Dict[str, Any]:
    """Build a visualContainerObjects dict carrying the title (and an
    optional sibling background).

    Returns ``{}`` when the title is disabled AND no styling exists
    (the caller can omit visualContainerObjects entirely). When
    disabled with no style we don't even emit a show=false marker —
    the absence of a title object is what hides it in PBI Desktop.
    When the title IS disabled but a style was specified we still skip
    emitting it, because styling a hidden title is meaningless.

    Style keys honored:
        fontSize       int/float/str (e.g. 11 or "11pt")
        fontColor      hex string (e.g. "#FFFFFF")
        fontFamily     family name
        fontWeight     "bold" -> bold=true; anything else ignored
        italic         truthy -> italic=true
        underline      truthy -> underline=true
        textAlign      "left"|"center"|"right"
        backgroundColor hex string (emitted as a sibling 'background'
                        entry on visualContainerObjects, NOT inside
                        title.properties — that's the spot PBI reads)
    """
    if not enabled:
        # Emit an explicit `show: false` rather than dropping the
        # bag entirely. Omitting the title block lets PBI Desktop's
        # default behaviour kick in (which shows the worksheet name
        # as a title); the explicit show=false is what actually
        # hides the title bar in the Format pane.
        return {
            "title": [{
                "properties": {"show": expr_lit("false")},
            }],
        }

    style = style or {}
    safe = (text or "").replace("'", "''")

    properties: Dict[str, Any] = {
        "show": expr_lit("true"),
        "text": expr_lit(f"'{safe}'"),
    }

    size_lit = normalize_font_size(style.get("fontSize"))
    if size_lit:
        properties["fontSize"] = expr_lit(size_lit)

    # Always emit a fontFamily — Arial when Tableau's choice isn't a
    # PBI-shipped font, or when no font was specified. Avoids falling
    # to PBI's invisible-default behavior for unknown fonts.
    if style.get("fontFamily") or "fontFamily" not in style:
        family = safe_font_family(style.get("fontFamily"))
        properties["fontFamily"] = expr_lit(f"'{family}'")

    if style.get("fontColor"):
        properties["fontColor"] = color_expr(style["fontColor"])

    if str(style.get("fontWeight", "")).lower() == "bold":
        properties["bold"] = expr_lit("true")
    if style.get("italic"):
        properties["italic"] = expr_lit("true")
    if style.get("underline"):
        properties["underline"] = expr_lit("true")

    align = style.get("textAlign") or style.get("alignment")
    if align:
        properties["alignment"] = expr_lit(f"'{align}'")

    objects: Dict[str, Any] = {"title": [{"properties": properties}]}

    # PBI puts the visualContainer background as its own sibling entry,
    # not inside the title block. This is also what tames the "white
    # rectangle" rendering quirks we used to get when the field was
    # nested.
    if style.get("backgroundColor"):
        objects["background"] = [{
            "properties": {
                "show":  expr_lit("true"),
                "color": color_expr(style["backgroundColor"]),
            },
        }]

    return objects


# Type alias for the field-projection callback. ReportBuilder owns the
# worksheet-aware field resolver, so the slicer module asks it (via
# this callback) to populate the projections dict for a given field.
# Shape mirrors ReportBuilder._add_proj's signature: it mutates
# ``projections`` in place, adding the field to the named bucket.
ProjectFieldFn = Callable[
    [Dict[str, Any], str, Dict[str, Any], str],
    None,
]


def build_placeholder_slicer(
    label: str,
    x: int, y: int, w: int, h: int, z: int, zid: str,
    *,
    field: Optional[Dict[str, Any]] = None,
    ds_name: str = "",
    title_style: Optional[Dict[str, Any]] = None,
    title_enabled: bool = True,
    widget_mode: str = "",
    param_binding: Optional[Tuple[str, str]] = None,
    project_field: Optional[ProjectFieldFn] = None,
) -> Dict[str, Any]:
    """Build a placeholder slicer visual with optional title styling.

    Args:
        label: The slicer/filter label/title text
        x, y, w, h, z: Position and size parameters
        zid: Zone ID for uniqueness
        field: Optional field dict for filter binding
        ds_name: Datasource name for field resolution
        title_style: Optional style dict for title formatting:
            - fontSize, fontColor, fontWeight, fontFamily
            - backgroundColor, textAlign, verticalAlign
        param_binding: Optional (table, column) for parameter slicers,
            bypassing the worksheet field resolver.
        project_field: Optional callback used to wire a non-parameter
            field into the slicer's Values bucket. ReportBuilder owns
            the resolver and passes its ``_add_proj`` here.

    Layout overrides:
        * Height is forced to 60px — slicer dropdowns / radio lists
          render best in a single-row strip, and Tableau's variable
          filter-widget heights translate inconsistently otherwise.
        * Title is hidden — the field name itself communicates the
          filter purpose, and an extra title row eats vertical space
          users need for the actual control. Background styling (if
          the parser surfaced any) is preserved separately.
    """
    h = 60
    projections: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    if param_binding:
        # Direct (table, col) binding for parameter slicers — skips
        # the worksheet-field resolver because parameter tables don't
        # live under any Tableau datasource.
        tbl, col = param_binding
        projections["Values"] = {"projections": [{
            "field": {
                "Column": {
                    "Expression": {"SourceRef": {"Entity": tbl}},
                    "Property":   col,
                },
            },
            "queryRef":       f"{tbl}.{col}",
            "nativeQueryRef": col,
            "active":         True,
            "displayName":    col,
        }]}
    elif field and ds_name and project_field is not None:
        project_field(projections, "Values", field, ds_name)

    slicer_visual: Dict[str, Any] = {
        "visualType":              "slicer",
        "drillFilterOtherVisuals": True,
    }
    # Title is forced off, but a background carried in title_style
    # should still land on the visual container — pass enabled=False
    # to title_object and merge the background separately.
    container = title_object(label, title_style, enabled=False)
    if title_style and title_style.get("backgroundColor"):
        container = container or {}
        container["background"] = [{
            "properties": {
                "show":  expr_lit("true"),
                "color": color_expr(title_style["backgroundColor"]),
            },
        }]
    if container:
        slicer_visual["visualContainerObjects"] = container

    slicer_objects = slicer_mode_objects(widget_mode)
    if slicer_objects:
        slicer_visual["objects"] = slicer_objects

    v: Dict[str, Any] = {
        "$schema": SCHEMA["visual"],
        "name":    hex_id("visual-slicer", zid or label, str(x), str(y)),
        "position": {
            "x": x, "y": y, "z": z,
            "height": h, "width": w,
            "tabOrder": z,
        },
        "visual":       slicer_visual,
        "filterConfig": {"filters": []},
    }
    if projections:
        v["visual"]["query"] = {"queryState": projections}
    return v
