"""Chart visual constellation.

Owns the chart-family branch of the visual dispatcher: builder methods
for chart visuals, the projection block (slot/encoding mapping), visual-
level filters (member-list, Top N, auto-stamped), sort definitions,
per-category color blocks, and the small bag of column-type helpers
they all rely on.

ChartBuilder is constructed once per ReportBuilder run and given
``(datasources, model, resolver)`` as collaborators. The resolver owns
the (table, column) lookup path and projection-shape emission; the
chart helpers call ``self.resolver.add_proj(...)`` / ``self.resolver.
resolve_visual_field(...)`` rather than re-implementing field
resolution. Internal helpers keep their underscore-prefixed names for
clarity; ``build_chart_visual`` is the public entry the dispatcher
calls.

Bookmark side-effect: when the chart picker routes a worksheet to an
``actionButton``, ChartBuilder flips ``self.needs_default_bookmark =
True`` so the writer knows to emit the ``_default_state`` bookmark file.
ReportBuilder reads this flag through ``self.chart_builder.
needs_default_bookmark`` after building.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .._logging import get_logger
from ..config import AGG_TABLE, SCHEMA, VISUAL_SLOTS
from ..field_resolver import FieldResolver
from ..model import SemanticModel
from ..utils import hex_id
from ..visual_picker import pick_visual_type
from .action_button import build_action_button

# Per-tag loggers — see tableau_to_pbi._logging.
_log_filter   = get_logger("FILTER")
_log_resolve  = get_logger("RESOLVE")
_log_sort     = get_logger("SORT")
_log_topn     = get_logger("TOPN")
_log_validate = get_logger("VALIDATE")
_log_vpick    = get_logger("VPICK")
_log_group    = get_logger("GROUP")
from .helpers import (
    color_expr as _color_expr,
    contrast_text_color as _contrast_text_color,
    expr_lit as _expr_lit,
    normalize_font_size as _normalize_font_size,
    PBI_SAFE_FONTS as _PBI_SAFE_FONTS,
    safe_font_family as _safe_font_family,
)
from .slicer import title_object


# Aggregations that map to a measure in PBI's projection field block
MEASURE_AGGS = {agg for agg, info in AGG_TABLE.items() if info["is_measure"]}


class ChartBuilder:
    def __init__(
        self,
        datasources: List[Dict[str, Any]],
        model:       SemanticModel,
        resolver:    FieldResolver,
    ):
        self.datasources = datasources
        self.model       = model
        self.resolver    = resolver
        # Flipped on when ``_build_chart_visual`` emits an actionButton.
        # ReportBuilder reads this after building to decide whether the
        # writer should emit the ``_default_state`` bookmark file.
        self.needs_default_bookmark = False

    # Public entry — the chart dispatcher in ReportBuilder calls this.
    def build_chart_visual(
        self, ws: Dict[str, Any],
        x: int, y: int, w: int, h: int, z: int,
    ) -> Dict[str, Any]:
        return self._build_chart_visual(ws, x, y, w, h, z)

    def _expand_group_filter_members(
        self,
        ds_name: str,
        field_name: str,
        members: List[Any],
    ) -> Tuple[str, List[Any]]:
        """If field_name is a Tableau group, expand group labels to base members.

        Returns:
            (field_to_resolve, expanded_members)

        Example:
            Region Group = ["East + West"]
        becomes:
            Region = ["East", "West"]
        """
        group_info = None

        if hasattr(self.model, "group_info"):
            group_info = self.model.group_info(ds_name, field_name)

        if not group_info:
            return field_name, members

        base_field = group_info.get("baseField") or field_name
        members_by_group = group_info.get("membersByGroup") or {}

        if not members:
            return base_field, members

        expanded: List[Any] = []

        for m in members:
            key = str(m).strip().strip('"')

            group_members = members_by_group.get(key)
            if group_members:
                for gm in group_members:
                    if gm not in expanded:
                        expanded.append(gm)
            else:
                # If the member is not a group label, keep it as-is.
                if m not in expanded:
                    expanded.append(m)

        _log_group.info(
            f"Expanded filter field '{field_name}' -> '{base_field}' "
            f"members={expanded}"
        )

        return base_field, expanded

    def _build_chart_visual(
        self, ws: Dict[str, Any],
        x: int, y: int, w: int, h: int, z: int,
    ) -> Dict[str, Any]:
        visual_type = self._pick_visual_type(ws)
        ds_name     = self.resolver.ds_name(ws.get("datasourceRef", ""))

        # One-line diagnostic so users can confirm which Tableau mark and
        # shelf shape produced each visual type. Helps debug "I expected a
        # card but got a textbox" claims by making the picker's decision
        # visible without firing up a debugger.
        mark = ws.get("markClass", "")
        rows = len(ws.get("rowFields") or [])
        cols = len(ws.get("colFields") or [])
        lbl  = (ws.get("labelField") or {}).get("field", "")
        _log_vpick.info(f"{ws.get('name','?'):40s} mark={mark:10s} "
              f"rows={rows} cols={cols} label='{lbl}' -> {visual_type}")

        # Button worksheets: no shelves, no encodings — render as a
        # Power BI actionButton bound to the default-state bookmark
        # (emitted once per report). Tableau dashboards use plain
        # worksheets with a single string calc field as 'Reset Filters' /
        # 'Apply' buttons; the picker routes them to `actionButton` and
        # this branch emits the right visual shape.
        if visual_type == "actionButton":
            visual, needs_bookmark = build_action_button(ws, x, y, w, h, z)
            if needs_bookmark:
                self.needs_default_bookmark = True
            return visual

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
        # visual build. The resolver consults ws_columns when binding a
        # column referenced as plain "HCP_ID" so it still picks the right
        # logical table.
        self.resolver.ws_columns = ws.get("wsColumns") or {}

        prefer_table = self.resolver.primary_table_for_ws(ws, ds_name)
        projections  = self._build_projections(ws, visual_type, ds_name,
                                               prefer_table)

        # Slot validators close the gap between Tableau's encoding-shelf
        # semantics and PBI's slot expectations — e.g. lifting a country
        # field off Details onto a filledMap's Location slot. Notes are
        # printed to stdout so users see exactly what was reshaped (the
        # same trace style as [HINT] / [DROP]).
        from ..validators import validate_slots
        for note in validate_slots(visual_type, ws, projections, ds_name,
                                    self.resolver.add_proj):
            _log_validate.info(f"{ws.get('name','?')}: {note}")

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
        if visual_type in ("map", "filledMap", "shapeMap", "azureMap",
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
        # The KPI / card / multiRowCard pattern in PBI relies on the
        # value itself acting as the headline, with the card's title
        # disabled by default. Tableau's worksheet title on a text /
        # KPI card duplicates the value (and steals vertical space),
        # so force the title off for card-class visuals regardless of
        # what the twb said. Users can re-enable in the Format pane.
        if visual_type in ("card", "cardVisual", "multiRowCard"):
            title_style.setdefault("fontSize", 14)
            title_enabled = False
        container = title_object(title_text, title_style,
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

        # Table / matrix header styling. PBI tableEx and pivotTable read
        # column-header font/background/alignment from the `columnHeaders`
        # bag, and pivotTable also reads row-header settings from
        # `rowHeaders`. Strategy:
        #
        #   1. Start with Tableau-like header defaults (bold black on
        #      white-ish background, centered, Arial 10pt) so PBI's
        #      lighter-grey defaults don't make the converted table
        #      look unfamiliar to a Tableau author. Without this every
        #      converted table renders with PBI's default header look
        #      regardless of whether the TWB had any header styling at
        #      all — which is what the user reported as "not set
        #      according to TWB".
        #   2. Layer the worksheet's titleStyle / labelStyle on top,
        #      since those are likely-but-not-certain header hints
        #      (Tableau pulls headers from the same family/size as the
        #      worksheet title in most workbooks).
        #   3. Layer the explicit column-header / row-header style-rule
        #      attrs the parser extracted from
        #      ``<style-rule element='column-header'|'row-header'|'header'>``
        #      (the parser's ``columnHeaderStyle`` / ``rowHeaderStyle``
        #      bags). These are the most specific, so they win over
        #      titleStyle / defaults.
        #   4. Layer ``backgroundColor`` underneath the header's own
        #      ``backgroundColor`` so a worksheet-wide bg fills in when
        #      the header rule didn't set one explicitly.
        #
        # PBI columnHeaders/rowHeaders property names:
        #   fontFamily, fontSize, fontColor, bold, italic, underline,
        #   backColor, alignment (Left/Center/Right), wordWrap
        if visual_type in ("tableEx", "pivotTable"):
            col_hdr  = ws.get("columnHeaderStyle") or {}
            row_hdr  = ws.get("rowHeaderStyle") or {}
            title_st = ws.get("titleStyle") or {}
            label_st = ws.get("labelStyle") or {}
            bg_fallback = ws.get("backgroundColor") or ""

            def _coalesce(*keys_layered) -> Any:
                """Return the first non-empty value across the layered
                style dicts, in precedence order (highest-precedence
                first)."""
                for hs, key in keys_layered:
                    val = (hs or {}).get(key)
                    if val not in (None, ""):
                        return val
                return None

            def _hdr_props(hs: Dict[str, Any]) -> Dict[str, Any]:
                # Tableau-like defaults: bold black on a light-grey
                # background, Arial 10pt, centered. These mirror the
                # Tableau header look so the converted table reads as
                # familiar to a Tableau author. Every value here can
                # be overridden by an explicit TWB rule.
                family = _coalesce(
                    (hs,       "fontFamily"),
                    (title_st, "fontFamily"),
                    (label_st, "fontFamily"),
                ) or "Arial"
                size = _coalesce(
                    (hs,       "fontSize"),
                    (title_st, "fontSize"),
                    (label_st, "fontSize"),
                ) or 10
                # Tableau frequently field-scopes the explicit header
                # fontColor (under `<style-rule element='label'>` with a
                # `field='[...].[col:nk]'` attr), which the parser drops
                # because field-scoped attrs are per-column overrides
                # rather than the general rule. The user-perceived
                # outcome is dark text on whatever backgroundColor the
                # unscoped header rule did set — so a dark backgroundColor
                # (e.g. #0e3567) renders dark text on dark blue and the
                # column-header text is unreadable.
                #
                # Fallback: when no explicit fontColor was supplied,
                # derive a contrast-appropriate color from the resolved
                # backgroundColor — white on dark, dark grey on light.
                explicit_color = _coalesce(
                    (hs,       "fontColor"),
                    (title_st, "fontColor"),
                )
                bg_for_contrast = hs.get("backgroundColor") or bg_fallback
                color = explicit_color or _contrast_text_color(bg_for_contrast)
                weight_raw = _coalesce(
                    (hs,       "fontWeight"),
                    (title_st, "fontWeight"),
                )
                bold = (
                    str(weight_raw or "").lower() == "bold"
                    or hs.get("bold")
                    or title_st.get("bold")
                )
                # Tableau column headers are bold by default — emit
                # bold unless the TWB explicitly de-bolded it.
                if bold is None or bold is False and not weight_raw:
                    bold = True
                italic = bool(hs.get("italic") or title_st.get("italic"))
                underline = bool(hs.get("underline") or title_st.get("underline"))
                bg = hs.get("backgroundColor") or bg_fallback
                align_raw = (hs.get("textAlign") or "").lower()
                if align_raw not in ("left", "center", "right"):
                    align_raw = "center"

                family_safe = _safe_font_family(family) or "Arial"
                size_lit    = _normalize_font_size(size) or "10D"

                props: Dict[str, Any] = {
                    "fontFamily": _expr_lit(f"'{family_safe}'"),
                    "fontSize":   _expr_lit(size_lit),
                    "fontColor":  _color_expr(color),
                    "bold":       _expr_lit("true" if bold else "false"),
                    "italic":     _expr_lit("true" if italic else "false"),
                    "underline":  _expr_lit("true" if underline else "false"),
                    "alignment":  _expr_lit(f"'{align_raw.capitalize()}'"),
                }
                if bg:
                    props["backColor"] = _color_expr(bg)
                return props

            objects["columnHeaders"] = [{"properties": _hdr_props(col_hdr)}]
            if visual_type == "pivotTable":
                # Row headers default to the same look as column headers
                # when the worksheet only declared one set of rules — a
                # Tableau crosstab styles both axes identically by
                # default.
                row_source = row_hdr or col_hdr
                objects["rowHeaders"] = [{"properties": _hdr_props(row_source)}]

        # Map default viewport — anchor on North America so the first
        # open shows the relevant region rather than the worldwide
        # auto-zoom. Applies to EVERY map visualType the converter
        # emits (azureMap is the new default; map / filledMap /
        # shapeMap kept for legacy callers).
        #
        # Center / zoom: lat 39.5°, long -104.99°, zoom 5 — roughly the
        # central US (longitudinally aligned with Denver). Zoom 5 fits
        # the contiguous United States at typical PBI aspect ratios
        # while still leaving Canadian and Mexican borders visible.
        #
        # The Azure Maps visual ships with its property bag names
        # documented in `capabilities.json` at
        # https://github.com/microsoft/Azure-Maps-Power-BI-Visual.
        # The `mapSettings` bag carries the "Initial map view" pane;
        # its enum property `view` accepts ``Auto`` / ``World`` /
        # ``UnitedStates`` / ``Custom``. The ``Custom`` view reads
        # ``customZoom`` / ``customCenterLat`` / ``customCenterLon`` —
        # we emit BOTH the preset AND the custom coords so PBI can
        # honour whichever it prefers. The ``controls.autoZoom`` flag
        # blocks the visual's default "auto-zoom to data" behaviour
        # which would otherwise override the initial view.
        #
        # Legacy `map` / `filledMap` / `shapeMap` visuals auto-fit to
        # the data extent and have NO supported viewport config in
        # `objects`; the bag is still emitted so a manual swap from
        # azureMap → map preserves intent, but the property is
        # effectively a no-op there.
        if visual_type in ("azureMap", "map", "filledMap", "shapeMap"):
            objects["mapSettings"] = [{
                "properties": {
                    "view":            _expr_lit("'UnitedStates'"),
                    "customZoom":      _expr_lit("5D"),
                    "customCenterLat": _expr_lit("39.5D"),
                    "customCenterLon": _expr_lit("-104.99D"),
                    # Some Desktop builds key off these alternate
                    # property names — emitting them is cheap and
                    # PBI silently ignores unknown bag properties.
                    "autoZoom":        _expr_lit("false"),
                    "zoom":            _expr_lit("5D"),
                    "centerLat":       _expr_lit("39.5D"),
                    "centerLong":      _expr_lit("-104.99D"),
                    "predefinedView":  _expr_lit("'UnitedStates'"),
                },
            }]
            objects["controls"] = [{
                "properties": {
                    "autoZoom": _expr_lit("false"),
                },
            }]

        if objects:
            v["visual"]["objects"] = objects
        return v

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
        prefer_table = self.resolver.prefer_table_for_ws
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

            # loc = self.model.resolve_field(ds_name, target_field,
            #                                prefer_table=prefer_table)
            # if not loc:
            #     loc = self.resolver.hint_lookup(ds_name, target_field)
            # if not loc:
            #     _log_sort.info(f"'{target_field}' (ds={ds_name}) not found — "
            #           f"sort directive dropped.")
            #     continue
            # tbl, col = loc
            
            loc = self.resolver.resolve_visual_field(
                ds_name,
                target_field,
                prefer_table=prefer_table,
                log_context="SORT",
            )

            if not loc:
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
        prefer_table = self.resolver.prefer_table_for_ws
        loc = self.model.resolve_field(ds_name, meas, prefer_table=prefer_table)
        if not loc:
            loc = self.resolver.hint_lookup(ds_name, meas)
        if not loc:
            _log_topn.info(f"ranking measure '{meas}' (ds={ds_name}) not "
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
        prefer_table = self.resolver.prefer_table_for_ws
        ws_columns   = self.resolver.ws_columns
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

            members = flt.get("members") or []

            fname, members = self._expand_group_filter_members(
                ds_name,
                fname,
                members,
            )
            # hint = fname
            # canonical = SemanticModel._strip_obj_suffix(fname) or fname
            # raw = ws_columns.get(canonical) or ws_columns.get(fname)
            # if raw and "(" in raw and ")" in raw and "(" not in fname:
            #     hint = raw
            # loc = self.model.resolve_field(ds_name, hint,
            #                                prefer_table=prefer_table)
            # if not loc:
            #     loc = self.resolver.hint_lookup(ds_name, fname)
            # if not loc:
            #     _log_filter.info(f"'{fname}' (ds={ds_name}) not found — "
            #           f"visual-level filter dropped.")
            #     continue
            # tbl, col = loc

            binding_ds = (flt.get("datasource") or "").strip()

            loc = self.resolver.resolve_visual_field(
                ds_name,
                fname,
                prefer_table=prefer_table,
                binding_ds=binding_ds,
                log_context="FILTER",
            )

            if not loc:
                continue

            tbl, col = loc

            is_measure = self.model.is_measure_ref(tbl, col)

            if is_measure:
                field_ref = {
                    "Measure": {
                        "Expression": {"SourceRef": {"Entity": tbl}},
                        "Property":   col,
                    },
                }
            else:
                field_ref = {
                    "Column": {
                        "Expression": {"SourceRef": {"Entity": tbl}},
                        "Property":   col,
                    },
                }

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
                "field": field_ref,
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

            # members = flt.get("members") or []
            if is_measure and members:
                _log_filter.info(
                    f"'{fname}' resolved to measure "
                    f"'{tbl}.{col}'. Member-list filters are not emitted "
                    f"for measures; keeping field binding only."
                )
                out.append(entry)
                continue
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
        prefer_table = self.resolver.prefer_table_for_ws
        loc = self.model.resolve_field(ds_name, meas,
                                       prefer_table=prefer_table)
        if not loc:
            loc = self.resolver.hint_lookup(ds_name, meas)
        if not loc:
            _log_topn.info(f"ranking measure '{meas}' (ds={ds_name}) not "
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

    def _build_projections(
        self, ws: Dict[str, Any], visual_type: str, ds_name: str,
        prefer_table: Optional[str] = None,
    ) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        # Stash the worksheet's primary table so every add_proj call below
        # can see it without changing each call's signature.
        self.resolver.prefer_table_for_ws = prefer_table
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

        # Tableau aggs that always indicate measure-context placement
        # (continuous axis / aggregated value) — extends the AGG_TABLE
        # measure list with user-calc and table-calc prefixes that the
        # AGG_TABLE doesn't currently enumerate.
        _MEASURE_CONTEXT_AGGS = MEASURE_AGGS | {
            "usr",          # user calc field — resolves to a DAX measure
            "pcto", "pct",  # percent-of-total / percent
            "cum",          # cumulative
            "rank",         # ranking
            "running_sum", "running_avg", "running_count",
            "running_min", "running_max",
            "window_sum", "window_avg", "window_count",
            "window_min", "window_max", "window_stdev", "window_var",
        }

        def is_measure(f: Dict[str, Any]) -> bool:
            """A field belongs in a measure slot (Y / Values) when:
              * its Tableau agg implies aggregation, OR
              * the resolved model binding is a DAX measure / a column
                whose `role='measure'`. Resolution is required because
                Tableau's `usr:` agg can wrap either a measure or a
                dimension calc field; same for an unaggregated reference
                to a `role='measure'` column."""
            agg = (f.get("agg") or "").lower()
            if agg in _MEASURE_CONTEXT_AGGS:
                return True
            fname = (f.get("field") or "").strip()
            if not fname:
                return False
            binding_ds = (f.get("datasource") or "").strip() or ds_name
            loc = self.model.resolve_field(
                binding_ds, fname, prefer_table=prefer_table,
            )
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
        if visual_type in ("map", "filledMap", "shapeMap", "azureMap"):
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
                    self.resolver.add_proj(out, slots.get("lat") or "Latitude",  plain, ds_name)
                elif is_lon and fname not in seen_lon:
                    seen_lon.add(fname)
                    plain = dict(f); plain["agg"] = ""
                    self.resolver.add_proj(out, slots.get("lon") or "Longitude", plain, ds_name)
            # Location-based maps
            if visual_type in ("filledMap", "shapeMap"):
                for f in category_fields:
                    self.resolver.add_proj(out, slots.get("location") or "Location", f, ds_name)
            if size_enc["field"]  and slots.get("size"):
                self.resolver.add_proj(out, slots["size"],  size_enc,  ds_name)
            if color_enc["field"] and slots.get("color"):
                self.resolver.add_proj(out, slots["color"], color_enc, ds_name)
            for f in detail_fields:
                if slots.get("details"):
                    self.resolver.add_proj(out, slots["details"], f, ds_name)
            for f in tooltip_fields:
                self.resolver.add_proj(out, "Tooltips", f, ds_name)
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
                self.resolver.add_proj(out, target, picked, ds_name)
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
                self.resolver.add_proj(out, cat_slot, f, ds_name)
            # Value preference: measure on rows/cols, then size, then label.
            vals = list(value_fields)
            if not vals and size_enc["field"]:
                vals.append(size_enc)
            if not vals and label_enc["field"] and is_measure(label_enc):
                vals.append(label_enc)
            for f in vals:
                self.resolver.add_proj(out, value_slot, f, ds_name)
            if color_enc["field"] and slots.get("color"):
                self.resolver.add_proj(out, slots["color"], color_enc, ds_name)
            for f in detail_fields:
                if slots.get("details"):
                    self.resolver.add_proj(out, slots["details"], f, ds_name)
            for f in tooltip_fields:
                self.resolver.add_proj(out, "Tooltips", f, ds_name)
            return out

        # ── Treemap: detail-style dim + size measure. ──────────────────
        if visual_type == "treemap":
            cat_slot   = slots.get("category") or "Category"
            value_slot = slots.get("value")    or "Values"
            cats = list(category_fields)
            if not cats and color_enc["field"]:
                cats.append(color_enc)
            for f in cats:
                self.resolver.add_proj(out, cat_slot, f, ds_name)
            vals = list(value_fields)
            if not vals and size_enc["field"]:
                vals.append(size_enc)
            for f in vals:
                self.resolver.add_proj(out, value_slot, f, ds_name)
            if color_enc["field"] and slots.get("color"):
                self.resolver.add_proj(out, slots["color"], color_enc, ds_name)
            for f in detail_fields:
                if slots.get("details"):
                    self.resolver.add_proj(out, slots["details"], f, ds_name)
            for f in tooltip_fields:
                self.resolver.add_proj(out, "Tooltips", f, ds_name)
            return out

        # ── Scatter / Bubble: cols = X, rows = Y, plus Color/Size/Details ─
        if visual_type in ("scatterChart", "bubbleChart"):
            if slots.get("x"):
                for f in col_fields:
                    if not f.get("isGeo", False):
                        self.resolver.add_proj(out, slots["x"], f, ds_name)
            if slots.get("y"):
                for f in row_fields:
                    if not f.get("isGeo", False):
                        self.resolver.add_proj(out, slots["y"], f, ds_name)
            if color_enc["field"] and slots.get("color"):
                self.resolver.add_proj(out, slots["color"], color_enc, ds_name)
            if size_enc["field"] and slots.get("size"):
                self.resolver.add_proj(out, slots["size"], size_enc, ds_name)
            for f in detail_fields:
                if slots.get("details"):
                    self.resolver.add_proj(out, slots["details"], f, ds_name)
            for f in tooltip_fields:
                self.resolver.add_proj(out, "Tooltips", f, ds_name)
            return out

        # ── Pivot table: Rows / Columns / Values. ──────────────────────
        if visual_type == "pivotTable":
            out.setdefault("Rows",    {"projections": []})
            out.setdefault("Columns", {"projections": []})
            out.setdefault("Values",  {"projections": []})
            for f in row_fields:
                self.resolver.add_proj(out, "Rows",    f, ds_name)
            for f in col_fields:
                self.resolver.add_proj(out, "Columns", f, ds_name)
            # If a measure exists on the size or label encoding (e.g. a
            # heatmap-style 'Square' worksheet), drop it into Values.
            if size_enc["field"] and is_measure(size_enc):
                self.resolver.add_proj(out, "Values", size_enc, ds_name)
            if label_enc["field"] and is_measure(label_enc):
                self.resolver.add_proj(out, "Values", label_enc, ds_name)
            if color_enc["field"]:
                self.resolver.add_proj(out, "Values", color_enc, ds_name)
            for f in detail_fields:
                if slots.get("details"):
                    self.resolver.add_proj(out, slots["details"], f, ds_name)
            for f in tooltip_fields:
                self.resolver.add_proj(out, "Tooltips", f, ds_name)
            return out

        # ── Combo charts (dual axis) ────────────────────────────────────
        if visual_type in ("lineClusteredColumnComboChart",
                           "lineStackedColumnComboChart"):
            cat_slot = slots.get("category")
            if cat_slot:
                for f in category_fields:
                    self.resolver.add_proj(out, cat_slot, f, ds_name)
            # Heuristic: if mark is explicitly 'dual', split measures evenly.
            # Otherwise first measure -> Y (columns), second -> Y2 (lines).
            mark = (ws.get("markClass") or "").lower()
            is_explicit_dual = mark == "dual"
            if value_fields:
                self.resolver.add_proj(out, slots.get("value_y") or "Y", value_fields[0], ds_name)
            if len(value_fields) > 1:
                self.resolver.add_proj(out, slots.get("value_y2") or "Y2", value_fields[1], ds_name)
            if len(value_fields) > 2:
                # 3rd+ measures: push into Series/Color if available
                series_slot = slots.get("color") or "Series"
                for vf in value_fields[2:]:
                    self.resolver.add_proj(out, series_slot, vf, ds_name)
            # Fallback encodings
            if not value_fields:
                if size_enc["field"] and is_measure(size_enc):
                    self.resolver.add_proj(out, slots.get("value_y") or "Y", size_enc, ds_name)
                elif label_enc["field"] and is_measure(label_enc):
                    self.resolver.add_proj(out, slots.get("value_y") or "Y", label_enc, ds_name)
            if color_enc["field"] and slots.get("color"):
                self.resolver.add_proj(out, slots["color"], color_enc, ds_name)
            for f in detail_fields:
                if slots.get("details"):
                    self.resolver.add_proj(out, slots["details"], f, ds_name)
            for f in tooltip_fields:
                self.resolver.add_proj(out, "Tooltips", f, ds_name)
            return out

        # ── Table / tableEx: every field becomes a column in the table. ─
        if visual_type == "tableEx":
            for f in row_fields + col_fields:
                self.resolver.add_proj(out, "Values", f, ds_name)
            for f in detail_fields:
                self.resolver.add_proj(out, "Values", f, ds_name)
            for f in tooltip_fields:
                self.resolver.add_proj(out, "Values", f, ds_name)
            # Encodings that didn't land elsewhere also go to Values.
            if color_enc["field"]:
                self.resolver.add_proj(out, "Values", color_enc, ds_name)
            if size_enc["field"]:
                self.resolver.add_proj(out, "Values", size_enc, ds_name)
            if label_enc["field"]:
                self.resolver.add_proj(out, "Values", label_enc, ds_name)
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
                ws_columns = self.resolver.ws_columns
                hint = fname
                canon = SemanticModel._strip_obj_suffix(fname) or fname
                raw   = ws_columns.get(canon) or ws_columns.get(fname)
                if raw and "(" in raw and ")" in raw and "(" not in fname:
                    hint = raw
                loc = self.model.resolve_field(ds_name, hint,
                                               prefer_table=prefer_table)
                if not loc:
                    loc = self.resolver.hint_lookup(ds_name, fname)
                if not loc:
                    _log_resolve.info(f"'{fname}' (ds={ds_name}) not found "
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
                # The synthesized columns are created during model.build()
                # before report binding, but keep the redirect type explicit
                # so the agg-default picker has the right Min/CountNonNull
                # fork even if a workbook omits the helper column.
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
                    # Single-row detail card detour: if the worksheet's
                    # filters narrow to exactly one categorical value,
                    # the card is showing the detail of a SELECTED row
                    # (Tableau dashboards commonly use this for 'show
                    # me the description of the currently filtered
                    # title'). For a string column in that context,
                    # MIN returns the alphabetically-first value across
                    # the matching rows — useless. Synthesise a
                    # SELECTEDVALUE helper measure that returns the
                    # single value when filter context is one row,
                    # and BLANK() otherwise.
                    if (
                        tmdl_type == "string"
                        and not agg_key  # no Tableau-supplied agg
                        and self._visual_is_single_row(ws)
                    ):
                        measure_name = self._ensure_card_value_measure(tbl, col)
                        out.setdefault(slot, {"projections": []})
                        out[slot]["projections"].append({
                            "field": {
                                "Measure": {
                                    "Expression": {"SourceRef": {"Entity": tbl}},
                                    "Property":   measure_name,
                                },
                            },
                            "queryRef":       f"{tbl}.{measure_name}",
                            "nativeQueryRef": measure_name,
                            "active":         True,
                            "displayName":    col,
                        })
                        return
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
                ws_columns = self.resolver.ws_columns
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
                        self.resolver.add_proj(out, value_slot, f, ds_name)
                        value_taken = True
                    else:
                        self.resolver.add_proj(out, tooltip_slot, f, ds_name)
                else:
                    if not category_taken:
                        self.resolver.add_proj(out, category_slot, f, ds_name)
                        category_taken = True
                    else:
                        self.resolver.add_proj(out, tooltip_slot, f, ds_name)

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
                self.resolver.add_proj(out, cat_slot, f, ds_name)

        value_slot = (slots.get("value") or slots.get("value_y") or
                      slots.get("value_x") or slots.get("y"))
        y2_slot    = slots.get("value_y2") or slots.get("y2")

        if value_slot:
            for i, f in enumerate(value_fields):
                if i == 0:
                    self.resolver.add_proj(out, value_slot, f, ds_name)
                elif i == 1 and y2_slot:
                    self.resolver.add_proj(out, y2_slot, f, ds_name)
                else:
                    # Additional measures create clustered groups in PBI
                    # when placed in the same primary slot.
                    self.resolver.add_proj(out, value_slot, f, ds_name)
            # Fallback: a measure on the size/label encoding becomes the
            # value if rows/cols didn't supply one.
            if not value_fields:
                if size_enc["field"] and is_measure(size_enc):
                    self.resolver.add_proj(out, value_slot, size_enc, ds_name)
                elif label_enc["field"] and is_measure(label_enc):
                    self.resolver.add_proj(out, value_slot, label_enc, ds_name)

        # Color encoding -> Series/Color/Legend slot.
        if color_enc["field"] and slots.get("color"):
            self.resolver.add_proj(out, slots["color"], color_enc, ds_name)

        # Size encoding -> Size slot.
        if size_enc["field"] and slots.get("size"):
            self.resolver.add_proj(out, slots["size"], size_enc, ds_name)

        # Detail fields -> Details slot
        for f in detail_fields:
            if slots.get("details"):
                self.resolver.add_proj(out, slots["details"], f, ds_name)

        # Tooltip fields
        for f in tooltip_fields:
            if f.get("field"):
                self.resolver.add_proj(out, "Tooltips", f, ds_name)

        return out

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
        ws_columns = self.resolver.ws_columns
        prefer_table = self.resolver.prefer_table_for_ws
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

    @staticmethod
    def _visual_is_single_row(ws: Dict[str, Any]) -> bool:
        """Return True when the worksheet's filters narrow to exactly one
        underlying row.

        Tableau dashboards use this pattern for 'show me the details of
        the currently selected title' cards: drag a worksheet onto the
        dashboard, then filter it to one value via a parameter or a
        cross-visual action. The worksheet's saved filter config
        captures the selection at save time as a single-value
        categorical filter — that's what we detect.

        Detection rule: ANY categorical-style filter whose member list
        has exactly one entry. We don't require ALL filters to be
        single-value because the dashboard might also have a Type='Movie'
        guard on the same card. Any single-value filter is sufficient
        signal that the card is showing one row.
        """
        for flt in ws.get("filters") or []:
            kind = (flt.get("kind") or flt.get("type") or "").lower()
            if kind not in ("categorical", "list", "enum"):
                continue
            members = flt.get("members") or flt.get("values") or []
            if isinstance(members, list) and len(members) == 1:
                return True
        return False

    def _ensure_card_value_measure(self, table_name: str, col_name: str) -> str:
        """Create (or reuse) a SELECTEDVALUE helper measure for a card.

        SELECTEDVALUE returns the underlying value when filter context
        has narrowed to exactly one row, and BLANK() otherwise — the
        right semantic for 'show the description of the currently
        selected title' cards. By caching under a deterministic name
        we ensure two cards binding to the same column share one
        measure rather than each emitting its own copy (PBI rejects
        duplicate measure names anyway).

        Returns the measure name. The measure is hidden so it doesn't
        clutter the fields list — users see only the card, not the
        plumbing.
        """
        from ..utils import lineage_tag  # local import: writer-side helper

        # Sanitize the column name into a measure-safe form. Strip
        # bracket-unsafe characters that would break TMDL or DAX
        # identifier rules.
        safe_col = re.sub(r"[\[\]']", "", col_name)
        measure_name = f"_card_{safe_col}"
        for t in self.model.tables:
            if t.get("name") != table_name:
                continue
            for m in t.get("measures") or []:
                if m.get("name") == measure_name:
                    return measure_name
            t.setdefault("measures", []).append({
                "name":       measure_name,
                "expression": f"SELECTEDVALUE('{table_name}'[{col_name}])",
                "lineageTag": lineage_tag(
                    "card_helper", table_name, col_name,
                ),
                "format":     "",
                "hidden":     True,
            })
            break
        return measure_name

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

        is_table = visual_type in ("tableEx", "pivotTable")
        is_map   = visual_type in ("map", "filledMap", "shapeMap", "azureMap")
        if is_table:
            bag = "values"
        elif is_map:
            bag = "dataLabels"
        else:
            bag = "labels"
        # PBI's tableEx / pivotTable `values` bag uses fontColor wrapped
        # in a solid-color envelope (same shape as columnHeaders/backColor).
        # Chart `labels` and map `dataLabels` use the flat `color` literal.
        # Wrong key = silently dropped by PBI Desktop, which is what
        # produced "font color not applied" on the table visual.
        color_key = "fontColor" if is_table else "color"

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
                properties[color_key] = _color_expr(style["fontColor"])
            if str(style.get("fontWeight", "")).lower() == "bold":
                properties["bold"] = _expr_lit("true")
            if style.get("italic"):
                properties["italic"] = _expr_lit("true")
            if style.get("underline"):
                properties["underline"] = _expr_lit("true")

        # Tableau-like default for table cell text when the TWB didn't
        # author one — PBI's default `values` bag has no color set, which
        # makes the cells render in the theme's faint text color. Pick a
        # contrast-appropriate default against any background style
        # that the values bag may carry.
        if is_table and color_key not in properties and enabled is not False:
            bg_hint = (style or {}).get("backgroundColor")
            properties[color_key] = _color_expr(_contrast_text_color(bg_hint))

        if not properties:
            return {}
        return {bag: [{"properties": properties}]}

