"""High-level cloud-conversion API.

Designed for server / backend integration. Where the CLI is the thin
human-facing surface, this module is the Python API a backend service
or job runner calls directly.

Example usage from a backend / job runner::

    from qlik_to_pbi.cloud import convert_from_cloud

    result = convert_from_cloud(
        tenant="https://your-tenant.us.qlikcloud.com",
        api_key="<bearer-token>",
        app_id="3a4b...uuid...",
        output_dir="/var/jobs/abc123/out",
        fetch_data=True,
    )
    # result.pbip_path / result.unbuilt_dir / result.data_dir /
    # result.report_path / result.stats

The function is **stateless** -- every call opens a fresh WebSocket,
authenticates with the supplied token, runs the conversion, and tears
down. No global state, no on-disk config files read. Safe to call
concurrently from multiple worker threads/processes as long as each
call uses its own ``output_dir``.

Compared to running the CLI: same conversion pipeline, just driven
programmatically instead of via argparse. Use the CLI for local work
and ``convert_from_cloud`` for anything that needs to scale.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ._logging import get_logger
from .converter import Converter
from .engine_fetch import (
    DEFAULT_ENGINE_URL, fetch_via_engine, read_loadmodel,
)
from .engine_unbuild import unbuild_via_engine

_log = get_logger("CLOUD")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class CloudConversionResult:
    """What :func:`convert_from_cloud` returns to the caller."""

    output_dir:   Path                   # the root <output>/
    unbuilt_dir:  Path                   # <output>/unbuilt/
    data_dir:     Optional[Path]         # <output>/data/ (None if no fetch)
    pbip_dir:     Path                   # <output>/pbip/
    pbip_path:    Path                   # the .pbip entry point file
    report_path:  Optional[Path]         # conversion_report.md if present
    csv_paths:    List[Path] = field(default_factory=list)
    stats:        Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def convert_from_cloud(
    tenant: str,
    api_key: str,
    app_id: str,
    output_dir: str | Path,
    *,
    name: Optional[str] = None,
    fetch_data: bool = True,
    credentials_path: Optional[str | Path] = None,
    live_mode: bool = False,
    default_connection_class: str = "",
    emit_format: str = "parquet",
    prune_columns: bool = True,
    prefetched_data_dir: Optional[str | Path] = None,
    db_connections: Optional[Dict[str, Dict[str, Any]]] = None,
) -> CloudConversionResult:
    """Convert a Qlik Cloud app to a PBIP project, end to end.

    Parameters
    ----------
    tenant
        Qlik Cloud tenant URL, e.g. ``https://acme.us.qlikcloud.com``.
        Schemes ``https://`` / ``http://`` / ``wss://`` / ``ws://`` are
        all accepted; bare hostnames are upgraded to ``wss://``.
    api_key
        Bearer token generated in Qlik Cloud > Settings > API keys.
        Never logged. Passed only as an ``Authorization`` header on
        the Engine WebSocket.
    app_id
        The app's UUID (visible in the cloud app URL).
    output_dir
        Root output folder. Three sibling folders are produced inside:
        ``unbuilt/`` (JSON IR), ``data/`` (one CSV per table when
        ``fetch_data=True``), ``pbip/`` (the Power BI Project itself).
    name
        Override the PBIP / report name. Default: the app's qTitle.
    fetch_data
        When True (default), extract every loadmodel table to CSV via
        the cloud Engine API and bind the partitions to those CSVs.
        Set False for a metadata-only conversion.
    emit_format
        ``"parquet"`` (default) or ``"csv"``. Parquet types each column
        from the engine qTags (no sniff/cast) and is the recommended /
        default format; falls back to CSV when pyarrow is absent. See
        ``docs/large-data-strategy.md``.
    prune_columns
        When True (**default**), extract only the fields the model
        references (`field_usage.collect_keep_fields` -- relationship
        keys + measure / dimension / visual fields, derived from the
        built model + cross-table keys), dropping columns nothing in the
        app uses. Fewer cells/call, smaller files, less memory.
        Validated to never drop a referenced column. Pass False to
        extract every source column.
    prefetched_data_dir
        Optional directory of data files supplied out-of-band -- typically
        user-uploaded QVDs already transcoded to typed Parquet by
        :mod:`qlik_to_pbi.qvd_ingest`, one ``<table>.parquet`` per table.
        Those files are copied into the run's ``data/`` folder and the
        Engine fetch SKIPS the tables they cover (matched by name), so the
        slow per-call extract runs only for the remaining tables. With every
        table supplied this way, no engine fetch happens at all -- the fast
        path for large apps. Tables not covered still fetch from the engine
        (or, with ``fetch_data=False``, stay empty stubs).

    Returns
    -------
    CloudConversionResult
        Carries every path the caller might want -- the PBIP entry
        file, the per-folder roots, the conversion report, the list
        of CSVs, and summary stats from the build.

    Raises
    ------
    RuntimeError
        On unrecoverable engine failures (auth rejected, app not
        found, tenant unreachable). The exception message is safe to
        surface to a UI; it never contains the API key.
    """
    output_root = Path(output_dir).resolve()
    unbuild_root = output_root / "unbuilt"
    data_root = output_root / "data"
    pbip_root = output_root / "pbip"

    _log.info(
        f"Cloud conversion: tenant={_redact_tenant(tenant)} "
        f"app={app_id} -> {output_root}"
    )

    # 1. Unbuild via cloud Engine API.
    unbuild_via_engine(
        output_dir=unbuild_root,
        tenant=tenant,
        api_key=api_key,
        app_id=app_id,
    )

    # 1b. Seed the data folder with any out-of-band files (transcoded
    #     uploaded QVDs). They land in the SAME data/ folder the engine
    #     fetch writes to, so the converter binds both from one place; the
    #     tables they cover are skipped by the engine fetch below.
    prefetched_tables: Set[str] = set()
    if prefetched_data_dir:
        prefetched_tables = _seed_prefetched(prefetched_data_dir, data_root)
        if prefetched_tables:
            _log.info(
                f"Using {len(prefetched_tables)} pre-supplied table file(s) "
                f"(uploaded QVD); engine fetch will skip them."
            )

    # 1c. Tables loaded from a SQL data connection the user supplied details
    #     for are repointed at the live source (a DB Import partition) by the
    #     model -- so DON'T fetch their engine-loaded snapshot. Detect them from
    #     the script and skip them in the fetch (like an uploaded QVD).
    db_skip: Set[str] = set()
    if db_connections:
        db_skip = _db_tables_to_skip(unbuild_root, db_connections)
        if db_skip:
            _log.info(
                f"Using a live DB source for {len(db_skip)} table(s); their "
                f"engine data will not be fetched: {sorted(db_skip)}"
            )

    # 2. Fetch data via the same engine (optional). Tables supplied as an
    #    uploaded QVD are removed entirely (skip_tables); tables repointed at a
    #    live DB source keep their SCHEMA (sidecar) but skip DATA extraction
    #    (data_skip_tables). When any table is DB-repointed we pass unbuild_dir
    #    so the engine-schema sidecar is written -- the model needs the DB
    #    tables' authoritative field names + keys to build + relate them.
    csv_paths: List[Path] = []
    if fetch_data:
        load_model = read_loadmodel(unbuild_root)
        keep_fields = None
        if prune_columns:
            from .field_usage import collect_keep_fields
            keep_fields = collect_keep_fields(unbuild_root)
        csv_paths = fetch_via_engine(
            load_model=load_model,
            output_dir=data_root,
            tenant=tenant,
            api_key=api_key,
            app_id=app_id,
            emit_format=emit_format,
            keep_fields=keep_fields,
            skip_tables=prefetched_tables or None,
            data_skip_tables=db_skip or None,
            unbuild_dir=unbuild_root if db_skip else None,
        )

    # The data/ folder is bindable when EITHER the engine wrote files OR we
    # seeded it with uploaded-QVD Parquet.
    have_data = bool((fetch_data and csv_paths) or prefetched_tables)

    # 3. Convert IR -> PBIP.
    conv = Converter(
        qlik_output_dir=unbuild_root,
        output=pbip_root,
        name=name,
        data_dir=data_root if have_data else None,
        credentials_path=credentials_path,
        live_mode=live_mode,
        default_connection_class=default_connection_class,
        db_connections=db_connections,
    )
    conv.convert()

    pbip_file = pbip_root / f"{conv.name}.pbip"
    report_file = pbip_root / "conversion_report.md"

    return CloudConversionResult(
        output_dir   = output_root,
        unbuilt_dir  = unbuild_root,
        data_dir     = data_root if have_data else None,
        pbip_dir     = pbip_root,
        pbip_path    = pbip_file,
        report_path  = report_file if report_file.exists() else None,
        csv_paths    = csv_paths,
        stats        = {
            "name":     conv.name,
            "csv_count": len(csv_paths),
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_prefetched(src_dir: str | Path, data_root: Path) -> Set[str]:
    """Copy out-of-band data files (transcoded uploaded QVDs) into the run's
    ``data/`` folder and return the set of table names they cover.

    Each file is named ``<table>.parquet`` (or ``.csv``) by
    :func:`qlik_to_pbi.qvd_ingest.transcode_qvd_map`. The table name is the
    file stem -- the same key the converter binds by and the engine fetch
    skips on. Copy (not move) so the caller's upload staging area is left
    intact for inspection / re-runs.
    """
    src = Path(src_dir)
    covered: Set[str] = set()
    if not src.is_dir():
        return covered
    data_root.mkdir(parents=True, exist_ok=True)
    for fp in sorted(src.iterdir()):
        if not fp.is_file() or fp.suffix.lower() not in (".parquet", ".csv"):
            continue
        try:
            shutil.copy2(fp, data_root / fp.name)
        except OSError as exc:  # noqa: BLE001
            _log.warning(f"  could not stage pre-supplied {fp.name}: {exc}")
            continue
        covered.add(fp.stem)
    return covered


def _db_tables_to_skip(
    unbuild_root: Path,
    db_connections: Dict[str, Dict[str, Any]],
) -> Set[str]:
    """Return the names of tables that will be repointed at a live DB source
    (so the engine fetch should skip them). A table qualifies when the script
    loads it from a ``LIB CONNECT TO [name]`` connection AND the user supplied
    that connection's details (matched by name, or a single supplied config).
    """
    from .script_parser import parse_db_sources
    script_path = unbuild_root / "script.qvs"
    if not script_path.is_file():
        return set()
    try:
        script = script_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    db_sources = parse_db_sources(script)
    if not db_sources:
        return set()
    cfg_keys = {(k or "").strip().lower() for k in db_connections}
    single = len(db_connections) == 1
    skip: Set[str] = set()
    for tname, info in db_sources.items():
        conn_name = (str(info.get("connection") or "")).strip().lower()
        if conn_name in cfg_keys or single:
            skip.add(tname)
    return skip


def _redact_tenant(tenant: str) -> str:
    """Return the tenant with any embedded credentials masked.

    Defensive -- the bearer key is passed separately, but defence in
    depth for logs.
    """
    if not tenant:
        return ""
    # Strip user:pass@ if anyone embeds them in the URL.
    import re
    return re.sub(r"//[^@/]+:[^@/]+@", "//*****@", tenant)
