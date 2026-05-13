"""Static configuration: lookups, default canvas sizes, and the PBIP
schema URLs we stamp into output files.

Nothing in this module reads or writes — it's pure data so callers can
treat it as a frozen-ish reference."""

from typing import Dict


# ---------------------------------------------------------------------------
# Canvas sizing
# ---------------------------------------------------------------------------

DEFAULT_PAGE_WIDTH:  int = 1280
DEFAULT_PAGE_HEIGHT: int = 720


# ---------------------------------------------------------------------------
# Mark / visualType lookup lives in visual_rules.json (so the user can edit
# selection rules without touching code). visual_picker.py reads that file.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Per-visualType slot mapping. Tells the report builder which Tableau shelf
# (rows / cols / color / size) feeds which PBI projection role.
# ---------------------------------------------------------------------------

VISUAL_SLOTS: Dict[str, Dict[str, str]] = {
    # Bars & columns
    "barChart":                       {"category": "Category", "value_x": "Y",        "color": "Series"},
    "columnChart":                    {"category": "Category", "value_y": "Y",        "color": "Series"},
    "clusteredBarChart":              {"category": "Category", "value_x": "Y",        "color": "Series"},
    "clusteredColumnChart":           {"category": "Category", "value_y": "Y",        "color": "Series"},
    "stackedBarChart":                {"category": "Category", "value_x": "Y",        "color": "Series"},
    "stackedColumnChart":             {"category": "Category", "value_y": "Y",        "color": "Series"},
    "hundredPercentStackedBarChart":  {"category": "Category", "value_x": "Y",        "color": "Series"},
    "hundredPercentStackedColumnChart":{"category": "Category", "value_y": "Y",       "color": "Series"},
    # Lines & areas
    "lineChart":                      {"category": "Category", "value_y": "Y",        "color": "Series"},
    "areaChart":                      {"category": "Category", "value_y": "Y",        "color": "Series"},
    "stackedAreaChart":               {"category": "Category", "value_y": "Y",        "color": "Series"},
    "lineStackedColumnComboChart":    {"category": "Category", "value_y": "Y", "value_y2": "Y2", "color": "Series"},
    "lineClusteredColumnComboChart":  {"category": "Category", "value_y": "Y", "value_y2": "Y2", "color": "Series"},
    # Scatter & bubble
    "scatterChart":                   {"x": "X",               "y": "Y",              "color": "Color", "size": "Size", "details": "Details"},
    "bubbleChart":                    {"x": "X",               "y": "Y",              "color": "Color", "size": "Size", "details": "Details"},
    # Circular
    "pieChart":                       {"category": "Category", "value":   "Y",        "color": "Legend"},
    "donutChart":                     {"category": "Category", "value":   "Y",        "color": "Legend"},
    "treemap":                        {"category": "Category", "value":   "Values",   "color": "ColorSaturation", "details": "Details"},
    "funnel":                         {"category": "Category", "value":   "Y",        "color": "ColorSaturation"},
    "waterfallChart":                 {"category": "Category", "value":   "Y",        "color": "Breakdown"},
    # Tables & cards
    "pivotTable":                     {"row": "Rows", "col": "Columns",                "value":   "Values", "details": "Details"},
    "tableEx":                        {"value": "Values", "details": "Details"},
    # PBI's modern KPI card uses visualType "cardVisual" with a single
    # "Data" well that accepts the aggregated headline value. Earlier code
    # emitted "card" with "Values" / "Category" wells (the legacy single-
    # value card); leaving both bindings here so anything still mapped to
    # "card" downstream keeps working with the legacy shape.
    "cardVisual":                     {"value": "Data", "category": "Data", "tooltip": "Tooltips"},
    "card":                           {"value": "Values", "category": "Category", "tooltip": "Tooltips"},
    "multiRowCard":                   {"value": "Values", "category": "Category", "tooltip": "Tooltips"},
    "slicer":                         {"value": "Values"},
    # Maps
    # Power BI's built-in `map`/`filledMap`/`shapeMap` visuals are
    # retained as legacy fall-throughs (kept so any caller still
    # referencing them by name doesn't break), but the picker now
    # routes every Tableau map mark — `map`, `point`, `polygon`,
    # `filled-map`, `multipolygon` — to the `azureMap` visualType.
    # Azure Maps has a richer base-map style set and is the modern
    # PBI default; PBI Desktop also auto-stamps the same Latitude /
    # Longitude / Size / Legend / Color / Tooltips wells on
    # azureMap, so the existing geo bindings carry over unchanged.
    "azureMap":                       {"lat": "Latitude", "lon": "Longitude", "location": "Location", "size": "Size", "color": "Bubble color", "legend": "Legend", "details": "Tooltips"},
    "map":                            {"lat": "Latitude", "lon": "Longitude", "size": "Size", "color": "Color", "details": "Details"},
    "filledMap":                      {"location": "Location", "color": "ColorSaturation", "details": "Details"},
    "shapeMap":                       {"location": "Location", "color": "ColorSaturation"},
    # Misc
    "ribbonChart":                    {"category": "Category", "value_y": "Y",        "color": "Series"},
}


