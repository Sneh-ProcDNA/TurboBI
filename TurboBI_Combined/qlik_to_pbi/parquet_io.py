"""Parquet emit + schema read for the large-data path (Phase 0).

Power BI has no QVD connector, and CSV forces a fragile sniff +
``Table.TransformColumnTypes`` cast that can fail at refresh. Parquet's
connector is GA (``Parquet.Document``) and the file carries a typed
schema, so a Parquet-backed partition is a single step with **no cast** —
the column types come straight from the file. See
``docs/large-data-strategy.md``.

This module is the typed-Parquet writer + schema reader. It is imported
lazily by the fetch / model paths so the rest of the converter keeps
working when ``pyarrow`` isn't installed; callers check
``PYARROW_AVAILABLE`` and fall back to CSV.

Two writers:

* :func:`write_parquet_columns` -- buffered, auto-detects each column's
  type from its values. For the cloud fetch (row-capped, small) and for
  any caller that already has the data column-wise.
* :class:`ParquetStreamWriter` -- streaming, declared schema, writes one
  Parquet row group per page. For the offline engine extract (whole
  tables, up to ~30M rows) where buffering every column is not an option.

:func:`sniff_parquet_schema` reads a ``.parquet`` file's schema into the
same column-descriptor shape ``csv_schema.sniff_csv_schema`` returns, so
``model._columns_for_table`` binds it the same way -- but with the types
already known, not guessed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._logging import get_logger

_log = get_logger("PARQUET")

try:  # pyarrow is an optional dependency (only the Parquet path needs it).
    import pyarrow as _pa
    import pyarrow.parquet as _pq
    PYARROW_AVAILABLE = True
except Exception:  # noqa: BLE001  -- ImportError or a broken install
    _pa = None
    _pq = None
    PYARROW_AVAILABLE = False


# Qlik (like Excel) numbers dates as days since 1899-12-30; the fractional
# part is the time of day. The offline engine extract emits a field's
# numeric ``qNum`` for date/timestamp fields, so a declared ``date`` /
# ``datetime`` column arrives here as that serial and we convert it back.
_QLIK_EPOCH = datetime(1899, 12, 30)


def qlik_serial_to_datetime(serial: float) -> Optional[datetime]:
    """Qlik/Excel date serial -> ``datetime``; ``None`` on bad input."""
    try:
        return _QLIK_EPOCH + timedelta(days=float(serial))
    except (TypeError, ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _build_array_auto(values: List[str]):
    """Build a typed Arrow array from a column of STRING cells, detecting
    int / double / bool / string. Dates are intentionally left as
    ``string`` in auto mode (text dates can't be parsed reliably without
    a per-field type; the engine path uses the declared-``date`` kind in
    :class:`ParquetStreamWriter` instead). Reuses the CSV kind detector
    so the classification matches the rest of the converter exactly."""
    from .csv_schema import _KindAccumulator

    acc = _KindAccumulator()
    for v in values:
        acc.feed(v)
    kind = acc.kind()
    if kind == "int":
        return _pa.array([_as_int(v) for v in values], type=_pa.int64())
    if kind == "double":
        return _pa.array([_as_float(v) for v in values], type=_pa.float64())
    if kind == "bool":
        return _pa.array([_as_bool(v) for v in values], type=_pa.bool_())
    # date / datetime / time / string -> string (no auto date promotion).
    return _pa.array([(v if (v is not None and v != "") else None) for v in values],
                     type=_pa.string())


def _resolve_auto_kind(values: List[str]) -> str:
    """Infer a streaming column's concrete kind (``int`` / ``double`` /
    ``string``) from a sample of its STRING cells. Used for engine fields
    the qTags didn't classify, so a numeric column (e.g. a 0/1 flag a
    measure SUMs) is stored as a NUMBER instead of defaulting to text --
    text-typed numeric columns are what make a Power BI card render its
    value in quotes.

    A numeric-looking value with a significant leading zero (``007``,
    ``00123``) is treated as an identifier/code -> the column stays
    ``string`` so leading zeros are not silently dropped. Dates are never
    auto-promoted (they arrive via the declared ``date``/``datetime`` kind
    from ``$date``/``$timestamp`` tags), matching ``_build_array_auto``."""
    from .csv_schema import _KindAccumulator

    acc = _KindAccumulator()
    for v in values:
        s = (v or "").strip()
        if len(s) > 1 and s[0] == "0" and s[1].isdigit():
            return "string"
        acc.feed(v)
    k = acc.kind()
    return k if k in ("int", "double") else "string"


def _as_int(v: str) -> Optional[int]:
    v = (v or "").strip()
    if v == "":
        return None
    try:
        return int(v)
    except ValueError:
        try:
            f = float(v)
            return int(f) if f == int(f) else None
        except ValueError:
            return None


def _as_float(v: str) -> Optional[float]:
    v = (v or "").strip()
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _as_bool(v: str) -> Optional[bool]:
    v = (v or "").strip().lower()
    if v == "true":
        return True
    if v == "false":
        return False
    return None


def _as_datetime(v: str) -> Optional[datetime]:
    """A declared date/datetime cell. The engine emits the Qlik numeric
    serial, so try that first; fall back to ISO text if a string slipped
    through; ``None`` (null) otherwise -- never raises, so one bad value
    can't abort the write."""
    v = (v or "").strip()
    if v == "" or v == "-":
        return None
    try:
        return qlik_serial_to_datetime(float(v))
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


