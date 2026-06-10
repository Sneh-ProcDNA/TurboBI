"""Which Qlik fields does the converted model actually use?

Column pruning (opt-in, ``--prune-columns``) extracts only the fields a
PBI model needs, so the engine fetch ships fewer cells per call (more
rows fit the 10k-cell GetHyperCubeData cap), smaller Parquet files, and
less VertiPaq memory. See ``docs/large-data-strategy.md``.

The hard requirement is **safety**: dropping a field the model references
(a relationship key, a measure's field, a displayed dimension, a calc
input) breaks the load. So this module computes a deliberately
CONSERVATIVE *keep* set -- a field is kept if its name appears as a token
in ANY expression / dimension / measure / variable / sheet-object
anywhere in the app. A field is eligible to drop only when its name is
referenced nowhere. Qlik always references a field by its name (bare or
``[bracketed]``), so "name appears nowhere" genuinely means "nothing can
use it". Over-keeping is harmless (just less pruning); under-keeping is
forbidden, and this rule cannot under-keep.

Cross-table keys are kept separately by the caller (a field shared by
>1 table is a join key), so this module only has to cover expression
usage.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Set

from ._logging import get_logger

_log = get_logger("PRUNE")

# A bracketed field ref ``[Field With Spaces]`` or a bare identifier.
_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
_BARE_IDENT_RE = re.compile(r"[A-Za-z_][\w.]*")

# IR dict keys whose STRING values are Qlik expressions worth scanning.
_EXPR_KEYS = {
    "qdef", "qdefinition", "qlabelexpression", "qexpression",
    "qlabel", "title", "expression", "definition",
}


def _tokens(text: str) -> Iterable[str]:
    """Yield lowercased field-name candidates from one expression string:
    every ``[bracketed]`` name (verbatim, spaces kept) plus every bare
    identifier. Over-inclusive on purpose -- keeping a non-field token is
    harmless, since it only ever ADDS to the keep set."""
    if not text:
        return
    for m in _BRACKET_RE.finditer(text):
        yield m.group(1).strip().lower()
    # Strip bracketed spans first so their inner words aren't re-split.
    stripped = _BRACKET_RE.sub(" ", text)
    for m in _BARE_IDENT_RE.finditer(stripped):
        yield m.group(0).strip().lower()


def _walk(obj: Any, out: Set[str]) -> None:
    """Recursively collect field tokens from an IR fragment.

    * ``qFieldDefs`` lists -> each entry is a literal field name.
    * Any key in ``_EXPR_KEYS`` with a string value -> tokenized.
    * Everything else is recursed into.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = (k or "").lower()
            if kl == "qfielddefs" and isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item.strip():
                        out.add(item.strip().lower())
                        # A qFieldDef can itself be an expression like
                        # ``=MonthName(Date)``; tokenize it too.
                        for t in _tokens(item):
                            out.add(t)
                continue
            if kl in _EXPR_KEYS and isinstance(v, str):
                for t in _tokens(v):
                    out.add(t)
                continue
            _walk(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk(item, out)


def collect_used_field_names(unbuild_dir: Path) -> Set[str]:
    """Return the lowercased set of field-name tokens referenced anywhere
    in the unbuilt app: master dimensions, master measures, variables,
    and every sheet / master-object hypercube. Empty set on parse failure
    (caller then keeps every column -- the safe fallback).

    Parsing reuses :func:`qlik_to_pbi.parser.parse_qlik_output` so the IR
    shapes match the rest of the converter exactly."""
    try:
        from .parser import parse_qlik_output
        ir = parse_qlik_output(unbuild_dir)
    except Exception as exc:  # noqa: BLE001 - never let usage scan abort a fetch
        _log.warning(f"  field-usage scan failed ({exc}); keeping all columns.")
        return set()

    used: Set[str] = set()
    for key in ("dimensions", "measures", "variables", "sheets",
                "master_objects"):
        _walk(ir.get(key) or [], used)
    used.discard("")
    return used


# A built-model DAX column reference: ``'Table'[Column]``.
_DAX_COLREF = re.compile(r"'([^']+)'\[([^\]]+)\]")


def collect_keep_fields(unbuild_dir: Path) -> Set[str]:
    """The full, authoritative set of source-field names to KEEP when
    pruning -- the safe input to ``engine_fetch._prune_table_fields``.

    Expression-token scanning alone (:func:`collect_used_field_names`)
    misses **join keys that are table-qualified** (e.g. ``HCO.HCO_ID`` on
    one table, ``HCP.HCO_ID`` on another): they share no name and appear
    in no measure/dimension expression, yet the model joins on them, so
    pruning them would dangle the relationship. To close that gap we also
    build the model (and report, to materialise inline measures / calc
    columns) and harvest its ACTUAL references, mapped back to each
    column's raw ``sourceColumn`` (the name the extract/Parquet uses):

      * every relationship endpoint column,
      * every column referenced by a measure's DAX,
      * every column a calculated-column expression depends on.

    Union with the expression tokens. On any failure we fall back to the
    expression-token set (still safe -- pruning's own cross-table-key and
    keep-all-fallback rules remain in force). All names lowercased."""
    keep = collect_used_field_names(unbuild_dir)
    try:
        from .parser import parse_qlik_output
        from .model import SemanticModel
        ir = parse_qlik_output(unbuild_dir)
        model = SemanticModel(ir)
        model.build()
        # Populate inline measures / expression calc-columns the report
        # synthesises, so their column refs are covered too.
        try:
            from .report import ReportBuilder
            ReportBuilder(ir, model).build_pages()
        except Exception as exc:  # noqa: BLE001 - report is best-effort here
            _log.debug(f"  keep-set: report build skipped ({exc})")

        srcmap: dict = {
            t["name"]: {c["name"]: c.get("sourceColumn", c["name"])
                        for c in t["columns"]}
            for t in model.tables
        }

        def _add(table: str, col: str) -> None:
            keep.add((col or "").lower())
            src = srcmap.get(table, {}).get(col)
            if src:
                keep.add(src.lower())

        for r in model.relationships:
            _add(r.get("fromTable", ""), r.get("fromColumn", ""))
            _add(r.get("toTable", ""), r.get("toColumn", ""))
        for meas in model.measures:
            for tbl, col in _DAX_COLREF.findall(meas.get("expression", "") or ""):
                _add(tbl, col)
        for t in model.tables:
            for c in t["columns"]:
                expr = c.get("expression")
                if expr:
                    for tbl, col in _DAX_COLREF.findall(expr):
                        _add(tbl, col)
    except Exception as exc:  # noqa: BLE001 - never let keep-set derivation abort a fetch
        _log.warning(
            f"  keep-set model harvest failed ({exc}); using expression "
            "tokens + cross-table keys only."
        )
    keep.discard("")
    return keep
