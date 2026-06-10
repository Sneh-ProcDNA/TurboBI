"""Page navigator visual.

PBI's ``pageNavigator`` renders all report pages as clickable tabs. We
emit one per dashboard that has Tableau ``goto-sheet`` buttons, with
styling carried over from the .twb's ``<button-visual-state>`` blocks.

Public API:
    * ``collect_canonical_nav_styles(dashboards)`` — choose which
      dashboard's nav button styling becomes the canonical source for
      every page's navigator. Returns the style list (one entry per
      button) so callers can pass it straight to ``build_page_navigator``.
    * ``build_page_navigator(...)`` — return the visual JSON dict.

Both functions are pure (no ReportBuilder coupling). The dispatcher
in ``report.py`` is responsible for deciding when to emit a navigator
at all and for the geometry (collapsed bounding box of the source
button zones).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import SCHEMA
from ..utils import hex_id
from .helpers import (
    color_expr,
    expr_lit,
    normalize_font_size,
    safe_font_family,
)


def collect_canonical_nav_styles(
    dashboards: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return the nav-button styles from the first dashboard that has
    any ``goto-sheet`` zones.

    The first dashboard's first button becomes PBI's ``selected``
    state and the first dashboard's second button becomes ``default``.
    Every page in the report then renders its pageNavigator with
    these styles, so the navigator's font, colors, and fill are
    identical across pages regardless of which dashboard the page
    was generated from.

    Returns an empty list when no dashboard has navigation buttons —
    callers should fall back to per-dashboard styling (or skip the
    navigator entirely) in that case.
    """
    for db in dashboards:
        nav_zones = [
            z for z in db.get("zones", [])
            if z.get("buttonAction") == "goto-sheet"
        ]
        if nav_zones:
            return [z.get("buttonStyle") or {} for z in nav_zones]
    return []


def build_page_navigator(
    x: int, y: int, w: int, h: int, z: int, zid: str,
    styles: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build a Power BI pageNavigator visual.

    The pageNavigator shows all report pages as clickable tabs/buttons,
    matching Tableau's goto-sheet navigation buttons.

    ``styles`` accepts either:
      * a list of per-button style dicts (preferred). The first entry
        populates the ``selected`` selector (active-page button); the
        second entry — or the first if only one exists — populates the
        ``default`` selector (inactive buttons). Properties common to
        all buttons (font family, font size, bold/italic/underline)
        are emitted under the no-selector entry, so PBI applies the
        same typography uniformly across every button on every page.
      * a single style dict (legacy call shape) — treated as a
        one-element list.

    The per-state selectors apply consistently across all pages —
    whichever page is active shows the ``selected`` styling and the
    rest show ``default``, with no page-by-page variation.

    Font family always resolves to a PBI-shipped font; unknown
    families (Tableau's bundled "Tableau Bold", "Tableau Medium",
    etc.) fall back to Arial via ``safe_font_family``. This keeps
    button labels legible on machines that do not have Tableau's
    custom fonts installed.
    """
    # Normalize input: list of styles, with empty dict fallbacks.
    if styles is None:
        style_list: List[Dict[str, Any]] = []
    elif isinstance(styles, dict):
        style_list = [styles]
    else:
        style_list = [s or {} for s in styles]

    selected_style = style_list[0] if len(style_list) >= 1 else {}
    default_style = (
        style_list[1] if len(style_list) >= 2 else selected_style
    )

    objects: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Text bag — font family / size / weight / italic / underline are
    # the same across all buttons and pages, so they live on the
    # no-selector entry. fontColor differs per state, so it's emitted
    # under the `selected` and `default` selectors — PBI applies the
    # same selector colors on every page.
    # fontFamily always emits — Arial when the Tableau font isn't
    # one PBI ships with.
    # ------------------------------------------------------------------
    text_entries: List[Dict[str, Any]] = []
    family = safe_font_family(selected_style.get("fontFamily")) or "Arial"
    base_props: Dict[str, Any] = {
        "fontFamily": expr_lit(f"'{family}'"),
    }
    if selected_style.get("fontSize"):
        size_lit = normalize_font_size(selected_style["fontSize"])
        if size_lit:
            base_props["fontSize"] = expr_lit(size_lit)
    if str(selected_style.get("fontWeight", "")).lower() == "bold":
        base_props["bold"] = expr_lit("true")
    if (selected_style.get("italic")
            or str(selected_style.get("fontStyle", "")).lower() == "italic"):
        base_props["italic"] = expr_lit("true")
    if selected_style.get("underline"):
        base_props["underline"] = expr_lit("true")
    text_entries.append({"properties": base_props})

    if selected_style.get("fontColor"):
        text_entries.append({
            "properties": {
                "fontColor": color_expr(selected_style["fontColor"]),
            },
            "selector": {"id": "selected"},
        })
    if default_style.get("fontColor"):
        text_entries.append({
            "properties": {
                "fontColor": color_expr(default_style["fontColor"]),
            },
            "selector": {"id": "default"},
        })
    objects["text"] = text_entries

    # ------------------------------------------------------------------
    # Fill bag — per-button background. Selected/default selectors
    # take their colors from the first / second nav button so the
    # active page visually matches its source Tableau button, applied
    # consistently across all pages.
    # ------------------------------------------------------------------
    fill_entries: List[Dict[str, Any]] = []
    if selected_style.get("backgroundColor"):
        fill_entries.append({
            "properties": {
                "show":      expr_lit("true"),
                "fillColor": color_expr(selected_style["backgroundColor"]),
            },
            "selector": {"id": "selected"},
        })
    if default_style.get("backgroundColor"):
        fill_entries.append({
            "properties": {
                "show":      expr_lit("true"),
                "fillColor": color_expr(default_style["backgroundColor"]),
            },
            "selector": {"id": "default"},
        })
    if fill_entries:
        objects["fill"] = fill_entries

    visual_obj: Dict[str, Any] = {
        "visualType":              "pageNavigator",
        "drillFilterOtherVisuals": True,
    }
    if objects:
        visual_obj["objects"] = objects

    return {
        "$schema": SCHEMA["visual"],
        "name":    hex_id("visual-pagenav", zid or "", str(x), str(y)),
        "position": {
            "x": x, "y": y, "z": z,
            "height": h, "width": w,
            "tabOrder": z,
        },
        "visual":       visual_obj,
        "filterConfig": {"filters": []},
    }