# ---------------------------------------------------------------------------
# Tableau datatype -> TMDL dataType.
# ---------------------------------------------------------------------------

DATATYPE_TMDL: Dict[str, str] = {
    "string":   "string",
    "integer":  "int64",
    "real":     "double",
    "boolean":  "boolean",
    "date":     "dateTime",
    "datetime": "dateTime",
    "spatial":  "string",
}


# ---------------------------------------------------------------------------
# Aggregation tokens. The "label" column is what we put in displayName for
# the visual; "fn" is the Power BI Aggregation.Function ordinal.
# Ordinals: 0=Sum, 1=Average, 2=Count, 3=Min, 4=Max, 5=CountNonNull,
# 6=Median, 7=StandardDeviation, 8=Variance.
# ---------------------------------------------------------------------------

AGG_TABLE: Dict[str, Dict[str, object]] = {
    "sum":     {"label": "Sum",      "fn": 0,  "is_measure": True},
    "avg":     {"label": "Average",  "fn": 1,  "is_measure": True},
    "average": {"label": "Average",  "fn": 1,  "is_measure": True},
    "cnt":     {"label": "Count",    "fn": 2,  "is_measure": True},
    "count":   {"label": "Count",    "fn": 2,  "is_measure": True},
    "min":     {"label": "Min",      "fn": 3,  "is_measure": True},
    "max":     {"label": "Max",      "fn": 4,  "is_measure": True},
    # Tableau emits Count Distinct as either 'cntd' or 'ctd' depending on
    # the workbook version. Both must register as measure aggregations,
    # otherwise CountD(field) lands in Category instead of the Y-axis.
    "cntd":    {"label": "Count distinct", "fn": 5, "is_measure": True},
    "ctd":     {"label": "Count distinct", "fn": 5, "is_measure": True},
    "med":     {"label": "Median",   "fn": 6,  "is_measure": True},
    "median":  {"label": "Median",   "fn": 6,  "is_measure": True},
    "std":     {"label": "StDev",    "fn": 7,  "is_measure": True},
    "stdev":   {"label": "StDev",    "fn": 7,  "is_measure": True},
    "var":     {"label": "Variance", "fn": 8,  "is_measure": True},
    # Date-part roles — we keep them so we can show "Year of Date"-style
    # display names without trying to wire DAX measures.
    "yr":      {"label": "Year",     "fn": None, "is_measure": False},
    "qr":      {"label": "Quarter",  "fn": None, "is_measure": False},
    "mn":      {"label": "Month",    "fn": None, "is_measure": False},
    "wk":      {"label": "Week",     "fn": None, "is_measure": False},
    "dy":      {"label": "Day",      "fn": None, "is_measure": False},
    "hr":      {"label": "Hour",     "fn": None, "is_measure": False},
    "mi":      {"label": "Minute",   "fn": None, "is_measure": False},
    "sd":      {"label": "Second",   "fn": None, "is_measure": False},
    "attr":    {"label": "First",    "fn": None, "is_measure": False},
}


# ---------------------------------------------------------------------------
# PBIP schema URLs.
# ---------------------------------------------------------------------------

SCHEMA = {
    "pbip":            "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
    "platform":        "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
    "pbism":           "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
    "pbir":            "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
    "report":          "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.2.0/schema.json",
    "version":         "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
    "pages_metadata":  "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
    "page":            "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json",
    "visual":          "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
}
