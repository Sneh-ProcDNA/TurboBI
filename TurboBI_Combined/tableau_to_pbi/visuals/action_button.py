"""Action button visual.

Tableau dashboards fake clickable buttons by dragging a worksheet that
contains only a label calc field (e.g. ``"Reset"``) and styling the
worksheet title to look like a button. The PBI analogue is a real
``actionButton`` — no projections, just a label and a navigation link.

These buttons clear slicers by re-applying the report's
``_default_state`` bookmark; the writer emits that bookmark once per
report when at least one action button asks for it.

Returns a (visual_dict, needs_default_bookmark) tuple so the caller —
which owns the report-level bookmark-emission flag — can light up
``ReportBuilder._needs_default_bookmark`` without this module reaching
back into builder state.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from ..config import SCHEMA
from ..utils import hex_id
from .helpers import expr_lit


def build_action_button(
    ws: Dict[str, Any],
    x: int, y: int, w: int, h: int, z: int,
) -> Tuple[Dict[str, Any], bool]:
    """Emit a Power BI actionButton bound to the default-state bookmark.

    Returns ``(visual_dict, needs_default_bookmark)``. The caller is
    responsible for recording the bookmark requirement (so the writer
    emits ``_default_state.bookmark.json`` exactly once per report).
    """
    label = (ws.get("name") or "").strip().rstrip()
    visual = {
        "$schema": SCHEMA["visual"],
        "name":    hex_id("visual", ws["name"]),
        "position": {
            "x": x, "y": y, "z": z,
            "height": h, "width": w, "tabOrder": z,
        },
        "visual": {
            "visualType": "actionButton",
            "drillFilterOtherVisuals": False,
            "objects": {
                "icon": [{"properties": {
                    "shapeType": expr_lit("CircleEmpty"),
                }}],
                "text": [{"properties": {
                    "text":       expr_lit(label or "Reset"),
                    "fontFamily": expr_lit("Arial"),
                    "fontSize":   expr_lit("11D"),
                    "show":       expr_lit("true"),
                }}],
                "visualLink": [{"properties": {
                    "type":     expr_lit("Bookmark"),
                    "bookmark": expr_lit("_default_state"),
                    "navigationSection": expr_lit(""),
                }}],
            },
        },
    }
    return visual, True
