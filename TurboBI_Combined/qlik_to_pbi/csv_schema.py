"""CSV schema sniffing for `--data-dir` mode.

When the user supplies a directory of exported CSVs, each table's
partition is bound to the matching CSV via ``Csv.Document(File.Contents
(...))`` instead of the empty `Table.FromRows({}, type table [...])`
stub. The TMDL column type and the M ``Table.TransformColumnTypes``
step have to agree, so we sniff each column's type from a sample of
the first ~200 rows.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._logging import get_logger

_log = get_logger("CSV-SCHEMA")


_INT_RE   = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$|^-?\d*\.\d+(?:[eE][+-]?\d+)?$|^-?\d+\.\d*(?:[eE][+-]?\d+)?$")

# Date-only patterns (no time component). Each one captures a different
# common layout so the sniffer can tell ``date`` from ``dateTime``.
_DATE_ONLY_PATTERNS = [
    r"^\d{4}-\d{1,2}-\d{1,2}$",                          # 2024-01-15
    r"^\d{4}/\d{1,2}/\d{1,2}$",                          # 2024/01/15
    r"^\d{1,2}/\d{1,2}/\d{4}$",                          # 01/15/2024  or 15/01/2024
    r"^\d{1,2}-\d{1,2}-\d{4}$",                          # 01-15-2024
    r"^\d{1,2}[-/](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/]\d{2,4}$",  # 15-Jan-2024
]
_DATE_RE = re.compile("|".join(_DATE_ONLY_PATTERNS), re.IGNORECASE)

# Datetime patterns (date + time component).
_DATETIME_PATTERNS = [
    r"^\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$",  # ISO
    r"^\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}(:\d{2})?(\s*[AP]M)?$",                       # US
    r"^\d{1,2}-\d{1,2}-\d{4}\s+\d{1,2}:\d{2}(:\d{2})?(\s*[AP]M)?$",
]
_DATETIME_RE = re.compile("|".join(_DATETIME_PATTERNS), re.IGNORECASE)

# Plain time-only (rare in CSVs but worth a category).
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?\s*([AP]M)?$", re.IGNORECASE)

# Only literal true/false sniff as logical. Power Query's `type logical`
# conversion accepts "true"/"false" text but NOT "Yes"/"No"/"Y"/"N" --
# typing a Yes/No column as logical throws at load time
# ("Expression.Error: We couldn't convert to Logical. Details: Yes").
# Qlik uses Yes/No as flag labels, so we keep them as TEXT: no load
# error and the original labels are preserved in the data view.
_BOOL_VALUES = {"true", "false"}


# (TMDL dataType, M-language type literal) pairs.
_TMDL_TYPE = {
    "string":   ("string",   "type text"),
    "int":      ("int64",    "Int64.Type"),
    "double":   ("double",   "type number"),
    "bool":     ("boolean",  "type logical"),
    "date":     ("dateTime", "type date"),
    "datetime": ("dateTime", "type datetime"),
    "time":     ("dateTime", "type time"),
}


def type_from_qlik_tags(tags: List[str]) -> Optional[Dict[str, Any]]:
    """Map a Qlik field's ``qTags`` to a TMDL/M type descriptor.

    Qlik's engine tags every field during the load script with
    semantic markers describing what kind of data the field holds.
    The tags that matter for type emission:

      * ``$integer``    -- whole numbers (counters, IDs, codes)
      * ``$numeric``    -- numeric, possibly with decimal part
      * ``$date``       -- date without time
      * ``$timestamp``  -- date + time
      * ``$text``       -- explicit text
      * ``$ascii``      -- ASCII text (always paired with $text)
      * ``$key``        -- structural; doesn't affect dataType
      * ``$hidden``     -- structural; doesn't affect dataType

    Tag precedence -- a single field may carry several. Resolution
    order, strictest-first:

      1. ``$timestamp``                       -> dateTime + ``yyyy-MM-dd HH:mm:ss``
      2. ``$date`` (no $timestamp)            -> dateTime + ``yyyy-MM-dd``
      3. ``$integer`` (no $date/$timestamp)   -> int64
      4. ``$numeric`` (no $integer)           -> double
      5. ``$text``                            -> string
      6. (no relevant tags)                   -> None (caller can sniff CSV)

    Returning ``None`` -- not a sentinel-typed dict -- when no
    type-relevant tag is present lets the caller fall back to CSV
    sniffing without us pretending we know the type. The schema
    sidecar may have been generated against a partially-loaded app
    where the engine hadn't yet tagged a field.
    """
    if not tags:
        return None
    tag_set = {(t or "").lower() for t in tags}
    if "$timestamp" in tag_set:
        return {
            "dataType":     "dateTime",
            "mType":        "type datetime",
            "formatString": "yyyy-MM-dd HH:mm:ss",
        }
    if "$date" in tag_set:
        return {
            "dataType":     "dateTime",
            "mType":        "type date",
            "formatString": "yyyy-MM-dd",
        }
    if "$integer" in tag_set:
        return {
            "dataType":     "int64",
            "mType":        "Int64.Type",
        }
    if "$numeric" in tag_set:
        return {
            "dataType":     "double",
            "mType":        "type number",
        }
    if "$text" in tag_set or "$ascii" in tag_set:
        return {
            "dataType":     "string",
            "mType":        "type text",
        }
    return None


def sniff_csv_schema(
    csv_path: Path, sample_rows: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Return a list of column descriptors for ``csv_path``.

    Each entry:
        {
          "name":         "<header column name>",
          "sourceColumn": "<header column name>",
          "dataType":     "<TMDL data type>",
          "mType":        "<M type expression>",
        }

    Type sniffing uses simple regex matches; empty cells do not vote; a
    column with any cell that doesn't fit a typed category falls back to
    ``string``.

    By default (``sample_rows=None``) **every** data row is inspected.
    This matters: the declared TMDL ``dataType`` and the partition's
    ``Table.TransformColumnTypes`` cast must agree with EVERY row PBI
    will load, or the refresh fails with *"We couldn't convert to
    Number/Date. Details: <value>"*. Sniffing only a head sample (the
    old 200-row cap) typed a column from clean early rows and then broke
    on a later non-conforming value. The scan is streaming (one running
    accumulator per column, no all-rows buffer) so the whole file is
    cheap regardless of size. ``sample_rows`` can still cap the scan for
    callers that explicitly want a sample.
    """
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                return []
            accums = [_KindAccumulator() for _ in header]
            n = len(header)
            for i, row in enumerate(reader):
                if sample_rows is not None and i >= sample_rows:
                    break
                for ci in range(n):
                    accums[ci].feed(row[ci] if ci < len(row) else "")
    except OSError as exc:
        _log.warning(f"Could not read CSV {csv_path.name}: {exc}")
        return []

    cols: List[Dict[str, Any]] = []
    for ci, raw_name in enumerate(header):
        cell_kind = accums[ci].kind()
        tmdl_dt, m_type = _TMDL_TYPE[cell_kind]
        # PBI's TMDL ``dataType`` lumps date / datetime / time into a
        # single ``dateTime``. The visible difference comes from the
        # ``formatString`` (and from the M-side ``type`` literal which
        # determines how Power Query parses the source). Stamp the
        # right format here so a date-only column renders as
        # ``2024-01-15`` rather than ``2024-01-15 00:00:00``.
        fmt = ""
        if cell_kind == "date":
            fmt = "yyyy-MM-dd"
        elif cell_kind == "datetime":
            fmt = "yyyy-MM-dd HH:mm:ss"
        elif cell_kind == "time":
            fmt = "HH:mm:ss"
        col_entry: Dict[str, Any] = {
            "name":         (raw_name or f"col_{ci}").strip(),
            "sourceColumn": (raw_name or f"col_{ci}").strip(),
            "dataType":     tmdl_dt,
            "mType":        m_type,
        }
        if fmt:
            col_entry["formatString"] = fmt
        cols.append(col_entry)
    return cols


