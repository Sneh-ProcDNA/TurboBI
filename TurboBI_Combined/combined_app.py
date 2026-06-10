"""
TurboBI — Combined Web UI
=========================
A single Flask server that hosts BOTH converters behind one modern UI:

    Tableau → Power BI   (in-process; tableau_to_pbi_agent)
    Qlik    → Power BI   (subprocess; python -m qlik_to_pbi)

The two original apps each owned the root routes (``/``, ``/convert``,
``/stream`` …), so they can't co-exist unprefixed. Here each app is mounted
as a Flask Blueprint under its own prefix — ``/tableau/*`` and ``/qlik/*`` —
and the root ``/`` serves a shell page with a collapsible burger sidebar that
switches between the two converters (each rendered in an iframe of its own
embedded page). Every existing endpoint, behaviour and feature is preserved;
only the URL prefix changed.

Usage:
    python combined_app.py                # http://localhost:5000
    python combined_app.py --port 8080
"""

from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask, redirect, render_template

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from tableau_backend import tableau_bp           # noqa: E402
from qlik_backend import qlik_bp, _MAX_UPLOAD_MB  # noqa: E402

app = Flask(__name__, template_folder=str(_HERE / "templates"))

# The Qlik QVD fast-path can upload large files; honour the same cap the
# standalone Qlik app used (guard-rail only — werkzeug streams to disk).
if _MAX_UPLOAD_MB and _MAX_UPLOAD_MB > 0:
    app.config["MAX_CONTENT_LENGTH"] = _MAX_UPLOAD_MB * 1024 * 1024

app.register_blueprint(tableau_bp)
app.register_blueprint(qlik_bp)


@app.route("/")
def shell():
    """Unified shell: burger sidebar + iframe host for the two converters."""
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return {"status": "ok", "converters": ["tableau", "qlik"]}


# Friendly redirects for anyone hitting the bare converter names.
@app.route("/tableau")
def _tableau_redirect():
    return redirect("/tableau/")


@app.route("/qlik")
def _qlik_redirect():
    return redirect("/qlik/")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    print(f"TurboBI Combined UI  -> http://{args.host}:{args.port}")
    print(f"  Tableau converter  -> http://{args.host}:{args.port}/tableau/")
    print(f"  Qlik converter     -> http://{args.host}:{args.port}/qlik/")
    print(f"Working dir          -> {_HERE}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