# Declared column kinds for the streaming writer.
_STREAM_ARROW_TYPE = {
    "int":      lambda: _pa.int64(),
    "double":   lambda: _pa.float64(),
    "string":   lambda: _pa.string(),
    "date":     lambda: _pa.timestamp("ms"),
    "datetime": lambda: _pa.timestamp("ms"),
}


# Compression codec for every Parquet we emit. zstd beats snappy on size
# (a meaningful win for the download/zip and for Power BI's read) at
# negligible extra CPU, and is read natively by PBI's Parquet connector.
_COMPRESSION = "zstd"

# Default row-group size (rows). Parquet row groups should be large
# (~hundreds of thousands to ~1M rows) for fast scan + good compression;
# the engine/cloud extract hands us small pages (≤10k-cell pages, often a
# few hundred to a few thousand rows), so ``ParquetStreamWriter`` buffers
# pages and flushes a row group only at this threshold instead of one
# tiny row group per page. Override via QLIK_PARQUET_ROW_GROUP. The cap
# bounds peak memory (buffered rows × columns) -- 250k rows is a good
# balance for typical widths.
import os as _os
try:
    _DEFAULT_ROW_GROUP_ROWS = max(1, int(_os.environ.get("QLIK_PARQUET_ROW_GROUP", "250000")))
except ValueError:
    _DEFAULT_ROW_GROUP_ROWS = 250_000

# Peak-memory guard. The writer buffers up to one row group of cell strings
# before flushing; at the 250k-row default a WIDE table (e.g. a 35-column
# table fetched unpruned) would buffer 250k×35 ≈ 8.75M cell strings before
# the first flush -- hundreds of MB. Cap the effective row-group by CELLS
# (rows × cols) so peak buffered memory stays flat regardless of table
# width: wide tables just get more, smaller row groups (negligible size
# cost). Override via QLIK_PARQUET_CELL_BUDGET.
try:
    _CELL_BUDGET = max(50_000, int(_os.environ.get("QLIK_PARQUET_CELL_BUDGET", "4000000")))
except ValueError:
    _CELL_BUDGET = 4_000_000

# VertiPaq-friendly row ordering. Sorting a row group by its
# lowest-cardinality column clusters equal values -> longer runs ->
# smaller Parquet + some VertiPaq RLE benefit on import. Row order is
# immaterial for an Import model, so it is value-preserving. Applied only
# to LARGE row groups (small tables/fixtures are never reordered). Set
# QLIK_PARQUET_SORT=0 to disable.
_SORT_ROWS = (_os.environ.get("QLIK_PARQUET_SORT", "1") or "1").strip() not in ("0", "false", "no", "")
_SORT_MIN_ROWS = 50_000
_SORT_SAMPLE = 4_000      # rows sampled to estimate per-column cardinality


