"""Direct QVF parser — read .qvf files without cloud OR Desktop engine.

This is a port of the standalone QVF parser merged in from a sibling
project. It auto-detects the on-disk QVF format (which varies a lot
between Qlik Cloud and Qlik Sense Desktop) and extracts the embedded
JSON / load script directly, with **no engine** required:

* **ZIP** — Qlik Sense Cloud exports. Entries are plain JSON / .qvs.
* **SQLite** — Qlik Sense Desktop's on-disk format. The objects live
  in BLOB columns of an ``objects`` (or similarly-named) table.
* **Qlik proprietary binary container** (magic ``FF FF``) — falls back
  to zlib-block scanning + targeted JSON anchoring around ``"qType"``
  markers.
* **Gzip-wrapped** content.
* **Raw bytes JSON scan** (utf-8 / utf-16-le / utf-16-be) as a last
  resort.

The :func:`qvf_to_unbuild_dir` adapter writes the parsed contents into
the same JSON folder layout that ``qlik app unbuild`` produces, so the
existing :mod:`qlik_to_pbi.parser` reads it unchanged.

When :func:`qvf_to_unbuild_dir` returns successfully, the converter can
skip the engine-unbuild step entirely -- so the full pipeline runs
without Qlik Sense Desktop running and without any cloud creds.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._logging import get_logger
from .utils import safe_filename

_log = get_logger("QVF-DIRECT")


# ---------------------------------------------------------------------------
# Parsed-contents container
# ---------------------------------------------------------------------------

@dataclass
class QvfContents:
    """Raw parsed contents of a QVF. No interpretation yet."""

    app_properties: Dict[str, Any] = field(default_factory=dict)
    load_script: str = ""
    sheets: List[Dict[str, Any]] = field(default_factory=list)
    objects: List[Dict[str, Any]] = field(default_factory=list)
    dimensions: List[Dict[str, Any]] = field(default_factory=list)
    measures: List[Dict[str, Any]] = field(default_factory=list)
    variables: List[Dict[str, Any]] = field(default_factory=list)
    bookmarks: List[Dict[str, Any]] = field(default_factory=list)
    raw_file_map: Dict[str, bytes] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class QvfParser:
    """Direct QVF reader. Detects format from the file header and dispatches."""

    def __init__(self, qvf_path: str | Path):
        self.qvf_path = Path(qvf_path)
        if not self.qvf_path.exists():
            raise FileNotFoundError(f"QVF not found: {qvf_path}")

    # ------------------------------------------------------------------
    def parse(self) -> QvfContents:
        header = self.qvf_path.read_bytes()[:32]

        # ZIP (most common: Qlik Sense Cloud / Server export)
        if header[:2] == b"PK":
            return self._parse_zip()

        # SQLite (Qlik Sense Desktop)
        if header[:16] == b"SQLite format 3\x00":
            return self._parse_sqlite()

        # Try ZIP regardless (some files have a short preamble)
        try:
            return self._parse_zip()
        except (zipfile.BadZipFile, Exception):
            pass

        # Try SQLite regardless
        try:
            return self._parse_sqlite()
        except Exception:
            pass

        # Gzip-wrapped
        if header[:2] == b"\x1f\x8b":
            try:
                return self._parse_gzip()
            except Exception:
                pass

        # Qlik proprietary binary container (header FF FF 01 00)
        if header[:2] == b"\xff\xff":
            try:
                return self._parse_qlik_binary()
            except Exception:
                pass

        # Last resort: raw bytes scan across encodings.
        for result in (
            self._parse_raw_json_scan("utf-8"),
            self._parse_raw_json_scan("utf-16-le"),
            self._parse_raw_json_scan("utf-16-be"),
        ):
            if (
                result.sheets
                or result.objects
                or result.measures
                or result.load_script
            ):
                return result

        raise ValueError(
            f"Could not parse '{self.qvf_path.name}'.\n"
            f"File header (hex): {header.hex()}\n"
            f"File size: {self.qvf_path.stat().st_size:,} bytes\n\n"
            "This file uses Qlik's proprietary binary container format "
            "(magic bytes FF FF). To convert it, re-export it from Qlik "
            "Sense as a standard QVF:\n"
            "  Qlik Sense Hub > App Menu (...) > Export app\n"
            "The exported .qvf will be a ZIP archive that this tool can parse."
        )

    # ------------------------------------------------------------------
    # ZIP
    # ------------------------------------------------------------------
    def _parse_zip(self) -> QvfContents:
        contents = QvfContents()
        with zipfile.ZipFile(self.qvf_path, "r") as zf:
            names = zf.namelist()
            _log.info(f"ZIP entries ({len(names)}): {names[:10]}")
            for name in names:
                if name.endswith("/"):
                    continue
                try:
                    data = zf.read(name)
                except KeyError:
                    continue
                contents.raw_file_map[name] = data
                self._dispatch(name, data, contents)

        # Second pass: targeted JSON scan across all raw bytes for any
        # sheet objects that were not caught by the primary dispatch.
        known_sheet_ids = {
            (s.get("qInfo") or {}).get("qId") for s in contents.sheets
        }
        scan = self._parse_targeted_json_scan(
            self.qvf_path.read_bytes(), "utf-8",
        )
        for s in scan.sheets:
            sid = (s.get("qInfo") or {}).get("qId")
            if sid not in known_sheet_ids:
                contents.sheets.append(s)
                known_sheet_ids.add(sid)
        for o in scan.objects:
            contents.objects.append(o)

        _log.info(
            f"After deep scan -- sheets: {len(contents.sheets)}, "
            f"objects: {len(contents.objects)}"
        )
        return contents

    # ------------------------------------------------------------------
    # SQLite
    # ------------------------------------------------------------------
    def _parse_sqlite(self) -> QvfContents:
        contents = QvfContents()
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        shutil.copy2(self.qvf_path, tmp.name)
        try:
            con = sqlite3.connect(tmp.name)
            con.row_factory = sqlite3.Row
            tables = [
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            _log.info(f"SQLite tables: {tables}")
            self._sqlite_extract(con, contents, tables)
            con.close()
        finally:
            os.unlink(tmp.name)
        return contents

    def _sqlite_extract(
        self,
        con: sqlite3.Connection,
        contents: QvfContents,
        tables: List[str],
    ) -> None:
        # Objects (sheets / charts / master items)
        obj_table = next(
            (t for t in tables if t.lower() in ("objects", "object")), None,
        )
        if obj_table:
            for row in con.execute(f'SELECT * FROM "{obj_table}"'):
                keys = list(row.keys())
                data_col = next(
                    (
                        k for k in keys
                        if k.lower() in (
                            "data", "layout", "properties", "json",
                        )
                    ),
                    None,
                )
                id_col = next(
                    (
                        k for k in keys
                        if k.lower() in ("id", "objectid", "object_id")
                    ),
                    None,
                )
                obj_id = row[id_col] if id_col else "unknown"
                if data_col and row[data_col]:
                    raw = row[data_col]
                    data_bytes = (
                        raw if isinstance(raw, bytes)
                        else raw.encode("utf-8", errors="replace")
                    )
                    contents.raw_file_map[f"sqlite/objects/{obj_id}"] = data_bytes
                    self._dispatch(
                        f"sqlite/objects/{obj_id}", data_bytes, contents,
                    )

        # Script
        script_table = next(
            (
                t for t in tables
                if t.lower() in ("script", "loadscript", "load_script")
            ),
            None,
        )
        if script_table:
            for row in con.execute(f'SELECT * FROM "{script_table}" LIMIT 1'):
                keys = list(row.keys())
                col = next(
                    (
                        k for k in keys
                        if any(x in k.lower() for x in (
                            "script", "code", "text",
                        ))
                    ),
                    keys[0] if keys else None,
                )
                if col and row[col]:
                    val = row[col]
                    contents.load_script = (
                        val if isinstance(val, str)
                        else val.decode("utf-8", errors="replace")
                    )

        # App properties
        for tbl in tables:
            if tbl.lower() in ("appproperties", "app_properties", "app"):
                for row in con.execute(f'SELECT * FROM "{tbl}" LIMIT 1'):
                    keys = list(row.keys())
                    data_col = next(
                        (
                            k for k in keys
                            if k.lower() in ("data", "json", "properties")
                        ),
                        None,
                    )
                    if data_col and row[data_col]:
                        raw = row[data_col]
                        data_bytes = (
                            raw if isinstance(raw, bytes)
                            else raw.encode("utf-8", errors="replace")
                        )
                        parsed = self._safe_json(data_bytes)
                        if parsed:
                            contents.app_properties = parsed
                break

        # Full scan: catch objects stored under non-standard tables/columns.
        seen_hashes: set = set()
        for tbl in tables:
            try:
                for row in con.execute(f'SELECT * FROM "{tbl}"'):
                    for key in row.keys():
                        val = row[key]
                        if not val:
                            continue
                        if isinstance(val, bytes):
                            data_bytes = val
                        elif isinstance(val, str):
                            data_bytes = val.encode("utf-8", errors="replace")
                        else:
                            continue
                        h = hash(data_bytes)
                        if h in seen_hashes:
                            continue
                        seen_hashes.add(h)
                        self._dispatch(
                            f"sqlite/{tbl}/{key}", data_bytes, contents,
                        )
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Qlik proprietary binary container
    # ------------------------------------------------------------------
    def _parse_qlik_binary(self) -> QvfContents:
        """Handle Qlik's internal binary container (magic FF FF 01 00).

        Strategy:
        1. Try zlib-compressed sub-blocks (each may itself be a QVF) and
           merge results from every block that parses cleanly.
        2. Targeted JSON extraction by searching for ``"qType"`` anchors,
           run across every encoding (UTF-8 / UTF-16-LE / UTF-16-BE /
           latin-1) -- different sheets are sometimes encoded
           differently inside the same container, so we union.
        3. App-properties scan (separate because it has no ``qType``).
        4. Load script text search.
        """
        data = self.qvf_path.read_bytes()
        merged = QvfContents()

        # Step 1: try every plausible zlib-compressed block, merge each
        # successfully-parsed candidate into the running result.
        zlib_magics = {b"\x78\x9c", b"\x78\xda", b"\x78\x01", b"\x78\x5e"}
        for i in range(4, len(data) - 8, 4):
            if data[i:i + 2] not in zlib_magics:
                continue
            for length in (
                len(data) - i,
                min(32 * 1024 * 1024, len(data) - i),
            ):
                try:
                    decompressed = zlib.decompress(data[i:i + length])
                except zlib.error:
                    break
                tmp = tempfile.NamedTemporaryFile(suffix=".qvf", delete=False)
                tmp.write(decompressed)
                tmp.close()
                try:
                    candidate = QvfParser(tmp.name).parse()
                    _merge_contents(merged, candidate)
                except Exception:
                    pass
                finally:
                    os.unlink(tmp.name)
                # One length attempt per offset is enough.
                break

        # Step 2: targeted JSON extraction across encodings. We KEEP
        # appending to ``merged`` (don't reset it -- the zlib loop above
        # may have already produced sheets / objects).
        for enc in ("utf-8", "utf-16-le", "utf-16-be", "latin-1"):
            result = self._parse_targeted_json_scan(data, enc)
            _merge_contents(merged, result)
            if not merged.load_script:
                self._extract_load_script(data, enc, merged)
            if not merged.app_properties:
                merged.app_properties = (
                    _scan_app_properties(data, enc) or {}
                )

        score = (
            len(merged.sheets) * 10
            + len(merged.objects) * 5
            + len(merged.measures) * 3
            + (1 if merged.load_script else 0)
        )
        if score > 0:
            return merged

        # Step 4: load script only as last resort.
        for enc in ("utf-16-le", "utf-8", "latin-1"):
            self._extract_load_script(data, enc, merged)
            if merged.load_script:
                return merged

        raise ValueError("No parseable content found in binary QVF.")

    def _parse_targeted_json_scan(
        self, data: bytes, encoding: str,
    ) -> QvfContents:
        """Find ``"qType"`` anchors and extract the surrounding JSON object.

        Better than depth-tracking through the whole file (which breaks
        on binary garbage). Works for both UTF-8 and UTF-16-LE.
        """
        contents = QvfContents()
        try:
            text = data.decode(encoding, errors="replace")
        except Exception:
            return contents

        anchor_re = re.compile(
            r'"qInfo"\s*:\s*\{[^}]*"qType"\s*:\s*"([^"]+)"',
            re.S,
        )

        seen_starts: set = set()
        for anchor_match in anchor_re.finditer(text):
            obj_start = text.rfind("{", 0, anchor_match.start())
            if obj_start < 0 or obj_start in seen_starts:
                continue
            seen_starts.add(obj_start)

            depth = 0
            obj_end: Optional[int] = None
            for i in range(obj_start, min(obj_start + 500_000, len(text))):
                ch = text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        obj_end = i + 1
                        break
            if obj_end is None:
                continue

            chunk = text[obj_start:obj_end]
            try:
                parsed = json.loads(chunk)
            except json.JSONDecodeError:
                trimmed = chunk[chunk.find("{"):]
                try:
                    parsed = json.loads(trimmed)
                except json.JSONDecodeError:
                    continue

            if not isinstance(parsed, dict):
                continue

            qtype = (parsed.get("qInfo", {}) or {}).get("qType", "").lower()
            if not qtype:
                qtype = (
                    (parsed.get("qProperty", {}) or {}).get("qInfo", {}) or {}
                ).get("qType", "").lower()

            if qtype == "sheet":
                contents.sheets.append(parsed)
            elif qtype == "measure":
                contents.measures.append(parsed)
            elif qtype == "dimension":
                contents.dimensions.append(parsed)
            elif qtype == "variable":
                contents.variables.append(parsed)
            elif qtype == "bookmark":
                contents.bookmarks.append(parsed)
            elif qtype:
                contents.objects.append(parsed)
        return contents

    def _extract_load_script(
        self, data: bytes, encoding: str, contents: QvfContents,
    ) -> None:
        """Search raw bytes for a Qlik LOAD script body."""
        try:
            text = data.decode(encoding, errors="ignore")
        except Exception:
            return
        m = re.search(
            r"((?:SET\s+\w|//[^\n]*\n|/\*|\bLOAD\b|\bFROM\b)"
            r".{200,}?(?:;\s*\n\s*;\s*|\Z))",
            text, re.S | re.I,
        )
        if m and len(m.group(1)) > 100:
            contents.load_script = m.group(1)
            return
        m2 = re.search(
            r"(\b(?:LOAD|SET ThousandSep).{50,})", text, re.S | re.I,
        )
        if m2 and len(m2.group(1)) > 50:
            contents.load_script = m2.group(1)[:20000]

    # ------------------------------------------------------------------
    # Gzip
    # ------------------------------------------------------------------
    def _parse_gzip(self) -> QvfContents:
        raw = gzip.decompress(self.qvf_path.read_bytes())
        tmp = tempfile.NamedTemporaryFile(suffix=".qvf", delete=False)
        tmp.write(raw)
        tmp.close()
        try:
            return QvfParser(tmp.name).parse()
        finally:
            os.unlink(tmp.name)

    # ------------------------------------------------------------------
    # Raw JSON scan
    # ------------------------------------------------------------------
    def _parse_raw_json_scan(
        self, encoding: str = "utf-8",
    ) -> QvfContents:
        data = self.qvf_path.read_bytes()
        return self._parse_targeted_json_scan(data, encoding)

    # ------------------------------------------------------------------
    # Dispatcher (per zip / sqlite entry)
    # ------------------------------------------------------------------
    def _dispatch(
        self, name: str, data: bytes, contents: QvfContents,
    ) -> None:
        lower = name.lower()

        if lower.endswith("loadscript.qvs") or lower.endswith("script.qvs"):
            contents.load_script = data.decode("utf-8", errors="replace")
            return

        parsed = self._safe_json(data)
        if parsed is None or not isinstance(parsed, dict):
            return

        if lower.endswith("appproperties.json") or lower.endswith("app.json") \
                or lower.endswith("app-properties.json"):
            contents.app_properties = parsed
            # Fall through: app-properties files sometimes embed lists.

        qtype = (parsed.get("qInfo", {}) or {}).get("qType", "").lower()
        if not qtype:
            qtype = (parsed.get("qMetaDef", {}) or {}).get("qType", "").lower()
        if not qtype:
            qtype = (
                (parsed.get("qProperty", {}) or {}).get("qInfo", {}) or {}
            ).get("qType", "").lower()
        if not qtype:
            qtype = parsed.get("qGenericType", "").lower()

        if qtype == "sheet":
            contents.sheets.append(parsed)
        elif qtype == "dimension":
            contents.dimensions.append(parsed)
        elif qtype == "measure":
            contents.measures.append(parsed)
        elif qtype == "variable":
            contents.variables.append(parsed)
        elif qtype == "bookmark":
            contents.bookmarks.append(parsed)
        elif qtype == "loadmodel":
            contents.objects.append(parsed)
        elif qtype:
            contents.objects.append(parsed)

        self._extract_nested_objects(parsed, contents)

    def _extract_nested_objects(
        self, parsed: dict, contents: QvfContents,
    ) -> None:
        """Recursively extract items from nested qAppObjectList / qItems lists."""
        existing_sheet_ids = {
            (s.get("qInfo") or {}).get("qId") for s in contents.sheets
        }
        existing_obj_ids = {
            (o.get("qInfo") or {}).get("qId") for o in contents.objects
        }

        candidate_lists: List[Any] = []
        for key in (
            "qAppObjectList", "qItems", "sheets", "objects",
            "qObjectList", "qChildList",
        ):
            val = parsed.get(key)
            if isinstance(val, list):
                candidate_lists.extend(val)
            elif isinstance(val, dict):
                inner = val.get("qItems") or val.get("items") or []
                if isinstance(inner, list):
                    candidate_lists.extend(inner)

        for item in candidate_lists:
            if not isinstance(item, dict):
                continue
            qinfo: Dict[str, Any] = item.get("qInfo") or {}
            qprop_info: Dict[str, Any] = (
                (item.get("qProperty") or {}).get("qInfo") or {}
            )
            itype = (
                qinfo.get("qType")
                or qprop_info.get("qType")
                or item.get("qGenericType")
                or ""
            ).lower()
            qid = qinfo.get("qId") or qprop_info.get("qId")

            if itype == "sheet" and qid not in existing_sheet_ids:
                contents.sheets.append(item)
                existing_sheet_ids.add(qid)
            elif itype == "measure":
                contents.measures.append(item)
            elif itype == "dimension":
                contents.dimensions.append(item)
            elif itype and qid not in existing_obj_ids:
                contents.objects.append(item)
                existing_obj_ids.add(qid)

    @staticmethod
    def _safe_json(data: bytes) -> Optional[Any]:
        try:
            text = data.decode("utf-8", errors="replace").lstrip("﻿")
            return json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None


def _merge_contents(target: QvfContents, source: QvfContents) -> None:
    """Union ``source`` into ``target``, deduping by qId where possible."""
    if source.app_properties and not target.app_properties:
        target.app_properties = source.app_properties
    if source.load_script and not target.load_script:
        target.load_script = source.load_script

    def _id(item: Dict[str, Any]) -> Optional[str]:
        return (
            (item.get("qInfo") or {}).get("qId")
            or ((item.get("qProperty") or {}).get("qInfo") or {}).get("qId")
        )

    for attr in ("sheets", "objects", "dimensions", "measures",
                 "variables", "bookmarks"):
        existing_ids = {
            _id(x) for x in getattr(target, attr) if isinstance(x, dict)
        }
        for item in getattr(source, attr) or []:
            iid = _id(item) if isinstance(item, dict) else None
            if iid and iid in existing_ids:
                continue
            getattr(target, attr).append(item)
            if iid:
                existing_ids.add(iid)


_APP_TITLE_RE = re.compile(
    r'"qTitle"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.S,
)
_APP_RELOAD_RE = re.compile(
    r'"qLastReloadTime"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.S,
)
_APP_VERSION_RE = re.compile(
    r'"qSavedInProductVersion"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.S,
)


def _scan_app_properties(data: bytes, encoding: str) -> Dict[str, Any]:
    """Pull qTitle / qLastReloadTime / qSavedInProductVersion out of raw bytes.

    The qType-anchored scan misses app properties because app-properties.json
    has no ``qType`` field. We grep for the well-known string keys directly.
    """
    try:
        text = data.decode(encoding, errors="replace")
    except Exception:
        return {}
    out: Dict[str, Any] = {}
    m = _APP_TITLE_RE.search(text)
    if m:
        out["qTitle"] = m.group(1).encode("utf-8").decode("unicode_escape", errors="replace") if "\\" in m.group(1) else m.group(1)
    m = _APP_RELOAD_RE.search(text)
    if m:
        out["qLastReloadTime"] = m.group(1)
    m = _APP_VERSION_RE.search(text)
    if m:
        out["qSavedInProductVersion"] = m.group(1)
    return out


# ---------------------------------------------------------------------------
# Adapter: QvfContents -> qlik-app-unbuild folder layout
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def qvf_to_unbuild_dir(
    qvf_path: str | Path, output_dir: str | Path,
) -> Path:
    """Direct-parse a .qvf and write the cloud-unbuild JSON layout.

    Returns the resolved output directory so the caller can hand it
    straight to :class:`qlik_to_pbi.converter.Converter` as
    ``qlik_output_dir``.

    Raises ``RuntimeError`` if the parse returned a usable result but
    no sheets and no script could be extracted -- the converter has
    nothing to work with in that case and the caller can fall back to
    :func:`qlik_to_pbi.engine_unbuild.unbuild_via_engine`.
    """
    qvf_path = Path(qvf_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "objects").mkdir(parents=True, exist_ok=True)

    _log.info(f"Direct-parsing {qvf_path}")
    contents = QvfParser(qvf_path).parse()
    # If the parser couldn't find an app title, default to the .qvf
    # filename stem so the PBIP isn't named "Untitled". Cloud-unbuild
    # always has qTitle in app-properties; binary containers often don't.
    if not (contents.app_properties or {}).get("qTitle"):
        contents.app_properties = dict(contents.app_properties or {})
        contents.app_properties["qTitle"] = qvf_path.stem
    _log.info(
        f"Parsed: {len(contents.sheets)} sheets, "
        f"{len(contents.objects)} objects, "
        f"{len(contents.dimensions)} dimensions, "
        f"{len(contents.measures)} measures, "
        f"{len(contents.variables)} variables, "
        f"{len(contents.load_script)} chars of script."
    )
    if not contents.sheets and not contents.load_script and not contents.objects:
        raise RuntimeError(
            "Direct parse produced no usable content. The QVF may be in "
            "an unsupported binary container -- try `--qvf-path` with a "
            "running Qlik Sense Desktop to fall back to the Engine API."
        )

    _write_app_properties(contents, output_dir)
    _write_script(contents, output_dir)
    _write_dimensions(contents, output_dir)
    _write_measures(contents, output_dir)
    _write_variables(contents, output_dir)
    _write_sheets(contents, output_dir)
    _write_master_objects(contents, output_dir)
    _write_loadmodel(contents, output_dir)

    _log.info(f"Direct unbuild written to {output_dir}")
    return output_dir


# ---------------------------------------------------------------------------
# Per-file writers
# ---------------------------------------------------------------------------

def _write_app_properties(contents: QvfContents, out: Path) -> None:
    props = dict(contents.app_properties or {})
    if "qTitle" not in props:
        # Fall back to the QVF filename as a sensible default.
        # The parser elsewhere needs *something* to name the report.
        props["qTitle"] = props.get("qTitle") or "Untitled"
    _write_json(out / "app-properties.json", props)


def _write_script(contents: QvfContents, out: Path) -> None:
    _write_text(out / "script.qvs", contents.load_script or "")


def _write_dimensions(contents: QvfContents, out: Path) -> None:
    _write_json(out / "dimensions.json", contents.dimensions or [])


def _write_measures(contents: QvfContents, out: Path) -> None:
    _write_json(out / "measures.json", contents.measures or [])


def _write_variables(contents: QvfContents, out: Path) -> None:
    _write_json(out / "variables.json", contents.variables or [])


def _write_sheets(contents: QvfContents, out: Path) -> None:
    """One sheet--<slug>-<qid>.json per parsed sheet.

    Some QVF formats (notably Desktop SQLite) store sheets as a flat
    ``{qInfo, qMetaDef, cells, ...}`` object -- no ``qProperty`` wrapper.
    Our :mod:`qlik_to_pbi.parser` always reads ``obj["qProperty"]``, so
    if the wrapper is missing we synthesise one here.
    """
    for sheet in contents.sheets:
        if "qProperty" in sheet:
            body = sheet
        else:
            # Flat shape -- wrap it so the parser can find qProperty.
            body = {
                "qProperty": sheet,
                "qChildren": _extract_child_property_trees(sheet, contents),
            }

        qinfo = (body.get("qProperty") or {}).get("qInfo") or {}
        qid = (qinfo.get("qId") or "").strip() or "untitled"
        title = (
            ((body.get("qProperty") or {}).get("qMetaDef") or {}).get("title")
            or ""
        ).strip()
        slug = _slugify(title or qid)
        fname = f"sheet--{slug}-{qid.lower()}.json"
        _write_json(out / "objects" / fname, body)


def _extract_child_property_trees(
    sheet: Dict[str, Any], contents: QvfContents,
) -> List[Dict[str, Any]]:
    """For a flat (Desktop-style) sheet, look up its cell children in
    ``contents.objects`` and produce the ``qChildren`` array the parser
    expects.

    Each cell's ``name`` is the qId of its child object. We resolve
    those against our object pool and synthesise the
    ``{qProperty, qChildren}`` tree.
    """
    cells = sheet.get("cells") or []
    object_by_id: Dict[str, Dict[str, Any]] = {}
    for o in contents.objects:
        oid = (
            (o.get("qInfo") or {}).get("qId")
            or ((o.get("qProperty") or {}).get("qInfo") or {}).get("qId")
        )
        if oid:
            object_by_id[oid] = o
    children: List[Dict[str, Any]] = []
    for cell in cells:
        name = cell.get("name") or ""
        if not name or name not in object_by_id:
            continue
        obj = object_by_id[name]
        if "qProperty" in obj:
            children.append({
                "qProperty": obj.get("qProperty") or {},
                "qChildren": obj.get("qChildren") or [],
            })
        else:
            children.append({"qProperty": obj, "qChildren": []})
    return children


def _write_master_objects(contents: QvfContents, out: Path) -> None:
    """Write top-level master visualizations to objects/masterobject-*.json."""
    for obj in contents.objects:
        qinfo = (obj.get("qInfo") or {})
        qtype = (qinfo.get("qType") or "").lower()
        if qtype != "masterobject":
            continue
        qid = qinfo.get("qId") or ""
        if not qid:
            continue
        title = (obj.get("qMetaDef") or {}).get("title") or qid
        slug = _slugify(title)
        fname = f"masterobject-{slug}-{qid.lower()}.json"
        _write_json(out / "objects" / fname, obj)


def _write_loadmodel(contents: QvfContents, out: Path) -> None:
    """Write objects/loadmodel---loadmodel.json.

    The direct parser doesn't always surface the engine's LoadModel
    object. Three sources, tried in order:

    1. A literal ``LoadModel``-typed object found during dispatch.
    2. A loadmodel-shaped dict scraped from raw bytes.
    3. Synthesise from the LOAD script tables and the field universe
       referenced across visuals -- minimal but enough for the
       downstream parser to build tables.
    """
    out_path = out / "objects" / "loadmodel---loadmodel.json"

    # 1. Look for a real LoadModel object.
    for obj in contents.objects:
        qinfo = obj.get("qInfo") or {}
        if (qinfo.get("qType") or "").lower() == "loadmodel":
            if obj.get("tables") or obj.get("qTables"):
                _write_json(out_path, obj)
                return

    # 2. Synthesise. Parse table-name -> [fields] pairs out of the
    #    LOAD script. Combined with field references from visuals,
    #    that gives us enough structure for the parser to build tables.
    synth = _synthesise_loadmodel(contents)
    _write_json(out_path, synth)


_LOAD_BLOCK_RE = re.compile(
    r"(?P<table>\w+)\s*:\s*LOAD\s+(?P<fields>.+?)\s+(?:FROM|RESIDENT|INLINE)\s+",
    re.I | re.S,
)
_BARE_LOAD_RE = re.compile(
    r"\bLOAD\s+(?P<fields>.+?)\s+(?:FROM|RESIDENT|INLINE)\s+",
    re.I | re.S,
)


def _synthesise_loadmodel(contents: QvfContents) -> Dict[str, Any]:
    """Build a minimal loadmodel from script LOAD blocks + visual fields.

    Shape emitted::

        {"tables": [
            {"id": "dsd.<Table>",
             "tableAlias": "<Table>",
             "tableName": "<Table>",
             "fields": [{"id": ..., "name": ..., "alias": ...}, ...]},
        ], "queries": [], "associations": {}}
    """
    tables: List[Dict[str, Any]] = []
    seen_tables: set = set()

    script = contents.load_script or ""
    if script:
        # Strip comments to reduce false matches.
        script_clean = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
        script_clean = re.sub(r"//[^\n]*", "", script_clean)

        for m in _LOAD_BLOCK_RE.finditer(script_clean):
            tname = m.group("table").strip()
            if tname.lower() in seen_tables:
                continue
            seen_tables.add(tname.lower())
            fields_text = m.group("fields")
            fields = _parse_load_fields(fields_text, tname)
            tables.append({
                "id":         f"dsd.{tname}",
                "tableAlias": tname,
                "tableName":  tname,
                "fields":     fields,
            })

    # If no tables came out of the script, fall back to inferring
    # tables from how fields are referenced inside visuals.
    #
    # Qlik fields are conventionally namespaced ``<Table>.<Column>`` --
    # e.g. ``HCP.City``, ``HCO.Name``, ``Referral Edge.Link_ID``. The
    # dot prefix tells us which table each field belongs to. Group by
    # prefix; fields with no namespace land in a default ``DataModel``
    # bucket. Each prefix becomes a separate Power BI table, which
    # gives the downstream relationship inference enough structure to
    # connect them via shared column names.
    if not tables:
        visual_fields = _collect_visual_fields(contents)
        by_table: Dict[str, List[str]] = {}
        for f in visual_fields:
            if "." in f:
                prefix, _, col = f.partition(".")
                prefix = prefix.strip()
                col = col.strip()
            else:
                prefix, col = "DataModel", f
            if not prefix or not col:
                continue
            by_table.setdefault(prefix, []).append(col)
        for tname, cols in by_table.items():
            # Dedupe columns within a table while preserving order.
            seen_cols: Dict[str, None] = {}
            for c in cols:
                seen_cols.setdefault(c, None)
            tables.append({
                "id":         f"dsd.{tname}",
                "tableAlias": tname,
                "tableName":  tname,
                "fields": [
                    {
                        "id":    f"dsd.{tname}.{c}",
                        "name":  c,
                        "alias": c,
                    }
                    for c in seen_cols
                ],
            })

    return {"tables": tables, "queries": [], "associations": {}}


_FIELD_PART_RE = re.compile(r",(?![^()\[\]]*[)\]])")


def _parse_load_fields(fields_text: str, table: str) -> List[Dict[str, str]]:
    """Split ``f1, f2 AS alias, [Field With Spaces], ...`` properly."""
    if fields_text.strip() == "*":
        return []
    out: List[Dict[str, str]] = []
    for raw in _FIELD_PART_RE.split(fields_text):
        f = raw.strip()
        if not f:
            continue
        as_m = re.match(r"^(.+?)\s+AS\s+(.+)$", f, re.I)
        if as_m:
            src = as_m.group(1).strip().strip("\"[]")
            alias = as_m.group(2).strip().strip("\"[]")
        else:
            src = f.strip().strip("\"[]")
            alias = src
        if not src:
            continue
        out.append({
            "id":    f"dsd.{table}.{src}",
            "name":  src,
            "alias": alias,
        })
    return out


def _collect_visual_fields(contents: QvfContents) -> List[str]:
    """Pull every bare field reference out of every visual hypercube."""
    seen: Dict[str, None] = {}
    for src in (contents.sheets, contents.objects):
        for item in src:
            _harvest_fields(item, seen)
    return list(seen.keys())


def _harvest_fields(node: Any, seen: Dict[str, None]) -> None:
    if isinstance(node, dict):
        if "qFieldDefs" in node and isinstance(node["qFieldDefs"], list):
            for fd in node["qFieldDefs"]:
                if isinstance(fd, str):
                    cleaned = fd.strip().strip("\"[]")
                    if cleaned and not cleaned.startswith("=") \
                            and len(cleaned) < 80:
                        seen.setdefault(cleaned, None)
        for v in node.values():
            _harvest_fields(v, seen)
    elif isinstance(node, list):
        for v in node:
            _harvest_fields(v, seen)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _slugify(title: str) -> str:
    s = _SLUG_RE.sub("-", (title or "").strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        s = "untitled"
    if len(s) > 60:
        s = s[:60].rstrip("-")
    return s


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text or "")
