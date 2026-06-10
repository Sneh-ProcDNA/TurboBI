"""Theme overrides — optional user config for font fallbacks etc.

Loads a YAML or JSON config that lets users override a small set of
hardcoded defaults baked into the converter: today, just the font
fallback (``Tableau Medium`` -> ``Arial`` becomes ``Tableau Medium`` ->
``Segoe UI`` if the user prefers).

Keep this module ZERO-DEP at the cold path. ``apply_theme`` reads from
the active singleton state; the singleton is empty (``{}``) until
``load_theme`` is called. So a CLI that never passes ``--theme`` pays
no cost; behavior is identical to the pre-Phase-5b code.

Public surface:

    from tableau_to_pbi.theme import load_theme, get_font_fallback

    # CLI entry calls this once, before SemanticModel.build() or
    # ReportBuilder.build().
    load_theme(Path("/path/to/theme.yaml"))    # or .json

    # Visual modules call the lookup helpers — they return the user's
    # override when present, the hardcoded default when not.
    get_font_fallback("Arial")   # -> "Arial" by default, or user's choice

Schema (YAML or JSON, same structure):

    font_fallback: "Segoe UI"     # default Arial. Used when a Tableau
                                  # font isn't in PBI's shipped set.
    font_allowlist:               # optional. List of additional fonts
      - Inter                     # to recognise as PBI-safe (extending
      - "JetBrains Mono"          # the built-in PBI_SAFE_FONTS set).

Either or both fields can be omitted; defaults apply.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Set

from ._logging import get_logger

_log = get_logger("THEME")


# Module-level singleton. Empty by default; populated by ``load_theme``.
# Keys mirror the YAML schema; consumers go through the typed helpers
# (``get_font_fallback``, ``is_font_allowlisted``) rather than reading
# ``_THEME`` directly.
_THEME: Dict[str, Any] = {}


# Default font fallback that the helpers fall back to when no theme is
# loaded. Matches the original hardcoded "Arial" in
# ``visuals/helpers.safe_font_family``.
_DEFAULT_FONT_FALLBACK = "Arial"


def load_theme(path: Optional[Path]) -> None:
    """Load a theme file. ``None`` is a no-op (idempotent reset)."""
    global _THEME
    if path is None:
        _THEME = {}
        return
    p = Path(path)
    if not p.exists():
        _log.warning(f"Theme file not found: {p} - using built-in defaults.")
        return
    raw = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore[import-untyped]
            except ImportError:
                _log.warning(
                    "PyYAML not installed - falling back to JSON parse. "
                    "Rename theme to .json or `pip install pyyaml`."
                )
                _THEME = json.loads(raw)
                return
            data = yaml.safe_load(raw) or {}
        elif suffix == ".json":
            data = json.loads(raw)
        else:
            _log.warning(
                f"Unrecognised theme file extension '{suffix}' - "
                f"trying JSON parse."
            )
            data = json.loads(raw)
    except Exception as e:
        _log.warning(
            f"Failed to parse theme file {p}: {type(e).__name__}: {e}. "
            f"Using built-in defaults."
        )
        return
    if not isinstance(data, dict):
        _log.warning(
            f"Theme file {p} did not parse to a dict - using built-in defaults."
        )
        return
    _THEME = data
    _log.info(f"Loaded theme from {p}")


def reset_theme() -> None:
    """Clear the loaded theme (useful for tests)."""
    global _THEME
    _THEME = {}


def get_font_fallback(default: Optional[str] = None) -> str:
    """Font to use when a Tableau-supplied font isn't recognised by PBI.

    Returns the theme's ``font_fallback`` when set, ``default`` when
    passed, otherwise the built-in ``"Arial"``.
    """
    override = _THEME.get("font_fallback")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return default if default is not None else _DEFAULT_FONT_FALLBACK


def extra_font_allowlist() -> Set[str]:
    """Additional fonts the user has allowlisted, lowercased for
    case-insensitive membership tests in ``safe_font_family``.

    Returns an empty set when no theme is loaded or the theme didn't
    set ``font_allowlist``.
    """
    raw = _THEME.get("font_allowlist") or []
    if not isinstance(raw, list):
        return set()
    return {str(x).strip().lower() for x in raw if str(x).strip()}


def autoload_from_env() -> None:
    """Convenience for CLI entry points: read ``TURBOBI_THEME`` env var
    and load it if set. Silent no-op otherwise.
    """
    p = os.environ.get("TURBOBI_THEME", "").strip()
    if p:
        load_theme(Path(p))
