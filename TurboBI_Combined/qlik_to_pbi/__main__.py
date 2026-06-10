"""CLI entry point for the Qlik -> PBIP converter.

Default output layout
---------------------

A single ``--output OUT`` produces three sibling folders under ``OUT``:

::

    OUT/
      unbuilt/    JSON IR from the unbuild step (skipped when --input
                  points at an already-unbuilt folder you own)
      data/      one CSV per loadmodel table (only when a fetch step ran)
      pbip/       the Power BI Project itself

``--keep-unbuild PATH`` and ``--fetch-data-dir PATH`` still override
where those two folders live, in which case the override paths are
used as-is and no sibling folder is created under ``OUT``.

Usage
-----
    # Convert from an existing `qlik app unbuild` output folder
    python -m qlik_to_pbi --input ./output --output ./out

    # Cloud unbuild + convert (needs qlik CLI on PATH and a configured
    # context with API key/tenant; see `qlik context ls`).
    python -m qlik_to_pbi --app <APP_ID> --output ./out

    # Cloud end-to-end: unbuild + fetch CSVs per data object + convert.
    python -m qlik_to_pbi --app <APP_ID> --fetch-data --output ./out

    # FULLY OFFLINE end-to-end from a .qvf alone (no cloud tenant at any
    # step): unbuild + fetch + convert all via Qlik Sense Desktop's local
    # WebSocket Engine API. Needs Desktop to be running and the
    # websocket-client package (pip install websocket-client).
    python -m qlik_to_pbi \\
        --qvf-path "C:/Users/me/Documents/Qlik/Sense/Apps/MyApp.qvf" \\
        --fetch-via-engine \\
        --output ./out

    # Cloud end-to-end using credentials already saved by the qlik CLI
    # (~/.qlik/contexts.yml). No need to paste the bearer token --
    # the converter reads it the same way `qlik app unbuild` does.
    python -m qlik_to_pbi \\
        --use-qlik-context \\
        --cloud-app-id <APP_UUID> \\
        --output ./out

    # Same, metadata-only (no data extraction; empty-stub partitions):
    python -m qlik_to_pbi \\
        --qvf-path "C:/Users/me/Documents/Qlik/Sense/Apps/MyApp.qvf" \\
        --output ./out
"""

import argparse
import sys
from pathlib import Path

from ._logging import configure_default
from .cloud import convert_from_cloud
from .converter import Converter, unbuild_via_cli
from .engine_fetch import (
    DEFAULT_ENGINE_URL, fetch_via_engine, read_loadmodel,
)
from .engine_unbuild import unbuild_via_engine
from .fetch_data import fetch_object_data
from .qvf_direct import qvf_to_unbuild_dir


