"""Snapshot test helpers.

Shared utilities for the golden-snapshot test harness. Owns the
normalization that strips run-to-run nondeterminism from converter
output before hashing — without this every snapshot would change on
every run because:

  * `.platform` files contain a fresh `uuid.uuid4()` per build.
  * The `RepoPath` M parameter is defaulted to the absolute path of
    the SemanticModel folder on the build machine.
  * Output folder names in visual paths are hex-of-content, which is
    already stable, but absolute output roots leak into a few error
    messages and log strings.

Normalization is the only thing standing between "useful regression
signal" and "test that always fails locally and never fails in CI."
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict


# UUID 8-4-4-4-12 pattern — matches `logicalId` in `.platform` files.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def normalize_content(text: str, output_dir: Path) -> str:
    """Strip machine-specific and run-specific noise from a generated file.

    Operations, in order:
      1. Replace any absolute path that starts with ``output_dir`` with
         the literal ``<OUTPUT>``. Catches the ``RepoPath`` default and
         any other absolute-path leakage.
      2. Replace UUIDs with ``<UUID>``. Catches ``.platform`` logicalId.
      3. Normalize line endings to ``\\n`` so Windows-vs-POSIX writes
         hash identically.

    Returns the normalized string. The caller hashes this, not the
    original file content.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    # Output dir replacement — both forward- and back-slash variants so
    # we catch both M-string paths (forward slashes by convention) and
    # any Python ``str(Path)`` leakage (backslashes on Windows).
    out_str = str(output_dir.resolve())
    for variant in (out_str, out_str.replace("\\", "/"), out_str.replace("/", "\\")):
        if variant:
            normalized = normalized.replace(variant, "<OUTPUT>")

    normalized = _UUID_RE.sub("<UUID>", normalized)
    return normalized


def hash_file(path: Path, output_dir: Path) -> str:
    """SHA256 of the normalized file content. Returns hex digest."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    normalized = normalize_content(raw, output_dir)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def collect_semantic_model_hashes(
    output_dir: Path, name: str,
) -> Dict[str, str]:
    """Hash every file under ``<name>.SemanticModel/definition/``.

    Keys are paths relative to the SemanticModel definition folder so
    snapshots stay portable across output locations.
    """
    sm_def = output_dir / f"{name}.SemanticModel" / "definition"
    if not sm_def.exists():
        return {}
    out: Dict[str, str] = {}
    for p in sorted(sm_def.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(sm_def).as_posix()
        out[rel] = hash_file(p, output_dir)
    return out


def collect_report_hashes(
    output_dir: Path, name: str,
) -> Dict[str, str]:
    """Hash every JSON file under ``<name>.Report/definition/``.

    Includes report.json, version.json, pages.json, every page.json,
    and every visual.json. Visual folder names are content-hashed
    (``hex_id``) so they're stable across runs.
    """
    rpt_def = output_dir / f"{name}.Report" / "definition"
    if not rpt_def.exists():
        return {}
    out: Dict[str, str] = {}
    for p in sorted(rpt_def.rglob("*.json")):
        rel = p.relative_to(rpt_def).as_posix()
        out[rel] = hash_file(p, output_dir)
    return out


def build_summary(output_dir: Path, name: str) -> Dict[str, Any]:
    """High-level counts that catch coarse regressions even when the
    detailed hashes happen to collide (or are intentionally updated).

    Captures table / column / measure / relationship counts from the
    semantic-model definition folder, and page / visual counts plus
    the visualType distribution from the report definition.
    """
    summary: Dict[str, Any] = {
        "tables": 0,
        "columns": 0,
        "measures": 0,
        "relationships": 0,
        "pages": 0,
        "visuals": 0,
        "visual_types": {},
    }

    # Tables / columns / measures from TMDL — count by parsing the
    # `column ` / `measure ` / `table ` keyword occurrences in tables
    # tmdl files. Cheap and stable enough for a sanity-check summary.
    tables_dir = output_dir / f"{name}.SemanticModel" / "definition" / "tables"
    if tables_dir.exists():
        for p in sorted(tables_dir.glob("*.tmdl")):
            text = p.read_text(encoding="utf-8", errors="replace")
            summary["tables"] += 1
            # Match lines starting with `column ` or `\tcolumn ` —
            # tmdl indents members under the table block.
            summary["columns"] += len(
                re.findall(r"(?m)^[ \t]*column[ \t]+", text)
            )
            summary["measures"] += len(
                re.findall(r"(?m)^[ \t]*measure[ \t]+", text)
            )

    rels_file = (
        output_dir / f"{name}.SemanticModel" / "definition" / "relationships.tmdl"
    )
    if rels_file.exists():
        text = rels_file.read_text(encoding="utf-8", errors="replace")
        summary["relationships"] = len(
            re.findall(r"(?m)^relationship[ \t]+", text)
        )

    # Pages + visuals from report definition
    pages_dir = output_dir / f"{name}.Report" / "definition" / "pages"
    if pages_dir.exists():
        for page_dir in sorted(pages_dir.iterdir()):
            if not page_dir.is_dir():
                continue
            summary["pages"] += 1
            visuals_dir = page_dir / "visuals"
            if not visuals_dir.exists():
                continue
            for vdir in sorted(visuals_dir.iterdir()):
                vjson = vdir / "visual.json"
                if not vjson.exists():
                    continue
                summary["visuals"] += 1
                try:
                    data = json.loads(vjson.read_text(encoding="utf-8"))
                    vt = data.get("visual", {}).get("visualType", "<unknown>")
                except Exception:
                    vt = "<parse-error>"
                summary["visual_types"][vt] = (
                    summary["visual_types"].get(vt, 0) + 1
                )

    return summary
