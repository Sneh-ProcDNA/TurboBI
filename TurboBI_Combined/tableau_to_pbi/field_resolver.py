"""Worksheet-aware field resolver for the report builder.

Owns the resolution path from a Tableau field reference to a PBI
``(table, column)`` binding — and the projection-shape emission that
every chart helper relies on. Stateful: each chart visual sets the
per-visual scope (``ws_columns`` / ``prefer_table_for_ws``) so suffix-
aware lookups pick the right logical table; the slicer path clears
that scope before binding its own filter field.

ReportBuilder holds one ``FieldResolver`` instance and delegates. The
chart family extraction (Phase 2 next step) will take a ``FieldResolver``
as a collaborator so chart helpers can call the resolver directly
without going through ReportBuilder.

Owned state:
  ``datasources`` / ``model`` / ``_ws_map`` / ``_hints`` — read-only,
  set once in ``__init__``.
  ``ws_columns`` / ``prefer_table_for_ws`` — per-visual scope, mutated
  by ReportBuilder while building each chart visual.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ._logging import get_logger
from .config import AGG_TABLE
from .model import SemanticModel
from .utils import clean_bracket

_log_ds    = get_logger("DS")
_log_hint  = get_logger("HINT")
_log_blend = get_logger("BLEND")


# Matches a Tableau (Object!Suffix) disambiguator embedded in a field
# name, e.g. 'Region (Dim!HCO)'. Used to extract a strong table hint
# when computing a worksheet's primary table.
_OBJ_SUFFIX_RE = re.compile(r"\(\s*[^) !]+!\s*([^)]+)\s*\)")


class FieldResolver:
    def __init__(
        self,
        datasources: List[Dict[str, Any]],
        worksheets:  List[Dict[str, Any]],
        model:       SemanticModel,
        hints:       Optional[Dict[str, Dict[str, List[str]]]] = None,
    ):
        self.datasources = datasources
        self.model       = model
        self._ws_map     = {ws["name"]: ws for ws in worksheets}
        # Resolution hints supplied externally (e.g. by the agent
        # codebase). Shape: {ds_name: {field_name: [table, column]}}.
        # Consulted as a last-resort lookup after the model's own
        # resolve_field returns None — never overrides a successful
        # native resolution. Default {} keeps converter behavior intact.
        self._hints = hints or {}
        # Per-visual mutable scope. ReportBuilder writes these directly
        # while building each chart visual; the slicer path clears them
        # so a previously-built chart visual doesn't steer the slicer
        # field to the wrong table.
        self.ws_columns: Dict[str, str] = {}
        self.prefer_table_for_ws: Optional[str] = None

    # ------------------------------------------------------------------
    # Pure parsing helpers — used to extract the filter field/label from
    # a dashboard filter zone's [ds].[role:field:key] reference.
    # ------------------------------------------------------------------

    @staticmethod
    def filter_label(zone: Dict[str, Any]) -> str:
        # Filter zones carry their target field in the param attribute,
        # shaped like '[ds].[role:field:key]'. Pull the field segment so
        # the slicer title says "Region" instead of "Actual Cost vs Budget".
        param = zone.get("param", "")
        if not param:
            return zone.get("name", "Filter")
        # Match the inner spec inside the second pair of brackets.
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
    def filter_field(zone: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return a field dict {field, agg} for the filter zone's param,
        or None if it cannot be parsed."""
        param = zone.get("param", "")
        if not param:
            return None
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

    # ------------------------------------------------------------------
    # Datasource name resolution
    # ------------------------------------------------------------------

    def ds_name(self, ds_ref: str) -> str:
        for ds in self.datasources:
            if ds["name"] == ds_ref or ds["caption"] == ds_ref:
                return ds["name"]
        # No match — log so the user can spot misrouted worksheets, then
        # fall back to the first datasource. Falling back keeps the visual
        # alive (instead of dropping it) but resolution may pick the wrong
        # table. The log line makes the silent guess auditable.
        if ds_ref:
            _log_ds.warning(f"worksheet datasourceRef '{ds_ref}' did not match any "
                  f"parsed datasource; falling back to first.")
        return self.datasources[0]["name"] if self.datasources else ""

    def ds_name_for_zone(self, zone: Dict[str, Any]) -> str:
        """Return the best-guess datasource name for a dashboard zone.

        Filter zones reference a field like '[ds].[role:field]'. We try
        to extract the datasource name from that reference."""
        param = zone.get("param", "")
        if param:
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
            return self.ds_name(ws.get("datasourceRef", ""))
        return self.datasources[0]["name"] if self.datasources else ""

    # ------------------------------------------------------------------
    # Field resolution
    # ------------------------------------------------------------------

    def hint_lookup(
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
                    _log_hint.info(f"'{field_name}' (ds={ds_name}) -> "
                          f"{tbl}.{col}")
                    return (tbl, col)
            for m in t.get("measures", []) or []:
                if m.get("name") == col:
                    _log_hint.info(f"'{field_name}' (ds={ds_name}) -> "
                          f"{tbl}.{col} (measure)")
                    return (tbl, col)
            return None
        return None

    def resolve_visual_field(
        self,
        ds_name: str,
        field_name: str,
        *,
        prefer_table: Optional[str] = None,
        binding_ds: str = "",
        log_context: str = "RESOLVE",
    ) -> Optional[Tuple[str, str]]:
        """Resolve a visual/filter/sort field using one consistent strategy."""
        if not field_name:
            return None

        fname = field_name.strip()
        if not fname or "(generated)" in fname.lower():
            return None

        # Per-binding datasource override for blended fields.
        if binding_ds and binding_ds != ds_name and binding_ds.lower() != "parameters":
            loc = self.model.resolve_field(binding_ds, fname, prefer_table=None)
            if loc:
                tbl, _ = loc
                _log_blend.info(
                    f"binding routed: '{fname}' -> "
                    f"ds={binding_ds} table={tbl} (primary ds={ds_name})"
                )
                return loc

        ws_columns = self.ws_columns or {}
        candidates: List[str] = []

        canonical = SemanticModel._strip_obj_suffix(fname) or fname
        raw = ws_columns.get(canonical) or ws_columns.get(fname)

        if raw:
            candidates.append(raw)

        candidates.extend([
            fname,
            canonical,
        ])

        # Dedupe while preserving order.
        deduped: List[str] = []
        seen: set = set()
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                deduped.append(c)

        for candidate in deduped:
            loc = self.model.resolve_field(
                ds_name,
                candidate,
                prefer_table=prefer_table,
            )
            if loc:
                return loc

        loc = self.hint_lookup(ds_name, fname)
        if loc:
            return loc

        get_logger(log_context).info(
            f"'{fname}' (ds={ds_name}) not found — field dropped."
        )
        return None

    # ------------------------------------------------------------------
    # Worksheet primary-table voting
    # ------------------------------------------------------------------

    def primary_table_for_ws(
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
    # Projection emission
    # ------------------------------------------------------------------

    def add_proj(
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
        # didn't pass one explicitly. _build_projections stashes it on
        # the resolver for the duration of building one visual.
        if prefer_table is None:
            prefer_table = self.prefer_table_for_ws

        binding_ds = (field.get("datasource") or "").strip()

        loc = self.resolve_visual_field(
            ds_name,
            fname,
            prefer_table=prefer_table,
            binding_ds=binding_ds,
            log_context="RESOLVE",
        )

        if not loc:
            return

        tbl, col = loc
        agg_key  = field.get("agg", "").lower()

        # Redirect date-part aggregations to the precomputed hierarchy
        # column the semantic model creates for every dateTime column.
        # Tableau's `yr:date_added:ok` resolves to ('netflix_titles.csv',
        # 'Date Added'), but the visual really wants the YEAR of that
        # date — bind to the synthesized 'Year of Date Added' column,
        # which carries the year as a precomputed integer. PBI doesn't
        # have a date-part aggregation function, so without redirection
        # we'd silently drop the agg and the card would show the raw
        # date value.
        # The generated columns are in model.tables before visual binding,
        # matching Tableau's model-first order of operations.
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
        # Dedupe within the slot. Two entries are duplicates when they
        # refer to the same (table, column, aggregation) tuple — i.e.
        # when their queryRef strings collide. PBI silently emits both
        # if we don't dedup, which clutters tooltips (Tableau workbooks
        # often surface the same field via regular tooltip encoding,
        # the customized-tooltip template, AND a detail encoding) and
        # breaks visuals that reject duplicate bindings (maps in
        # particular silently fail to render when the same field
        # appears twice on Tooltips).
        existing = projections[slot]["projections"]
        new_key = entry.get("queryRef") or entry.get("nativeQueryRef")
        if new_key and any(
            (e.get("queryRef") or e.get("nativeQueryRef")) == new_key
            for e in existing
        ):
            return
        existing.append(entry)

    def slicer_project_field(
        self,
        projections: Dict[str, Any],
        slot:        str,
        field:       Dict[str, Any],
        ds_name:     str,
    ) -> None:
        """Callback handed to ``build_placeholder_slicer`` so the
        slicer module can wire a non-parameter field into the slicer's
        Values bucket without knowing about the worksheet-aware
        resolver.

        Clears worksheet-scoped state first — a previously-built chart
        visual may have left ``prefer_table_for_ws`` / ``ws_columns``
        populated, which would steer the slicer field to the wrong
        table. Filter slicers carry their own explicit (Object!Suffix)
        ref so suffix-based resolution is enough.
        """
        self.prefer_table_for_ws = None
        self.ws_columns = {}
        self.add_proj(projections, slot, field, ds_name)
