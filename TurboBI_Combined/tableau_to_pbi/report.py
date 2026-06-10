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
    DEFAULT_PAGE_HEIGHT,
    DEFAULT_PAGE_WIDTH,
)
from .field_resolver import FieldResolver
from .model import SemanticModel
from .utils import hex_id
from .visuals.chart import ChartBuilder
from .visuals.navigator import (
    build_page_navigator,
    collect_canonical_nav_styles,
)
from .visuals.slicer import build_placeholder_slicer
from .visuals.textbox import build_textbox


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
        # Worksheet-aware field resolver. Owns the (table, column)
        # lookup path and the projection-shape emission every chart
        # helper relies on.
        self.resolver = FieldResolver(datasources, worksheets, model, hints)
        self._ws_map = self.resolver._ws_map
        # Chart visual constellation lives in visuals/chart.py. The
        # bookmark side-effect (chart can route a worksheet to an
        # actionButton, which needs the `_default_state` bookmark to
        # exist) is read back from ``chart_builder.needs_default_bookmark``.
        self.chart_builder = ChartBuilder(datasources, model, self.resolver)
        # Canonical pageNavigator styling — computed lazily on first
        # access from the first dashboard that has `goto-sheet` zones,
        # then reused on every other page that renders a navigator.
        # This keeps the navigator's font / color / fill consistent
        # across every page even when later dashboards style their own
        # navigation buttons differently (or omit them entirely).
        self._canonical_nav_styles: Optional[List[Dict[str, Any]]] = None

    def bookmarks_to_emit(self) -> List[Dict[str, Any]]:
        """Return the list of bookmark dicts the writer should serialize.

        Currently produces at most one: ``_default_state``, the
        unfiltered baseline that 'Reset Filters' buttons jump back to.
        """
        if not self.chart_builder.needs_default_bookmark:
            return []
        return [{
            "name":        "_default_state",
            "displayName": "Default",
            "explorationState": {
                # An empty explorationState means "no filters, default
                # page". Power BI restores this state on click —
                # effectively clearing slicers back to defaults, which
                # is the semantic the Tableau 'Reset Filters' button
                # originally provided.
                "filters":  [],
                "sections": {},
            },
        }]

    # ------------------------------------------------------------------
    # Top level — pages
    # ------------------------------------------------------------------

    def build(self) -> List[Dict[str, Any]]:
        if self.dashboards:
            # Cache the canonical nav styles once before per-dashboard
            # rendering so every page's navigator inherits the same
            # font / color / fill (sourced from the first dashboard
            # that defines goto-sheet buttons).
            self._canonical_nav_styles = collect_canonical_nav_styles(
                self.dashboards,
            )
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
            # Use the canonical nav styles cached on the builder so the
            # navigator's font / colors / fill are identical on every
            # page (regardless of how this specific dashboard styles its
            # own buttons). The cache holds the first dashboard's button
            # styles; first entry maps to PBI's `selected` selector
            # (active-page button), second entry to `default` (inactive
            # buttons). Falling back to this dashboard's own buttons
            # keeps single-dashboard reports working unchanged.
            styles = (
                self._canonical_nav_styles
                or [z.get("buttonStyle") or {} for z in nav_zones]
            )
            zid   = nav_zones[0].get("id", "")
            visuals.append(
                build_page_navigator(
                    x1, y1, nav_w, nav_h, z_cur, zid, styles,
                )
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
        visual = self.chart_builder.build_chart_visual(ws, 0, 0,
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
            return self.chart_builder.build_chart_visual(ws, x, y, w, h, z)

        # Filter widget — emit a slicer. We try to resolve the filter
        # field so the slicer actually drives the report.
        if ztype == "filter":
            flabel = self.resolver.filter_label(zone)
            ffield = self.resolver.filter_field(zone)
            # Title styling carries over only when the parser surfaces it.
            # Use `titleStyle` for the new path; fall back to legacy
            # `filterStyle` so older parsers keep working. None means "no
            # styling override" — _build_placeholder_slicer treats that as
            # 'use defaults'.
            title_style   = zone.get("titleStyle") or zone.get("filterStyle")
            title_enabled = zone.get("titleEnabled", True)
            return build_placeholder_slicer(
                flabel, x, y, w, h, z, zid,
                field=ffield,
                ds_name=self.resolver.ds_name_for_zone(zone),
                title_style=title_style,
                title_enabled=title_enabled,
                widget_mode=zone.get("mode", ""),
                project_field=self.resolver.slicer_project_field,
            )

        # Parameter control -> placeholder slicer bound to the parameter
        # table. List parameters get bound to the Label column so the
        # slicer shows the human-readable alias (Tableau's `<member
        # alias='Option A' value='1'>`), not the raw Value.
        if ztype in ("parameter", "paramctrl"):
            param_binding = self._resolve_parameter_binding(zone.get("param", ""))
            return build_placeholder_slicer(
                zname or zone.get("param", "Parameter"),
                x, y, w, h, z, zid,
                title_style=zone.get("titleStyle"),
                title_enabled=zone.get("titleEnabled", True),
                widget_mode=zone.get("mode", ""),
                param_binding=param_binding,
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
            return build_textbox(zname or "Legend", x, y, w, h, z, zid,
                                       style=container_style)

        # Text and title zones -> textbox carrying the actual extracted
        # text and any font/container styling the parser pulled from the
        # zone's <run> + <zone-style> blocks.
        if ztype in ("text", "title"):
            text = zone.get("text") or zname or " "
            return build_textbox(text, x, y, w, h, z, zid,
                                       style=zone.get("textStyle"))

        # Image / bitmap zones -> textbox placeholder. We don't bundle the
        # image into the PBIP because Tableau stores it inside the .twbx
        # and we want the converter to keep working on plain .twb files.
        if ztype == "bitmap":
            return build_textbox("[Image]", x, y, w, h, z, zid,
                                       style=container_style)

        # dashboard-object: edit/refresh widgets and any non-navigation buttons.
        # Navigation buttons (goto-sheet) are already merged into a single
        # pageNavigator in _page_from_dashboard and never reach this path.
        if ztype == "dashboard-object":
            text = zone.get("caption") or zname or " "
            style = zone.get("buttonStyle") or {}
            return build_textbox(text, x, y, w, h, z, zid, style=style)

        # Anything else with no name and no recognized type just becomes
        # a quiet empty textbox — keeps the layout intact. We still pass
        # the container style so a Tableau zone that paints a colored
        # tile but has no recognised type doesn't lose its background.
        return build_textbox(zname or " ", x, y, w, h, z, zid,
                                   style=container_style)

    # ------------------------------------------------------------------
    # Parameter slicer binding
    # ------------------------------------------------------------------

    def _resolve_parameter_binding(
        self, param_ref: str,
    ) -> Optional[Tuple[str, str]]:
        """Resolve a Tableau parameter reference (``[Parameters].[Name]``)
        to the (table, column) that a slicer should bind to.

        List parameters carry a ``(Value, Label)`` two-column table; we
        bind to ``Label`` so the user-visible alias surfaces in the
        slicer. Range / Any parameters have a single ``Value`` column —
        that's what gets bound.
        """
        if not param_ref:
            return None
        import re
        m = re.match(r"^\[[^\]]+\]\s*\.\s*\[([^\]]+)\]\s*$", param_ref)
        if not m:
            return None
        name = m.group(1).strip()
        if not name:
            return None
        loc_list = self.model.col_locator.get(("Parameters", name)) or []
        if not loc_list:
            return None
        tbl, _ = loc_list[0]
        param_tbl = next(
            (t for t in self.model.tables if t.get("name") == tbl),
            None,
        )
        if param_tbl is None:
            return None
        col_names = {c["name"] for c in param_tbl.get("columns") or []}
        if "Label" in col_names:
            return tbl, "Label"
        if "Value" in col_names:
            return tbl, "Value"
        return None

