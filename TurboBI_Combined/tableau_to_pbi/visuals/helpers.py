"""Shared formatting helpers used by every visual builder.

These were originally module-level private helpers inside ``report.py``
(``_expr_lit``, ``_color_expr``, etc.). Hoisting them out lets each
per-visual-family module in this package call them without importing
from ``report.py`` — which would create a circular import the moment
``report.py`` starts delegating to its own ``visuals`` submodules.

Public names (no underscore prefix) are the new convention for shared
package API. ``report.py`` re-exports them under the legacy private
names so its many internal call sites don't have to change.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


# Font families PBI Desktop ships with. Anything not on this list gets
# replaced with Arial — Tableau workbooks routinely use proprietary fonts
# ('Tableau Medium', 'Tableau Book') that PBI silently falls back to a
# default for, producing visually inconsistent output unless we
# substitute up-front.
PBI_SAFE_FONTS = {
    "arial", "arial black", "arial unicode ms",
    "calibri", "calibri light", "cambria", "cambria math",
    "candara", "comic sans ms", "consolas", "constantia", "corbel",
    "courier new", "din", "din light",
    "georgia", "lato", "lucida sans unicode",
    "segoe", "segoe ui", "segoe ui light", "segoe ui semibold",
    "symbol", "tahoma", "times new roman", "trebuchet ms",
    "verdana", "wingdings",
}


def expr_lit(value: str) -> Dict[str, Any]:
    """Wrap a literal value in PBI's expr/Literal envelope.

    PBI property values inside visualContainerObjects/objects use
    {"expr": {"Literal": {"Value": "<v>"}}}. String literals must be
    single-quoted ('foo'); numeric literals use a 'D' suffix (11D);
    booleans are bare 'true'/'false'.
    """
    return {"expr": {"Literal": {"Value": value}}}


def color_expr(hex_color: str) -> Dict[str, Any]:
    """Wrap a hex color in PBI's solid/color envelope."""
    return {"solid": {"color": expr_lit(f"'{hex_color}'")}}


def contrast_text_color(bg_hex: Optional[str]) -> str:
    """Pick a readable text color (#1b1b1b or #ffffff) given a header
    backgroundColor. Used when the TWB sets the header background but
    its matching fontColor is field-scoped (and therefore dropped by
    `_parse_worksheet_header_style`). Without this fallback, a dark
    header background renders dark default text and the column-name
    text becomes invisible.

    Returns dark grey for empty / unparseable backgrounds so the
    pre-existing Tableau-like default (#1b1b1b on white-ish) stays
    intact when no background was specified.
    """
    if not bg_hex:
        return "#1b1b1b"
    s = bg_hex.strip().lstrip("#")
    # Strip alpha if present (#RRGGBBAA).
    if len(s) == 8:
        s = s[:6]
    if len(s) != 6:
        return "#1b1b1b"
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
    except ValueError:
        return "#1b1b1b"
    # WCAG relative luminance approximation; threshold ~140 (out of 255)
    # is the canonical readability cutoff for picking light vs dark text.
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#ffffff" if luminance < 140 else "#1b1b1b"


def safe_font_family(name: Optional[str]) -> str:
    """Return `name` if it's a PBI-shipped font; otherwise the theme's
    configured fallback (``Arial`` by default).

    PBI Desktop only renders the fonts it ships with. Tableau-specific
    fonts ('Tableau Medium', 'Tableau Bold') silently fall back to a PBI
    default when the file opens, so the visuals never look like Tableau
    intended. Substituting up-front keeps font sizes / weights consistent
    across the converted report.

    The fallback target AND additional allowlisted font names are
    user-configurable via ``tableau_to_pbi.theme.load_theme(...)``. When
    no theme is loaded (the common case), behaviour is identical to the
    pre-Phase-5b code.
    """
    from ..theme import get_font_fallback, extra_font_allowlist
    fallback = get_font_fallback()
    if not name:
        return fallback
    lower = name.strip().lower()
    if lower in PBI_SAFE_FONTS or lower in extra_font_allowlist():
        return name
    return fallback


def normalize_font_size(size: Any) -> Optional[str]:
    """Coerce a font size (int/float/str like '11pt') to PBI's '11D' literal."""
    if size is None:
        return None
    if isinstance(size, (int, float)):
        return f"{size}D"
    s = str(size).strip()
    if not s:
        return None
    digits = re.sub(r"[^\d.]", "", s)
    if not digits:
        return None
    return f"{digits}D"
