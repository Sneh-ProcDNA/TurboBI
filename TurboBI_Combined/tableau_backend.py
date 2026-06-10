"""
TurboBI Combined — Tableau → Power BI blueprint
================================================
Faithful port of the standalone Tableau ``app.py``, re-expressed as a Flask
Blueprint mounted under ``/tableau`` so it can live alongside the Qlik
converter in one server. Conversion still runs IN-PROCESS via
``tableau_to_pbi_agent.orchestrator.run_with_agent`` on a background thread,
streaming stdout to the browser over SSE — identical behaviour to the
original app.

Routes (all under the ``/tableau`` prefix):
    GET  /tableau/                      embedded converter UI
    POST /tableau/convert               start a job
    GET  /tableau/stream/<job_id>       SSE log stream
    GET  /tableau/status/<job_id>       job status + report
    GET  /tableau/download/<job_id>     download the zipped .pbip
    GET  /tableau/credentials-template  blank credentials template
    POST /tableau/save-credentials      persist credentials.json
"""

from __future__ import annotations

import io
import json
import queue
import shutil
import sys
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from flask import (
    Blueprint,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
)

# ---------------------------------------------------------------------------
# Paths: this module lives at the combined-app root, next to the
# tableau_to_pbi* packages. Conversion is in-process, so the packages just
# need to be importable (combined_app.py puts the root on sys.path).
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from tableau_to_pbi_agent.orchestrator import run_with_agent  # noqa: E402

UPLOAD_ROOT = _HERE / "uploads" / "tableau"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

CREDENTIALS_PATH = _HERE / "credentials.json"  # saved via UI upload

# In-memory job registry  {job_id: JobState}
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_SECONDS = 1800  # clean up 30 min after completion

tableau_bp = Blueprint("tableau", __name__, url_prefix="/tableau")


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------

def _new_job(job_id: str, work_dir: Path) -> Dict[str, Any]:
    return {
        "id": job_id,
        "work_dir": work_dir,
        "status": "pending",       # pending | running | done | error
        "log_queue": queue.Queue(),
        "log_lines": [],           # persistent copy for late-join clients
        "report": None,
        "zip_path": None,
        "zip_name": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
    }


def _push_log(job: Dict[str, Any], line: str) -> None:
    line = line.rstrip("\n")
    if not line:
        return
    job["log_lines"].append(line)
    job["log_queue"].put(line)


# ---------------------------------------------------------------------------
# Line-buffered stdout redirect
# ---------------------------------------------------------------------------

class _LineCapture(io.TextIOBase):
    """Write-only stream that splits on newlines and pushes each line
    into a job's log queue in real time."""

    def __init__(self, job: Dict[str, Any], original: io.TextIOBase):
        self._job = job
        self._original = original
        self._buf = ""

    def write(self, text: str) -> int:
        self._original.write(text)           # mirror to real stdout
        self._original.flush()
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            _push_log(self._job, line)
        return len(text)

    def flush(self) -> None:
        self._original.flush()


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_job(
    job: Dict[str, Any],
    twbx_path: str,
    credentials_path: Optional[str],
    skip_llm: bool,
) -> None:
    job["status"] = "running"
    job["started_at"] = datetime.utcnow().isoformat()

    capture = _LineCapture(job, sys.stdout)

    try:
        _push_log(job, "=== TurboBI conversion started ===")

        from contextlib import redirect_stdout
        with redirect_stdout(capture):
            report = run_with_agent(
                twbx_path,
                model=None,
                skip_llm=skip_llm,
                credentials_path=credentials_path,
            )

        job["report"] = report
        _push_log(job, "")
        _push_log(job, "=" * 50)
        _push_log(job, f"warnings before : {report['warnings_before']}")
        _push_log(job, f"warnings after  : {report['warnings_after']}")
        _push_log(job, f"hints added     : {report['hints_added']}")
        _push_log(job, f"hints total     : {report['hints_total']}")
        _push_log(job, "=" * 50)

        # Zip the output _pbip folder
        src_path = Path(twbx_path)
        stem = src_path.stem
        pbip_dir = src_path.parent / f"{stem}_pbip"

        if pbip_dir.exists():
            zip_name = f"{stem}_pbip.zip"
            zip_path = Path(job["work_dir"]) / zip_name

            _push_log(job, "")
            _push_log(job, f"[UI] Zipping output folder: {pbip_dir.name}")
            _zip_directory(pbip_dir, zip_path)
            _push_log(job, f"[UI] Zip ready: {zip_name}")

            job["zip_path"] = str(zip_path)
            job["zip_name"] = zip_name
        else:
            _push_log(job, f"[UI] WARNING: expected output folder not found: {pbip_dir}")

        job["status"] = "done"

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        # Tee to stderr so the real traceback shows in the terminal.
        print(tb, file=sys.stderr, flush=True)
        _push_log(job, f"[ERROR] {exc}")
        for tb_line in tb.splitlines():
            _push_log(job, tb_line)
        job["error"] = str(exc)
        job["status"] = "error"

    finally:
        job["finished_at"] = datetime.utcnow().isoformat()
        job["log_queue"].put(None)  # sentinel so SSE stream closes
        t = threading.Timer(_JOB_TTL_SECONDS, _cleanup_job, args=[job["id"]])
        t.daemon = True
        t.start()


