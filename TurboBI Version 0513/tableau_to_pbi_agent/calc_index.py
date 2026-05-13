"""Build a {tableau_calc_id: caption} index from a .twb file.

Why this exists outside the parser: tableau_to_pbi.parser.TWBParser
only collects top-level <datasources>/<datasource>/<column> entries.
Calculated fields authored *per worksheet* live under
<view>/<datasource-dependencies>/<column> and never reach the model.
For the agent, those captions are exactly the bridge it needs to map a
warning like:

    [RESOLVE] 'Calculation_3378544172854779916' (ds=federated.0xt8...)

to the model column it most likely aliases (here, the caption is
'Region', and the model has a REGION column).

We do NOT modify the converter for this — pulling captions on demand
from the .twb keeps the agent self-contained.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional


def build_calc_index(twb_path: str) -> Dict[str, Dict[str, Any]]:
    """Walk every <column> in the .twb (top-level AND worksheet-scoped)
    and return {calc_id: {caption, role, datatype, formula}}.

    `calc_id` is the bare token without the surrounding `[...]` so it
    matches what the converter prints in [RESOLVE] warnings.
    """
    try:
        root = ET.parse(twb_path).getroot()
    except (OSError, ET.ParseError):
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for col in root.iter("column"):
        raw = (col.get("name") or "").strip()
        if not raw or "Calculation_" not in raw:
            continue
        calc_id = raw.strip("[]")
        if calc_id in out:
            continue  # first definition wins

        entry: Dict[str, Any] = {
            "caption":  col.get("caption"),
            "role":     col.get("role"),
            "datatype": col.get("datatype"),
        }
        calc_el = col.find("calculation")
        if calc_el is not None:
            formula = calc_el.get("formula")
            if formula:
                # Trim — full formulas can be long; we only need the
                # surface form to spot 'AVG(0)' or '[REGION]' patterns.
                entry["formula"] = formula[:200]

        # Drop empties so the JSON we send Claude stays small.
        out[calc_id] = {k: v for k, v in entry.items() if v}

    return out