class _KindAccumulator:
    """Streaming type detector for one CSV column.

    ``feed`` each cell value (in any order); ``kind`` returns one of
    ``string | int | double | bool | date | datetime | time``. The kind
    drives both the TMDL ``dataType`` and the M ``type`` literal: int ->
    ``int64`` / ``Int64.Type``, decimal -> ``double`` / ``type number``,
    date-only -> ``dateTime`` / ``type date`` (so Power Query parses
    without a time component), etc.

    Streaming (rather than buffering every cell) keeps memory flat when
    the whole file is scanned. Semantics are identical to inspecting the
    full cell list at once: ANY cell that doesn't fit a typed category
    latches the column to ``string`` -- the safe default for
    heterogeneous data, and what guarantees the partition cast can't
    fail on a stray value.
    """

    __slots__ = (
        "seen_int", "seen_float", "seen_bool",
        "seen_date", "seen_datetime", "seen_time",
        "nonempty", "_string",
    )

    def __init__(self) -> None:
        self.seen_int = self.seen_float = self.seen_bool = 0
        self.seen_date = self.seen_datetime = self.seen_time = 0
        self.nonempty = 0
        self._string = False

    def feed(self, cell: Any) -> None:
        if self._string:
            return
        c = (cell or "").strip()
        if not c:
            return
        self.nonempty += 1
        if _INT_RE.match(c):
            self.seen_int += 1
        elif _FLOAT_RE.match(c):
            self.seen_float += 1
        elif _DATETIME_RE.match(c):
            self.seen_datetime += 1
        elif _DATE_RE.match(c):
            self.seen_date += 1
        elif _TIME_RE.match(c):
            self.seen_time += 1
        elif c.lower() in _BOOL_VALUES:
            self.seen_bool += 1
        else:
            self._string = True

    def kind(self) -> str:
        if self._string or self.nonempty == 0:
            return "string"
        # Numeric: mixed int+float -> double, all int -> int.
        if (self.seen_int + self.seen_float) == self.nonempty and self.seen_float > 0:
            return "double"
        if self.seen_int == self.nonempty:
            return "int"
        # Date / datetime / time: prefer the strictest match.
        if (self.seen_date + self.seen_datetime) == self.nonempty and self.seen_datetime > 0:
            return "datetime"
        if self.seen_date == self.nonempty:
            return "date"
        if self.seen_time == self.nonempty:
            return "time"
        if self.seen_bool == self.nonempty:
            return "bool"
        return "string"


