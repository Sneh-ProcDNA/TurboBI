"""Report builder.

Turns parsed worksheets and dashboards into the in-memory tree that the
writer drops onto disk. The cardinal rule: if a visual's field can't be
resolved against the model, drop *that field*, not the visual. Page
layout is what the user wants to see; empty visuals will render as
"no data" tiles in Power BI Desktop, which is fine.

Two visual flavors come out of this module:

    chart visual   — has a query.queryState and projections
    decoration     — textbox / placeholder slicer for filters / images
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    AGG_TABLE,
    DEFAULT_PAGE_HEIGHT,
    DEFAULT_PAGE_WIDTH,
    SCHEMA,
    VISUAL_SLOTS,
)
from .model import SemanticModel
from .utils import hex_id
from .visual_picker import pick_visual_type


# Matches a Tableau (Object!Suffix) disambiguator embedded in a field
# name, e.g. 'Region (Dim!HCO)'. Used to extract a strong table hint
# when computing a worksheet's primary table.
_OBJ_SUFFIX_RE = re.compile(r"\(\s*[^) !]+!\s*([^)]+)\s*\)")


# Aggregations that map to a measure in PBI's projection field block
MEASURE_AGGS = {agg for agg, info in AGG_TABLE.items() if info["is_measure"]}


def _expr_lit(value: str) -> Dict[str, Any]:
    """Wrap a literal value in PBI's expr/Literal envelope.

    PBI property values inside visualContainerObjects/objects use
    {"expr": {"Literal": {"Value": "<v>"}}}. String literals must be
    single-quoted ('foo'); numeric literals use a 'D' suffix (11D);
    booleans are bare 'true'/'false'.
    """
    return {"expr": {"Literal": {"Value": value}}}


def _color_expr(hex_color: str) -> Dict[str, Any]:
    """Wrap a hex color in PBI's solid/color envelope."""
    return {"solid": {"color": _expr_lit(f"'{hex_color}'")}}


# Font families PBI Desktop ships with. Anything not on this list gets
# replaced with Arial — Tableau workbooks routinely use proprietary fonts
# ('Tableau Medium', 'Tableau Book') that PBI silently falls back to a
# default for, producing visually inconsistent output unless we
# substitute up-front.
_PBI_SAFE_FONTS = {
    "arial", "arial black", "arial unicode ms",
    "calibri", "calibri light", "cambria", "cambria math",
    "candara", "comic sans ms", "consolas", "constantia", "corbel",
    "courier new", "din", "din light",
    "georgia", "lato", "lucida sans unicode",
    "segoe", "segoe ui", "segoe ui light", "segoe ui semibold",
    "symbol", "tahoma", "times new roman", "trebuchet ms",
    "verdana", "wingdings",
}


def _safe_font_family(name: Optional[str]) -> str:
    """Return `name` if it's a PBI-shipped font; otherwise 'Arial'.

    PBI Desktop only renders the fonts it ships with. Tableau-specific
    fonts ('Tableau Medium', 'Tableau Bold') silently fall back to a PBI
    default when the file opens, so the visuals never look like Tableau
    intended. Substituting Arial here keeps font sizes / weights consistent
    across the converted report.
    """
    if not name:
        return "Arial"
    return name if name.strip().lower() in _PBI_SAFE_FONTS else "Arial"


def _normalize_font_size(size: Any) -> Optional[str]:
    """Coerce a font size (int/float/str like '11pt') to PBI's '11D' literal."""
    if size is None:
        return None
    if isinstance(size, (int, float)):
        return f"{size}D"
    s = str(size).strip()
    if not s:
        return None
    digits = re.sub(r"[^\d.]", "", s)
    if not digits:
        return None
    return f"{digits}D"


