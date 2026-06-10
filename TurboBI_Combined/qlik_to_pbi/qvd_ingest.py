"""Transcode user-supplied Qlik **QVD** files to typed **Parquet**.

Why this exists
---------------
The Engine-API fetch is latency-bound: every ``GetHyperCubeData`` call moves
at most 10k cells and costs a full cloud round-trip, so a multi-million-row
table takes many minutes (see ``docs/large-data-strategy.md``). When the user
already has the app's source **QVD** files, reading them **locally** is the
real step-change -- a columnar read at disk speed, with none of the engine's
cell-cap or round-trip ceiling. This is Phase 1 of the large-data strategy.

Power BI cannot read QVD natively (no GA/preview connector), so we transcode:
``pyqvd`` reads the QVD's already-typed columns (integer / double / money /
date / timestamp / string -- and, crucially, leading-zero codes like ``"007"``
stay *string*, not coerced to a number) and we write them straight to Parquet.
The model then binds that Parquet with the same single-step
``Parquet.Document(File.Contents(...))`` partition it uses for engine-fetched
Parquet -- no cast, no schema drift, identical downstream path.

``pyqvd`` is an OPTIONAL dependency, imported lazily exactly like ``pyarrow``
in :mod:`qlik_to_pbi.parquet_io`. With it absent (or ``pyarrow`` absent) the
caller gets a clear, actionable error and can fall back to the engine fetch --
the QVD path is never required for a conversion to succeed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ._logging import get_logger
from .utils import safe_filename

_log = get_logger("QVD")

# Lazy / optional import -- mirrors parquet_io.PYARROW_AVAILABLE. A missing
# pyqvd must never crash an import of the package; it only blocks the QVD
# fast-path, which is opt-in.
try:  # pragma: no cover - exercised by availability, not unit tests
    from pyqvd import QvdTable  # type: ignore
    PYQVD_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure means "unavailable"
    QvdTable = None  # type: ignore
    PYQVD_AVAILABLE = False


def qvd_available() -> bool:
    """True iff BOTH pyqvd and pyarrow are importable (the transcode needs
    pyqvd to read and pyarrow to write). The UI uses this to decide whether
    to offer the QVD-upload step at all."""
    if not PYQVD_AVAILABLE:
        return False
    try:
        from .parquet_io import PYARROW_AVAILABLE
        return bool(PYARROW_AVAILABLE)
    except Exception:  # noqa: BLE001
        return False


def _unavailable_reason() -> str:
    bits = []
    if not PYQVD_AVAILABLE:
        bits.append("pyqvd (`pip install pyqvd`)")
    try:
        from .parquet_io import PYARROW_AVAILABLE
        if not PYARROW_AVAILABLE:
            bits.append("pyarrow (`pip install pyarrow`)")
    except Exception:  # noqa: BLE001
        bits.append("pyarrow (`pip install pyarrow`)")
    return "QVD transcoding needs " + " and ".join(bits) + "."


def qvd_to_parquet(
    qvd_path: str | Path,
    out_path: str | Path,
    *,
    row_group_rows: Optional[int] = None,
) -> Dict[str, Any]:
    """Read one QVD and write it to ``out_path`` as typed Parquet.

    Returns ``{rows, cols, columns, path, bytes}``. Column dtypes are taken
    straight from pyqvd's typed read (it already distinguishes integer /
    double / datetime / string and preserves leading-zero string codes), so
    the Parquet carries the right schema and the bound partition needs no
    cast -- identical to the engine-fetched Parquet path.

    Raises ``RuntimeError`` if pyqvd/pyarrow are unavailable or the file
    can't be read; the caller decides whether to fall back to the engine.
    """
    if not qvd_available():
        raise RuntimeError(_unavailable_reason())

    import pyarrow as pa
    import pyarrow.parquet as pq
    from .parquet_io import _COMPRESSION, _DEFAULT_ROW_GROUP_ROWS

    qvd_path = Path(qvd_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rg = int(row_group_rows or _DEFAULT_ROW_GROUP_ROWS)

    try:
        tbl = QvdTable.from_qvd(str(qvd_path))  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not read QVD {qvd_path.name}: {exc}") from exc

    # pyqvd hands us a fully-typed pandas frame: int64 / float64 /
    # datetime64 / object(string). preserve_index=False so the row index
    # never leaks in as a phantom column.
    df = tbl.to_pandas()
    columns = [str(c) for c in df.columns]
    rows = int(len(df))

    # Build the Arrow table ONCE (fixes one consistent schema across all
    # row groups -- slicing-then-inferring per chunk would let an all-null
    # leading chunk pick a different type than a later populated one), then
    # drop the pandas frame before the write so peak memory is one copy, not
    # two. write_table chunks the OUTPUT into <=rg-row groups (bounded PBI
    # scan + bounded reader memory) in a single call.
    table = pa.Table.from_pandas(df, preserve_index=False)
    del df
    pq.write_table(
        table, str(out_path),
        compression=_COMPRESSION,
        row_group_size=max(1, rg),
    )

    try:
        nbytes = out_path.stat().st_size
    except OSError:
        nbytes = 0
    _log.info(
        f"  {qvd_path.name} -> {out_path.name} "
        f"({rows:,} rows x {len(columns)} cols, {nbytes:,} bytes)"
    )
    return {
        "rows": rows,
        "cols": len(columns),
        "columns": columns,
        "path": str(out_path),
        "bytes": nbytes,
    }


def transcode_qvd_map(
    mapping: Dict[str, str | Path],
    out_dir: str | Path,
    *,
    row_group_rows: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    """Transcode a ``{table_name: qvd_path}`` map to ``<out_dir>/<table>.parquet``.

    The Parquet is named ``<safe_filename(table_name)>.parquet`` so the
    converter's exact-match CSV/Parquet binding tier picks it up by table
    name with no further renaming -- the same naming the engine fetch uses.

    One bad file never aborts the batch: failures are recorded under the
    table's entry as ``{"error": "..."}`` so the caller can report them and
    still fall back to the engine for those specific tables.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: Dict[str, Dict[str, Any]] = {}
    for table_name, qvd_path in mapping.items():
        safe = safe_filename(str(table_name), max_len=80)
        dest = out_dir / f"{safe}.parquet"
        try:
            info = qvd_to_parquet(qvd_path, dest, row_group_rows=row_group_rows)
            info["table"] = table_name
            results[table_name] = info
        except Exception as exc:  # noqa: BLE001 - record + continue
            _log.warning(f"  QVD transcode failed for {table_name!r}: {exc}")
            results[table_name] = {"table": table_name, "error": str(exc)}
    return results


def transcoded_table_names(results: Dict[str, Dict[str, Any]]) -> List[str]:
    """The table names that transcoded successfully (no ``error`` key).
    These are the tables the engine fetch should SKIP -- their data is
    already on disk as Parquet."""
    return [t for t, info in results.items() if info and "error" not in info]
