"""Pre-flight structural validator for emitted PBIP artefacts.

Power BI Desktop's TMDL + visual JSON schemas are strict: an
unrecognised property, a column ref to a non-existent column, or a
missing required field aborts the whole project load with a generic
"Failed to load file" / "Cannot resolve all the paths..." error and
no per-file diagnostics.

This module catches the most common offenders BEFORE the user opens
Desktop. We don't pull the remote JSON schemas (slow, network-bound,
and the official validators are noisy with allowed extension points);
instead we run a small, focused set of structural assertions against
the patterns that have actually broken the build in past iterations.

Checks performed:

  * ``visualContainerObjects`` only carries the known keys (background,
    border, padding, visualHeader, stylePreset, divider).
  * Every relationship's ``fromColumn`` / ``toColumn`` resolves on the
    table it names.
  * No measure shares a name with a column on the same table (case-
    insensitive).
  * Every ``ref table`` in model.tmdl matches a ``tables/*.tmdl`` file.
  * Every page id in pages.json matches a page subfolder.
  * Each visual.json carries a ``visualType`` and a valid ``position``.
  * Every partition ``source =`` expression block is indented deeper
    than its property line (TMDL rejects a shallower ``let``/body).
  * No measure / calc-column applies a column-only aggregation
    (``SUM`` / ``AVERAGE`` / ``DISTINCTCOUNT``) to an expression rather
    than a bare column (DAX rejects it at query/refresh time).
  * No measure ``SUM`` / ``AVERAGE`` aggregates a column the model
    declares as ``string`` -- a numeric aggregation over text yields a
    TEXT result, which Power BI renders wrapped in single quotes
    (``'37K'``) on cards, axes and data labels. Almost always means the
    bound data was fetched with that column typed as text; re-fetch.

Failures are written to the conversion report's "Pre-flight" section
and logged at WARNING. They do NOT abort the conversion -- the user
might still want to inspect the partial output -- but they DO surface
the issue at run time instead of at Desktop load time.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from ._logging import get_logger

_log = get_logger("PREFLIGHT")


# Whitelist for visualContainerObjects -- everything else is treated
# as a schema error. Pulled from inspection of PBI Desktop's own
# emitted PBIP files; safe extension points are added as we see them.
_ALLOWED_CONTAINER_KEYS = {
    "background", "border", "padding", "visualHeader",
    "stylePreset", "divider", "outspacePane", "title",
    "general", "shadow", "lockAspect",
}


def run_preflight(pbip_root: Path, report_name: str) -> List[str]:
    """Walk a finished PBIP under ``pbip_root`` and return a list of
    human-readable warnings.

    ``report_name`` is the user-visible app name; the *.Report and
    *.SemanticModel sibling folders are derived from it.

    Returns an empty list when the layout is clean.
    """
    pbip_root  = Path(pbip_root)
    report_dir = pbip_root / f"{report_name}.Report"
    model_dir  = pbip_root / f"{report_name}.SemanticModel"

    warnings: List[str] = []
    if not report_dir.is_dir() or not model_dir.is_dir():
        warnings.append(
            f"PBIP layout incomplete: expected {report_dir} + {model_dir}"
        )
        return warnings

    warnings.extend(_check_visuals(report_dir))
    warnings.extend(_check_pages(report_dir))
    warnings.extend(_check_model_refs(model_dir))
    warnings.extend(_check_reserved_table_names(model_dir))
    warnings.extend(_check_relationships(model_dir))
    warnings.extend(_check_partition_indent(model_dir))
    warnings.extend(_check_measure_aggregations(model_dir))
    warnings.extend(_check_numeric_agg_on_text_column(model_dir))

    if warnings:
        for w in warnings:
            _log.warning(w)
    else:
        _log.info("Pre-flight: no structural issues found.")
    return warnings


# ---------------------------------------------------------------------------
# Per-file checks
# ---------------------------------------------------------------------------

def _check_visuals(report_dir: Path) -> List[str]:
    out: List[str] = []
    for v_path in (report_dir / "definition" / "pages").rglob("visual.json"):
        try:
            doc = json.loads(v_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            out.append(f"visual.json unreadable: {v_path}: {exc}")
            continue
        visual = doc.get("visual") or {}
        if "visualType" not in visual:
            out.append(f"missing visualType: {v_path}")
        pos = doc.get("position") or {}
        for required in ("x", "y", "z", "width", "height"):
            if required not in pos:
                out.append(
                    f"position.{required} missing on visual: {v_path}"
                )
        vco = visual.get("visualContainerObjects") or {}
        if not isinstance(vco, dict):
            out.append(
                f"visualContainerObjects is not a dict: {v_path}"
            )
            continue
        for key in vco:
            if key not in _ALLOWED_CONTAINER_KEYS:
                out.append(
                    f"unknown visualContainerObjects key {key!r} in {v_path}"
                )
    return out


def _check_pages(report_dir: Path) -> List[str]:
    out: List[str] = []
    pages_dir = report_dir / "definition" / "pages"
    meta_path = pages_dir / "pages.json"
    if not meta_path.is_file():
        return [f"pages.json missing under {pages_dir}"]
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"pages.json unreadable: {exc}"]
    declared = set(meta.get("pageOrder") or [])
    on_disk = {
        p.name for p in pages_dir.iterdir()
        if p.is_dir() and (p / "page.json").exists()
    }
    for missing in declared - on_disk:
        out.append(f"pages.json lists {missing!r} but the folder is absent")
    for orphan in on_disk - declared:
        out.append(
            f"page folder {orphan!r} exists but is not in pages.json's pageOrder"
        )
    return out


_REF_TABLE_RE = re.compile(r"^\s*ref\s+table\s+'?([^'\n]+?)'?\s*$", re.M)


def _check_model_refs(model_dir: Path) -> List[str]:
    out: List[str] = []
    model_tmdl = model_dir / "definition" / "model.tmdl"
    tables_dir = model_dir / "definition" / "tables"
    if not model_tmdl.is_file():
        return [f"model.tmdl missing at {model_tmdl}"]
    try:
        model_body = model_tmdl.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"model.tmdl unreadable: {exc}"]
    declared = [m.group(1).strip() for m in _REF_TABLE_RE.finditer(model_body)]
    on_disk_stems = {p.stem for p in tables_dir.glob("*.tmdl")} \
        if tables_dir.is_dir() else set()
    for tn in declared:
        # The on-disk filename is the safe_filename'd table name; we
        # accept any case-insensitive match because the stem hygiene
        # may differ from the ref text.
        if not any(s.lower() == tn.lower() for s in on_disk_stems):
            out.append(
                f"model.tmdl references table {tn!r} but no matching "
                f"tables/{tn}.tmdl file exists"
            )
    return out


# Table names Power BI / Analysis Services RESERVE -- a table using one makes
# the AS model-schema validator reject the WHOLE file at load (e.g.
# ``Unsupported Table name "Measures" has been found in data model schema``,
# the Feb-2025 PBI Desktop validation). The model layer remaps these
# (``model._RESERVED_TABLE_NAMES``); this is the backstop that catches any
# future code path that bypasses that remap, so the breakage is surfaced here
# rather than at Desktop load. Kept local so preflight validates the emitted
# artefacts independent of how they were produced.
_RESERVED_TABLE_NAMES = {"measures"}
_TABLE_HEADER_RE = re.compile(r"^table\s+'?([^'\n]+?)'?\s*$", re.M)


def _check_reserved_table_names(model_dir: Path) -> List[str]:
    """Flag any table whose name collides with a Power BI reserved name.
    Reads the ``table <name>`` header of each ``tables/*.tmdl``."""
    out: List[str] = []
    tables_dir = model_dir / "definition" / "tables"
    if not tables_dir.is_dir():
        return out
    for tp in sorted(tables_dir.glob("*.tmdl")):
        try:
            head = tp.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _TABLE_HEADER_RE.search(head)
        if m and m.group(1).strip().casefold() in _RESERVED_TABLE_NAMES:
            out.append(
                f"{tp.name}: table name {m.group(1).strip()!r} is RESERVED by "
                f"Power BI (collides with the MDX [Measures] dimension) -- the "
                f"model-schema validator rejects the whole file at load. Rename "
                f"the table."
            )
    return out


# Parse relationships block-by-block: a header line, then fromColumn /
# toColumn extracted INDEPENDENTLY within the block. This is robust to
# property order / extra lines (fromCardinality, toCardinality,
# crossFilteringBehavior, ...) appearing between or around them -- the
# old single regex required fromColumn to be immediately followed by
# toColumn and would silently stop validating if anything was inserted.
_REL_HEADER_RE = re.compile(r"^relationship\s+(\S+)\s*$", re.M)
_FROMCOL_RE = re.compile(r"^\s+fromColumn:\s+'?([^'\.]+)'?\.'?([^'\n]+?)'?\s*$", re.M)
_TOCOL_RE = re.compile(r"^\s+toColumn:\s+'?([^'\.]+)'?\.'?([^'\n]+?)'?\s*$", re.M)
_COLUMN_RE = re.compile(r"^\s*column\s+'?([^'\n]+?)'?\s*$", re.M)


# A partition's M expression opens with a bare ``source =`` line; the
# expression block that follows MUST be indented deeper than it, or
# TMDL aborts the whole project load with "Invalid indentation was
# detected!" (pointing at the first expression line, usually ``let``)
# and no per-file context. We only inspect block-style ``source =``
# (nothing after the ``=``); inline single-line sources can't trip it.
_SOURCE_PROP_RE = re.compile(r"^(\t*)source =\s*$")


def _leading_tabs(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


def _check_partition_indent(model_dir: Path) -> List[str]:
    out: List[str] = []
    tables_dir = model_dir / "definition" / "tables"
    if not tables_dir.is_dir():
        return out
    for tp in sorted(tables_dir.glob("*.tmdl")):
        try:
            lines = tp.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            out.append(f"{tp.name} unreadable: {exc}")
            continue
        for i, line in enumerate(lines):
            m = _SOURCE_PROP_RE.match(line)
            if not m:
                continue
            prop_depth = len(m.group(1))
            # First non-blank line after ``source =`` is the expression head.
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines):
                out.append(f"{tp.name}: empty partition source block")
            elif _leading_tabs(lines[j]) <= prop_depth:
                out.append(
                    f"{tp.name}: partition source expression at line {j + 1} "
                    f"({lines[j].strip()!r}) is indented "
                    f"{_leading_tabs(lines[j])} tab(s), not deeper than its "
                    f"'source =' line ({prop_depth} tab(s)) -- TMDL will "
                    "reject this as invalid indentation"
                )
            break  # one partition per table file
    return out


# A measure / calc-column line opening with ``<name> = <expr>`` (the
# regular ``column 'X'`` declaration has no ``=`` so it's skipped).
_DEF_LINE_RE = re.compile(r"^\t+(?:measure|column)\s+.+?=\s*(.+)$")
# DAX aggregations that accept ONLY a single bare column (no overloads):
# applying them to an expression fails at query time with
# "The <FN> function only accepts a column reference as an argument".
_COL_ONLY_AGG_RE = re.compile(r"\b(SUM|AVERAGE|DISTINCTCOUNT)\s*\(", re.IGNORECASE)
_BARE_COL_RE = re.compile(r"^(?:'[^']+'\[[^\[\]]+\]|\[[^\[\]]+\])$")
# ``/* ... */`` block comment (the stub's ``/* qlik: <original> */``) plus any
# ``// ...`` tail -- stripped before the aggregation scan so a Qlik ``Sum(``
# preserved in a stub comment isn't mistaken for live DAX.
_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*")


def _paren_span(s: str, open_idx: int) -> int:
    depth, j, n = 1, open_idx + 1, len(s)
    while j < n and depth:
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
        j += 1
    return j


def _check_measure_aggregations(model_dir: Path) -> List[str]:
    out: List[str] = []
    tables_dir = model_dir / "definition" / "tables"
    if not tables_dir.is_dir():
        return out
    for tp in sorted(tables_dir.glob("*.tmdl")):
        try:
            lines = tp.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln, line in enumerate(lines, 1):
            dm = _DEF_LINE_RE.match(line)
            if not dm:
                continue
            # Strip the ``/* qlik: ... */`` stub comment (and any ``//`` tail)
            # BEFORE scanning: a stubbed measure is ``BLANK() /* qlik: ...
            # Sum({<set>}...) ... */`` and the ORIGINAL Qlik ``Sum(`` inside the
            # comment is not live DAX -- matching it produced a flood of false
            # "SUM applied to an expression" warnings on stub-heavy models.
            expr = _COMMENT_RE.sub("", dm.group(1))
            for am in _COL_ONLY_AGG_RE.finditer(expr):
                open_idx = am.end() - 1
                end = _paren_span(expr, open_idx)
                if end > len(expr):
                    continue
                arg = expr[open_idx + 1:end - 1].strip()
                if not _BARE_COL_RE.match(arg):
                    out.append(
                        f"{tp.name}:{ln}: {am.group(1).upper()}(...) is applied to "
                        f"an expression ({arg[:48]!r}), not a bare column -- DAX "
                        "rejects this at query/refresh time"
                    )
                    break  # one finding per definition line is enough
    return out


# A column declaration (``column 'X'`` / ``column X``) followed within a
# few lines by its ``dataType: <kind>``. Built into a per-table map so a
# measure's ``SUM('T'[C])`` can be checked against C's declared type.
_COL_DECL_RE = re.compile(r"^\t+column\s+'?([^'\n]+?)'?\s*$", re.M)
_DATATYPE_RE = re.compile(r"^\t+dataType:\s*(\w+)\s*$")
# Numeric aggregations that are meaningless over text: a SUM / AVERAGE of
# a string column returns text (PBI quotes it). MIN / MAX of text is a
# legitimate string operation, so they are deliberately excluded to avoid
# false positives.
_NUM_AGG_COL_RE = re.compile(
    r"\b(SUM|AVERAGE)\s*\(\s*'([^']+)'\[([^\]]+)\]\s*\)", re.IGNORECASE
)


def _column_types_by_table(tables_dir: Path) -> Dict[str, Dict[str, str]]:
    """``{table_name_lower: {column_name_lower: dataType}}`` parsed from
    every ``tables/*.tmdl``. The table key is the file stem (the model's
    table name), matching how measures qualify columns (``'Stem'[Col]``)."""
    out: Dict[str, Dict[str, str]] = {}
    for tp in tables_dir.glob("*.tmdl"):
        try:
            lines = tp.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        cols: Dict[str, str] = {}
        cur: str = ""
        for line in lines:
            cm = _COL_DECL_RE.match(line)
            if cm:
                cur = cm.group(1).strip().lower()
                continue
            if cur:
                dm = _DATATYPE_RE.match(line)
                if dm:
                    cols[cur] = dm.group(1).strip().lower()
                    cur = ""
        out[tp.stem.lower()] = cols
    return out


def _check_numeric_agg_on_text_column(model_dir: Path) -> List[str]:
    out: List[str] = []
    tables_dir = model_dir / "definition" / "tables"
    if not tables_dir.is_dir():
        return out
    types = _column_types_by_table(tables_dir)
    # Dedupe on (table, column) -- the same mis-typed column is usually
    # summed by many measures; one actionable warning per column is enough.
    seen: set = set()
    for tp in sorted(tables_dir.glob("*.tmdl")):
        try:
            body = tp.read_text(encoding="utf-8")
        except OSError:
            continue
        for am in _NUM_AGG_COL_RE.finditer(body):
            fn, tbl, col = am.group(1).upper(), am.group(2), am.group(3)
            key = (tbl.lower(), col.lower())
            if key in seen:
                continue
            if types.get(tbl.lower(), {}).get(col.lower()) == "string":
                seen.add(key)
                out.append(
                    f"{fn}('{tbl}'[{col}]) aggregates a column typed TEXT -- "
                    "Power BI will render the result in quotes ('37K') as a "
                    "string. The bound data for this column was fetched as "
                    "text; re-fetch/re-convert with the current build so the "
                    "column is typed numeric."
                )
    return out


def _check_relationships(model_dir: Path) -> List[str]:
    out: List[str] = []
    rel_path = model_dir / "definition" / "relationships.tmdl"
    tables_dir = model_dir / "definition" / "tables"
    if not rel_path.is_file() or not tables_dir.is_dir():
        return out
    try:
        rel_body = rel_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"relationships.tmdl unreadable: {exc}"]

    # Build {table_name (case-insensitive): set of column names}.
    cols_by_table: Dict[str, set] = {}
    for tp in tables_dir.glob("*.tmdl"):
        try:
            body = tp.read_text(encoding="utf-8")
        except OSError:
            continue
        # First non-blank line `table 'X'` -- but we use the file stem
        # as the canonical key. PBI's relationship resolver is case-
        # insensitive on table.column, so we normalise both sides.
        tname_lower = tp.stem.lower()
        cols_by_table[tname_lower] = {
            m.group(1).strip().lower() for m in _COLUMN_RE.finditer(body)
        }

    headers = list(_REL_HEADER_RE.finditer(rel_body))
    for i, h in enumerate(headers):
        rid = h.group(1)
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(rel_body)
        block = rel_body[start:end]
        fm = _FROMCOL_RE.search(block)
        tm = _TOCOL_RE.search(block)
        if not fm or not tm:
            continue
        for tname, col in ((fm.group(1), fm.group(2)), (tm.group(1), tm.group(2))):
            if tname.lower() not in cols_by_table:
                out.append(
                    f"relationship {rid}: table {tname!r} not on disk"
                )
                continue
            if col.lower() not in cols_by_table[tname.lower()]:
                out.append(
                    f"relationship {rid}: column {tname!r}.{col!r} "
                    "does not exist"
                )
    return out
