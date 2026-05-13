"""PBIP writer.

Drops the in-memory tree onto disk in the layout Power BI Desktop expects.

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

import gc
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Dict, List

from .config import SCHEMA
from .model import SemanticModel
from .utils import new_logical_id, write_json


# ---------------------------------------------------------------------------
# Windows-safe cleanup helpers
# ---------------------------------------------------------------------------

def _unlink_windows_safe(path: Path) -> None:
    """Delete a file with basic Windows read-only handling."""
    if not path.exists():
        return

    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def _rmtree_windows_safe(path: Path, retries: int = 6) -> None:
    """Remove a directory tree with Windows-safe retry handling.

    Windows can intermittently fail with:
        OSError: [WinError 145] The directory is not empty

    This commonly occurs for generated PBIP semantic model data folders,
    especially:
        <model>.SemanticModel/data/federated...

    Strategy:
      1. Retry recursive delete.
      2. Reset read-only attributes where possible.
      3. Wait for delayed handles/indexers.
      4. Rename stale folder as fallback so generation can continue.
    """
    if not path.exists():
        return

    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            gc.collect()
            shutil.rmtree(path, onerror=_onerror)
            return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.5 * attempt)

    # Fallback: rename stale folder so the writer can recreate the expected path.
    stale_path = path.with_name(
        f"{path.name}.__stale_{int(time.time())}"
    )

    try:
        path.rename(stale_path)
        print(f"  [WRITE CLEAN] Renamed stale folder to: {stale_path}")

        # Best-effort deletion after rename.
        try:
            shutil.rmtree(stale_path, onerror=_onerror)
        except OSError:
            print(
                f"  [WRITE CLEAN] Stale folder is still locked. "
                f"Conversion will continue. Delete manually later: {stale_path}"
            )

        return

    except Exception as rename_exc:
        raise OSError(
            f"Could not delete or rename generated folder: {path}. "
            f"Close Power BI Desktop, File Explorer, terminals, or any process "
            f"using this PBIP folder, then rerun."
        ) from (last_exc or rename_exc)


class PBIPWriter:

    def __init__(self, out_root: Path, name: str):
        self.root = out_root
        self.name = name
        self.report_dir = out_root / f"{name}.Report"
        self.model_dir = out_root / f"{name}.SemanticModel"

    def write(
        self, model: SemanticModel, pages: List[Dict[str, Any]],
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

        # Wipe any stale output. Running on top of an old conversion
        # leaves orphaned visual/model folders that Power BI Desktop tries
        # to load and can choke on.
        #
        # Use Windows-safe cleanup here instead of raw shutil.rmtree(path),
        # because SemanticModel/data/federated... folders can fail with:
        # OSError: [WinError 145] The directory is not empty.
        for path in (
            self.report_dir,
            self.model_dir,
            self.root / f"{self.name}.pbip",
        ):
            if not path.exists():
                continue

            if path.is_dir():
                _rmtree_windows_safe(path)
            else:
                _unlink_windows_safe(path)

        self._write_pbip()
        self._write_semantic_model(model)
        self._write_report(pages)

    # ------------------------------------------------------------------
    # PBIP master file
    # ------------------------------------------------------------------

    def _write_pbip(self) -> None:
        write_json(self.root / f"{self.name}.pbip", {
            "$schema": SCHEMA["pbip"],
            "version": "1.0",
            "artifacts": [{"report": {"path": f"{self.name}.Report"}}],
            "settings": {"enableAutoRecovery": True},
        })

    # ------------------------------------------------------------------
    # SemanticModel
    # ------------------------------------------------------------------

    def _write_semantic_model(self, model: SemanticModel) -> None:
        self.model_dir.mkdir(parents=True, exist_ok=True)

        write_json(self.model_dir / ".platform", {
            "$schema": SCHEMA["platform"],
            "metadata": {"type": "SemanticModel", "displayName": self.name},
            "config": {"version": "2.0", "logicalId": new_logical_id()},
        })

        write_json(self.model_dir / "definition.pbism", {
            "$schema": SCHEMA["pbism"],
            "version": "4.2",
            "settings": {},
        })

        model.write_tmdl(self.model_dir)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def _write_report(self, pages: List[Dict[str, Any]]) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)

        write_json(self.report_dir / ".platform", {
            "$schema": SCHEMA["platform"],
            "metadata": {"type": "Report", "displayName": self.name},
            "config": {"version": "2.0", "logicalId": new_logical_id()},
        })

        write_json(self.report_dir / "definition.pbir", {
            "$schema": SCHEMA["pbir"],
            "version": "4.0",
            "datasetReference": {"byPath": {
                "path": f"../{self.name}.SemanticModel",
            }},
        })

        defdir = self.report_dir / "definition"
        defdir.mkdir(exist_ok=True)

        write_json(defdir / "version.json", {
            "$schema": SCHEMA["version"],
            "version": "2.0.0",
        })

        write_json(defdir / "report.json", {
            "$schema": SCHEMA["report"],
            "themeCollection": {
                "baseTheme": {
                    "name": "CY24SU02",
                    "reportVersionAtImport": {
                        "visual": "1.8.89",
                        "report": "2.0.89",
                        "page": "1.3.89",
                    },
                    "type": "SharedResources",
                },
            },
            "objects": {
                "section": [{
                    "properties": {
                        "verticalAlignment": {
                            "expr": {"Literal": {"Value": "'Top'"}},
                        },
                    },
                }],
            },
            "resourcePackages": [{
                "name": "SharedResources",
                "type": "SharedResources",
                "items": [{
                    "name": "CY24SU02",
                    "path": "BaseThemes/CY24SU02.json",
                    "type": "BaseTheme",
                }],
            }],
            "settings": {
                "useStylableVisualContainerHeader": True,
                "defaultDrillFilterOtherVisuals": True,
                "useEnhancedTooltips": True,
                "useDefaultAggregateDisplayName": True,
            },
        })

        pages_dir = defdir / "pages"
        pages_dir.mkdir(exist_ok=True)

        write_json(pages_dir / "pages.json", {
            "$schema": SCHEMA["pages_metadata"],
            "pageOrder": [p["id"] for p in pages],
            "activePageName": pages[0]["id"] if pages else "",
        })

        for page in pages:
            self._write_page(pages_dir, page)

    def _write_page(self, pages_dir: Path, page: Dict[str, Any]) -> None:
        page_dir = pages_dir / page["id"]
        page_dir.mkdir(exist_ok=True)

        page_obj: Dict[str, Any] = {
            "$schema": SCHEMA["page"],
            "name": page["id"],
            "displayName": page["displayName"],
            "displayOption": "ActualSize",
            "height": page["height"],
            "width": page["width"],
        }

        # Dashboard background color carries over from twb's
        # <style-rule element='table'> on the dashboard. PBI reads
        # page background under objects.background.color.
        bg = page.get("backgroundColor")
        if bg:
            # Page background only accepts 'color' and optionally
            # 'transparency'. Visual-container background accepts 'show',
            # but page-level rejects it as an unknown property.
            page_obj["objects"] = {
                "background": [{
                    "properties": {
                        "color": {
                            "solid": {
                                "color": {
                                    "expr": {
                                        "Literal": {
                                            "Value": f"'{bg}'",
                                        },
                                    },
                                },
                            },
                        },
                    },
                }],
            }

        write_json(page_dir / "page.json", page_obj)

        if not page.get("visuals"):
            return

        visuals_dir = page_dir / "visuals"
        visuals_dir.mkdir(exist_ok=True)

        # Each visual must have a unique folder name within its page. Two
        # visuals sharing a hash would otherwise overwrite each other.
        used_names: set = set()

        for v in page["visuals"]:
            name, suffix = v["name"], 1

            while name in used_names:
                suffix += 1
                name = f"{v['name']}{suffix:02x}"[:20]

            used_names.add(name)
            v["name"] = name
            self._write_visual(visuals_dir, v)

    @staticmethod
    def _write_visual(visuals_dir: Path, visual: Dict[str, Any]) -> None:
        v_dir = visuals_dir / visual["name"]
        v_dir.mkdir(exist_ok=True)

        write_json(v_dir / "visual.json", visual)