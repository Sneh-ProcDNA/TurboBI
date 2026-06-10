"""Qlik Sense -> Power BI colour-theme mapping.

Power BI's default theme (CY24SU02 base) colours series with its own
palette (#118DFF blue first), which looks nothing like a Qlik Sense
app. This module owns the converter's colour knowledge:

* The built-in Qlik Sense theme palettes (hex), keyed by theme id.
* ``build_report_theme`` -- turns the captured Qlik theme (the
  ``theme.json`` sidecar written by the engine unbuild, when the app
  uses a custom theme) or the app's theme id into a Power BI report
  theme document. The writer registers it as a RegisteredResources
  CustomTheme so every visual's default series colours match Qlik
  without per-visual overrides.

The PBI theme's ``dataColors`` list is ordered: series ``i`` takes
``dataColors[i]``, which mirrors how Qlik walks its data palette, so
multi-series charts keep Qlik's colour-to-series assignment. The
single-series default (Qlik's ``dataColors.primaryColor``) is applied
per-visual by the report builder (see ``_build_chart_objects``)
because PBI has no separate "single colour" theme slot -- it would
otherwise use ``dataColors[0]``, which in Qlik themes is NOT the
primary colour.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ._logging import get_logger

_log = get_logger("THEME")


# ---------------------------------------------------------------------------
# Built-in Qlik Sense palettes
# ---------------------------------------------------------------------------

# Qlik Cloud "Horizon" theme (the default for new cloud apps since 2023).
# Ordered data palette -- confirmed against rendered apps; these are the
# values the report builder has always used for ``dimensionScheme: "12"``.
QLIK_HORIZON_12: List[str] = [
    "#3F8097",  # 0  teal blue
    "#73B88E",  # 1  sage green
    "#C8D97D",  # 2  lime
    "#FFC44D",  # 3  amber
    "#F0835E",  # 4  coral
    "#B35D90",  # 5  mauve
    "#65A4C1",  # 6  sky blue
    "#A0CAAA",  # 7  light sage
    "#D9E4A3",  # 8  light lime
    "#FFD799",  # 9  light amber
    "#F5B08C",  # 10 light coral
    "#D49AC4",  # 11 light lavender
]

# Classic Qlik Sense themes ("Sense Classic" / "Sense Focus" /
# "Sense Breeze" and on-prem Enterprise defaults). The "12 colors"
# data palette is the muted-rainbow scheme shared by all three.
QLIK_CLASSIC_12: List[str] = [
    "#332288", "#6699cc", "#88ccee", "#44aa99", "#117733", "#999933",
    "#ddcc77", "#661100", "#cc6677", "#aa4466", "#882255", "#aa4499",
]

# Single-colour defaults (``dataColors.primaryColor`` in the Qlik theme
# JSON). This is the colour a default-coloured single-measure bar/line
# chart actually renders with in Qlik.
QLIK_CLASSIC_PRIMARY = "#26a0a7"   # the signature Sense teal
QLIK_HORIZON_PRIMARY = "#3F8097"   # horizon data palette colour 0

# theme id (lower-case) -> (data palette, primary colour). The classic
# theme family shares one palette; "horizon" is the cloud default.
_BUILTIN_THEMES: Dict[str, Tuple[List[str], str]] = {
    "horizon":        (QLIK_HORIZON_12, QLIK_HORIZON_PRIMARY),
    "sense":          (QLIK_CLASSIC_12, QLIK_CLASSIC_PRIMARY),
    "sense-classic":  (QLIK_CLASSIC_12, QLIK_CLASSIC_PRIMARY),
    "classic":        (QLIK_CLASSIC_12, QLIK_CLASSIC_PRIMARY),
    "card":           (QLIK_CLASSIC_12, QLIK_CLASSIC_PRIMARY),
    "sense-focus":    (QLIK_CLASSIC_12, QLIK_CLASSIC_PRIMARY),
    "focus":          (QLIK_CLASSIC_12, QLIK_CLASSIC_PRIMARY),
    "sense-breeze":   (QLIK_CLASSIC_12, QLIK_CLASSIC_PRIMARY),
    "breeze":         (QLIK_CLASSIC_12, QLIK_CLASSIC_PRIMARY),
    "bright":         (QLIK_CLASSIC_12, QLIK_CLASSIC_PRIMARY),
}

# Cloud apps default to horizon when no theme id was captured.
_DEFAULT_THEME_ID = "horizon"


def builtin_theme_ids() -> List[str]:
    """The theme ids we ship palettes for (no tenant fetch needed)."""
    return sorted(_BUILTIN_THEMES.keys())


# ---------------------------------------------------------------------------
# Qlik theme JSON -> palette extraction
# ---------------------------------------------------------------------------

def _normalize_hex(c: Any) -> Optional[str]:
    """`#abc`/`#aabbcc`(/+alpha) -> canonical `#aabbcc` form, else None."""
    if not isinstance(c, str):
        return None
    c = c.strip()
    if not c.startswith("#"):
        return None
    body = c[1:]
    if not all(ch in "0123456789abcdefABCDEF" for ch in body):
        return None
    if len(body) in (3, 4):
        body = "".join(ch * 2 for ch in body)
    if len(body) in (6, 8):
        return "#" + body
    return None


def _largest_color_list(scale: Any) -> List[str]:
    """A Qlik data-palette ``scale`` is either a flat colour list or (for
    "pyramid" palettes) a list of lists keyed by class count -- take the
    longest row so we keep the full palette."""
    if not isinstance(scale, list) or not scale:
        return []
    if all(isinstance(x, list) for x in scale):
        scale = max(scale, key=len)
    out: List[str] = []
    for c in scale:
        h = _normalize_hex(c)
        if h:
            out.append(h)
    return out


def _palette_from_qlik_theme(theme_json: Dict[str, Any]) -> Tuple[List[str], str]:
    """Extract ``(data_palette, primary)`` from a Qlik theme JSON document.

    Qlik custom themes carry::

        {"dataColors": {"primaryColor": "#...", "nullColor": ...},
         "palettes":   {"data": [{"name": ..., "scale": [...]}, ...],
                        "ui":   [...]},
         "scales":     [...gradients, not used...]}

    Either half may be missing; we fall back to the horizon defaults for
    whatever isn't resolvable.
    """
    palette: List[str] = []
    primary = ""

    data_palettes = ((theme_json.get("palettes") or {}).get("data")) or []
    if isinstance(data_palettes, list):
        for p in data_palettes:
            if not isinstance(p, dict):
                continue
            colors = _largest_color_list(p.get("scale") or p.get("colors"))
            if colors:
                palette = colors
                break

    dc = theme_json.get("dataColors")
    if isinstance(dc, dict):
        primary = _normalize_hex(dc.get("primaryColor")) or ""

    if not palette:
        palette = list(QLIK_HORIZON_12)
    if not primary:
        primary = palette[0]
    return palette, primary


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_palette(
    qlik_theme: Optional[Dict[str, Any]],
    theme_id: Optional[str],
) -> Dict[str, Any]:
    """Resolve the colour palette the converted report should use.

    Priority:
      1. A captured Qlik theme document (``theme.json`` sidecar) -- the
         app's actual theme, custom or built-in.
      2. The app's theme id matched against the built-in palette table.
      3. The horizon defaults (current Qlik Cloud default theme).

    Returns ``{"data_colors": [...], "primary": "#...", "source": str}``.
    """
    if isinstance(qlik_theme, dict) and qlik_theme:
        palette, primary = _palette_from_qlik_theme(qlik_theme)
        return {
            "data_colors": palette,
            "primary": primary,
            "source": "captured app theme",
        }

    tid = (theme_id or "").strip().lower()
    if tid in _BUILTIN_THEMES:
        palette, primary = _BUILTIN_THEMES[tid]
        return {
            "data_colors": list(palette),
            "primary": primary,
            "source": f"built-in theme '{tid}'",
        }

    palette, primary = _BUILTIN_THEMES[_DEFAULT_THEME_ID]
    return {
        "data_colors": list(palette),
        "primary": primary,
        "source": f"default ('{_DEFAULT_THEME_ID}')"
                  + (f"; unknown theme id '{tid}'" if tid else ""),
    }


def build_report_theme(
    qlik_theme: Optional[Dict[str, Any]],
    theme_id: Optional[str],
) -> Dict[str, Any]:
    """Build the Power BI report-theme document + palette info.

    Returns ``{"pbi_theme": <theme JSON dict>, "data_colors": [...],
    "primary": "#...", "source": str}``. The ``pbi_theme`` dict is what
    the writer drops under ``StaticResources/RegisteredResources/`` and
    registers as the report's CustomTheme.

    The theme document is deliberately minimal -- ``name`` +
    ``dataColors`` + ``tableAccent`` -- so it overrides ONLY the series
    palette and inherits everything else (fonts, backgrounds, spacing)
    from the base theme. Stamping foreground/background too would
    restyle every textbox and table in ways Qlik didn't ask for.
    """
    info = resolve_palette(qlik_theme, theme_id)
    pbi_theme: Dict[str, Any] = {
        "name": "QlikSenseColors",
        "dataColors": list(info["data_colors"]),
        "tableAccent": info["primary"],
    }
    return {
        "pbi_theme": pbi_theme,
        "data_colors": list(info["data_colors"]),
        "primary": info["primary"],
        "source": info["source"],
    }
