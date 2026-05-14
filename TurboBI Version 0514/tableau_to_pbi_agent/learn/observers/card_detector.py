"""Detect 'text-only worksheet -> card' opportunities.

Pattern, in concrete terms:
    markClass.lower() == 'automatic'
    AND rowFields is empty
    AND colFields is empty
    AND labelField (or text encoding) is set
The visual_picker currently falls all the way through auto_rules and
lands on the fallback ('tableEx'). The right answer for these is a
single-value 'card' — Tableau renders them as KPI tiles.

The observer's data-level fix: prepend a new auto_rules entry to
visual_rules.json that catches this shape and emits 'card'. We prepend
(rather than append) so the new rule runs BEFORE the existing
catch-alls, but AFTER any rule that already requires non-zero
measure/dim counts (so we don't accidentally over-fire).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..observer_base import ChangeProposal, Observer


_RULES_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tableau_to_pbi" / "visual_rules.json"
)


# The rule we want to land in visual_rules.json. Held in code so the
# observer is self-contained; we look for it by `name` to make the
# `apply()` step idempotent.
_RULE = {
    "name":   "card (text-only worksheet, label encoding)",
    "when":   {
        "measure_count":   "0",
        "dimension_count": "0",
        "encoding":        "label",
    },
    "visual": "card",
}


def _matches_pattern(ws: Dict[str, Any]) -> bool:
    if (ws.get("markClass") or "").lower() != "automatic":
        return False
    if ws.get("rowFields"):
        return False
    if ws.get("colFields"):
        return False
    label = (ws.get("labelField") or {}).get("field", "")
    return bool(label)


class CardObserver(Observer):
    name = "card_from_text_only_worksheet"
    description = (
        "Tableau worksheets with mark='Automatic' and only a text/label "
        "encoding (no rows or cols) are KPI cards. Currently they fall "
        "through the picker and land as tableEx."
    )

    def observe(self, corpus_facts: List[Dict[str, Any]]) -> List[ChangeProposal]:
        """corpus_facts is a list of {workbook_label, parser_worksheets,
        snapshot_visuals}. We look for matching worksheets and the
        visuals they map to in the current snapshot.

        A worksheet placed on multiple dashboard pages produces multiple
        visual.json files sharing the same hex_id. We flag ALL of them
        — the rule flips every one, so the regression gate's expected-
        diff list must too.
        """
        # Already-merged check: if visual_rules.json already contains a
        # rule whose `name` matches ours, the previous run handled it.
        if _rule_already_present():
            return []

        flagged: List[Dict[str, Any]] = []
        for fact in corpus_facts:
            ws_list = fact.get("worksheets") or []
            visuals = fact.get("visuals") or {}
            wb = fact.get("workbook_label", "?")
            # Map worksheet name -> ALL visual_paths that materialize it.
            ws_to_paths = _index_visuals_by_worksheet(visuals, ws_list)
            for ws in ws_list:
                if not _matches_pattern(ws):
                    continue
                paths = ws_to_paths.get(ws["name"]) or []
                for vpath in paths:
                    cur_visual = visuals.get(vpath, {})
                    cur_type = (cur_visual.get("visual") or {}).get("visualType")
                    if cur_type == "card":
                        continue  # already a card — nothing to learn
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
    """Prepend the new rule to auto_rules. Idempotent — duplicate names
    are skipped on the second run."""
    rules = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    auto = rules.get("auto_rules") or []
    if any(r.get("name") == _RULE["name"] for r in auto):
        return
    rules["auto_rules"] = [_RULE] + auto
    _RULES_PATH.write_text(
        json.dumps(rules, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _index_visuals_by_worksheet(
    visuals: Dict[str, Any], ws_list: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Map worksheet name -> ALL visual_paths whose visual.name matches
    the hex_id the converter would assign that worksheet.

    Returns a list (not a single path) because the same worksheet can
    appear on multiple dashboard pages — each placement is a separate
    visual.json with the same `name` but a different page directory.
    """
    from tableau_to_pbi.utils import hex_id
    # name -> [path, path, ...]
    name_to_paths: Dict[str, List[str]] = {}
    for path, v in visuals.items():
        if not isinstance(v, dict):
            continue
        vname = v.get("name", "")
        if vname:
            name_to_paths.setdefault(vname, []).append(path)
    out: Dict[str, List[str]] = {}
    for ws in ws_list:
        ws_name = ws.get("name", "")
        if not ws_name:
            continue
        candidate = hex_id("visual", ws_name)
        paths = name_to_paths.get(candidate)
        if paths:
            out[ws_name] = list(paths)
    return out
