"""PBIP writer for the Qlik -> PBIP pipeline.

Output layout (mirrors PBI Desktop's expectation):

    {name}.pbip
    {name}.Report/
        .platform
        definition.pbir
        definition/
            report.json
            version.json
            pages/
                pages.json
                {pageId}/
                    page.json
                    visuals/{visualId}/visual.json
    {name}.SemanticModel/
        .platform
        definition.pbism
        definition/
            database.tmdl
            model.tmdl
            relationships.tmdl
            cultures/en-US.tmdl
            tables/{tableName}.tmdl
"""

import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._logging import get_logger
from .config import SCHEMA
from .model import SemanticModel
from .utils import clear_mkdir_cache, mkdir_p, new_logical_id, write_json

_log = get_logger("WRITE")


class PBIPWriter:
    def __init__(self, out_root: Path, name: str):
        self.root = Path(out_root)
        self.name = name
        self.report_dir = self.root / f"{name}.Report"
        self.model_dir  = self.root / f"{name}.SemanticModel"

    def write(
        self,
        model: SemanticModel,
        pages: List[Dict[str, Any]],
        bookmarks: Optional[List[Dict[str, Any]]] = None,
        theme: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.bookmarks = bookmarks or []
        # Optional report theme (PBI theme JSON dict from
        # pbi_theme.build_report_theme). Registered as a CustomTheme so
        # default series colours match the Qlik palette.
        self.theme = theme if isinstance(theme, dict) and theme else None
        mkdir_p(self.root)

        # Wipe any stale conversion output so PBI Desktop doesn't try to
        # load orphaned folders.
        for target in (
            self.report_dir,
            self.model_dir,
            self.root / f"{self.name}.pbip",
        ):
            if not target.exists():
                continue
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            except OSError as exc:
                _log.warning(f"Could not remove stale {target}: {exc}")
                # Best-effort rename so we don't blow up.
                try:
                    target.rename(target.with_name(
                        f"{target.name}.__stale_{int(time.time())}"
                    ))
                except OSError:
                    raise

        # We may have just rmtree'd directories whose paths are still in
        # the process-wide mkdir cache (e.g. re-emitting into the same
        # output path twice in one process, as the Flask UI / CLI can).
        # Forget them so the subsequent emit re-creates the tree instead
        # of trusting a stale "already made" hit and failing on open().
        clear_mkdir_cache()

        self._write_pbip()
        self._write_semantic_model(model)
        self._write_report(pages)
        self._verify_artifacts()

    # ------------------------------------------------------------------
    def _verify_artifacts(self) -> None:
        """Fail loudly if any required PBIP/PBIR artifact was written empty
        or as invalid JSON.

        Power BI reports an empty ``definition.pbir`` (or any required
        report file) as an opaque *"ReportDefinition: Required artifact is
        missing ... IsJsonLegallyEmpty"* only when you try to OPEN the
        file. The converter always writes valid content, so an empty file
        means the write itself was truncated (disk full, antivirus quarantine,
        a locked/again-open file, an interrupted run). Catch it here and
        fail the conversion with a clear, actionable message instead of
        shipping a PBIP that dies in Desktop. Mirrors PBI's own
        ``IsJsonLegallyEmpty`` check: a file that is missing, empty/whitespace,
        or whose JSON is an empty object/array/null is rejected."""
        required = [
            self.root / f"{self.name}.pbip",
            self.report_dir / "definition.pbir",
            self.report_dir / "definition" / "report.json",
            self.report_dir / "definition" / "version.json",
            self.model_dir / "definition.pbism",
        ]
        # Every page/visual file we emitted must also be non-empty JSON.
        defdir = self.report_dir / "definition"
        required += sorted(defdir.glob("pages/**/page.json"))
        required += sorted(defdir.glob("pages/**/visual.json"))

        problems: List[str] = []
        for path in required:
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                problems.append(f"{path.name}: missing")
                continue
            except OSError as exc:
                problems.append(f"{path.name}: unreadable ({exc})")
                continue
            if not raw.strip():
                problems.append(f"{path.name}: empty")
                continue
            try:
                obj = json.loads(raw)
            except ValueError as exc:
                problems.append(f"{path.name}: invalid JSON ({exc})")
                continue
            if obj is None or (isinstance(obj, (dict, list)) and len(obj) == 0):
                problems.append(f"{path.name}: legally-empty JSON")

        if problems:
            raise RuntimeError(
                "PBIP artifact verification failed -- these files would make "
                "Power BI report 'Required artifact is missing': "
                + "; ".join(problems)
                + ". This is a write failure (disk full, antivirus, or a "
                "locked file), not a content bug -- re-run the conversion."
            )
        _log.info(f"Artifact check: {len(required)} required files non-empty + valid JSON.")

    # ------------------------------------------------------------------
    def _write_pbip(self) -> None:
        write_json(self.root / f"{self.name}.pbip", {
            "$schema":   SCHEMA["pbip"],
            "version":   "1.0",
            "artifacts": [{"report": {"path": f"{self.name}.Report"}}],
            "settings":  {"enableAutoRecovery": True},
        })

    def _write_semantic_model(self, model: SemanticModel) -> None:
        mkdir_p(self.model_dir)
        write_json(self.model_dir / ".platform", {
            "$schema": SCHEMA["platform"],
            "metadata": {"type": "SemanticModel", "displayName": self.name},
            "config":  {"version": "2.0", "logicalId": new_logical_id()},
        })
        write_json(self.model_dir / "definition.pbism", {
            "$schema":  SCHEMA["pbism"],
            "version":  "4.2",
            "settings": {},
        })

        # Copy CSV files into <name>.SemanticModel/data/. The TMDL
        # partition's File.Contents call references them via the
        # RepoPath parameter (anchored at the SemanticModel folder).
        if model.table_csv:
            data_dir = self.model_dir / "data"
            mkdir_p(data_dir)
            for src_path in set(model.table_csv.values()):
                dest = data_dir / src_path.name
                try:
                    shutil.copy2(src_path, dest)
                    _log.info(f"copied {src_path.name} -> data/")
                except OSError as exc:
                    _log.warning(f"could not copy {src_path}: {exc}")

        model.write_tmdl(self.model_dir)

    def _write_report(self, pages: List[Dict[str, Any]]) -> None:
        mkdir_p(self.report_dir)
        write_json(self.report_dir / ".platform", {
            "$schema": SCHEMA["platform"],
            "metadata": {"type": "Report", "displayName": self.name},
            "config":  {"version": "2.0", "logicalId": new_logical_id()},
        })
        write_json(self.report_dir / "definition.pbir", {
            "$schema": SCHEMA["pbir"],
            "version": "4.0",
            "datasetReference": {"byPath": {
                "path": f"../{self.name}.SemanticModel",
            }},
        })
        defdir = self.report_dir / "definition"
        mkdir_p(defdir)
        write_json(defdir / "version.json", {
            "$schema": SCHEMA["version"],
            "version": "2.0.0",
        })

        # Theme registration. The base theme is always PBI's stock
        # CY24SU02; when a Qlik-matching theme dict was supplied it is
        # written under StaticResources/RegisteredResources/ and layered
        # on top as the report's CustomTheme (PBI applies customTheme
        # over baseTheme, so only the keys the theme sets -- the data
        # palette -- are overridden).
        theme_collection: Dict[str, Any] = {
            "baseTheme": {
                "name": "CY24SU02",
                "reportVersionAtImport": {
                    "visual": "1.8.89",
                    "report": "2.0.89",
                    "page":   "1.3.89",
                },
                "type": "SharedResources",
            },
        }
        resource_packages: List[Dict[str, Any]] = [{
            "name": "SharedResources",
            "type": "SharedResources",
            "items": [{
                "name": "CY24SU02",
                "path": "BaseThemes/CY24SU02.json",
                "type": "BaseTheme",
            }],
        }]
        if self.theme:
            theme_file = f"{self.theme.get('name') or 'QlikSenseColors'}.json"
            write_json(
                self.report_dir / "StaticResources" / "RegisteredResources" / theme_file,
                self.theme,
            )
            # ThemeMetadata (report schema 3.2.0) REQUIRES all three of
            # name / reportVersionAtImport / type -- mirror the base
            # theme's version block.
            theme_collection["customTheme"] = {
                "name": theme_file,
                "reportVersionAtImport": {
                    "visual": "1.8.89",
                    "report": "2.0.89",
                    "page":   "1.3.89",
                },
                "type": "RegisteredResources",
            }
            resource_packages.insert(0, {
                "name": "RegisteredResources",
                "type": "RegisteredResources",
                "items": [{
                    "name": theme_file,
                    "path": theme_file,
                    "type": "CustomTheme",
                }],
            })
            _log.info(
                f"Registered Qlik-matching report theme ({theme_file}, "
                f"{len(self.theme.get('dataColors') or [])} data colours)."
            )

        write_json(defdir / "report.json", {
            "$schema": SCHEMA["report"],
            "themeCollection": theme_collection,
            "resourcePackages": resource_packages,
            "settings": {
                "useStylableVisualContainerHeader": True,
                "defaultDrillFilterOtherVisuals":   True,
                "useEnhancedTooltips":              True,
                "useDefaultAggregateDisplayName":   True,
            },
        })

        pages_dir = defdir / "pages"
        mkdir_p(pages_dir)
        write_json(pages_dir / "pages.json", {
            "$schema":     SCHEMA["pages_metadata"],
            "pageOrder":   [p["id"] for p in pages],
            "activePageName": pages[0]["id"] if pages else "",
        })

        for page in pages:
            self._write_page(pages_dir, page)

        # Bookmarks. Power BI stores each bookmark as a JSON file
        # under definition/bookmarks/. We can't reconstruct field
        # selections from Qlik's bookmark blobs (the qBookmark schema
        # references qPatches by field id, which doesn't translate),
        # but we DO emit one PBI bookmark per Qlik bookmark with the
        # name + display name set so the user has scaffolding to
        # complete in Desktop's bookmark pane.
        #
        # PBIP bookmark schema gotchas (each cost a re-run to find):
        #   * File name is ``<name>.bookmark.json`` (the ``.bookmark``
        #     middle is required -- PBI Desktop ignores plain ``<name>.json``).
        #   * ``bookmarks.json``'s ``items`` is an array of OBJECTS
        #     ``{"name": "<id>"}``, not an array of bare-string ids.
        #     With strings, PBI silently ignores the entire bookmark
        #     list and the bookmark pane stays empty.
        #   * ``explorationState`` MUST carry ``version`` (we emit
        #     ``"1.0"``). Missing it == silent rejection.
        #   * ``sections`` cannot be ``{}``; it must contain at least
        #     one entry keyed by page id with a ``visualContainers``
        #     field (also possibly empty). An empty sections dict
        #     means "no captured state" and PBI may skip the bookmark.
        if self.bookmarks:
            bk_dir = defdir / "bookmarks"
            mkdir_p(bk_dir)
            bookmark_ids: List[str] = []
            # Qlik sheet id -> PBI page id, so a bookmark lands on its own
            # sheet's page rather than always page 1.
            sheet_to_page = {
                p["sheet_id"]: p["id"] for p in pages if p.get("sheet_id")
            }
            for bm in self.bookmarks:
                # Skip malformed entries.
                title = ""
                bm_sheet = ""
                if isinstance(bm, dict):
                    info = bm.get("qInfo") or {}
                    meta = bm.get("qMetaDef") or {}
                    title = (meta.get("title") or info.get("qId") or "").strip()
                    bm_sheet = (bm.get("sheetId") or "").strip()
                if not title:
                    continue
                bm_id = new_logical_id()
                bookmark_ids.append(bm_id)
                # Landing page = the bookmark's own sheet, else page 1.
                active_section = (
                    sheet_to_page.get(bm_sheet)
                    or (pages[0]["id"] if pages else "")
                )
                # ``sections`` needs at least one page entry. Without
                # captured selection state we just declare an empty
                # ``visualContainers`` block for the active section --
                # PBI Desktop renders it as a no-op bookmark the user
                # can complete via "Add to current bookmark".
                sections: Dict[str, Any] = {}
                if active_section:
                    sections[active_section] = {
                        "visualContainers": {},
                    }
                write_json(bk_dir / f"{bm_id}.bookmark.json", {
                    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmark/1.0.0/schema.json",
                    "name":           bm_id,
                    "displayName":    title,
                    "explorationState": {
                        "version":       "1.0",
                        "activeSection": active_section,
                        "sections":      sections,
                    },
                })
            if bookmark_ids:
                write_json(bk_dir / "bookmarks.json", {
                    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmarksMetadata/1.0.0/schema.json",
                    "items": [{"name": bid} for bid in bookmark_ids],
                })
                _log.info(f"Emitted {len(bookmark_ids)} bookmark(s).")

    def _write_page(self, pages_dir: Path, page: Dict[str, Any]) -> None:
        page_dir = pages_dir / page["id"]
        mkdir_p(page_dir)
        page_obj = {
            "$schema":       SCHEMA["page"],
            "name":          page["id"],
            "displayName":   page["displayName"],
            "displayOption": "ActualSize",
            "height":        page["height"],
            "width":         page["width"],
        }
        # Pass page-level background through if the builder set one.
        # Power BI's page schema accepts a ``background`` object with
        # ``color`` + ``transparency`` (or an ``image`` for richer
        # canvases, not used here).
        bg = page.get("background")
        if isinstance(bg, dict):
            page_obj["background"] = bg
        write_json(page_dir / "page.json", page_obj)

        visuals = page.get("visuals") or []
        if not visuals:
            return

        visuals_dir = page_dir / "visuals"
        mkdir_p(visuals_dir)
        seen: set = set()
        for v in visuals:
            name = v["name"]
            suffix = 1
            while name in seen:
                suffix += 1
                name = f"{v['name']}{suffix:02x}"[:20]
            seen.add(name)
            v["name"] = name
            v_dir = visuals_dir / name
            mkdir_p(v_dir)
            write_json(v_dir / "visual.json", v)
