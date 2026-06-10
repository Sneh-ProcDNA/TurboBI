"""Offline data extraction via Qlik Sense Desktop's WebSocket Engine API.

This module is the offline counterpart to :mod:`qlik_to_pbi.fetch_data`.
Where ``fetch_data`` shells out to the cloud ``qlik`` CLI (which is
limited to whichever charts the app author happened to build, and only
returns the first ~500 rows per chart), this module talks JSON-RPC over
a localhost WebSocket to a running Qlik Sense Desktop instance and
synthesises a **session-object hypercube per loadmodel table** -- one
``qDimension`` per field of the table -- to extract the raw table data
with arbitrary pagination.

Key properties:

* **Offline.** Everything happens on ``ws://localhost:4848``. No cloud
  tenant, no API key, no data ever leaves the machine. Desktop runs
  under the current OS user, so Qlik permissions match file
  permissions -- you can only read your own apps.
* **Schema-driven.** Tables and fields are read from the parsed
  ``loadmodel---loadmodel.json``, so we extract exactly the underlying
  data model regardless of how charts in the app are shaped.
* **Pagination.** The Qlik Engine caps each ``GetHyperCubeData`` call
  to 10,000 cells. We loop ``NxPage`` rectangles until every row has
  been streamed out -- no truncation.
* **Graceful degradation.** If Desktop is not running, the .qvf path
  is wrong, or the websocket library is missing, we log a warning
  and return an empty list. The converter then falls back to
  empty-stub partitions and the PBIP still opens.

Prerequisites:

* Qlik Sense Desktop installed and **running** locally (the engine
  listens on ``ws://localhost:4848``).
* ``websocket-client`` (``pip install websocket-client``).
"""

from __future__ import annotations

import csv as _csv
import gc
import json
import os
import socket
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

try:
    from websocket import (  # type: ignore
        create_connection, WebSocket, WebSocketException,
    )
except ImportError:  # pragma: no cover - import-time guard
    create_connection = None  # type: ignore
    WebSocket = None  # type: ignore

    class WebSocketException(Exception):  # type: ignore
        pass

from ._logging import get_logger
from .script_parser import parse_field_renames
from .utils import safe_filename

_log = get_logger("ENGINE")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ENGINE_URL = "ws://localhost:4848"

# Engine API caps each GetHyperCubeData call to 10,000 cells across all
# requested pages. We size pages off this limit and the column count.
# Overridable via QLIK_MAX_CELLS_PER_CALL for environments with very tight
# memory budgets; the engine cap is still 10,000 so values above that are
# clamped down.
_MAX_CELLS_PER_CALL = min(
    10_000,
    int(os.environ.get("QLIK_MAX_CELLS_PER_CALL", "10000") or "10000"),
)

# Soft upper bound on rows per call regardless of column count. Lowered
# from 3,000 -> 1,000 to keep the JSON-RPC response (and the transient
# `raw` string + parsed dict pair) small enough that Python's allocator
# can release pages between requests. Overridable via QLIK_MAX_ROWS_PER_CALL.
_MAX_ROWS_PER_CALL = max(
    100,
    int(os.environ.get("QLIK_MAX_ROWS_PER_CALL", "1000") or "1000"),
)

# Row-range parallelism (2026-06). A single big table is otherwise fetched
# by ONE worker serially -- and the fetch is latency-bound (each <=10k-cell
# call costs a full cloud round-trip), so the fetch cost of a table is
# proportional to its CELLS (rows x columns), not its rows: each call moves
# 10k cells regardless of table shape. We split a table's row range into N
# contiguous slices, fetch each slice on its own WebSocket concurrently, and
# merge the part files.
#
# The split TRIGGER is cell-based (2026-06 fix). A row-count threshold misses
# the common shape where the dominant table is "only" ~140k rows but 49 cols
# wide (~6.8M cells = ~78% of an app's whole extract) -- under a 300k-row gate
# it never split, so the worker pool only parallelised the small tables and
# the fat table still ran serially on one worker (the user-reported "8 slices
# didn't help"). Splitting on cells fixes exactly that: any table whose cells
# exceed _RANGE_SPLIT_MIN_CELLS is sliced; N targets _RANGE_CHUNK_CELLS per
# slice, bounded by the worker count.
try:
    _RANGE_SPLIT_MIN_CELLS = max(1, int(os.environ.get("QLIK_RANGE_SPLIT_MIN_CELLS", "1200000") or "1200000"))
except ValueError:
    _RANGE_SPLIT_MIN_CELLS = 1_200_000
try:
    _RANGE_CHUNK_CELLS = max(1, int(os.environ.get("QLIK_RANGE_CHUNK_CELLS", "1000000") or "1000000"))
except ValueError:
    _RANGE_CHUNK_CELLS = 1_000_000

# ---------------------------------------------------------------------------
# Worker-budget sizing.
#
# The Engine fetch is latency-bound (each <=10k-cell GetHyperCubeData call is a
# full round-trip), so concurrent WebSocket sessions speed it up -- up to the
# engine pod's contention ceiling. The DEFAULT is a FLAT 8 workers for every
# cloud app (set 2026-06-04 at user request, backed by the worker-count
# benchmark on the 7.5M-row Hospital app: 6 workers -> 182s, **8 -> 128s**,
# 12 -> 164s with more `code 15` contention -- 8 was the sweet spot, and more
# only adds exclusive-ClearAll contention without speedup; correctness held at
# every count). Earlier this was record-tiered (Basic 4 / Medium 6 / High 8);
# the count-anchor extract removed the heavy over-the-wire fetch of big facts
# (they now come back as distinct combos + a count, expanded locally), so the
# remaining work is per-table overhead that 8 workers covers well regardless of
# app size -- hence a flat default. Desktop's single localhost engine is
# single-threaded, so it stays capped at 2 (8 there would only self-contend).
# An explicit QLIK_FETCH_WORKERS overrides exactly (still clamped to the cap).
_TIER_BASIC_MAX_RECORDS = 5_000_000
_TIER_MEDIUM_MAX_RECORDS = 20_000_000
# Flat default: every cloud app uses the cap (8); Desktop uses its own cap (2).
# The tier constants are kept (all 8) so the sizing is trivial to re-tier later
# without reshaping `_auto_worker_count` or its callers; raise the cloud cap +
# QLIK_FETCH_WORKERS together if a tenant proves it sustains more.
_TIER_BASIC_WORKERS = 8
_TIER_MEDIUM_WORKERS = 8
_TIER_HIGH_WORKERS = 8
_CLOUD_WORKER_CAP = 8
_DESKTOP_WORKER_CAP = 2
_DEFAULT_FETCH_WORKERS = 8   # also the fallback when the record count is unknown

# Legacy force-MAX override kept False (a 2026-06-03 experiment that forced 20
# workers overloaded the pod and lost data -- since fixed by resilience, but the
# flat-8 default makes it moot). An explicit QLIK_FETCH_WORKERS still wins.
_FORCE_MAX_WORKERS = False


# ---------------------------------------------------------------------------
# IPv4-preferred engine connection.
#
# Qlik Cloud tenant hosts publish BOTH A (IPv4) and AAAA (IPv6) DNS records.
# On a machine whose IPv6 route is dead -- common on corporate Windows and
# many home networks -- ``websocket-client`` tries the AAAA addresses first and
# each one burns the full ~21s TCP connect timeout before (if ever) falling
# back to IPv4. REST (``requests``) is unaffected because it picks a reachable
# address, so the API key authenticates fine while the Engine WebSocket
# "cannot reach"/times out intermittently -- exactly the flaky-connect,
# getaddrinfo-failed, slow-fetch behaviour seen in the field. We install a
# process-wide ``getaddrinfo`` shim ONCE that sorts IPv4 ahead of IPv6 for
# ``qlikcloud.com`` hosts only; IPv6 is KEPT as a fallback (so IPv6-only
# environments still work) and every other host is returned untouched. Opt out
# with QLIK_PREFER_IPV4=0.
_IPV4_PREF_INSTALLED = False


def _install_ipv4_preference() -> None:
    """Sort IPv4 ahead of IPv6 in getaddrinfo for qlikcloud hosts. Idempotent;
    called at import (single-threaded) so workers never race on the install."""
    global _IPV4_PREF_INSTALLED
    if _IPV4_PREF_INSTALLED:
        return
    if (os.environ.get("QLIK_PREFER_IPV4", "1") or "1").strip() in ("0", "false", "no"):
        _IPV4_PREF_INSTALLED = True
        return
    _orig_getaddrinfo = socket.getaddrinfo

    def _ipv4_first(host, *args, **kwargs):
        res = _orig_getaddrinfo(host, *args, **kwargs)
        try:
            if isinstance(host, str) and host.lower().endswith("qlikcloud.com"):
                res = sorted(res, key=lambda r: 0 if r[0] == socket.AF_INET else 1)
        except Exception:  # noqa: BLE001 -- never let the shim break resolution
            return res
        return res

    socket.getaddrinfo = _ipv4_first
    _IPV4_PREF_INSTALLED = True


_install_ipv4_preference()

# Count-anchor faithful extract (2026-06). A dimension-only hypercube returns
# DISTINCT combinations, which both ADDS phantom rows (orphan members of a
# shared key come back NULL-padded) and DROPS exact-duplicate physical rows.
# The fix appends a table-scoped ``=Count([anchor])`` measure + qSuppressZero
# and expands each combo by its count. Default on; opt out with
# QLIK_COUNT_ANCHOR=0 (reverts to the dimension-only extract).
_COUNT_ANCHOR = (
    (os.environ.get("QLIK_COUNT_ANCHOR", "1") or "1").strip()
    not in ("0", "false", "no")
)

# Cube-create retry on a TRANSIENT abort (engine code 15). When several workers
# share one cloud app session, each connect's exclusive Doc.ClearAll can abort
# another worker's in-flight CreateSessionObject / GetLayout. A short backoff
# lets the exclusive burst drain; the retry then succeeds.
_CUBE_CREATE_ATTEMPTS = 4
_CUBE_RETRY_BACKOFF = 0.5   # seconds * attempt


def _is_transient_abort(exc: Exception) -> bool:
    """True for the engine's transient 'Request aborted ... family requests'
    (code 15) -- caused by a concurrent exclusive op (another worker's ClearAll)
    on the shared app session, not a real cube error. Retry clears it."""
    m = str(exc).lower()
    return (
        "request aborted" in m
        or "family requests" in m
        or "code 15" in m
        or "beginexclusive" in m
        or "exclusive request aborted" in m
    )


def _auto_worker_count(total_records, cap):
    """Worker count for an app of ``total_records`` physical rows, by tier,
    clamped to ``cap``. Pure (no I/O) so it is unit-testable.

    Bigger app -> more concurrent sockets, up to the pod-contention cap, so a
    huge app reaches the MAX (20) automatically while a small one stays lean
    (10). Unknown size (``total_records <= 0``) -> ``_DEFAULT_FETCH_WORKERS``.
    ``cap`` is applied last so a Desktop cap of 2 always wins over the tier."""
    cap = max(1, int(cap))
    r = int(total_records or 0)
    if r <= 0:
        return min(_DEFAULT_FETCH_WORKERS, cap)
    if r <= _TIER_BASIC_MAX_RECORDS:
        n = _TIER_BASIC_WORKERS
    elif r <= _TIER_MEDIUM_MAX_RECORDS:
        n = _TIER_MEDIUM_WORKERS
    else:
        n = _TIER_HIGH_WORKERS
    return min(cap, n)


# ---------------------------------------------------------------------------
# Row-fidelity guard.
#
# A Qlik hypercube returns the DISTINCT combinations of its dimensions. So
# fetching only a SUBSET of a table's columns (what column pruning does) can
# collapse physical rows whose values happen to match on the kept columns --
# silently undercounting any measure at row grain. Fetching ALL columns
# preserves every physical row (verified: a 6.83M-row fact returns 6.83M with
# all columns, but only ~51k pruned to its key alone). The guard below probes
# each pruned table's distinct count and, when it falls short of the physical
# row count, restores all columns for that table.
_ROW_FIDELITY_TOL = 0.005   # tolerate <=0.5% (null-combination rounding noise)
_ROW_FIDELITY_GUARD = (
    os.environ.get("QLIK_PRUNE_ROW_GUARD", "1") or "1"
).strip().lower() not in ("0", "false", "no", "")


def _is_row_collapse(qcy, physical, tol=_ROW_FIDELITY_TOL):
    """True iff a hypercube's distinct-row count ``qcy`` falls meaningfully
    below the table's physical row count ``physical`` (i.e. pruning the cube's
    columns collapsed rows). Pure, so the policy is unit-testable. Returns
    False when either count is unknown/zero (nothing to compare)."""
    qcy = int(qcy or 0)
    physical = int(physical or 0)
    if qcy <= 0 or physical <= 0:
        return False
    return qcy < physical * (1.0 - tol)