def _sort_buffer_low_card(rows: List[List[str]], n_cols: int) -> None:
    """In-place sort ``rows`` by the single lowest-cardinality column
    (estimated from a sample). Skips columns that are constant (no benefit)
    or near-unique (no run benefit, just sort cost). No-op if no suitable
    key column exists. Cells are strings, so the sort key is total-ordered."""
    m = min(len(rows), _SORT_SAMPLE)
    if m < 2:
        return
    best_ci, best_card = -1, None
    hi = max(2, m // 2)            # skip near-unique columns
    for ci in range(n_cols):
        seen = set()
        for r in rows[:m]:
            seen.add(r[ci] if ci < len(r) else "")
            if len(seen) > hi:     # high-cardinality -> not a useful key
                break
        card = len(seen)
        if 2 <= card <= hi and (best_card is None or card < best_card):
            best_card, best_ci = card, ci
    if best_ci < 0:
        return
    ci = best_ci
    rows.sort(key=lambda r: r[ci] if ci < len(r) else "")


def _stream_cell(kind: str, v: str) -> Any:
    if kind == "int":
        return _as_int(v)
    if kind == "double":
        return _as_float(v)
    if kind in ("date", "datetime"):
        return _as_datetime(v)
    return (v if (v is not None and v != "") else None)


def write_parquet_columns(
    out_path: Path, columns: List[Tuple[str, List[str]]]
) -> int:
    """Write ``columns`` (``[(name, [cell, ...]), ...]``) to a Parquet
    file, auto-typing each column. Returns the row count. Raises
    ``RuntimeError`` if pyarrow is unavailable (callers should guard with
    ``PYARROW_AVAILABLE`` and fall back to CSV)."""
    if not PYARROW_AVAILABLE:
        raise RuntimeError("pyarrow is not installed; cannot write Parquet.")
    if not columns:
        return 0
    arrays = [_build_array_auto(vals) for _, vals in columns]
    names = [name for name, _ in columns]
    table = _pa.table(arrays, names=names)
    _pq.write_table(table, str(out_path), compression=_COMPRESSION)
    return table.num_rows


class ParquetStreamWriter:
    """Streaming Parquet writer with a declared schema.

    ``fields`` is ``[(column_name, kind), ...]`` where ``kind`` is one of
    ``int`` / ``double`` / ``string`` / ``date`` / ``datetime`` / ``auto``.
    Cell values are STRINGS (as the engine extract produces them); each is
    converted to the declared type (a Qlik date serial -> ``datetime``),
    with unconvertible / empty cells written as null.

    ``auto`` defers the column's type: the schema is finalized from the
    first buffered row group (``_resolve_auto_kind``), so a numeric column
    the engine qTags didn't classify is stored as a number rather than
    text (text-typed numeric columns render quoted in Power BI cards). The
    inference samples the first row group (up to ``row_group_rows``);
    engine columns are type-uniform per field, and a later value that
    doesn't fit the inferred type is written as null rather than aborting.

    **Row-group batching.** ``write_page`` accumulates incoming pages in a
    buffer and only flushes a Parquet **row group** once the buffer
    reaches ``row_group_rows`` (default ``_DEFAULT_ROW_GROUP_ROWS``). The
    extract hands us small pages (≤10k-cell engine pages, or one cloud
    data-page), and writing one row group per page produced many tiny row
    groups -- slow for Power BI to scan and poor for compression. Batching
    yields large row groups (faster Import, smaller files) while peak
    memory stays bounded at one row-group's worth of rows."""

    def __init__(
        self,
        out_path: Path,
        fields: List[Tuple[str, str]],
        row_group_rows: int = _DEFAULT_ROW_GROUP_ROWS,
    ):
        if not PYARROW_AVAILABLE:
            raise RuntimeError("pyarrow is not installed; cannot write Parquet.")
        self._out_path = str(out_path)
        self._fields = list(fields)              # [(name, kind), ...]
        self._n_cols = len(fields)
        # Bound the row group by CELLS so a wide table can't balloon the
        # in-memory buffer (peak memory stays flat across table widths).
        rg = max(1, int(row_group_rows))
        if self._n_cols > 0:
            rg = min(rg, max(1, _CELL_BUDGET // self._n_cols))
        self._row_group_rows = rg
        self._buf: List[List[str]] = []          # rows pending flush
        self.rows = 0
        # ``auto`` columns defer schema creation until the first flush, when
        # their concrete kind is inferred from the buffered rows.
        self._kinds: Optional[List[str]] = None
        self._schema = None
        self._writer = None
        if not any(k == "auto" for _, k in self._fields):
            self._finalize_schema([k for _, k in self._fields])

    def _finalize_schema(self, kinds: List[str]) -> None:
        self._kinds = kinds
        self._schema = _pa.schema([
            (name, _STREAM_ARROW_TYPE.get(kind, _STREAM_ARROW_TYPE["string"])())
            for (name, _), kind in zip(self._fields, kinds)
        ])
        self._writer = _pq.ParquetWriter(
            self._out_path, self._schema, compression=_COMPRESSION,
        )

    def _resolve_auto_from_buffer(self) -> None:
        """Finalize the schema, inferring each ``auto`` column's kind from
        the rows buffered so far (the first row group)."""
        kinds: List[str] = []
        for ci, (_, kind) in enumerate(self._fields):
            if kind != "auto":
                kinds.append(kind)
                continue
            vals = [row[ci] if ci < len(row) else "" for row in self._buf]
            kinds.append(_resolve_auto_kind(vals))
        self._finalize_schema(kinds)

    def write_page(self, rows: List[List[str]]) -> None:
        if not rows:
            return
        self._buf.extend(rows)
        self.rows += len(rows)
        if len(self._buf) >= self._row_group_rows:
            self._flush()

    def _flush(self) -> None:
        """Convert the buffered rows to one typed Arrow batch and write it
        as a single Parquet row group, then clear the buffer."""
        if not self._buf:
            return
        if self._writer is None:          # deferred ``auto`` schema
            self._resolve_auto_from_buffer()
        # VertiPaq-friendly ordering: sort a LARGE row group by its
        # lowest-cardinality column so equal values cluster -> longer
        # runs -> better Parquet compression and some VertiPaq RLE benefit
        # on import. Row order is immaterial for a Power BI Import model,
        # so this is value-preserving. Gated to big buffers so it never
        # reorders small fixtures and adds no cost to small tables.
        if _SORT_ROWS and len(self._buf) >= _SORT_MIN_ROWS and self._n_cols > 0:
            _sort_buffer_low_card(self._buf, self._n_cols)
        cols: List[List[Any]] = [[] for _ in range(self._n_cols)]
        for row in self._buf:
            for ci in range(self._n_cols):
                cell = row[ci] if ci < len(row) else ""
                cols[ci].append(_stream_cell(self._kinds[ci], cell))
        batch = _pa.record_batch(
            [_pa.array(cols[ci], type=self._schema.field(ci).type)
             for ci in range(self._n_cols)],
            schema=self._schema,
        )
        # row_group_size >= the batch length keeps the whole batch as ONE
        # row group (write_table would otherwise split at its own default).
        self._writer.write_table(
            _pa.Table.from_batches([batch], schema=self._schema),
            row_group_size=len(self._buf),
        )
        self._buf = []

    def close(self) -> None:
        if self._writer is None and not self._buf:
            # 0-row table whose ``auto`` columns never got a sample: fall
            # back to string so an empty (but schema'd) file is still written.
            self._finalize_schema(
                ["string" if k == "auto" else k for _, k in self._fields])
        self._flush()              # write any remaining buffered rows
        self._writer.close()

    def __enter__(self) -> "ParquetStreamWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Reading the schema (for binding)
# ---------------------------------------------------------------------------

def _tmdl_from_arrow(field: Any) -> Dict[str, Any]:
    """Map a pyarrow field's type to a TMDL/M descriptor."""
    t = field.type
    types = _pa.types
    if types.is_integer(t):
        return {"dataType": "int64", "mType": "Int64.Type"}
    if types.is_floating(t) or types.is_decimal(t):
        return {"dataType": "double", "mType": "type number"}
    if types.is_boolean(t):
        return {"dataType": "boolean", "mType": "type logical"}
    if types.is_timestamp(t):
        return {"dataType": "dateTime", "mType": "type datetime",
                "formatString": "yyyy-MM-dd HH:mm:ss"}
    if types.is_date(t):
        return {"dataType": "dateTime", "mType": "type date",
                "formatString": "yyyy-MM-dd"}
    # string / large_string / anything else -> text.
    return {"dataType": "string", "mType": "type text"}


def sniff_parquet_schema(path: Path) -> List[Dict[str, Any]]:
    """Return column descriptors for a ``.parquet`` file from its SCHEMA
    (no content scan -- the types are in the file). Shape matches
    ``csv_schema.sniff_csv_schema`` so ``model._columns_for_table`` reuses
    the same assembly. Empty list on any read error / missing pyarrow, so
    the caller falls back to the stub path."""
    if not PYARROW_AVAILABLE:
        _log.warning("pyarrow not installed; cannot read Parquet schema for %s", path)
        return []
    try:
        schema = _pq.read_schema(str(path))
    except Exception as exc:  # noqa: BLE001
        _log.warning(f"Could not read Parquet schema {Path(path).name}: {exc}")
        return []
    cols: List[Dict[str, Any]] = []
    for field in schema:
        desc = _tmdl_from_arrow(field)
        entry: Dict[str, Any] = {
            "name":         (field.name or "").strip(),
            "sourceColumn": (field.name or "").strip(),
            "dataType":     desc["dataType"],
            "mType":        desc["mType"],
        }
        if desc.get("formatString"):
            entry["formatString"] = desc["formatString"]
        cols.append(entry)
    return cols