class ReportBuilder:
    def __init__(
        self,
        datasources: List[Dict[str, Any]],
        worksheets:  List[Dict[str, Any]],
        dashboards:  List[Dict[str, Any]],
        model:       SemanticModel,
        hints:       Optional[Dict[str, Dict[str, List[str]]]] = None,
    ):
        self.datasources = datasources
        self.worksheets  = worksheets
        self.dashboards  = dashboards
        self.model       = model
        self._ws_map     = {ws["name"]: ws for ws in worksheets}
        # Resolution hints supplied externally (e.g. by the agent
        # codebase). Shape: {ds_name: {field_name: [table, column]}}.
        # Consulted as a last-resort lookup after the model's own
        # resolve_field returns None — never overrides a successful
        # native resolution. Default {} keeps converter behavior intact.
        self._hints = hints or {}

    # ------------------------------------------------------------------
    # Top level — pages
    # ------------------------------------------------------------------

    def build(self) -> List[Dict[str, Any]]:
        if self.dashboards:
            return [self._page_from_dashboard(db) for db in self.dashboards]
        return [self._page_from_worksheet(ws) for ws in self.worksheets]

    def _page_from_dashboard(self, db: Dict[str, Any]) -> Dict[str, Any]:
        # Honor the dashboard's actual canvas size — Tableau commonly
        # authors dashboards larger than the default page.
        page_w = max(db.get("width",  DEFAULT_PAGE_WIDTH),  DEFAULT_PAGE_WIDTH)
        page_h = max(db.get("height", DEFAULT_PAGE_HEIGHT), DEFAULT_PAGE_HEIGHT)

        zones = db.get("zones", [])

        # All navigation-button zones are merged into a single pageNavigator
        # that spans their collective bounding box.  Individual button zones
        # are then skipped in the main loop below.
        nav_zones = [z for z in zones if z.get("buttonAction") == "goto-sheet"]

        visuals: List[Dict[str, Any]] = []
        z_cur = 1000

        if nav_zones:
            x1 = min(z.get("x", 0) for z in nav_zones)
            y1 = min(z.get("y", 0) for z in nav_zones)
            x2 = max(z.get("x", 0) + z.get("w", 0) for z in nav_zones)
            y2 = max(z.get("y", 0) + z.get("h", 0) for z in nav_zones)
            x1 = max(0, x1); y1 = max(0, y1)
            x2 = min(page_w, x2); y2 = min(page_h, y2)
            nav_w = max(40, x2 - x1); nav_h = max(30, y2 - y1)
            style = nav_zones[0].get("buttonStyle") or {}
            zid   = nav_zones[0].get("id", "")
            visuals.append(
                self._build_page_navigator(x1, y1, nav_w, nav_h, z_cur, zid, style)
            )
            z_cur += 1000

        for zone in zones:
            if zone.get("buttonAction") == "goto-sheet":
                continue  # already collapsed into the single pageNavigator above
            visual = self._visual_from_zone(zone, z_cur, page_w, page_h)
            if visual is not None:
                visuals.append(visual)
                z_cur += 1000

        return {
            "id":          hex_id("page", db["name"]),
            "displayName": db["name"],
            "width":       page_w,
            "height":      page_h,
            "visuals":     visuals,
            # The writer reads this and emits objects.background on
            # page.json. Empty string / None means "no background"
            # — page.json then omits the objects block entirely.
            "backgroundColor": db.get("backgroundColor"),
        }

    def _page_from_worksheet(self, ws: Dict[str, Any]) -> Dict[str, Any]:
        visual = self._build_chart_visual(ws, 0, 0,
                                          DEFAULT_PAGE_WIDTH,
                                          DEFAULT_PAGE_HEIGHT, 1000)
        return {
            "id":          hex_id("page", ws["name"]),
            "displayName": ws["name"],
            "width":       DEFAULT_PAGE_WIDTH,
            "height":      DEFAULT_PAGE_HEIGHT,
            "visuals":     [visual] if visual else [],
        }

    # ------------------------------------------------------------------
    # Zone -> visual dispatch
    # ------------------------------------------------------------------

    def _visual_from_zone(
        self, zone: Dict[str, Any], z: int, page_w: int, page_h: int,
    ) -> Optional[Dict[str, Any]]:
        x = max(0, zone.get("x", 0))
        y = max(0, zone.get("y", 0))
        w = max(40, zone.get("w", 100))
        h = max(30, zone.get("h", 30))

        # Clip to the page — Tableau dashboards sometimes overflow.
        if x >= page_w: x = page_w - w
        if y >= page_h: y = page_h - h
        x = max(0, x); y = max(0, y)
        w = min(w, page_w - x); h = min(h, page_h - y)

        ztype = zone.get("type", "")
        zname = zone.get("name", "")
        zid   = zone.get("id", "")

        # Worksheet zone -> chart visual. Worksheet zones either have an
        # empty type-v2 or report 'paneZone'. They always carry the
        # worksheet name in `name`.
        ws = self._ws_map.get(zname) if zname else None
        if ws is not None and ztype not in ("filter", "parameter",
                                            "color", "legend", "text",
                                            "title", "bitmap",
"dashboard-object"):
            return self._build_chart_visual(ws, x, y, w, h, z)

        # Filter widget — emit a slicer. We try to resolve the filter
        # field so the slicer actually drives the report.
        if ztype == "filter":
            flabel = self._filter_label(zone)
            ffield = self._filter_field(zone)
            # Title styling carries over only when the parser surfaces it.
            # Use `titleStyle` for the new path; fall back to legacy
            # `filterStyle` so older parsers keep working. None means "no
            # styling override" — _build_placeholder_slicer treats that as
            # 'use defaults'.
            title_style   = zone.get("titleStyle") or zone.get("filterStyle")
            title_enabled = zone.get("titleEnabled", True)
            return self._build_placeholder_slicer(
                flabel, x, y, w, h, z, zid,
                field=ffield,
                ds_name=self._ds_name_for_zone(zone),
                title_style=title_style,
                title_enabled=title_enabled,
                widget_mode=zone.get("mode", ""),
            )

        # Parameter control -> placeholder slicer.
        if ztype in ("parameter", "paramctrl"):
            return self._build_placeholder_slicer(
                zname or zone.get("param", "Parameter"),
                x, y, w, h, z, zid,
                title_style=zone.get("titleStyle"),
                title_enabled=zone.get("titleEnabled", True),
                widget_mode=zone.get("mode", ""),
            )

        # `containerStyle` carries background / border / padding from the
        # zone's `<zone-style>` block. It's already merged into the more
        # specific textStyle / titleStyle / buttonStyle for zones that
        # have those, so we only need it on the bare-textbox paths
        # (color / legend / bitmap / fallback) where no other style dict
        # is built.
        container_style = zone.get("containerStyle") or {}

        # Color / legend zones -> small textbox so the legend area still
        # reserves space in the layout.
        if ztype in ("color", "legend"):
            return self._build_textbox(zname or "Legend", x, y, w, h, z, zid,
                                       style=container_style)

        # Text and title zones -> textbox carrying the actual extracted
        # text and any font/container styling the parser pulled from the
        # zone's <run> + <zone-style> blocks.
        if ztype in ("text", "title"):
            text = zone.get("text") or zname or " "
            return self._build_textbox(text, x, y, w, h, z, zid,
                                       style=zone.get("textStyle"))

        # Image / bitmap zones -> textbox placeholder. We don't bundle the
        # image into the PBIP because Tableau stores it inside the .twbx
        # and we want the converter to keep working on plain .twb files.
        if ztype == "bitmap":
            return self._build_textbox("[Image]", x, y, w, h, z, zid,
                                       style=container_style)

        # dashboard-object: edit/refresh widgets and any non-navigation buttons.
        # Navigation buttons (goto-sheet) are already merged into a single
        # pageNavigator in _page_from_dashboard and never reach this path.
        if ztype == "dashboard-object":
            text = zone.get("caption") or zname or " "
            style = zone.get("buttonStyle") or {}
            return self._build_textbox(text, x, y, w, h, z, zid, style=style)

        # Anything else with no name and no recognized type just becomes
        # a quiet empty textbox — keeps the layout intact. We still pass
        # the container style so a Tableau zone that paints a colored
        # tile but has no recognised type doesn't lose its background.
        return self._build_textbox(zname or " ", x, y, w, h, z, zid,
                                   style=container_style)

    @staticmethod
    def _filter_label(zone: Dict[str, Any]) -> str:
        # Filter zones carry their target field in the param attribute,
        # shaped like '[ds].[role:field:key]'. Pull the field segment so
        # the slicer title says "Region" instead of "Actual Cost vs Budget".
        param = zone.get("param", "")
        if not param:
            return zone.get("name", "Filter")
        # Match the inner spec inside the second pair of brackets.
        import re
        m = re.match(r"^\[[^\]]+\]\s*\.\s*\[([^\]]+)\]$", param)
        if not m:
            return zone.get("name", "Filter")
        spec  = m.group(1)
        parts = spec.split(":")
        if len(parts) >= 3:
            return parts[1].strip() or zone.get("name", "Filter")
        if len(parts) == 2:
            return parts[1].strip() or zone.get("name", "Filter")
        return parts[0].strip() or zone.get("name", "Filter")

    @staticmethod
    def _filter_field(zone: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return a field dict {field, agg} for the filter zone's param,
        or None if it cannot be parsed."""
        param = zone.get("param", "")
        if not param:
            return None
        import re
        m = re.match(r"^\[[^\]]+\]\s*\.\s*\[([^\]]+)\]$", param)
        if not m:
            return None
        spec = m.group(1)
        parts = spec.split(":")
        if len(parts) >= 3:
            agg = parts[0].strip().lower()
            field = ":".join(parts[1:-1]).strip()
        elif len(parts) == 2:
            agg, field = parts[0].strip().lower(), parts[1].strip()
        else:
            agg, field = "", parts[0].strip()
        if not field:
            return None
        return {"field": field, "agg": agg}

    def _hint_lookup(
        self, ds_name: str, field_name: str,
    ) -> Optional[Tuple[str, str]]:
        """Last-resort field lookup against externally supplied hints.

        Hints are validated against the live model: a (table, column)
        pair is only honored when both still exist. This keeps stale
        agent-suggested hints from pointing at columns that were
        renamed or removed in a later run.
        """
        if not self._hints:
            return None
        ds_hints = self._hints.get(ds_name) or self._hints.get("*") or {}
        loc = ds_hints.get(field_name)
        if not loc or len(loc) != 2:
            return None
        tbl, col = loc[0], loc[1]
        for t in self.model.tables:
            if t.get("name") != tbl:
                continue
            for c in t.get("columns", []) or []:
                if c.get("name") == col:
                    print(f"[HINT] '{field_name}' (ds={ds_name}) -> "
                          f"{tbl}.{col}")
                    return (tbl, col)
            for m in t.get("measures", []) or []:
                if m.get("name") == col:
                    print(f"[HINT] '{field_name}' (ds={ds_name}) -> "
                          f"{tbl}.{col} (measure)")
                    return (tbl, col)
            return None
        return None

    def _ds_name_for_zone(self, zone: Dict[str, Any]) -> str:
        """Return the best-guess datasource name for a dashboard zone.

        Filter zones reference a field like '[ds].[role:field]'. We try
        to extract the datasource name from that reference."""
        param = zone.get("param", "")
        if param:
            import re
            m = re.match(r"^\[([^\]]+)\]", param)
            if m:
                ds_hint = m.group(1).strip()
                for ds in self.datasources:
                    if ds["name"] == ds_hint or ds["caption"] == ds_hint:
                        return ds["name"]
        # Fallback to the datasource of the worksheet this filter is
        # attached to (zones that share the worksheet's name).
        zname = zone.get("name", "")
        ws = self._ws_map.get(zname)
        if ws is not None:
            return self._ds_name(ws.get("datasourceRef", ""))
        return self.datasources[0]["name"] if self.datasources else ""

    # ------------------------------------------------------------------
    # Chart visual
    # ------------------------------------------------------------------

    def _build_chart_visual(
        self, ws: Dict[str, Any],
        x: int, y: int, w: int, h: int, z: int,
    ) -> Dict[str, Any]:
        visual_type = self._pick_visual_type(ws)
        ds_name     = self._ds_name(ws.get("datasourceRef", ""))

        # One-line diagnostic so users can confirm which Tableau mark and
        # shelf shape produced each visual type. Helps debug "I expected a
        # card but got a textbox" claims by making the picker's decision
        # visible without firing up a debugger.
        mark = ws.get("markClass", "")
        rows = len(ws.get("rowFields") or [])
        cols = len(ws.get("colFields") or [])
        lbl  = (ws.get("labelField") or {}).get("field", "")
        print(f"[VPICK] {ws.get('name','?'):40s} mark={mark:10s} "
              f"rows={rows} cols={cols} label='{lbl}' -> {visual_type}")

        # Single-value card (cardVisual / legacy card) layout default.
        # Tableau text-mark worksheets land in arbitrary zone heights
        # (often 100-200px) but PBI's KPI cards render best in a tight
        # 50px strip — matches the single-line title + value shape.
        # multiRowCard keeps Tableau's zone height since it stacks
        # multiple value rows and needs the room.
        if visual_type in ("card", "cardVisual"):
            h = 50

        # Stash the worksheet's column registry (canonical -> raw name with
        # any (Object!Suffix) disambiguator) for the duration of this
        # visual build. _add_proj enriches the resolver's hint with this
        # data so a column referenced as plain "HCP_ID" still binds to the
        # right logical table.
        self._ws_columns = ws.get("wsColumns") or {}

        prefer_table = self._primary_table_for_ws(ws, ds_name)
        projections  = self._build_projections(ws, visual_type, ds_name,
                                               prefer_table)

        # Slot validators close the gap between Tableau's encoding-shelf
        # semantics and PBI's slot expectations — e.g. lifting a country
        # field off Details onto a filledMap's Location slot. Notes are
        # printed to stdout so users see exactly what was reshaped (the
        # same trace style as [HINT] / [DROP]).
        from .validators import validate_slots
        for note in validate_slots(visual_type, ws, projections, ds_name,
                                    self._add_proj):
            print(f"[VALIDATE] {ws.get('name','?')}: {note}")

        # We build the visual.json shape Power BI Desktop expects. The
        # order of keys here matches the reference report we modeled,
        # which makes manual diffing easier.
        # Visual-level filters: every Tableau worksheet filter that
        # actually restricts data (members chosen, not the default 'all')
        # becomes a PBI visual-level filter. Filters declared as 'all' or
        # missing members are skipped — those are typically the dashboard
        # filter widgets (slicers) that PBI handles via cross-filtering.
        vfilters = self._build_visual_filters(ws.get("filters") or [],
                                              ds_name)

        # Per-shelf-binding auto-filters that PBI Desktop stamps on save.
        # Map visuals and the modern KPI cards (cardVisual / multiRowCard)
        # get them — Desktop's auto-stamp behavior is most active on
        # those visual types, and round-tripping a converter-emitted file
        # through Desktop without these would make the saved version diff
        # against ours. Bar/pie/table outputs aren't currently mutated,
        # so they skip. Existing user filters take precedence — we build
        # the (table, column, is_aggregated) key set from `vfilters` so
        # a user filter on Region doesn't get a duplicate auto-filter.
        if visual_type in ("map", "filledMap", "shapeMap",
                           "cardVisual", "multiRowCard") and projections:
            existing_keys: set = set()
            for f in vfilters:
                tbl, col, is_agg = self._projection_field_key(
                    f.get("field") or {}
                )
                if tbl and col:
                    existing_keys.add((tbl, col, is_agg))
            auto_filters = self._build_auto_filters(
                projections, existing_keys, ws["name"],
            )
            vfilters.extend(auto_filters)

        v: Dict[str, Any] = {
            "$schema": SCHEMA["visual"],
            "name":    hex_id("visual", ws["name"]),
            "position": {
                "x": x, "y": y, "z": z,
                "height": h, "width": w,
                "tabOrder": z,
            },
            "visual": {
                "visualType":              visual_type,
                "drillFilterOtherVisuals": True,
            },
            "filterConfig": {"filters": vfilters},
        }
        if projections:
            v["visual"]["query"] = {"queryState": projections}

        # Sort directives lifted from Tableau's <shelf-sort-v2> blocks.
        # Each becomes a query.sortDefinition.sort entry so the PBI visual
        # opens with the same row order the Tableau worksheet used.
        sort_defs = self._build_sort_definition(ws.get("sortSpecs") or [],
                                                ds_name)

        # PBI's VisualTopN filter only carries ItemCount — the ranking
        # measure and direction (Top vs Bottom) come from the visual's
        # sortDefinition. So when a worksheet has a Top N filter, fold
        # its ranking into sort_defs (unless an existing sort entry
        # already covers that measure).
        for flt in (ws.get("filters") or []):
            tspec = flt.get("topN")
            if not tspec:
                continue
            extra = self._sort_def_from_top_n(tspec, ds_name, sort_defs)
            if extra:
                sort_defs.extend(extra)

        if sort_defs:
            v["visual"].setdefault("query", {"queryState": {}})
            v["visual"]["query"]["sortDefinition"] = {
                "sort":          sort_defs,
                "isDefaultSort": False,
            }

        # Title: honor twb's "show title" flag and any extracted style.
        # When the parser supplies titleText we prefer it over the bare
        # worksheet name (it may include user-authored prefixes/suffixes).
        # titleEnabled=None means 'no hint', treated as enabled.
        title_enabled_raw = ws.get("titleEnabled")
        title_enabled = True if title_enabled_raw is None else bool(title_enabled_raw)
        title_text    = ws.get("titleText") or ws["name"]
        title_style   = dict(ws.get("titleStyle") or {})
        # Card visuals get a fixed 14pt title — too-large titles eat
        # vertical space the value field needs in a 50px-tall card.
        if visual_type in ("card", "cardVisual", "multiRowCard"):
            title_style.setdefault("fontSize", 14)
        container = self._title_object(title_text, title_style,
                                       enabled=title_enabled)
        # Card visuals: emit a default white background when Tableau
        # didn't carry one through. Cards on a transparent dashboard
        # blend into the page; an explicit white card matches PBI's
        # default card chrome and the user's stated convention.
        bg = ws.get("backgroundColor")
        if visual_type in ("card", "cardVisual", "multiRowCard") and not bg:
            bg = "#ffffff"
        if bg:
            if not container:
                container = {}
            container["background"] = [{
                "properties": {
                    "show":  {"expr": {"Literal": {"Value": "true"}}},
                    "color": {"solid": {"color": {
                        "expr": {"Literal": {"Value": f"'{bg}'"}}
                    }}},
                },
            }]
        if container:
            v["visual"]["visualContainerObjects"] = container

        # Mark labels: a worksheet with labels disabled in Tableau should
        # render with data labels off in PBI. Style is applied only when
        # labels are enabled.
        objects: Dict[str, Any] = {}
        label_enabled = ws.get("labelEnabled")
        label_style   = ws.get("labelStyle")

        # Legacy 'card' visualType uses the 'labels' bag for its value
        # font/color/size. multiRowCard uses 'dataLabels' with just a
        # 'color' property — its font is inherited from the visual's
        # global style, not per-bag. cardVisual emits its own 'value'
        # bag below and is skipped here entirely.
        if visual_type == "card":
            label_style = dict(label_style or {})
            label_style.setdefault("fontSize", 15)
            label_style.setdefault("fontFamily", "Arial")
            if label_enabled is None:
                label_enabled = True

        if (label_enabled is not None or label_style) and visual_type not in (
            "cardVisual", "multiRowCard"
        ):
            label_obj = self._label_object(label_enabled, label_style,
                                           visual_type)
            if label_obj:
                objects.update(label_obj)

        # multiRowCard styling — emit color / fontFamily / fontSize /
        # bold from Tableau's labelStyle (parsed from `<customized-label>/
        # <run>`, worksheet, cell, or mark style-rules). All four
        # properties live on the same `dataLabels` bag for multiRowCard
        # — emitting only color leaves PBI Desktop to fall back to its
        # default font (Segoe UI), so a twb that says Calibri would
        # render as Segoe UI without these.
        # Skip emitting the bag entirely if Tableau supplied nothing
        # value-relevant.
        if visual_type == "multiRowCard":
            ls = label_style or {}
            props: Dict[str, Any] = {}
            if ls.get("fontColor"):
                props["color"] = _color_expr(ls["fontColor"])
            if ls.get("fontFamily"):
                family = _safe_font_family(ls["fontFamily"]) or "Arial"
                props["fontFamily"] = _expr_lit(f"'{family}'")
            if ls.get("fontSize"):
                size_lit = _normalize_font_size(ls["fontSize"])
                if size_lit:
                    props["fontSize"] = _expr_lit(size_lit)
            if str(ls.get("fontWeight", "")).lower() == "bold":
                props["bold"] = _expr_lit("true")
            if ls.get("italic"):
                props["italic"] = _expr_lit("true")
            if ls.get("underline"):
                props["underline"] = _expr_lit("true")
            if props:
                objects["dataLabels"] = [{"properties": props}]

        # Legacy card / multiRowCard: hide the field-name (category) label
        # below the value. cardVisual handles this via its own 'value'
        # bag (no separate categoryLabels for the modern card).
        if visual_type in ("card", "multiRowCard"):
            objects["categoryLabels"] = [{
                "properties": {"show": _expr_lit("false")},
            }]

        # cardVisual value styling. PBI's modern KPI card uses these
        # property bags (each with selector {id: "default"}):
        #   value      - headline value font / color / size / alignment
        #   label      - category label below the value (we hide it)
        #   outline    - card border (we hide it)
        #   divider    - line under title (we hide it)
        #   fillCustom - per-data-point fill (we disable; the card chrome
        #                background lives on visualContainerObjects)
        #
        # The 'callout' bag was emitted by an earlier iteration but PBI
        # Desktop ignores it on the modern card — value/label/outline/
        # divider are the bags it actually reads.
        #
        # Tableau labelStyle (parsed from worksheet/cell/mark style-rules)
        # supplies fontFamily / fontSize / fontColor when present;
        # otherwise we fall back to Arial / 15pt / black. The card chrome
        # (background color) is on visualContainerObjects.background and
        # was already set above via the `bg` block.
        if visual_type == "cardVisual":
            ls = label_style or {}
            font_size   = ls.get("fontSize", 15)
            font_color  = ls.get("fontColor", "#000000")
            font_family = _safe_font_family(ls.get("fontFamily")) or "Arial"
            value_props: Dict[str, Any] = {
                "fontFamily":          _expr_lit(f"'{font_family}'"),
                "fontSize":            _expr_lit(_normalize_font_size(font_size) or "15D"),
                "fontColor":           _color_expr(font_color),
                "horizontalAlignment": _expr_lit("'center'"),
            }
            if str(ls.get("fontWeight", "")).lower() == "bold":
                value_props["bold"] = _expr_lit("true")
            if ls.get("italic"):
                value_props["italic"] = _expr_lit("true")
            if ls.get("underline"):
                value_props["underline"] = _expr_lit("true")

            # Each cardVisual format bag carries a selector keyed on
            # {id: "default"} — that's the slot the modern card reads
            # for its primary "Value" pane.
            default_sel = {"id": "default"}

            objects["fillCustom"] = [{
                "properties": {"show": _expr_lit("false")},
            }]
            objects["value"] = [{
                "properties": value_props,
                "selector":   default_sel,
            }]
            objects["label"] = [{
                "properties": {"show": _expr_lit("false")},
                "selector":   default_sel,
            }]
            objects["outline"] = [{
                "properties": {"show": _expr_lit("false")},
                "selector":   default_sel,
            }]
            objects["divider"] = [{
                "properties": {"show": _expr_lit("false")},
                "selector":   default_sel,
            }]
            # Strip the legacy bags defensively — they would be silently
            # ignored by cardVisual at best, and may flag schema warnings.
            objects.pop("labels", None)
            objects.pop("categoryLabels", None)
            objects.pop("callout", None)


        # Mark-color override: paint every data point with the user's
        # chosen color. Skip on visual types that use a different
        # color bag (cards / textboxes / pivots / maps render colors
        # differently and a misplaced dataPoint object would be
        # silently dropped at best, and emit a 'property not allowed'
        # at worst).
        mark_color = ws.get("markColor")
        if mark_color and visual_type in (
            "barChart", "columnChart", "lineChart", "areaChart",
            "stackedBarChart", "stackedColumnChart",
            "scatterChart", "bubbleChart",
            "lineClusteredColumnComboChart",
            "lineStackedColumnComboChart",
            "ribbonChart",
        ):
            objects["dataPoint"] = [{
                "properties": {
                    "defaultColor": {
                        "solid": {"color": _expr_lit(f"'{mark_color}'")},
                    },
                },
            }]

        # Tableau datasource-level color palette: when the worksheet's
        # color shelf is bound to a field that has a per-bucket color
        # override declared on the datasource (`<encoding attr='color'>`
        # with `<map to='#hex'><bucket>"Value"</bucket></map>`), emit a
        # `dataPoint` block keyed by category so PBI paints each series
        # with the same hex Tableau used. Applies to chart types that
        # actually expose a Series/Color slot.
        if visual_type in (
            "areaChart", "lineChart", "stackedAreaChart",
            "pieChart", "donutChart",
            "barChart", "columnChart",
            "stackedBarChart", "stackedColumnChart",
        ):
            color_block = self._build_per_category_color_block(
                ws, ds_name, visual_type,
            )
            if color_block:
                objects["dataPoint"] = color_block

        # Area / line chart defaults — turn data-point markers on as
        # circles by default. Tableau renders area charts with circle
        # markers at each data point; PBI hides them by default. Honor
        # any Tableau-supplied mark color so the markers visually match
        # the area fill color.
        if visual_type in ("areaChart", "lineChart", "stackedAreaChart"):
            marker_props: Dict[str, Any] = {
                "show":  _expr_lit("true"),
                "shape": _expr_lit("'circle'"),
            }
            if mark_color:
                marker_props["color"] = _color_expr(mark_color)
            # Preserve per-series color block already on dataPoint (if any).
            existing_dp = objects.get("dataPoint")
            if existing_dp:
                existing_dp[0].setdefault("properties", {})
                existing_dp[0]["properties"]["showAllDataPoints"] = _expr_lit("true")
            else:
                objects["dataPoint"] = [{
                    "properties": {"showAllDataPoints": _expr_lit("true")},
                }]
            objects["markers"] = [{"properties": marker_props}]

        # Pie / donut chart defaults — show legend on by default so the
        # color encoding is readable. Tableau pie charts always show a
        # legend; PBI hides it by default and the user's reference
        # explicitly enables it.
        if visual_type in ("pieChart", "donutChart"):
            objects["legend"] = [{
                "properties": {
                    "show":     _expr_lit("true"),
                    "position": _expr_lit("'Right'"),
                },
            }]

        if objects:
            v["visual"]["objects"] = objects
        return v

    # ------------------------------------------------------------------
    # Sort definition (mirrors Tableau's shelf-sort directives)
    # ------------------------------------------------------------------

    def _build_sort_definition(
        self, sort_specs: List[Dict[str, Any]], ds_name: str,
    ) -> List[Dict[str, Any]]:
        """Translate parsed shelf-sort directives into PBI sort entries.

        Each PBI sort entry is:
            {"field": <field-ref>, "direction": "Ascending"|"Descending"}

        We resolve the *measure* (when present) against the model and
        emit it wrapped in Aggregation (so PBI sorts the visual by the
        aggregated measure). When no measure was specified Tableau is
        sorting the dimension alphabetically — emit the dimension itself
        as a Column reference.

        Unresolved fields are dropped silently (same policy as
        projections). The visual still renders, just without our sort.
        """
        out: List[Dict[str, Any]] = []
        prefer_table = getattr(self, "_prefer_table_for_ws", None)
        for spec in sort_specs or []:
            direction = ("Descending" if (spec.get("direction") == "DESC")
                         else "Ascending")
            measure = (spec.get("measure") or "").strip()
            measure_agg = (spec.get("measureAgg") or "").lower()
            dim     = (spec.get("dimension") or "").strip()
            dim_agg = (spec.get("dimensionAgg") or "").lower()

            # Prefer the measure: that's the column actually being sorted
            # on (Tableau lets you sort a dimension by a measure). When the
            # measure equals the dimension OR no measure is given, fall
            # back to the dimension column.
            target_field = ""
            target_agg   = ""
            if measure and measure.lower() != dim.lower():
                target_field = measure
                target_agg   = measure_agg
            elif dim:
                target_field = dim
                target_agg   = dim_agg
            if not target_field:
                continue

            loc = self.model.resolve_field(ds_name, target_field,
                                           prefer_table=prefer_table)
            if not loc:
                loc = self._hint_lookup(ds_name, target_field)
            if not loc:
                print(f"[SORT] '{target_field}' (ds={ds_name}) not found — "
                      f"sort directive dropped.")
                continue
            tbl, col = loc

            agg_info = AGG_TABLE.get(target_agg)
            if self.model.is_measure_ref(tbl, col):
                field_ref = {
                    "Measure": {
                        "Expression": {"SourceRef": {"Entity": tbl}},
                        "Property":   col,
                    },
                }
            elif agg_info and agg_info["is_measure"] and agg_info.get("fn") is not None:
                field_ref = {
                    "Aggregation": {
                        "Expression": {
                            "Column": {
                                "Expression": {"SourceRef": {"Entity": tbl}},
                                "Property":   col,
                            },
                        },
                        "Function": agg_info["fn"],
                    },
                }
            else:
                field_ref = {
                    "Column": {
                        "Expression": {"SourceRef": {"Entity": tbl}},
                        "Property":   col,
                    },
                }
            out.append({"field": field_ref, "direction": direction})
        return out

    def _sort_def_from_top_n(
        self,
        top_spec: Dict[str, Any],
        ds_name: str,
        existing: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Synthesize a sortDefinition entry from a Tableau Top N spec.

        PBI infers Top vs Bottom and the ranking column from the visual's
        sortDefinition, NOT from the filter itself (VisualTopN only carries
        ItemCount). So:
            direction TOP    -> sort Descending by the ranking measure
            direction BOTTOM -> sort Ascending  by the ranking measure

        Skipped when:
          - the spec has no ranking measure
          - the measure can't be resolved against the model
          - an `existing` sort entry already references the same property
            (avoids duplicate sort entries when Tableau emits both a
            shelf-sort-v2 and a Top N filter pointing at the same measure)
        """
        meas = (top_spec.get("measure") or "").strip()
        if not meas:
            return []
        meas_agg = (top_spec.get("measureAgg") or "").lower()
        prefer_table = getattr(self, "_prefer_table_for_ws", None)
        loc = self.model.resolve_field(ds_name, meas, prefer_table=prefer_table)
        if not loc:
            loc = self._hint_lookup(ds_name, meas)
        if not loc:
            print(f"[TOPN] ranking measure '{meas}' (ds={ds_name}) not "
                  f"found — visual will use its default sort.")
            return []
        tbl, col = loc

        # Skip if some shelf-sort already covers this column.
        for s in existing:
            f = s.get("field") or {}
            for kind in ("Measure", "Column", "Aggregation"):
                node = f.get(kind)
                if not node:
                    continue
                if kind == "Aggregation":
                    inner = (node.get("Expression") or {}).get("Column") or {}
                    if inner.get("Property") == col:
                        return []
                else:
                    if node.get("Property") == col:
                        return []

        agg_info = AGG_TABLE.get(meas_agg)
        if self.model.is_measure_ref(tbl, col):
            field_ref = {
                "Measure": {
                    "Expression": {"SourceRef": {"Entity": tbl}},
                    "Property":   col,
                },
            }
        elif agg_info and agg_info["is_measure"] and agg_info.get("fn") is not None:
            field_ref = {
                "Aggregation": {
                    "Expression": {
                        "Column": {
                            "Expression": {"SourceRef": {"Entity": tbl}},
                            "Property":   col,
                        },
                    },
                    "Function": agg_info["fn"],
                },
            }
        else:
            field_ref = {
                "Column": {
                    "Expression": {"SourceRef": {"Entity": tbl}},
                    "Property":   col,
                },
            }
        is_top = (top_spec.get("direction", "TOP").upper() == "TOP")
        direction = "Descending" if is_top else "Ascending"
        return [{"field": field_ref, "direction": direction}]

    # ------------------------------------------------------------------
    # Visual-level filters
    # ------------------------------------------------------------------

    def _build_visual_filters(
        self, filters: List[Dict[str, Any]], ds_name: str,
    ) -> List[Dict[str, Any]]:
        """Translate parsed worksheet filters into PBI visual-level filters.

        Two flavors come out of here, mirroring how the parser walks the
        filter's groupfilter tree:

          * members present  -> a Where condition (In or Not/In) carrying
                                the picked values, so the PBI visual is
                                actually restricted to those values when
                                opened.
          * no members       -> a column-only binding so the field shows
                                up on the visual's filter pane (Tableau's
                                filter shelf with 'all selected').

        Unresolved fields are dropped (same policy as projections).
        Duplicate (table, column) bindings are deduped.
        """
        out: List[Dict[str, Any]] = []
        prefer_table = getattr(self, "_prefer_table_for_ws", None)
        ws_columns   = getattr(self, "_ws_columns", None) or {}
        seen_keys: set = set()

        for idx, flt in enumerate(filters or []):
            fname = (flt.get("field") or "").strip()
            if not fname:
                continue

            # Tableau pseudo-columns ('Latitude (generated)' /
            # 'Longitude (generated)') don't exist in the data — they're
            # geocoded at render time from a Country/State field. Skip
            # silently rather than logging a noisy [FILTER] not-found
            # warning the user can't act on anyway.
            if "(generated)" in fname.lower():
                continue

            hint = fname
            canonical = SemanticModel._strip_obj_suffix(fname) or fname
            raw = ws_columns.get(canonical) or ws_columns.get(fname)
            if raw and "(" in raw and ")" in raw and "(" not in fname:
                hint = raw
            loc = self.model.resolve_field(ds_name, hint,
                                           prefer_table=prefer_table)
            if not loc:
                loc = self._hint_lookup(ds_name, fname)
            if not loc:
                print(f"[FILTER] '{fname}' (ds={ds_name}) not found — "
                      f"visual-level filter dropped.")
                continue
            tbl, col = loc

            key = (tbl, col)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            ftype = "Categorical"
            tcls = (flt.get("type") or "").lower()
            if tcls in ("quantitative",):
                ftype = "Advanced"
            top_spec = flt.get("topN")
            # We use `TopN` (not `VisualTopN`) for the filter container's
            # `type` even though the inner Condition discriminator IS
            # `VisualTopN`. Both values are schema-valid for the container,
            # but PBI Desktop treats `VisualTopN` as a frozen / system
            # filter — the filter-type dropdown in the filter pane is
            # hidden, so the user can't switch to Basic / Advanced without
            # deleting and re-adding the filter. `TopN` keeps the user-
            # facing UI editable while the PBIP-format Where clause still
            # uses the schema-required `VisualTopN` shape.
            if top_spec:
                ftype = "TopN"

            entry: Dict[str, Any] = {
                "name": f"Filter{idx + 1}",
                "type": ftype,
                "field": {
                    "Column": {
                        "Expression": {"SourceRef": {"Entity": tbl}},
                        "Property":   col,
                    },
                },
                "howCreated":  "User",
                "displayName": col,
            }

            if top_spec:
                topn_filter = self._build_top_n_filter(
                    tbl, col, top_spec, ds_name,
                )
                if topn_filter:
                    entry["filter"] = topn_filter
                    out.append(entry)
                    continue
                # Top N spec was present but couldn't be turned into a
                # filter (count<=0). Fall back to a plain categorical
                # field-only binding so the filter pane still surfaces
                # the column the user intended to constrain.
                entry["type"] = "Categorical"

            members = flt.get("members") or []
            if members:
                # Build literal values. Tableau gives us pre-stripped
                # tokens; we re-quote strings (with single-quote escape)
                # and emit numerics with the L suffix so PBI types them
                # right. Treat anything that fully parses as a number as
                # numeric; otherwise quote as string.
                values: List[List[Dict[str, Any]]] = []
                for m in members:
                    s = str(m)
                    # Boolean values: emit BARE keyword literal. Without
                    # this branch, ``true`` / ``false`` would fall through
                    # to the string path and be wrapped as ``'true'`` /
                    # ``'false'`` — PBI would then try to compare a
                    # boolean column against a string literal and fail
                    # with "field is not available". The raw value is
                    # the unquoted keyword that DAX accepts. This matters
                    # for boolean-dim calc columns like ``Date Range``
                    # that are used as visual-level filters.
                    if s.lower() in ("true", "false"):
                        lit = {"Literal": {"Value": s.lower()}}
                    else:
                        try:
                            # Integer first; PBI accepts L suffix for it.
                            int(s)
                            lit = {"Literal": {"Value": f"{s}L"}}
                        except ValueError:
                            try:
                                float(s)
                                lit = {"Literal": {"Value": f"{s}D"}}
                            except ValueError:
                                esc = s.replace("'", "''")
                                lit = {"Literal": {"Value": f"'{esc}'"}}
                    values.append([lit])

                in_expr = {
                    "In": {
                        "Expressions": [{
                            "Column": {
                                "Expression": {"SourceRef": {"Source": "t"}},
                                "Property":   col,
                            },
                        }],
                        "Values": values,
                    },
                }
                condition = ({"Not": {"Expression": in_expr}}
                             if flt.get("exclude") else in_expr)
                entry["filter"] = {
                    "Version": 2,
                    "From": [{"Name": "t", "Entity": tbl, "Type": 0}],
                    "Where": [{"Condition": condition}],
                }

            out.append(entry)
        return out

    def _build_top_n_filter(
        self,
        tbl: str, col: str,
        top_spec: Dict[str, Any],
        ds_name: str,
    ) -> Optional[Dict[str, Any]]:
        """Translate a Tableau Top-N spec into a PBI subquery-based Top N
        filter.

        PBI Desktop's filter pane writes Top N filters as an `In` clause
        whose right-hand side is a subquery. The subquery selects the
        dimension column ordered by the ranking aggregation, with a
        `Top: <N>` cap. This is the form a user sees when they apply
        Top N through the filter UI and saves the report — round-trips
        cleanly, supports the "By value" measure picker, and renders
        correctly in PBI Desktop.

        Resulting shape:
            "filter": {
              "Version": 2,
              "From": [
                {
                  "Name": "subquery",
                  "Expression": {
                    "Subquery": {
                      "Query": {
                        "Version": 2,
                        "From": [{"Name": "c", "Entity": "<tbl>", "Type": 0}],
                        "Select": [
                          {"Column": {...col...}, "Name": "field"}
                        ],
                        "OrderBy": [
                          {
                            "Direction": 2,           # 2 = Descending, 1 = Ascending
                            "Expression": {"Aggregation": {...measure...}}
                          }
                        ],
                        "Top": <N>
                      }
                    }
                  },
                  "Type": 2
                },
                {"Name": "c", "Entity": "<tbl>", "Type": 0}
              ],
              "Where": [
                {
                  "Condition": {
                    "In": {
                      "Expressions": [
                        {"Column": {"Expression": {"SourceRef": {"Source": "c"}}, "Property": "<col>"}}
                      ],
                      "Table": {"SourceRef": {"Source": "subquery"}}
                    }
                  }
                }
              ]
            }

        The ranking measure must be resolvable against the model. When
        the spec carries no measure (alphabetical Top N), we skip and
        return None — a Top N without a ranking measure is a Tableau
        construct PBI can't represent through this shape.
        """
        count = int(top_spec.get("count") or 0)
        if count <= 0:
            return None

        # Resolve the ranking measure. Without one, the subquery has no
        # OrderBy expression and PBI rejects the filter.
        meas = (top_spec.get("measure") or "").strip()
        if not meas:
            return None
        meas_agg = (top_spec.get("measureAgg") or "").lower()
        prefer_table = getattr(self, "_prefer_table_for_ws", None)
        loc = self.model.resolve_field(ds_name, meas,
                                       prefer_table=prefer_table)
        if not loc:
            loc = self._hint_lookup(ds_name, meas)
        if not loc:
            print(f"[TOPN] ranking measure '{meas}' (ds={ds_name}) not "
                  f"found — Top N filter dropped.")
            return None
        meas_tbl, meas_col = loc

        # When the ranking measure lives in a different table from the
        # dimension being filtered (e.g. UseCase2: HCO Name from Dim_HCO,
        # ranked by Count Distinct of Hcp Id from Dim_HCP), the inner
        # subquery's From needs BOTH tables with distinct aliases —
        # otherwise the OrderBy column reference resolves against the
        # wrong table and PBI silently picks the wrong column (or fails
        # to load). Sharing a single alias only works when dim and
        # measure belong to the same table.
        cross_table  = (meas_tbl != tbl)
        meas_source  = "m" if cross_table else "c"

        # Build the OrderBy aggregation expression. Resolved DAX measures
        # use a Measure reference; columns wrap in Aggregation with the
        # caller-supplied agg, defaulting to CountNonNull (Function: 5)
        # when no agg was specified.
        if self.model.is_measure_ref(meas_tbl, meas_col):
            order_expr = {
                "Measure": {
                    "Expression": {"SourceRef": {"Source": meas_source}},
                    "Property":   meas_col,
                },
            }
        else:
            agg_info = AGG_TABLE.get(meas_agg)
            fn = (agg_info["fn"]
                  if agg_info and agg_info.get("fn") is not None
                  else 5)
            order_expr = {
                "Aggregation": {
                    "Expression": {
                        "Column": {
                            "Expression": {"SourceRef": {"Source": meas_source}},
                            "Property":   meas_col,
                        },
                    },
                    "Function": fn,
                },
            }

        # Direction: 2 = Descending (Top), 1 = Ascending (Bottom).
        direction = 1 if (top_spec.get("direction") or "").upper() == "BOTTOM" else 2

        # Subquery From clauses: dimension's table + measure's table when
        # they differ, otherwise just the dimension's table.
        sub_from: List[Dict[str, Any]] = [
            {"Name": "c", "Entity": tbl, "Type": 0},
        ]
        if cross_table:
            sub_from.append({"Name": meas_source, "Entity": meas_tbl, "Type": 0})

        return {
            "Version": 2,
            "From": [
                {
                    "Name": "subquery",
                    "Expression": {
                        "Subquery": {
                            "Query": {
                                "Version": 2,
                                "From": sub_from,
                                "Select": [
                                    {
                                        "Column": {
                                            "Expression": {"SourceRef": {"Source": "c"}},
                                            "Property":   col,
                                        },
                                        "Name": "field",
                                    },
                                ],
                                "OrderBy": [
                                    {
                                        "Direction":  direction,
                                        "Expression": order_expr,
                                    },
                                ],
                                "Top": count,
                            },
                        },
                    },
                    "Type": 2,
                },
                {"Name": "c", "Entity": tbl, "Type": 0},
            ],
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [
                                {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Source": "c"}},
                                        "Property":   col,
                                    },
                                },
                            ],
                            "Table": {"SourceRef": {"Source": "subquery"}},
                        },
                    },
                },
            ],
        }

    def _pick_visual_type(self, ws: Dict[str, Any]) -> str:
        # Delegated to visual_picker, which reads visual_rules.json so
        # the user can tune behavior without code changes.
        return pick_visual_type(ws)

    def _ds_name(self, ds_ref: str) -> str:
        for ds in self.datasources:
            if ds["name"] == ds_ref or ds["caption"] == ds_ref:
                return ds["name"]
        # No match — log so the user can spot misrouted worksheets, then
        # fall back to the first datasource. Falling back keeps the visual
        # alive (instead of dropping it) but resolution may pick the wrong
        # table. The log line makes the silent guess auditable.
        if ds_ref:
            print(f"[DS] worksheet datasourceRef '{ds_ref}' did not match any "
                  f"parsed datasource; falling back to first.")
        return self.datasources[0]["name"] if self.datasources else ""

    def _primary_table_for_ws(
        self, ws: Dict[str, Any], ds_name: str,
    ) -> Optional[str]:
        """Determine which model table this worksheet is "primarily about".

        Many Tableau worksheets reference a column without an explicit
        (Object!Suffix) hint, leaving the converter unable to tell which
        of several same-named columns is meant. In practice each worksheet
        has *some* fields that ARE unambiguous — either because they only
        live in one table, or because they carry an explicit suffix. Those
        fields vote for a primary table, which is then used as a tiebreaker
        for the rest.

        Voting weights:
            +5  field has a (Object!Suffix) hint that maps to a table
            +1  field resolves to exactly one candidate (no ambiguity)

        Returns None if no table accumulates any votes.
        """
        if not ds_name:
            return None
        all_fields: List[Dict[str, Any]] = []
        all_fields.extend(ws.get("rowFields", []) or [])
        all_fields.extend(ws.get("colFields", []) or [])
        for enc_key in ("colorField", "sizeField", "labelField"):
            enc = ws.get(enc_key) or {}
            if enc.get("field"):
                all_fields.append(enc)
        all_fields.extend(ws.get("detailFields", []) or [])
        all_fields.extend(ws.get("tooltipFields", []) or [])
        for filt in ws.get("filters", []) or []:
            if filt.get("field"):
                all_fields.append({"field": filt["field"]})

        # Map a Tableau object suffix (e.g. 'HCO' from 'Dim!HCO') to a
        # model table name once, so suffix votes can resolve quickly.
        # Restrict to tables owned by this worksheet's datasource so a
        # suffix that exists in BOTH datasources (common when a workbook
        # has a "Logical" and "Physical" version of the same model)
        # never votes for the wrong one.
        model_tables = [
            t["name"] for t in self.model.tables
            if t.get("datasource") == ds_name
        ] or list(self.model.table_columns.keys())
        votes: Dict[str, int] = {}

        def vote(table_name: str, weight: int) -> None:
            if table_name:
                votes[table_name] = votes.get(table_name, 0) + weight

        # The worksheet's wsColumns map preserves the raw column name
        # WITH its (Object!Suffix) when the parser found one in the
        # <datasource-dependencies> block — even if the row/col shelf
        # references the column without the suffix. Voting from this
        # map captures hints that would otherwise be invisible to the
        # shelf-only voter below.
        ws_columns = ws.get("wsColumns") or {}
        for canonical, raw in ws_columns.items():
            m = _OBJ_SUFFIX_RE.search(raw or "")
            if not m:
                continue
            suffix = m.group(1).strip().lower()
            for tbl in model_tables:
                if suffix in tbl.lower():
                    vote(tbl, 5)
                    break

        for f in all_fields:
            fname = (f.get("field") or "").strip()
            if not fname:
                continue
            # Suffix hint — strongest signal.
            m = _OBJ_SUFFIX_RE.search(fname)
            if m:
                suffix = m.group(1).strip().lower()
                for tbl in model_tables:
                    if suffix in tbl.lower():
                        vote(tbl, 5)
                        break
            # Unambiguous lookup.
            from .utils import clean_bracket
            col = clean_bracket(fname)
            cands = self.model.col_locator.get((ds_name, col))
            if not cands:
                # Try alias map.
                aliased = self.model._aliases.get(ds_name, {}).get(col)
                if aliased:
                    cands = self.model.col_locator.get((ds_name, aliased))
            if not cands:
                stripped = SemanticModel._strip_obj_suffix(col)
                if stripped and stripped != col:
                    cands = self.model.col_locator.get((ds_name, stripped))
            if cands and len(cands) == 1:
                vote(cands[0][0], 1)

        if not votes:
            return None
        return max(votes.items(), key=lambda kv: kv[1])[0]

    # ------------------------------------------------------------------
    # Projections
    # ------------------------------------------------------------------

    def _build_projections(
        self, ws: Dict[str, Any], visual_type: str, ds_name: str,
        prefer_table: Optional[str] = None,
    ) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        # Stash the worksheet's primary table so every _add_proj call below
        # can see it without changing each call's signature.
        self._prefer_table_for_ws = prefer_table
        slots = VISUAL_SLOTS.get(visual_type, VISUAL_SLOTS["tableEx"])
        out:   Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

        row_fields = ws.get("rowFields", [])
        col_fields = ws.get("colFields", [])
        color_enc  = ws.get("colorField") or {"field": "", "agg": ""}
        size_enc   = ws.get("sizeField")  or {"field": "", "agg": ""}
        label_enc  = ws.get("labelField") or {"field": "", "agg": ""}
        detail_fields = ws.get("detailFields", [])
        tooltip_fields = ws.get("tooltipFields", [])

        # Why no fallback for empty tooltip_fields:
        #
        # PBI's hover popup auto-shows the values of every bound shelf
        # (Lat/Lon, Size, Color, Details, Rows, Cols), so the visual's
        # default tooltip already mirrors what Tableau shows when no
        # explicit <tooltip> encoding exists. Stuffing those same fields
        # into the Tooltips slot creates duplicates that force PBI to
        # re-fetch them through a separate query path — when the bound
        # main shelves and the duplicated tooltip field live in different
        # tables, that path requires an active relationship that may not
        # exist (or may exist with a different cardinality), and the
        # visual silently fails to load.
        #
        # The Tooltips slot is intentionally reserved for EXTRA fields the
        # user explicitly requested via <tooltip> or <customized-tooltip>
        # — those land in tooltip_fields via the parser and bypass this
        # block entirely.

        def is_measure(f: Dict[str, Any]) -> bool:
            return (f.get("agg") or "").lower() in MEASURE_AGGS

        # All shelf fields (rows+cols) split by measure / dim / geo.
        all_fields = row_fields + col_fields
        category_fields: List[Dict[str, Any]] = []
        value_fields:    List[Dict[str, Any]] = []
        for f in all_fields:
            if is_measure(f):
                value_fields.append(f)
            elif not f.get("isGeo", False):
                category_fields.append(f)

        # ── Map visual: lat/lon shelves -> Latitude/Longitude ───────────
        if visual_type in ("map", "filledMap", "shapeMap"):
            # Latitude and longitude must be bare Column references in PBI
            # — wrapping them in Aggregation (SUM/AVG/etc.) collapses every
            # row's coordinate into a single point and the map renders
            # nothing useful. Tableau frequently tags lat/lon with avg
            # (its default for continuous numerics on the rows/cols
            # shelves), so we explicitly clear the agg before projecting.
            #
            # Detection order:
            # 1. Tableau's `semantic-role='[Geographical].[Latitude]'` /
            #    '[Longitude]' on the underlying column (carried through
            #    the parser as the field's `geoRole`). Authoritative —
            #    survives column renames so a column called 'hcp_lat'
            #    still binds to PBI's Latitude well when its semantic-role
            #    declares it as a coordinate.
            # 2. Name match — fallback for sources that don't carry
            #    explicit semantic-role tagging.
            #
            # Two more rules:
            # 1. Tableau worksheets often place AVG(Latitude) on Rows
            #    TWICE for dual-axis layouts. PBI's map rejects
            #    duplicate slot bindings, so dedupe by field name.
            # 2. 'Latitude (generated)' / 'Longitude (generated)' are
            #    Tableau-internal pseudo-columns derived from the
            #    Country/State field via geocoding. They don't exist
            #    in the underlying data (the resolver correctly drops
            #    them) and binding them is meaningless — PBI auto-
            #    geocodes via the Location slot instead.
            seen_lat: set = set()
            seen_lon: set = set()
            for f in row_fields + col_fields:
                fname = (f.get("field") or "").strip()
                fname_l = fname.lower()
                if "(generated)" in fname_l:
                    continue
                geo_role = (f.get("geoRole") or "").lower()
                is_lat = (
                    geo_role == "latitude"
                    or (not geo_role and "latitude" in fname_l)
                )
                is_lon = (
                    geo_role == "longitude"
                    or (not geo_role and "longitude" in fname_l)
                )
                if is_lat and fname not in seen_lat:
                    seen_lat.add(fname)
                    plain = dict(f); plain["agg"] = ""
                    self._add_proj(out, slots.get("lat") or "Latitude",  plain, ds_name)
                elif is_lon and fname not in seen_lon:
                    seen_lon.add(fname)
                    plain = dict(f); plain["agg"] = ""
                    self._add_proj(out, slots.get("lon") or "Longitude", plain, ds_name)
            # Location-based maps
            if visual_type in ("filledMap", "shapeMap"):
                for f in category_fields:
                    self._add_proj(out, slots.get("location") or "Location", f, ds_name)
            if size_enc["field"]  and slots.get("size"):
                self._add_proj(out, slots["size"],  size_enc,  ds_name)
            if color_enc["field"] and slots.get("color"):
                self._add_proj(out, slots["color"], color_enc, ds_name)
            for f in detail_fields:
                if slots.get("details"):
                    self._add_proj(out, slots["details"], f, ds_name)
            for f in tooltip_fields:
                self._add_proj(out, "Tooltips", f, ds_name)
            # PBI Desktop auto-fills X / Y / Series on map visuals from the
            # Longitude / Latitude / Color shelves when a user opens and
            # saves the report. Emit those mirrors here so the saved file
            # is byte-stable through the open/save cycle. Slot names below
            # are the actual PBI well IDs the map config exposes; we mirror
            # by-slot rather than by-field so a renamed lat/lon column
            # (geoRole=Latitude but field='hcp_lat') still mirrors right.
            self._mirror_projection(out, "X", slots.get("lon") or "Longitude")
            self._mirror_projection(out, "Y", slots.get("lat") or "Latitude")
            if color_enc["field"] and slots.get("color"):
                self._mirror_projection(out, "Series", slots["color"])
            return out

        # ── Slicer: pick the first useful dimension field. ──────────────
        if visual_type == "slicer":
            target = slots.get("value") or "Values"
            picked = (next((f for f in category_fields), None) or
                      next((f for f in row_fields + col_fields if not is_measure(f)), None))
            if picked:
                self._add_proj(out, target, picked, ds_name)
            return out

        # ── Pie / Donut: color is the legend, size/angle is the value. ─
        if visual_type in ("pieChart", "donutChart"):
            cat_slot   = slots.get("category") or "Category"
            value_slot = slots.get("value")    or slots.get("y") or "Y"
            # Category preference: dim on rows/cols beats color encoding
            cats = list(category_fields)
            if not cats and color_enc["field"]:
                cats.append(color_enc)
            for f in cats:
                self._add_proj(out, cat_slot, f, ds_name)
            # Value preference: measure on rows/cols, then size, then label.
            vals = list(value_fields)
            if not vals and size_enc["field"]:
                vals.append(size_enc)
            if not vals and label_enc["field"] and is_measure(label_enc):
                vals.append(label_enc)
            for f in vals:
                self._add_proj(out, value_slot, f, ds_name)
            if color_enc["field"] and slots.get("color"):
                self._add_proj(out, slots["color"], color_enc, ds_name)
            for f in detail_fields:
                if slots.get("details"):
                    self._add_proj(out, slots["details"], f, ds_name)
            for f in tooltip_fields:
                self._add_proj(out, "Tooltips", f, ds_name)
            return out

        # ── Treemap: detail-style dim + size measure. ──────────────────
        if visual_type == "treemap":
            cat_slot   = slots.get("category") or "Category"
            value_slot = slots.get("value")    or "Values"
            cats = list(category_fields)
            if not cats and color_enc["field"]:
                cats.append(color_enc)
            for f in cats:
                self._add_proj(out, cat_slot, f, ds_name)
            vals = list(value_fields)
            if not vals and size_enc["field"]:
                vals.append(size_enc)
            for f in vals:
                self._add_proj(out, value_slot, f, ds_name)
            if color_enc["field"] and slots.get("color"):
                self._add_proj(out, slots["color"], color_enc, ds_name)
            for f in detail_fields:
                if slots.get("details"):
                    self._add_proj(out, slots["details"], f, ds_name)
            for f in tooltip_fields:
                self._add_proj(out, "Tooltips", f, ds_name)
            return out

        # ── Scatter / Bubble: cols = X, rows = Y, plus Color/Size/Details ─
        if visual_type in ("scatterChart", "bubbleChart"):
            if slots.get("x"):
                for f in col_fields:
                    if not f.get("isGeo", False):
                        self._add_proj(out, slots["x"], f, ds_name)
            if slots.get("y"):
                for f in row_fields:
                    if not f.get("isGeo", False):
                        self._add_proj(out, slots["y"], f, ds_name)
            if color_enc["field"] and slots.get("color"):
                self._add_proj(out, slots["color"], color_enc, ds_name)
            if size_enc["field"] and slots.get("size"):
                self._add_proj(out, slots["size"], size_enc, ds_name)
            for f in detail_fields:
                if slots.get("details"):
                    self._add_proj(out, slots["details"], f, ds_name)
            for f in tooltip_fields:
                self._add_proj(out, "Tooltips", f, ds_name)
            return out

        # ── Pivot table: Rows / Columns / Values. ──────────────────────
        if visual_type == "pivotTable":
            out.setdefault("Rows",    {"projections": []})
            out.setdefault("Columns", {"projections": []})
            out.setdefault("Values",  {"projections": []})
            for f in row_fields:
                self._add_proj(out, "Rows",    f, ds_name)
            for f in col_fields:
                self._add_proj(out, "Columns", f, ds_name)
            # If a measure exists on the size or label encoding (e.g. a
            # heatmap-style 'Square' worksheet), drop it into Values.
            if size_enc["field"] and is_measure(size_enc):
                self._add_proj(out, "Values", size_enc, ds_name)
            if label_enc["field"] and is_measure(label_enc):
                self._add_proj(out, "Values", label_enc, ds_name)
            if color_enc["field"]:
                self._add_proj(out, "Values", color_enc, ds_name)
            for f in detail_fields:
                if slots.get("details"):
                    self._add_proj(out, slots["details"], f, ds_name)
            for f in tooltip_fields:
                self._add_proj(out, "Tooltips", f, ds_name)
            return out

        # ── Combo charts (dual axis) ────────────────────────────────────
        if visual_type in ("lineClusteredColumnComboChart",
                           "lineStackedColumnComboChart"):
            cat_slot = slots.get("category")
            if cat_slot:
                for f in category_fields:
                    self._add_proj(out, cat_slot, f, ds_name)
            # Heuristic: if mark is explicitly 'dual', split measures evenly.
            # Otherwise first measure -> Y (columns), second -> Y2 (lines).
            mark = (ws.get("markClass") or "").lower()
            is_explicit_dual = mark == "dual"
            if value_fields:
                self._add_proj(out, slots.get("value_y") or "Y", value_fields[0], ds_name)
            if len(value_fields) > 1:
                self._add_proj(out, slots.get("value_y2") or "Y2", value_fields[1], ds_name)
            if len(value_fields) > 2:
                # 3rd+ measures: push into Series/Color if available
                series_slot = slots.get("color") or "Series"
                for vf in value_fields[2:]:
                    self._add_proj(out, series_slot, vf, ds_name)
            # Fallback encodings
            if not value_fields:
                if size_enc["field"] and is_measure(size_enc):
                    self._add_proj(out, slots.get("value_y") or "Y", size_enc, ds_name)
                elif label_enc["field"] and is_measure(label_enc):
                    self._add_proj(out, slots.get("value_y") or "Y", label_enc, ds_name)
            if color_enc["field"] and slots.get("color"):
                self._add_proj(out, slots["color"], color_enc, ds_name)
            for f in detail_fields:
                if slots.get("details"):
                    self._add_proj(out, slots["details"], f, ds_name)
            for f in tooltip_fields:
                self._add_proj(out, "Tooltips", f, ds_name)
            return out

        # ── Table / tableEx: every field becomes a column in the table. ─
        if visual_type == "tableEx":
            for f in row_fields + col_fields:
                self._add_proj(out, "Values", f, ds_name)
            for f in detail_fields:
                self._add_proj(out, "Values", f, ds_name)
            for f in tooltip_fields:
                self._add_proj(out, "Values", f, ds_name)
            # Encodings that didn't land elsewhere also go to Values.
            if color_enc["field"]:
                self._add_proj(out, "Values", color_enc, ds_name)
            if size_enc["field"]:
                self._add_proj(out, "Values", size_enc, ds_name)
            if label_enc["field"]:
                self._add_proj(out, "Values", label_enc, ds_name)
            return out

        # ── Card / MultiRowCard / CardVisual ──────────────────────────────
        # PBI's modern KPI card (visualType='cardVisual') has a single
        # 'Data' slot that ALWAYS expects an aggregated value — a bare
        # Column reference renders blank. So every field landing on the
        # card gets wrapped in a default aggregation:
        #
        #   * DAX measure                 -> Measure reference (no wrap)
        #   * Tableau measure agg         -> use that agg directly
        #   * String column               -> Min      (Function: 3)
        #   * Numeric / dateTime column   -> CountNonNull / CountD
        #                                    (Function: 5)
        #
        # Min on strings returns the alphabetically first value — works
        # as a sensible "show one value" default that PBI's card UI also
        # produces when a user manually drags a string onto the Data well.
        # CountNonNull on numerics counts the distinct non-null records,
        # which matches PBI's auto-aggregation default for numeric columns
        # in the Data well.
        #
        # Legacy 'card' / 'multiRowCard' types use the old 'Values' /
        # 'Category' wells; we keep that path unchanged for any caller
        # still mapped to those types.
        # Both cardVisual (single-value modern card) and multiRowCard
        # (multi-row legacy card) share the same aggregation-aware field
        # emitter. The difference is just the slot name and how many
        # fields land in the "primary" slot:
        #   * cardVisual  -> first field on Data, rest on Tooltips
        #   * multiRowCard -> ALL fields on Values
        if visual_type in ("cardVisual", "multiRowCard"):
            if visual_type == "multiRowCard":
                data_slot    = slots.get("value") or "Values"
                tooltip_slot = slots.get("tooltip") or "Tooltips"
                multi_value  = True   # every projection lands in data_slot
            else:
                data_slot    = slots.get("value") or "Data"
                tooltip_slot = slots.get("tooltip") or "Tooltips"
                multi_value  = False  # first → Data; rest → Tooltips
            seen_card: set = set()
            data_taken = False

            def _add_card_field(f: Dict[str, Any], slot: str) -> None:
                """Emit a card projection with default aggregation when
                the field's binding is a bare Column."""
                fname = f.get("field", "")
                if not fname or fname in seen_card:
                    return
                seen_card.add(fname)
                # Skip Tableau-generated geo pseudo-columns — they don't
                # exist in the data and would resolve to nothing.
                if "(generated)" in fname.lower():
                    return
                # Resolve via the same path _add_proj uses so we know
                # what the field will look like in PBI's projection JSON.
                ws_columns = getattr(self, "_ws_columns", None) or {}
                hint = fname
                canon = SemanticModel._strip_obj_suffix(fname) or fname
                raw   = ws_columns.get(canon) or ws_columns.get(fname)
                if raw and "(" in raw and ")" in raw and "(" not in fname:
                    hint = raw
                loc = self.model.resolve_field(ds_name, hint,
                                               prefer_table=prefer_table)
                if not loc:
                    loc = self._hint_lookup(ds_name, fname)
                if not loc:
                    print(f"[RESOLVE] '{fname}' (ds={ds_name}) not found "
                          f"— field dropped from card.")
                    return
                tbl, col = loc
                # Date-part redirect — same logic as _add_proj. When the
                # agg is yr/qr/mn/dy and the resolved column is dateTime,
                # bind to the synthesized hierarchy column emitted by
                # the model (Year of <Date>, Quarter of <Date>, etc.)
                # rather than the raw date column. PBI has no date-part
                # aggregation function, so without this redirection the
                # card would render the raw date instead of the year.
                #
                # The synthesized columns aren't in model.tables (they're
                # written straight into TMDL), so the tmdl-type lookup
                # below would miss them. Track the redirect's TMDL type
                # explicitly so the agg-default picker still gets the
                # right Min/CountNonNull fork.
                agg_field = (f.get("agg") or "").lower()
                date_part_levels = {
                    "yr": ("Year",    "int64"),
                    "qr": ("Quarter", "string"),
                    "mn": ("Month",   "string"),
                    "dy": ("Day",     "int64"),
                }
                redirect_type: Optional[str] = None
                if (agg_field in date_part_levels
                        and self.model.is_datetime_col(tbl, col)):
                    level_name, redirect_type = date_part_levels[agg_field]
                    col = f"{level_name} of {col}"
                    f = dict(f)
                    f["agg"] = ""
                # DAX measures: emit Measure ref directly (no wrap).
                if self.model.is_measure_ref(tbl, col):
                    out.setdefault(slot, {"projections": []})
                    out[slot]["projections"].append({
                        "field": {
                            "Measure": {
                                "Expression": {"SourceRef": {"Entity": tbl}},
                                "Property":   col,
                            },
                        },
                        "queryRef":       f"{tbl}.{col}",
                        "nativeQueryRef": col,
                        "active":         True,
                        "displayName":    col,
                    })
                    return
                # Pick the aggregation function:
                #   1. Tableau-supplied measure agg (sum/avg/min/max/...)
                #   2. tmdlType-driven default for bare columns
                agg_key  = (f.get("agg") or "").lower()
                agg_info = AGG_TABLE.get(agg_key)
                if agg_info and agg_info.get("is_measure") and agg_info.get("fn") is not None:
                    fn_code = agg_info["fn"]
                    fn_label = agg_info["label"]
                else:
                    tmdl_type = (redirect_type
                                 or self._column_tmdl_type(tbl, col))
                    if tmdl_type == "string":
                        fn_code, fn_label = 3, "Min"
                    elif tmdl_type in ("int64", "double", "decimal", "dateTime"):
                        fn_code, fn_label = 5, "CountNonNull"
                    else:
                        fn_code, fn_label = 3, "Min"
                out.setdefault(slot, {"projections": []})
                out[slot]["projections"].append({
                    "field": {
                        "Aggregation": {
                            "Expression": {
                                "Column": {
                                    "Expression": {"SourceRef": {"Entity": tbl}},
                                    "Property":   col,
                                },
                            },
                            "Function": fn_code,
                        },
                    },
                    "queryRef":       f"{fn_label}({tbl}.{col})",
                    "nativeQueryRef": col,
                    "active":         True,
                    "displayName":    col,
                })

            def _route_card(f: Dict[str, Any]) -> None:
                nonlocal data_taken
                # multiRowCard: every successful add lands in the Values
                # slot. cardVisual: first add goes to Data; everything
                # after that overflows to Tooltips so it stays reachable.
                if multi_value:
                    slot = data_slot
                else:
                    slot = data_slot if not data_taken else tooltip_slot
                before = len(seen_card)
                _add_card_field(f, slot)
                if not data_taken and len(seen_card) > before:
                    data_taken = True

            # Priority order: shelf fields → label encoding(s) → extras.
            # Tableau text-mark cards put their primary value(s) on the
            # label shelf. We iterate ALL labelFields so a worksheet
            # with multiple <text> encodings doesn't lose values — even
            # when the visual_picker keeps the worksheet on cardVisual,
            # only the first one lands in Data; the rest fall through
            # to Tooltips so they remain reachable.
            label_fields = ws.get("labelFields") or (
                [label_enc] if label_enc.get("field") else []
            )
            if all_fields:
                for f in all_fields:
                    _route_card(f)
                for f in label_fields:
                    _route_card(f)
            else:
                for f in label_fields:
                    _route_card(f)
            for f in detail_fields:
                _route_card(f)
            for f in tooltip_fields:
                _route_card(f)
            if color_enc["field"]:
                _route_card(color_enc)
            if size_enc["field"]:
                _route_card(size_enc)
            return out

        if visual_type in ("card", "cardVisual", "multiRowCard"):
            # Legacy card path (kept for compatibility — everything new
            # routes to cardVisual above).
            value_slot    = slots.get("value")    or "Values"
            category_slot = slots.get("category") or "Category"
            tooltip_slot  = slots.get("tooltip")  or "Tooltips"
            seen_card: set = set()
            value_taken    = False
            category_taken = False

            def _is_value_field(f: Dict[str, Any]) -> bool:
                if is_measure(f):
                    return True
                if (f.get("agg") or "").lower() == "usr":
                    return True
                fname = (f.get("field") or "").strip()
                if not fname:
                    return False
                ws_columns = getattr(self, "_ws_columns", None) or {}
                hint = fname
                canon = SemanticModel._strip_obj_suffix(fname) or fname
                raw   = ws_columns.get(canon) or ws_columns.get(fname)
                if raw and "(" in raw and ")" in raw and "(" not in fname:
                    hint = raw
                loc = self.model.resolve_field(ds_name, hint,
                                               prefer_table=prefer_table)
                if not loc:
                    return False
                tbl, col = loc
                if self.model.is_measure_ref(tbl, col):
                    return True
                for t in self.model.tables:
                    if t.get("name") != tbl:
                        continue
                    for c in (t.get("columns") or []):
                        if c.get("name") == col:
                            return c.get("role") == "measure"
                    break
                return False

            def _route_card(f: Dict[str, Any]) -> None:
                nonlocal value_taken, category_taken
                fname = f.get("field", "")
                if not fname or fname in seen_card:
                    return
                seen_card.add(fname)
                if _is_value_field(f):
                    if not value_taken:
                        self._add_proj(out, value_slot, f, ds_name)
                        value_taken = True
                    else:
                        self._add_proj(out, tooltip_slot, f, ds_name)
                else:
                    if not category_taken:
                        self._add_proj(out, category_slot, f, ds_name)
                        category_taken = True
                    else:
                        self._add_proj(out, tooltip_slot, f, ds_name)

            if label_enc["field"]:
                _route_card(label_enc)
            for f in all_fields:
                _route_card(f)
            for f in detail_fields:
                _route_card(f)
            for f in tooltip_fields:
                _route_card(f)
            if color_enc["field"]:
                _route_card(color_enc)
            if size_enc["field"]:
                _route_card(size_enc)
            return out

        # ── Generic chart (bar / column / line / area / ribbon / etc.) ──
        cat_slot = slots.get("category")
        if cat_slot:
            for f in category_fields:
                self._add_proj(out, cat_slot, f, ds_name)

        value_slot = (slots.get("value") or slots.get("value_y") or
                      slots.get("value_x") or slots.get("y"))
        y2_slot    = slots.get("value_y2") or slots.get("y2")

        if value_slot:
            for i, f in enumerate(value_fields):
                if i == 0:
                    self._add_proj(out, value_slot, f, ds_name)
                elif i == 1 and y2_slot:
                    self._add_proj(out, y2_slot, f, ds_name)
                else:
                    # Additional measures create clustered groups in PBI
                    # when placed in the same primary slot.
                    self._add_proj(out, value_slot, f, ds_name)
            # Fallback: a measure on the size/label encoding becomes the
            # value if rows/cols didn't supply one.
            if not value_fields:
                if size_enc["field"] and is_measure(size_enc):
                    self._add_proj(out, value_slot, size_enc, ds_name)
                elif label_enc["field"] and is_measure(label_enc):
                    self._add_proj(out, value_slot, label_enc, ds_name)

        # Color encoding -> Series/Color/Legend slot.
        if color_enc["field"] and slots.get("color"):
            self._add_proj(out, slots["color"], color_enc, ds_name)

        # Size encoding -> Size slot.
        if size_enc["field"] and slots.get("size"):
            self._add_proj(out, slots["size"], size_enc, ds_name)

        # Detail fields -> Details slot
        for f in detail_fields:
            if slots.get("details"):
                self._add_proj(out, slots["details"], f, ds_name)

        # Tooltip fields
        for f in tooltip_fields:
            if f.get("field"):
                self._add_proj(out, "Tooltips", f, ds_name)

        return out

    def _add_proj(
        self,
        projections:  Dict[str, Dict[str, List[Dict[str, Any]]]],
        slot:         str,
        field:        Dict[str, Any],
        ds_name:      str,
        prefer_table: Optional[str] = None,
    ) -> None:
        if not slot:
            return
        fname = field.get("field", "")
        if not fname:
            return
        # Fall back to the worksheet-level primary table when the caller
        # didn't pass one explicitly. _build_projections stashes it on the
        # instance for the duration of building one visual.
        if prefer_table is None:
            prefer_table = getattr(self, "_prefer_table_for_ws", None)
        # Phase D — per-binding datasource override. When the encoding
        # carries a `[ds].[col]` prefix that points at a non-primary
        # datasource (Tableau data blending), the resolver should look
        # in THAT ds's column registry, not the worksheet's primary.
        # Falls back to ds_name when the field is bare (no prefix) or
        # when the override doesn't resolve. Logs a [BLEND] note when
        # the override actually fires so behaviour is visible.
        binding_ds = (field.get("datasource") or "").strip()
        loc: Optional[Tuple[str, str]] = None
        if binding_ds and binding_ds != ds_name and binding_ds.lower() != "parameters":
            override_loc = self.model.resolve_field(
                binding_ds, fname, prefer_table=None,
            )
            if override_loc:
                tbl, _ = override_loc
                print(
                    f"[BLEND] binding routed: '{fname}' -> "
                    f"ds={binding_ds} table={tbl} (primary ds={ds_name})"
                )
                loc = override_loc
        # The worksheet's <datasource-dependencies> block may declare this
        # column under a disambiguated raw name like "[HCP_ID (Dim!HCO)]"
        # even when the visual references it as plain "HCP_ID". Promote
        # the raw name to the resolver hint so the (Object!Suffix) regex
        # in _pick_from_candidates can pick the right logical table.
        ws_columns = getattr(self, "_ws_columns", None) or {}
        hint = fname
        canonical = SemanticModel._strip_obj_suffix(fname) or fname
        raw = ws_columns.get(canonical) or ws_columns.get(fname)
        if raw and "(" in raw and ")" in raw and "(" not in fname:
            hint = raw
        if loc is None:
            loc = self.model.resolve_field(ds_name, hint, prefer_table=prefer_table)
        if not loc:
            loc = self._hint_lookup(ds_name, fname)
        if not loc:
            # Honest behavior: a field that doesn't exist gets dropped
            # silently, but the visual itself stays so the layout is
            # preserved.
            print(f"[RESOLVE] '{fname}' (ds={ds_name}) not found — field dropped from visual.")
            return
        tbl, col = loc
        agg_key  = field.get("agg", "").lower()

        # Redirect date-part aggregations to the precomputed hierarchy
        # column the model writer emits for every dateTime column.
        # Tableau's `yr:date_added:ok` resolves to ('netflix_titles.csv',
        # 'Date Added'), but the visual really wants the YEAR of that
        # date — bind to the synthesized 'Year of Date Added' column,
        # which carries the year as a precomputed integer. PBI doesn't
        # have a date-part aggregation function, so without redirection
        # we'd silently drop the agg and the card would show the raw
        # date value.
        # No has_column guard needed: model.write_tmdl unconditionally
        # emits Year/Quarter/Month/Day + Year-Trunc/Year-Quarter/Year-Month
        # columns for every dateTime column.
        #
        # Two flavours:
        #   * date-part   (yr/qr/mn/dy) — extract an integer level; bind
        #     to ``Year of X`` / ``Quarter of X`` / ``Month of X`` / ``Day of X``.
        #   * truncate-date (ty/tqr/tmn/tmd) — return a *date* truncated to
        #     the period start; bind to ``Year-Trunc of X`` / ``Year-Quarter
        #     of X`` / ``Year-Month of X`` / X itself for day. Tableau filter
        #     widgets and continuous date axes use the truncate-date form
        #     (e.g. `tmn:Date of Visit` for a month-by-month filter slicer).
        date_part_levels = {"yr": "Year", "qr": "Quarter",
                            "mn": "Month", "dy": "Day"}
        date_trunc_levels = {
            "ty":  "Year-Trunc",
            "tqr": "Year-Quarter",
            "tmn": "Year-Month",
        }
        if (agg_key in date_part_levels
                and self.model.is_datetime_col(tbl, col)):
            col = f"{date_part_levels[agg_key]} of {col}"
            agg_key = ""
        elif (agg_key in date_trunc_levels
                and self.model.is_datetime_col(tbl, col)):
            col = f"{date_trunc_levels[agg_key]} of {col}"
            agg_key = ""
        elif (agg_key in ("tmd", "td")
                and self.model.is_datetime_col(tbl, col)):
            # Truncate-Day is a no-op when the source is a pure date with
            # no time component — the original column is already the
            # truncated value. Just drop the agg and bind to the column.
            agg_key = ""

        agg_info = AGG_TABLE.get(agg_key)

        if self.model.is_measure_ref(tbl, col):
            # Resolved field is a DAX measure (translated from a Tableau
            # calculated field). Emit a Measure reference — measures
            # already encode their aggregation in the DAX expression, so
            # wrapping them in Aggregation is invalid. This is the
            # 'usr'-aggregation case in Tableau's XML.
            entry = {
                "field": {
                    "Measure": {
                        "Expression": {"SourceRef": {"Entity": tbl}},
                        "Property":   col,
                    },
                },
                "queryRef":       f"{tbl}.{col}",
                "nativeQueryRef": col,
                "active":         True,
                "displayName":    col,
            }
        elif agg_info and agg_info["is_measure"] and agg_info.get("fn") is not None:
            entry = {
                "field": {
                    "Aggregation": {
                        "Expression": {
                            "Column": {
                                "Expression": {"SourceRef": {"Entity": tbl}},
                                "Property":   col,
                            },
                        },
                        "Function": agg_info["fn"],
                    },
                },
                "queryRef":       f"{agg_info['label']}({tbl}.{col})",
                "nativeQueryRef": col,
                "active":         True,
                "displayName":    f"{agg_info['label']} of {col}",
            }
        else:
            entry = {
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
            }
        projections.setdefault(slot, {"projections": []})
        projections[slot]["projections"].append(entry)

    @staticmethod
    def _mirror_projection(
        projections: Dict[str, Dict[str, List[Dict[str, Any]]]],
        dst_slot: str, src_slot: str,
    ) -> None:
        """Copy the first projection under `src_slot` to `dst_slot` without
        the `active: true` marker.

        PBI Desktop's map visual auto-fills X/Y/Series wells from Lat/Lon/
        Color when a user saves the report. Emitting those mirror entries
        up-front keeps the saved JSON byte-stable through the open/save
        cycle — otherwise PBI Desktop rewrites the file on first open and
        every diff between converter output and saved file shows mirror
        churn that has no semantic meaning.
        """
        src = projections.get(src_slot)
        if not src or not src.get("projections"):
            return
        # Strip 'active' — PBI marks only the originals as active; mirrors
        # are inert helpers that follow the original's binding.
        mirror = {k: v for k, v in src["projections"][0].items()
                  if k != "active"}
        projections.setdefault(dst_slot, {"projections": []})
        projections[dst_slot]["projections"].append(mirror)

    @staticmethod
    def _projection_field_key(field: Dict[str, Any]) -> Tuple[str, str, bool]:
        """Return (table, column, is_aggregated) for a projection's field
        dict. Empty strings when unrecognized.
        """
        if "Column" in field:
            c = field["Column"]
            tbl = (c.get("Expression") or {}).get("SourceRef", {}).get("Entity", "")
            return tbl, c.get("Property", ""), False
        if "Measure" in field:
            m = field["Measure"]
            tbl = (m.get("Expression") or {}).get("SourceRef", {}).get("Entity", "")
            return tbl, m.get("Property", ""), False
        if "Aggregation" in field:
            inner = ((field["Aggregation"].get("Expression") or {}).get("Column")
                     or {})
            tbl = (inner.get("Expression") or {}).get("SourceRef", {}).get("Entity", "")
            return tbl, inner.get("Property", ""), True
        return "", "", False

    def _build_per_category_color_block(
        self, ws: Dict[str, Any], ds_name: str, visual_type: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """Translate Tableau's datasource-level color palette overrides
        into per-category PBI dataPoint entries.

        Looks up the worksheet's color-encoding field in the datasource's
        `colorMaps` dict (parsed from `<encoding attr='color'>` style
        rules). For each `{bucket_value: hex}` pair, emits a dataPoint
        property entry where the selector scopes the fill to that one
        category value via a `scopeId` Comparison expression
        (Equality / ComparisonKind=0):

            {
              "properties": {"fill": {...}},
              "selector": {
                "data": [{"scopeId": {
                  "Comparison": {
                    "ComparisonKind": 0,
                    "Left":  {"Column": {... <field> ...}},
                    "Right": {"Literal": {"Value": "'<bucket>'"}}
                  }
                }}]
              }
            }

        This shape is the schema-correct selector for "match all rows
        whose <Column> equals <Literal>". The earlier dataViewWildcard
        attempt was rejected at load time — the formattingObjectDefinitions
        schema requires `matchingOption` and forbids the `matchingConditions`
        / `target` properties we previously emitted.

        Falls back to None when the worksheet has no color encoding, the
        field can't be resolved, or no matching `colorMaps` entry exists.
        """
        color_enc = ws.get("colorField") or {}
        color_field = (color_enc.get("field") or "").strip()
        if not color_field:
            return None

        color_map = self._lookup_color_map(ds_name, color_field)
        if not color_map:
            return None

        # Resolve the field to (table, col) so the selector references
        # the same column as the visual's Series binding.
        ws_columns = getattr(self, "_ws_columns", None) or {}
        prefer_table = getattr(self, "_prefer_table_for_ws", None)
        hint = color_field
        canon = SemanticModel._strip_obj_suffix(color_field) or color_field
        raw = ws_columns.get(canon) or ws_columns.get(color_field)
        if raw and "(" in raw and ")" in raw and "(" not in color_field:
            hint = raw
        loc = self.model.resolve_field(ds_name, hint,
                                       prefer_table=prefer_table)
        if not loc:
            return None
        tbl, col = loc

        entries: List[Dict[str, Any]] = []
        for bucket_value, hex_color in color_map.items():
            esc = bucket_value.replace("'", "''")
            entries.append({
                "properties": {
                    "fill": _color_expr(hex_color),
                },
                "selector": {
                    "data": [{
                        "scopeId": {
                            "Comparison": {
                                "ComparisonKind": 0,
                                "Left": {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Entity": tbl}},
                                        "Property":   col,
                                    },
                                },
                                "Right": {
                                    "Literal": {"Value": f"'{esc}'"},
                                },
                            },
                        },
                    }],
                },
            })
        return entries or None

    def _lookup_color_map(
        self, ds_name: str, field_ref: str,
    ) -> Dict[str, str]:
        """Look up a datasource's colorMaps entry for a field reference,
        trying the raw form first and then the canonical (suffix-stripped)
        name. Returns {} when nothing matches.
        """
        for ds in self.datasources:
            if ds.get("name") != ds_name:
                continue
            cmap = ds.get("colorMaps") or {}
            if not cmap:
                return {}
            if field_ref in cmap:
                return cmap[field_ref]
            canon = SemanticModel._strip_obj_suffix(field_ref) or field_ref
            return cmap.get(canon, {})
        return {}

    def _column_tmdl_type(self, tbl: str, col: str) -> str:
        """Return the TMDL dataType for a (table, column) pair, or '' when
        the column isn't found. Used to pick a default aggregation when
        binding a bare column to a card or other slot that requires one."""
        for t in self.model.tables:
            if t.get("name") != tbl:
                continue
            for c in t.get("columns") or []:
                if c.get("name") == col:
                    return c.get("tmdlType", "")
            for m in t.get("measures") or []:
                if m.get("name") == col:
                    return "measure"
            break
        return ""

    def _column_filter_type(self, tbl: str, col: str) -> str:
        """Return PBI filter `type` for a bare column reference.

        String columns get 'Categorical' (PBI's In-list filter UI).
        Numeric / date / boolean columns get 'Advanced' (range-style filter).
        Aggregated bindings always get 'Advanced' regardless of the
        underlying column type — the caller passes is_aggregated and
        bypasses this check.
        """
        for t in self.model.tables:
            if t.get("name") != tbl:
                continue
            for c in t.get("columns") or []:
                if c.get("name") == col:
                    return ("Categorical" if c.get("tmdlType") == "string"
                            else "Advanced")
            for m in t.get("measures") or []:
                if m.get("name") == col:
                    return "Advanced"
            break
        return "Categorical"

    def _build_auto_filters(
        self,
        projections: Dict[str, Dict[str, List[Dict[str, Any]]]],
        existing_keys: set,
        ws_name: str,
    ) -> List[Dict[str, Any]]:
        """Build the per-shelf-binding visual-level filters PBI Desktop
        auto-stamps on save.

        For every projection on the visual that isn't already present in
        `existing_keys` (set of (table, column, is_aggregated) tuples
        from user-defined filters), emit a filter entry with a hex name,
        no `howCreated`, no `displayName`, no inner `filter` clause —
        just `name` / `field` / `type`. This matches the shape PBI Desktop
        writes on first open/save of any visual that has shelf bindings.

        Mirror projections (X / Y / Series with no `active` marker) are
        skipped so we don't double-stamp lat/lon/color filters.
        """
        out: List[Dict[str, Any]] = []
        # Stable iteration order so hex_ids are deterministic across runs.
        for slot in sorted(projections.keys()):
            slot_data = projections[slot] or {}
            for proj in slot_data.get("projections") or []:
                # Skip mirror projections — only the original binding
                # gets a filter entry. Mirrors are easy to spot: they
                # lack the `active` key (originals always have it).
                if not proj.get("active"):
                    continue
                field = proj.get("field") or {}
                tbl, col, is_agg = self._projection_field_key(field)
                if not tbl or not col:
                    continue
                key = (tbl, col, is_agg)
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                ftype = ("Advanced" if is_agg
                         else self._column_filter_type(tbl, col))
                # hex_id keyed on (ws_name, slot, tbl, col, agg-flag) so
                # the same field projected onto two slots gets two distinct
                # filter names — matches PBI Desktop's per-binding stamping.
                fname = hex_id("autofilter", ws_name, slot, tbl, col,
                               "agg" if is_agg else "col")
                out.append({"name": fname, "field": field, "type": ftype})
        return out

    # ------------------------------------------------------------------
    # Decorations: textboxes and placeholder slicers
    # ------------------------------------------------------------------

    def _build_textbox(
        self, label: str, x: int, y: int, w: int, h: int, z: int, zid: str,
        style: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        style = style or {}

        # PBI textbox paragraph runs use CSS-style keys (fontSize, color,
        # fontFamily, fontWeight, fontStyle, textDecoration). The "pt"
        # suffix on fontSize is what PBI Desktop expects for textbox runs.
        text_style: Dict[str, Any] = {}
        if "fontSize" in style:
            fs = style["fontSize"]
            text_style["fontSize"] = (f"{fs}pt" if isinstance(fs, (int, float))
                                      else str(fs))
        if "fontFamily" in style:
            text_style["fontFamily"] = style["fontFamily"]
        if "fontColor" in style:
            text_style["color"] = style["fontColor"]
        weight = style.get("fontWeight")
        if weight:
            text_style["fontWeight"] = weight
        if style.get("italic"):
            text_style["fontStyle"] = "italic"
        if style.get("underline"):
            text_style["textDecoration"] = "underline"

        text_run: Dict[str, Any] = {"value": label or " "}
        if text_style:
            text_run["textStyle"] = text_style

        paragraph: Dict[str, Any] = {"textRuns": [text_run]}
        align = style.get("textAlign")
        if align:
            # Per-paragraph alignment lives on the paragraph itself, not
            # on the run. PBI accepts 'left' | 'center' | 'right'.
            paragraph["horizontalTextAlignment"] = align

        objects: Dict[str, Any] = {
            "general": [{
                "properties": {"paragraphs": [paragraph]},
            }],
        }

        # Container background and border live on `visualContainerObjects`
        # — the chrome bag PBI reads for the visual's outer frame. We
        # were previously emitting them under `objects` which is the
        # data-formatting bag; PBI Desktop ignores entries it doesn't
        # recognise there, so the colored tile silently went missing
        # for textbox visuals.
        container_objects: Dict[str, Any] = {}
        if style.get("backgroundColor"):
            container_objects["background"] = [{
                "properties": {
                    "show":  _expr_lit("true"),
                    "color": _color_expr(style["backgroundColor"]),
                },
            }]

        # Container border — only emitted when the parser saw a real
        # border (style.borderStyle != 'none' was already filtered).
        if (style.get("borderColor") or style.get("borderWidth")
                or style.get("borderStyle")):
            border_props: Dict[str, Any] = {"show": _expr_lit("true")}
            if style.get("borderColor"):
                border_props["color"] = _color_expr(style["borderColor"])
            if style.get("borderWidth"):
                border_props["width"] = _expr_lit(f"{style['borderWidth']}D")
            container_objects["border"] = [{"properties": border_props}]

        visual_block: Dict[str, Any] = {
            "visualType":              "textbox",
            "drillFilterOtherVisuals": True,
            "objects":                 objects,
        }
        if container_objects:
            visual_block["visualContainerObjects"] = container_objects
        return {
            "$schema": SCHEMA["visual"],
            "name":    hex_id("visual-text", zid or label, str(x), str(y)),
            "position": {
                "x": x, "y": y, "z": z,
                "height": h, "width": w,
                "tabOrder": z,
            },
            "visual": visual_block,
            "filterConfig": {"filters": []},
        }

    def _build_page_navigator(
        self, x: int, y: int, w: int, h: int, z: int, zid: str,
        style: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a Power BI pageNavigator visual.

        The pageNavigator shows all report pages as clickable tabs/buttons,
        matching Tableau's goto-sheet navigation buttons.
        """
        style = style or {}
        objects: Dict[str, Any] = {}

        if "backgroundColor" in style:
            objects["background"] = [{
                "properties": {
                    "show":  {"expr": {"Literal": {"Value": "true"}}},
                    "color": {"solid": {"color": style["backgroundColor"]}},
                },
            }]

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

    def _build_placeholder_slicer(
        self, label: str,
        x: int, y: int, w: int, h: int, z: int, zid: str,
        field: Optional[Dict[str, Any]] = None,
        ds_name: str = "",
        title_style: Optional[Dict[str, Any]] = None,
        title_enabled: bool = True,
        widget_mode: str = "",
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
        # Slicer with a title. If the filter field can be resolved
        # against the model, we bind it so the slicer actually works.
        projections: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        if field and ds_name:
            # Filter slicers typically carry an explicit (Object!Suffix)
            # in the field reference, so suffix-based resolution is enough.
            # Clear any worksheet-level state that may have leaked from
            # the previously-built chart visual.
            self._prefer_table_for_ws = None
            self._ws_columns = {}
            self._add_proj(projections, "Values", field, ds_name)

        slicer_visual: Dict[str, Any] = {
            "visualType":              "slicer",
            "drillFilterOtherVisuals": True,
        }
        # Title is forced off, but a background carried in title_style
        # should still land on the visual container — pass enabled=False
        # to _title_object and merge the background separately.
        container = self._title_object(label, title_style, enabled=False)
        if title_style and title_style.get("backgroundColor"):
            container = container or {}
            container["background"] = [{
                "properties": {
                    "show":  _expr_lit("true"),
                    "color": _color_expr(title_style["backgroundColor"]),
                },
            }]
        if container:
            slicer_visual["visualContainerObjects"] = container

        slicer_objects = self._slicer_mode_objects(widget_mode)
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

    # ------------------------------------------------------------------
    # Slicer widget mode (single vs multi-select, list vs dropdown)
    # ------------------------------------------------------------------

    @staticmethod
    def _slicer_mode_objects(mode: str) -> Dict[str, Any]:
        """Translate Tableau filter-widget `mode` into PBI slicer settings.

        Tableau widget modes (lowercase, from the <zone mode='...'> attr).
        These are the actual tokens Tableau emits — verified by surveying
        the `mode='...'` attribute frequency across our workbook corpus:

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
            objects.data[0].properties.mode  = 'Dropdown' | 'Basic'
                (Basic = list. Dropdown = collapsed dropdown control.)
            objects.general[0].properties.singleSelect  = true | false
                (false = multi-select; true = single-select)

        Returns {} when the mode doesn't map to a list/dropdown control —
        e.g. a range slider — so PBI Desktop falls back to its default.
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
            general_props["singleSelect"] = _expr_lit("true")
        elif is_multi:
            general_props["singleSelect"] = _expr_lit("false")
        if general_props:
            objects["general"] = [{"properties": general_props}]

        if is_dropdown:
            objects["data"] = [{
                "properties": {"mode": _expr_lit("'Dropdown'")},
            }]
        return objects

    # ------------------------------------------------------------------
    # Title objects with styling
    # ------------------------------------------------------------------

    @staticmethod
    def _title_object(
        text: str,
        style: Optional[Dict[str, Any]] = None,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        """Build a visualContainerObjects dict carrying the title (and an
        optional sibling background).

        Returns {} when the title is disabled AND no styling exists (the
        caller can omit visualContainerObjects entirely). When disabled
        with no style we don't even emit a show=false marker — the absence
        of a title object is what hides it in PBI Desktop. When the title
        IS disabled but a style was specified we still skip emitting it,
        because styling a hidden title is meaningless.

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
            return {}

        style = style or {}
        safe = (text or "").replace("'", "''")

        properties: Dict[str, Any] = {
            "show": _expr_lit("true"),
            "text": _expr_lit(f"'{safe}'"),
        }

        size_lit = _normalize_font_size(style.get("fontSize"))
        if size_lit:
            properties["fontSize"] = _expr_lit(size_lit)

        # Always emit a fontFamily — Arial when Tableau's choice isn't a
        # PBI-shipped font, or when no font was specified. Avoids falling
        # to PBI's invisible-default behavior for unknown fonts.
        if style.get("fontFamily") or "fontFamily" not in style:
            family = _safe_font_family(style.get("fontFamily"))
            properties["fontFamily"] = _expr_lit(f"'{family}'")

        if style.get("fontColor"):
            properties["fontColor"] = _color_expr(style["fontColor"])

        if str(style.get("fontWeight", "")).lower() == "bold":
            properties["bold"] = _expr_lit("true")
        if style.get("italic"):
            properties["italic"] = _expr_lit("true")
        if style.get("underline"):
            properties["underline"] = _expr_lit("true")

        align = style.get("textAlign") or style.get("alignment")
        if align:
            properties["alignment"] = _expr_lit(f"'{align}'")

        objects: Dict[str, Any] = {"title": [{"properties": properties}]}

        # PBI puts the visualContainer background as its own sibling entry,
        # not inside the title block. This is also what tames the "white
        # rectangle" rendering quirks we used to get when the field was
        # nested.
        if style.get("backgroundColor"):
            objects["background"] = [{
                "properties": {
                    "show":  _expr_lit("true"),
                    "color": _color_expr(style["backgroundColor"]),
                },
            }]

        return objects

    @staticmethod
    def _label_object(
        enabled: Optional[bool],
        style: Optional[Dict[str, Any]] = None,
        visual_type: str = "",
    ) -> Dict[str, Any]:
        """Build an `objects` dict carrying mark-label (data-label) settings.

        `enabled=False` -> emit show=false so PBI explicitly turns labels
        off (overriding any default-on behavior). `enabled=True` with a
        style emits show=true plus formatting. Returns {} when there is
        nothing to say (enabled is None and style is empty).

        The PBI label-bag name varies by visual type:
            tableEx / pivotTable -> 'values'
            map / filledMap -> 'dataLabels'
            everything else -> 'labels'
        Picking the right bag means PBI Desktop actually applies the
        formatting instead of silently ignoring it.
        """
        style = style or {}
        if enabled is None and not style:
            return {}

        if visual_type in ("tableEx", "pivotTable"):
            bag = "values"
        elif visual_type in ("map", "filledMap", "shapeMap"):
            bag = "dataLabels"
        else:
            bag = "labels"

        properties: Dict[str, Any] = {}
        if enabled is False:
            properties["show"] = _expr_lit("false")
        elif enabled is True or style:
            properties["show"] = _expr_lit("true")

        if style and (enabled is not False):
            size_lit = _normalize_font_size(style.get("fontSize"))
            if size_lit:
                properties["fontSize"] = _expr_lit(size_lit)
            if style.get("fontFamily") or "fontFamily" in style:
                family = _safe_font_family(style.get("fontFamily"))
                properties["fontFamily"] = _expr_lit(f"'{family}'")
            if style.get("fontColor"):
                properties["color"] = _color_expr(style["fontColor"])
            if str(style.get("fontWeight", "")).lower() == "bold":
                properties["bold"] = _expr_lit("true")
            if style.get("italic"):
                properties["italic"] = _expr_lit("true")
            if style.get("underline"):
                properties["underline"] = _expr_lit("true")

        if not properties:
            return {}
        return {bag: [{"properties": properties}]}