def main(argv=None) -> int:
    # Wire logging up first so the cloud unbuild + fetch stages (which
    # run BEFORE the Converter is instantiated) produce visible
    # [ENGINE] / [UNBUILD] / [CLOUD] progress on stderr. Without this
    # call the cloud path looks "stuck" between "Loaded cloud creds"
    # and the first Converter message -- it's actually busy with the
    # 30-120 s engine app-load + multi-minute table extraction.
    configure_default()

    parser = argparse.ArgumentParser(prog="qlik_to_pbi")
    parser.add_argument(
        "--input", "-i",
        help=(
            "Path to an existing `qlik app unbuild` output folder. "
            "When given, the JSON IR is read from there and no unbuild "
            "subfolder is created under --output."
        ),
    )
    parser.add_argument(
        "--app", "-a",
        help=(
            "Qlik app ID. When given without --input, the cloud "
            "`qlik app unbuild` CLI is invoked to produce the JSON IR. "
            "Required for --fetch-data."
        ),
    )

    parser.add_argument(
        "--output", "-o",
        help=(
            "Output folder. Three sibling folders are produced under "
            "it: unbuilt/, data/, pbip/. Default: derived from the "
            "input source (e.g. <qvf-stem>_pbip)."
        ),
    )
    parser.add_argument("--name", help="Override the report name (default: app title).")
    parser.add_argument(
        "--keep-unbuild",
        help=(
            "Explicit path for the JSON IR folder. Overrides the "
            "default OUT/unbuilt/ location. Useful when you want the "
            "IR to persist somewhere outside the output tree."
        ),
    )
    parser.add_argument(
        "--data-dir",
        help=(
            "Pre-existing directory of CSV exports to bind table "
            "partitions to. Bypasses the fetch step entirely - use "
            "this when you already have CSVs from a previous run."
        ),
    )
    parser.add_argument(
        "--fetch-data",
        action="store_true",
        help=(
            "Cloud CLI fetch: shell out to `qlik app object export` for "
            "every data-bearing object and drop the CSVs. Limited to the "
            "per-chart ~500-row initial-fetch cap. Requires --app."
        ),
    )
    parser.add_argument(
        "--fetch-data-dir",
        help=(
            "Explicit path for the fetch step's CSV output. Overrides "
            "the default OUT/data/ location."
        ),
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help=(
            "Emit fetched data as CSV instead of the default typed "
            "Parquet (applies to --fetch-data and --fetch-via-engine). "
            "Parquet is the default because it carries its schema (the "
            "bound partition needs no type-cast) and loads large tables "
            "faster. Use --csv only if pyarrow is unavailable or you need "
            "human-readable text exports. (Parquet auto-falls-back to CSV "
            "anyway when pyarrow is missing.)"
        ),
    )
    parser.add_argument(
        "--no-prune-columns",
        action="store_true",
        help=(
            "Disable column pruning. By DEFAULT the converter extracts "
            "only the columns the model actually references (relationship "
            "keys + measure / dimension / variable / visual fields), "
            "dropping unused source columns -- fewer cells per Engine call "
            "(more rows fit the 10k-cell cap), smaller data files, less "
            "in-model memory. The keep-set is derived from the built "
            "model's real references (validated to never drop a referenced "
            "column). Pass this flag to extract every source column "
            "instead. Applies to the Engine fetch paths "
            "(--fetch-via-engine and cloud-context)."
        ),
    )
    parser.add_argument(
        "--qlik-cmd",
        default="qlik",
        help="Path/name of the qlik CLI binary (default: 'qlik').",
    )

    # ------------------------------------------------------------------
    # Offline Engine API (Qlik Sense Desktop)
    # ------------------------------------------------------------------
    parser.add_argument(
        "--qvf-path",
        help=(
            "Absolute path to a .qvf file. When given without --input, "
            "the converter performs an OFFLINE unbuild via Qlik Sense "
            "Desktop's localhost WebSocket Engine API. No cloud tenant "
            "required. Desktop must be running."
        ),
    )
    parser.add_argument(
        "--fetch-via-engine",
        action="store_true",
        help=(
            "Offline data extract: open the .qvf in Qlik Sense Desktop "
            "and pull one CSV per loadmodel table via the Engine API. "
            "Bypasses the cloud CLI's per-chart row cap. Requires "
            "--qvf-path."
        ),
    )
    parser.add_argument(
        "--engine-url",
        default=DEFAULT_ENGINE_URL,
        help=(
            "WebSocket base URL of the Qlik engine "
            f"(default: '{DEFAULT_ENGINE_URL}'). Override only if "
            "Desktop listens on a non-standard port."
        ),
    )

    # ------------------------------------------------------------------
    # Live database credentials (Sql.Database / PostgreSQL / Snowflake /
    # Databricks). When --live is on, the converter emits live partition
    # M expressions wired to the matching credentials entry instead of
    # CSV / empty-stub partitions. Passwords / tokens are NOT embedded;
    # the user wires them up in PBI Desktop > Transform Data > Data
    # source settings (a credentials_manifest.json is dropped next to
    # the PBIP to document what's needed).
    # ------------------------------------------------------------------
    parser.add_argument(
        "--credentials",
        help=(
            "Path to a credentials JSON / XLSX file. When omitted, "
            "the converter looks for ``credentials.json`` in the current "
            "directory. Format: see qlik_to_pbi/credentials.py docstring."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Emit live database partition M expressions (DirectQuery) "
            "for tables that match a credentials entry. Without --live, "
            "credentials are ignored and the existing CSV / empty-stub "
            "partition path is used. Compatible with --data-dir / "
            "--fetch-data; live partitions take precedence per table."
        ),
    )
    parser.add_argument(
        "--connection-class",
        default="",
        help=(
            "When the credentials file contains entries for multiple "
            "classes (e.g. postgres + databricks), use this flag to "
            "pick the default class for tables that aren't explicitly "
            "pinned via a ``table:`` field in the credentials entry. "
            "Examples: ``--connection-class postgres``, "
            "``--connection-class databricks``."
        ),
    )

    # ------------------------------------------------------------------
    # Qlik Cloud Engine API (no Desktop required)
    # ------------------------------------------------------------------
    parser.add_argument(
        "--cloud-tenant",
        help=(
            "Qlik Cloud tenant URL "
            "(e.g. 'https://your-tenant.us.qlikcloud.com'). When given "
            "with --cloud-api-key and --cloud-app-id, the converter "
            "skips Desktop entirely and talks to the cloud Engine API "
            "directly. Designed for server / backend deployments."
        ),
    )
    parser.add_argument(
        "--cloud-api-key",
        help=(
            "Qlik Cloud API key / bearer token. Generate in Qlik Cloud "
            "> Settings > API keys. Never logged."
        ),
    )
    parser.add_argument(
        "--cloud-app-id",
        help=(
            "Qlik Cloud app UUID. Visible in the app URL inside the "
            "Qlik Cloud hub."
        ),
    )
    parser.add_argument(
        "--use-qlik-context",
        nargs="?",
        const="__current__",
        default=None,
        metavar="NAME",
        help=(
            "Load --cloud-tenant and --cloud-api-key from your qlik "
            "CLI context (~/.qlik/contexts.yml). Use the bare flag to "
            "pick the active context, or '--use-qlik-context NAME' "
            "for a specific one. Combine with --cloud-app-id (or "
            "--app) so you don't have to paste the bearer token on "
            "the command line."
        ),
    )

    parser.add_argument(
        "--prefetched-data-dir",
        help=(
            "Directory of data files supplied out-of-band -- typically "
            "user-uploaded QVDs already transcoded to typed Parquet (one "
            "<table>.parquet per table). Those files are staged into the "
            "run's data/ folder and the cloud Engine fetch SKIPS the tables "
            "they cover, so the slow per-call extract runs only for the "
            "rest. With every table supplied this way, no engine fetch "
            "happens at all -- the fast path for large apps. Cloud mode only."
        ),
    )

    parser.add_argument(
        "--db-connections",
        help=(
            "Path to a JSON file of database connection details, keyed by the "
            "Qlik connection NAME, e.g. "
            '{"Databricks": {"class":"databricks", "server":"<host>", '
            '"http_path":"/sql/1.0/warehouses/<id>"}}. Tables the load script '
            "pulls from one of these connections (LIB CONNECT TO + SQL SELECT) "
            "are repointed at the live source as an Import partition instead of "
            "binding the engine-loaded snapshot. Cloud mode only."
        ),
    )

    # ---- Ergonomics / sanity-check flags -----------------------------
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Parse + plan the conversion, but skip writing PBIP files. "
            "Useful for sanity-checking that the IR parses and a model "
            "can be built without producing artefacts."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "Suppress per-table progress output. Sets QLIK_LOG_LEVEL to "
            "WARNING for the run."
        ),
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help=(
            "Skip the PBIP emit but still write conversion_report.md. "
            "Handy for triaging how a Qlik app would translate without "
            "committing to a full conversion."
        ),
    )

    args = parser.parse_args(argv)

    # Apply --quiet by setting the env var the logger reads at config time.
    if args.quiet:
        import os
        os.environ["QLIK_LOG_LEVEL"] = "WARNING"

    # Auto-detect credentials.json in the current working directory
    # when --live is set and --credentials is not. Lets the user drop
    # a credentials.json next to where they invoke the CLI and have it
    # picked up implicitly.
    if args.live and not args.credentials:
        candidate = Path.cwd() / "credentials.json"
        if candidate.is_file():
            args.credentials = str(candidate)
            print(
                f"[CONVERT] Auto-detected credentials file: {candidate}",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # Pre-resolve: pull cloud creds from the qlik CLI context if asked.
    # The actual cloud short-circuit below runs only after this fills
    # in args.cloud_tenant / args.cloud_api_key, so the user can
    # combine --use-qlik-context with --cloud-app-id (or with --app,
    # which we promote to --cloud-app-id when no explicit value is
    # passed).
    # ------------------------------------------------------------------
    if args.use_qlik_context is not None:
        from .qlik_context import load_qlik_context
        ctx_name = (
            None
            if args.use_qlik_context == "__current__"
            else args.use_qlik_context
        )
        ctx = load_qlik_context(name=ctx_name)
        if ctx is None:
            parser.error(
                "Could not load tenant / API key from your qlik CLI "
                "context. Verify ~/.qlik/contexts.yml exists (e.g. via "
                "`qlik context create`) or pass --cloud-tenant + "
                "--cloud-api-key explicitly."
            )
        if not args.cloud_tenant:
            args.cloud_tenant = ctx.tenant
        if not args.cloud_api_key:
            args.cloud_api_key = ctx.api_key
        if not args.cloud_app_id and args.app:
            args.cloud_app_id = args.app
        print(
            f"[CONVERT] Loaded cloud creds from qlik CLI context "
            f"'{ctx.name}'.",
            file=sys.stderr,
        )

    # ------------------------------------------------------------------
    # Cloud Engine API short-circuit. When all three cloud flags are
    # set, route the whole conversion through the cloud Engine API and
    # bypass every Desktop / direct-parse path.
    # ------------------------------------------------------------------
    if args.cloud_tenant or args.cloud_api_key or args.cloud_app_id:
        missing = [
            flag for flag, val in (
                ("--cloud-tenant",  args.cloud_tenant),
                ("--cloud-api-key", args.cloud_api_key),
                ("--cloud-app-id",  args.cloud_app_id),
            ) if not val
        ]
        if missing:
            parser.error(
                "Cloud mode requires all three of --cloud-tenant, "
                "--cloud-api-key, --cloud-app-id. Missing: "
                + ", ".join(missing)
            )
        # Cloud mode handles its own output layout under args.output.
        output_root = _resolve_output_root(args)
        result = convert_from_cloud(
            tenant     = args.cloud_tenant,
            api_key    = args.cloud_api_key,
            app_id     = args.cloud_app_id,
            output_dir = output_root,
            name       = args.name,
            fetch_data = not args.data_dir,  # honor --data-dir override
            credentials_path = args.credentials,
            live_mode  = args.live,
            default_connection_class = args.connection_class,
            emit_format = "csv" if getattr(args, "csv", False) else "parquet",
            prune_columns = not getattr(args, "no_prune_columns", False),
            prefetched_data_dir = args.prefetched_data_dir,
            db_connections = _load_db_connections(getattr(args, "db_connections", None)),
        )
        print(f"[CONVERT] Done. Open {result.pbip_path}")
        return 0

    # ------------------------------------------------------------------
    # Validate flag combinations
    # ------------------------------------------------------------------
    if not args.input and not args.app and not args.qvf_path:
        parser.error(
            "One of --input, --app, or --qvf-path must be supplied.\n"
            "  --input <dir>       existing `qlik app unbuild` JSON folder\n"
            "  --app <ID>          unbuild from Qlik Cloud (needs tenant)\n"
            "  --qvf-path <path>   offline unbuild via Qlik Sense Desktop"
        )
    if args.fetch_data and not args.app:
        parser.error(
            "--fetch-data requires --app <APP_ID> because the export step "
            "talks to the Qlik engine via the CLI."
        )
    if args.fetch_via_engine and not args.qvf_path:
        parser.error(
            "--fetch-via-engine requires --qvf-path <absolute path to .qvf>."
        )
    if args.fetch_data and args.fetch_via_engine:
        parser.error(
            "--fetch-data and --fetch-via-engine are mutually exclusive. "
            "Pick one: cloud CLI (--fetch-data) or local Desktop engine "
            "(--fetch-via-engine)."
        )

    # ------------------------------------------------------------------
    # Resolve the three sibling output paths.
    #
    # Order of precedence per folder:
    #   1. An explicit --keep-unbuild / --fetch-data-dir / etc. flag.
    #   2. <OUT>/unbuilt|data|pbip when --output is given.
    #   3. A sensible default derived from the input source.
    # ------------------------------------------------------------------
    output_root = _resolve_output_root(args)
    unbuild_root = (
        Path(args.keep_unbuild).resolve()
        if args.keep_unbuild
        else (output_root / "unbuilt")
    )
    data_root = (
        Path(args.fetch_data_dir).resolve()
        if args.fetch_data_dir
        else (output_root / "data")
    )
    pbip_root = output_root / "pbip"

    # ------------------------------------------------------------------
    # Step 1 — produce the unbuild JSON IR (unless --input was given)
    # ------------------------------------------------------------------
    if args.input:
        qlik_dir = Path(args.input).resolve()
        if not qlik_dir.is_dir():
            parser.error(f"--input dir does not exist: {qlik_dir}")
    elif args.qvf_path:
        qvf_p = Path(args.qvf_path).resolve()
        if not qvf_p.is_file():
            parser.error(f"--qvf-path does not exist: {qvf_p}")
        # Prefer Qlik Sense Desktop's Engine API when it's reachable
        # AND can actually open this QVF -- it returns the real
        # loadmodel (tables + queries + associations) which makes
        # downstream relationship + table inference far better than
        # what we can squeeze out of a proprietary binary container.
        # Desktop accepts arbitrary file paths via the path-style URL,
        # but the QVF must be in a location Desktop can read (typically
        # ~/Documents/Qlik/Sense/Apps/). When the engine path produces
        # an empty result (Desktop running but app not loaded), we
        # fall back to direct-parse so the user gets *something* useful.
        qlik_dir = None
        if _desktop_engine_reachable(args.engine_url):
            print(
                f"[CONVERT] Qlik Sense Desktop detected at "
                f"{args.engine_url} -- trying Engine API unbuild first.",
                file=sys.stderr,
            )
            try:
                candidate = unbuild_via_engine(
                    qvf_path=qvf_p,
                    output_dir=unbuild_root,
                    engine_url=args.engine_url,
                )
            except (RuntimeError, OSError) as exc:
                print(
                    f"[CONVERT] Engine API unbuild errored: {exc}",
                    file=sys.stderr,
                )
                candidate = None
            if candidate and _unbuild_dir_looks_useful(candidate):
                qlik_dir = candidate
            else:
                print(
                    "[CONVERT] Engine API unbuild produced no usable "
                    "content (is the QVF loaded in Desktop?). Falling "
                    "back to direct QVF parse.",
                    file=sys.stderr,
                )
        if qlik_dir is None:
            try:
                qlik_dir = qvf_to_unbuild_dir(qvf_p, unbuild_root)
            except (RuntimeError, ValueError) as exc:
                parser.error(
                    f"Both Engine API and direct-parse failed for "
                    f"{qvf_p}: {exc}\n"
                    f"Open the .qvf in Qlik Sense Desktop and retry, "
                    f"or pass an existing unbuild folder via --input."
                )
    else:
        qlik_dir = unbuild_via_cli(
            args.app, unbuild_root, qlik_command=args.qlik_cmd,
        )

    # ------------------------------------------------------------------
    # Step 2 — fetch CSVs (cloud CLI or local Desktop engine)
    # ------------------------------------------------------------------
    data_dir = args.data_dir
    emit_format = "csv" if getattr(args, "csv", False) else "parquet"
    if args.fetch_data:
        fetch_object_data(
            app_id=args.app,
            qlik_input_dir=Path(qlik_dir),
            output_dir=data_root,
            qlik_command=args.qlik_cmd,
            emit_format=emit_format,
        )
        if not data_dir:
            data_dir = str(data_root)
    elif args.fetch_via_engine:
        load_model = read_loadmodel(Path(qlik_dir))
        keep_fields = None
        if not getattr(args, "no_prune_columns", False):
            from .field_usage import collect_keep_fields
            keep_fields = collect_keep_fields(Path(qlik_dir))
        fetch_via_engine(
            qvf_path=Path(args.qvf_path).resolve(),
            load_model=load_model,
            output_dir=data_root,
            engine_url=args.engine_url,
            unbuild_dir=Path(qlik_dir),
            emit_format=emit_format,
            keep_fields=keep_fields,
        )
        if not data_dir:
            data_dir = str(data_root)

    # ------------------------------------------------------------------
    # Step 3 — convert the JSON IR to PBIP
    # ------------------------------------------------------------------
    conv = Converter(
        qlik_output_dir=qlik_dir,
        output=pbip_root,
        name=args.name,
        data_dir=data_dir,
        credentials_path=args.credentials,
        live_mode=args.live,
        default_connection_class=args.connection_class,
        dry_run=args.dry_run,
        report_only=args.report_only,
    )
    conv.convert()
    return 0


def _load_db_connections(path):
    """Load the ``--db-connections`` JSON (name -> connection details dict).
    Returns ``None`` when no path / unreadable, so cloud conversion behaves
    exactly as before. Never raises."""
    if not path:
        return None
    try:
        import json as _json
        with open(path, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        if isinstance(data, dict) and data:
            # Accept either {name: {...}} or {"connections": {name: {...}}}.
            conns = data.get("connections") if "connections" in data else data
            if isinstance(conns, dict) and conns:
                return {str(k): dict(v) for k, v in conns.items()
                        if isinstance(v, dict)}
    except (OSError, ValueError) as exc:
        print(f"[CONVERT] WARNING: could not read --db-connections {path!r}: {exc}")
    return None


def _unbuild_dir_looks_useful(unbuild_dir: Path) -> bool:
    """Return True iff the unbuild produced at least one sheet file.

    Engine API unbuilds against a non-loaded app return empty JSON
    files (every JSON-RPC call gets "Invalid Params"). We treat any
    such result as a failure so the caller can fall back to direct
    parse.
    """
    objects = Path(unbuild_dir) / "objects"
    if not objects.is_dir():
        return False
    has_sheet = any(objects.glob("sheet--*.json"))
    return has_sheet


def _desktop_engine_reachable(engine_url: str, timeout: float = 1.5) -> bool:
    """Probe whether a Qlik Sense Desktop engine is listening on engine_url.

    Cheap TCP-connect test — we don't open a WebSocket here, just see
    if the port accepts connections. Used to pick between the Engine
    API unbuild path (better, but needs Desktop running) and the
    direct-parse fallback.
    """
    import socket
    import urllib.parse

    try:
        parsed = urllib.parse.urlparse(engine_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 4848
    except Exception:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except (OSError, socket.timeout):
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _resolve_output_root(args: argparse.Namespace) -> Path:
    """Pick the root output dir under which unbuilt/, data/, pbip/ live."""
    if args.output:
        return Path(args.output).resolve()
    # No explicit --output: derive a sensible default from the input.
    if args.input:
        in_path = Path(args.input).resolve()
        return in_path.parent / f"{in_path.name}_pbip"
    if args.qvf_path:
        qvf = Path(args.qvf_path).resolve()
        return Path.cwd() / f"{qvf.stem}_pbip"
    # --app only.
    short = (args.app or "qlik")[:12]
    return Path.cwd() / f"{short}_pbip"


if __name__ == "__main__":
    sys.exit(main())
