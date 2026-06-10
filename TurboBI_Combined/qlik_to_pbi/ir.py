"""Typed intermediate representation for the Qlik -> PBIP pipeline.

The parser reads the ``qlik app unbuild`` directory into a single object
that every downstream stage (model, report, conversion-report, writer)
consumes. Historically that object was a bare ``dict`` accessed through
``ir.get("sheets")`` etc. ``QlikIR`` formalises that contract as a typed
dataclass so the module boundaries document exactly what flows between
stages -- WITHOUT changing any leaf access pattern: it still supports
``ir.get(key)``, ``ir[key]``, ``ir[key] = value`` and ``key in ir`` so
the dozens of existing call sites keep working verbatim.

Carrying a typed object (rather than a free-form dict) also lets the
orchestrator release the heavy raw-input slots once the model is built
(see ``release_raw_inputs``) to lower peak memory on large apps, and
makes the per-stage inputs/outputs obvious when reading ``converter.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields as _dc_fields
from typing import Any, Dict, Iterator, List, Optional

# The canonical set of IR slots, in the order the parser populates them.
# Kept as a module constant so ``QlikIR`` and any mapping-style helper
# agree on exactly which keys exist.
IR_SLOTS = (
    "app", "script", "dimensions", "measures", "variables", "bookmarks",
    "sheets", "master_objects", "load_model", "app_props", "engine_schema",
    "fields", "field_renames", "script_blocks", "db_sources",
    "evaluated", "qlik_theme",
)


@dataclass
class QlikIR:
    """Parsed Qlik app, the single hand-off object between pipeline stages.

    Every field maps 1:1 to a key the old IR dict carried. Defaults match
    the parser's defaults so a partially-built IR (or a hand-rolled one in
    a test) behaves identically to the historical dict.
    """

    # ---- raw input documents (heavy; released after the model build) ---
    app: Dict[str, Any] = field(default_factory=dict)
    script: str = ""
    dimensions: List[Dict[str, Any]] = field(default_factory=list)
    measures: List[Dict[str, Any]] = field(default_factory=list)
    variables: List[Dict[str, Any]] = field(default_factory=list)
    bookmarks: List[Dict[str, Any]] = field(default_factory=list)
    sheets: List[Dict[str, Any]] = field(default_factory=list)
    master_objects: List[Dict[str, Any]] = field(default_factory=list)
    load_model: Optional[Dict[str, Any]] = None
    app_props: Optional[Dict[str, Any]] = None
    engine_schema: Optional[Dict[str, Any]] = None

    # ---- derived indexes the parser computes once ----------------------
    fields: List[str] = field(default_factory=list)
    field_renames: Dict[str, Dict[str, str]] = field(default_factory=dict)
    script_blocks: List[Dict[str, Any]] = field(default_factory=list)
    # {final_table: {connection, catalog, schema, source_table, sql_columns}}
    # for tables loaded from a SQL data connection (LIB CONNECT TO + SQL SELECT).
    db_sources: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Engine-evaluated text-expression snapshots from the unbuild's
    # evaluated-expressions.json sidecar:
    # {"objects": {objectId: {cId: text}}, "expressions": {expr: text}}.
    # Read by the report builder (textbox content / title resolution) --
    # NOT released by release_raw_inputs.
    evaluated: Dict[str, Any] = field(default_factory=dict)
    # The app's Qlik theme JSON (theme.json sidecar; cloud unbuild only).
    # Consumed by pbi_theme.build_report_theme before the model build.
    qlik_theme: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Mapping-style access so every historical ``ir.get(...)`` /
    # ``ir[...]`` / ``ir[...] = ...`` call site keeps working unchanged.
    # ------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default) if key in IR_SLOTS else default

    def __getitem__(self, key: str) -> Any:
        if key not in IR_SLOTS:
            raise KeyError(key)
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        if key not in IR_SLOTS:
            raise KeyError(key)
        setattr(self, key, value)

    def __contains__(self, key: str) -> bool:
        return key in IR_SLOTS

    def keys(self) -> Iterator[str]:  # pragma: no cover - convenience
        return iter(IR_SLOTS)

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "QlikIR":
        """Build a ``QlikIR`` from a plain dict (back-compat path).

        Unknown keys are ignored; missing keys take the dataclass
        defaults. Lets callers that still hand the pipeline a raw dict
        (older tests, the qvf-direct path) flow through unchanged.
        """
        if isinstance(d, cls):
            return d
        known = {f.name for f in _dc_fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    # ------------------------------------------------------------------
    def release_raw_inputs(self) -> None:
        """Drop the large raw-input documents the model build consumed but
        nothing downstream reads again, to lower peak memory on large apps.

        Released here are exactly the slots with NO reader after
        ``SemanticModel.build()``:

        * ``app`` -- read once in ``SemanticModel.__init__`` (captured as
          ``app_title``).
        * ``script`` -- consumed only by the parser.
        * ``load_model`` / ``engine_schema`` / ``fields`` -- read only in
          ``build()`` (table/column synthesis).
        * ``app_props`` -- not read by any downstream stage.

        Deliberately PRESERVED (still have live readers):
        * ``sheets`` / ``master_objects`` / ``dimensions`` / ``variables``
          -- the report builder reads them in ``build_pages()``.
        * ``script_blocks`` -- ``model._script_partition_m`` reads it in
          ``write_tmdl`` (writer) and the conversion report enumerates it.
        * ``bookmarks`` -- the writer emits PBI bookmark scaffolds and the
          conversion report lists them.
        * ``field_renames`` -- consumed during the model build but cheap;
          left for symmetry.

        Correctness-neutral: only references nothing touches again are
        dropped. Call it AFTER the model is built and BEFORE the (heavier)
        report build + write, so the report build runs with a smaller
        resident set.
        """
        self.app = {}
        self.script = ""
        self.load_model = None
        self.app_props = None
        self.engine_schema = None
        self.fields = []