def _zip_directory(src_dir: Path, dest_zip: Path) -> None:
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in src_dir.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(src_dir.parent))


def _cleanup_job(job_id: str) -> None:
    with _JOBS_LOCK:
        job = _JOBS.pop(job_id, None)
    if job and job.get("work_dir"):
        try:
            shutil.rmtree(job["work_dir"], ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@tableau_bp.route("/")
def index():
    return render_template("tableau.html", embedded=True)


@tableau_bp.route("/convert", methods=["POST"])
def convert():
    """Accept the uploaded files and kick off a background job."""
    if "twbx" not in request.files:
        return jsonify({"error": "No .twbx file provided"}), 400

    twbx_file = request.files["twbx"]
    if not twbx_file.filename.lower().endswith((".twbx", ".twb")):
        return jsonify({"error": "Only .twbx or .twb files are supported"}), 400

    skip_llm = request.form.get("skip_llm", "true").lower() in ("true", "1", "yes")

    job_id = str(uuid.uuid4())
    work_dir = UPLOAD_ROOT / job_id
    work_dir.mkdir(parents=True)

    twbx_dest = work_dir / twbx_file.filename
    twbx_file.save(str(twbx_dest))

    # Credentials: per-job upload takes priority, else use saved project file.
    creds_path: Optional[str] = None
    creds_file = request.files.get("credentials")
    if creds_file and creds_file.filename:
        creds_dest = work_dir / "credentials.json"
        creds_file.save(str(creds_dest))
        creds_path = str(creds_dest)
    elif CREDENTIALS_PATH.exists():
        creds_path = str(CREDENTIALS_PATH)

    job = _new_job(job_id, work_dir)
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    t = threading.Thread(
        target=_run_job,
        args=(job, str(twbx_dest), creds_path, skip_llm),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id})


@tableau_bp.route("/stream/<job_id>")
def stream(job_id: str):
    """Server-Sent Events endpoint — streams log lines to the browser."""

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

            if line is None:  # sentinel
                yield f"event: done\ndata: {job['status']}\n\n"
                break

            yield f"data: {line}\n\n"

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@tableau_bp.route("/status/<job_id>")
def status(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify({
        "status": job["status"],
        "report": job["report"],
        "zip_name": job["zip_name"],
        "error": job["error"],
    })


@tableau_bp.route("/download/<job_id>")
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


# ---------------------------------------------------------------------------
# Credentials — template download + save to project root
# ---------------------------------------------------------------------------

_CREDENTIALS_TEMPLATE = {
    "note": (
        "One entry per live data source. 'class' must match the Tableau connection "
        "class (sqlserver, postgres, snowflake, etc.). 'server' is optional — "
        "when omitted the entry matches any server of that class."
    ),
    "connections": [
        {
            "class": "postgres", "server": "", "database": "", "port": "5432",
            "schema": "public", "username": "", "password": "",
        },
        {
            "class": "databricks", "server": "", "http_path": "", "catalog": "",
            "schema": "", "connector_function": "DatabricksMultiCloud.Catalogs",
            "token": "",
        },
    ],
}


@tableau_bp.route("/credentials-template")
def credentials_template():
    return Response(
        json.dumps(_CREDENTIALS_TEMPLATE, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=credentials_template.json"},
    )


@tableau_bp.route("/save-credentials", methods=["POST"])
def save_credentials():
    f = request.files.get("credentials")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400
    raw = f.read()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return jsonify({"error": f"Invalid JSON: {exc}"}), 400
    if "connections" not in parsed:
        return jsonify({"error": "JSON must contain a 'connections' array"}), 400
    with open(CREDENTIALS_PATH, "wb") as fh:
        fh.write(raw)
    return jsonify({"saved": True, "connection_count": len(parsed["connections"])})