def _plan_split_n(est_rows, ncols, workers,
                  min_cells=None, chunk_cells=None):
    """How many parallel row-slices to split a table into, from its CELL
    estimate (rows x columns). Returns 1 = don't split.

    Pure (no I/O) so the split policy is unit-testable. A table is split when
    its cells exceed ``min_cells``; the slice count targets ``chunk_cells``
    per slice, capped by the worker budget and never exceeding the row count.
    Splitting on cells (not rows) is deliberate -- fetch cost is cells, so a
    wide-but-short dominant table must split too (see the module note)."""
    min_cells = _RANGE_SPLIT_MIN_CELLS if min_cells is None else min_cells
    chunk_cells = _RANGE_CHUNK_CELLS if chunk_cells is None else chunk_cells
    est_rows = int(est_rows or 0)
    ncols = max(1, int(ncols or 1))
    if workers <= 1 or est_rows < 2:
        return 1
    cells = est_rows * ncols
    if cells <= min_cells:
        return 1
    n = max(2, min(int(workers), -(-cells // chunk_cells)))
    return min(n, est_rows)

# Run an explicit `gc.collect()` every N rows of a single-table extract so
# the dict cycles created by parsing many JSON-RPC responses are released
# promptly. CPython's generational GC handles most of this for free, but
# very-large extracts (1M+ rows of wide tables) benefit from a nudge.
_GC_EVERY_ROWS = 100_000


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def read_loadmodel(qlik_unbuild_dir: Path) -> Dict[str, Any]:
    """Return the parsed ``loadmodel---loadmodel.json`` from an unbuild dir.

    A tiny helper so the CLI can grab the loadmodel without paying for
    a full :func:`qlik_to_pbi.parser.parse_qlik_output` walk just to
    read one file.
    """
    objects = Path(qlik_unbuild_dir) / "objects"
    if not objects.is_dir():
        return {}
    for fp in objects.glob("loadmodel*.json"):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning(f"Could not read loadmodel at {fp}: {exc}")
            return {}
    return {}


def _prune_table_fields(
    tables: List[Tuple[str, List[Dict[str, Any]]]],
    keep_fields: Set[str],
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Drop fields the model can't reference. A field is KEPT when:

      * its ``alias`` or ``name`` is in ``keep_fields`` (referenced by
        some expression / dimension / measure / variable -- see
        ``field_usage.collect_used_field_names``), OR
      * it appears in **more than one table** (a join key -- the basis
        for every relationship), OR
      * (fallback) dropping would leave the table with no fields, in
        which case the whole table is kept unchanged.

    Safe-by-construction: a field is removed only when nothing in the app
    references its name AND it is not a cross-table key, so no
    relationship, measure, or visual can depend on it. Comparison is
    case-insensitive on both ``alias`` and ``name``."""
    # Cross-table fields = join keys. Count by lowercased alias/name.
    counts: Dict[str, int] = {}
    for _tname, fields in tables:
        seen_here: Set[str] = set()
        for f in fields:
            for nm in (f.get("alias"), f.get("name")):
                low = (nm or "").strip().lower()
                if low and low not in seen_here:
                    seen_here.add(low)
        for low in seen_here:
            counts[low] = counts.get(low, 0) + 1
    multi_table = {k for k, c in counts.items() if c > 1}

    def _keep(f: Dict[str, Any]) -> bool:
        alias = (f.get("alias") or "").strip().lower()
        name = (f.get("name") or "").strip().lower()
        return (
            alias in keep_fields or name in keep_fields
            or alias in multi_table or name in multi_table
        )

    out: List[Tuple[str, List[Dict[str, Any]]]] = []
    total_before = total_after = 0
    for tname, fields in tables:
        total_before += len(fields)
        kept = [f for f in fields if _keep(f)]
        if not kept:
            # Never emit a zero-column table; keep it whole instead.
            kept = fields
        total_after += len(kept)
        if len(kept) < len(fields):
            _log.info(
                f"  {tname}: pruned {len(fields) - len(kept)} unused "
                f"column(s) ({len(fields)} -> {len(kept)})"
            )
        out.append((tname, kept))
    dropped = total_before - total_after
    if dropped:
        _log.info(
            f"Column pruning: dropped {dropped} unused column(s) across "
            f"{len(tables)} table(s) ({total_before} -> {total_after} total)."
        )
    else:
        _log.info("Column pruning: no unused columns found; nothing dropped.")
    return out


def fetch_via_engine(
    qvf_path: Optional[Path] = None,
    load_model: Optional[Dict[str, Any]] = None,
    output_dir: Path = Path("."),
    engine_url: str = DEFAULT_ENGINE_URL,
    tenant: Optional[str] = None,
    api_key: Optional[str] = None,
    app_id: Optional[str] = None,
    unbuild_dir: Optional[Path] = None,
    emit_format: str = "parquet",
    keep_fields: Optional[Set[str]] = None,
    skip_tables: Optional[Set[str]] = None,
    data_skip_tables: Optional[Set[str]] = None,
) -> List[Path]:
    """Extract every loadmodel table to Parquet (or CSV) via the Engine API.

    ``data_skip_tables`` -- tables to include in the engine SCHEMA refresh
    (so they land in the engine-schema sidecar with their authoritative field
    names + keys, and the model can build + relate them) but to EXCLUDE from the
    DATA extraction. Used for tables repointed at a live DB source: the model
    needs their schema, but their rows come from the source, not a fetched
    snapshot. Contrast ``skip_tables`` (uploaded QVD), which removes the table
    entirely (its schema comes from the supplied file).

    ``emit_format`` -- ``"parquet"`` (default) or ``"csv"``. Parquet
    types each column from the engine's qTags (no sniff, no cast) and is
    the default at large row counts (docs/large-data-strategy.md).
    Falls back to CSV when ``pyarrow`` isn't installed.

    Parameters
    ----------
    qvf_path
        Absolute path to the .qvf file Desktop should open. Desktop
        accepts arbitrary file paths via the path-based WebSocket URL
        -- the file does not have to live in Desktop's default Apps
        folder.
    load_model
        The parsed ``loadmodel---loadmodel.json`` dict. We read
        ``tables[].tableAlias`` and ``tables[].fields[].name`` /
        ``.alias``.
    output_dir
        Where to drop CSVs. One CSV per loadmodel table, named
        ``<tableAlias>.csv`` so the exact-match tier in
        :func:`qlik_to_pbi.csv_schema.match_csv_for_table` binds it
        without any further renaming.
    engine_url
        WebSocket base URL. Default ``ws://localhost:4848``.

    Returns
    -------
    list of Path
        CSV files successfully written. Failures are logged and skipped
        so a single broken table does not abort the whole run.
    """
    if create_connection is None:
        _log.warning(
            "The websocket-client package is not installed. Run "
            "`pip install websocket-client` and retry."
        )
        return []

    is_cloud = bool(tenant and api_key and app_id)
    if not is_cloud:
        if qvf_path is None:
            _log.warning("No qvf_path and no cloud creds; nothing to fetch.")
            return []
        qvf_path = Path(qvf_path).resolve()
        if not qvf_path.is_file():
            _log.warning(f"QVF not found: {qvf_path}")
            return []

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    tables = _extract_table_field_pairs(load_model or {})
    if not tables:
        _log.warning(
            "loadmodel has no tables / fields. Skipping engine extract."
        )
        return []

    # Skip tables whose data was already supplied out-of-band (e.g. a
    # user-uploaded QVD transcoded to Parquet by qvd_ingest). Matching is by
    # safe-filename-normalised name -- the same normalisation the data files
    # are written under -- so the loadmodel display name lines up with the
    # supplied file regardless of spaces/casing. Saves the whole fetch (incl.
    # the split probe + per-call round-trips) for those tables.
    if skip_tables:
        skip_norm = {safe_filename(s, max_len=80).lower() for s in skip_tables}
        kept = [(nm, f) for (nm, f) in tables
                if safe_filename(nm, max_len=80).lower() not in skip_norm]
        n_skipped = len(tables) - len(kept)
        if n_skipped:
            _log.info(
                f"Engine extract: skipping {n_skipped} table(s) supplied "
                f"out-of-band (e.g. uploaded QVD)."
            )
        tables = kept
        if not tables:
            _log.info(
                "Engine extract: every table supplied out-of-band; "
                "no engine fetch needed."
            )
            return []
    _log.info(
        f"Engine extract plan: {len(tables)} loadmodel table(s) -> {output_dir}"
    )

    # Replace the (stale) loadmodel field list per table with the
    # engine's CURRENT schema via GetTablesAndKeys. The loadmodel
    # snapshot is frozen at the last reload and routinely diverges
    # from what the script's autogenerated section produces (renamed
    # keys like `From_HCP_ID-HCP_ID`, script-added GeoMakePoint
    # columns, etc.). We honour all of those by using the engine's
    # post-script field list as the source of truth.
    # Recover per-table field rename map from the script's autogenerated
    # section so the CSV header (and downstream TMDL column names) can
    # use the friendlier ORIGINAL name. The map is also bundled into
    # the engine schema sidecar so model.py can translate qk key
    # records into per-table relationship endpoints.
    field_renames: Dict[str, Dict[str, str]] = {}
    if unbuild_dir is not None:
        script_path = Path(unbuild_dir) / "script.qvs"
        if script_path.is_file():
            try:
                with open(script_path, "r", encoding="utf-8") as f:
                    field_renames = parse_field_renames(f.read())
            except OSError as exc:
                _log.warning(f"  could not read script.qvs: {exc}")

    tables, row_counts = _refresh_field_lists_from_engine(
        tables, qvf_path, engine_url, tenant, api_key, app_id,
        unbuild_dir=unbuild_dir, field_renames=field_renames,
    )

    # Exclude live-DB tables from DATA extraction AFTER the schema refresh, so
    # their authoritative field names + keys are still captured in the
    # engine-schema sidecar (the model builds + relates them) but their rows are
    # not fetched -- they come from the live source instead.
    if data_skip_tables:
        ds_norm = {safe_filename(s, max_len=80).lower() for s in data_skip_tables}
        before = len(tables)
        tables = [(nm, f) for (nm, f) in tables
                  if safe_filename(nm, max_len=80).lower() not in ds_norm]
        n_ds = before - len(tables)
        if n_ds:
            _log.info(
                f"Engine extract: schema captured for {n_ds} live-DB table(s); "
                f"their DATA is not fetched (repointed at the source)."
            )
        if not tables:
            _log.info(
                "Engine extract: every table is supplied out-of-band or "
                "repointed at a live source; no data extraction needed."
            )
            return []

    # Count-anchor selection (one connection): pick a table-scoped
    # ``Count([own-only field])`` anchor per table so the extract reproduces the
    # physical rows exactly -- dropping orphan-key phantom rows AND restoring
    # exact-duplicate rows the distinct-only hypercube would collapse -- and
    # does so REGARDLESS of column pruning. Tables with no qualifying field
    # (e.g. pure all-key link tables) fall back to the dimension-only extract.
    anchors = _select_table_anchors(
        tables, row_counts, qvf_path, engine_url, tenant, api_key, app_id,
    )

    # Optional column pruning: extract only fields the model can actually
    # reference. Safe-by-construction for the MODEL (-- see _prune_table_fields)
    # but a pruned column set can COLLAPSE physical rows in the hypercube (which
    # returns distinct combinations of its dimensions), silently undercounting
    # row-grain measures. After pruning, the row-fidelity guard RESTORES ALL
    # COLUMNS for any table whose pruned set would collapse rows -- the only set
    # proven to preserve every physical row (the distinct combination of a
    # table's full column set is its own row set). full_by_name holds the
    # pre-prune fields the guard restores from.
    full_by_name: Dict[str, List[Dict[str, Any]]] = {}
    if keep_fields:
        full_by_name = {tname: flds for tname, flds in tables}
        tables = _prune_table_fields(tables, keep_fields)
        tables, fidelity_warnings = _guard_row_fidelity(
            tables, full_by_name, row_counts,
            qvf_path, engine_url, tenant, api_key, app_id,
            anchors=anchors,
        )
        for w in fidelity_warnings:
            _log.warning(f"  row-fidelity: {w}")

    # The full set of tables we intend to produce (for the post-fetch backstop).
    expected_plan = list(tables)

    # Resolve emit format once; degrade Parquet -> CSV when pyarrow is absent.
    fmt = (emit_format or "csv").lower()
    if fmt == "parquet":
        from .parquet_io import PYARROW_AVAILABLE
        if not PYARROW_AVAILABLE:
            _log.warning(
                "--parquet requested but pyarrow is not installed; "
                "falling back to CSV. `pip install pyarrow` to enable Parquet."
            )
            fmt = "csv"

    # Worker budget. Cloud routes each WebSocket to its own engine pod; cap 8
    # cloud (8 was the measured sweet spot; more adds exclusive-ClearAll
    # contention without speedup). Desktop's single localhost engine is
    # single-threaded -> 2.
    is_cloud = bool(tenant and api_key and app_id)
    cap = _CLOUD_WORKER_CAP if is_cloud else _DESKTOP_WORKER_CAP

    # Worker sizing: a FLAT default of 8 for every cloud app (Desktop 2),
    # via _auto_worker_count -> min(cap, 8). An explicit QLIK_FETCH_WORKERS
    # overrides exactly (still clamped to the cap).
    total_records = sum(int(row_counts.get(tname, 0)) for tname, _ in tables)
    env_raw = os.environ.get("QLIK_FETCH_WORKERS")
    if env_raw is not None and env_raw.strip():
        try:
            workers = max(1, min(int(env_raw), cap))
            _log.info(
                f"Engine extract: QLIK_FETCH_WORKERS={env_raw.strip()} "
                f"-> {workers} worker(s) (cap {cap})."
            )
        except ValueError:
            workers = max(1, min(_DEFAULT_FETCH_WORKERS, cap))
            _log.warning(
                f"QLIK_FETCH_WORKERS={env_raw!r} is not an integer; "
                f"using {workers}."
            )
    elif _FORCE_MAX_WORKERS:
        workers = cap
        _log.info(
            f"Engine extract: workers forced to MAX = {cap} "
            f"(temporary override; ~{total_records:,} records)."
        )
    else:
        workers = _auto_worker_count(total_records, cap)
        _log.info(
            f"Engine extract: using the default {workers} worker(s) "
            f"(~{total_records:,} records across {len(tables)} pool table(s); "
            f"cap {cap}; set QLIK_FETCH_WORKERS to override)."
        )

    if workers <= 1:
        written = _extract_serial(
            tables, output_dir, qvf_path, engine_url, tenant, api_key, app_id,
            emit_format=fmt, anchors=anchors,
        )
    else:
        _log.info(f"Engine extract: using {workers} parallel worker(s).")
        written = _extract_parallel(
            tables, output_dir, qvf_path, engine_url,
            tenant, api_key, app_id, workers, emit_format=fmt,
            row_counts=row_counts, anchors=anchors,
        )

    # Backstop: verify every EXPECTED table landed with its physical row count
    # -- catches a silently-missing table (-> 0 rows in PBI) and a
    # short/collapsed one. Covers both extraction paths.
    for w in _validate_fetched_rows(written, expected_plan, row_counts):
        _log.error(f"  ROW-FIDELITY: {w}")

    _log.info(
        f"Engine extract done: {len(written)}/{len(expected_plan)} "
        f"table(s) written."
    )
    return written


# ---------------------------------------------------------------------------
# Engine-current schema refresh
# ---------------------------------------------------------------------------

def _engine_fields_for_table(
    engine_field_names: List[str],
    engine_field_tags: Dict[str, List[str]],
    table_renames: Dict[str, str],
    original_to_engines: Dict[str, set],
) -> Tuple[List[Dict[str, Any]], int]:
    """Build the resolved ``{name, alias, candidates, trusted, tags}`` field
    list for one engine table. Shared by the loadmodel-matched refresh and the
    engine-only-table reconciliation so both produce identical field shapes.
    Returns ``(fields, renamed_count)``."""
    new_fields: List[Dict[str, Any]] = []
    renamed_count = 0
    for fname in engine_field_names:
        # Bracket-wrap names with chars Qlik's expression parser treats
        # specially (spaces, dots, hyphens, slashes force [Field] syntax).
        bracket = f"[{fname}]" if any(c in fname for c in " .-/") else None
        candidates = [fname]
        if bracket and bracket not in candidates:
            candidates.append(bracket)
        original = table_renames.get(fname, fname)
        if original != fname:
            # Refuse to revert when two engine fields map back to the same
            # original name (would collide into a spurious relationship).
            if len(original_to_engines.get(original, set())) > 1:
                original = fname
            else:
                renamed_count += 1
        new_fields.append({
            "name":       fname,
            "alias":      original,
            "candidates": candidates,
            "trusted":    True,
            "tags":       engine_field_tags.get(fname, []),
        })
    return new_fields, renamed_count


def _refresh_field_lists_from_engine(
    tables: List[Tuple[str, List[Dict[str, str]]]],
    qvf_path: Optional[Path],
    engine_url: str,
    tenant: Optional[str],
    api_key: Optional[str],
    app_id: Optional[str],
    unbuild_dir: Optional[Path] = None,
    field_renames: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[List[Tuple[str, List[Dict[str, str]]]], Dict[str, int]]:
    """Replace each table's loadmodel-derived field list with the
    engine's current (post-script) field list via ``GetTablesAndKeys``.

    Returns ``(tables, row_counts)`` where ``row_counts`` maps each
    loadmodel table name to the engine's physical row count
    (``qNoOfRows``) -- used by the extractor to decide row-range
    parallelism. ``row_counts`` is ``{}`` on any fallback path.

    Matching strategy:
      * Engine table names are compared case-insensitively to the
        loadmodel table alias.
      * When a match is found, the loadmodel field entries are
        REPLACED with one ``{name, alias, candidates}`` entry per
        engine field. ``alias`` is set to the engine field name so
        the CSV header carries the real column name; the loadmodel
        alias is not preserved because the engine name is what the
        downstream Qlik associative join uses.
      * Tables with no engine match (e.g. only loadmodel-defined,
        never reloaded) keep their original loadmodel field list and
        fall back to the probe path in ``resolve_fields``.

    Failures of the GetTablesAndKeys call (no engine, no app, older
    versions) are non-fatal: the original tables are returned and the
    probe path handles per-field resolution.
    """
    client = EngineClient(
        qvf_path=qvf_path,
        engine_base_url=engine_url,
        tenant=tenant,
        api_key=api_key,
        app_id=app_id,
    )
    try:
        client.connect()
    except (OSError, WebSocketException, RuntimeError, socket.timeout) as exc:
        _log.warning(
            f"  could not open engine for schema refresh ({exc}); "
            "falling back to loadmodel field list with probe-based resolution."
        )
        return tables, {}

    try:
        engine_schema = client.get_tables_and_keys_full()
    finally:
        client.close()

    if not engine_schema or not engine_schema.get("tables"):
        _log.warning(
            "  GetTablesAndKeys returned no tables; falling back to "
            "loadmodel field list."
        )
        return tables, {}

    engine_tables: Dict[str, List[str]] = {
        tname: [f["name"] for f in tbl["fields"]]
        for tname, tbl in engine_schema["tables"].items()
    }

    # Bundle the script-derived rename map into the schema so
    # downstream consumers (model.py) can translate qk key records
    # into per-table relationship endpoints.
    if field_renames:
        engine_schema["field_renames"] = field_renames

    # Persist the full engine schema as a sidecar in the unbuild dir.
    # parser.py reads this back into the IR so model.py can use it as
    # the source of truth for table layout, field-to-table mapping,
    # and key-based relationship inference.
    if unbuild_dir is not None:
        try:
            sidecar = Path(unbuild_dir) / "objects" / "engine-schema.json"
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            with open(sidecar, "w", encoding="utf-8") as f:
                json.dump(engine_schema, f, indent=2)
            _log.info(
                f"  wrote engine schema sidecar -> {sidecar} "
                f"({len(engine_schema['tables'])} tables, "
                f"{len(engine_schema.get('keys') or [])} keys, "
                f"{sum(len(v) for v in (field_renames or {}).values())} "
                f"rename mapping(s))"
            )
        except OSError as exc:
            _log.warning(f"  could not write engine schema sidecar: {exc}")

    by_lower = {k.lower(): k for k in engine_tables}
    # Case-insensitive lookup for rename maps -- loadmodel and script
    # don't always agree on the casing of table names.
    renames_by_lower = {
        (k or "").lower(): v
        for k, v in (field_renames or {}).items()
    }
    # Build the global ``original_name -> set(engine_fields)`` map so
    # we can detect ambiguous reverse-renames. When TWO different
    # engine fields both map back to the same original name -- the
    # classic disambiguation case ``[City] AS [HCP.City]`` and
    # ``[City] AS [HCO.City]`` both reverse to ``City`` -- reverting
    # would collide and the converter would mistakenly emit a
    # shared-name relationship between them. We KEEP the engine name
    # in that case. The join-key case (same engine field, different
    # originals per table) is safe: each original has exactly one
    # engine source, so the rule lets both be reverted.
    original_to_engines: Dict[str, set] = {}
    for tbl_renames in (field_renames or {}).values():
        for eng_fld, orig in (tbl_renames or {}).items():
            if eng_fld == orig:
                continue
            original_to_engines.setdefault(orig, set()).add(eng_fld)

    refreshed: List[Tuple[str, List[Dict[str, str]]]] = []
    for table_name, fields in tables:
        engine_key = by_lower.get(table_name.lower())
        if not engine_key:
            _log.info(
                f"  {table_name!r}: no match in engine schema; using "
                f"loadmodel fields ({len(fields)})"
            )
            refreshed.append((table_name, fields))
            continue
        engine_fields = engine_tables[engine_key]
        # Field-name -> Qlik qTags, so the Parquet emit path can type each
        # column from the engine's own judgement instead of guessing.
        engine_field_tags = {
            f.get("name"): (f.get("tags") or [])
            for f in engine_schema["tables"][engine_key]["fields"]
        }
        # Per-table rename map: engine_field -> original_field.
        table_renames = (
            renames_by_lower.get(table_name.lower())
            or renames_by_lower.get(engine_key.lower())
            or {}
        )
        new_fields, renamed_count = _engine_fields_for_table(
            engine_fields, engine_field_tags, table_renames, original_to_engines,
        )
        suffix = f", {renamed_count} renamed to friendlier names" if renamed_count else ""
        _log.info(
            f"  {table_name!r}: refreshed from engine -> "
            f"{len(new_fields)} field(s) (was {len(fields)} in loadmodel{suffix})"
        )
        refreshed.append((table_name, new_fields))

    # Reconcile the plan with the engine's table set: ADD any engine table the
    # loadmodel omits. The loadmodel (the data-model-viewer layout) drops
    # disconnected "island" tables and other tables it doesn't lay out, so a
    # plan built from the loadmodel alone silently SKIPS them -- the table is
    # never fetched and loads as 0 rows / absent in PBI (e.g. a 3-row reference
    # island, the general case of the "entire table not fetched" report).
    # GetTablesAndKeys is authoritative (per CLAUDE.md), so every engine table
    # with fields belongs in the fetch plan.
    matched_keys = {
        by_lower.get(tn.lower()) for tn, _ in tables if by_lower.get(tn.lower())
    }
    added = 0
    for ekey, efields in engine_tables.items():
        if ekey in matched_keys or not efields:
            continue
        etags = {
            f.get("name"): (f.get("tags") or [])
            for f in engine_schema["tables"][ekey]["fields"]
        }
        erenames = renames_by_lower.get(ekey.lower()) or {}
        nf, _rc = _engine_fields_for_table(
            efields, etags, erenames, original_to_engines,
        )
        if nf:
            refreshed.append((ekey, nf))
            added += 1
            _log.info(
                f"  {ekey!r}: engine-only table not in loadmodel -> added to "
                f"fetch plan ({len(nf)} field(s))"
            )
    if added:
        _log.info(
            f"  reconciled fetch plan with engine: added {added} engine-only "
            f"table(s) the loadmodel omitted."
        )

    # Physical row counts (qNoOfRows) per table, for the extractor's row-range
    # split decision. Iterate `refreshed` so engine-only tables are covered too.
    row_counts: Dict[str, int] = {}
    for table_name, _ in refreshed:
        ek = by_lower.get(table_name.lower())
        if ek:
            row_counts[table_name] = int(
                (engine_schema["tables"].get(ek) or {}).get("row_count") or 0)
    return refreshed, row_counts


# ---------------------------------------------------------------------------
# Serial / parallel extraction strategies
# ---------------------------------------------------------------------------

def _extract_one_table(
    table_name: str,
    fields: List[Dict[str, str]],
    output_dir: Path,
    qvf_path: Optional[Path],
    engine_url: str,
    tenant: Optional[str],
    api_key: Optional[str],
    app_id: Optional[str],
    client: Optional["EngineClient"] = None,
    emit_format: str = "parquet",
    part: int = 0,
    nparts: int = 1,
    out_path: Optional[Path] = None,
    resolved: Optional[List[Dict[str, Any]]] = None,
    kinds: Optional[List[str]] = None,
    anchor: Optional[str] = None,
) -> Optional[Path]:
    """Extract a single table (or one row-slice of it) to Parquet/CSV.
    Returns the path on success, None on failure or empty result. Either
    reuses ``client`` (serial mode) or opens its own (parallel mode,
    where each worker thread owns its EngineClient because the WS
    connection is not thread-safe).

    ``part``/``nparts`` fetch one contiguous row-slice (row-range
    parallelism); ``out_path`` overrides the output file (used to write a
    per-slice part file that a later merge concatenates). ``resolved``
    (pre-resolved field list) skips the per-field probe -- shared across a
    table's slices so they don't each re-probe; ``kinds`` forces an
    explicit per-column Parquet kind so all slices share one schema and
    merge cleanly. Defaults = whole table, self-resolved, to its canonical
    path (unchanged behaviour).
    """
    ext = "parquet" if (emit_format or "csv").lower() == "parquet" else "csv"
    csv_path = out_path or (
        output_dir / f"{safe_filename(table_name, max_len=80)}.{ext}")
    owns_client = client is None
    if owns_client:
        client = EngineClient(
            qvf_path=qvf_path,
            engine_base_url=engine_url,
            tenant=tenant,
            api_key=api_key,
            app_id=app_id,
        )
        try:
            client.connect()
        except (OSError, WebSocketException, RuntimeError, socket.timeout) as exc:
            _log.warning(f"  {table_name} -> connect failed: {exc}")
            return None

    try:
        # Probe each loadmodel field against the engine before building
        # the real hypercube. The loadmodel reports both a source-column
        # ``name`` and a post-LOAD ``alias`` per field, and either side
        # may be wrong (the script's `... AS [Alias]` only sometimes
        # propagates to the snapshot). Probing pins each field to the
        # spelling the engine actually accepts -- and drops anything
        # that has no match, so the CSV header column count matches
        # the per-row cell count emitted by ``extract_table``.
        if resolved is None:
            resolved = client.resolve_fields(fields)
        if not resolved:
            _log.warning(
                f"  {table_name} -> no fields resolved against engine; "
                "table will be skipped"
            )
            return None
        dropped = len(fields) - len(resolved)
        if dropped:
            _log.warning(
                f"  {table_name} -> {dropped}/{len(fields)} field(s) "
                "could not be matched to engine; CSV will omit them"
            )
        # Use the hypercube-based extract. This path has caller-
        # controlled column ordering: dimensions are submitted in the
        # exact order of ``resolved``, and the engine returns each
        # row's cells in that same order. Header (``[f["alias"] for f
        # in resolved]``) and data therefore line up by construction.
        #
        # We previously routed through ``Doc.GetTableData`` here, but
        # that API turns out to return ``qValue[]`` in the engine's
        # internal **table layout** order (post-join physical column
        # layout), which does NOT match ``GetTablesAndKeys.qFields``
        # order (the logical field list). Trying to align the two
        # produced CSV files where the header column names and the
        # data cells were in different orders -- e.g. the column
        # labelled ``HCP.ZIP`` actually carried a numeric counter
        # value. The hypercube has no such ambiguity.
        row_iter = client.extract_table(
            resolved, part=part, nparts=nparts, anchor=anchor)
        headers = [f["alias"] for f in resolved]
        if ext == "parquet":
            n = _write_parquet(csv_path, resolved, row_iter, kinds=kinds)
        else:
            n = _write_csv(csv_path, headers, row_iter)
    except (WebSocketException, RuntimeError, OSError) as exc:
        _log.warning(f"  {table_name} -> FAILED ({exc})")
        try:
            if csv_path.exists():
                csv_path.unlink()
        except OSError:
            pass
        gc.collect()
        return None
    finally:
        if owns_client:
            client.close()

    if n > 0:
        _log.info(
            f"  {table_name} -> {csv_path.name} "
            f"({csv_path.stat().st_size} bytes, {n} rows)"
        )
        gc.collect()
        return csv_path
    _log.warning(f"  {table_name} -> empty hypercube; CSV not written")
    try:
        csv_path.unlink()
    except OSError:
        pass
    gc.collect()
    return None


def _extract_serial(
    tables: List[Tuple[str, List[Dict[str, str]]]],
    output_dir: Path,
    qvf_path: Optional[Path],
    engine_url: str,
    tenant: Optional[str],
    api_key: Optional[str],
    app_id: Optional[str],
    emit_format: str = "parquet",
    anchors: Optional[Dict[str, str]] = None,
) -> List[Path]:
    """Extract tables one at a time on a single WebSocket. Preserves
    the original behaviour for QLIK_FETCH_WORKERS<=1.

    ``anchors`` maps table name -> count-anchor engine field (see
    :func:`_select_table_anchors`); a table with an anchor is extracted
    row-faithfully (phantoms dropped, duplicates restored).
    """
    client = EngineClient(
        qvf_path=qvf_path,
        engine_base_url=engine_url,
        tenant=tenant,
        api_key=api_key,
        app_id=app_id,
    )
    try:
        client.connect()
    except (OSError, WebSocketException, RuntimeError, socket.timeout) as exc:
        target = client.url if client else (engine_url or "<unknown>")
        _log.warning(
            f"Cannot reach Qlik engine at {target}: {exc}. "
            "For Desktop mode: make sure Qlik Sense Desktop is running. "
            "For cloud mode: verify the tenant URL and API key. "
            "Continuing with empty-stub partitions."
        )
        return []
    anchors = anchors or {}
    written: List[Path] = []
    try:
        for table_name, fields in tables:
            csv = _extract_one_table(
                table_name, fields, output_dir,
                qvf_path, engine_url, tenant, api_key, app_id,
                client=client, emit_format=emit_format,
                anchor=anchors.get(table_name),
            )
            if csv:
                written.append(csv)
    finally:
        client.close()
    return written


def _probe_split(
    table_name: str,
    fields: List[Dict[str, Any]],
    qvf_path: Optional[Path],
    engine_url: str,
    tenant: Optional[str],
    api_key: Optional[str],
    app_id: Optional[str],
    sample_rows: int = 2000,
) -> Optional[Tuple[List[Dict[str, Any]], int, Optional[List[str]]]]:
    """Probe a split-candidate table on its own connection: resolve fields
    once, read the real ``qcy``, and resolve EVERY column to a concrete
    Parquet kind. Tagged columns come from qTags; an untagged (``auto``)
    column is inferred from a small head sample -- then FIXED, so all
    row-slices write an identical explicit schema and the part files merge
    with no schema conflict (and the typing matches what a serial ``auto``
    extract would pick: numeric stays numeric, misfits become null).

    Returns ``(resolved, qcy, kinds)`` -- ``kinds`` is ``None`` when the
    cube was empty (caller then keeps it a single unit). Returns ``None``
    on any probe failure, so the caller falls back to a normal single,
    self-resolving unit (never worse than today).
    """
    client = EngineClient(
        qvf_path=qvf_path, engine_base_url=engine_url,
        tenant=tenant, api_key=api_key, app_id=app_id,
    )
    try:
        client.connect()
    except (OSError, WebSocketException, RuntimeError, socket.timeout) as exc:
        _log.warning(f"  {table_name}: split probe connect failed ({exc}); serial fallback")
        return None
    handle = None
    try:
        resolved = client.resolve_fields(fields)
        if not resolved:
            return None
        names = [f.get("engine_name") or f["name"] for f in resolved]
        handle, qcy, qcx = client._try_create_cube(names)
        if handle is None or qcy == 0:
            return (resolved, int(qcy or 0), None)
        base = [_field_kind_from_tags(f) for f in resolved]
        kinds: List[str] = list(base)
        if any(k == "auto" for k in base):
            from .parquet_io import _resolve_auto_kind
            m = min(sample_rows, qcy)
            sample = list(client._stream_hypercube_rows(
                handle, qcy, qcx, names, row_start=0, row_end=m))
            for ci, k in enumerate(base):
                if k != "auto":
                    continue
                col = [r[ci] if ci < len(r) else "" for r in sample]
                kinds[ci] = _resolve_auto_kind(col) if col else "string"
        return (resolved, int(qcy), kinds)
    except (RuntimeError, OSError, WebSocketException) as exc:
        _log.warning(f"  {table_name}: split probe failed ({exc}); serial fallback")
        return None
    finally:
        if handle is not None:
            client._destroy_session_object(handle)
        client.close()


def _merge_parquet_parts(part_paths: List[Path], final_path: Path) -> int:
    """Concatenate Parquet part files (identical schemas) into one file by
    streaming row groups -- bounded memory, no full-table load. Returns the
    total row count; deletes the part files on success."""
    import pyarrow.parquet as _pq
    from .parquet_io import _COMPRESSION
    parts = [p for p in part_paths if p and p.exists()]
    if not parts:
        return 0
    total = 0
    writer = None
    try:
        for p in parts:
            pf = _pq.ParquetFile(str(p))
            if writer is None:
                writer = _pq.ParquetWriter(
                    str(final_path), pf.schema_arrow, compression=_COMPRESSION,
                )
            for rg in range(pf.num_row_groups):
                tbl = pf.read_row_group(rg)
                total += tbl.num_rows
                writer.write_table(tbl)
                tbl = None
            pf = None
    finally:
        if writer is not None:
            writer.close()
    for p in parts:
        try:
            p.unlink()
        except OSError:
            pass
    return total


def _own_only_engine_names(
    tables: List[Tuple[str, List[Dict[str, Any]]]],
) -> Set[str]:
    """Engine field names that appear in exactly ONE table across ``tables``.

    Such a field is table-SCOPED: ``Count([F])`` over it counts only that
    table's records and cannot bleed across shared-key associations -- the
    property the count-anchor relies on (and the reason the old ``Count(1)``
    expansion was wrong). Pure; unit-testable without an engine."""
    counts: Dict[str, int] = {}
    for _tname, fields in tables:
        seen: Set[str] = set()
        for f in fields:
            nm = f.get("engine_name") or f.get("name")
            if nm:
                seen.add(nm)
        for nm in seen:
            counts[nm] = counts.get(nm, 0) + 1
    return {nm for nm, c in counts.items() if c == 1}


def _select_table_anchors(
    tables: List[Tuple[str, List[Dict[str, Any]]]],
    row_counts: Dict[str, int],
    qvf_path: Optional[Path],
    engine_url: str,
    tenant: Optional[str],
    api_key: Optional[str],
    app_id: Optional[str],
    max_probes: int = 16,
) -> Dict[str, str]:
    """Choose a row-faithful count-anchor field per table.

    An anchor is an OWN-ONLY engine field ``F`` (see
    :func:`_own_only_engine_names`) for which ``Count([F])`` over the whole app
    equals the table's physical row count (``row_counts``). That equality
    proves ``F`` is non-null on every row, so expanding each distinct
    combination by its per-combo ``Count([F])`` reconstructs exactly the
    physical rows -- dropping orphan-key phantom rows (count 0) and restoring
    exact duplicates (count > 1).

    Returns ``{table_name: anchor_engine_name}``; tables with no qualifying
    field (e.g. a pure all-key link table, or one whose every own field has
    NULLs) are omitted and fall back to the dimension-only extract. Probes at
    most ``max_probes`` own fields per table (declared order) to bound cost --
    a miss is non-fatal (fallback). Any connect failure -> ``{}`` (fallback)."""
    if not _COUNT_ANCHOR or not row_counts:
        return {}
    own = _own_only_engine_names(tables)
    if not own:
        return {}
    client = EngineClient(
        qvf_path=qvf_path, engine_base_url=engine_url,
        tenant=tenant, api_key=api_key, app_id=app_id,
    )
    try:
        client.connect()
    except (OSError, WebSocketException, RuntimeError, socket.timeout) as exc:
        _log.warning(
            f"  count-anchor: probe connect failed ({exc}); "
            "using the dimension-only extract."
        )
        return {}
    anchors: Dict[str, str] = {}
    best_effort = 0
    probed = 0
    try:
        for tname, fields in tables:
            phys = int(row_counts.get(tname, 0) or 0)
            if phys <= 0:
                continue
            cands = []
            for f in fields:
                nm = f.get("engine_name") or f.get("name")
                if nm and nm in own and nm not in cands:
                    cands.append(nm)
            exact = None
            best_nm, best_cnt = None, -1
            for nm in cands[:max_probes]:
                cnt = client.scalar_measure(f"=Count([{nm}])")
                probed += 1
                if cnt is None:
                    continue
                c = int(round(cnt))
                if c == phys:
                    exact = nm
                    break
                if c > best_cnt:
                    best_nm, best_cnt = nm, c
            if exact is not None:
                anchors[tname] = exact          # perfect anchor (the common case)
                continue
            # No own field is non-null on every row. The dimension-only extract
            # then either OVERcounts (orphan-key phantom rows) or UNDERcounts
            # (collapsed duplicates). A count-anchor can never exceed physical,
            # so when the dim-only cube OVERcounts we adopt the highest-count
            # own field as a best-effort anchor: it strips the phantom rows
            # (their count is 0) at the cost of at most the rows where that
            # field is null -- strictly better than fabricating phantom rows.
            # (For a pure collapse with no exact anchor we leave it dim-only and
            # let the post-fetch validation flag it -- the .qvd upload is the
            # lossless remedy.)
            if best_nm is not None and 0 <= best_cnt < phys:
                names = [f.get("engine_name") or f.get("name") for f in fields]
                names = [n for n in names if n]
                handle, dim_qcy, _qcx = (None, 0, 0)
                try:
                    handle, dim_qcy, _qcx = client._try_create_cube(names)
                except (RuntimeError, OSError, WebSocketException):
                    dim_qcy = 0
                finally:
                    if handle is not None:
                        client._destroy_session_object(handle)
                if dim_qcy > phys:               # phantoms present -> anchor wins
                    anchors[tname] = best_nm
                    best_effort += 1
                    _log.info(
                        f"  count-anchor: {tname!r}: no exact anchor; using "
                        f"best-effort [{best_nm}] (Count={best_cnt:,} of "
                        f"{phys:,}) to drop {dim_qcy - phys:,} phantom row(s)."
                    )
    finally:
        client.close()
    extra = f" ({best_effort} best-effort)" if best_effort else ""
    if anchors:
        _log.info(
            f"  count-anchor: anchored {len(anchors)}/{len(tables)} table(s)"
            f"{extra} ({probed} probe(s)); the rest use the dimension-only "
            f"extract."
        )
    else:
        _log.info(
            f"  count-anchor: no per-table anchor found ({probed} probe(s)); "
            "using the dimension-only extract."
        )
    return anchors


def _guard_row_fidelity(
    tables: List[Tuple[str, List[Dict[str, Any]]]],
    full_by_name: Dict[str, List[Dict[str, Any]]],
    row_counts: Dict[str, int],
    qvf_path: Optional[Path],
    engine_url: str,
    tenant: Optional[str],
    api_key: Optional[str],
    app_id: Optional[str],
    anchors: Optional[Dict[str, str]] = None,
) -> Tuple[List[Tuple[str, List[Dict[str, Any]]]], List[str]]:
    """Ensure column pruning never silently drops rows, by RESTORING ALL
    COLUMNS for any pruned table whose kept columns would collapse rows.

    Tables that have a count-anchor (``anchors``) are SKIPPED: their extract
    expands each combination by ``Count([anchor])`` to the physical row count,
    so row fidelity holds regardless of which columns are kept -- and we avoid
    the slow full-width re-fetch of a big pruned fact. The full-column restore
    below is the fallback only for anchorless tables (e.g. pure all-key link
    tables).

    For every pruned table (kept columns < full columns), cheaply probe the
    hypercube's distinct-row count (``GetLayout`` qcy -- no row data pulled)
    over the kept columns. If that falls below the physical row count
    (``row_counts`` from ``GetTablesAndKeys`` qNoOfRows), the kept columns
    don't uniquely identify each physical row, so we RESTORE ALL COLUMNS for
    that table -- the only set proven to preserve every physical row. Returns
    ``(corrected_tables, warnings)``.

    (A faster ``Count(1)``-based count-expansion was tried and reverted: in a
    multi-table model ``Count(1)`` grouped by a SHARED key is not table-scoped,
    so a small table sharing a key with a big one got the big table's row
    count. Restoring all columns is correct because the distinct combination of
    a table's full column set is its own row set, independent of associations.)

    Skipped (returns tables unchanged) when disabled (``QLIK_PRUNE_ROW_GUARD=0``)
    or no physical counts are available. Any probe/connection failure is
    non-fatal: the table is left pruned and the post-fetch validation
    (:func:`_validate_fetched_rows`) remains the backstop.
    """
    if not _ROW_FIDELITY_GUARD or not row_counts:
        return tables, []
    pruned_any = any(
        len(flds) < len(full_by_name.get(tn, flds)) for tn, flds in tables
    )
    if not pruned_any:
        return tables, []

    client = EngineClient(
        qvf_path=qvf_path, engine_base_url=engine_url,
        tenant=tenant, api_key=api_key, app_id=app_id,
    )
    try:
        client.connect()
    except (OSError, WebSocketException, RuntimeError, socket.timeout) as exc:
        _log.warning(
            f"  row-fidelity: collapse probe connect failed ({exc}); "
            "leaving pruning as-is (post-fetch validation will verify)."
        )
        return tables, []

    anchors = anchors or {}
    corrected: List[Tuple[str, List[Dict[str, Any]]]] = []
    warnings: List[str] = []
    try:
        for tname, kept in tables:
            full = full_by_name.get(tname, kept)
            phys = int(row_counts.get(tname, 0))
            if tname in anchors:
                corrected.append((tname, kept))            # count-expansion fixes rows
                continue
            if len(kept) >= len(full) or phys <= 0:
                corrected.append((tname, kept))            # not pruned / unknown
                continue
            names = [f.get("engine_name") or f.get("name") for f in kept]
            names = [n for n in names if n]
            handle = None
            qcy = 0
            try:
                handle, qcy, _qcx = client._try_create_cube(names)
            except (RuntimeError, OSError, WebSocketException):
                qcy = 0
            finally:
                if handle is not None:
                    client._destroy_session_object(handle)
            if qcy and _is_row_collapse(qcy, phys):
                warnings.append(
                    f"{tname}: pruning to {len(kept)} of {len(full)} columns "
                    f"collapses {phys:,} physical rows to {qcy:,} distinct "
                    f"combinations; restoring all {len(full)} columns so every "
                    f"row is fetched."
                )
                corrected.append((tname, full))            # restore full width
            else:
                corrected.append((tname, kept))            # pruned set is safe
    finally:
        client.close()
    return corrected, warnings


def _validate_fetched_rows(
    written: List[Path],
    tables: List[Tuple[str, List[Dict[str, Any]]]],
    row_counts: Dict[str, int],
) -> List[str]:
    """Backstop: verify every EXPECTED table (from the fetch plan) landed with
    its physical row count. Returns warnings for two failure modes that must
    never be silent:

      * **MISSING / empty** -- the table was expected to have rows but no
        non-empty file was written (a failed fetch -> the converter binds an
        empty-stub partition -> the table loads as 0 rows in Power BI). This is
        the "some tables had 0 rows" symptom.
      * **SHORT** -- fewer rows than physical (hypercube distinct-combination
        collapse the fidelity guard couldn't fully prevent, e.g. genuine
        full-row duplicates).

    Cheap -- reads Parquet ``num_rows`` metadata, never the data. Iterates the
    EXPECTED plan (not just what was written), so a table that silently
    vanished is caught. CSV row counts aren't checked (skipped), but a missing
    CSV is still flagged. The remedy for either mode is re-running, lowering
    QLIK_FETCH_WORKERS, or the lossless QVD upload.
    """
    if not row_counts:
        return []
    try:
        import pyarrow.parquet as _pq
    except Exception:  # noqa: BLE001 - pyarrow absent
        _pq = None
    written_by_stem: Dict[str, Path] = {p.stem.lower(): p for p in written}
    warnings: List[str] = []
    for tname, _fields in tables:
        phys = int(row_counts.get(tname, 0))
        if phys <= 0:
            continue   # unknown / legitimately empty -> nothing to assert
        path = written_by_stem.get(safe_filename(tname, max_len=80).lower())
        if path is None:
            warnings.append(
                f"{tname}: NOT fetched (expected {phys:,} rows) -- this table "
                f"will load as 0 rows / empty. Re-run, lower QLIK_FETCH_WORKERS, "
                f"or upload its source .qvd."
            )
            continue
        if path.suffix.lower() != ".parquet" or _pq is None:
            continue   # CSV: presence verified above; row-count check skipped
        try:
            got = int(_pq.ParquetFile(str(path)).metadata.num_rows)
        except Exception:  # noqa: BLE001
            continue
        if got <= 0:
            warnings.append(
                f"{tname}: fetched 0 rows (expected {phys:,}) -- will load empty."
            )
        elif _is_row_collapse(got, phys):
            warnings.append(
                f"{tname}: fetched {got:,} of {phys:,} physical rows "
                f"({100.0 * got / phys:.1f}%) -- the source table has duplicate "
                f"rows the Engine hypercube collapses. Row-grain measures "
                f"(counts/sums) will undercount. For full fidelity, upload this "
                f"table's source .qvd (lossless)."
            )
    return warnings


def _run_unit(unit, output_dir, qvf_path, engine_url,
              tenant, api_key, app_id, emit_format):
    """Run one extraction unit (whole table or one row-slice) on its own fresh
    EngineClient. Shared by the parallel pool and the serial retry pass so both
    go through identical logic. Returns the written path or None."""
    table_name, fields, k, n, out_path, resolved, kinds, anchor = unit
    return _extract_one_table(
        table_name, fields, output_dir, qvf_path, engine_url,
        tenant, api_key, app_id, None, emit_format,
        k, n, out_path, resolved, kinds, anchor,
    )


def _run_units_resilient(units, run_fn, output_dir, ext, row_counts, workers):
    """Run all extraction ``units`` with resilience, returning
    ``(written_paths, hard_failures)``.

    ``run_fn(unit) -> Optional[Path]`` does the actual fetch of one unit (whole
    table or one row-slice); it is injected so this orchestration is unit-
    testable without an engine. The contract that fixes the silent data-loss
    bug:

    1. Run every unit in a thread pool (up to ``workers`` concurrent).
    2. **Serially retry** any unit that failed -- most parallel failures are
       transient pod contention / dead WS, which a no-contention serial retry
       clears. This stops a flaky slice from becoming missing rows.
    3. A split table (n>1) is written **only if ALL n slices are present** --
       never a partial merge. Any table still missing slices (or a single table
       that never produced a file, when it was expected to have rows) is a
       **hard failure**: nothing is written for it and its name is returned, so
       it is reported loudly instead of silently degrading to a 0-row stub.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from collections import defaultdict
    import threading

    row_counts = row_counts or {}
    units_by_key: Dict[Tuple[str, int, int], tuple] = {
        (u[0], u[2], u[3]): u for u in units
    }
    got: Dict[Tuple[str, int, int], Path] = {}

    nworkers = max(1, workers)
    _log.info(
        f"Engine extract: dispatching {len(units)} unit(s) across {nworkers} "
        f"worker thread(s) (interleaved '[fetch-N] start ...' lines below = "
        f"workers running in parallel)."
    )

    def _runner(u):
        # Log the START of each unit FROM ITS WORKER THREAD so the log visibly
        # shows concurrency: with N workers you see up to N '[fetch-N] start'
        # lines before the first completion. (Each worker opens its own engine
        # session, so the '[ENGINE] Cloud app handle -> 1' lines all read
        # "handle 1" -- that's the per-session DOC HANDLE, not a worker count.)
        slice_tag = f" slice {u[2] + 1}/{u[3]}" if u[3] > 1 else ""
        _log.info(f"  [{threading.current_thread().name}] start {u[0]}{slice_tag}")
        return run_fn(u)

    with ThreadPoolExecutor(
        max_workers=nworkers, thread_name_prefix="fetch"
    ) as pool:
        futures = {pool.submit(_runner, u): (u[0], u[2], u[3]) for u in units}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                path = fut.result()
            except Exception as exc:  # noqa: BLE001 - worker died
                _log.warning(
                    f"  {key[0]} (slice {key[1]}/{key[2]}) -> crashed ({exc})"
                )
                path = None
            if path:
                got[key] = path

    # Serial retry of every failed unit (no contention). Tables expected to
    # have rows get 2 attempts; unknown counts get 1; genuinely-empty tables
    # (row_counts == 0) are not retried.
    missing = [u for k, u in units_by_key.items() if k not in got]
    if missing:
        _log.warning(
            f"  {len(missing)} of {len(units)} extraction unit(s) failed under "
            f"parallel load; retrying them serially (no contention)..."
        )
    for u in missing:
        key = (u[0], u[2], u[3])
        phys = int(row_counts.get(u[0], 0))
        max_attempts = 2 if (phys > 0 or u[0] not in row_counts) else 1
        for attempt in range(1, max_attempts + 1):
            try:
                path = run_fn(u)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    f"  serial retry {attempt}/{max_attempts} for {key[0]} "
                    f"(slice {key[1]}/{key[2]}) failed ({exc})"
                )
                path = None
            if path:
                got[key] = path
                _log.info(
                    f"  recovered {key[0]} (slice {key[1]}/{key[2]}) on "
                    f"serial retry {attempt}."
                )
                break

    # Assemble: split tables need ALL slices; never write a partial.
    n_by_table: Dict[str, int] = {}
    parts_by_table: Dict[str, Dict[int, Path]] = defaultdict(dict)
    single_path: Dict[str, Optional[Path]] = {}
    for (table_name, k, n), _u in units_by_key.items():
        n_by_table[table_name] = n
        if n == 1:
            single_path[table_name] = got.get((table_name, k, n))
        elif (table_name, k, n) in got:
            parts_by_table[table_name][k] = got[(table_name, k, n)]

    written: List[Path] = []
    hard_failures: List[str] = []
    for table_name, n in n_by_table.items():
        final_path = output_dir / f"{safe_filename(table_name, max_len=80)}.{ext}"
        phys = int(row_counts.get(table_name, 0))
        if n == 1:
            p = single_path.get(table_name)
            if p:
                written.append(p)
            elif phys > 0:
                hard_failures.append(table_name)
            continue
        parts = parts_by_table.get(table_name, {})
        if len(parts) < n:
            _log.error(
                f"  {table_name}: only {len(parts)}/{n} row-slices fetched; "
                f"refusing to write a PARTIAL table. Cleaning up part files."
            )
            for p in parts.values():
                try:
                    p.unlink()
                except OSError:
                    pass
            hard_failures.append(table_name)
            continue
        ordered = [parts[k] for k in sorted(parts)]
        try:
            total = _merge_parquet_parts(ordered, final_path)
        except Exception as exc:  # noqa: BLE001
            _log.error(f"  {table_name} -> part merge FAILED ({exc})")
            hard_failures.append(table_name)
            continue
        if total > 0:
            _log.info(
                f"  {table_name} -> {final_path.name} "
                f"({n} slices merged, {total:,} rows)"
            )
            written.append(final_path)
        elif phys > 0:
            hard_failures.append(table_name)

    return written, hard_failures


def _extract_parallel(
    tables: List[Tuple[str, List[Dict[str, str]]]],
    output_dir: Path,
    qvf_path: Optional[Path],
    engine_url: str,
    tenant: Optional[str],
    api_key: Optional[str],
    app_id: Optional[str],
    workers: int,
    emit_format: str = "parquet",
    row_counts: Optional[Dict[str, int]] = None,
    anchors: Optional[Dict[str, str]] = None,
) -> List[Path]:
    """Extract tables concurrently, one WebSocket per worker thread.

    Work is a flat list of UNITS: a small table is one unit (whole table);
    a big table (probed via ``_probe_split``) is split into N row-range
    slices, each its own unit writing a part file, merged afterwards. So a single
    huge table -- which the old across-tables-only pool fetched on ONE
    worker -- now uses the whole worker budget. The WS isn't thread-safe,
    so each unit owns its EngineClient; the work is I/O-bound (engine +
    JSON), so threads are the right model.
    """
    row_counts = row_counts or {}
    anchors = anchors or {}
    ext = "parquet" if (emit_format or "csv").lower() == "parquet" else "csv"
    do_split = ext == "parquet" and workers > 1

    # Plan units. unit = (table, fields, part_k, n_parts, out_path,
    # resolved, kinds). A big table is probed once (real qcy + concrete
    # per-column kinds shared across its slices); small tables stay a
    # single self-resolving unit. The probe is the only serial step and
    # runs just for split candidates (typically the 1-2 dominant tables).
    units = []
    for table_name, fields in tables:
        est = int(row_counts.get(table_name, 0))
        n, resolved, kinds, qcy = 1, None, None, est
        # Probe (one serial connection) only tables the metadata cell estimate
        # says are worth splitting; the real slice count is recomputed from the
        # probed qcy + resolved column count.
        if do_split and _plan_split_n(est, len(fields), workers) > 1:
            probe = _probe_split(
                table_name, fields, qvf_path, engine_url,
                tenant, api_key, app_id,
            )
            if probe is not None:
                resolved, qcy, kinds = probe
                if kinds is not None:
                    n = _plan_split_n(qcy, len(resolved or fields), workers)
        safe = safe_filename(table_name, max_len=80)
        anchor = anchors.get(table_name)
        if n == 1:
            units.append((table_name, fields, 0, 1, None, resolved, kinds, anchor))
        else:
            _log.info(
                f"  {table_name}: {qcy:,} rows -> {n} parallel row-slices"
            )
            for k in range(n):
                part_path = output_dir / f"{safe}.part{k:02d}of{n:02d}.parquet"
                units.append((table_name, fields, k, n, part_path, resolved, kinds, anchor))

    written, hard_failures = _run_units_resilient(
        units,
        lambda u: _run_unit(u, output_dir, qvf_path, engine_url,
                            tenant, api_key, app_id, emit_format),
        output_dir, ext, row_counts, workers,
    )
    if hard_failures:
        _log.error(
            "ENGINE FETCH INCOMPLETE -- the following table(s) could NOT be "
            "fully fetched and were NOT written (so their data is missing, not "
            "silently partial): " + ", ".join(sorted(hard_failures)) + ". "
            "Re-run the conversion, lower QLIK_FETCH_WORKERS to reduce engine "
            "load, or upload these tables' source .qvd files (lossless)."
        )
    return written


# ---------------------------------------------------------------------------
# Loadmodel -> per-table extraction plan
# ---------------------------------------------------------------------------

def _extract_table_field_pairs(
    load_model: Dict[str, Any],
) -> List[Tuple[str, List[Dict[str, str]]]]:
    """Pull (display_name, fields) pairs out of the loadmodel.

    ``fields`` is a list of ``{name, alias, candidates}`` dicts:

    * ``name``       - first candidate; the spelling we'd default to
                       if probing is skipped.
    * ``alias``      - the display label written to the CSV header.
    * ``candidates`` - every plausible engine spelling for this field
                       (alias, name, dotted-id leaf, plus bracket-
                       wrapped variants for names with special chars).
                       :meth:`EngineClient.resolve_fields` probes each
                       in turn and pins ``engine_name`` to the one the
                       engine actually accepts.
    """
    out: List[Tuple[str, List[Dict[str, str]]]] = []
    for tbl in load_model.get("tables", []) or []:
        alias = (
            tbl.get("tableAlias")
            or tbl.get("tableName")
            or tbl.get("id")
            or ""
        ).strip()
        if not alias:
            continue
        fields: List[Dict[str, str]] = []
        seen: set[str] = set()
        for fld in tbl.get("fields", []) or []:
            raw_name = (fld.get("name") or "").strip()
            raw_alias = (fld.get("alias") or "").strip()
            raw_id = (fld.get("id") or "").strip()
            # Display label for the CSV header. Prefer the alias (the
            # user-facing label in Qlik's data model) so the column
            # name in PBI matches what the Qlik author named it.
            display = raw_alias or raw_name or raw_id
            if not display or display in seen:
                continue
            seen.add(display)

            # Loadmodel inconsistencies mean we cannot pick a single
            # "correct" engine field name up front:
            #   * For `[City] AS [HCO.City]` the engine field is the
            #     ALIAS (`HCO.City`); `City` does not exist.
            #   * For `[HCO_ID]` (no rename) the engine field is the
            #     NAME (`HCO_ID`); the loadmodel may report a qualified
            #     `HCO.HCO_ID` alias that does NOT exist as a field.
            # So we enumerate every plausible spelling and let
            # `resolve_fields` probe each one to find the candidate the
            # engine actually accepts.
            id_leaf = raw_id.rsplit(".", 1)[-1] if "." in raw_id else ""
            candidates: List[str] = []
            for raw in (raw_alias, raw_name, id_leaf):
                if not raw:
                    continue
                if raw not in candidates:
                    candidates.append(raw)
                bracketed = f"[{raw}]"
                if any(c in raw for c in " .-/") and bracketed not in candidates:
                    candidates.append(bracketed)
            if not candidates:
                continue
            field_rec: Dict[str, Any] = {
                "name":       candidates[0],
                "alias":      display,
                "candidates": candidates,
            }
            fields.append(field_rec)
        if fields:
            out.append((alias, fields))
    return out


# ---------------------------------------------------------------------------
# WebSocket JSON-RPC client
# ---------------------------------------------------------------------------

class EngineClient:
    """Minimal Qlik Engine JSON-RPC client.

    Supports two transports, picked from constructor args:

    **Desktop / local engine** (``qvf_path`` given, ``api_key`` not):

    1. Global endpoint ``ws://localhost:4848/app/engineData`` followed
       by ``OpenDoc(<absolute path>)``. The returned handle is used
       for every subsequent call.
    2. Path-style ``ws://localhost:4848/app/<encoded-path>`` as a
       fallback for Desktop versions that don't accept OpenDoc on
       the global endpoint.

    **Qlik Cloud** (``tenant`` + ``api_key`` + ``app_id`` given):

    ``wss://<tenant>/app/<app-id>`` with an
    ``Authorization: Bearer <api_key>`` header. The path-style URL
    routes the socket to the right engine pod but does NOT
    deterministically auto-open the doc at a known handle, so
    ``connect()`` follows up with ``GetActiveDoc`` (fast path) or
    ``OpenDoc`` (fallback) to resolve the real handle. All
    subsequent JSON-RPC calls then look identical to Desktop.

    The two modes are mutually exclusive in any one instance; the
    class is intentionally stateless across calls so the same client
    can be instantiated freshly for each conversion in a server
    deployment.
    """

    def __init__(
        self,
        qvf_path: Optional[Path] = None,
        engine_base_url: str = DEFAULT_ENGINE_URL,
        tenant: Optional[str] = None,
        api_key: Optional[str] = None,
        app_id: Optional[str] = None,
    ):
        self.qvf_path: Optional[Path] = (
            Path(qvf_path).resolve() if qvf_path else None
        )
        self.tenant: Optional[str] = (tenant or "").strip() or None
        self.api_key: Optional[str] = (api_key or "").strip() or None
        self.app_id: Optional[str] = (app_id or "").strip() or None
        self.is_cloud = bool(self.tenant and self.api_key and self.app_id)

        if self.is_cloud:
            # Cloud: wss://<tenant>/app/<app-id>
            base = self.tenant.rstrip("/")
            if base.startswith("http://"):
                base = "ws://" + base[len("http://"):]
            elif base.startswith("https://"):
                base = "wss://" + base[len("https://"):]
            elif not base.startswith(("ws://", "wss://")):
                base = "wss://" + base
            self.url = f"{base}/app/{self.app_id}"
            self.path_url: Optional[str] = None
            self.headers: List[str] = [
                f"Authorization: Bearer {self.api_key}",
            ]
        else:
            if not self.qvf_path:
                raise ValueError(
                    "EngineClient requires either qvf_path (local mode) "
                    "or tenant+api_key+app_id (cloud mode)."
                )
            base = engine_base_url.rstrip("/")
            self.url = f"{base}/app/engineData"
            path_for_engine = str(self.qvf_path).replace("\\", "/")
            encoded = urllib.parse.quote(path_for_engine, safe="")
            self.path_url = f"{base}/app/{encoded}"
            self.headers = []

        self.ws: Optional[WebSocket] = None
        self._next_id = 1
        self.app_handle: int = -1  # filled in by connect()

    # ------------------------------------------------------------------
    def connect(self) -> None:
        if create_connection is None:
            raise RuntimeError(
                "websocket-client is not installed "
                "(pip install websocket-client)."
            )

        if self.is_cloud:
            # Cloud: single URL with bearer auth.
            _log.info(f"Connecting to cloud engine: {self.url}")
            try:
                self.ws = create_connection(
                    self.url, timeout=30, header=self.headers,
                )
            except (OSError, WebSocketException, socket.timeout) as exc:
                raise RuntimeError(
                    f"Cannot reach cloud engine at {self.url}: {exc}"
                ) from exc
            self._drain_notifications()
            # Resolve the actual app handle. The path-style URL routes
            # the socket to the right engine pod but does NOT auto-open
            # the doc deterministically across engine versions, so we
            # have to ask. Try GetActiveDoc first (cheap, no side
            # effects); fall back to OpenDoc (idempotent against an
            # already-open doc).
            self.app_handle = self._resolve_cloud_app_handle()
            _log.info(f"Cloud app handle -> {self.app_handle}")
            # Reset any inherited selection state -- see
            # ``_clear_all_selections`` docstring for why.
            self._clear_all_selections()
            return

        # Local Desktop path: try global endpoint + OpenDoc first,
        # fall back to path-style URL.
        _log.info(f"Connecting to engine: {self.url}")
        try:
            self.ws = create_connection(self.url, timeout=30)
        except (OSError, WebSocketException, socket.timeout) as exc:
            raise RuntimeError(str(exc)) from exc
        self._drain_notifications()

        try:
            result = self.request(
                "OpenDoc", -1,
                [str(self.qvf_path).replace("\\", "/")],
            )
            handle = ((result.get("qReturn") or {}).get("qHandle"))
            if isinstance(handle, int) and handle > 0:
                self.app_handle = handle
                _log.info(f"Opened doc via OpenDoc -> handle {handle}")
                # Reset any inherited selection state.
                self._clear_all_selections()
                return
        except RuntimeError as exc:
            _log.info(
                f"OpenDoc on global endpoint failed: {exc}. "
                "Falling back to path-style URL."
            )

        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

        _log.info(f"Connecting to engine (path-style): {self.path_url}")
        try:
            self.ws = create_connection(self.path_url, timeout=30)
        except (OSError, WebSocketException, socket.timeout) as exc:
            raise RuntimeError(
                f"Cannot reach engine at {self.path_url}: {exc}"
            ) from exc
        self._drain_notifications()
        self.app_handle = 1
        # Path-style fallthrough: also clear inherited selection state.
        self._clear_all_selections()

    def _clear_all_selections(self) -> None:
        """Reset every selection / lock on the opened doc, in every
        alternate state.

        Why this matters: when a QVF is saved with selections active
        (typically because the author saved while a bookmark was
        applied), the engine restores that selection state when the
        doc is opened. Subsequent ``GetHyperCubeData`` / data-extract
        calls then return only the rows that pass the saved filter.
        For the converter that means the CSV / model has partial
        data, and PBI bookmarks (which only encode visual-layer
        filter state, not data-layer filters) cannot recover the
        full dataset for the "no filters" bookmark.

        ``Doc.ClearAll(qLockedAlso=true, qStateName="")`` clears
        every selection including locked ones in the default state,
        which is what we want -- the data extract must reflect the
        whole table, not a filtered slice.

        Failures here are non-fatal: an older engine or restricted
        app may reject the call. We log and continue; the worst case
        is we re-introduce the inherited-selection issue, but the
        rest of the extract still runs.
        """
        if self.app_handle is None or self.app_handle < 0:
            return
        try:
            self.request("ClearAll", self.app_handle, [True, ""])
        except RuntimeError as exc:
            _log.info(
                f"ClearAll(qLockedAlso=true) failed: {exc}; "
                "extracts may inherit the doc's saved selection state"
            )

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    # ------------------------------------------------------------------
    def _resolve_cloud_app_handle(self) -> int:
        """Return the cloud app's doc handle.

        Path-style URLs do not auto-open the doc reliably across engine
        versions. The two callable resolutions:

        1. ``GetActiveDoc(-1, [])`` -- returns the auto-opened doc if
           the engine did open it on connect. Cheap, no side effects.
        2. ``OpenDoc(-1, [app_id])`` -- explicit open; idempotent
           against an already-open doc (returns the existing handle).

        Either works; we try both because some cloud regions reject
        ``GetActiveDoc`` with permission errors when the auto-open
        hasn't happened.
        """
        try:
            res = self.request("GetActiveDoc", -1, [])
            handle = ((res.get("qReturn") or {}).get("qHandle"))
            if isinstance(handle, int) and handle >= 0:
                return handle
        except RuntimeError as exc:
            _log.info(
                f"GetActiveDoc failed ({exc}); falling back to OpenDoc."
            )

        try:
            res = self.request("OpenDoc", -1, [self.app_id])
            handle = ((res.get("qReturn") or {}).get("qHandle"))
            if isinstance(handle, int) and handle >= 0:
                return handle
        except RuntimeError as exc:
            raise RuntimeError(
                "Could not resolve cloud app handle. WebSocket "
                "connection succeeded but the engine refused both "
                "GetActiveDoc and OpenDoc. Verify the app id is "
                "correct and that the API key has read access to "
                f"this app. Underlying error: {exc}"
            ) from exc

        raise RuntimeError(
            "Cloud connection succeeded but no app handle was "
            "returned by either GetActiveDoc or OpenDoc."
        )

    # ------------------------------------------------------------------
    def request(self, method: str, handle: int, params: Any) -> Dict[str, Any]:
        """Send one JSON-RPC call, drop notifications, return ``result``.

        Raises ``RuntimeError`` if the engine returns an error object.
        """
        if self.ws is None:
            raise RuntimeError("Engine client is not connected.")

        rid = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id":      rid,
            "method":  method,
            "handle":  handle,
            "params":  params,
        }
        self.ws.send(json.dumps(payload))
        while True:
            raw = self.ws.recv()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                raw = None
                continue
            # Free the raw JSON string as soon as we have the parsed
            # object. For large GetHyperCubeData responses `raw` can be
            # tens of MB; without this it stays alive on the local frame
            # for the duration of the caller's processing of `msg`.
            raw = None
            # Engine pushes change notifications mid-stream
            # (qInvalidated, OnAuthenticationInformation, etc.) -
            # always skip anything that isn't OUR response.
            if msg.get("id") != rid:
                msg = None
                continue
            err = msg.get("error")
            if err:
                raise RuntimeError(
                    f"Engine error on {method}: "
                    f"{err.get('message') or err} (code {err.get('code')})"
                )
            result = msg.get("result", {}) or {}
            msg = None
            return result

    def _drain_notifications(self) -> None:
        """Non-blocking pre-roll drain of engine notifications.

        Uses the underlying socket's timeout to bail as soon as the
        engine has nothing pending. Anything received here can only be
        a notification (we haven't sent any request yet), so we just
        discard it.
        """
        if self.ws is None:
            return
        sock = self.ws.sock
        original_timeout = sock.gettimeout() if sock else None
        try:
            if sock:
                sock.settimeout(0.25)
            while True:
                try:
                    raw = self.ws.recv()
                except (socket.timeout, WebSocketException):
                    return
                if not raw:
                    return
        finally:
            if sock and original_timeout is not None:
                sock.settimeout(original_timeout)

    # ------------------------------------------------------------------
    # Engine-current data model snapshot
    # ------------------------------------------------------------------

    def get_tables_and_keys(self) -> Dict[str, List[str]]:
        """Return ``{table_name: [field_name, ...]}`` for the engine's
        CURRENT data model.

        Convenience wrapper around :meth:`get_tables_and_keys_full`
        that flattens the full response down to a simple table->fields
        map. Use the ``_full`` variant when you also need key records,
        field tags, or qSrcTables cross-references for relationship
        inference.
        """
        full = self.get_tables_and_keys_full()
        out: Dict[str, List[str]] = {}
        for tname, tbl in full.get("tables", {}).items():
            out[tname] = [f["name"] for f in tbl["fields"]]
        return out

    def get_tables_and_keys_full(self) -> Dict[str, Any]:
        """Return the engine's CURRENT data-model snapshot.

        This is the authoritative data-model source -- it reflects the
        engine state AFTER the load script's autogenerated section has
        run, including:

          * synthesised keys (``[HCP_ID] AS [From_HCP_ID-HCP_ID]``)
          * script-added columns (``GeoMakePoint(...) AS ...``)
          * renamed aliases (``[City] AS [HCP.City]``)
          * DROP'd source fields are absent

        Shape::

            {
              "tables": {
                <table_name>: {
                  "fields": [
                    {"name": <str>, "key_type": <str>, "tags": [<str>]},
                    ...
                  ],
                  "row_count": <int>,
                },
                ...
              },
              "keys": [
                {"key_fields": [<field_name>, ...],
                 "tables": [<table_name>, ...]},
                ...
              ],
            }

        The ``keys`` array comes from the engine's ``qk`` source-key
        records: the engine's own statement of which fields are join
        keys and which tables they appear in. This is far more reliable
        than guessing relationships from shared column names.

        Returns ``{}`` if the call fails (older engine versions, or
        an app that has never been reloaded).
        """
        try:
            result = self.request(
                "GetTablesAndKeys",
                self.app_handle,
                [
                    {"qcx": 1000, "qcy": 1000},  # qWindowSize
                    {"qcx": 0,    "qcy": 0},     # qNullSize
                    30,                          # qCellHeight
                    False,                       # qSyntheticMode -- expose
                                                 # natural relations, not the
                                                 # engine's synthetic-key
                                                 # internals
                    False,                       # qIncludeSysVars
                ],
            )
        except RuntimeError as exc:
            _log.warning(f"GetTablesAndKeys failed: {exc}")
            return {}

        tables_out: Dict[str, Dict[str, Any]] = {}
        for tbl in result.get("qtr") or []:
            tname = (tbl.get("qName") or "").strip()
            if not tname:
                continue
            fields_out: List[Dict[str, Any]] = []
            for fld in tbl.get("qFields") or []:
                fname = (fld.get("qName") or "").strip()
                if not fname:
                    continue
                fields_out.append({
                    "name":     fname,
                    "key_type": (fld.get("qKeyType") or "NOT_KEY").strip(),
                    "tags":     list(fld.get("qTags") or []),
                    "is_hidden": bool(fld.get("qIsHidden") or False),
                    "is_system": bool(fld.get("qIsSystem") or False),
                    "is_semantic": bool(fld.get("qIsSemantic") or False),
                })
            tables_out[tname] = {
                "fields":    fields_out,
                "row_count": int(tbl.get("qNoOfRows") or 0),
            }

        keys_out: List[Dict[str, Any]] = []
        for k in result.get("qk") or []:
            key_fields = [
                (f or "").strip()
                for f in (k.get("qKeyFields") or [])
                if (f or "").strip()
            ]
            tables_for_key = [
                (t or "").strip()
                for t in (k.get("qTables") or [])
                if (t or "").strip()
            ]
            if key_fields and tables_for_key:
                keys_out.append({
                    "key_fields": key_fields,
                    "tables":     tables_for_key,
                })

        return {"tables": tables_out, "keys": keys_out}

    # ------------------------------------------------------------------
    # Per-field resolution via engine probes
    # ------------------------------------------------------------------

    def _field_exists(self, field_name: str) -> bool:
        """Return True if ``field_name`` resolves to a real Qlik field.

        Creates a throw-away 1-dim hypercube and inspects ``qcx``. A
        non-existent field is silently dropped by the engine, so the
        cube comes back with ``qcx == 0``.
        """
        cube_def = {
            "qInfo": {"qType": "qlik2pbi-probe"},
            "qHyperCubeDef": {
                "qDimensions": [{
                    "qDef": {"qFieldDefs": [field_name]},
                    "qNullSuppression": False,
                }],
                "qMeasures": [],
                "qInitialDataFetch": [{
                    "qLeft": 0, "qTop": 0, "qWidth": 1, "qHeight": 1,
                }],
                "qSuppressZero":    False,
                "qSuppressMissing": False,
            },
        }
        try:
            result = self.request(
                "CreateSessionObject", self.app_handle, [cube_def],
            )
        except RuntimeError:
            return False
        obj_handle = ((result.get("qReturn") or {}).get("qHandle"))
        if not isinstance(obj_handle, int):
            return False
        try:
            layout = self.request("GetLayout", obj_handle, [])
            hc = ((layout.get("qLayout") or {}).get("qHyperCube") or {})
            qcx = int((hc.get("qSize") or {}).get("qcx") or 0)
            return qcx >= 1
        except RuntimeError:
            return False
        finally:
            self._destroy_session_object(obj_handle)

    def resolve_fields(
        self, fields: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Pin each field to a candidate the engine accepts.

        Returns a new list of field dicts with ``engine_name`` set to
        the candidate that resolved. Fields where no candidate resolves
        are dropped from the result (logged at WARNING level) so the
        CSV header / cube width stay consistent with the actual data.
        """
        resolved: List[Dict[str, Any]] = []
        for f in fields:
            # Fast path: GetTablesAndKeys already confirmed this field
            # exists -- no probe needed, saving one round-trip per field
            # per table on large schemas.
            if f.get("trusted"):
                resolved.append({**f, "engine_name": f["name"]})
                continue
            candidates = f.get("candidates") or [f.get("name") or f.get("alias")]
            engine_name = None
            tried: List[str] = []
            for cand in candidates:
                if not cand or cand in tried:
                    continue
                tried.append(cand)
                if self._field_exists(cand):
                    engine_name = cand
                    break
            if engine_name is None:
                _log.warning(
                    f"    field {f.get('alias')!r}: no engine match "
                    f"(tried {tried}); column will be omitted"
                )
                continue
            resolved.append({**f, "engine_name": engine_name})
        return resolved

    # ------------------------------------------------------------------
    # Extract one table by listing every field as a hypercube dimension.
    # This is the single extract path: it gives us caller-controlled
    # column ordering (dimensions are submitted in caller order; the
    # engine returns cells in that same order) so the CSV header /
    # data alignment is guaranteed by construction.
    # ------------------------------------------------------------------

    def extract_table(
        self,
        fields: List[Dict[str, str]],
        part: int = 0,
        nparts: int = 1,
        anchor: Optional[str] = None,
    ) -> Iterator[List[str]]:
        """Yield rows for ``fields`` by paginating GetHyperCubeData.

        ``part``/``nparts`` select a contiguous row slice for row-range
        parallelism: slice ``part`` of ``nparts`` is rows
        ``[part*qcy//nparts, (part+1)*qcy//nparts)``. Every worker creates
        an identical cube def, so all see the same ``qcy`` and the slices
        partition ``[0, qcy)`` exactly. ``nparts=1`` (default) = whole
        table, byte-identical to the pre-parallelism behaviour. Row order
        is immaterial for a Power BI Import model, so concatenating slices
        reconstructs the table correctly regardless of order.

        Strategy:

        1. ``CreateSessionObject`` with a hypercube whose
           ``qDimensions`` are exactly one entry per field. No
           measures, no suppression, no totals -- we want the raw
           cross-product the engine evaluates on the in-memory data
           model.
        2. ``GetLayout`` to learn the total row count
           (``qHyperCube.qSize.qcy``).
        3. Loop ``GetHyperCubeData`` with rectangles of
           ``min(MAX_ROWS_PER_CALL, MAX_CELLS_PER_CALL // qcx)`` rows
           until every row has been streamed.
        4. ``DestroySessionObject`` to release engine-side memory
           before the next table's cube is created -- without this,
           every table extracted in one session leaves its hypercube
           live in the engine for the lifetime of the WebSocket.
        """
        if not fields:
            return

        # ``fields`` here is the resolved list from ``resolve_fields`` --
        # every entry carries an ``engine_name`` that has already been
        # confirmed to exist in the engine, so the cube creation below
        # cannot silently drop dimensions. The number of cells per row
        # in the yielded matrix will equal ``len(fields)``.
        names = [f.get("engine_name") or f["name"] for f in fields]
        n_dims = len(names)
        obj_handle, qcy, qcx = self._try_create_cube(names, anchor=anchor)
        if obj_handle is None:
            return
        if qcy == 0:
            _log.warning(
                "    hypercube returned no rows; table will be empty "
                "(app may not have been reloaded)"
            )
            self._destroy_session_object(obj_handle)
            return
        expected_qcx = n_dims + (1 if anchor else 0)
        if qcx != expected_qcx:
            # Should not happen now that resolve_fields probed each
            # field, but log defensively so a future regression is
            # visible rather than silent.
            _log.warning(
                f"    cube width mismatch: requested {expected_qcx} col(s), "
                f"engine returned {qcx}. Some columns may be misaligned."
            )
        nparts = max(1, int(nparts))
        part = max(0, min(int(part), nparts - 1))
        row_start = (part * qcy) // nparts
        row_end = qcy if nparts == 1 else ((part + 1) * qcy) // nparts
        try:
            yield from self._stream_hypercube_rows(
                obj_handle, qcy, qcx, names,
                row_start=row_start, row_end=row_end,
                n_dims=n_dims, anchor=anchor,
            )
        finally:
            self._destroy_session_object(obj_handle)

    def _try_create_cube(
        self, field_names: List[str], anchor: Optional[str] = None,
    ) -> Tuple[Optional[int], int, int]:
        """Create a session-object hypercube for ``field_names``.

        Returns ``(handle, qcy, qcx)`` -- handle is None if creation
        itself failed (treat as a fatal error for that attempt), and
        ``qcy`` is 0 if the cube was created but yielded no rows. The
        caller is responsible for destroying the session object.

        ``anchor`` (an engine field name native to this table) turns the
        cube into a row-faithful extract: it appends a single
        ``=Count([anchor])`` measure and sets ``qSuppressZero`` so that
        (a) phantom rows from orphan members of a SHARED key are dropped
        (their count is 0) and (b) the caller can expand each distinct
        combination by its count to restore exact-duplicate physical rows.
        ``qcx`` then includes the measure column (``n_dims + 1``); the
        caller knows ``n_dims = len(field_names)``. ``anchor=None`` keeps
        the original dimension-only cube byte-for-byte.
        """
        n_dims = len(field_names)
        measures = (
            [{"qDef": {"qDef": f"=Count([{anchor}])"}}] if anchor else []
        )
        cube_def = {
            "qInfo": {"qType": "qlik2pbi-extract"},
            "qHyperCubeDef": {
                "qDimensions": [
                    {
                        "qDef": {"qFieldDefs": [name]},
                        "qNullSuppression": False,
                    }
                    for name in field_names
                ],
                "qMeasures": measures,
                "qInitialDataFetch": [{
                    "qLeft":   0,
                    "qTop":    0,
                    "qWidth":  n_dims + len(measures),
                    "qHeight": 1,
                }],
                "qSuppressZero":    bool(anchor),
                "qSuppressMissing": False,
            },
        }
        # CreateSessionObject + GetLayout, with retry on a TRANSIENT abort.
        # Under parallel load several workers connect to the same cloud app
        # session and each runs an exclusive Doc.ClearAll; one worker's
        # exclusive op aborts another's in-flight request -- "Request aborted
        # (... Exclusive/BeginExclusive ... family requests) (code 15)". That is
        # transient: a short backoff lets the exclusive burst drain and the
        # retry succeeds. Retrying here (at the source) stops these from
        # surfacing as a spurious "empty hypercube" and a heavier unit-level
        # serial retry. A NON-transient rejection (bad field, real error) is
        # returned immediately as before.
        import time as _time
        last_exc: Optional[Exception] = None
        for attempt in range(1, _CUBE_CREATE_ATTEMPTS + 1):
            try:
                result = self.request(
                    "CreateSessionObject", self.app_handle, [cube_def],
                )
            except RuntimeError as exc:
                if _is_transient_abort(exc) and attempt < _CUBE_CREATE_ATTEMPTS:
                    last_exc = exc
                    _time.sleep(_CUBE_RETRY_BACKOFF * attempt)
                    continue
                _log.info(f"    CreateSessionObject rejected: {exc}")
                return None, 0, 0
            obj_handle = ((result.get("qReturn") or {}).get("qHandle"))
            result = None
            if not isinstance(obj_handle, int):
                return None, 0, 0
            try:
                layout = self.request("GetLayout", obj_handle, [])
            except RuntimeError as exc:
                self._destroy_session_object(obj_handle)
                if _is_transient_abort(exc) and attempt < _CUBE_CREATE_ATTEMPTS:
                    last_exc = exc
                    _time.sleep(_CUBE_RETRY_BACKOFF * attempt)
                    continue
                _log.info(f"    GetLayout rejected: {exc}")
                return None, 0, 0
            hc = ((layout.get("qLayout") or {}).get("qHyperCube") or {})
            qsize = hc.get("qSize") or {}
            qcy = int(qsize.get("qcy") or 0)
            qcx = int(qsize.get("qcx") or n_dims)
            if attempt > 1:
                _log.info(f"    cube created on attempt {attempt} (transient abort cleared)")
            _log.info(f"    hypercube: {qcy} rows x {qcx} cols")
            return obj_handle, qcy, qcx
        _log.info(f"    cube create gave up after transient aborts: {last_exc}")
        return None, 0, 0

    def scalar_measure(self, expr: str) -> Optional[float]:
        """Evaluate one scalar measure over the whole app (no dimensions,
        no selection), e.g. ``=Count([Field])``. Returns the numeric value or
        ``None`` on any failure. Cheap (one CreateSessionObject + GetLayout
        with a 1x1 fetch); used to pick and validate a table's count-anchor.
        """
        cube_def = {
            "qInfo": {"qType": "qlik2pbi-scalar"},
            "qHyperCubeDef": {
                "qDimensions": [],
                "qMeasures": [{"qDef": {"qDef": expr}}],
                "qInitialDataFetch": [{
                    "qLeft": 0, "qTop": 0, "qWidth": 1, "qHeight": 1,
                }],
                "qSuppressZero": False,
                "qSuppressMissing": False,
            },
        }
        try:
            result = self.request(
                "CreateSessionObject", self.app_handle, [cube_def],
            )
        except RuntimeError:
            return None
        obj_handle = ((result.get("qReturn") or {}).get("qHandle"))
        if not isinstance(obj_handle, int):
            return None
        try:
            layout = self.request("GetLayout", obj_handle, [])
        except RuntimeError:
            self._destroy_session_object(obj_handle)
            return None
        hc = ((layout.get("qLayout") or {}).get("qHyperCube") or {})
        val: Any = None
        try:
            val = hc["qDataPages"][0]["qMatrix"][0][0].get("qNum")
        except (KeyError, IndexError, TypeError, AttributeError):
            try:
                val = (hc.get("qGrandTotalRow") or [{}])[0].get("qNum")
            except (KeyError, IndexError, TypeError, AttributeError):
                val = None
        self._destroy_session_object(obj_handle)
        if isinstance(val, (int, float)) and val == val:  # finite (NaN != NaN)
            return float(val)
        return None

    def _destroy_session_object(self, obj_handle: int) -> None:
        try:
            self.request(
                "DestroySessionObject", self.app_handle, [obj_handle],
            )
        except (RuntimeError, OSError, WebSocketException):
            # Best effort; the WS close will reap anything we miss.
            pass

    def _reconnect_and_recreate_cube(
        self, field_names: List[str], anchor: Optional[str] = None,
    ) -> Optional[int]:
        """Re-open the WebSocket and re-create a session-object hypercube
        for ``field_names``. Returns the new ``obj_handle`` (or None on
        failure).

        Used by :meth:`_stream_hypercube_rows` to recover from dead-WS
        errors mid-extract. The old session object is implicitly gone
        when the WS closes -- we don't bother with DestroySessionObject
        on a socket that's already dead.
        """
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
        self.ws = None
        # Reset id counter to keep request IDs fresh on the new WS.
        self._next_id = 1
        self.connect()
        new_handle, qcy, qcx = self._try_create_cube(field_names, anchor=anchor)
        return new_handle

    def _stream_hypercube_rows(
        self, obj_handle: int, qcy: int, qcx: int,
        field_names: Optional[List[str]] = None,
        row_start: int = 0,
        row_end: Optional[int] = None,
        n_dims: Optional[int] = None,
        anchor: Optional[str] = None,
    ) -> Iterator[List[str]]:
        """Inner pagination loop. Split out so :meth:`extract_table`
        can wrap it in try/finally for session-object cleanup without
        duplicating the row-yielding logic.

        ``row_start``/``row_end`` bound the absolute row range fetched
        (for row-range parallelism); the defaults cover the whole cube.

        When ``field_names`` is provided, a connection-class failure
        (``ConnectionResetError`` / ``SSL`` bad-length / ``BrokenPipeError``)
        triggers a full reconnect: WS torn down, re-opened, app handle
        re-resolved, and the session object re-created with the same
        cube def. Pagination then resumes from the failed ``top`` row
        so we don't lose progress. Without ``field_names``, the legacy
        sleep-and-retry path runs.
        """
        page_rows = max(
            1,
            min(_MAX_ROWS_PER_CALL, _MAX_CELLS_PER_CALL // max(1, qcx)),
        )
        end = qcy if row_end is None else min(qcy, int(row_end))
        top = max(0, int(row_start))
        # Count-anchor expansion: when the cube carries a trailing
        # ``=Count([anchor])`` measure (qcx > n_dims), the last cell of each
        # matrix row is the number of physical records that share this distinct
        # combination. We emit the n_dims dimension cells repeated that many
        # times, reconstructing exact-duplicate rows that the distinct-only cube
        # would otherwise collapse. ``qSuppressZero`` already dropped count-0
        # phantom rows, so every emitted combo has count >= 1.
        expand = n_dims is not None and qcx > n_dims
        dims_w = n_dims if expand else qcx
        # Log a heartbeat every ~50K rows so large extracts don't feel
        # stuck. Suppressed for single-page tables.
        log_interval = 50_000
        last_logged = top
        last_gc = 0
        while top < end:
            height = min(page_rows, end - top)
            # Page-level retry. Two failure modes:
            #   * Transient frame corruption (SSL: BAD_LENGTH on a still-
            #     alive WS) -- sleep and re-send works.
            #   * Dead WS (ConnectionResetError / WinError 10054) -- the
            #     socket is gone; sleeping won't fix it, we must reopen
            #     the WS and re-create the session object before retry.
            attempts = 0
            while True:
                try:
                    data = self.request(
                        "GetHyperCubeData",
                        obj_handle,
                        [
                            "/qHyperCubeDef",
                            [{
                                "qLeft":   0,
                                "qTop":    top,
                                "qWidth":  qcx,
                                "qHeight": height,
                            }],
                        ],
                    )
                    break
                except (OSError, WebSocketException, RuntimeError) as exc:
                    attempts += 1
                    if attempts >= 4:
                        raise
                    _log.warning(
                        f"    page {top}-{top + height} failed "
                        f"({exc.__class__.__name__}: {exc}); retry {attempts}/4"
                    )
                    import time as _time
                    # Detect dead-WS errors. WinError 10054 (Windows) and
                    # generic ConnectionResetError/BrokenPipeError (POSIX)
                    # are unrecoverable on the same WS; "BAD_LENGTH" is
                    # also typically followed by a dead socket once it
                    # appears mid-stream.
                    msg = str(exc).lower()
                    dead_ws = (
                        isinstance(exc, (ConnectionResetError,
                                         ConnectionAbortedError,
                                         BrokenPipeError))
                        or "10054" in msg
                        or "bad_length" in msg
                        or "bad length" in msg
                        or "connection is already closed" in msg
                    )
                    if dead_ws and field_names is not None:
                        _time.sleep(2.0 * attempts)
                        try:
                            new_handle = self._reconnect_and_recreate_cube(
                                field_names, anchor=anchor,
                            )
                        except Exception as rexc:  # noqa: BLE001
                            _log.warning(
                                f"    reconnect failed: {rexc}; will retry"
                            )
                            continue
                        if new_handle is None:
                            continue
                        obj_handle = new_handle
                        _log.info(
                            f"    reconnected; resuming from row {top:,}"
                        )
                    else:
                        # Transient: just back off.
                        _time.sleep(0.5 * attempts)
            # Pop pages off the response dict so the rest of `data` can
            # be released immediately and only the matrix we're actively
            # iterating stays alive. Each cell yielded downstream is a
            # fresh list of strings -- the upstream dict-of-dicts that
            # the JSON parser built is no longer referenced.
            pages = data.pop("qDataPages", None) or []
            data = None
            for page in pages:
                matrix = page.pop("qMatrix", None) or []
                # Drop the outer page dict reference so only `matrix`
                # itself survives into the yield loop.
                page.clear()
                if expand:
                    for row in matrix:
                        cnt = _count_from_measure_cell(row[dims_w])
                        if cnt <= 0:
                            continue
                        data = [_cell_value(c) for c in row[:dims_w]]
                        for _ in range(cnt):
                            yield data
                else:
                    for row in matrix:
                        yield [_cell_value(c) for c in row]
                matrix = None
            pages = None
            top += height
            if (end - max(0, int(row_start))) > page_rows and (
                top - last_logged >= log_interval or top >= end
            ):
                _log.info(f"      fetched {top:,}/{end:,} rows")
                last_logged = top
            # Periodic GC nudge for very large tables. The per-page dicts
            # above are reference-counted away immediately, but the JSON
            # parser also produces transient cycle-prone structures the
            # generational collector handles on its own schedule. Forcing
            # collection every ~100K rows keeps RSS flat across the whole
            # extract.
            if top - last_gc >= _GC_EVERY_ROWS:
                gc.collect()
                last_gc = top


# ---------------------------------------------------------------------------
# Cell / CSV helpers
# ---------------------------------------------------------------------------

def _cell_value(cell: Any) -> str:
    """Pick a CSV-safe value from a Qlik matrix cell.

    Qlik cells are dual values: ``{qText, qNum, qElemNumber, qState,
    qIsNull}``. For numeric fields ``qText`` is locale-formatted
    (e.g. ``"1,234.56"``) while ``qNum`` is the raw number. For text
    fields ``qNum`` is the string ``"NaN"`` (the engine's sentinel
    for "not numeric").

    Rules, in order:

    1. **Null detection.** Qlik signals NULL with ``qIsNull: true``,
       ``qElemNumber: -2``, or ``qState in ("L","X")`` paired with
       the literal text ``"-"`` (the app's null-display string).
       Any of these maps to an empty CSV cell -- otherwise we end up
       with a column full of ``"-"`` placeholders that PBI's CSV
       loader then types as ``text`` and breaks numeric aggregates.
    2. **Numeric.** ``qNum`` is a finite number -> stringify, dropping
       a trailing ``.0`` so the CSV-schema sniffer can classify the
       column as int64.
    3. **Text fallback.** ``qText`` straight through. Empty text
       becomes an empty CSV cell.
    """
    if not isinstance(cell, dict):
        return ""
    # 1. Explicit null signals from the engine.
    if cell.get("qIsNull") is True:
        return ""
    elem = cell.get("qElemNumber")
    if isinstance(elem, int) and elem == -2:
        return ""
    state = cell.get("qState")
    qtext = cell.get("qText")
    # Some engine versions only signal null via qState=="X" plus the
    # app's null-display string in qText (default "-").
    if state == "X" and (qtext in (None, "", "-")):
        return ""
    # 2. Numeric path.
    qnum = cell.get("qNum")
    if isinstance(qnum, (int, float)):
        # NaN is the only float that is not equal to itself, so this
        # rejects the "not numeric" sentinel without importing math.
        if qnum == qnum:
            as_int = int(qnum)
            if as_int == qnum:
                return str(as_int)
            return str(qnum)
    # 3. Text fallback. Treat the literal "-" with no numeric value
    # as null too -- this is the only thing Qlik shows for a dimension
    # value that resolved to NULL with default app settings.
    if qtext == "-" and (qnum is None or qnum == "NaN"):
        return ""
    return qtext or ""


def _count_from_measure_cell(cell: Any) -> int:
    """Integer record-count from a ``=Count([anchor])`` measure cell.

    Drives count-anchor row expansion: a distinct combination is emitted this
    many times to reconstruct its physical duplicate rows. ``int(round(qNum))``
    when finite (0 -> skip, the row was a count-0 phantom qSuppressZero usually
    removes); a missing/NaN value defaults to ``1`` so a genuine row is never
    dropped on a malformed measure cell."""
    if not isinstance(cell, dict):
        return 1
    qnum = cell.get("qNum")
    if isinstance(qnum, (int, float)) and qnum == qnum:  # finite: NaN != NaN
        n = int(round(qnum))
        return n if n > 0 else 0
    return 1


def _write_csv(path: Path, headers: List[str], rows: Iterable[List[str]]) -> int:
    """Stream ``rows`` to a UTF-8 (BOM) CSV. Returns the row count."""
    n = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = _csv.writer(f)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
            n += 1
    return n


def _field_kind_from_tags(field: Dict[str, Any]) -> str:
    """Declared Parquet column kind for an engine field from its qTags
    (``$timestamp``/``$date``/``$integer``/``$numeric``/``$text``).

    ``$text``/``$ascii`` -> ``string`` (preserve zero-padded codes). An
    UNTAGGED field -> ``auto``: the writer value-types it from the clean
    ``qNum`` cells the extract emits, so a numeric column the schema didn't
    tag (e.g. a 0/1 flag a measure SUMs) is stored as a number instead of
    text. A text-typed numeric column is what makes a Power BI card render
    its value in quotes -- and apps fetched without a ``GetTablesAndKeys``
    schema have NO tags at all, so every numeric column would otherwise
    default to text. ``auto`` never promotes a date (those need the tag)."""
    tags = {(t or "").lower() for t in (field.get("tags") or [])}
    if "$timestamp" in tags:
        return "datetime"
    if "$date" in tags:
        return "date"
    if "$integer" in tags:
        return "int"
    if "$numeric" in tags:
        return "double"
    if "$text" in tags or "$ascii" in tags:
        return "string"
    return "auto"


def _write_parquet(
    path: Path, resolved: List[Dict[str, Any]], rows: Iterable[List[str]],
    kinds: Optional[List[str]] = None,
) -> int:
    """Stream ``rows`` to a typed Parquet file. Column types come from
    each resolved field's engine qTags (no sniff, no cast). The engine's
    ``_cell_value`` already emits clean ``qNum`` for numeric/date fields
    (a date is its Qlik serial), which the writer converts to the
    declared type. Returns the row count. Buffers one page (4096 rows)
    at a time so peak memory stays flat for 30M-row tables.

    ``kinds`` (one per resolved field) overrides the per-field tag-derived
    kind. Used by the row-range-split path so every slice writes an
    IDENTICAL explicit schema (untagged ``auto`` columns are pre-resolved
    to a concrete kind once, then fixed for all slices, so the part files
    merge with no schema conflict)."""
    from .parquet_io import ParquetStreamWriter

    if kinds is not None:
        fields = [(f["alias"], kinds[i]) for i, f in enumerate(resolved)]
    else:
        fields = [(f["alias"], _field_kind_from_tags(f)) for f in resolved]
    n = 0
    page: List[List[str]] = []
    with ParquetStreamWriter(path, fields) as pw:
        for row in rows:
            page.append(row)
            if len(page) >= 4096:
                pw.write_page(page)
                n += len(page)
                page = []
        if page:
            pw.write_page(page)
            n += len(page)
    return n
