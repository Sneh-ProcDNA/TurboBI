"""High-level orchestrator for the Qlik -> PBIP pipeline.

Two modes:
  1. Convert from an existing `qlik app unbuild` output folder.
  2. Run `qlik app unbuild --app <ID> --dir <tmp>` first, then convert.

The CLI in `__main__` wires the choice.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._logging import configure_default, get_logger
from .conversion_report import (
    ConversionReport, populate_from_model, write_report,
)
from .credentials import (
    CredentialStore, load_credentials, write_credentials_manifest,
)
from .ir import QlikIR
from .model import SemanticModel
from .parser import parse_qlik_output
from .report import ReportBuilder
from .utils import resolve_qlik_command, safe_filename
from .writer import PBIPWriter

_log = get_logger("CONVERT")


class Converter:
    def __init__(
        self,
        qlik_output_dir: str | Path,
        output: Optional[str | Path] = None,
        name: Optional[str] = None,
        data_dir: Optional[str | Path] = None,
        credentials_path: Optional[str | Path] = None,
        live_mode: bool = False,
        default_connection_class: str = "",
        db_connections: Optional[Dict[str, Dict[str, Any]]] = None,
        dry_run: bool = False,
        report_only: bool = False,
    ):
        self.qlik_dir = Path(qlik_output_dir).resolve()
        self.dry_run = bool(dry_run)
        self.report_only = bool(report_only)

        if output:
            self.output = Path(output)
        else:
            self.output = self.qlik_dir.parent / f"{self.qlik_dir.name}_pbip"

        self.name = name  # resolved later from app-properties if missing
        # Optional directory of CSV exports; each table the model
        # produces is matched against the CSVs by name and (if matched)
        # gets a `Csv.Document` partition pointing at a copy of the
        # file under `<name>.SemanticModel/data/`.
        self.data_dir: Optional[Path] = (
            Path(data_dir).resolve() if data_dir else None
        )
        # Live-DB credentials. When set, the model attaches a per-table
        # ``connection`` dict from the matching credentials entry and
        # the partition switches from CSV/empty-stub to live M.
        self.credentials_path: Optional[Path] = (
            Path(credentials_path).resolve() if credentials_path else None
        )
        self.live_mode: bool = bool(live_mode)
        self.default_connection_class: str = (default_connection_class or "").strip()
        # User-supplied DB connection details, keyed by Qlik connection name.
        # Repoints script-detected DB-source tables at the live source. See
        # SemanticModel.db_connections.
        self.db_connections: Optional[Dict[str, Dict[str, Any]]] = db_connections

    # ------------------------------------------------------------------
    def convert(self) -> Path:
        configure_default()
        _log.info(f"Qlik -> PBIP | source: {self.qlik_dir}")
        if self.data_dir:
            _log.info(f"Data dir: {self.data_dir}")
        _log.info("[1/4] Parsing Qlik output...")
        ir = parse_qlik_output(self.qlik_dir)

        # Resolve the Qlik colour palette -> PBI report theme NOW (the
        # app-properties slot that carries the theme id is released
        # after the model build). The writer registers the theme; the
        # report builder uses the palette's primary for single-series
        # chart defaults.
        from .pbi_theme import build_report_theme
        theme_info = build_report_theme(
            ir.get("qlik_theme"),
            (ir.get("app") or {}).get("theme"),
        )
        _log.info(
            f"Report theme: {len(theme_info['data_colors'])} Qlik data "
            f"colours, primary {theme_info['primary']} "
            f"(source: {theme_info['source']})."
        )
        evaluated = ir.get("evaluated") or {}
        n_eval = len((evaluated.get("expressions") or {})) if isinstance(evaluated, dict) else 0
        if n_eval:
            _log.info(f"Evaluated text-expression snapshots available: {n_eval}.")

        # Load credentials (if any). The user can drop a
        # ``credentials.json`` at the project root and we'll pick it up
        # automatically when ``--live`` is set; an explicit
        # ``--credentials path`` always wins.
        creds: Optional[CredentialStore] = None
        if self.credentials_path:
            creds = load_credentials(self.credentials_path)
            _log.info(
                f"Credentials: {self.credentials_path} "
                f"({len(creds.entries)} entries, live_mode={self.live_mode})"
            )

        _log.info("[2/4] Building semantic model...")
        model = SemanticModel(
            ir,
            data_dir=self.data_dir,
            credentials=creds,
            live_mode=self.live_mode,
            default_connection_class=self.default_connection_class,
            db_connections=self.db_connections,
        )
        model.build()

        # The model build consumed the heavy raw-input documents
        # (loadmodel / engine schema / field universe / script body).
        # Release the slots nothing downstream reads again so the report
        # build + write run with a smaller resident set. ``release_raw_inputs``
        # is correctness-neutral (it preserves every slot with a live
        # reader -- sheets, master_objects, dimensions, variables,
        # script_blocks, bookmarks). The model and report share this one
        # IR object, so the release is visible to both.
        if isinstance(ir, QlikIR):
            ir.release_raw_inputs()

        if not self.name:
            self.name = safe_filename(model.app_title or "Report")

        _log.info(f"[3/4] Building report (name='{self.name}')...")
        report = ReportBuilder(ir, model, theme_palette=theme_info)
        pages = report.build_pages()

        self._preflight_warnings: List[str] = []
        if self.dry_run:
            _log.info(
                "[4/4] --dry-run: skipping PBIP write. "
                f"Plan: {len(model.tables)} tables, {len(model.measures)} measures, "
                f"{len(model.relationships)} relationships, {len(pages)} pages."
            )
        elif self.report_only:
            _log.info(
                "[4/4] --report-only: skipping PBIP write; "
                "conversion_report.md will still be emitted below."
            )
        else:
            _log.info(f"[4/4] Writing PBIP to {self.output} ...")
            writer = PBIPWriter(self.output, self.name)
            writer.write(
                model, pages,
                bookmarks=ir.get("bookmarks") or [],
                theme=theme_info.get("pbi_theme"),
            )

            # Pre-flight structural validation. Catches dangling refs,
            # unknown visualContainerObjects keys, missing visualType
            # fields -- the kind of error PBI Desktop reports only as
            # "Cannot resolve all the paths..." with no per-file context.
            try:
                from .preflight import run_preflight
                self._preflight_warnings = run_preflight(self.output, self.name)
            except Exception as exc:  # noqa: BLE001
                _log.warning(f"Pre-flight check failed to run: {exc}")

        # Write a sibling conversion_report.md so the user has a punch
        # list of measures that fell back to BLANK() stubs and other
        # issues that need manual review. The report lives next to the
        # PBIP (under <OUT>/pbip/) so it travels with the artefact.
        # Even --dry-run produces this; it's the explicit goal of
        # --report-only.
        if self.dry_run and not self.report_only:
            # Dry-run with no explicit report flag: skip writes entirely.
            _log.info(f"Dry-run done.")
            return self.output
        try:
            report_data = ConversionReport(app_name=self.name)
            populate_from_model(report_data, model, pages)
            # Preflight warnings get appended to the report.
            if getattr(self, "_preflight_warnings", None):
                report_data.preflight_warnings = list(self._preflight_warnings)
            # Per-cell build failures that were degraded to placeholders.
            for msg in getattr(report, "build_issues", []) or []:
                report_data.add("warning", "visual", "cell", msg,
                                suggestion="Rebuild this visual manually in PBI Desktop.")
            # Dynamic textbox expressions that had no engine-evaluated
            # snapshot (offline conversion / pre-sidecar unbuild folder)
            # -- the textbox shows the label/formula instead of the value.
            for expr in sorted(getattr(report, "text_eval_misses", []) or [])[:20]:
                report_data.add(
                    "info", "visual", "textbox expression",
                    f"Textbox expression shown as label, not value "
                    f"(no engine snapshot): {expr[:120]}",
                    suggestion=(
                        "Re-run the conversion with a Qlik connection "
                        "(cloud context or Desktop engine) so text "
                        "expressions are evaluated to their values."
                    ),
                )
            # Qlik conditional-visibility expressions that were detected
            # but not wired as PBI filters (PBI's filter schema needs a
            # real measure ref). Surface them so the user knows.
            for cond in getattr(report, "_visibility_notes", []) or []:
                report_data.add(
                    "info", "visual", "visibility condition",
                    f"Qlik show/calc condition not wired as a PBI filter: {cond}",
                    suggestion="Recreate as a visual-level filter or bookmark in PBI Desktop if the visual should be conditionally shown.",
                )
            self.output.mkdir(parents=True, exist_ok=True)
            report_path = self.output / "conversion_report.md"
            write_report(report_data, report_path)
            _log.info(
                f"Conversion report: {report_path} "
                f"(stubbed measures: {report_data.measures_stubbed})"
            )
        except Exception as exc:
            _log.warning(f"Could not write conversion report: {exc}")

        # Credentials manifest -- one row per table that landed on a
        # live DB partition, so the user knows what to wire up in PBI
        # Desktop > Transform Data > Data source settings.
        if model.manifest_entries:
            try:
                manifest_path = self.output / "credentials_manifest.json"
                write_credentials_manifest(manifest_path, model.manifest_entries)
            except Exception as exc:
                _log.warning(f"Could not write credentials manifest: {exc}")

        _log.info(f"Done. Open {self.output / (self.name + '.pbip')}")
        return self.output


# ---------------------------------------------------------------------------
# Optional: run `qlik app unbuild` so the caller only needs the app id.
# ---------------------------------------------------------------------------

def unbuild_via_cli(
    app_id: str,
    unbuild_dir: Optional[str | Path] = None,
    qlik_command: str = "qlik",
) -> Path:
    """Run `qlik app unbuild --app <id> --dir <unbuild_dir>` and return the path.

    The qlik binary is located via ``shutil.which`` -- it accepts a bare
    ``"qlik"`` (resolved against PATH, honouring PATHEXT on Windows so
    ``qlik.exe`` / ``qlik.bat`` are found), an absolute path like
    ``C:\\Users\\me\\qlik.exe``, or any other invocable command name.
    Falling back to ``shell=True`` here (the previous behaviour) is
    fragile on Windows because cmd.exe's PATH resolution differs from
    the parent process's, which is the failure mode we hit.
    """
    if unbuild_dir is None:
        unbuild_dir = Path(tempfile.mkdtemp(prefix="qlik_unbuild_"))
    else:
        unbuild_dir = Path(unbuild_dir)
        unbuild_dir.mkdir(parents=True, exist_ok=True)

    resolved = resolve_qlik_command(qlik_command)
    if not resolved:
        raise RuntimeError(
            f"Could not locate the qlik CLI. Tried PATH lookup of "
            f"{qlik_command!r} and common install locations "
            f"(~/qlik.exe, %LOCALAPPDATA%/Programs/qlik-cli/qlik.exe, "
            f"C:/Program Files/qlik-cli/qlik.exe, etc.). Either install "
            f"the qlik CLI (https://qlik.dev/toolkits/qlik-cli) or pass "
            f"--qlik-cmd <absolute-path-to-qlik.exe>."
        )
    _log.info(f"Using qlik CLI at: {resolved}")

    cmd = [resolved, "app", "unbuild", "--app", app_id, "--dir", str(unbuild_dir)]
    _log.info("Running: " + " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, shell=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"qlik unbuild failed -- could not invoke {resolved!r}. "
            f"Pass --qlik-cmd <absolute-path-to-qlik.exe> to override. "
            f"Original error: {exc}"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"qlik unbuild failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return Path(unbuild_dir)


def cleanup_unbuild_dir(path: Path) -> None:
    """Best-effort removal of a temp unbuild dir."""
    try:
        shutil.rmtree(path)
    except OSError:
        pass
