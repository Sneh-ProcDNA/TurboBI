"""
TurboBI Combined — Qlik → Power BI blueprint
=============================================
Faithful port of the standalone Qlik ``app.py``, re-expressed as a Flask
Blueprint mounted under ``/qlik``. Conversion runs as a SUBPROCESS
(``python -m qlik_to_pbi``) from the combined-app root, streaming combined
stdout/stderr to the browser over SSE — identical behaviour to the original
app. Only the package-relative imports (``from .x``) were rewritten to
absolute (``from qlik_to_pbi.x``) because this module lives at the app root
rather than inside the package.

Routes (all under the ``/qlik`` prefix):
    GET  /qlik/                     embedded converter UI
    POST /qlik/convert              start a job (JSON or multipart+QVD)
    GET  /qlik/estimate             extract size/time estimate
    GET  /qlik/tables               per-table list for QVD mapping
    GET  /qlik/db-connections       detect DB load sources
    GET  /qlik/stream/<job_id>      SSE log stream
    GET  /qlik/status/<job_id>      job status (categorised error)
    GET  /qlik/log/<job_id>         full captured raw log
    GET  /qlik/download/<job_id>    download the zipped output tree
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from flask import (
    Blueprint,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
)

# ---------------------------------------------------------------------------
# Paths. This module lives at the combined-app root, alongside the
# ``qlik_to_pbi`` package, so the subprocess runs ``python -m qlik_to_pbi``
# with cwd = the root (where the package is importable).
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_RUN_CWD = _HERE
if str(_RUN_CWD) not in sys.path:
    sys.path.insert(0, str(_RUN_CWD))

UPLOAD_ROOT = _HERE / "uploads" / "qlik"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

# Max upload size for QVD files on multipart /convert, in MB (guard-rail only;
# werkzeug streams uploads to disk). Raise via QLIK_MAX_UPLOAD_MB.
try:
    _MAX_UPLOAD_MB = int(os.environ.get("QLIK_MAX_UPLOAD_MB", "8192") or "8192")
except ValueError:
    _MAX_UPLOAD_MB = 8192

qlik_bp = Blueprint("qlik", __name__, url_prefix="/qlik")


@qlik_bp.app_errorhandler(413)
def _payload_too_large(_e):
    """A QVD upload exceeded MAX_CONTENT_LENGTH -> clean JSON for the SPA."""
    return jsonify({
        "error": (
            f"Uploaded QVD file(s) exceed the {_MAX_UPLOAD_MB} MB server upload "
            f"limit. Map smaller QVDs, or raise QLIK_MAX_UPLOAD_MB on the server "
            f"and restart."
        ),
        "error_category": "data_source",
        "error_title": "Upload too large",
        "error_detail": (
            f"The combined upload exceeded {_MAX_UPLOAD_MB} MB. The limit is set "
            f"by the QLIK_MAX_UPLOAD_MB environment variable."
        ),
    }), 413


_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_SECONDS = 1800


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_job(job_id: str, work_dir: Path) -> Dict[str, Any]:
    return {
        "id": job_id,
        "work_dir": work_dir,
        "status": "pending",
        "log_queue": queue.Queue(),
        "log_lines": [],
        "zip_path": None,
        "zip_name": None,
        "error": None,
        "error_category": None,
        "error_title": None,
        "error_detail": None,
        "started_at": None,
        "finished_at": None,
    }


def _push_log(job: Dict[str, Any], line: str) -> None:
    line = line.rstrip("\n")
    job["log_lines"].append(line)
    job["log_queue"].put(line)


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

_CATEGORY_META = {
    "api": (
        "Qlik connection / API error",
        "TurboBI couldn't authenticate or connect to Qlik Cloud. The API "
        "key may be missing, incorrect, or expired, or the App ID may be "
        "wrong or not accessible with this key. Re-create the API key "
        "(Qlik Cloud → Settings → API keys) or refresh your qlik "
        "context, double-check the App ID, and try again.",
    ),
    "semantic_model": (
        "Semantic model creation failed",
        "The Qlik data model couldn't be translated into a Power BI "
        "semantic model (tables, relationships, measures, or DAX). The raw "
        "output below points to the table or measure that failed.",
    ),
    "data_source": (
        "Data source issue",
        "TurboBI hit a problem with the data sources — fetching table "
        "data, binding CSVs, or building partitions. The model structure "
        "may be fine; check data connectivity or the Fetch-data option, "
        "then see the raw output below.",
    ),
    "visual": (
        "Visual conversion issue",
        "One or more Qlik sheets or visuals couldn't be rebuilt as Power BI "
        "report pages. The raw output below points to the visual or sheet "
        "that failed.",
    ),
    "technical": (
        "Technical failure",
        "The conversion failed due to an unexpected technical error. Expand "
        "the raw output below to see the full detail.",
    ),
}

_TB_MODULE_CATEGORY = {
    "qlik_context": "api", "cloud": "api", "engine_unbuild": "api",
    "engine_fetch": "data_source", "fetch_data": "data_source",
    "csv_schema": "data_source", "partition_m": "data_source",
    "qvf_direct": "data_source", "script_to_m": "data_source",
    "script_parser": "data_source",
    "model": "semantic_model",
    "report": "visual",
}

_API_SIG = re.compile(
    r"\b(401|403|429)\b|unauthor|forbidden|authenticat|bearer|"
    r"api[\s_-]?key|\bexpired\b|invalid.{0,12}token|missing server or bearer|"
    r"could not load tenant|no usable qlik|no qlik cli|could not locate the qlik|"
    r"handshake|connection refused|could not connect|getaddrinfo|"
    r"name or service|certificate|\bssl\b|\b1006\b|app not found|"
    r"access denied|invalid api key", re.I,
)
_DATA_SIG = re.compile(
    r"hypercube|gettabledata|fetch.?data|object export|\.csv\b|\bcsv\b|"
    r"partition|data source|datasource|csv\.document|\bqvd\b|odbc|"
    r"sql\.database|connection string", re.I,
)
_MODEL_SIG = re.compile(
    r"semantic model|\.tmdl\b|\btmdl\b|relationship|cardinality|dangling|"
    r"measure name|\bdax\b|engine schema", re.I,
)
_VISUAL_SIG = re.compile(
    r"\bvisual\b|\bsheet\b|\bchart\b|\bpage\b|report\.json|\bpbir\b|"
    r"visualcontainer", re.I,
)


def _error_region(log_lines) -> str:
    full = "\n".join(log_lines)
    idx = full.rfind("Traceback (most recent call last)")
    if idx != -1:
        return full[idx:]
    errish = [ln for ln in log_lines if re.search(
        r"\berror\b|\[error\]|exception|\bfail|\braise\b|cannot|could not|"
        r"invalid|denied|unauthor|forbidden|\bmissing\b|not found|refused|"
        r"timed out", ln, re.I)]
    if errish:
        return "\n".join(errish)
    benign = re.compile(r"^\s*\[(CONVERT|MODEL|PREFLIGHT|UI|WRITE|INFO)\]", re.I)
    return "\n".join(ln for ln in log_lines[-15:] if not benign.match(ln))


def _category_payload(category: str) -> Dict[str, str]:
    title, message = _CATEGORY_META.get(category, _CATEGORY_META["technical"])
    return {"category": category, "title": title, "message": message}


def _classify_failure(log_lines) -> Dict[str, str]:
    region = _error_region(log_lines)
    if _API_SIG.search(region):
        return _category_payload("api")
    tb_mods = re.findall(r"qlik_to_pbi[\\/](\w+)\.py", "\n".join(log_lines))
    for mod in reversed(tb_mods):
        if mod in _TB_MODULE_CATEGORY:
            return _category_payload(_TB_MODULE_CATEGORY[mod])
    if _DATA_SIG.search(region):
        return _category_payload("data_source")
    if _MODEL_SIG.search(region):
        return _category_payload("semantic_model")
    if _VISUAL_SIG.search(region):
        return _category_payload("visual")
    full = "\n".join(log_lines)
    if "Building report" in full and "Writing PBIP" not in full:
        return _category_payload("visual")
    if "Building semantic model" in full and "Building report" not in full:
        return _category_payload("semantic_model")
    return _category_payload("technical")


def _set_job_error(job: Dict[str, Any], payload: Dict[str, str], short: str) -> None:
    job["error"] = short
    job["error_category"] = payload["category"]
    job["error_title"] = payload["title"]
    job["error_detail"] = payload["message"]
    job["status"] = "error"


# ---------------------------------------------------------------------------
# Connection probes
# ---------------------------------------------------------------------------

def _qlik_cli_available(qlik_cmd: str) -> bool:
    from qlik_to_pbi.utils import resolve_qlik_command
    try:
        return resolve_qlik_command(qlik_cmd) is not None
    except Exception:
        return False


def _has_qlik_context() -> bool:
    try:
        from qlik_to_pbi.qlik_context import load_qlik_context
        return load_qlik_context() is not None
    except Exception:
        return False


_EXTRACT_CELLS_PER_SEC = 15_000
_APP_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_PRUNE_CELL_RETAIN = 0.6
_PARALLEL_WORST = 2.0
_CLOUD_WORKER_CAP = 8


def _estimate_extract(app_id: str, prune: bool = True) -> Dict[str, Any]:
    if not _APP_ID_RE.match(app_id):
        return {"ok": False, "reason": "app id is not a UUID"}
    try:
        from qlik_to_pbi.qlik_context import load_qlik_context
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "context module unavailable"}
    ctx = load_qlik_context()
    if not ctx:
        return {"ok": False, "reason": "no cloud context"}
    import requests
    base = ctx.tenant.rstrip("/")
    if base.startswith(("ws://", "wss://")):
        base = "https://" + base.split("://", 1)[1]
    url = f"{base}/api/v1/apps/{app_id}/data/metadata"
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {ctx.api_key}"}, timeout=30,
    )
    resp.raise_for_status()
    meta = resp.json()
    tables = [t for t in (meta.get("tables") or []) if not t.get("is_system")]
    rows = sum(int(t.get("no_of_rows") or 0) for t in tables)
    cells = sum(int(t.get("no_of_rows") or 0) * max(1, int(t.get("no_of_fields") or 1))
                for t in tables)
    serial_full = cells / _EXTRACT_CELLS_PER_SEC + len(tables) * 1.5
    eff_cells = cells * (_PRUNE_CELL_RETAIN if prune else 1.0)
    serial_eff = eff_cells / _EXTRACT_CELLS_PER_SEC + len(tables) * 0.8
    try:
        from qlik_to_pbi.engine_fetch import _auto_worker_count
        best_workers = float(_auto_worker_count(int(rows), _CLOUD_WORKER_CAP))
    except Exception:  # noqa: BLE001
        best_workers = 12.0
    est_low = serial_eff / best_workers + len(tables) * 0.2
    est_high = serial_eff / _PARALLEL_WORST + len(tables) * 0.2
    return {
        "ok": True,
        "tables": len(tables),
        "rows": rows,
        "bytes": int(meta.get("static_byte_size") or 0),
        "est_full_seconds": int(round(serial_full)),
        "est_low_seconds": int(round(est_low)),
        "est_high_seconds": int(round(est_high)),
        "pruned": bool(prune),
    }


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_job(
    job: Dict[str, Any],
    app_id: str,
    output_dir: str,
    qlik_cmd: str,
    fetch_data: bool,
    prune_columns: bool = True,
    qvd_dir: str | None = None,
    db_config: Dict[str, Any] | None = None,
) -> None:
    job["status"] = "running"
    job["started_at"] = _now()

    try:
        # Optional QVD fast-path.
        prefetched_dir: str | None = None
        if qvd_dir and Path(qvd_dir).is_dir():
            qvd_files = sorted(Path(qvd_dir).glob("*.qvd"))
            if qvd_files:
                from qlik_to_pbi.qvd_ingest import (
                    qvd_available, transcode_qvd_map, transcoded_table_names,
                )
                if not qvd_available():
                    _push_log(
                        job,
                        "[QVD] Uploaded QVD(s) ignored: pyqvd/pyarrow not "
                        "installed on the server. Falling back to the Engine "
                        "fetch. (pip install pyqvd pyarrow to enable.)",
                    )
                else:
                    mapping = {p.stem: str(p) for p in qvd_files}
                    _push_log(
                        job,
                        f"[QVD] Transcoding {len(mapping)} uploaded QVD(s) "
                        f"to Parquet (local, no engine round-trips)...",
                    )
                    pre = Path(output_dir) / "_prefetched"
                    results = transcode_qvd_map(mapping, pre)
                    ok = transcoded_table_names(results)
                    for tname, info in results.items():
                        if "error" in info:
                            _push_log(
                                job,
                                f"[QVD]   {tname}: FAILED ({info['error']}) "
                                f"-> will fetch from engine instead",
                            )
                        else:
                            _push_log(
                                job,
                                f"[QVD]   {tname}: {info['rows']:,} rows x "
                                f"{info['cols']} cols -> {info['bytes']:,} bytes",
                            )
                    if ok:
                        prefetched_dir = str(pre)
                        _push_log(
                            job,
                            f"[QVD] {len(ok)} table(s) supplied from QVD; the "
                            f"Engine fetch will skip them.",
                        )

        use_context = _has_qlik_context()
        cli_ok = _qlik_cli_available(qlik_cmd)
        if use_context:
            mode_label = "cloud-context (Engine API)"
            cmd = [
                sys.executable, "-m", "qlik_to_pbi",
                "--use-qlik-context",
                "--cloud-app-id", app_id,
                "--output", output_dir,
            ]
            if not fetch_data:
                cmd.extend(["--data-dir", str(Path(output_dir) / "_nodata")])
        elif cli_ok:
            mode_label = f"qlik CLI ({qlik_cmd})"
            cmd = [
                sys.executable, "-m", "qlik_to_pbi",
                "--app", app_id,
                "--output", output_dir,
                "--qlik-cmd", qlik_cmd,
            ]
            if fetch_data:
                cmd.append("--fetch-data")
        else:
            _push_log(
                job,
                "[ERROR] No usable Qlik connection. Either install the "
                "qlik CLI (https://qlik.dev/toolkits/qlik-cli) and put "
                "qlik.exe on PATH, or configure a cloud context via "
                "`qlik context create` so ~/.qlik/contexts.yml has a "
                "tenant + API key.",
            )
            _set_job_error(
                job, _category_payload("api"),
                "No qlik CLI or cloud context available",
            )
            return

        if fetch_data and not prune_columns:
            cmd.append("--no-prune-columns")

        if prefetched_dir and use_context:
            cmd.extend(["--prefetched-data-dir", prefetched_dir])

        if db_config and use_context:
            safe_cfg = {
                name: {k: v for k, v in fields.items()
                       if k not in ("token", "password", "pat", "access_token")}
                for name, fields in db_config.items()
            }
            db_json = Path(output_dir) / "_db_connections.json"
            db_json.parent.mkdir(parents=True, exist_ok=True)
            db_json.write_text(json.dumps(safe_cfg, indent=2), encoding="utf-8")
            cmd.extend(["--db-connections", str(db_json)])
            for name, fields in safe_cfg.items():
                _push_log(
                    job,
                    f"[DB] Connection {name!r}: {fields.get('class','?')} @ "
                    f"{fields.get('server','?')} -> tables will Import from the "
                    f"live source (not the loaded snapshot).",
                )

        _push_log(job, f"[UI] Unbuild mode      : {mode_label}")
        _push_log(job, "=== TurboBI conversion started ===")
        _push_log(job, f"[UI] Working directory : {_RUN_CWD}")
        _push_log(job, f"[UI] Command           : {' '.join(cmd)}")
        _push_log(job, "")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(_RUN_CWD),
        )

        for raw_line in proc.stdout:
            _push_log(job, raw_line.rstrip("\n"))

        proc.wait()

        _push_log(job, "")
        if proc.returncode != 0:
            _push_log(job, f"[ERROR] Process exited with code {proc.returncode}")
            _set_job_error(
                job, _classify_failure(job["log_lines"]),
                f"Converter exited with code {proc.returncode}",
            )
            return

        pbip_dir = Path(output_dir) / "pbip"
        if not pbip_dir.exists():
            candidates = list(Path(output_dir).glob("*_pbip"))
            if candidates:
                pbip_dir = candidates[0]

        if not pbip_dir.exists():
            _push_log(job, "[UI] WARNING: pbip/ output folder not found after conversion")
            job["status"] = "done"
            return

        short_id = app_id[:8]
        zip_name = f"qlik_{short_id}_pbip.zip"
        zip_path = Path(job["work_dir"]) / zip_name

        zip_root = Path(output_dir)
        arcname_root = f"qlik_{short_id}"
        _push_log(
            job,
            f"[UI] Zipping output tree: {arcname_root}/{{pbip,data,unbuilt}}",
        )
        _zip_directory(zip_root, zip_path, arcname_root=arcname_root)
        _push_log(job, f"[UI] Zip ready: {zip_name}")

        job["zip_path"] = str(zip_path)
        job["zip_name"] = zip_name

        _push_log(job, "")
        _push_log(job, "=" * 50)
        _push_log(job, "=== Conversion complete ===")
        job["status"] = "done"

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr, flush=True)
        _push_log(job, f"[ERROR] {exc}")
        for line in tb.splitlines():
            _push_log(job, line)
        _set_job_error(job, _classify_failure(job["log_lines"]), str(exc))

    finally:
        job["finished_at"] = _now()
        job["log_queue"].put(None)
        t = threading.Timer(_JOB_TTL_SECONDS, _cleanup_job, args=[job["id"]])
        t.daemon = True
        t.start()


def _zip_directory(
    src_dir: Path, dest_zip: Path, arcname_root: str | None = None,
) -> None:
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in src_dir.rglob("*"):
            if fp.is_file():
                rel = fp.relative_to(src_dir)
                arc = Path(arcname_root) / rel if arcname_root else (
                    fp.relative_to(src_dir.parent)
                )
                zf.write(fp, arc)


def _cleanup_job(job_id: str) -> None:
    with _JOBS_LOCK:
        job = _JOBS.pop(job_id, None)
    if job and job.get("work_dir"):
        shutil.rmtree(job["work_dir"], ignore_errors=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@qlik_bp.route("/")
def index():
    return render_template("qlik.html", embedded=True)


def _form_bool(val: Any, default: bool) -> bool:
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


@qlik_bp.route("/convert", methods=["POST"])
def convert():
    is_multipart = bool(
        request.content_type
        and request.content_type.startswith("multipart/")
    )
    if is_multipart:
        form = request.form
        app_id        = (form.get("app_id") or "").strip()
        qlik_cmd      = (form.get("qlik_cmd") or "qlik").strip()
        fetch_data    = _form_bool(form.get("fetch_data"), True)
        prune_columns = _form_bool(form.get("prune_columns"), True)
        db_raw        = form.get("db_connections")
    else:
        data          = request.get_json(silent=True) or {}
        app_id        = (data.get("app_id") or "").strip()
        qlik_cmd      = (data.get("qlik_cmd") or "qlik").strip()
        fetch_data    = bool(data.get("fetch_data", True))
        prune_columns = bool(data.get("prune_columns", True))
        db_raw        = data.get("db_connections")

    if not app_id:
        return jsonify({"error": "No App ID provided"}), 400

    db_config: Dict[str, Any] | None = None
    if db_raw:
        try:
            parsed = json.loads(db_raw) if isinstance(db_raw, str) else db_raw
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            cleaned = {
                str(name): {k: str(v).strip() for k, v in fields.items()
                            if v is not None and str(v).strip()}
                for name, fields in parsed.items()
                if isinstance(fields, dict)
                and (fields.get("server") or fields.get("host"))
            }
            db_config = cleaned or None

    job_id     = str(uuid.uuid4())
    work_dir   = UPLOAD_ROOT / job_id
    work_dir.mkdir(parents=True)
    output_dir = str(work_dir / "output")

    qvd_dir: str | None = None
    if is_multipart and request.files:
        from qlik_to_pbi.utils import safe_filename
        qd = work_dir / "qvd_uploads"
        saved = 0
        for key, fs in request.files.items(multi=True):
            if not key.startswith("qvdfile:") or not fs or not fs.filename:
                continue
            table = key[len("qvdfile:"):].strip()
            if not table:
                continue
            qd.mkdir(exist_ok=True)
            fs.save(str(qd / f"{safe_filename(table, max_len=80)}.qvd"))
            saved += 1
        if saved:
            qvd_dir = str(qd)

    job = _new_job(job_id, work_dir)
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    threading.Thread(
        target=_run_job,
        args=(job, app_id, output_dir, qlik_cmd, fetch_data, prune_columns,
              qvd_dir, db_config),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@qlik_bp.route("/estimate")
def estimate():
    app_id = (request.args.get("app_id") or "").strip()
    if not app_id:
        return jsonify({"ok": False, "reason": "no app id"}), 400
    prune = (request.args.get("prune") or "1").strip().lower() not in (
        "0", "false", "no", "")
    try:
        return jsonify(_estimate_extract(app_id, prune=prune))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "reason": str(exc)[:200]})


@qlik_bp.route("/tables")
def tables():
    app_id = (request.args.get("app_id") or "").strip()
    if not _APP_ID_RE.match(app_id):
        return jsonify({"ok": False, "reason": "app id is not a UUID"})
    try:
        from qlik_to_pbi.qvd_ingest import qvd_available
        qvd_ok = bool(qvd_available())
    except Exception:  # noqa: BLE001
        qvd_ok = False
    try:
        from qlik_to_pbi.qlik_context import load_qlik_context
        ctx = load_qlik_context()
        if not ctx:
            return jsonify({"ok": False, "reason": "no cloud context",
                            "qvd_supported": qvd_ok})
        import requests
        base = ctx.tenant.rstrip("/")
        if base.startswith(("ws://", "wss://")):
            base = "https://" + base.split("://", 1)[1]
        resp = requests.get(
            f"{base}/api/v1/apps/{app_id}/data/metadata",
            headers={"Authorization": f"Bearer {ctx.api_key}"}, timeout=30,
        )
        resp.raise_for_status()
        meta = resp.json()
        out = []
        for t in (meta.get("tables") or []):
            if t.get("is_system"):
                continue
            out.append({
                "name": t.get("name"),
                "rows": int(t.get("no_of_rows") or 0),
                "cols": int(t.get("no_of_fields") or 0),
            })
        out.sort(key=lambda r: -r["rows"])
        return jsonify({"ok": True, "tables": out, "qvd_supported": qvd_ok,
                        "max_upload_mb": _MAX_UPLOAD_MB})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "reason": str(exc)[:200],
                        "qvd_supported": qvd_ok, "max_upload_mb": _MAX_UPLOAD_MB})


@qlik_bp.route("/db-connections")
def db_connections_detect():
    app_id = (request.args.get("app_id") or "").strip()
    if not _APP_ID_RE.match(app_id):
        return jsonify({"ok": False, "reason": "app id is not a UUID"})
    try:
        from qlik_to_pbi.qlik_context import load_qlik_context
        from qlik_to_pbi.engine_fetch import EngineClient, DEFAULT_ENGINE_URL
        from qlik_to_pbi.script_parser import parse_db_sources
        from qlik_to_pbi.model import SemanticModel
        ctx = load_qlik_context()
        if not ctx:
            return jsonify({"ok": False, "reason": "no cloud context"})
        client = EngineClient(
            qvf_path=None, engine_base_url=DEFAULT_ENGINE_URL,
            tenant=ctx.tenant, api_key=ctx.api_key, app_id=app_id,
        )
        try:
            client.connect()
            script = client.request("GetScript", client.app_handle, []).get("qScript", "")
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        db_sources = parse_db_sources(script or "")
        if not db_sources:
            return jsonify({"ok": True, "connections": []})
        by_conn: Dict[str, Dict[str, Any]] = {}
        for tname, info in db_sources.items():
            cname = str(info.get("connection") or "").strip() or "(unnamed)"
            grp = by_conn.setdefault(cname, {
                "name": cname,
                "class": SemanticModel._infer_db_class(cname),
                "tables": [],
                "catalog": info.get("catalog") or "",
                "schema": info.get("schema") or "",
            })
            grp["tables"].append(tname)

        def _fields_for(cls: str):
            if cls == "databricks":
                return [
                    {"key": "server", "label": "Server hostname",
                     "placeholder": "adb-xxxx.azuredatabricks.net", "required": True},
                    {"key": "http_path", "label": "HTTP path",
                     "placeholder": "/sql/1.0/warehouses/abc123", "required": True},
                    {"key": "catalog", "label": "Catalog", "required": False},
                    {"key": "schema", "label": "Schema", "required": False},
                ]
            if cls == "snowflake":
                return [
                    {"key": "server", "label": "Server (account URL)", "required": True},
                    {"key": "warehouse", "label": "Warehouse", "required": True},
                    {"key": "database", "label": "Database", "required": False},
                    {"key": "schema", "label": "Schema", "required": False},
                ]
            return [
                {"key": "server", "label": "Server / host", "required": True},
                {"key": "database", "label": "Database", "required": True},
                {"key": "schema", "label": "Schema", "required": False},
            ]
        conns = []
        for grp in by_conn.values():
            grp["tables"].sort()
            grp["fields"] = _fields_for(grp["class"])
            conns.append(grp)
        conns.sort(key=lambda g: g["name"].lower())
        return jsonify({"ok": True, "connections": conns})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "reason": str(exc)[:200]})


@qlik_bp.route("/stream/<job_id>")
def stream(job_id: str):
    def _generate():
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if not job:
            yield "data: [ERROR] Unknown job id\n\n"
            return
        for line in list(job["log_lines"]):
            yield f"data: {line}\n\n"
        while True:
            try:
                line = job["log_queue"].get(timeout=30)
            except queue.Empty:
                yield ": keep-alive\n\n"
                if job["status"] in ("done", "error"):
                    break
                continue
            if line is None:
                yield f"event: done\ndata: {job['status']}\n\n"
                break
            yield f"data: {line}\n\n"

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@qlik_bp.route("/status/<job_id>")
def status(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify({
        "status":         job["status"],
        "zip_name":       job["zip_name"],
        "error":          job["error"],
        "error_category": job.get("error_category"),
        "error_title":    job.get("error_title"),
        "error_detail":   job.get("error_detail"),
    })


@qlik_bp.route("/log/<job_id>")
def job_log(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify({"lines": list(job["log_lines"])})


@qlik_bp.route("/download/<job_id>")
def download(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job or not job.get("zip_path"):
        return jsonify({"error": "No output available"}), 404
    return send_file(
        job["zip_path"],
        as_attachment=True,
        download_name=job["zip_name"],
        mimetype="application/zip",
    )
