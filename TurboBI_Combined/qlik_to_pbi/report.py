"""Report builder for the Qlik -> PBIP pipeline.

Each Qlik sheet becomes a PBI page; each cell on the sheet becomes a
PBI visual. Cell bounds are percentages of a 100x100 canvas, which we
project onto PBI's default 1280x720 page.

Visual coverage:
  * auto-chart / linechart / barchart / piechart   -> chart family
  * kpi / sn-kpi                                   -> cardVisual
  * treemap                                        -> treemap
  * table / pivot-table                            -> tableEx / pivotTable
  * filterpane / listbox                           -> slicer (one per dim)
  * text-image                                     -> textbox
  * action-button                                  -> actionButton
  * container / sn-layout-container / sn-tabbed-container
        -> expanded into the child visuals they hold (layout containers
           keep each child's percent position; tabbed containers tile the
           tabs in a grid). Empty/unresolvable -> grey placeholder.

Anything we don't recognise still emits a textbox placeholder so the
page layout survives.
"""

import math
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from ._logging import get_logger
from .config import (
    DEFAULT_PAGE_HEIGHT,
    DEFAULT_PAGE_WIDTH,
    SCHEMA,
    VISUAL_TYPE_MAP,
)
from .dax_translator import translate_qlik_to_dax
from .ir import QlikIR
from .model import SemanticModel
from .pbi_theme import QLIK_HORIZON_12, QLIK_HORIZON_PRIMARY
from .text_eval import eval_static_expression
from .utils import clean_label, hex_id, lineage_tag

_log = get_logger("REPORT")


# ---------------------------------------------------------------------------
# Qlik styling extraction
# ---------------------------------------------------------------------------

# Qlik uses T-shirt sizing for many of its font controls. Map to PBI
# point sizes that roughly preserve the visual hierarchy a user would
# expect. Values calibrated against PBI Desktop's default themes.
_QLIK_FONT_SIZE_PT = {
    "xs": 10, "s": 11, "sm": 12, "m": 13, "md": 13,
    "l": 16, "lg": 16, "xl": 20, "xxl": 24,
}

# Qlik colour records can be either an indexed palette ref, a hex
# string, or an {index, color, alpha} record. We always want the hex.
def _normalize_hex(c: Any) -> Optional[str]:
    """Normalise a hex colour to PBI's expected ``#RRGGBB`` / ``#AARRGGBB``
    form. Expands 3-/4-digit shorthand (``#abc`` -> ``#aabbcc``,
    ``#fabc`` -> ``#ffaabbcc``) and rejects anything that isn't a valid
    hex colour -- PBI renders a malformed colour literal as black /
    default, so it's better to drop it (return None) and keep the theme."""
    if not isinstance(c, str):
        return None
    c = c.strip()
    if not c.startswith("#"):
        return None
    body = c[1:]
    if not re.fullmatch(r"[0-9a-fA-F]+", body):
        return None
    if len(body) in (3, 4):                       # shorthand -> expand
        body = "".join(ch * 2 for ch in body)
    if len(body) in (6, 8):
        return "#" + body
    return None


def _css_color_to_hex(css: Any) -> Optional[str]:
    """Convert a CSS colour token to ``#RRGGBB``.  Handles ``#hex``,
    ``rgb(r,g,b)``, and ``rgba(r,g,b,a)``."""
    if not isinstance(css, str):
        return None
    css = css.strip()
    h = _normalize_hex(css)
    if h:
        return h
    m = re.match(
        r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)",
        css, re.IGNORECASE,
    )
    if m:
        return "#{:02x}{:02x}{:02x}".format(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
        )
    return None


def _qlik_color_to_hex(color: Any) -> Optional[str]:
    """Return a hex colour string ``"#RRGGBB"`` (or with alpha) from a
    Qlik colour dict/string, normalised via ``_normalize_hex``. Returns
    None when the input doesn't carry a resolvable colour."""
    if isinstance(color, str):
        return _normalize_hex(color)
    if not isinstance(color, dict):
        return None
    # Most common: {index, color, alpha}; we prefer the explicit hex.
    inner = color.get("color")
    if isinstance(inner, str):
        h = _normalize_hex(inner)
        if h:
            return h
    # Sometimes nested another level: {color: {color: "#xxx"}}.
    if isinstance(inner, dict):
        return _normalize_hex(inner.get("color"))
    # Index-only record ({index: N} without a "color" field): fall back
    # to the general-palette lookup table.
    idx = color.get("index")
    if isinstance(idx, int):
        return _QLIK_PALETTE.get(idx)
    return None


# ---------------------------------------------------------------------------
# Qlik colour palette tables
# ---------------------------------------------------------------------------

# General / UI palette -- maps the integer ``index`` values that Qlik
# stores in ``{index, color, alpha}`` colour records.  When a record
# carries only an index (no ``color`` field), _qlik_color_to_hex looks
# here.  This is the full 16-entry "ui" palette from the Sense default
# theme (every previously-confirmed index -- 2, 4, 10, 11, 13, 15 --
# matches this table, validating the rest). Index 0 = "none"
# (transparent) is deliberately absent so it resolves to no colour;
# index -1 = custom (always carries ``color``, never needs a lookup).
_QLIK_PALETTE: Dict[int, str] = {
    1:  "#ffffff",  # white
    2:  "#99cfcd",  # soft teal (backgrounds, highlights)
    3:  "#66a9a6",  # teal
    4:  "#c4cfda",  # soft blue-grey (KPI background)
    5:  "#7e97ad",  # slate blue
    6:  "#41555d",  # dark slate -- Qlik's DEFAULT KPI value colour
    7:  "#dfe2e5",  # light grey
    8:  "#b8c5cd",  # grey-blue
    9:  "#7d8a91",  # mid grey-blue
    10: "#f93f17",  # signal red-orange (negative trend indicators)
    11: "#e0bd8d",  # warm tan (highlights)
    12: "#f9ec86",  # pale yellow
    13: "#b0afae",  # medium grey (table header background)
    14: "#56473f",  # dark brown
    15: "#000000",  # black (title / text colour)
}

# Qlik Sense default 12-colour DIMENSION palette (``dimensionScheme: "12"``).
# The palette tables now live in pbi_theme.py (shared with the report-level
# registered theme); this alias keeps the historical module-local name.
_QLIK_DIM_PALETTE_12: List[str] = list(QLIK_HORIZON_12)


def _qlik_font_pt(value: Any) -> Optional[int]:
    """Convert a Qlik fontSize value to a PBI point size. Accepts the
    T-shirt strings ("S", "M", "L") and numeric ints / strings."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n if 6 <= n <= 96 else None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _QLIK_FONT_SIZE_PT:
            return _QLIK_FONT_SIZE_PT[v]
        # Allow "13pt", "13px", "13"
        digits = "".join(ch for ch in v if ch.isdigit())
        if digits:
            n = int(digits)
            if 6 <= n <= 96:
                return n
    return None


def _resolve_bar_combo_type(
    ctype: str, props: Dict[str, Any], default: Optional[str],
) -> Optional[str]:
    """Pick the right PBI chart type for a Qlik bar / combo chart from
    its stacking + orientation.

    Qlik stores:
      * ``barGrouping.grouping`` -> ``"grouped"`` (clustered) | ``"stacked"``
      * ``orientation``          -> ``"vertical"`` (columns) | ``"horizontal"`` (bars)

    PBI mapping (same matrix for ``barchart`` AND ``combochart`` --
    Qlik's combochart presentation in many real apps is just a styled
    bar / column without a line series, so we follow the same
    orientation + grouping rule):

      | grouping | orientation | PBI visualType            |
      |----------|-------------|---------------------------|
      | grouped  | horizontal  | clusteredBarChart         |
      | grouped  | vertical    | clusteredColumnChart      |
      | stacked  | horizontal  | stackedBarChart           |
      | stacked  | vertical    | stackedColumnChart        |

    Mekko maps to the closest stacked form.
    """
    grouping = ""
    bg = props.get("barGrouping")
    if isinstance(bg, dict):
        grouping = (bg.get("grouping") or "").strip().lower()
    elif isinstance(bg, str):
        grouping = bg.strip().lower()
    # ``stacked`` covers Qlik's "stacked"; everything else (grouped,
    # empty) is treated as clustered -- Qlik's default is grouped.
    stacked = grouping == "stacked"
    orientation = (props.get("orientation") or "").strip().lower()
    # Qlik's bar-chart default orientation is vertical (columns); only
    # an explicit "horizontal" yields bars.
    horizontal = orientation == "horizontal"

    if ctype in ("mekko-chart", "sn-mekko-chart"):
        # Marimekko has no native PBI type; a stacked column is the
        # closest single-visual approximation.
        return "stackedColumnChart"
    # barchart and combochart share the same matrix.
    if horizontal:
        return "stackedBarChart" if stacked else "clusteredBarChart"
    return "stackedColumnChart" if stacked else "clusteredColumnChart"


def _resolve_pie_type(props: Dict[str, Any], default: Optional[str]) -> Optional[str]:
    """Pick pie vs donut from Qlik's pie chart presentation.

    Two detection paths:
    * ``donut.showAsDonut: true`` (the explicit toggle).
    * ``components[key=slices].style.innerRadius > 0`` (the visual
      radius setting that produces a donut hole even when the toggle
      is absent or false).
    """
    donut = props.get("donut")
    if isinstance(donut, dict) and bool(donut.get("showAsDonut")):
        return "donutChart"
    for comp in (props.get("components") or []):
        if not isinstance(comp, dict):
            continue
        if (comp.get("key") or "").strip().lower() == "slices":
            inner_r = (comp.get("style") or {}).get("innerRadius")
            if isinstance(inner_r, (int, float)) and float(inner_r) > 0:
                return "donutChart"
    return default


def _has_hypercube_data(props: Dict[str, Any]) -> bool:
    """True when a cell carries a non-empty hypercube -- dimensions or
    measures -- i.e. it has data worth rendering as a table when no PBI
    chart type maps. Map cells keep their data on ``gaLayers`` rather
    than the top-level hypercube, so check those too."""
    dims, meas = _hypercube_counts(props)
    return bool(dims or meas)


def _hypercube_counts(props: Dict[str, Any]) -> Tuple[int, int]:
    """``(dimension_count, measure_count)`` summed across the top-level
    hypercube and any map ``gaLayers`` / ``layers``. Used to tell a
    KPI/card (no dimensions, one-or-more measures) apart from a real
    table (has dimensions)."""
    dims = meas = 0
    hc = props.get("qHyperCubeDef") or {}
    dims += len(hc.get("qDimensions") or [])
    meas += len(hc.get("qMeasures") or [])
    for layer in (props.get("gaLayers") or props.get("layers") or []):
        if isinstance(layer, dict):
            lhc = layer.get("qHyperCubeDef") or {}
            dims += len(lhc.get("qDimensions") or [])
            meas += len(lhc.get("qMeasures") or [])
    return dims, meas


# Qlik cell types whose author EXPLICITLY chose a tabular renderer. For
# these we keep ``tableEx`` even with zero dimensions (respect intent);
# every other cell that falls through to ``tableEx`` is eligible to be
# re-routed to a card when it has no dimensions (see ``_build_visual``).
_EXPLICIT_TABLE_CTYPES = {
    "table", "sn-table", "straight-table", "straighttable",
    "grid-chart", "gridchart", "sn-grid-chart", "sn-org-chart",
}

# Cell types that HOLD other visuals. Instead of a single placeholder, each
# expands into the visuals it contains (see ReportBuilder._build_container):
#   * sn-layout-container : free layout, per-child PERCENT bounds, every child
#     visible at once.
#   * container / sn-tabbed-container : tabbed (one child shown at a time),
#     children referenced by qId; PBI has no tab container so the children are
#     tiled in a grid so none are lost.
_CONTAINER_CTYPES = {"container", "sn-layout-container", "sn-tabbed-container"}

# Guard against a pathological / cyclic container graph blowing the stack.
_MAX_CONTAINER_DEPTH = 4


def _num(value: Any, default: float = 0.0) -> float:
    """Coerce a Qlik bounds value to float, tolerating None / junk."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rebase_child_bounds(
    container: Dict[str, Any], child_pct: Dict[str, Any],
) -> Dict[str, float]:
    """Project a child's PERCENT-OF-CONTAINER rectangle into the sheet's
    percent space, given the container's own percent-of-sheet rectangle.

    A layout-container child stores ``bounds`` as a percentage of the
    container; re-basing it onto the container's sheet rectangle lets the
    existing ``_scale()`` map it onto the page exactly like any other cell."""
    bx, by = _num(container.get("x"), 0.0), _num(container.get("y"), 0.0)
    bw, bh = _num(container.get("width"), 100.0), _num(container.get("height"), 100.0)
    return {
        "x":      bx + (_num(child_pct.get("x"), 0.0) / 100.0) * bw,
        "y":      by + (_num(child_pct.get("y"), 0.0) / 100.0) * bh,
        "width":  (_num(child_pct.get("width"), 100.0) / 100.0) * bw,
        "height": (_num(child_pct.get("height"), 100.0) / 100.0) * bh,
    }


