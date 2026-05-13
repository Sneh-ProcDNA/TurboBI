"""KPI variant: '1 dim + measure on Label encoding' -> card.

Pattern, in concrete terms:
    markClass.lower() == 'automatic'
    AND len(rowFields) + len(colFields) == 1     (single dim on a shelf)
    AND labelField is a measure                   (the value to show)
    AND no other shelves have measures

Tableau renders this as a labeled KPI tile (the Production Report has
many of these — 'Tableau US Step Number' worksheets). Without a rule
they currently fall to tableEx. The card visual is the right answer:
PBI shows the latest aggregated label value as a single number with
the dimension as the row context.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..observer_base import ChangeProposal, Observer
from tableau_to_pbi.config import AGG_TABLE


_RULES_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tableau_to_pbi" / "visual_rules.json"
)


_RULE = {
    "name":     "card (label measure with single grouping dimension)",
    # Priority lower than the existing card rule (10) so the
    # text-only-pattern still wins when both could match. The KPI-dim
    # pattern is more specific in shape but the text-only pattern is
    # closer to user intent for single-cell KPIs.
    "priority": 15,
    "when": {
        "measure_count":   "0",
        "dimension_count": "==1",
        "encoding":        "label",
    },
    "visual":   "card",
}


_MEASURE_AGGS = {agg for agg, info in AGG_TABLE.items() if info["is_measure"]}


def _matches_pattern(ws: Dict[str, Any]) -> bool:
    if (ws.get("markClass") or "").lower() != "automatic":
        return False
    rf = ws.get("rowFields") or []
    cf = ws.get("colFields") or []
    if len(rf) + len(cf) != 1:
        return False
    # The single shelf field must be a dimension (not a measure).
    only = (rf + cf)[0]
    only_agg = (only.get("agg") or "").lower()
    if only_agg in _MEASURE_AGGS:
        return False
    # Label must be set AND must be a measure.
    label = ws.get("labelField") or {}
    label_agg = (label.get("agg") or "").lower()
    if not label.get("field") or label_agg not in _MEASURE_AGGS:
        return False
    return True


class KpiDimLabelObserver(Observer):
    name = "kpi_dim_with_label_measure"
    description = (
        "Tableau worksheets with mark='Automatic', a single dimension "
        "on a shelf, and a measure on the Label encoding render as KPI "
        "cards in Tableau but currently fall through to tableEx."
    )

    def observe(self, corpus_facts: List[Dict[str, Any]]) -> List[ChangeProposal]:
        if _rule_already_present():
            return []

        flagged: List[Dict[str, Any]] = []
        for fact in corpus_facts:
            ws_list = fact.get("worksheets") or []
            visuals = fact.get("visuals") or {}
            wb = fact.get("workbook_label", "?")
            from .card_detector import _index_visuals_by_worksheet
            ws_to_paths = _index_visuals_by_worksheet(visuals, ws_list)
            for ws in ws_list:
                if not _matches_pattern(ws):
                    continue
                paths = ws_to_paths.get(ws["name"]) or []
                for vpath in paths:
                    cur_visual = visuals.get(vpath, {})
                    cur_type = (cur_visual.get("visual") or {}).get("visualType")
                    if cur_type == "card":
                        continue
                    flagged.append({
                        "workbook":    wb,
                        "visual_path": vpath,
                        "from":        cur_type or "tableEx",
                        "to":          "card",
                        "worksheet":   ws["name"],
                    })

        if not flagged:
            return []
        return [ChangeProposal(
            name=self.name,
            description=self.description,
            files_to_modify=[_RULES_PATH],
            apply=_apply_rule,
            expected_diffs=flagged,
            summary=(
                f"prepend visual_rules.json auto_rule "
                f"'{_RULE['name']}' (flips {len(flagged)} visual(s) "
                f"from tableEx -> card)"
            ),
        )]


def _rule_already_present() -> bool:
    if not _RULES_PATH.exists():
        return False
    rules = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    for r in rules.get("auto_rules") or []:
        if r.get("name") == _RULE["name"]:
            return True
    return False


def _apply_rule() -> None:
    rules = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    auto = rules.get("auto_rules") or []
    if any(r.get("name") == _RULE["name"] for r in auto):
        return
    rules["auto_rules"] = [_RULE] + auto
    _RULES_PATH.write_text(
        json.dumps(rules, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