def _column_kind(cells: List[str]) -> str:
    """Kind of a column given the full list of its cell values.

    Thin wrapper over :class:`_KindAccumulator` kept for direct callers
    / tests; the streaming path in ``sniff_csv_schema`` uses the
    accumulator directly so it never buffers all rows.
    """
    acc = _KindAccumulator()
    for cell in cells:
        acc.feed(cell)
    return acc.kind()


def match_csv_for_table(
    table_name: str,
    csv_files: List[Path],
) -> Optional[Path]:
    """Try a few naming conventions to find a CSV that maps to ``table_name``.

    Match priority (first hit wins):
      1. Exact case-insensitive stem match.
      2. Same after stripping spaces / underscores / hyphens.
      3. ``Extract.<table>.csv`` prefix (matches the working
         tableau-converted PBIP's federated-data naming).
      4. CSV stem CONTAINS the table name, or vice versa
         (covers "Customers Master.csv" -> table "Customers", or
         the auto-fetched "Overview - Sales by Customer.csv" ->
         a "Sales" table). Tie-broken by shortest stem so the
         closest semantic match wins.

    Returns the first match. Empty if no candidate fits.
    """
    norm_table = _normalize(table_name)
    norm_table_compact = _normalize(table_name.replace(" ", ""))

    # Tier 1+2+3: deterministic matches.
    for fp in csv_files:
        stem = fp.stem
        stem_norm = _normalize(stem)
        if stem_norm == norm_table or stem_norm == norm_table_compact:
            return fp
        if stem.lower().startswith("extract."):
            short = _normalize(stem.split(".", 1)[1])
            if short == norm_table or short == norm_table_compact:
                return fp

    # Tier 4: prefix match against the CSV stem. The table name must
    # appear at the START of the CSV stem (after normalisation) for it
    # to count as a match -- NOT anywhere inside.
    #
    # Why "start" and not "anywhere": real fetched CSVs are named after
    # *charts* like "Overview - HCO by HCP Count.csv". A plain
    # substring match would bind BOTH the HCO and HCP loadmodel tables
    # to that one file (each table name appears inside the stem),
    # producing a corrupted model. The start-anchored rule means
    # "Customers Master.csv" still maps to a "Customers" table, but
    # "HCO by HCP Count.csv" only maps to a hypothetical "HCO" table
    # (whichever name is closer to the front), never to "HCP".
    #
    # Tie-broken by shortest stem so a tighter match wins over a
    # looser one.
    if not norm_table:
        return None
    candidates: List[Path] = []
    for fp in csv_files:
        stem_norm = _normalize(fp.stem)
        if stem_norm.startswith(norm_table) or norm_table.startswith(stem_norm):
            candidates.append(fp)
    if candidates:
        candidates.sort(key=lambda p: (len(p.stem), p.stem.lower()))
        return candidates[0]
    return None


def _normalize(s: str) -> str:
    return re.sub(r"[\s_\-.]+", "", (s or "").lower())
