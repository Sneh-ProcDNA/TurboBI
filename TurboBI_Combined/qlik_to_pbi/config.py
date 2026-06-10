"""Static configuration for the Qlik -> PBIP converter."""

import json
from pathlib import Path

DEFAULT_PAGE_WIDTH: int = 1280
DEFAULT_PAGE_HEIGHT: int = 720

DEFAULT_TABLE_NAME = "Data"

SCHEMA = {
    "pbip":           "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
    "platform":       "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
    "pbism":          "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
    "pbir":           "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
    "report":         "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.2.0/schema.json",
    "version":        "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
    "pages_metadata": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
    "page":           "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json",
    "visual":         "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
}


def _load_visual_type_map() -> dict:
    """Load the Qlik -> PBI visual-type lookup table.

    Authoritative source is ``visual_rules.json`` next to this file.
    The JSON keeps the mapping editable without touching Python --
    drop a new entry there to extend coverage. Keys are normalised to
    lower-case at load time so caller lookups need no extra hygiene.

    If the file is missing or malformed we fall back to a minimal
    built-in set so the converter still produces something useful.
    """
    rules_path = Path(__file__).with_name("visual_rules.json")
    try:
        with open(rules_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        data = {}
    raw = data.get("visual_type_map") or {}
    return {(k or "").lower(): v for k, v in raw.items()}


VISUAL_TYPE_MAP = _load_visual_type_map()
