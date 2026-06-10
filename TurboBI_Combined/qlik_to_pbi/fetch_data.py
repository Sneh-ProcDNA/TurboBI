"""Fetch per-object data from a Qlik Cloud app via the `qlik` CLI.

Qlik does NOT expose per-table data via a single API call — its data is
script-loaded into the engine and exposed through *objects* (sheets,
master visualisations, charts). To get tabular data out, you export
each data-bearing object as CSV.

This helper:

  1. Walks the local `qlik app unbuild` output to enumerate objects
     that carry a ``qHyperCubeDef`` (master objects, plus any
     chart/table/pivot-table cell inside a sheet).
  2. For each one, shells out to ``qlik app object export <id> ...``
     and drops a CSV into ``--output`` (default: ``./csv_exports``).
  3. Returns the list of files written so the caller can hand them
     straight to ``--data-dir``.

The CSV file names are derived from each object's title (or its
parent sheet + visualization type when no title is set). When you
have multiple objects per source table, manually rename the ones
that match your `loadmodel` table names so the converter picks them
up via :mod:`qlik_to_pbi.csv_schema.match_csv_for_table`.
"""

from __future__ import annotations

import gc
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._logging import get_logger
from .utils import resolve_qlik_command, safe_filename

_log = get_logger("FETCH")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_object_data(
    app_id: str,
    qlik_input_dir: Path,
    output_dir: Path,
    object_types: Optional[List[str]] = None,
    qlik_command: str = "qlik",
    emit_format: str = "parquet",
) -> List[Path]:
    """Export every data-bearing Qlik object to Parquet (or CSV).

    ``emit_format`` -- ``"parquet"`` (default) or ``"csv"``. Parquet
    carries a typed schema, so the bound partition needs no sniff/cast
    (see docs/large-data-strategy.md). Falls back to CSV with a warning
    when ``pyarrow`` isn't installed.

    Parameters
    ----------
    app_id
        Qlik app id (the same value passed to ``qlik app unbuild``).
    qlik_input_dir
        The directory produced by ``qlik app unbuild`` (we read its
        ``objects/`` subfolder to enumerate hypercubes locally; that
        way we skip a round trip to the engine for the listing step).
    output_dir
        Directory to write CSV files into. Created if missing.
    object_types
        Optional allow-list. Default keeps the object kinds that
        actually carry a hypercube: ``masterobject``, ``table``,
        ``pivot-table``, ``sn-pivot-table``, ``auto-chart``,
        ``barchart``, ``linechart``, ``piechart``, ``treemap``,
        ``scatterplot``, ``combochart``, ``kpi``, ``sn-kpi``.
    qlik_command
        The CLI binary to invoke. Default ``"qlik"``. Override when
        the binary lives at a non-PATH location.

    Returns
    -------
    list of Path
        Successfully written CSV files. Failures are logged and
        skipped.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Resolve emit format. Parquet needs pyarrow; degrade to CSV (never
    # fail the fetch) when it's missing so the converter still produces
    # a bindable artifact.
    fmt = (emit_format or "csv").lower()
    if fmt == "parquet":
        from .parquet_io import PYARROW_AVAILABLE
        if not PYARROW_AVAILABLE:
            _log.warning(
                "--parquet requested but pyarrow is not installed; "
                "falling back to CSV. `pip install pyarrow` to enable Parquet."
            )
            fmt = "csv"
    ext = "parquet" if fmt == "parquet" else "csv"

    # Resolve the binary up-front so failures point at the right cause
    # instead of erroring once per object below. The shared resolver
    # checks PATH plus common install locations like ~/qlik.exe and
    # %LOCALAPPDATA%/Programs/qlik-cli/qlik.exe.
    resolved = resolve_qlik_command(qlik_command)
    if not resolved:
        _log.warning(
            f"Could not locate the qlik CLI ('{qlik_command}'). "
            f"Skipping data export. Install from "
            f"https://qlik.dev/toolkits/qlik-cli, authenticate with "
            f"`qlik context use <name>`, then pass --qlik-cmd <path> "
            f"if the binary is not on PATH."
        )
        return []
    _log.info(f"Using qlik CLI at: {resolved}")

    candidates = enumerate_data_objects(qlik_input_dir, object_types)
    _log.info(f"Found {len(candidates)} data-bearing objects to export.")

    written: List[Path] = []
    used_names: set[str] = set()
    for obj in candidates:
        # File name candidate priority:
        #   1. The object's qMetaDef.title.
        #   2. Sheet title + visualization type, if a child of a sheet.
        #   3. The object id (always unique, never pretty).
        base = obj.get("display_name") or obj["id"]
        fname = safe_filename(base, max_len=60).strip(" _")
        candidate = fname or obj["id"]
        suffix = 2
        while candidate.lower() in used_names:
            candidate = f"{fname}_{suffix}"
            suffix += 1
        used_names.add(candidate.lower())

        out_file = output_dir / f"{candidate}.{ext}"
        if _export_object_to_csv(app_id, obj["id"], out_file, resolved, emit_format=fmt):
            written.append(out_file)

    _log.info(f"Exported {len(written)}/{len(candidates)} object(s) to {output_dir}.")
    return written


# ---------------------------------------------------------------------------
# Object enumeration (walks unbuilt JSON locally)
# ---------------------------------------------------------------------------

DEFAULT_DATA_OBJECT_TYPES: Tuple[str, ...] = (
    "masterobject",
    "table",
    "pivot-table",
    "sn-pivot-table",
    "auto-chart",
    "barchart",
    "linechart",
    "piechart",
    "treemap",
    "scatterplot",
    "combochart",
    "kpi",
    "sn-kpi",
    "histogram",
    "boxplot",
    "waterfallchart",
)


def enumerate_data_objects(
    qlik_input_dir: Path,
    object_types: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """List every Qlik object that exposes a hypercube.

    Returns a list of ``{id, type, display_name, parent_sheet}`` dicts.
    We walk the local unbuild output, not the live engine, so this
    needs no network access.
    """
    allowed = set(object_types) if object_types else set(DEFAULT_DATA_OBJECT_TYPES)
    objects_dir = qlik_input_dir / "objects"
    out: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    if not objects_dir.is_dir():
        _log.warning(f"No objects/ folder at {qlik_input_dir}; nothing to export.")
        return out

    for fp in sorted(objects_dir.glob("*.json")):
        # Skip non-data top-level files.
        name = fp.name.lower()
        if name.startswith(("loadmodel", "appprops")):
            continue

        try:
            with open(fp, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning(f"Could not read {fp.name}: {exc}")
            continue

        if name.startswith("sheet"):
            # Cells in a sheet may be data-bearing charts.
            sheet_prop = payload.get("qProperty", {}) or {}
            sheet_meta = sheet_prop.get("qMetaDef", {}) or {}
            sheet_title = sheet_meta.get("title", "") or fp.stem
            for cell in sheet_prop.get("cells", []) or []:
                ctype = (cell.get("type") or "").lower()
                if ctype not in allowed:
                    continue
                cell_id = cell.get("name", "")
                if not cell_id or cell_id in seen_ids:
                    continue
                # Pull the child's properties so we can grab its title.
                child_props = _find_child_props(payload, cell_id)
                viz_name = (child_props.get("title", "")
                            or _pretty_type(ctype))
                display = f"{sheet_title} - {viz_name}" if viz_name else sheet_title
                seen_ids.add(cell_id)
                out.append({
                    "id":            cell_id,
                    "type":          ctype,
                    "display_name":  display,
                    "parent_sheet":  sheet_title,
                })
        else:
            # Top-level object file (masterobject, etc.).
            qtype = ((payload.get("qInfo") or {}).get("qType") or "").lower()
            if qtype not in allowed:
                continue
            qid = (payload.get("qInfo") or {}).get("qId", "")
            if not qid or qid in seen_ids:
                continue
            meta = payload.get("qMetaDef") or {}
            title = (meta.get("title") or fp.stem)
            seen_ids.add(qid)
            out.append({
                "id":            qid,
                "type":          qtype,
                "display_name":  title,
                "parent_sheet":  None,
            })

    return out


def _find_child_props(sheet_payload: Dict[str, Any], child_id: str) -> Dict[str, Any]:
    """Find the qProperty bag for a child object inside a sheet payload."""
    for child in sheet_payload.get("qChildren", []) or []:
        c_prop = child.get("qProperty", {}) or {}
        if (c_prop.get("qInfo", {}) or {}).get("qId") == child_id:
            return c_prop
    return {}


def _pretty_type(ctype: str) -> str:
    return ctype.replace("-", " ").title()


# ---------------------------------------------------------------------------
# CLI shell-out
# ---------------------------------------------------------------------------

import csv as _csv


def _export_object_to_csv(
    app_id: str,
    object_id: str,
    out_file: Path,
    qlik_command: str = "qlik",
    emit_format: str = "parquet",
) -> bool:
    """Shell out to ``qlik app object layout`` and write the hypercube
    to ``out_file`` as a real comma-separated CSV (or Parquet when
    ``emit_format='parquet'``).

    The ``layout`` subcommand returns the full ``qHyperCube`` JSON
    (dimensions + measures + the initial data page). We parse that
    JSON, flatten the matrix to rows, and write a header + rows CSV.

    Why not the more obvious ``qlik app object data``? That subcommand
    prints space-padded fixed-width text designed for terminal viewing,
    which is unparseable when the data itself contains spaces (e.g.
    ``"CLEVELAND CLINIC"`` becomes ambiguous against the column
    boundary). ``layout`` is the only stable path back to row-level
    data through the public CLI.

    Returns True iff a non-empty CSV was written.

    Caveat: ``layout`` only includes the object's *initial data fetch*
    page -- usually 500-1000 rows depending on the chart's
    ``qInitialDataFetch`` shape. Charts with more rows than that get
    truncated. Fetching the rest requires the WebSocket
    ``GetHyperCubeData`` call which the CLI doesn't expose; the
    workaround is to bump the source object's initial fetch in Qlik
    Sense before unbuild, or to re-fetch via REST API directly.
    """
    cmd = [
        qlik_command, "app", "object", "layout", object_id,
        "--app", app_id, "--json",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            shell=False,
            timeout=120,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning(f"  {object_id} -> FAILED ({exc})")
        return False

    if proc.returncode not in (0, 255):
        # Exit 255 is what this CLI returns even on success (the JSON
        # is on stdout regardless). Anything else is a real failure.
        _log.warning(
            f"  {object_id} -> FAILED (exit {proc.returncode}: "
            f"{(proc.stderr or '').strip()[:200]})"
        )
        return False

    raw = proc.stdout or ""
    if not raw.strip():
        _log.warning(f"  {object_id} -> FAILED (empty stdout)")
        return False

    # Drop our reference to the CompletedProcess (and therefore to its
    # `.stdout` / `.stderr` buffers) once we've taken what we need.
    # Otherwise the subprocess output sits in memory for the entire
    # duration of the JSON parse + CSV write below -- doubling peak
    # RSS for large hypercube layouts.
    proc = None

    try:
        layout = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning(f"  {object_id} -> FAILED (not JSON: {exc})")
        return False
    # Free the raw JSON string immediately after parsing. The parsed
    # `layout` dict alone is large enough; we don't need the source
    # text alive in parallel.
    raw = None

    if (emit_format or "csv").lower() == "parquet":
        rows_written = _write_hypercube_parquet(layout, out_file)
    else:
        rows_written = _write_hypercube_csv(layout, out_file)
    # Release the layout dict before logging / returning so the caller
    # loop starts the next object with a clean allocator footprint.
    layout = None
    gc.collect()
    if rows_written <= 0:
        _log.warning(f"  {object_id} -> FAILED (no rows in hypercube)")
        return False
    _log.info(
        f"  {object_id} -> {out_file.name} "
        f"({out_file.stat().st_size} bytes, {rows_written} rows)"
    )
    return True


def _write_hypercube_csv(layout: Dict[str, Any], out_file: Path) -> int:
    """Flatten a ``qHyperCube`` JSON document into a CSV file.

    Column order: dimensions first (in declaration order), then
    measures. Cell values come from ``qText`` (the display value) for
    dimensions and from ``qNum`` for measures when ``qNum`` is a
    finite number, falling back to ``qText`` otherwise (covers
    string-aggregation measures and the ``NaN`` sentinel).

    Returns the row count actually written. Zero means the hypercube
    had no data pages.
    """
    hc = layout.get("qHyperCube") or {}
    dims = hc.get("qDimensionInfo") or []
    meas = hc.get("qMeasureInfo") or []
    # `pop` rather than `get` so the source dict no longer holds the
    # pages list -- when the caller drops its reference to `layout`,
    # only the matrices currently being iterated stay alive instead of
    # the whole tree.
    pages = hc.pop("qDataPages", None) or []

    headers = (
        [d.get("qFallbackTitle") or f"dim_{i}" for i, d in enumerate(dims)]
        + [m.get("qFallbackTitle") or f"meas_{i}" for i, m in enumerate(meas)]
    )
    if not headers:
        return 0

    n_dims = len(dims)
    n_total = len(headers)
    # Per-dimension "render as a raw number" flags, from each field's
    # Qlik tags / number-format (see _dim_is_numeric). Numeric dimensions
    # are written from qNum so they don't pick up display formatting that
    # would make the CSV sniffer mis-type them as text.
    dim_numeric = [_dim_is_numeric(d) for d in dims]

    rows = 0
    with open(out_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = _csv.writer(f)
        writer.writerow(headers)
        while pages:
            # `pop(0)` keeps the index stable and lets the just-processed
            # page get reclaimed before we move on to the next one. For
            # large layouts this is the difference between holding 1
            # page's worth of cell-dicts and all of them at once.
            page = pages.pop(0)
            matrix = page.pop("qMatrix", None) or []
            page.clear()
            while matrix:
                matrix_row = matrix.pop(0)
                csv_row = []
                for ci, cell in enumerate(matrix_row[:n_total]):
                    if ci >= n_dims:
                        csv_row.append(_csv_cell_value(cell, is_measure=True))
                    else:
                        csv_row.append(_csv_cell_value(
                            cell, is_measure=False, numeric_dim=dim_numeric[ci]))
                writer.writerow(csv_row)
                rows += 1
    return rows


def _dim_kind(dim_info: Dict[str, Any]) -> str:
    """Declared Parquet column kind for a hypercube DIMENSION, from its
    Qlik metadata: ``$timestamp``/``$date`` -> datetime/date,
    numeric (``_dim_is_numeric``) -> double, else string. Numeric dims
    are emitted from ``qNum`` (a Qlik date serial for dates), so the
    Parquet column gets the real type instead of formatted text."""
    tags = {(t or "").lower() for t in (dim_info.get("qTags") or [])}
    if "$timestamp" in tags:
        return "datetime"
    if "$date" in tags:
        return "date"
    if _dim_is_numeric(dim_info):
        return "double"
    return "string"


def _write_hypercube_parquet(layout: Dict[str, Any], out_file: Path) -> int:
    """Flatten a ``qHyperCube`` to a typed Parquet file (streaming, one
    row group per data page). Column types come from the Qlik dimension/
    measure metadata, so the bound partition needs no sniff and no cast.
    Dimensions use the same ``qNum``-for-numeric rule as the CSV path
    (``_dim_is_numeric``); measures are numeric. Returns rows written."""
    from .parquet_io import ParquetStreamWriter

    hc = layout.get("qHyperCube") or {}
    dims = hc.get("qDimensionInfo") or []
    meas = hc.get("qMeasureInfo") or []
    pages = hc.pop("qDataPages", None) or []

    headers = (
        [d.get("qFallbackTitle") or f"dim_{i}" for i, d in enumerate(dims)]
        + [m.get("qFallbackTitle") or f"meas_{i}" for i, m in enumerate(meas)]
    )
    if not headers:
        return 0
    n_dims = len(dims)
    n_total = len(headers)
    # Declared kind per column: dims from metadata, measures always double.
    dim_kinds = [_dim_kind(d) for d in dims]
    fields = (
        [(headers[i], dim_kinds[i]) for i in range(n_dims)]
        + [(headers[n_dims + j], "double") for j in range(len(meas))]
    )
    # Emit the raw ``qNum`` (not formatted ``qText``) for any dimension
    # whose declared kind is numeric OR temporal: a date/timestamp dim's
    # ``qNum`` is the Qlik date serial, which the writer converts back to
    # a datetime (locale-formatted qText like "1/15/2024" would not
    # parse). Plain ``string`` dims keep qText.
    dim_as_number = [k in ("double", "date", "datetime") for k in dim_kinds]

    rows = 0
    with ParquetStreamWriter(out_file, fields) as pw:
        while pages:
            page = pages.pop(0)
            matrix = page.pop("qMatrix", None) or []
            page.clear()
            page_rows: List[List[str]] = []
            for matrix_row in matrix:
                out_row = []
                for ci, cell in enumerate(matrix_row[:n_total]):
                    if ci >= n_dims:
                        out_row.append(_csv_cell_value(cell, is_measure=True))
                    else:
                        out_row.append(_csv_cell_value(
                            cell, is_measure=False, numeric_dim=dim_as_number[ci]))
                page_rows.append(out_row)
            matrix.clear()
            pw.write_page(page_rows)
            rows += len(page_rows)
    return rows


def _csv_cell_value(cell: Any, is_measure: bool, numeric_dim: bool = False) -> str:
    """Pick a CSV-safe value from a Qlik matrix cell. See the matching
    docstring on :func:`qlik_to_pbi.engine_fetch._cell_value` for the
    full ruleset -- duplicated here so this module stays independent of
    the engine-fetch path.

    ``numeric_dim`` -- the column is a Qlik dimension the engine tags as
    numeric (``$numeric`` / ``$integer`` and not a date/text; see
    :func:`_dim_is_numeric`). Such a cell is emitted from the raw
    ``qNum`` (e.g. ``1234.5``), NOT the formatted ``qText`` (which may
    carry thousands separators or a currency symbol like ``1,234.50`` /
    ``$1,234``). Writing the formatted text made the CSV sniffer type the
    column ``string`` -- so a field that is numeric in Qlik loaded as
    text in PBI (can't aggregate, mismatches the source) and broke any
    ``Table.TransformColumnTypes`` numeric cast. Measures already use
    ``qNum``; this extends the same treatment to numeric dimensions.
    Dates and genuine text dimensions keep ``qText``."""
    if not isinstance(cell, dict):
        return ""
    if cell.get("qIsNull") is True:
        return ""
    elem = cell.get("qElemNumber")
    if isinstance(elem, int) and elem == -2:
        return ""
    qtext = cell.get("qText")
    state = cell.get("qState")
    if state == "X" and (qtext in (None, "", "-")):
        return ""
    qnum = cell.get("qNum")
    want_number = is_measure or numeric_dim
    # ``qnum == qnum`` is the finite/NaN guard (NaN != NaN).
    if want_number and isinstance(qnum, (int, float)) and qnum == qnum:
        as_int = int(qnum)
        if as_int == qnum:
            return str(as_int)
        return str(qnum)
    if numeric_dim:
        # Numeric dimension cell with no finite qNum (blank / null-ish):
        # emit empty rather than a formatted-text fallback, so the column
        # stays uniformly numeric and castable instead of being demoted
        # to text by one stray value.
        return ""
    if qtext == "-" and (qnum is None or qnum == "NaN"):
        return ""
    return qtext or ""


def _dim_is_numeric(dim_info: Dict[str, Any]) -> bool:
    """True when a hypercube dimension should be emitted as a raw number.

    Decided from the dimension's Qlik metadata (``qDimensionInfo``):
      * ``qTags`` containing ``$date`` / ``$timestamp`` / ``$text`` /
        ``$ascii``  -> NOT numeric (dates keep their formatted text so
        the CSV sniffer detects a date; text stays text, preserving
        e.g. zero-padded codes).
      * ``qTags`` containing ``$numeric`` / ``$integer``  -> numeric.
      * else fall back to the number-format type code
        ``qNumFormat.qType`` in {I, R, F, M} (integer / real / fixed /
        money) -> numeric.
      * everything else (general ``U``, intervals, untagged)  -> not
        numeric (keep ``qText``; the sniffer still detects clean numbers).

    Conservative on purpose: only fields Qlik itself marks numeric are
    rendered from ``qNum``, so we never strip meaning from a text code
    that merely looks numeric in the sample."""
    tags = {(t or "").lower() for t in (dim_info.get("qTags") or [])}
    if tags & {"$date", "$timestamp", "$text", "$ascii"}:
        return False
    if tags & {"$numeric", "$integer"}:
        return True
    qtype = ((dim_info.get("qNumFormat") or {}).get("qType") or "").upper()
    return qtype in ("I", "R", "F", "M")
