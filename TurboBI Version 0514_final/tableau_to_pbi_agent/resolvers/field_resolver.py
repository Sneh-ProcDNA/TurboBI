"""Resolve [RESOLVE] / [FILTER] warnings via Claude.

Input:  one warning record + the workbook's parsed IR + model snapshot.
Output: a (table, column) tuple, or None if Claude couldn't pick one.

The resolver runs all warnings concurrently against a single cached
system prompt, so a workbook with 30 dropped fields makes ~30 small
parallel API calls that share the cached snapshot.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

from ..claude_client import ClaudeClient
from ..context_builder import (
    model_snapshot,
    render_snapshot_for_system,
    warning_context,
)


_SYSTEM_TEMPLATE = """You map a single Tableau field reference to its (table, column) home in a Power BI semantic model.

Reply with ONLY a JSON object — no prose, no code fences:
  {"table": "<exact table name>", "column": "<exact column name>"}
or, when you genuinely cannot decide:
  {"table": null, "column": null}

Rules:
- Both names must appear EXACTLY as listed in the snapshot below; do not invent, rename, or paraphrase.
- A field name like "Region (Dim!HCO)" carries an explicit table hint — the suffix "Dim!HCO" maps to a table whose name contains "HCO". Use that.
- A field name with no suffix is ambiguous; favor a table mentioned in the worksheet name or the siblings.
- Strip aggregation prefixes when matching: "Sum of Sales" -> match column "Sales".
- If the field is a calc-field name like "Calculation_..." prefer measures (entries marked "(measure)" in the column list).
- If multiple tables hold a same-named column and you have no signal to pick one, return nulls. A wrong hint is worse than a missing one.

Workbook model snapshot (trust this list as authoritative):
{snapshot}
"""


class FieldResolver:
    def __init__(self, client: ClaudeClient):
        self.client = client

    def build_system(self, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        text = _SYSTEM_TEMPLATE.format(
            snapshot=render_snapshot_for_system(snapshot),
        )
        return self.client.cached_system(text)

    async def resolve_one(
        self,
        warning: Dict[str, Any],
        worksheets: List[Dict[str, Any]],
        snapshot: Dict[str, Any],
        system_blocks: List[Dict[str, Any]],
        datasources: Optional[List[Dict[str, Any]]] = None,
        calc_index: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[Tuple[str, str]]:
        ctx = warning_context(warning, worksheets, snapshot,
                              datasources=datasources,
                              calc_index=calc_index)
        if ctx is None:
            return None
        # Restrict the user turn to the ds-specific column list — it's
        # already in the cached system prompt, but referencing the right
        # subset upfront helps Claude pick from the right pool when the
        # workbook has multiple datasources.
        ds_tables = (snapshot.get(ctx["datasource"], {}) or {}).get("tables", {})
        user_payload = {
            **ctx,
            "tablesForDatasource": ds_tables,
        }
        user_text = (
            "Resolve this Tableau field to its (table, column):\n"
            + json.dumps(user_payload, indent=2)
        )
        reply = await self.client.ask_json(system_blocks, user_text)
        if not reply:
            return None
        tbl = reply.get("table")
        col = reply.get("column")
        if not tbl or not col:
            return None
        # Final sanity: the answer must exist in the snapshot. Claude
        # occasionally hallucinates column names; we'd rather drop the
        # hint than feed a phantom into the converter.
        if tbl not in ds_tables:
            return None
        if col not in ds_tables[tbl] and f"{col} (measure)" not in ds_tables[tbl]:
            return None
        return (tbl, col)

    async def resolve_all(
        self,
        warnings: List[Dict[str, Any]],
        worksheets: List[Dict[str, Any]],
        snapshot: Dict[str, Any],
        datasources: Optional[List[Dict[str, Any]]] = None,
        calc_index: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[Tuple[str, str], Tuple[str, str]]:
        """Resolve every field-resolution warning concurrently.

        Returns {(ds_name, field_name): (table, column)}. Warnings the
        agent couldn't (or wouldn't) resolve are simply absent from the
        result — the converter will keep dropping them.
        """
        # Filter to the kinds we handle. ('ds' kind is for the
        # datasource-routing resolver, not this one.)
        targets = [w for w in warnings if w.get("kind") in ("resolve", "filter")]
        if not targets:
            return {}

        # Dedup at the (ds, field) level — same dropped field warned by
        # both the projection path AND the filter path needs only one
        # resolution.
        unique: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for w in targets:
            key = (w["ds"], w["field"])
            if key not in unique:
                unique[key] = w

        system_blocks = self.build_system(snapshot)

        tasks = [
            self.resolve_one(w, worksheets, snapshot, system_blocks,
                            datasources=datasources,
                            calc_index=calc_index)
            for w in unique.values()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        out: Dict[Tuple[str, str], Tuple[str, str]] = {}
        for key, res in zip(unique.keys(), results):
            if res is not None:
                out[key] = res
        return out
