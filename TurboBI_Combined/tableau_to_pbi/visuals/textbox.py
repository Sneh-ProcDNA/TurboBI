"""Textbox visual.

PBI's ``textbox`` is the catch-all for label/legend/image-placeholder
zones in a Tableau dashboard. The visual carries one paragraph of
formatted text and an optional background/border on its container.

This module emits the JSON dict only — the dispatcher in ``report.py``
decides when a Tableau zone falls back to a textbox (legend zones,
text-only tiles, image placeholders, unrecognised dashboard objects).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..config import SCHEMA
from ..utils import hex_id
from .helpers import color_expr, expr_lit


def build_textbox(
    label: str, x: int, y: int, w: int, h: int, z: int, zid: str,
    style: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Emit a PBI textbox visual JSON dict.

    ``style`` is a flat dict carrying any of: ``fontSize``, ``fontFamily``,
    ``fontColor``, ``fontWeight``, ``italic``, ``underline``,
    ``textAlign``, ``backgroundColor``, ``borderColor``, ``borderWidth``,
    ``borderStyle``. Missing keys produce a minimal textbox with PBI
    defaults.

    Container chrome (background / border) is placed under
    ``visualContainerObjects`` — PBI silently ignores those keys under
    ``objects`` for textbox visuals, which is why an earlier version of
    this code lost colored tiles on rebuild.
    """
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
                "show":  expr_lit("true"),
                "color": color_expr(style["backgroundColor"]),
            },
        }]

    # Container border — only emitted when the parser saw a real
    # border (style.borderStyle != 'none' was already filtered).
    if (style.get("borderColor") or style.get("borderWidth")
            or style.get("borderStyle")):
        border_props: Dict[str, Any] = {"show": expr_lit("true")}
        if style.get("borderColor"):
            border_props["color"] = color_expr(style["borderColor"])
        if style.get("borderWidth"):
            border_props["width"] = expr_lit(f"{style['borderWidth']}D")
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