def _resolve_layout_children(
    objects: List[Dict[str, Any]], child_children: List[Dict[str, Any]],
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """``(child_props, percent_bounds)`` pairs for a LAYOUT container.

    ``objects[]`` (on the container props) gives each child's ``childRefId`` +
    ``bounds`` (percent of the container); the child definitions are the cell's
    ``child_children`` (each carrying a matching ``childRefId``). Author object
    order is preserved; a child with no layout entry renders at full size."""
    bounds_by_ref: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for obj in objects or []:
        if isinstance(obj, dict) and obj.get("childRefId"):
            bounds_by_ref[obj["childRefId"]] = obj.get("bounds") or {}
            order.append(obj["childRefId"])
    child_by_ref: Dict[str, Dict[str, Any]] = {
        cp["childRefId"]: cp
        for cp in (child_children or [])
        if isinstance(cp, dict) and cp.get("childRefId")
    }
    out: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    seen = set()
    for ref in order:
        cp = child_by_ref.get(ref)
        if cp is not None:
            out.append((cp, bounds_by_ref.get(ref) or {}))
            seen.add(ref)
    # Children with no objects[] layout entry (or no childRefId at all):
    # render them stacked at full size rather than dropping them.
    for cp in child_children or []:
        if isinstance(cp, dict) and cp.get("childRefId") not in seen:
            out.append((cp, {}))
    return out


def _grid_tiles(n: int) -> List[Dict[str, float]]:
    """``n`` near-square PERCENT tiles tiling a 100x100 box left-to-right,
    top-to-bottom. Used to lay out a tabbed container's tabs so every chart is
    visible (PBI has no tab container)."""
    if n <= 0:
        return []
    cols = int(math.ceil(math.sqrt(n)))
    rows = int(math.ceil(n / cols))
    cw, ch = 100.0 / cols, 100.0 / rows
    tiles: List[Dict[str, float]] = []
    for idx in range(n):
        r, c = divmod(idx, cols)
        tiles.append({"x": c * cw, "y": r * ch, "width": cw, "height": ch})
    return tiles


# Fallback font family. Qlik apps that lean on the app theme often carry
# no explicit font on a visual; without this PBI falls back to Segoe UI.
# We standardise on Arial for titles / labels / values when Qlik didn't
# name one (per the converter's default-font policy).
_DEFAULT_FONT_FAMILY = "Arial"


def _extract_visual_style(
    props: Dict[str, Any],
    resolve: Optional[Callable[[Any], str]] = None,
) -> Dict[str, Any]:
    """Walk a Qlik cell qProperty and normalise every styling hint we
    care about into a flat dict the visual factories can consume.

    ``resolve`` (optional) maps a raw Qlik title value -- a plain
    string, an ``=``-expression, or a ``qStringExpression`` dict -- to
    display text using the engine-evaluated snapshots; ``clean_label``
    is the fallback when absent.

    Output keys (all optional -- missing means "leave PBI default"):

        title, subtitle, footnote     str
        showTitle                     bool
        textAlign                     "left" | "center" | "right"
        fontSize                      int (pt)
        fontColor                     "#RRGGBB"
        fontFamily                    str
        backgroundColor               "#RRGGBB"
        borderColor                   "#RRGGBB"
        borderWidth                   int (px)
        padding                       int (px)
        legendShow                    bool
        showDataLabels                bool

    Sources we consult, in priority order:
      * top-level qProperty fields (Qlik's "common chart format"
        properties: ``fontSize``, ``textAlign``, ``showTitles``, ...);
      * ``layoutOptions`` (for filter/nav menu);
      * ``components[]`` (the Qlik "presentation" array that holds the
        big style records keyed by ``general`` / ``theme`` / ``slices``
        / ``title``).
    """
    out: Dict[str, Any] = {}

    # title / subtitle / footnote can be plain strings OR Qlik
    # expression objects ({qStringExpression:{qExpr:...}}); the resolver
    # substitutes engine-evaluated snapshot text for expressions, with
    # clean_label (strip `=` + quotes) as the final fallback.
    _txt = resolve if resolve else clean_label
    title = _txt(props.get("title"))
    if title:
        out["title"] = title
    subtitle = _txt(props.get("subtitle"))
    if subtitle:
        out["subtitle"] = subtitle
    footnote = _txt(props.get("footnote"))
    if footnote:
        out["footnote"] = footnote
    if "showTitles" in props:
        out["showTitle"] = bool(props.get("showTitles"))

    pt = _qlik_font_pt(props.get("fontSize"))
    if pt is not None:
        out["fontSize"] = pt

    text_align = (props.get("textAlign") or "").strip().lower()
    if text_align in ("left", "center", "right", "auto"):
        out["textAlign"] = text_align if text_align != "auto" else "center"

    # Direct background/border at the cell level (rare but supported by
    # some Qlik visualization extensions).
    bg = _qlik_color_to_hex(props.get("backgroundColor")) or _qlik_color_to_hex(
        (props.get("background") or {}).get("color")
        if isinstance(props.get("background"), dict)
        else None
    )
    if bg:
        out["backgroundColor"] = bg

    legend = props.get("legend")
    if isinstance(legend, dict):
        if "show" in legend:
            out["legendShow"] = bool(legend.get("show"))
        # Legend placement: Qlik's ``dock`` (top/bottom/left/right, or
        # the older near/far) -> PBI legend ``position``.
        dock = legend.get("dock")
        _DOCK_TO_POS = {
            "top": "Top", "bottom": "Bottom", "left": "Left", "right": "Right",
            "near": "Left", "far": "Right",
        }
        if isinstance(dock, str) and dock.strip().lower() in _DOCK_TO_POS:
            out["legendPosition"] = _DOCK_TO_POS[dock.strip().lower()]

    # Data labels: Qlik's dataPoint.showLabels is tri-state (True /
    # False / "auto" / "none" / missing). Only forward the explicit
    # booleans -- treat "auto"/missing as "leave PBI default" so we
    # don't force-hide a label on every visual where Qlik didn't say.
    dp = props.get("dataPoint")
    if isinstance(dp, dict):
        sl = dp.get("showLabels")
        if isinstance(sl, bool):
            out["showDataLabels"] = sl
        elif isinstance(sl, str) and sl.lower() in ("true", "false"):
            out["showDataLabels"] = sl.lower() == "true"

    # Axis show + axis-title flags. Qlik stores per-axis state in
    # ``dimensionAxis`` (category axis -- X on column/line, Y on bar)
    # and ``measureAxis`` (value axis):
    #
    #   show = "all"    -> show axis labels AND axis title
    #   show = "labels" -> show labels only (hide title)
    #   show = "none"   -> hide axis entirely
    #   missing         -> PBI default
    #
    # We forward both flags (showAxis + showAxisTitle) so the chart
    # mirrors Qlik's setting. ``categoryAxis`` and ``valueAxis`` are
    # PBI's object keys for these on bar/column/line/combo/scatter.
    for qlik_key, prefix in (
        ("dimensionAxis", "categoryAxis"),
        ("measureAxis",   "valueAxis"),
    ):
        ax = props.get(qlik_key)
        if not isinstance(ax, dict):
            continue
        show = (ax.get("show") or "").strip().lower() if isinstance(ax.get("show"), str) else None
        if show == "none":
            out[f"{prefix}Show"] = False
            out[f"{prefix}TitleShow"] = False
        elif show == "labels":
            out[f"{prefix}Show"] = True
            out[f"{prefix}TitleShow"] = False
        elif show == "all":
            out[f"{prefix}Show"] = True
            out[f"{prefix}TitleShow"] = True

    # Fixed value-axis range -- only when Qlik explicitly turned auto
    # min/max OFF (otherwise PBI's auto-fit is the better default).
    meas_ax = props.get("measureAxis")
    if isinstance(meas_ax, dict) and meas_ax.get("autoMinMax") is False:
        if isinstance(meas_ax.get("min"), (int, float)):
            out["valueAxisStart"] = float(meas_ax["min"])
        if isinstance(meas_ax.get("max"), (int, float)):
            out["valueAxisEnd"] = float(meas_ax["max"])

    # Walk ``components[]`` for richer styling. Components is an array
    # of ``{key, ...}`` records; the ``general`` and ``theme`` entries
    # carry colours, fonts, dividers, etc.
    for comp in (props.get("components") or []):
        if not isinstance(comp, dict):
            continue
        key = (comp.get("key") or "").strip().lower()

        if key == "general":
            # Title font / weight / colour bundle.
            title_block = comp.get("title") or {}
            if isinstance(title_block, dict):
                main_title = title_block.get("main") or {}
                if isinstance(main_title, dict):
                    fs = _qlik_font_pt(main_title.get("fontSize"))
                    if fs and "fontSize" not in out:
                        out["fontSize"] = fs
                    fc = _qlik_color_to_hex(main_title.get("color"))
                    if fc and "fontColor" not in out:
                        out["fontColor"] = fc
                    ff = main_title.get("fontFamily")
                    if isinstance(ff, str) and ff.strip():
                        out["fontFamily"] = ff.strip()
            # Background colour at the visual level.
            bg_block = comp.get("bgColor") or {}
            if isinstance(bg_block, dict):
                bg = _qlik_color_to_hex(bg_block.get("color")) or \
                     _qlik_color_to_hex(bg_block)
                if bg and "backgroundColor" not in out:
                    out["backgroundColor"] = bg
            # Border / divider colour.
            border = comp.get("borderColor") or {}
            bc = _qlik_color_to_hex(border) if isinstance(border, (dict, str)) else None
            if bc:
                out["borderColor"] = bc
            if isinstance(comp.get("borderWidth"), (int, float)):
                out["borderWidth"] = int(comp["borderWidth"])
            if isinstance(comp.get("padding"), (int, float)):
                out["padding"] = int(comp["padding"])

        elif key == "theme":
            content = comp.get("content") or {}
            if isinstance(content, dict):
                default_color = _qlik_color_to_hex(content.get("defaultColor"))
                if default_color and "fontColor" not in out:
                    out["fontColor"] = default_color
                hl = _qlik_color_to_hex(content.get("highlightColor"))
                if hl and "borderColor" not in out:
                    out["borderColor"] = hl

        elif key == "slices":
            # Pie / donut slice border colour.
            slice_style = comp.get("style") or {}
            sc = _qlik_color_to_hex(slice_style.get("strokeColor"))
            if sc and sc != "#ffffff":  # white stroke = invisible, skip
                out["pieSliceStroke"] = sc

    # Chart data colours from Qlik's ``color`` block (bar/line/pie/scatter).
    # The ``mode`` field is the canonical discriminant:
    #   * "primary"  (auto: false)  -- single fill for every data point;
    #     hex comes from ``paletteColor``.
    #   * "byDimension" / "byExpression" / "byMeasure" -- series /
    #     expression / gradient colouring. Recorded as ``chartColorMode``
    #     so the chart builder knows NOT to force the single-colour
    #     default; the actual palette comes from the report-level
    #     registered theme (pbi_theme.py), which is how PBI assigns
    #     per-series colours. (Per-visual ``dataColors`` entries without
    #     selectors can't express a palette -- the old multi-entry emit
    #     collapsed to one fill in Desktop.)
    color_block = props.get("color")
    if isinstance(color_block, dict):
        mode = (color_block.get("mode") or "").strip().lower()
        auto = color_block.get("auto")
        palette_ref = color_block.get("paletteColor")

        if auto is False and mode == "primary":
            # Explicit single-colour mode.
            c = _qlik_color_to_hex(palette_ref)
            if c:
                out["chartPrimaryColor"] = c
        elif mode in ("bydimension", "byexpression", "bymeasure") and auto is False:
            out["chartColorMode"] = "dimension" if mode == "bydimension" else "expression"

    # KPI value colour. When conditional coloring is OFF, Qlik renders
    # the value in ``conditionalColoring.paletteSingleColor`` -- every
    # real app stores it that way regardless of the ``singleColor``
    # enum value (observed: singleColor 3 with paletteSingleColor
    # {index: 6} = the default dark slate, or an explicit
    # {index: -1, color: "#..."}). The previous ``singleColor == 2``
    # gate matched no real app, so KPI values always fell back to the
    # generic font colour. Conditional (segments-based) coloring stays
    # un-replicated -- leave PBI's default then.
    hc = props.get("qHyperCubeDef") or {}
    measures = hc.get("qMeasures") or []
    if measures:
        cc = (measures[0].get("qDef") or {}).get("conditionalColoring") or {}
        if not cc.get("useConditionalColoring"):
            kpi_c = _qlik_color_to_hex(cc.get("paletteSingleColor"))
            if kpi_c:
                out["kpiValueColor"] = kpi_c

    # Default font family (Arial) when Qlik named none -- applied to
    # titles (_title_properties), data labels / axes, and KPI values.
    out.setdefault("fontFamily", _DEFAULT_FONT_FAMILY)
    return out


def _expr_literal(value: str) -> Dict[str, Any]:
    """Wrap a literal value in PBI's expression-literal shape."""
    return {"expr": {"Literal": {"Value": value}}}


def _bool_expr(b: bool) -> Dict[str, Any]:
    return _expr_literal("true" if b else "false")


def _solid_color_expr(hex_color: str) -> Dict[str, Any]:
    """Build a {solid: {color: <expr>}} block from a hex literal."""
    return {"solid": {"color": _expr_literal(f"'{hex_color}'")}}


def _apply_container_styling(
    visual_block: Dict[str, Any],
    style: Dict[str, Any],
) -> None:
    """Mutate ``visual_block`` to add visualContainerObjects entries
    (background / border / padding) and the within-objects ``title``
    bag from a normalised style dict.

    The PBI visual schema is strict about what lives where:

      * ``visualContainerObjects`` accepts only ``background``,
        ``border``, ``padding``, ``visualHeader``, ``stylePreset`` and
        a couple of others. NOT ``visualTitle`` -- earlier versions of
        this code emitted it there and Desktop refused to load the
        PBIP with "An additional property 'visualTitle' was included
        in the /visual/visualContainerObjects property".
      * The actual title text+colour+font lives under
        ``visual.objects.title[]`` -- the same bag a chart's legend /
        labels go in. That's what we write here.

    All entries are gated on the style key being present so we never
    overwrite PBI defaults with empty colour literals -- that would
    actually force a black-on-black look in Desktop.
    """
    container: Dict[str, Any] = visual_block.setdefault("visualContainerObjects", {})

    # Background colour on the visual container.
    bg = style.get("backgroundColor")
    if bg:
        container["background"] = [{
            "properties": {
                "show":  _bool_expr(True),
                "color": _solid_color_expr(bg),
            },
        }]

    # Border (PBI calls this "visualHeader" border / "outspacePane"
    # depending on theme; the modern path is `border`).
    border_color = style.get("borderColor")
    border_width = style.get("borderWidth")
    if border_color or border_width:
        props: Dict[str, Any] = {"show": _bool_expr(True)}
        if border_color:
            props["color"] = _solid_color_expr(border_color)
        if border_width:
            props["radius"] = _expr_literal(f"{int(border_width)}D")
        container["border"] = [{"properties": props}]

    # Padding (visualContainerObjects.padding{left,right,top,bottom}).
    pad = style.get("padding")
    if pad:
        container["padding"] = [{
            "properties": {
                "left":   _expr_literal(f"{int(pad)}D"),
                "right":  _expr_literal(f"{int(pad)}D"),
                "top":    _expr_literal(f"{int(pad)}D"),
                "bottom": _expr_literal(f"{int(pad)}D"),
            },
        }]

    # Title. In PBIR the title is a CONTAINER object
    # (``visualContainerObjects.title``) -- NOT ``objects.title``,
    # which PBI silently ignores (the cause of "titles not being
    # picked"). The visualContainer schema 2.8.0 / visualConfiguration
    # 2.3.0 define title under visualContainerObjects with
    # show / text / fontColor / fontSize / fontFamily / alignment /
    # background.
    title = style.get("title")
    if title and style.get("showTitle", True):
        if "title" not in container:
            container["title"] = [{"properties": _title_properties(style, title)}]
    elif style.get("showTitle") is False and "title" not in container:
        container["title"] = [{"properties": {"show": _bool_expr(False)}}]

    if not container:
        visual_block.pop("visualContainerObjects", None)
    if not visual_block.get("objects"):
        visual_block.pop("objects", None)


def _title_properties(style: Dict[str, Any], title: str) -> Dict[str, Any]:
    """Build the PBIR ``visualContainerObjects.title`` property bag from
    a normalised style dict. Property names per the visualConfiguration
    schema: show / text / fontColor / fontSize / fontFamily / alignment
    / background."""
    safe = title.replace("'", "''")
    props: Dict[str, Any] = {
        "show": _bool_expr(True),
        "text": _expr_literal(f"'{safe}'"),
    }
    align = style.get("textAlign")
    if align in ("left", "center", "right"):
        props["alignment"] = _expr_literal(f"'{align}'")
    fc = style.get("fontColor")
    if fc:
        props["fontColor"] = _solid_color_expr(fc)
    fs = style.get("fontSize")
    if fs:
        props["fontSize"] = _expr_literal(f"{int(fs)}D")
    ff = style.get("fontFamily")
    if ff:
        props["fontFamily"] = _expr_literal(f"'{ff}'")
    tbg = style.get("titleBackgroundColor")
    if tbg:
        props["background"] = _solid_color_expr(tbg)
    return props


class ReportBuilder:
    def __init__(
        self,
        ir: "QlikIR | Dict[str, Any]",
        model: SemanticModel,
        theme_palette: Optional[Dict[str, Any]] = None,
    ):
        # Same dual-accept contract as SemanticModel: typed QlikIR in the
        # normal pipeline, a plain dict from back-compat callers/tests.
        self.ir = QlikIR.from_dict(ir) if isinstance(ir, dict) else ir
        self.model = model
        # Resolved Qlik colour palette (pbi_theme.resolve_palette shape).
        # ``primary`` is the single-series default the chart builder
        # stamps on auto-coloured one-measure charts so they match
        # Qlik's default instead of PBI's theme colour 0.
        theme_palette = theme_palette or {}
        self.theme_primary: str = theme_palette.get("primary") or QLIK_HORIZON_PRIMARY
        # Engine-evaluated text-expression snapshots from the unbuild
        # (see text_eval.py). Empty when the IR predates the sidecar --
        # resolution then falls through to local static evaluation.
        evaluated = self.ir.get("evaluated") or {}
        self._eval_objects: Dict[str, Dict[str, str]] = (
            evaluated.get("objects") or {} if isinstance(evaluated, dict) else {}
        )
        self._eval_exprs: Dict[str, str] = (
            evaluated.get("expressions") or {} if isinstance(evaluated, dict) else {}
        )
        # Variable name -> definition, for the local static evaluator's
        # $(var) expansion.
        self._var_defs: Dict[str, str] = {}
        for v in self.ir.get("variables") or []:
            vn = (v.get("qName") or "").strip()
            vd = v.get("qDefinition")
            if vn and isinstance(vd, str):
                self._var_defs[vn] = vd
        # Index Qlik library items by qId for cell-level lookups.
        self.dim_by_id = {
            (d.get("qInfo", {}) or {}).get("qId", ""): d
            for d in ir.get("dimensions", []) or []
        }
        # Master objects in the IR are wrapped: top-level dict with
        # ``qProperty`` (the visual definition) and ``qChildren``. The
        # qId we want to key by lives inside ``qProperty.qInfo.qId``.
        # We store the inner qProperty so consumers can read
        # ``visualization``, ``qHyperCubeDef``, ``layoutOptions``, etc.
        # without re-walking the wrapper.
        self.master_by_id: Dict[str, Dict[str, Any]] = {}
        for m in ir.get("master_objects", []) or []:
            mprop = m.get("qProperty") or m
            qid = (mprop.get("qInfo") or {}).get("qId", "")
            if qid:
                self.master_by_id[qid] = mprop
        # Qlik sheet qId -> PBI page id (the same hex_id() the page
        # builder uses). Filled in by build_pages() before any visual
        # is emitted so action-button page navigation can resolve.
        self._sheet_to_page: Dict[str, str] = {}
        # Display order: needed for nextSheet / prevSheet navigation.
        self._sheet_order: List[str] = []
        # Non-fatal per-cell build failures, surfaced in the report.
        self.build_issues: List[str] = []
        # Dynamic textbox expressions that had no engine snapshot and
        # weren't statically evaluable -- the textbox shows the label /
        # formula instead of the value. Surfaced in conversion_report.md
        # so the user knows a connected re-run would capture them.
        self.text_eval_misses: set = set()
        # Shared, incrementally-maintained name index. Building a fresh
        # ``reserved_ci`` set from EVERY measure + EVERY column on every
        # inline-measure / calc-column synthesis was O(measures x columns)
        # per visual -- quadratic on a large app. We build the set ONCE
        # here (lazily, the first time it's needed) and update it in place
        # as new measures/columns are synthesised, so dedup is O(1)
        # amortised. ``_measure_home`` resolves a measure name to its
        # owning table in O(1) instead of re-scanning ``model.measures``.
        self._reserved_ci: Optional[set] = None
        self._measure_home_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Text-expression resolution (evaluated snapshots > static eval >
    # clean_label). See text_eval.py for the unbuild-side evaluation.
    # ------------------------------------------------------------------
    def _lookup_text_expr(self, obj_id: str, cid: str, expr: Any) -> Optional[str]:
        """Resolve one textbox expression reference to display text.

        Order: the object-context engine snapshot (exact, formatted),
        the doc-level snapshot keyed by the raw expression string, then
        the local static evaluator (literals / ``&`` concats /
        ``$(var)``). ``None`` means the caller keeps its label fallback.
        """
        if obj_id:
            per_obj = self._eval_objects.get(obj_id)
            if per_obj and cid in per_obj:
                return per_obj[cid]
        if isinstance(expr, str) and expr:
            hit = self._eval_exprs.get(expr)
            if hit is None:
                hit = self._eval_exprs.get(expr.strip())
            if hit is not None:
                return hit
            static = eval_static_expression(expr, self._var_defs)
            if static is None and len(self.text_eval_misses) < 50:
                self.text_eval_misses.add(expr.strip())
            return static
        return None

    def _resolve_text(self, raw: Any) -> str:
        """Resolve a title-ish value (plain string / ``=``-expression /
        ``qStringExpression`` dict) to display text.

        Expression values prefer the engine-evaluated snapshot, then the
        local static evaluator; ``clean_label`` (strip ``=`` + quotes)
        remains the last resort so behaviour never regresses for
        unresolvable expressions.
        """
        expr: Optional[str] = None
        if isinstance(raw, dict):
            inner = raw.get("qStringExpression")
            if isinstance(inner, dict):
                expr = inner.get("qExpr") or ""
            elif isinstance(inner, str):
                expr = inner
            else:
                expr = raw.get("qExpr") or ""
            expr = (expr or "").strip()
            if not expr:
                return clean_label(raw)
        elif isinstance(raw, str) and raw.strip().startswith("="):
            expr = raw.strip()
        else:
            return clean_label(raw)

        hit = self._eval_exprs.get(expr)
        if hit is None:
            hit = self._eval_exprs.get(expr.lstrip("=").strip())
        if hit is not None:
            return hit.strip()
        static = eval_static_expression(expr, self._var_defs)
        if static is not None:
            return static.strip()
        return clean_label(expr)

    # ------------------------------------------------------------------
    def _reserved_names(self) -> set:
        """Lazily build + return the case-insensitive set of every
        measure and column name currently in the model. Maintained in
        place by the synthesis sites (see ``_register_name``)."""
        if self._reserved_ci is None:
            s = {(m["name"] or "").lower() for m in self.model.measures}
            for t in self.model.tables:
                s.update((c["name"] or "").lower() for c in t["columns"])
            self._reserved_ci = s
        return self._reserved_ci

    def _register_name(self, name: str) -> None:
        """Record a newly-synthesised measure/column name so subsequent
        dedup checks see it without rebuilding the whole set."""
        if self._reserved_ci is not None and name:
            self._reserved_ci.add(name.lower())

    def _measure_home(self, mname: str) -> str:
        """Owning table for a measure name, O(1) after first lookup.

        Falls back to the first real table (never literal ``Data``) the
        same way the original inline ``next(...)`` scans did."""
        cached = self._measure_home_cache.get(mname)
        if cached is not None:
            return cached
        home = next(
            (m["table"] for m in self.model.measures if m["name"] == mname),
            self.model.tables[0]["name"] if self.model.tables else "Data",
        )
        self._measure_home_cache[mname] = home
        return home

    # ------------------------------------------------------------------
    def build_pages(self) -> List[Dict[str, Any]]:
        sheets = self.ir.get("sheets", []) or []
        # Two-pass: first compute every page's id so action-button
        # page-navigation can resolve targets even when the target
        # sheet hasn't been visited yet. Then build visuals.
        for sheet in sheets:
            page_id = hex_id("page", sheet["id"])
            self._sheet_to_page[sheet["id"]] = page_id
            self._sheet_order.append(sheet["id"])
        pages: List[Dict[str, Any]] = []
        for sheet in sheets:
            pages.append(self._build_page(sheet))
        if not pages:
            pages.append(self._blank_page())
        return pages

    # ------------------------------------------------------------------
    def _blank_page(self) -> Dict[str, Any]:
        return {
            "id":          hex_id("page", "blank"),
            "displayName": "Page 1",
            "height":      DEFAULT_PAGE_HEIGHT,
            "width":       DEFAULT_PAGE_WIDTH,
            "visuals":     [],
        }

    def _build_page(self, sheet: Dict[str, Any]) -> Dict[str, Any]:
        # Record the sheet currently being built so relative-navigation
        # buttons (nextSheet / prevSheet) on its cells can resolve their
        # target against ``_sheet_order``.
        self._current_sheet_id = sheet["id"]
        page = {
            "id":          hex_id("page", sheet["id"]),
            # Originating Qlik sheet id -- lets the writer anchor a
            # bookmark (which carries its own ``sheetId``) to the right
            # PBI page instead of always page 1.
            "sheet_id":    sheet["id"],
            "displayName": sheet["title"],
            "height":      DEFAULT_PAGE_HEIGHT,
            "width":       DEFAULT_PAGE_WIDTH,
            "visuals":     [],
        }

        # Page-level styling: Qlik sheets can carry a backgroundColor
        # (rare on standard sheets, common on dashboards with explicit
        # themes) plus a background image. Map both to PBI's page
        # ``background`` object so the canvas matches.
        bg_color = _qlik_color_to_hex(sheet.get("backgroundColor")) or \
                   _qlik_color_to_hex((sheet.get("background") or {}).get("color")
                                       if isinstance(sheet.get("background"), dict)
                                       else None)
        if bg_color:
            page["background"] = {
                "color": {"value": bg_color},
                "transparency": 0,
            }

        for z_index, cell in enumerate(sheet.get("cells", []) or []):
            try:
                visuals = self._build_visual(cell, z_index)
            except Exception as exc:  # noqa: BLE001
                # One malformed cell must never abort the whole report.
                # Degrade to a placeholder so the slot is preserved and
                # the failure is surfaced in conversion_report.md.
                ctype = (cell.get("type") or "?").strip()
                ctitle = clean_label((cell.get("properties") or {}).get("title")) or ctype
                msg = (
                    f"Sheet '{sheet.get('title', '?')}' cell '{ctitle}' "
                    f"(type={ctype}): build failed ({type(exc).__name__}: {exc})"
                )
                _log.warning(msg)
                self.build_issues.append(msg)
                bounds = cell.get("bounds", {}) or {}
                visuals = [self._build_textbox_visual(
                    {"title": f"[unconverted: {ctype}] {ctitle}"},
                    _scale(bounds.get("x", 0), DEFAULT_PAGE_WIDTH),
                    _scale(bounds.get("y", 0), DEFAULT_PAGE_HEIGHT),
                    max(40, _scale(bounds.get("width", 10), DEFAULT_PAGE_WIDTH)),
                    max(30, _scale(bounds.get("height", 10), DEFAULT_PAGE_HEIGHT)),
                    z_index,
                )]
            for v in visuals or []:
                page["visuals"].append(v)
        return page

    # ------------------------------------------------------------------
    def _build_visual(
        self, cell: Dict[str, Any], z: int, depth: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return ONE OR MORE PBI visuals for a Qlik sheet cell.

        Most cells produce a single visual; a filterpane expands into
        one slicer per child listbox, and a container expands into the
        visuals it holds, so the converted PBIP carries every filter and
        chart the author wired up. Empty list = drop the cell. ``depth``
        tracks container nesting so a cyclic graph can't recurse forever.
        """
        ctype = (cell.get("type") or "").strip().lower()
        bounds = cell.get("bounds", {}) or {}
        x = _scale(bounds.get("x", 0), DEFAULT_PAGE_WIDTH)
        y = _scale(bounds.get("y", 0), DEFAULT_PAGE_HEIGHT)
        w = max(40, _scale(bounds.get("width", 10), DEFAULT_PAGE_WIDTH))
        h = max(30, _scale(bounds.get("height", 10), DEFAULT_PAGE_HEIGHT))

        props = cell.get("properties", {}) or {}

        # Master-object reference resolution. When a sheet cell
        # carries ``qExtendsId``, its real visual definition lives on
        # the referenced master object. We merge the master's
        # ``qProperty`` under the cell's local overrides so the rest
        # of this method sees a unified ``props`` bag.
        #
        # The cell name is NOT a reliable master id, contra earlier
        # versions of this code: Qlik's sheet-cell `cells[].name`
        # matches the child generic-object qId (e.g. `VqujSt`), not
        # the referenced master qId. Falling back to it would
        # mis-resolve unrelated cells.
        mref = (
            props.get("qExtendsId")
            or props.get("masterReferenceId")
            or ""
        )
        if mref and mref in self.master_by_id:
            master_props = self.master_by_id[mref]
            # Local cell overrides win (title, bounds-derived hints),
            # but the master supplies qHyperCubeDef, visualization,
            # layoutOptions, components, etc.
            props = {**master_props, **{k: v for k, v in props.items() if v}}
            master_viz = (master_props.get("visualization") or "").strip().lower()
            if master_viz:
                ctype = master_viz

        # The explicit ``visualization`` property is the authoritative
        # renderer choice and can DIFFER from the cell's qType: a Qlik
        # straight table reports qType "pivot-table" but visualization
        # "sn-table", and an auto-chart reports qType "auto-chart" with
        # the real chart in ``visualization``. Prefer it when present and
        # recognised, so a straight table renders as a flat TABLE rather
        # than a pivot.
        viz = (props.get("visualization") or "").strip().lower()
        if viz and viz != ctype and (ctype == "auto-chart" or viz in VISUAL_TYPE_MAP):
            ctype = viz

        pbi_type = VISUAL_TYPE_MAP.get(ctype)

        # Bar / combo charts: refine the PBI type from Qlik's stacking
        # + orientation so a stacked column doesn't become a clustered
        # bar (and vice-versa).
        if ctype in ("barchart", "mekko-chart", "sn-mekko-chart"):
            pbi_type = _resolve_bar_combo_type(ctype, props, pbi_type)
        # Pie charts: switch to donutChart when Qlik's
        # ``donut.showAsDonut`` is set in the presentation settings.
        if ctype == "piechart":
            pbi_type = _resolve_pie_type(props, pbi_type)

        if ctype in ("text-image", "sn-text", "text", "sn-image", "image"):
            # Text and image dashboard objects both render as a PBI
            # textbox: the text body (markdown/title) shows directly; an
            # image's caption/title is preserved as a placeholder (the
            # binary itself lives in Qlik's media library and can't be
            # ported, but the layout slot and label survive).
            return [self._build_textbox_visual(props, x, y, w, h, z)]
        if ctype in ("action-button", "sn-action-button", "button"):
            return [self._build_action_button(props, x, y, w, h, z)]
        if ctype == "filterpane":
            return self._build_filterpane_slicers(cell, props, x, y, w, h, z)
        if ctype == "listbox":
            visual = self._build_slicer(props, x, y, w, h, z)
            return [visual] if visual else []
        if ctype in ("sn-nav-menu", "nav-menu"):
            return [self._build_page_navigator(props, x, y, w, h, z)]
        if ctype in _CONTAINER_CTYPES:
            return self._build_container(cell, props, ctype, x, y, w, h, z, depth)
        if pbi_type in (None, ""):
            # No PBI visual maps to this Qlik type. Keep TABLE as the
            # default whenever the cell carries a data hypercube, so an
            # unsupported chart (sankey, radar, mekko variants, custom
            # extensions, ...) still shows its figures instead of
            # vanishing. Only a data-less cell degrades to a textbox stub.
            if _has_hypercube_data(props):
                pbi_type = "tableEx"
            else:
                return [self._build_textbox_visual(
                    {"title": clean_label(props.get("title", "")) or ctype},
                    x, y, w, h, z,
                )]

        # A "table" with NO dimensions is semantically a card/KPI -- it's
        # a single row of measure values, not a grid. Qlik KPI / multi-KPI
        # objects and KPI-like cells that fall through to the ``tableEx``
        # default (custom extensions, an ``auto-chart`` whose real type
        # didn't resolve, etc.) otherwise render as a one-row table. Route
        # them to PBI's card visual instead -- the faithful equivalent,
        # and what the user expects. ``cardVisual`` handles one OR many
        # measures (multi-card). We DON'T override an author's explicit
        # table choice (``_EXPLICIT_TABLE_CTYPES``); KPIs already map
        # straight to ``cardVisual`` and never reach here.
        if pbi_type == "tableEx" and ctype not in _EXPLICIT_TABLE_CTYPES:
            dim_n, meas_n = _hypercube_counts(props)
            if dim_n == 0 and meas_n >= 1:
                pbi_type = "cardVisual"
        return [self._build_chart(pbi_type, props, x, y, w, h, z)]

    def _build_filterpane_slicers(
        self,
        cell: Dict[str, Any],
        props: Dict[str, Any],
        x: int, y: int, w: int, h: int, z: int,
    ) -> List[Dict[str, Any]]:
        """Expand a Qlik filterpane (which holds N child listboxes) into
        N PBI slicers tiled inside the parent rectangle. When the cell
        has no resolvable children the parent is rendered as a single
        slicer using whatever the parent props expose; if even that
        fails the slot becomes an empty-titled textbox so the layout
        survives.

        Tiling direction matches the author's intent:
          * ``layoutOptions.orientation`` on the filterpane wins when
            set (Qlik's explicit "horizontal" / "vertical").
          * Falls back to the cell's aspect ratio -- wide-and-short
            (w/h > 2) tiles horizontally; everything else vertically.
            This matches the natural read of a header-strip filter bar
            vs a sidebar filter column.
        """
        children = cell.get("child_children") or []
        layout = props.get("layoutOptions") or {}
        ori_str = (layout.get("orientation") or "").strip().lower()
        if ori_str in ("horizontal", "vertical"):
            horizontal = ori_str == "horizontal"
        else:
            horizontal = (w / max(1, h)) >= 2.0
        if not children:
            slicer = self._build_slicer(props, x, y, w, h, z, horizontal=horizontal)
            if slicer:
                return [slicer]
            return [self._build_textbox_visual(
                {"title": clean_label(props.get("title", "")) or "Filter"},
                x, y, w, h, z,
            )]

        # Tile children inside the parent rectangle. Vertical tiling
        # (default) carves height-shares; horizontal carves width-shares.
        # Each tile gets at least 30px on its growing dimension so PBI
        # doesn't reject the bounds.
        out: List[Dict[str, Any]] = []
        n = len(children)
        if horizontal:
            tile_w = max(40, w // n)
        else:
            tile_h = max(30, h // n)
        for i, child_props in enumerate(children):
            if horizontal:
                cx = x + i * tile_w
                cw = tile_w if i < n - 1 else max(40, w - i * tile_w)
                cy, ch = y, h
            else:
                cy = y + i * tile_h
                ch = tile_h if i < n - 1 else max(30, h - i * tile_h)
                cx, cw = x, w
            slicer = self._build_slicer(
                child_props, cx, cy, cw, ch, z + i, horizontal=horizontal,
            )
            if slicer:
                out.append(slicer)
            else:
                # Failed to resolve a field for this listbox. Drop a
                # placeholder so the user notices the gap rather than
                # silently losing the filter.
                title = clean_label(child_props.get("title", "")) or "Filter"
                out.append(self._build_textbox_visual(
                    {"title": title}, cx, cy, cw, ch, z + i,
                ))
        return out

    # ------------------------------------------------------------------
    def _build_container(
        self,
        cell: Dict[str, Any],
        props: Dict[str, Any],
        ctype: str,
        x: int, y: int, w: int, h: int, z: int,
        depth: int = 0,
    ) -> List[Dict[str, Any]]:
        """Expand a Qlik container into the visuals it holds, instead of
        dropping its contents as a single placeholder.

        Two container shapes, both preserved:

          * **Layout container** (``sn-layout-container``): children are
            positioned freely. Each ``props.objects[]`` entry carries the
            child's ``bounds`` as PERCENT OF THE CONTAINER and all children are
            visible at once; we re-base each percent rectangle onto the
            container's own rectangle (``_rebase_child_bounds``) and render it.
          * **Tabbed container** (``container`` / ``sn-tabbed-container``):
            children are tabs (one shown at a time) referenced by qId in
            ``props.children[]`` (master objects). PBI has no tab container, so
            we tile the tabs in a near-square grid (``_grid_tiles``) so every
            chart survives rather than only the active tab.

        Each child's ``qProperty`` is a COMPLETE visual definition (qType,
        hypercube, styling) exactly like a top-level cell, so we recurse
        through ``_build_visual`` and inherit every chart/KPI/table/slicer/text
        builder, master-object resolution, and styling extraction for free.

        Degrades to the historical grey placeholder when no child resolves (an
        empty / unrecognised / too-deeply-nested container still keeps its
        slot)."""
        children: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        if depth < _MAX_CONTAINER_DEPTH:
            if ctype == "sn-layout-container":
                children = _resolve_layout_children(
                    props.get("objects") or [], cell.get("child_children") or [],
                )
            else:  # tabbed: container / sn-tabbed-container
                children = self._resolve_tabbed_children(cell, props)

        placeholder = lambda: [self._build_textbox_visual(  # noqa: E731
            {"title": clean_label(props.get("title", "")) or "Container"},
            x, y, w, h, z, style={"backgroundColor": "#f7f7f7"},
        )]
        if not children:
            return placeholder()

        # Each child's OWN children (grandchildren of the container), keyed by
        # the child's qId, so a filterpane/container child can be handed its
        # listboxes / inner cells and expand fully instead of degrading.
        node_by_id = {
            (n.get("qProperty", {}) or {}).get("qInfo", {}).get("qId", ""): n
            for n in cell.get("child_nodes") or []
        }
        container_bounds = cell.get("bounds") or {}
        out: List[Dict[str, Any]] = []
        for i, (child_props, child_pct) in enumerate(children):
            qid = (child_props.get("qInfo") or {}).get("qId", "")
            grandchildren = [
                (g.get("qProperty") or {})
                for g in (node_by_id.get(qid, {}).get("qChildren") or [])
            ]
            synth = {
                "name": qid,
                "type": ((child_props.get("qInfo") or {}).get("qType")
                         or child_props.get("visualization") or ""),
                "properties": child_props,
                "bounds": _rebase_child_bounds(container_bounds, child_pct),
                # Hand the child its own children so a filterpane expands into
                # slicers and a nested container expands further -- never lost.
                "child_children": grandchildren,
                "child_nodes": node_by_id.get(qid, {}).get("qChildren") or [],
            }
            try:
                out.extend(self._build_visual(synth, z + 1 + i, depth + 1))
            except Exception as exc:  # noqa: BLE001
                # One bad child must not drop the rest of the container.
                ctitle = (clean_label(child_props.get("title", ""))
                          or synth["type"] or "child")
                msg = (f"Container child '{ctitle}' build failed "
                       f"({type(exc).__name__}: {exc})")
                _log.warning(msg)
                self.build_issues.append(msg)
        return out or placeholder()

    def _resolve_tabbed_children(
        self, cell: Dict[str, Any], props: Dict[str, Any],
    ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """``(child_props, percent_tile)`` pairs for a TABBED container.

        ``props.children[]`` references each tab by qId (``refId``); the
        definition is a master object (``master_by_id``) or, when inlined, one
        of the cell's ``child_children``. Tabs carry no bounds, so they are
        tiled in a near-square grid so all are visible."""
        inline_by_id = {
            (cp.get("qInfo") or {}).get("qId", ""): cp
            for cp in cell.get("child_children") or []
            if isinstance(cp, dict)
        }
        defs: List[Dict[str, Any]] = []
        for ref in props.get("children") or []:
            rid = ref.get("refId") if isinstance(ref, dict) else None
            cp = (self.master_by_id.get(rid) or inline_by_id.get(rid)) if rid else None
            if cp:
                defs.append(cp)
        if not defs:
            # Some tabbed containers inline children with no children[] index.
            defs = [cp for cp in cell.get("child_children") or [] if isinstance(cp, dict)]
        return list(zip(defs, _grid_tiles(len(defs))))

    # ------------------------------------------------------------------
    # Visual factories
    # ------------------------------------------------------------------
    def _frame(self, x: int, y: int, w: int, h: int, z: int, key: str) -> Dict[str, Any]:
        return {
            "$schema":  SCHEMA["visual"],
            "name":     hex_id("v", getattr(self, "_current_sheet_id", ""), key, str(x), str(y), str(z)),
            "position": {
                "x": x, "y": y, "z": z,
                "height": h, "width": w, "tabOrder": z,
            },
            "filterConfig": {"filters": []},
        }

    def _build_textbox_visual(
        self,
        props: Dict[str, Any],
        x: int, y: int, w: int, h: int, z: int,
        style: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # Three content sources, in priority order:
        #   1. ``text``  (sn-text / Lexical JSON) -- modern Qlik rich-text
        #      format; carries inline CSS, bold/italic bitmask, and
        #      expression-node references.
        #   2. ``markdown`` -- legacy text-image format with custom
        #      alignment/size/colour markers.
        #   3. ``title`` -- plain text fallback (always present).
        text_json = (props.get("text") or "").strip()
        markdown = (props.get("markdown") or "").strip()
        if text_json and text_json.startswith("{"):
            # Expression nodes resolve through the engine-evaluated
            # snapshots captured at unbuild time (object-context first),
            # so the PBI textbox shows Qlik's VALUES, not raw formulas.
            obj_id = ((props.get("qInfo") or {}).get("qId") or "").strip()
            lookup = lambda cid, expr: self._lookup_text_expr(obj_id, cid, expr)  # noqa: E731
            paragraphs = _lexical_to_paragraphs(text_json, props, lookup)
        elif markdown:
            paragraphs = _qlik_markdown_to_paragraphs(markdown)
        else:
            label = self._resolve_text(props.get("title", "")) or "Text"
            paragraphs = [{"textRuns": [{"value": label}]}]
        # Pull the first non-empty value off the first run for the
        # frame-id hint so the visual ID is stable across re-runs.
        hint = ""
        if paragraphs and paragraphs[0].get("textRuns"):
            hint = (paragraphs[0]["textRuns"][0].get("value") or "")
        visual_block: Dict[str, Any] = {
            "visualType": "textbox",
            "drillFilterOtherVisuals": True,
            "objects": {"general": [{"properties": {"paragraphs": paragraphs}}]},
        }
        # Merge legacy ``style`` arg into the extracted style (style
        # arg takes precedence for backgroundColor since it's how
        # container cells force a tint).
        extracted = _extract_visual_style(props, self._resolve_text)
        if style:
            extracted = {**extracted, **{k: v for k, v in style.items() if v}}
        # For text-image we don't render a container title -- the
        # markdown body IS the content. Suppress it explicitly so PBI's
        # default header bar doesn't sit on top of the text.
        extracted["showTitle"] = False
        _apply_container_styling(visual_block, extracted)
        out = self._frame(x, y, w, h, z, "text-" + (hint or "tb")[:24])
        out["visual"] = visual_block
        return out

    def _build_action_button(
        self, props: Dict[str, Any], x: int, y: int, w: int, h: int, z: int,
    ) -> Dict[str, Any]:
        button_style = props.get("style", {}) or {}
        # The button's visible TEXT. Qlik stores it across several
        # field names depending on the authoring version; check them all
        # (each may also be a ``qStringExpression`` -> clean_label
        # unwraps it). Without a real label PBI shows an empty button.
        label = (
            self._resolve_text(button_style.get("label", ""))
            or self._resolve_text(button_style.get("text", ""))
            or self._resolve_text(props.get("label", ""))
            or self._resolve_text(props.get("text", ""))
            or self._resolve_text(props.get("title", ""))
            or "Button"
        )
        default_sel = {"id": "default"}

        # Normalise the button's font/background styling. Modern
        # ``sn-action-button`` nests these in OBJECTS
        # (``style.font = {color, size, fontFamily, style:{bold,...}}``,
        # ``style.background = {color}``); older apps use flat fields
        # (``color``, ``fontSize``, ``font``, ``backgroundColor``). Read
        # both so the converted button matches Qlik. The old code read
        # only the flat form -- and treated ``style.font`` as a string,
        # so when it was an object the font family (and the Arial
        # default) were dropped entirely.
        font_obj = button_style.get("font") if isinstance(button_style.get("font"), dict) else {}
        bg_obj = button_style.get("background") if isinstance(button_style.get("background"), dict) else {}
        font_style = font_obj.get("style") if isinstance(font_obj.get("style"), dict) else {}

        text_props: Dict[str, Any] = {
            "show": _bool_expr(True),
            "text": _expr_literal(f"'{label}'"),
        }
        btn_color = (
            _qlik_color_to_hex(font_obj.get("color"))
            or _qlik_color_to_hex(button_style.get("color"))
            or _qlik_color_to_hex(button_style.get("fontColor"))
        )
        if btn_color:
            text_props["fontColor"] = _solid_color_expr(btn_color)
        btn_fs = _qlik_font_pt(font_obj.get("size")) or _qlik_font_pt(button_style.get("fontSize"))
        if btn_fs:
            text_props["textSize"] = _expr_literal(f"{int(btn_fs)}D")
        btn_family = (
            font_obj.get("fontFamily")
            or font_obj.get("family")
            or (button_style.get("font") if isinstance(button_style.get("font"), str) else None)
            or button_style.get("fontFamily")
            or _DEFAULT_FONT_FAMILY
        )
        if isinstance(btn_family, str) and btn_family.strip():
            text_props["fontFamily"] = _expr_literal(f"'{btn_family.strip()}'")
        is_bold = (
            bool(font_style.get("bold"))
            or (button_style.get("fontWeight") or "").lower() == "bold"
            or (font_obj.get("weight") or "").lower() == "bold"
        )
        if is_bold:
            text_props["bold"] = _bool_expr(True)
        if bool(font_style.get("italic")):
            text_props["italic"] = _bool_expr(True)
        if bool(font_style.get("underline")):
            text_props["underline"] = _bool_expr(True)
        align = (
            button_style.get("textAlign")
            or font_obj.get("align")
            or button_style.get("horizontalAlign")
            or ""
        ).lower()
        if align in ("left", "center", "right"):
            text_props["horizontalAlignment"] = _expr_literal(f"'{align}'")

        objects: Dict[str, Any] = {
            "text": [{"properties": text_props, "selector": default_sel}],
        }

        # Shape fill (button background) from Qlik style -- nested
        # ``background.color`` (modern) or flat ``backgroundColor``.
        btn_bg = (
            _qlik_color_to_hex(bg_obj.get("color"))
            or _qlik_color_to_hex(button_style.get("backgroundColor"))
        )
        if btn_bg:
            objects["fill"] = [{
                "properties": {
                    "show":            _bool_expr(True),
                    "fillColor":       _solid_color_expr(btn_bg),
                    "transparency":    _expr_literal("0D"),
                },
                "selector": default_sel,
            }]

        # ----- Page navigation ------------------------------------------------
        # Qlik writes button navigation in several shapes depending on
        # the Qlik Sense version that authored the app. Try them all
        # before giving up.
        nav_target_page = self._resolve_button_navigation(props)
        if nav_target_page:
            objects["icon"] = [{
                "properties": {"show": _bool_expr(True)},
                "selector": default_sel,
            }]
            objects["shape"] = [{
                "properties": {"show": _bool_expr(True)},
                "selector": default_sel,
            }]
            objects["visualAction"] = [{
                "properties": {
                    "show":              _bool_expr(True),
                    "type":              _expr_literal("'PageNavigation'"),
                    "navigationSection": _expr_literal(f"'{nav_target_page}'"),
                },
                "selector": default_sel,
            }]

        visual_block = {
            "visualType": "actionButton",
            "drillFilterOtherVisuals": True,
            "objects": objects,
        }
        # Container-level styling (background/border/padding) honouring
        # the same Qlik styling extractor as charts. The button label
        # already lives in the ``text`` object bag, so suppress
        # container title.
        style = _extract_visual_style(props, self._resolve_text)
        style["showTitle"] = False
        _apply_container_styling(visual_block, style)
        out = self._frame(x, y, w, h, z, "button-" + label[:24])
        out["visual"] = visual_block
        return out

    def _resolve_button_navigation(
        self, props: Dict[str, Any],
    ) -> Optional[str]:
        """Return the PBI page id this Qlik action-button navigates to,
        or ``None`` if the button has no recognisable navigation action.

        Shapes handled:
          * ``actions[i].actionType == "goToSheet"`` + ``actions[i].sheet``
          * ``actions[i].action == "goToSheet"`` + ``actions[i].sheet``
          * ``navigation.action == "goToSheet"`` + ``navigation.sheet``
          * Same with ``"goToSheetById"``.
          * ``"nextSheet"`` / ``"prevSheet"`` -- resolved against
            ``self._sheet_order``. Cannot be evaluated when the source
            sheet is unknown (called from a master object detached
            from any sheet); returns None in that case.
        """
        nav_specs: List[Dict[str, Any]] = []
        for a in (props.get("actions") or []):
            if isinstance(a, dict):
                nav_specs.append(a)
        nav = props.get("navigation")
        if isinstance(nav, dict):
            nav_specs.append(nav)

        for spec in nav_specs:
            action_kind = (
                spec.get("actionType")
                or spec.get("action")
                or spec.get("type")
                or ""
            ).strip()
            ak = action_kind.lower()
            if ak in ("gotosheet", "gotosheetbyid"):
                target_sheet = (
                    spec.get("sheet")
                    or spec.get("sheetId")
                    or spec.get("targetSheet")
                    or ""
                ).strip()
                target = self._sheet_to_page.get(target_sheet)
                if target:
                    return target
            elif ak in ("nextsheet", "prevsheet"):
                # Relative navigation: resolve against the display order,
                # anchored on the sheet currently being built.
                cur = getattr(self, "_current_sheet_id", None)
                if cur and cur in self._sheet_order:
                    idx = self._sheet_order.index(cur)
                    tgt = idx + 1 if ak == "nextsheet" else idx - 1
                    if 0 <= tgt < len(self._sheet_order):
                        target = self._sheet_to_page.get(self._sheet_order[tgt])
                        if target:
                            return target
        return None

    def _build_page_navigator(
        self, props: Dict[str, Any], x: int, y: int, w: int, h: int, z: int,
    ) -> Dict[str, Any]:
        """Map Qlik's ``sn-nav-menu`` to PBI's built-in
        ``pageNavigator`` visual. PBI auto-renders one button per page
        in the report; we just supply orientation + alignment hints
        from Qlik's ``layoutOptions`` so the bar's look approximately
        matches the original.
        """
        layout = props.get("layoutOptions") or {}
        # Qlik writes orientation as "horizontal" / "vertical"; PBI's
        # pageNavigator wants 0 / 1 numeric. Fallback: derive from the
        # cell's own aspect ratio (wide+short = horizontal, tall+narrow
        # = vertical).
        ori_str = (layout.get("orientation") or "").strip().lower()
        if ori_str == "vertical":
            ori_pbi = "1"
        elif ori_str == "horizontal":
            ori_pbi = "0"
        else:
            ori_pbi = "0" if w >= h else "1"

        default_sel = {"id": "default"}
        objects: Dict[str, Any] = {
            "shape": [{
                "properties": {"show": {"expr": {"Literal": {"Value": "true"}}}},
                "selector": default_sel,
            }],
            "general": [{
                "properties": {
                    "orientation": {"expr": {"Literal": {"Value": ori_pbi}}},
                },
            }],
        }
        # PBI honours a ``showHiddenPages`` toggle but defaults to
        # hiding hidden pages, which matches the user's intent here.
        # We don't try to translate Qlik's per-item alignment into PBI;
        # the navigator visual auto-fills with one button per page.
        # Map Qlik's nav-menu theme colours onto PBI's pageNavigator
        # button text / fill. The Qlik master object's ``components``
        # array exposes a ``general`` block (bgColor, title.main.color)
        # and a ``theme`` block (defaultColor, highlightColor).
        style = _extract_visual_style(props, self._resolve_text)
        if style.get("backgroundColor"):
            objects["fill"] = [{
                "properties": {
                    "show":      _bool_expr(True),
                    "fillColor": _solid_color_expr(style["backgroundColor"]),
                },
                "selector": default_sel,
            }]
        if style.get("fontColor"):
            objects["text"] = [{
                "properties": {
                    "show":      _bool_expr(True),
                    "fontColor": _solid_color_expr(style["fontColor"]),
                },
                "selector": default_sel,
            }]
        visual_block = {
            "visualType": "pageNavigator",
            "drillFilterOtherVisuals": True,
            "objects": objects,
        }
        # Apply container styling but suppress the title (the navigator
        # is its own bar; a header on top would double up).
        style["showTitle"] = False
        _apply_container_styling(visual_block, style)
        out = self._frame(x, y, w, h, z, "navmenu")
        out["visual"] = visual_block
        return out

    def _build_slicer(
        self, props: Dict[str, Any], x: int, y: int, w: int, h: int, z: int,
        horizontal: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """Build a PBI slicer from a Qlik listbox / filterpane child.

        Three field-resolution paths, in order:
          1. ``qListObjectDef.qLibraryId`` -> lookup in
             ``ir['dimensions']`` for the field name. Qlik UI typically
             writes listboxes as a library reference, so this is the
             common case.
          2. ``qListObjectDef.qDef.qFieldDefs[0]`` -> the field name
             written inline.
          3. ``qHyperCubeDef.qDimensions[0]`` -> the legacy shape from
             older Qlik versions.

        ``horizontal`` controls the slicer's PBI orientation property
        (0 = horizontal pill row, 1 = vertical list). When unset, we
        infer from the cell's own aspect ratio.

        Returns ``None`` when no resolvable field is found; the caller
        decides whether to render a placeholder.
        """
        if horizontal is None:
            horizontal = (w / max(1, h)) >= 2.0
        field_ref: Optional[Tuple[str, str]] = None
        column_label: Optional[str] = None

        # Path 1 / 2: qListObjectDef.
        lod = props.get("qListObjectDef") or {}
        lib_id = (lod.get("qLibraryId") or "").strip()
        qdef = lod.get("qDef") or {}
        if lib_id and lib_id in self.dim_by_id:
            lib_dim = self.dim_by_id[lib_id]
            lib_def = lib_dim.get("qDim", {}) or {}
            for fd in (lib_def.get("qFieldDefs") or []):
                field_ref = self._resolve_field_name(fd)
                if field_ref:
                    column_label = fd
                    break
            if not field_ref:
                title = (lib_dim.get("qMetaDef") or {}).get("title")
                if title:
                    field_ref = self._resolve_field_name(title)
                    column_label = title
        if not field_ref:
            for fd in (qdef.get("qFieldDefs") or []):
                field_ref = self._resolve_field_name(fd)
                if field_ref:
                    column_label = fd
                    break

        # Path 3: qHyperCubeDef.qDimensions (legacy).
        if not field_ref:
            hyper = props.get("qHyperCubeDef") or {}
            for d in (hyper.get("qDimensions") or []):
                ref = self._resolve_field(d, d.get("qDef") or {})
                if ref:
                    field_ref = ref
                    column_label = (
                        (d.get("qDef") or {}).get("qFieldDefs") or [None]
                    )[0] or ref[1]
                    break

        if field_ref is None:
            return None
        table, column = field_ref
        slicer_projection = self._projection(table, column, is_measure=False)
        # Default the slicer to **dropdown** mode. The legacy emit set
        # ``general.orientation`` (0=horizontal pill row / 1=vertical
        # list) which PBI Desktop applied *before* the data.mode hint,
        # so dropdown was effectively ignored and the user saw a
        # vertical checkbox list. We now omit orientation entirely so
        # the dropdown setting wins on first load -- a compact selector
        # that matches Qlik's listbox / filterpane UX better than the
        # long checkbox list PBI defaults to.
        visual_block = {
            "visualType": "slicer",
            "drillFilterOtherVisuals": True,
            "objects": {
                "data": [{"properties": {"mode": _expr_literal("'Dropdown'")}}],
            },
            "query": {
                "queryState": {
                    "Values": {"projections": [slicer_projection]},
                },
            },
        }
        # Slicer header text -- prefer the listbox's user-set title,
        # then the library dimension's title, then the column name.
        style = _extract_visual_style(props, self._resolve_text)
        header = self._resolve_text(
            style.get("title")
            or props.get("title")
            or (qdef.get("title") if isinstance(qdef, dict) else "")
            or column_label
            or column
        )
        if header:
            hdr_props: Dict[str, Any] = {
                "show": _bool_expr(True),
                "text": _expr_literal(f"'{header}'"),
            }
            if style.get("fontColor"):
                hdr_props["fontColor"] = _solid_color_expr(style["fontColor"])
            if style.get("fontSize"):
                hdr_props["textSize"] = _expr_literal(f"{int(style['fontSize'])}D")
            if style.get("fontFamily"):
                hdr_props["fontFamily"] = _expr_literal(f"'{style['fontFamily']}'")
            visual_block.setdefault("objects", {})["header"] = [{"properties": hdr_props}]
        # The slicer header IS the title here, so suppress the container
        # title to avoid a duplicate header bar.
        style["showTitle"] = False
        _apply_container_styling(visual_block, style)
        out = self._frame(x, y, w, h, z, "slicer-" + (column or "f")[:24])
        out["visual"] = visual_block
        return out

    def _collect_map_fields(
        self, props: Dict[str, Any],
    ) -> Tuple[
        List[Tuple[str, str]],
        List[Tuple[str, str, bool]],
        Dict[str, Any],
    ]:
        """Pull (categories, measures, extras) out of a Qlik map's
        ``gaLayers[]`` and any nested ``qHyperCubeDef``s.

        Qlik writes map bindings as one or more layers, each layer
        being a record with ``locationOrLatitude`` (a field ref like
        ``"HCP.Latitude"`` or an inline ``"=From_Latitude"``),
        optionally ``locationLongitude``, a ``size.expression`` (a
        Qlik measure expression), plus a ``qHyperCubeDef`` carrying
        the layer's dimension(s).

        Returns:

          * ``category_fields`` -- ``[(table, column), ...]`` for the
            map's grouping dimensions (legend / detail).
          * ``measure_refs``   -- empty (size goes into ``extras`` so
            it can be slotted under ``Size`` rather than the generic
            value-projection set).
          * ``extras``         -- ``{latitude_proj, longitude_proj,
            size_proj}`` projection dicts to slot into the visual's
            queryState by the caller. Any of these may be missing if
            the layer didn't bind that channel.
        """
        layers = props.get("gaLayers") or props.get("layers") or []
        category_fields: List[Tuple[str, str]] = []
        extras: Dict[str, Any] = {}
        seen_cats: set = set()
        # An explicit ``size.expression`` (a point layer's bubble-size
        # binding) must win over a different layer's hypercube measure,
        # regardless of layer order -- otherwise a choropleth layer that
        # happens to come first claims the Size slot. Track whether the
        # current size_proj came from an explicit expression.
        size_is_explicit = False

        for layer in layers:
            if not isinstance(layer, dict):
                continue
            # Latitude/Longitude binding. ``locationOrLatitude`` may
            # carry either an explicit field name (key) or a Qlik
            # expression (``=<expr>``). Strip the leading "=" before
            # field-table lookup so e.g. ``"=From_Latitude"`` resolves.
            lat_block = layer.get("locationOrLatitude") or {}
            lon_block = layer.get("locationLongitude") or {}
            lat_field = self._field_from_ga_ref(lat_block)
            lon_field = self._field_from_ga_ref(lon_block)
            if lat_field and "latitude_proj" not in extras:
                t, c = lat_field
                extras["latitude_proj"] = self._projection(t, c, is_measure=False)
            if lon_field and "longitude_proj" not in extras:
                t, c = lon_field
                extras["longitude_proj"] = self._projection(t, c, is_measure=False)

            # Size measure -- ``size.expression`` is a Qlik measure
            # expression like ``"=Sum(distinct Patients_Count)"``. Try
            # the native-aggregation fast path; if that fails, fall
            # back to synthesising a DAX measure.
            size_block = layer.get("size") or {}
            size_expr_block = size_block.get("expression") or {}
            if size_expr_block and not size_is_explicit:
                ref = None
                key = (size_expr_block.get("key") or "").strip()
                # A ``libraryItem`` size expression references a MASTER
                # measure by id (``key``); ``label`` is its friendly
                # name. Bind to the already-built measure via
                # ``qLibraryId`` rather than passing the id as an inline
                # expression (which made the translator emit a dangling
                # ``'Table'[<id>]`` column ref and synthesise a broken
                # "Measure <id>" measure, once per map that used it).
                if size_expr_block.get("type") == "libraryItem":
                    if key in self.model.measure_by_id:
                        ref = self._resolve_measure({"qLibraryId": key})
                    # else: the master measure wasn't built -- skip
                    # rather than synthesise a dangling measure.
                else:
                    size_expr = (key or size_expr_block.get("label") or "").lstrip("=")
                    if size_expr:
                        ref = self._resolve_measure({"qDef": {"qDef": size_expr}})
                if ref:
                    t, c, is_meas = ref
                    extras["size_proj"] = self._projection(t, c, is_measure=is_meas)
                    size_is_explicit = True

            # Per-layer hypercube dimensions become "Category" /
            # "Series" projections.
            hyper = layer.get("qHyperCubeDef") or {}
            for d in hyper.get("qDimensions", []) or []:
                ref = self._resolve_field(d, d.get("qDef") or {})
                if ref and ref not in seen_cats:
                    seen_cats.add(ref)
                    category_fields.append(ref)
            for m in hyper.get("qMeasures", []) or []:
                ref = self._resolve_measure(m)
                if ref and "size_proj" not in extras:
                    t, c, is_meas = ref
                    extras["size_proj"] = self._projection(t, c, is_measure=is_meas)

        return category_fields, [], extras

    def _field_from_ga_ref(
        self, ref: Dict[str, Any],
    ) -> Optional[Tuple[str, str]]:
        """Resolve a Qlik gaLayer location-block into ``(table, column)``.

        The block shape is ``{key, label, type}``. ``key`` is either a
        bare field name (``"HCP.Latitude"``) or a Qlik expression
        prefixed with ``=``. We strip the prefix and try the
        field-table lookup; anything that doesn't resolve returns None.
        """
        if not isinstance(ref, dict):
            return None
        from .model import _sanitize_column_name as _san
        key = (ref.get("key") or "").strip().lstrip("=").strip()
        if not key:
            return None
        # Try direct field lookup, then sanitised, then a simple
        # 1-field expression like ``[From_Latitude]``.
        import re
        m = re.match(r"^\[?([A-Za-z_][A-Za-z0-9_. \-]{0,80}?)\]?\s*$", key)
        candidate = m.group(1).strip() if m else key
        table = (
            self.model.field_table.get(candidate)
            or self.model.field_table.get(_san(candidate))
        )
        if not table:
            return None
        return (table, _san(candidate))

    def _collect_chart_fields(
        self,
        pbi_type: str,
        props: Dict[str, Any],
    ) -> Tuple[
        List[Tuple[str, str]],
        List[Tuple[str, str, bool]],
        List[Tuple[str, str, bool]],
        Dict[str, Any],
    ]:
        """Resolve a chart cell's category fields, value measures,
        tooltip-only measures, and map extras, AND compute the per-visual
        sort chain (stored on ``self._current_sort_specs``).

        Map visuals don't carry their bindings in ``qHyperCubeDef`` --
        they're spread across ``gaLayers[]`` (PointLayer / LineLayer /
        ChoroplethLayer / etc.); ``_collect_map_fields`` funnels those
        into the same ``(category_fields, measure_refs)`` shape the rest
        of the chart builder consumes. Pure extraction of the collection
        head of ``_build_chart`` -- identical resolution + sort logic."""
        tooltip_refs: List[Tuple[str, str, bool]] = []
        # Reset per-visual sort accumulator; the map branch below
        # bypasses the hypercube-driven population path.
        self._current_sort_specs = []
        if pbi_type == "azureMap" and (props.get("gaLayers") or props.get("layers")):
            category_fields, measure_refs, map_extras = self._collect_map_fields(props)
            return category_fields, measure_refs, tooltip_refs, map_extras

        map_extras: Dict[str, Any] = {}
        hyper = props.get("qHyperCubeDef", {}) or {}
        dim_defs = hyper.get("qDimensions", []) or []
        meas_defs = hyper.get("qMeasures", []) or []
        category_fields = []
        # Per-visual sort candidates, each tagged with its Qlik
        # hypercube COLUMN INDEX (dimensions 0..D-1, measures
        # D..D+M-1) and EXPLICIT direction (or None for "Auto"). We
        # order them by ``qInterColumnSortOrder`` -- Qlik's statement
        # of WHICH column drives the sort and in what priority -- then
        # resolve Auto on the topmost field below. Tuple shape:
        # (qlik_col_index, table, name, is_measure, explicit_dir|None).
        sort_candidates: List[Tuple[int, str, str, bool, Optional[str]]] = []
        for di, d in enumerate(dim_defs):
            ref = self._resolve_field(d, d.get("qDef") or {})
            if ref:
                category_fields.append(ref)
                sort_candidates.append(
                    (di, ref[0], ref[1], False, _sort_direction_from_qlik(d))
                )
        measure_refs = []
        # cardVisual (KPI) and gauge bind a single scalar value and
        # require a real Measure, not a Column -- force synthesis.
        force_meas = pbi_type in ("cardVisual", "gauge")
        for mi, m in enumerate(meas_defs):
            ref = self._resolve_measure(m, force_measure=force_meas)
            if not ref:
                continue
            # Tooltip-only routing. Qlik flags tooltip measures
            # via an explicit ``isTooltip`` boolean (newer apps)
            # or via the measure's qLabel starting with "Tooltip:"
            # (older convention). When detected, route the
            # projection to the Tooltips slot rather than the
            # value-projection set; charts pick this up in
            # ``_query_state_for_type`` via the cat[1:]+val[1:]
            # tooltip-tail pattern, but PBI also accepts an
            # explicit Tooltips slot.
            qdef = m.get("qDef") or {}
            label = (qdef.get("qLabel") or "").strip()
            if (m.get("isTooltip") is True
                    or qdef.get("isTooltip") is True
                    or label.lower().startswith("tooltip:")
                    or label.lower().startswith("tooltip ")):
                tooltip_refs.append(ref)
            else:
                measure_refs.append(ref)
                sort_candidates.append(
                    (len(dim_defs) + mi, ref[0], ref[1], ref[2],
                     _sort_direction_from_qlik(m))
                )
        # Order candidates by Qlik's qInterColumnSortOrder -- the
        # column-priority list whose FIRST entry is the "topmost"
        # field in Qlik's sorting section (the primary sort). Columns
        # not named in it keep their natural order, after the named
        # ones.
        inter = hyper.get("qInterColumnSortOrder")
        has_priority = isinstance(inter, list) and len(inter) > 0
        if has_priority:
            rank = {idx: p for p, idx in enumerate(inter)}
            sort_candidates.sort(key=lambda e: rank.get(e[0], 10 ** 6))
        # Resolve into the final sort chain. Qlik sorts by the TOPMOST
        # field (first after ordering). When that field's mode is
        # "Auto" (no explicit numeric/ascii/expression direction -- a
        # load-order-only or empty criteria), Qlik's effective order
        # depends on the FIELD TYPE: it auto-sorts a MEASURE
        # descending-by-value but a DIMENSION ascending (alphabetical
        # / numeric). [Verified against real app metadata: Auto dims
        # serialise {qSortByNumeric:1,...} = ascending; Auto measures
        # {qSortByNumeric:-1} = descending.] So the primary field
        # always emits a sort -- its explicit direction, or that
        # type-based default; lower-priority fields contribute only
        # when they carry an EXPLICIT direction (a deliberate
        # multi-level sort), so naturally-ordered secondary columns
        # aren't force-sorted. Emitted below as a single
        # ``visual.query.sortDefinition`` (PBI rejects per-projection
        # ``sortDirection``).
        num_dims = len(dim_defs)
        sort_specs: List[Tuple[str, str, bool, str]] = []
        for pos, (idx, t, c, is_m, direction) in enumerate(sort_candidates):
            if direction is None:
                if pos == 0:
                    # Auto on the primary field: measure -> Descending,
                    # dimension -> Ascending (Qlik's per-type default).
                    # Use the QLIK column role (index < num_dims is a
                    # dimension, >= is a measure), NOT the PBI
                    # ``is_m`` flag -- a native-aggregation measure
                    # like ``Sum(X)`` binds as a PBI Column yet is a
                    # Qlik MEASURE that auto-sorts descending.
                    is_qlik_measure = idx >= num_dims
                    direction = "Descending" if is_qlik_measure else "Ascending"
                else:
                    continue
            sort_specs.append((t, c, is_m, direction))
        self._current_sort_specs = sort_specs
        return category_fields, measure_refs, tooltip_refs, map_extras

    def _build_sort_definition(
        self, query_state: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Build the ``visual.query.sortDefinition`` block from the
        per-visual sort specs (``self._current_sort_specs``), or ``None``
        when nothing sortable reached the query state.

        PBI accepts a single ``sortDefinition`` sibling of ``queryState``
        (per-projection ``sortDirection`` is rejected). We forward every
        Qlik qSortBy hint collected for this visual, filtered to the
        projections that actually reached the query state. Pure extraction
        of the sort-emit block from ``_build_chart``."""
        sort_specs = getattr(self, "_current_sort_specs", []) or []
        present: set[Tuple[str, str]] = set()
        for slot in (query_state or {}).values():
            for proj in (slot.get("projections") or []):
                qr = proj.get("queryRef", "")
                if "." in qr:
                    t, c = qr.split(".", 1)
                    present.add((t, c))
        sort_entries: List[Dict[str, Any]] = []
        agg_map = getattr(self, "_column_aggregations", {}) or {}
        for t, c, is_m, direction in sort_specs:
            if (t, c) not in present:
                continue
            field_inner = {
                "Expression": {"SourceRef": {"Entity": t}},
                "Property":   c,
            }
            # A Qlik measure such as ``Sum(X)`` binds as a native
            # aggregation COLUMN (``is_m`` is False) whose projection
            # wraps the column in an ``Aggregation`` block. The sort
            # field MUST carry the SAME wrapper -- a bare
            # ``{"Column": {"Property": "X"}}`` does not match the
            # aggregated projection, so PBI silently ignores the sort
            # and falls back to sorting the category axis
            # alphabetically. Mirror ``_projection``'s Aggregation
            # shape here so "Sort axis by <measure>" actually binds.
            agg_fn = None if is_m else agg_map.get((t, c))
            if agg_fn:
                _AGG_TO_PBI_NUM = {
                    "Sum": 0, "Average": 1, "Count": 2, "Min": 3,
                    "Max": 4, "CountNonNull": 5,
                }
                sort_field = {"Aggregation": {
                    "Expression": {"Column": field_inner},
                    "Function":   _AGG_TO_PBI_NUM.get(agg_fn, 0),
                }}
            else:
                kind = "Measure" if is_m else "Column"
                sort_field = {kind: field_inner}
            sort_entries.append({
                "field":     sort_field,
                "direction": direction,
            })
        if not sort_entries:
            return None
        return {
            "sort":          sort_entries,
            "isDefaultSort": True,
        }

    def _build_chart_objects(
        self,
        pbi_type: str,
        props: Dict[str, Any],
        style: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Build the visual-level ``objects`` bag for a chart.

        Covers legend visibility/position, data-label show + font,
        category/value axis show + title + font + fixed range, reference
        lines, the minimum cardVisual (KPI) chrome bags, and azureMap
        mapSettings/controls. Pure extraction of the objects-bag section
        of ``_build_chart`` -- identical keys + values, same order.

        (The visual title is NOT emitted here; it goes into
        ``visualContainerObjects.title`` via ``_apply_container_styling``,
        the schema-correct location PBI actually reads.)"""
        objects: Dict[str, Any] = {}

        # Legend visibility -- honour Qlik's legend.show toggle when
        # the visual type has a legend concept (charts only; cards and
        # tables ignore it).
        if ("legendShow" in style or "legendPosition" in style) and pbi_type in (
            "lineChart", "clusteredBarChart", "clusteredColumnChart",
            "stackedBarChart", "stackedColumnChart",
            "pieChart", "donutChart", "treemap", "scatterChart", "columnChart",
            "lineClusteredColumnComboChart", "lineStackedColumnComboChart",
            "waterfallChart", "funnel",
        ):
            legend_props: Dict[str, Any] = {}
            if "legendShow" in style:
                legend_props["show"] = _bool_expr(bool(style["legendShow"]))
            if style.get("legendPosition"):
                legend_props["position"] = _expr_literal(f"'{style['legendPosition']}'")
            if legend_props:
                objects["legend"] = [{"properties": legend_props}]

        # Data labels show / hide.
        if "showDataLabels" in style and pbi_type in (
            "lineChart", "clusteredBarChart", "clusteredColumnChart",
            "stackedBarChart", "stackedColumnChart",
            "pieChart", "donutChart", "treemap", "scatterChart", "columnChart",
            "lineClusteredColumnComboChart", "lineStackedColumnComboChart",
            "waterfallChart", "funnel",
        ):
            label_props: Dict[str, Any] = {
                "show": _bool_expr(bool(style["showDataLabels"])),
            }
            # Carry the visual's font family onto the data labels so the
            # whole chart uses the Qlik font consistently (font family is
            # unambiguous and safe to share; size/colour are left to PBI
            # to avoid mixing the title's styling into the labels).
            if style.get("fontFamily"):
                label_props["fontFamily"] = _expr_literal(f"'{style['fontFamily']}'")
            objects["labels"] = [{"properties": label_props}]

        # Axis visibility + axis-title visibility (categoryAxis = X on
        # column/line, Y on bar; valueAxis = the other). Mirrors Qlik's
        # ``dimensionAxis.show`` / ``measureAxis.show`` ("all" / "labels"
        # / "none") translated by _extract_visual_style. Only emitted
        # for chart types that have these PBI object keys.
        _AXIS_TYPES = (
            "lineChart", "clusteredBarChart", "clusteredColumnChart",
            "stackedBarChart", "stackedColumnChart", "columnChart",
            "scatterChart", "lineClusteredColumnComboChart",
            "lineStackedColumnComboChart", "waterfallChart",
        )
        if pbi_type in _AXIS_TYPES:
            for sk, pk in (
                ("categoryAxis", "categoryAxis"),
                ("valueAxis",    "valueAxis"),
            ):
                show_key = f"{sk}Show"
                title_key = f"{sk}TitleShow"
                has_range = pk == "valueAxis" and (
                    "valueAxisStart" in style or "valueAxisEnd" in style
                )
                if show_key not in style and title_key not in style and not has_range:
                    continue
                axis_props: Dict[str, Any] = {}
                if show_key in style:
                    axis_props["show"] = _bool_expr(bool(style[show_key]))
                if title_key in style:
                    axis_props["showAxisTitle"] = _bool_expr(bool(style[title_key]))
                # Share the visual's font family with the axis labels.
                if style.get("fontFamily"):
                    axis_props["fontFamily"] = _expr_literal(f"'{style['fontFamily']}'")
                # Fixed value-axis range from Qlik's measureAxis min/max.
                if pk == "valueAxis":
                    if "valueAxisStart" in style:
                        axis_props["start"] = _expr_literal(f"{style['valueAxisStart']}D")
                    if "valueAxisEnd" in style:
                        axis_props["end"] = _expr_literal(f"{style['valueAxisEnd']}D")
                if axis_props:
                    objects[pk] = [{"properties": axis_props}]

        # cardVisual: emit the minimum KPI chrome bags so Desktop's
        # validator doesn't reject an incomplete cardVisual. Honour
        # Qlik's fontSize / textAlign / fontColor on the primary value
        # pane so a KPI with ``fontSize: "L"`` in Qlik renders large
        # in PBI too.
        # Reference lines on chart family. Qlik stores them in
        # ``refLines`` / ``measureAxis.refLines`` per chart type;
        # walk all the conventional locations and emit one PBI
        # referenceLine entry per Qlik ref-line.
        if pbi_type in (
            "lineChart", "clusteredBarChart", "clusteredColumnChart",
            "stackedBarChart", "stackedColumnChart",
            "columnChart", "scatterChart", "lineClusteredColumnComboChart",
            "lineStackedColumnComboChart",
        ):
            ref_lines = self._collect_ref_lines(props)
            if ref_lines:
                objects["referenceLine"] = ref_lines

        # Chart data colours. Multi-series palettes come from the
        # report-level registered theme (pbi_theme.py) -- per-visual
        # ``dataColors`` entries can't express an ordered palette, so
        # only a SINGLE default fill is emitted here:
        #   * chartPrimaryColor -- the author's explicit single colour
        #     (auto: false, mode "primary").
        #   * Otherwise, an auto-coloured chart that renders as ONE
        #     series in Qlik (<=1 dimension, exactly 1 measure) gets the
        #     Qlik theme's primaryColor -- Qlik paints those with its
        #     single-colour default, NOT data-palette colour 0, which is
        #     what PBI would otherwise pick from the theme.
        # Dimension-coloured types (pie/donut/treemap/funnel) and
        # waterfall (sentiment colours) are left to the theme palette.
        _COLOR_CHART_TYPES = (
            "lineChart", "clusteredBarChart", "clusteredColumnChart",
            "stackedBarChart", "stackedColumnChart", "columnChart",
            "pieChart", "donutChart", "scatterChart", "treemap", "funnel",
            "lineClusteredColumnComboChart", "lineStackedColumnComboChart",
            "waterfallChart",
        )
        _SINGLE_SERIES_TYPES = (
            "lineChart", "clusteredBarChart", "clusteredColumnChart",
            "stackedBarChart", "stackedColumnChart", "columnChart",
            "scatterChart",
        )
        if pbi_type in _COLOR_CHART_TYPES:
            primary = style.get("chartPrimaryColor")
            if (
                not primary
                and pbi_type in _SINGLE_SERIES_TYPES
                and style.get("chartColorMode") not in ("dimension", "expression")
            ):
                dim_n, meas_n = _hypercube_counts(props)
                if meas_n == 1 and dim_n <= 1:
                    primary = self.theme_primary
            if primary:
                # ``dataPoint.defaultColor`` is the PBI object that
                # actually drives the default series fill on cartesian /
                # pie / scatter charts. (The earlier ``dataColors``
                # emission was NOT a real visual-object name -- Desktop
                # silently ignored it, so single-colour charts rendered
                # with the theme palette instead of Qlik's colour.)
                objects["dataPoint"] = [
                    {"properties": {"defaultColor": _solid_color_expr(primary)}}
                ]

        if pbi_type == "cardVisual":
            default_sel = {"id": "default"}
            objects.setdefault("fillCustom", [{
                "properties": {"show": _bool_expr(False)},
            }])
            val_fs = style.get("fontSize") or 15
            # kpiValueColor: the palette colour set on the KPI measure
            # (resolved for every real app now -- see the
            # conditionalColoring extraction). Falls back to fontColor
            # (an explicit object-level theme colour), then Qlik's
            # default KPI value colour -- the dark slate (#41555d), NOT
            # black, which is what Qlik actually renders by default.
            val_color = (
                style.get("kpiValueColor")
                or style.get("fontColor")
                or "#41555d"
            )
            val_align = style.get("textAlign") or "center"
            val_family = style.get("fontFamily") or "Arial"
            objects["value"] = [{
                "properties": {
                    "fontFamily": _expr_literal(f"'{val_family}'"),
                    "fontSize":   _expr_literal(f"{int(val_fs)}D"),
                    "fontColor":  _solid_color_expr(val_color),
                    "horizontalAlignment": _expr_literal(f"'{val_align}'"),
                },
                "selector": default_sel,
            }]
            # Hide secondary panes by default.
            for pane in ("label", "outline", "divider"):
                objects[pane] = [{
                    "properties": {"show": _bool_expr(False)},
                    "selector": default_sel,
                }]

        # azureMap: include mapSettings + controls so the map config is
        # complete.
        if pbi_type == "azureMap":
            objects["mapSettings"] = [{
                "properties": {
                    "view": {"expr": {"Literal": {"Value": "'UnitedStates'"}}},
                    "customZoom": {"expr": {"Literal": {"Value": "5D"}}},
                    "customCenterLat": {"expr": {"Literal": {"Value": "39.5D"}}},
                    "customCenterLon": {"expr": {"Literal": {"Value": "-104.99D"}}},
                    "autoZoom": {"expr": {"Literal": {"Value": "false"}}},
                },
            }]
            objects["controls"] = [{
                "properties": {
                    "autoZoom": {"expr": {"Literal": {"Value": "false"}}},
                },
            }]

        return objects

    def _build_chart(
        self,
        pbi_type: str,
        props: Dict[str, Any],
        x: int, y: int, w: int, h: int, z: int,
    ) -> Dict[str, Any]:
        # Resolve category / value / tooltip fields + map extras, and
        # compute the per-visual sort chain (-> self._current_sort_specs).
        (category_fields, measure_refs, tooltip_refs,
         map_extras) = self._collect_chart_fields(pbi_type, props)

        # Build PBI projections shape.
        cat_proj = [self._projection(table, col, is_measure=False)
                    for table, col in category_fields]
        val_proj = [self._projection(table, col, is_measure=is_measure)
                    for table, col, is_measure in measure_refs]
        tooltip_proj = [
            self._projection(table, col, is_measure=is_meas)
            for table, col, is_meas in (tooltip_refs if pbi_type != "azureMap"
                                        else [])
        ]

        query_state = _query_state_for_type(pbi_type, cat_proj, val_proj)
        # Tooltip-only measures get their own slot, replacing the
        # default "extras spill into Tooltips" behaviour of
        # _query_state_for_type. Doing this AFTER builds gives us
        # priority over the spill.
        if tooltip_proj:
            existing = (query_state.get("Tooltips") or {}).get("projections") or []
            query_state["Tooltips"] = {"projections": tooltip_proj + existing}
        # Map: inject Latitude / Longitude / Size slots from the
        # gaLayers extraction (these slot names match PBI's azureMap
        # field wells exactly).
        if pbi_type == "azureMap" and map_extras:
            lat = map_extras.get("latitude_proj")
            lon = map_extras.get("longitude_proj")
            size = map_extras.get("size_proj")
            if lat:
                query_state.setdefault("Latitude",  {"projections": [lat]})
            if lon:
                query_state.setdefault("Longitude", {"projections": [lon]})
            if size:
                # PBI azureMap puts the bubble-size measure on ``Size``;
                # if _query_state_for_type already populated it from a
                # qHyperCube measure, prefer the gaLayer one (it's the
                # author-set value).
                query_state["Size"] = {"projections": [size]}

        style = _extract_visual_style(props, self._resolve_text)
        title = style.get("title") or self._resolve_text(props.get("title", "")) or ""

        visual_block: Dict[str, Any] = {
            "visualType": pbi_type,
            "drillFilterOtherVisuals": True,
        }
        if query_state:
            visual_block["query"] = {"queryState": query_state}
            sort_def = self._build_sort_definition(query_state)
            if sort_def is not None:
                visual_block["query"]["sortDefinition"] = sort_def

        # Visual-level ``objects`` bag (legend / labels / axes / ref-lines
        # / cardVisual chrome / map settings). Built in one place so
        # ``_build_chart`` stays readable; emitted output is unchanged.
        objects = self._build_chart_objects(pbi_type, props, style)
        if objects:
            visual_block["objects"] = objects

        # Container-level styling -- background colour, border, padding,
        # and the modern visualTitle bag (PBI Desktop's header chrome).
        # Note: cards / cardVisuals get this too because Qlik authors
        # commonly set a tint on the cell background.
        _apply_container_styling(visual_block, style)

        # Conditional show/hide. Qlik writes a visibility condition
        # under qCalcCondition.qCond or showCondition. Translate the
        # condition expression to DAX and write it onto
        # ``visualFilters`` as a "show when expression is truthy"
        # filter. Best-effort: if translation fails the visual stays
        # always-visible (the current behaviour).
        show_cond = self._extract_show_condition(props)
        if show_cond:
            visual_block.setdefault("filterConfig", {"filters": []})
            visual_block["filterConfig"].setdefault("filters", []).append(show_cond)

        out = self._frame(x, y, w, h, z, f"{pbi_type}-{title[:16]}")
        out["visual"] = visual_block
        return out

    def _collect_ref_lines(
        self, props: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Return PBI referenceLine object entries for a chart.

        Qlik stashes ref-lines under various keys depending on chart
        type and Qlik version:
          * ``refLines``                  (newest)
          * ``measureAxis.refLines``      (bar/line)
          * ``yaxis.refLines``            (scatter)
        Each entry has ``value`` (number or expression), ``color``,
        ``label``, ``style`` (solid/dashed/dotted), ``show``.
        """
        candidates: List[Any] = []
        for key in ("refLines",):
            v = props.get(key)
            if isinstance(v, list):
                candidates.extend(v)
        for parent_key in ("measureAxis", "yaxis", "xaxis"):
            block = props.get(parent_key) or {}
            if isinstance(block, dict):
                v = block.get("refLines")
                if isinstance(v, list):
                    candidates.extend(v)

        out: List[Dict[str, Any]] = []
        for i, rl in enumerate(candidates):
            if not isinstance(rl, dict):
                continue
            if rl.get("show") is False:
                continue
            # Value can be a number or a Qlik expression. Try literal
            # first; expressions get a 0 fallback (the user can edit in
            # Desktop).
            v_block = rl.get("value")
            if isinstance(v_block, (int, float)):
                value_literal = f"{v_block}D"
            elif isinstance(v_block, dict):
                qv = (v_block.get("qv") or v_block.get("value") or "")
                if qv.lstrip("=").strip().replace(".", "", 1).replace("-", "", 1).isdigit():
                    value_literal = f"{qv.lstrip('=').strip()}D"
                else:
                    value_literal = "0D"
            elif isinstance(v_block, str) and v_block.lstrip("=").strip().replace(".", "", 1).replace("-", "", 1).isdigit():
                value_literal = f"{v_block.lstrip('=').strip()}D"
            else:
                value_literal = "0D"

            color = _qlik_color_to_hex(rl.get("paletteColor")) or \
                    _qlik_color_to_hex(rl.get("color")) or "#FF0000"
            label = (rl.get("label") or rl.get("text") or "").strip()
            style_name = (rl.get("style") or "dashed").lower()
            style_pbi = {
                "solid":  "'solid'",
                "dashed": "'dashed'",
                "dotted": "'dotted'",
                "dash":   "'dashed'",
                "dot":    "'dotted'",
            }.get(style_name, "'dashed'")

            props_dict: Dict[str, Any] = {
                "show":       _bool_expr(True),
                "value":      _expr_literal(value_literal),
                "lineColor":  _solid_color_expr(color),
                "style":      _expr_literal(style_pbi),
                "transparency": _expr_literal("0D"),
            }
            if label:
                props_dict["dataLabelText"] = _expr_literal(f"'{label}'")
                props_dict["dataLabelShow"] = _bool_expr(True)
            out.append({
                "properties": props_dict,
                "selector": {"id": f"refLine{i}"},
            })
        return out

    def _extract_show_condition(
        self, props: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Translate a Qlik visibility condition into a PBI filter.

        Qlik stores the condition in one of two places (both optional):

          * ``qCalcCondition.qCond.qv``   -- expression to evaluate
          * ``showCondition``             -- legacy alias

        PBI's filter shape for a condition-driven hide is a measure
        filter referencing a synthetic DAX measure that returns 1 or 0.
        Rather than synthesising a measure we emit a comment-prefixed
        BLANK() and let the user wire it up in Desktop; the value here
        is showing them that a condition existed.
        """
        cond = ""
        if isinstance(props.get("qCalcCondition"), dict):
            qcond = (props["qCalcCondition"].get("qCond") or {})
            cond = (qcond.get("qv") or "").strip()
        if not cond:
            cond = (props.get("showCondition") or "").strip()
        if not cond:
            return None
        # Translate the condition for DAX -- we don't actually wire it
        # into a filter yet (PBI's filter schema needs a real measure
        # ref); we attach a meta annotation so the conversion report
        # surfaces it.
        meta_notes = getattr(self, "_visibility_notes", None)
        if meta_notes is None:
            meta_notes = []
            self._visibility_notes = meta_notes
        meta_notes.append(cond.lstrip("="))
        # Returning None here means we don't pollute the visual JSON
        # with a half-wired filter; the note is for the conversion
        # report. (See task #41.)
        return None

    # ------------------------------------------------------------------
    # Field / measure resolution
    # ------------------------------------------------------------------
    def _resolve_field(self, dim_block: Dict[str, Any], qdef: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        # Library-linked dimension.
        lib_id = dim_block.get("qLibraryId")
        if lib_id and lib_id in self.dim_by_id:
            qdim = self.dim_by_id[lib_id].get("qDim", {}) or {}
            field_defs = qdim.get("qFieldDefs") or []
            labels = qdim.get("qFieldLabels")
            # 1) Simple field defs.
            for fd in field_defs:
                resolved = self._resolve_field_name(fd)
                if resolved:
                    return resolved
            # 2) Expression field defs -> calculated column. MUST come
            #    before the title fallback: a master dimension always
            #    carries a friendly ``title`` (e.g. "Weight Group") that
            #    is NOT a real column, and returning on it would mask a
            #    perfectly translatable expression dimension.
            for fd in field_defs:
                ce = self._resolve_dimension_expr(fd, labels)
                if ce:
                    return ce
            # 3) Last resort: the dimension's title, if it happens to be
            #    a real field name.
            title = qdim.get("title")
            if title:
                resolved = self._resolve_field_name(title)
                if resolved:
                    return resolved

        # Inline qFieldDefs.
        for fd in qdef.get("qFieldDefs", []) or []:
            resolved = self._resolve_field_name(fd)
            if resolved:
                return resolved
        # Inline EXPRESSION field def -> calculated column.
        for fd in qdef.get("qFieldDefs", []) or []:
            ce = self._resolve_dimension_expr(fd, qdef.get("qFieldLabels"))
            if ce:
                return ce
        label = (qdef.get("qFieldLabels") or [None])[0]
        if label:
            return self._resolve_field_name(label)
        return None

    @staticmethod
    def _strip_dim_expr(fd: str) -> str:
        """Strip a Qlik dimension expression down to its core: drop the
        leading ``=``, any trailing dangling binary operator (Qlik
        exports sometimes truncate ``... &``), and a single layer of
        wrapping parens."""
        s = (fd or "").strip()
        if s.startswith("="):
            s = s[1:].strip()
        s = s.rstrip(" &+-*/").strip()
        while s.startswith("(") and s.endswith(")"):
            inner = s[1:-1].strip()
            if not inner:
                break
            s = inner
        return s

    def _resolve_field_name(self, fd: str) -> Optional[Tuple[str, str]]:
        if not fd:
            return None
        name = fd.strip()
        if name.startswith("="):
            # An expression dimension. Many are just a field with an
            # ``=`` prefix (``=HCP_Name``); resolve those directly.
            # Genuinely calculated ones (functions / operators) are
            # handled by _resolve_dimension_expr (calc column).
            name = self._strip_dim_expr(name)
            if not re.fullmatch(r"\[?[A-Za-z_][\w .\-]*\]?", name or ""):
                return None
        name = name.strip("[]").strip()
        from .model import _sanitize_column_name  # local import - cyclic
        col_name = _sanitize_column_name(name)
        # Lookup: try raw name first, then the sanitised form (matches
        # whichever side the field_table was seeded with).
        table = (self.model.field_table.get(name)
                 or self.model.field_table.get(col_name))
        if table:
            return (table, col_name)
        # Field is referenced by some visual but lives in no real
        # data-source table. We do NOT synthesise a phantom ``Extras``
        # table any more (the engine-current schema refresh in
        # engine_fetch should already include every real field). The
        # visual's reference to this field is dropped instead -- the
        # visual still renders but the missing column is omitted from
        # its field well. Caller decides whether to skip the whole
        # visual.
        return None

    def _resolve_dimension_expr(
        self, fd: str, labels: Optional[List[str]] = None,
    ) -> Optional[Tuple[str, str]]:
        """Synthesise a calculated COLUMN for a Qlik expression dimension
        (``=MonthName(Date)`` etc.) and return ``(table, column)`` so the
        visual binds its category/legend to it.

        Only row-level expressions are accepted: if the translation
        aggregates (SUM/COUNT/CALCULATE/...) or stubs, we bail and the
        dimension is dropped (no phantom blank well). Idempotent --
        re-resolving the same expression reuses the column it made."""
        raw = (fd or "").strip()
        if not raw.startswith("="):
            return None
        expr = self._strip_dim_expr(raw)
        if not expr:
            return None
        # A bare field after stripping is handled by _resolve_field_name.
        if re.fullmatch(r"\[?[A-Za-z_][\w .\-]*\]?", expr):
            return None

        # Home table for the calc column. A DAX calculated column may only
        # reference columns ON ITS OWN TABLE: a cross-table bare ref (e.g.
        # ``'Cost Type'[Name]`` inside a Facts calc column) raises *"A single
        # value for column ... cannot be determined"* at query time -- and
        # since the converter makes every relationship many-to-many, even
        # ``RELATED()`` couldn't resolve it. So pick the table that owns EVERY
        # referenced field, not just the first field's primary owner. Example:
        # ``[Cost Type ID] & ' - ' & [Cost Type Name]`` -- ``Cost Type ID`` is
        # on {Facts, Cost Type} (a shared key) but ``Cost Type Name`` only on
        # {Cost Type}, so the intersection {Cost Type} is the home; binding it
        # to Facts (the first field's owner) is what produced the error.
        from .model import _sanitize_column_name
        owners: Dict[str, set] = {}
        cols_by_table: Dict[str, set] = {}
        for t in self.model.tables:
            tn = t.get("name")
            cset = cols_by_table.setdefault(tn, set())
            for c in t.get("columns") or []:
                cn = c.get("name")
                if cn and not c.get("expression"):   # real data columns only
                    owners.setdefault(cn.lower(), set()).add(tn)
                    cset.add(cn.lower())
        ref_owner_sets = []
        for bracketed, bare in re.findall(r"\[([^\[\]]+)\]|([A-Za-z_][\w.]*)", expr):
            cand = (bracketed or bare).strip()
            if not cand:
                continue
            oset = (owners.get(cand.lower())
                    or owners.get(_sanitize_column_name(cand).lower()))
            if oset:
                ref_owner_sets.append(oset)
        if not ref_owner_sets:
            return None
        # A table owning ALL referenced fields if one exists (the common
        # single-table case trivially yields itself); else fall back to the
        # first field's owner (a genuine cross-table expr -- may still stub).
        common = set.intersection(*ref_owner_sets)
        home = sorted(common)[0] if common else sorted(ref_owner_sets[0])[0]

        # Name the calc column from its label, else a slug of the expr.
        base = ""
        if labels:
            base = clean_label(labels[0]) if labels[0] else ""
        if not base:
            base = _derive_inline_measure_label(expr) or "Calc"
        col_name = self._unique_measure_label(base)

        # Reuse an identical calc column if we already made one.
        cache = getattr(self, "_calc_col_cache", None)
        if cache is None:
            cache = self._calc_col_cache = {}
        key = (home, expr)
        if key in cache:
            return (home, cache[key])

        # Home-aware field resolver: a field that EXISTS on the home table
        # qualifies to the home (not its global primary owner), so a shared
        # key like ``Cost Type ID`` -- on both Facts and Cost Type -- binds to
        # the calc column's own table rather than re-introducing the
        # cross-table reference. Other fields delegate to the global resolver
        # (measures, materialised vars, genuinely foreign columns).
        _base_resolver = self.model._make_field_resolver()
        _home_cols = cols_by_table.get(home, set())

        def _calc_resolver(name: str) -> Optional[str]:
            san = _sanitize_column_name(name)
            if name.lower() in _home_cols or san.lower() in _home_cols:
                return f"'{home}'[{san}]"
            return _base_resolver(name)

        dax = translate_qlik_to_dax(
            expr, home,
            variable_lookup=self._var_lookup,
            measure_lookup=self._measure_lookup,
            field_resolver=_calc_resolver,
        )
        if dax.startswith("BLANK() /* qlik:"):
            return None
        # A calculated column is row-level: reject any aggregation /
        # filter-context function -- those only belong in a measure.
        if re.search(
            r"\b(SUM|SUMX|AVERAGE|AVERAGEX|MIN|MINX|MAX|MAXX|COUNT|COUNTA"
            r"|COUNTAX|COUNTX|DISTINCTCOUNT|CALCULATE|MEDIAN|PERCENTILE\.INC)\s*\(",
            dax, re.IGNORECASE,
        ):
            return None

        # A month/year-level label (``FORMAT(<date>, "MMM yyyy")`` etc.)
        # is TEXT, so PBI would sort the axis alphabetically (Apr, Aug,
        # Dec, ...) instead of chronologically -- the same intent Qlik
        # encodes via the dimension's ``qSortByExpression`` on the date.
        # Synthesise a hidden integer sort key at the SAME granularity as
        # the label (``YEAR*100+MONTH`` is 1:1 with "MMM yyyy", which PBI
        # requires for ``sortByColumn``) and point the label's TMDL
        # ``sortByColumn`` at it. Sorting by the date column itself would
        # violate PBI's 1:1 rule (many days per month label).
        sort_by_col = self._derive_chrono_sort_key(home, dax)

        # Attach the calc column to the home table.
        for t in self.model.tables:
            if t["name"] == home:
                col_entry: Dict[str, Any] = {
                    "name":       col_name,
                    "dataType":   "string",
                    "expression": dax,        # signals a calculated column
                    "summarizeBy": "none",
                }
                if sort_by_col:
                    col_entry["sortByColumn"] = sort_by_col
                t["columns"].append(col_entry)
                self._register_name(col_name)
                break
        else:
            return None
        self.model.field_table.setdefault(col_name, home)
        cache[key] = col_name
        return (home, col_name)

    # Month/year tokens in a FORMAT pattern. A day-level token (d/dd)
    # means the label is already daily and chronological text-sort is
    # acceptable, so we only build a key when month-grain or coarser.
    _CHRONO_FMT_RE = re.compile(
        r'^FORMAT\(\s*(?P<ref>.+?)\s*,\s*"(?P<fmt>[^"]*)"\s*\)$',
        re.IGNORECASE | re.DOTALL,
    )

    def _derive_chrono_sort_key(self, home: str, dax: str) -> Optional[str]:
        """For a month/year-level ``FORMAT(<dateref>, "<fmt>")`` calc
        column, synthesise (once) a hidden integer column that sorts the
        label chronologically, and return its name to use as the label's
        ``sortByColumn``. Returns ``None`` when the DAX is not a
        month-grain date format (in which case no key is emitted)."""
        m = self._CHRONO_FMT_RE.match((dax or "").strip())
        if not m:
            return None
        ref = m.group("ref").strip()
        fmt = m.group("fmt")
        fmt_l = fmt.lower()
        has_month = "m" in fmt_l                      # M / MM / MMM / MMMM
        has_year = "y" in fmt_l
        has_day = "d" in fmt_l                         # daily already sortable
        # Only month-grain labels (month present, no day) need a key.
        # Pure-year labels sort fine as text; daily labels too.
        if not has_month or has_day:
            return None
        # The sort key must be 1:1 with the label. ``YEAR*100+MONTH``
        # matches a "month within year" label ("MMM yyyy", "MMMM yyyy");
        # a year-less month label ("MMM") repeats across years, so key on
        # MONTH alone there.
        if has_year:
            key_dax = f"YEAR({ref}) * 100 + MONTH({ref})"
            key_base = "Month Sort Key"
        else:
            key_dax = f"MONTH({ref})"
            key_base = "Month Number"
        cache = getattr(self, "_chrono_key_cache", None)
        if cache is None:
            cache = self._chrono_key_cache = {}
        ckey = (home, key_dax)
        if ckey in cache:
            return cache[ckey]
        key_name = self._unique_measure_label(key_base)
        for t in self.model.tables:
            if t["name"] == home:
                t["columns"].append({
                    "name":        key_name,
                    "dataType":    "int64",
                    "expression":  key_dax,
                    "summarizeBy": "none",
                    "isHidden":    True,
                })
                self._register_name(key_name)
                break
        else:
            return None
        self.model.field_table.setdefault(key_name, home)
        cache[ckey] = key_name
        return key_name

    def _unique_measure_label(self, base: str) -> str:
        """Sanitise + dedupe a measure name against every existing
        measure and column (case-insensitive, as PBI requires)."""
        from .model import _sanitize_measure_name as _smn
        label = _smn(base) or "Measure"
        reserved_ci = self._reserved_names()
        if label.lower() in reserved_ci:
            b, i = label, 2
            label = f"{b} ({i})"
            while label.lower() in reserved_ci:
                i += 1
                label = f"{b} ({i})"
        return label

    def _resolve_measure(
        self,
        meas_block: Dict[str, Any],
        force_measure: bool = False,
    ) -> Optional[Tuple[str, str, bool]]:
        # Library-linked measure -- always becomes a DAX measure in the
        # model so it's reusable from other visuals + the user can edit
        # it by name in PBI.
        lib_id = meas_block.get("qLibraryId")
        if lib_id and lib_id in self.model.measure_by_id:
            mname = self.model.measure_by_id[lib_id]
            # Home on the measure's own table. Fall back to the first
            # REAL table (never the literal "Data", which may not exist
            # as a table -- that would make the projection's Entity point
            # at nothing and render the visual empty).
            return (self._measure_home(mname), mname, True)

        qdef = meas_block.get("qDef") or {}
        if not self.model.tables:
            return None

        expr = (qdef.get("qDef") or "").strip()

        # Safety net: a bare master-measure id passed as an inline
        # expression. Some map size / colour blocks reference a
        # ``libraryItem`` by its id (``key``) rather than via
        # ``qLibraryId``; if that id reaches us as the expression, bind
        # to the already-built measure instead of letting the translator
        # treat the id as a field and synthesise a dangling
        # ``'Table'[<id>]`` measure (the "Measure YPCHRbB =
        # 'Accounts'[YPCHRbB]" failure). Master-measure ids are random
        # tokens, so a real Qlik expression never collides with one.
        bare_id = expr.lstrip("=").strip()
        if bare_id and bare_id in self.model.measure_by_id:
            mname = self.model.measure_by_id[bare_id]
            return (self._measure_home(mname), mname, True)

        # Native-aggregation fast path: when the inline expression is
        # a recognisable simple aggregation of ONE field (``Sum(X)``,
        # ``Count(X)``, ``Avg(X)``, ``Min(X)``, ``Max(X)``,
        # ``Count(distinct X)``), bind PBI's built-in column aggregation
        # to the underlying column instead of synthesising a DAX
        # measure. Keeps the model clean -- Qlik users routinely drop
        # raw fields with an aggregation onto a chart and don't expect
        # those to surface as named measures in the PBI field well.
        native = self._native_aggregation_projection(expr)
        if native is not None:
            if not force_measure:
                return native
            # Value-only visuals (cardVisual / gauge) require a real
            # Measure in their data slot -- promote the native aggregation
            # to a synthesised DAX measure.
            return self._promote_native_agg_to_measure(native, qdef, expr)

        # Inline measure (composite expression) -- synthesise a real DAX
        # measure into the model so the visual has something concrete to
        # bind to. Anonymous inline measures are how Qlik's auto-charts
        # and KPI cards carry their numbers; column refs cannot reference
        # an expression like ``Sum(A)/Sum(B)`` or a set-analysis block.
        return self._synthesise_inline_measure(qdef, expr)

    # ------------------------------------------------------------------
    def _promote_native_agg_to_measure(
        self,
        native: Tuple[str, str, bool],
        qdef: Dict[str, Any],
        expr: str,
    ) -> Tuple[str, str, bool]:
        """Promote a native-aggregation projection (``(table, col, False)``)
        into a synthesised DAX measure.

        Used for value-only visuals (cardVisual / gauge) whose data slot
        rejects a Column binding -- a bare or aggregated Column makes the
        card render empty / error, so the same ``Sum(X)`` becomes a real
        ``SUM('T'[X])`` measure. Behaviour is identical to the inline
        block this was extracted from."""
        home, col, _ = native
        pbi_agg = (getattr(self, "_column_aggregations", {}) or {}).get(
            (home, col), "Sum"
        )
        dax_fn = {
            "Sum": "SUM", "Average": "AVERAGE", "Min": "MIN",
            "Max": "MAX", "Count": "COUNT", "CountNonNull": "COUNT",
        }.get(pbi_agg, "SUM")
        dax = f"{dax_fn}('{home}'[{col}])"
        base = (
            qdef.get("qLabel")
            or clean_label(qdef.get("qLabelExpression", ""))
            or _derive_inline_measure_label(expr)
            or col
        )
        label = self._unique_measure_label(base)
        self.model.measures.append({
            "name":         label,
            "table":        home,
            "expression":   dax,
            "formatString": "",
            "source":       expr,
        })
        self._register_name(label)
        self._measure_home_cache[label] = home
        return (home, label, True)

    # ------------------------------------------------------------------
    def _inline_measure_home(
        self, expr: str,
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """Resolve the home table for an inline-measure expression.

        Returns ``(home, operand_field_raw, operand_field_sanitised)``.
        The home is the OWNING table of the first referenced real field;
        when the operand isn't a known column on any data table we home
        the measure on the first real table so the TMDL still parses
        (the DAX body then typically falls through to the BLANK() stub,
        but the report loads). Extracted verbatim from the inline path."""
        from .model import _sanitize_column_name
        _ft = self.model.field_table
        operand_field_raw = _extract_first_field(
            expr,
            is_known=lambda n: n in _ft or _sanitize_column_name(n) in _ft,
        )
        operand_field = (
            _sanitize_column_name(operand_field_raw)
            if operand_field_raw else None
        )
        home: Optional[str] = None
        if operand_field_raw:
            home = (self.model.field_table.get(operand_field_raw)
                    or self.model.field_table.get(operand_field))
        if not home:
            home = self.model.tables[0]["name"]
        return home, operand_field_raw, operand_field

    def _synthesise_inline_measure(
        self,
        qdef: Dict[str, Any],
        expr: str,
    ) -> Tuple[str, str, bool]:
        """Synthesise a real DAX measure from an anonymous inline Qlik
        expression and append it to the model.

        Pure extraction of the inline-measure tail of ``_resolve_measure``:
        derive + sanitise the label, resolve the home table, dedupe the
        name against every existing measure/column, translate the body
        (with the operand name sanitised so the emitted column ref matches
        the on-disk TMDL column), and register the new measure."""
        label = (
            qdef.get("qLabel")
            or clean_label(qdef.get("qLabelExpression", ""))
            or _derive_inline_measure_label(expr)
        )
        # Strip DAX-forbidden characters (``()[]{};`` etc.) from the
        # measure name. Qlik's default labels are the raw expression --
        # e.g. ``Count(distinct [FieldX])`` -- and PBI silently rejects
        # a measure whose name contains those characters, leaving every
        # referencing visual empty.
        from .model import _sanitize_measure_name as _smn
        label = _smn(label)

        # Home table from the FIRST referenced field (see helper).
        home, operand_field_raw, operand_field = self._inline_measure_home(expr)

        # Dedupe against existing measure and column names — PBI rejects
        # the model when a new measure shadows an existing column, and
        # the uniqueness check is CASE-INSENSITIVE. Compare lower-cased.
        # Shared, incrementally-maintained set (see ``_reserved_names``)
        # instead of an O(measures x columns) rebuild per inline measure.
        reserved_ci = self._reserved_names()
        base = label
        if label.lower() in reserved_ci:
            from .model import _aggregated_measure_name
            preferred = _aggregated_measure_name(label, expr)
            if preferred and preferred.lower() not in reserved_ci:
                label = preferred
            else:
                i = 2
                label = f"{base} ({i})"
                while label.lower() in reserved_ci:
                    i += 1
                    label = f"{base} ({i})"

        # Translate. The validator inside translate_qlik_to_dax stubs
        # anything we cannot turn into legal DAX so the model loads.
        # The translator receives the SANITISED operand name when there
        # is one, so its emitted column ref matches what's in TMDL.
        var_lookup = self._var_lookup
        # Substitute the sanitised name in the source expression before
        # translation so the translator's column-ref output matches
        # the on-disk column.
        translate_src = expr
        if operand_field_raw and operand_field and operand_field_raw != operand_field:
            translate_src = expr.replace(operand_field_raw, operand_field)
        dax = translate_qlik_to_dax(
            translate_src, home,
            variable_lookup=var_lookup,
            measure_lookup=self._measure_lookup,
            field_resolver=self.model._make_field_resolver(),
        )
        self.model.measures.append({
            "name":         label,
            "table":        home,
            "expression":   dax,
            "formatString": "",
            "source":       expr,
        })
        self._register_name(label)
        self._measure_home_cache[label] = home
        return (home, label, True)

    def _native_aggregation_projection(
        self, expr: str,
    ) -> Optional[Tuple[str, str, bool]]:
        """Return ``(table, column, False)`` for a simple
        ``Sum(Field)`` / ``Count(distinct Field)`` / ``Avg(Field)`` /
        etc. expression -- so the caller can bind PBI's built-in
        column aggregation directly instead of synthesising a DAX
        measure. Returns ``None`` for anything more complex.

        The third tuple element is ``False`` to signal "column, not
        measure" to the projection emitter. The aggregation function
        is encoded into the projection by setting ``aggregation`` on
        the field block -- see ``_projection`` where ``aggregation_fn``
        is honoured.

        Tracks the chosen aggregation by stashing it on ``self`` keyed
        by the column name; the projection emitter reads it back.
        """
        import re
        from .model import _sanitize_column_name as _san
        if not expr:
            return None
        # Strip leading "=" Qlik occasionally prefixes inline measures with.
        src = expr.strip()
        if src.startswith("="):
            src = src[1:].strip()
        # Bare ``[Field]`` or ``Field`` with no aggregation -- bind
        # the column with the column's natural type (PBI defaults to
        # Sum for numeric, Count for text).
        m = re.match(r"^\[?([A-Za-z_][A-Za-z0-9_. \-]{0,80}?)\]?\s*$", src)
        if m:
            field = m.group(1).strip()
            home = (self.model.field_table.get(field)
                    or self.model.field_table.get(_san(field)))
            if home:
                return (home, _san(field), False)
            return None
        # Simple <Agg>(<field>) or <Agg>(distinct <field>).
        m = re.match(
            r"^(Sum|Count|Avg|Min|Max|Only|First|Last)\s*\(\s*"
            r"(?:distinct\s+)?"
            r"\[?([A-Za-z_][A-Za-z0-9_. \-]{0,80}?)\]?\s*\)\s*$",
            src, re.IGNORECASE,
        )
        if not m:
            return None
        agg = m.group(1).lower()
        is_distinct = bool(re.search(r"\(\s*distinct\b", src, re.IGNORECASE))
        # Distinct-count cannot ride PBI's native column-aggregation
        # ``Aggregation.Function`` enum: that enum has no portable slot
        # for DistinctCount (PBI versions disagree on the value, and
        # the slot we used to emit -- ``5`` -- actually means
        # ``CountNonNull`` in the visualContainerObjects schema, so
        # ``Count(distinct X)`` was being rendered as a Count-of-
        # non-null rather than a distinct count. Defer those to the
        # DAX-measure synthesis path below, which produces an
        # unambiguous ``DISTINCTCOUNT('<table>'[<col>])`` measure.
        if is_distinct and agg == "count":
            return None
        field = m.group(2).strip()
        if not field:
            return None
        home = (self.model.field_table.get(field)
                or self.model.field_table.get(_san(field)))
        if not home:
            return None
        column_name = _san(field)
        # Stash the aggregation so ``_projection`` can pick it up when
        # the caller emits this binding into a visual.
        if not hasattr(self, "_column_aggregations"):
            self._column_aggregations: Dict[Tuple[str, str], str] = {}
        # Sum/Avg/Min/Max map to PBI's standard column-aggregation enum.
        pbi_agg = {
            "sum":   "Sum",
            "avg":   "Average",
            "min":   "Min",
            "max":   "Max",
            "count": "CountNonNull",
            "only":  "Min",       # closest PBI equivalent
            "first": "Min",
            "last":  "Max",
        }[agg]
        self._column_aggregations[(home, column_name)] = pbi_agg
        return (home, column_name, False)

    @property
    def _var_lookup(self):
        """Lazy-build the Qlik variable -> definition lookup used by the
        DAX translator. Variables that were materialised as DAX measures
        (see ``model._materialize_variables_as_measures``) return the
        bracketed measure-ref ``[varX]`` instead of the raw body, so
        master measures/dimensions reference the measure directly
        instead of inlining the body in every consumer."""
        if not hasattr(self, "__var_lookup"):
            defs = {}
            for v in self.ir.get("variables", []) or []:
                vn = (v.get("qName") or "").strip()
                vd = v.get("qDefinition") or ""
                if vn:
                    defs[vn] = vd
            mat = getattr(self.model, "materialized_vars", {}) or {}

            def _lookup(name: str) -> Optional[str]:
                if name in mat:
                    return f"[{mat[name]}]"
                return defs.get(name)

            self.__var_lookup = _lookup
        return self.__var_lookup

    @property
    def _measure_lookup(self):
        """Resolver-side measure lookup. Returns the bare measure name
        for any name PBI would treat as a measure -- materialised
        variables AND library-bound measures already in the model.
        Used by the translator to emit ``[Name]`` (bare measure ref)
        instead of ``'<table>'[Name]`` (column ref) when a reference
        actually targets a measure."""
        if not hasattr(self, "__meas_lookup"):
            # Names that are ALSO real columns must resolve as COLUMN
            # refs, never measure refs -- otherwise a bare/bracketed field
            # reference whose name coincides with a measure gets
            # mis-emitted as ``[Name]`` (e.g. a measure auto-named after a
            # field). Measure-vs-column dedup normally prevents the
            # overlap, but exclude column names defensively so a bare
            # reference always binds to the column when one exists.
            col_names_ci = set()
            for t in self.model.tables:
                for c in t["columns"]:
                    cn = (c.get("name") or "").lower()
                    if cn:
                        col_names_ci.add(cn)
            names_ci = set()
            for m in self.model.measures:
                n = (m.get("name") or "").lower()
                if n and n not in col_names_ci:
                    names_ci.add(n)

            def _lookup(name: str) -> Optional[str]:
                return name if (name or "").lower() in names_ci else None

            self.__meas_lookup = _lookup
        return self.__meas_lookup

    def _projection(self, table: str, name: str, is_measure: bool) -> Dict[str, Any]:
        """Emit one projection entry.

        Shape mirrors what PBI Desktop emits when it saves a project:

            {
              "field": {
                "Column"|"Measure": {
                  "Expression": {"SourceRef": {"Entity": "<table>"}},
                  "Property":   "<column or measure name>"
                }
              },
              "queryRef":       "<table>.<name>",
              "nativeQueryRef": "<name>",
              "active":         true,
              "displayName":    "<name>"
            }

        Note: ``Entity`` is the canonical key inside ``SourceRef`` -- not
        ``Source``. The latter is reserved for explicit query aliases
        (subqueries / VisualTopN); using it at the projection root
        causes the visual to silently fail at load.
        """
        kind = "Measure" if is_measure else "Column"
        field_inner: Dict[str, Any] = {
            "Expression": {"SourceRef": {"Entity": table}},
            "Property":   name,
        }
        # Native column aggregation: when this projection was registered
        # by ``_native_aggregation_projection`` we wrap the Column
        # entry in an ``Aggregation`` block carrying the PBI function
        # enum. This matches what PBI Desktop emits when a user drops
        # a numeric column into a value well -- no DAX measure needed.
        proj: Dict[str, Any] = {
            "queryRef":       f"{table}.{name}",
            "nativeQueryRef": name,
            "active":         True,
            "displayName":    name,
        }
        # Sort direction from Qlik qSortBy is NOT emitted at the
        # projection level (PBI's schema rejects ``sortDirection``
        # there). It is collected per-visual in ``_current_sort_specs``
        # and rendered into a ``visual.query.sortDefinition`` block
        # after the queryState is assembled.
        agg_fn = None
        if not is_measure:
            agg_map = getattr(self, "_column_aggregations", {}) or {}
            agg_fn = agg_map.get((table, name))
        if agg_fn:
            # PBI's column-aggregation enum (IQueryAggregateFunction):
            #   Sum=0, Avg=1, Count=2, Min=3, Max=4, CountNonNull=5,
            #   StandardDeviation=6, Variance=7, Median=8.
            #
            # Note there is intentionally NO ``CountDistinct`` entry --
            # distinct count is unambiguously expressed by synthesising
            # a DAX ``DISTINCTCOUNT(...)`` measure, not a native column
            # aggregation. Callers route ``Count(distinct X)`` to the
            # measure-synthesis path (see _native_aggregation_projection).
            _AGG_TO_PBI_NUM = {
                "Sum": 0, "Average": 1, "Count": 2, "Min": 3, "Max": 4,
                "CountNonNull": 5,
            }
            agg_num = _AGG_TO_PBI_NUM.get(agg_fn, 0)
            proj["field"] = {"Aggregation": {
                "Expression": {"Column": field_inner},
                "Function":   agg_num,
            }}
            # Friendlier display name: "Sum of X", "Count of X" etc.
            display = {
                "Sum": "Sum of", "Average": "Average of", "Count": "Count of",
                "Min": "Min of", "Max": "Max of",
                "CountNonNull": "Count of",
            }.get(agg_fn, "")
            if display:
                proj["displayName"] = f"{display} {name}"
        else:
            proj["field"] = {kind: field_inner}
        return proj


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scale(percent: float, full: int) -> int:
    try:
        return int(round(float(percent) * full / 100.0))
    except (ValueError, TypeError):
        return 0


def _sort_direction_from_qlik(qd: Dict[str, Any]) -> Optional[str]:
    """Read a Qlik dimension/measure block's sort intent and return the
    PBI sort direction string (``"Ascending"`` / ``"Descending"``) or
    ``None`` when no explicit, directional sort was set.

    Qlik's two block shapes store sort in DIFFERENT places (per the
    Engine API ``NxDimension`` / ``NxMeasure`` schemas), and the earlier
    version only read a singular ``qSortBy`` -- which **dimensions do not
    have** -- so dimension sorting was silently dropped:

      * **Dimension** (``NxDimension``): a LIST at
        ``qDef.qSortCriterias[]`` (each a ``SortCriteria``), with
        ``qDef.qReverseSort`` flipping the result. The first criteria
        with a directional flag wins.
      * **Measure** (``NxMeasure``): a single ``SortCriteria`` at the
        block-level ``qSortBy`` (sibling of ``qDef``).
      * Legacy fallback: ``qDef.qSortBy``.

    Within a ``SortCriteria`` we honour, in priority order,
    ``qSortByNumeric`` -> ``qSortByAscii`` -> ``qSortByExpression``
    (tri-state: -1 desc, 0 unset, 1 asc). ``qSortByLoadOrder`` and
    ``qSortByFrequency`` are deliberately NOT mapped -- PBI has no
    load-order / frequency sort, and coercing them to alphabetical would
    re-order the data wrongly; we leave PBI's default instead.
    """
    if not isinstance(qd, dict):
        return None
    qdef = qd.get("qDef") or {}
    reverse = bool(qdef.get("qReverseSort"))

    # Collect candidate SortCriteria objects in resolution priority.
    criterias: List[Dict[str, Any]] = []
    if isinstance(qd.get("qSortBy"), dict):            # measure (sibling)
        criterias.append(qd["qSortBy"])
    sc = qdef.get("qSortCriterias")                    # dimension (list)
    if isinstance(sc, list):
        criterias.extend(c for c in sc if isinstance(c, dict))
    elif isinstance(sc, dict):
        criterias.append(sc)
    if isinstance(qdef.get("qSortBy"), dict):          # legacy
        criterias.append(qdef["qSortBy"])

    for sb in criterias:
        for key in ("qSortByNumeric", "qSortByAscii", "qSortByExpression"):
            v = sb.get(key)
            if isinstance(v, (int, float)) and v != 0:
                ascending = v > 0
                if reverse:
                    ascending = not ascending
                return "Ascending" if ascending else "Descending"
    return None


# Qlik text-image markdown markers used in the cell's ``markdown`` field:
#
#   ^[<content>](center|left|right)   <- horizontal alignment
#   #[<content>]({...json style...})  <- run with explicit color
#   %[<content>](<n>)                 <- size index
#   **bold**     *italic*             <- standard markdown
#
# Qlik nests these freely (the canonical form is
# ``^[#[%[**Text**](4)]({"color":"#191919"})](center)``). We strip the
# wrappers in order from outside in, accumulating style attributes
# onto the resulting text-run dicts. Pragmatic -- exact font-size
# mapping isn't worth recreating; carrying colour + bold + alignment +
# the actual text is enough to keep titles readable in PBI.
#
# The body-matching regex uses lazy `+?` plus a tail anchor so the
# outer marker doesn't greedily swallow inner markers' closing
# parens. The size / style markers' "argument" half is restricted to
# `[^()]*` so nested-paren content doesn't confuse boundary detection.

_QLIK_ALIGN_RE = __import__("re").compile(
    r"^\s*\^\[(?P<body>[\s\S]+)\]\s*\(\s*(?P<align>center|left|right|justify)\s*\)\s*$",
    __import__("re").IGNORECASE,
)
_QLIK_STYLE_RE = __import__("re").compile(
    r"^\s*\#\[(?P<body>[\s\S]+)\]\s*\(\s*(?P<json>\{[^{}]*\})\s*\)\s*$",
)
_QLIK_SIZE_RE = __import__("re").compile(
    r"^\s*\%\[(?P<body>[\s\S]+)\]\s*\(\s*(?P<size>-?\d+)\s*\)\s*$",
)

# Heading-size index -> approximate PBI font-size (pt). Qlik's size
# index inverts the typical HTML semantics (4 ~= h4); these are pulled
# from inspecting saved Qlik apps and Power BI's textbox renderer.
_QLIK_SIZE_TO_PT = {-2: 36, -1: 28, 0: 24, 1: 22, 2: 20, 3: 18, 4: 16, 5: 14}

# Lexical format bitmask (same values as the Lexical editor source).
_LEX_BOLD = 1
_LEX_ITALIC = 2
_LEX_UNDERLINE = 8


def _lexical_style_to_textrun_style(
    inline_css: str,
    fmt_bitmask: int,
) -> Dict[str, Any]:
    """Build a PBI ``textStyle`` dict from a Lexical node's CSS ``style``
    attribute and ``format`` bitmask.  Returns an empty dict when nothing
    meaningful is present."""
    ts: Dict[str, Any] = {}
    if inline_css:
        m = re.search(r"font-size:\s*(\d+(?:\.\d+)?)px", inline_css)
        if m:
            pt = max(6, min(96, int(round(float(m.group(1)) * 0.75))))
            ts["fontSize"] = f"{pt}pt"
        m = re.search(r"color:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]+\))", inline_css)
        if m:
            c = _css_color_to_hex(m.group(1))
            if c:
                ts["fontColor"] = c
    if fmt_bitmask & _LEX_BOLD:
        ts["fontWeight"] = "bold"
    if fmt_bitmask & _LEX_ITALIC:
        ts["italic"] = True
    if fmt_bitmask & _LEX_UNDERLINE:
        ts["underline"] = True
    return ts


def _formatstyle_to_textrun_style(fmt_style: Dict[str, Any]) -> Dict[str, Any]:
    """Build a PBI ``textStyle`` dict from a Lexical ``formatStyle`` object
    (used on ``qlik.expression.node`` nodes)."""
    ts: Dict[str, Any] = {}
    if fmt_style.get("fontWeight") == "bold":
        ts["fontWeight"] = "bold"
    if fmt_style.get("fontStyle") == "italic":
        ts["italic"] = True
    sz = str(fmt_style.get("fontSize") or "")
    m = re.search(r"(\d+(?:\.\d+)?)px", sz)
    if m:
        pt = max(6, min(96, int(round(float(m.group(1)) * 0.75))))
        ts["fontSize"] = f"{pt}pt"
    col = fmt_style.get("color") or ""
    if col:
        c = _css_color_to_hex(col)
        if c:
            ts["fontColor"] = c
    return ts


def _lexical_to_paragraphs(
    text_json: str,
    props: Dict[str, Any],
    lookup: Optional[Callable[[str, Any], Optional[str]]] = None,
) -> List[Dict[str, Any]]:
    """Convert Qlik's ``sn-text`` Lexical JSON string to PBI textbox
    paragraphs.

    Static text nodes (``type: "text"``) are carried over verbatim with
    their CSS colour/size and Lexical format bitmask (bold/italic) mapped
    to PBI ``textStyle``.

    Expression nodes (``type: "qlik.expression.node"``) resolve through
    ``lookup(cId, rawExpr)`` -- the engine-evaluated snapshot captured
    at unbuild time, falling back to local static evaluation (see
    ``ReportBuilder._lookup_text_expr``) -- so the converted textbox
    shows the VALUE Qlik rendered. Only when nothing resolves does the
    measure's label (or its expression string) substitute, keeping the
    textbox human-readable rather than blank.
    """
    import json as _json  # local import -- only called from textbox builder

    # expressionId -> (label fallback, raw expression) from the
    # hypercube measures.
    hc = props.get("qHyperCubeDef") or {}
    expr_label: Dict[str, str] = {}
    expr_raw: Dict[str, str] = {}
    for meas in hc.get("qMeasures") or []:
        mdef = meas.get("qDef") or {}
        cid = mdef.get("cId", "")
        if cid:
            raw = mdef.get("qDef", "")
            if isinstance(raw, str) and raw.strip():
                expr_raw[cid] = raw.strip()
            lbl = (
                clean_label(mdef.get("qLabel", ""))
                or clean_label(mdef.get("qLabelExpression", ""))
                or clean_label(raw)
                or "[expression]"
            )
            expr_label[cid] = lbl[:4000]

    try:
        tree = _json.loads(text_json)
    except (ValueError, TypeError):
        return [{"textRuns": [{"value": ""}]}]

    root = tree.get("root") or {}
    paragraphs: List[Dict[str, Any]] = []

    for para_node in root.get("children") or []:
        if para_node.get("type") not in ("paragraph", "heading", "quote"):
            continue
        runs: List[Dict[str, Any]] = []
        for node in para_node.get("children") or []:
            ntype = node.get("type", "")
            if ntype == "text":
                txt = node.get("text", "")
                if not txt:
                    continue
                ts = _lexical_style_to_textrun_style(
                    node.get("style", ""), node.get("format", 0),
                )
                run: Dict[str, Any] = {"value": txt}
                if ts:
                    run["textStyle"] = ts
                runs.append(run)
            elif ntype == "qlik.expression.node":
                eid = node.get("expressionId", "")
                value: Optional[str] = None
                if lookup is not None:
                    value = lookup(eid, expr_raw.get(eid))
                if value is None:
                    value = expr_label.get(eid, "[expression]")
                ts = _formatstyle_to_textrun_style(
                    node.get("formatStyle") or {},
                )
                run = {"value": value}
                if ts:
                    run["textStyle"] = ts
                runs.append(run)
            elif ntype == "linebreak":
                # Soft line-break within a paragraph -- emit as empty run.
                runs.append({"value": ""})

        if not runs:
            runs = [{"value": ""}]
        para: Dict[str, Any] = {"textRuns": runs}
        # Paragraph-level text alignment from textStyle CSS.
        para_css = para_node.get("textStyle", "")
        if isinstance(para_css, str) and "text-align:" in para_css:
            m = re.search(r"text-align:\s*(\w+)", para_css)
            if m:
                align = m.group(1).lower()
                if align in ("left", "center", "right"):
                    para["horizontalTextAlignment"] = align
        paragraphs.append(para)

    return paragraphs or [{"textRuns": [{"value": ""}]}]


def _qlik_markdown_to_paragraphs(markdown: str) -> List[Dict[str, Any]]:
    """Convert Qlik's text-image markdown into PBI textbox paragraphs.

    Returns a list with one paragraph per source markdown line (split
    on ``\\n``). Each paragraph carries an optional
    ``horizontalTextAlignment`` and a list of ``textRuns``, each with a
    ``value`` and an optional ``textStyle`` (``fontWeight``, ``fontSize``,
    ``fontColor``, ``italic``).

    Unrecognised content survives as plain text -- never raises.
    """
    out: List[Dict[str, Any]] = []
    for line in (markdown or "").split("\n"):
        line = line.rstrip()
        if not line:
            out.append({"textRuns": [{"value": ""}]})
            continue
        align: Optional[str] = None
        style: Dict[str, Any] = {}

        # Peel off wrappers in a loop until the line stabilises. Qlik
        # nests these in arbitrary order -- canonical is
        # ``^[#[%[**T**](sz)]({json})](align)`` but earlier versions of
        # the Qlik UI emit any layer order, so we test all three each
        # iteration. Cap at 10 iterations to defend against pathological
        # inputs.
        for _ in range(10):
            stripped = False
            m = _QLIK_ALIGN_RE.match(line)
            if m:
                if align is None:
                    align = m.group("align").lower()
                line = m.group("body").strip()
                stripped = True
                continue
            m = _QLIK_STYLE_RE.match(line)
            if m:
                try:
                    import json as _json
                    js = _json.loads(m.group("json"))
                    if isinstance(js, dict):
                        col = js.get("color")
                        if isinstance(col, str) and col:
                            style["fontColor"] = col
                except (ValueError, TypeError):
                    pass
                line = m.group("body").strip()
                stripped = True
                continue
            m = _QLIK_SIZE_RE.match(line)
            if m:
                try:
                    size_idx = int(m.group("size"))
                    if size_idx in _QLIK_SIZE_TO_PT:
                        style["fontSize"] = f"{_QLIK_SIZE_TO_PT[size_idx]}pt"
                except ValueError:
                    pass
                line = m.group("body").strip()
                stripped = True
                continue
            if not stripped:
                break

        runs = _qlik_inline_runs(line, base_style=style)
        para: Dict[str, Any] = {"textRuns": runs or [{"value": ""}]}
        if align:
            para["horizontalTextAlignment"] = align
        out.append(para)
    return out or [{"textRuns": [{"value": ""}]}]


_BOLD_RE = __import__("re").compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = __import__("re").compile(r"(?<!\*)\*([^*]+)\*(?!\*)")


def _qlik_inline_runs(text: str, base_style: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Split a one-line markdown fragment into PBI textRuns, honouring
    ``**bold**`` and ``*italic*``. Bold is matched first because the
    italic regex would otherwise mis-bind ``**bold**`` as italics.
    """
    if not text:
        return []
    # Find bold spans first.
    spans: List[Tuple[int, int, Dict[str, Any]]] = []
    for m in _BOLD_RE.finditer(text):
        spans.append((m.start(), m.end(), {**base_style, "fontWeight": "bold"}))
    # Italics that don't overlap a bold span.
    for m in _ITALIC_RE.finditer(text):
        if any(s <= m.start() < e for s, e, _ in spans):
            continue
        spans.append((m.start(), m.end(), {**base_style, "italic": True}))
    if not spans:
        run: Dict[str, Any] = {"value": text}
        if base_style:
            run["textStyle"] = dict(base_style)
        return [run]
    # Walk the text emitting plain + styled runs.
    spans.sort()
    runs: List[Dict[str, Any]] = []
    cursor = 0
    for s, e, st in spans:
        if s > cursor:
            seg = text[cursor:s]
            r: Dict[str, Any] = {"value": seg}
            if base_style:
                r["textStyle"] = dict(base_style)
            runs.append(r)
        # The styled run's *inner* text strips the marker chars.
        inner = text[s:e].strip("*")
        r2: Dict[str, Any] = {"value": inner, "textStyle": dict(st)}
        runs.append(r2)
        cursor = e
    if cursor < len(text):
        tail = text[cursor:]
        r: Dict[str, Any] = {"value": tail}
        if base_style:
            r["textStyle"] = dict(base_style)
        runs.append(r)
    return runs


_INLINE_FIELD_RE = __import__("re").compile(
    r"\[([^\[\]]+)\]|\b([A-Za-z_][A-Za-z0-9_.]{1,60})\b"
)

# Qlik built-in function names + control keywords that should never
# be treated as field references. Used by `_extract_first_field` to
# skip past function call openings before reaching the real field
# operand. Names compared case-insensitively.
_QLIK_BUILTINS: set = {
    # control flow / logic
    "if", "and", "or", "not", "xor", "true", "false", "null", "isnull",
    "isnum", "istext",
    # aggregations
    "sum", "count", "avg", "min", "max", "only", "first", "last",
    "distinct", "fabs", "stddev", "median", "mode", "concat",
    "rangesum", "rangeavg", "rangemax", "rangemin", "rangecount",
    "rangestdev", "rangefractile",
    # string functions
    "len", "left", "right", "mid", "trim", "ltrim", "rtrim",
    "upper", "lower", "capitalize", "replace", "subfield", "purgechar",
    "keepchar", "index", "substringcount", "text", "chr", "ord",
    "evaluate", "applycodepage",
    # numeric functions
    "round", "abs", "ceil", "floor", "div", "mod", "fmod", "frac",
    "sqr", "sqrt", "exp", "log", "log10", "pow", "sign",
    "num", "interval", "money", "time", "timestamp",
    # date/time functions
    "today", "now", "date", "datestamp", "timestamp", "year", "month",
    "day", "week", "weekday", "hour", "minute", "second", "yearstart",
    "yearend", "monthstart", "monthend", "quarterstart", "quarterend",
    "weekstart", "weekend", "addmonths", "addyears", "adddays",
    "makedate", "maketime", "yeartodate", "yearname", "monthname",
    "dayname", "weekname", "quartername", "age",
    # set / lookup / aggregate functions
    "aggr", "above", "below", "peek", "previous", "before", "after",
    "lookup", "exists", "fieldvalue", "fieldindex", "fieldvaluecount",
    "getfieldselections", "getpossiblecount", "getselectedcount",
    "match", "wildmatch", "mixmatch", "pick", "alt", "coalesce",
    "class", "if", "firstsortedvalue",
    # conditional aggregations
    "rank", "hrank", "row", "rowno", "norows",
    # geo
    "geomakepoint", "applymap", "lower",
    # color / formatting
    "rgb", "argb", "color", "colormix1", "colormix2",
    "white", "black", "red", "green", "blue",
    # other
    "let", "set",
}


def _extract_first_field(
    expr: str,
    is_known: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """Pull the first plausible field reference out of a Qlik measure
    expression. Used to home a measure on the right table.

    Strategy:
      1. Strip set-analysis modifier blocks (``{...}``). Field names
         appearing inside them are FILTERS, not the measure's primary
         operand -- they would home the measure on the wrong table.
      2. Prefer the first ``[Bracketed Name]`` token, since brackets
         disambiguate field references from function names in Qlik.
      3. Fall back to the first bare identifier that isn't a known
         Qlik built-in function or control keyword.

    ``is_known(name) -> bool`` (optional): when supplied, prefer the
    first candidate that is an ACTUAL known field, falling back to the
    first candidate only if none resolve. This stops a leading token
    that looks field-like but isn't a real column (e.g. a dotted
    ``Table.Field`` qualifier, or a token the home-table map doesn't
    carry) from homing the measure on the wrong table / on none.
    """
    if not expr:
        return None
    import re
    from .dax_translator import _strip_comments
    # Strip Qlik comments first. A leading ``//...`` line comment (the
    # expression resumes after the newline) must not contribute its words
    # as candidate field names -- that would home the measure on the
    # wrong table, or on none at all. ``//`` runs to end of line.
    src = _strip_comments(expr)
    if src.startswith("="):
        src = src[1:]
    # Strip set-analysis modifier blocks. They are filter contexts,
    # not the measure's main operand, so fields inside them must not
    # win the home-table lookup. Loop because braces can nest -- a
    # single `re.sub` pass only removes the innermost level.
    src_no_set = src
    for _ in range(10):
        new_src = re.sub(r"\{[^{}]*\}", " ", src_no_set)
        if new_src == src_no_set:
            break
        src_no_set = new_src
    # If stripping set blocks left fields visible, prefer that view;
    # otherwise fall back to the original (some pathological exprs
    # contain only set-block content).
    candidates_pool = src_no_set if "[" in src_no_set or any(
        c.isalpha() for c in src_no_set
    ) else src

    # Collect candidates in priority order: bracketed names first (they
    # are unambiguous in Qlik and survive function-name collisions --
    # "Date" the function vs "[Date]" the field), then bare identifiers
    # filtered against the Qlik built-in name list.
    candidates: List[str] = []
    for m in _INLINE_FIELD_RE.finditer(candidates_pool):
        name = (m.group(1) or "").strip()
        if name and not name.isdigit() and name.lower() not in _QLIK_BUILTINS:
            candidates.append(name)
    for m in _INLINE_FIELD_RE.finditer(candidates_pool):
        name = (m.group(2) or "").strip()
        if name and not name.isdigit() and name.lower() not in _QLIK_BUILTINS:
            candidates.append(name)
    if not candidates:
        return None
    # Prefer the first candidate that is a REAL field, when the caller
    # can tell us; otherwise the first candidate (legacy behaviour).
    if is_known is not None:
        for c in candidates:
            if is_known(c):
                return c
    return candidates[0]


def _derive_inline_measure_label(expr: str) -> str:
    """Synthesise a readable name for an unnamed inline Qlik measure.

    Examples:
        Sum(Patients_Diagnosed)         -> "Sum Patients_Diagnosed"
        Count(distinct [HCO.Territory]) -> "Count Distinct HCO.Territory"
        Count([From_HCP_ID-HCP_ID])     -> "Count From_HCP_ID-HCP_ID"

    The ``distinct`` keyword is preserved in the label because the
    synthesised DAX is ``DISTINCTCOUNT(...)`` -- without the marker
    the user cannot tell a count-of-non-null apart from a count-of-
    distinct value in the field well, and Qlik's source did make
    that distinction.
    """
    import re
    if not expr:
        return "Measure"
    s = expr.strip()
    if s.startswith("="):
        s = s[1:].strip()
    m = re.match(r"^(?P<fn>[A-Za-z_]+)\s*\(", s)
    fn = m.group("fn").capitalize() if m else "Measure"
    inner = s[m.end():-1] if m and s.endswith(")") else s
    is_distinct = bool(
        re.match(r"^\s*distinct\b", inner, re.IGNORECASE)
    )
    inner = re.sub(r"^\s*distinct\s+", "", inner, flags=re.IGNORECASE).strip()
    inner = inner.strip("[]").strip()
    qualifier = " Distinct" if is_distinct else ""
    if not inner:
        return f"{fn}{qualifier}".strip()
    # Limit length so it shows nicely in PBI's field well.
    inner = inner[:48]
    return f"{fn}{qualifier} {inner}"


def _query_state_for_type(
    pbi_type: str,
    cat: List[Dict[str, Any]],
    val: List[Dict[str, Any]],
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Map (visualType, category_projections, value_projections) to the
    `queryState` shape PBI expects: `{slot: {projections: [...]}}`.

    Qlik's hypercube places every dimension in a single ordered list;
    the chart type implicitly assigns positions. The convention we
    follow here (matches Qlik Sense + Power BI defaults):

    * **Bar / column / line**: dim 0 -> X axis, dim 1 -> Legend/Series,
      remaining dims -> Tooltips. Multiple measures -> Y.
    * **Combo**: like bar, but val[0] -> Y (column), val[1:] -> Y2 (line).
    * **Pie / donut / funnel / treemap**: dim 0 -> Category (legend),
      val[0] -> Y/Values, additional dims -> Tooltips.
    * **Scatter**: val[0] -> X, val[1] -> Y, val[2] -> Size,
      dim 0 -> Details (per-bubble identity), dim 1 -> Play axis.
    * **Pivot**: dim 0 -> Rows, dim 1 -> Columns, dim 2+ -> nested
      Rows, measures -> Values.
    * **Map**: dim 0 -> Category (location), dim 1 -> Series,
      val[0] -> Size.
    * **Gauge / KPI / card**: val[0] -> Data; val[1] -> TargetValue
      when available.

    Empty slots are omitted entirely so the visual schema validator
    doesn't reject `{projections: []}` as an unknown shape.
    """
    if pbi_type == "actionButton":
        return {}
    if pbi_type == "cardVisual":
        slots = {
            "Data":        val[:1],
            "TargetValue": val[1:2],
        }
    elif pbi_type == "gauge":
        slots = {
            "Y":           val[:1],
            "MinValue":    val[1:2],
            "MaxValue":    val[2:3],
            "TargetValue": val[3:4],
            "Tooltips":    cat,
        }
    elif pbi_type in ("pieChart", "donutChart", "funnel"):
        slots = {
            "Category": cat[:1],
            "Y":        val[:1],
            "Tooltips": cat[1:] + val[1:],
        }
    elif pbi_type == "treemap":
        slots = {
            "Category": cat[:1],
            "Details":  cat[1:2],     # second dim -> sub-grouping
            "Values":   val[:1],
            "Tooltips": cat[2:] + val[1:],
        }
    elif pbi_type == "tableEx":
        # Table: everything is in Values, dimensions first then measures.
        slots = {"Values": cat + val}
    elif pbi_type == "pivotTable":
        slots = {
            "Rows":    cat[:1] + cat[2:],   # nest extra dims under Rows
            "Columns": cat[1:2],
            "Values":  val,
        }
    elif pbi_type in ("scatterChart", "bubbleChart"):
        slots = {
            "X":        val[:1],
            "Y":        val[1:2],
            "Size":     val[2:3],
            "Details":  cat[:1],
            "Play":     cat[1:2],
            "Tooltips": cat[2:] + val[3:],
        }
    elif pbi_type in (
        "lineClusteredColumnComboChart",
        "lineStackedColumnComboChart",
    ):
        slots = {
            "Category": cat[:1],
            "Series":   cat[1:2],
            "Y":        val[:1],
            "Y2":       val[1:],
            "Tooltips": cat[2:],
        }
    elif pbi_type == "azureMap":
        slots = {
            "Category": cat[:1],   # location
            "Series":   cat[1:2],  # legend grouping
            "Size":     val[:1],
            "Tooltips": cat[2:] + val[1:],
        }
    elif pbi_type == "waterfallChart":
        slots = {
            "Category":   cat[:1],
            "Breakdown":  cat[1:2],
            "Y":          val[:1],
            "Tooltips":   cat[2:] + val[1:],
        }
    elif pbi_type == "slicer":
        slots = {"Values": cat[:1]}
    elif pbi_type == "textbox":
        return {}
    else:
        # Default family: bar / column / line / area / ribbon /
        # clusteredBar / stackedBar / histogram / columnChart, etc.
        # First dim -> X axis, second dim -> Legend, remaining ->
        # Tooltips. All measures stay on Y.
        slots = {
            "Category": cat[:1],
            "Series":   cat[1:2],
            "Y":        val,
            "Tooltips": cat[2:],
        }
    return {
        slot: {"projections": projs}
        for slot, projs in slots.items()
        if projs
    }
