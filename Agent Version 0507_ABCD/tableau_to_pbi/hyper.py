"""Hyper file processing: real data + metadata extraction.

Uses pantab (DataFrame bridge) and tableauhyperapi (catalog metadata) to pull
actual table data and column types from a .hyper extract.  The results feed:

    - SemanticModel  -- accurate TMDL column types, replacing the coarser
                        types inferred from the TWB XML.
    - write_tmdl()   -- CSV-backed M partitions so Power BI Desktop loads real
                        data when the project is refreshed.

Everything is gracefully disabled when pantab / tableauhyperapi are absent.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import pandas as pd
    import pantab as pt
    from tableauhyperapi import HyperProcess, Telemetry, Connection
    _HYPER_OK = True
except ImportError:
    _HYPER_OK = False


# ---------------------------------------------------------------------------
# Type mapping: Hyper catalog type string -> TMDL dataType
# ---------------------------------------------------------------------------

def hyper_type_to_tmdl(type_str: str) -> str:
    s = type_str.upper().strip()
    if "INT" in s:
        return "int64"
    if any(x in s for x in ("DOUBLE", "FLOAT", "REAL", "NUMERIC", "DECIMAL")):
        return "double"
    if "BOOL" in s:
        return "boolean"
    if any(x in s for x in ("TIMESTAMP", "DATE")):
        return "dateTime"
    return "string"


# ---------------------------------------------------------------------------
# HyperAPI name helper
# ---------------------------------------------------------------------------

def _name_str(n: Any) -> str:
    """Return the plain (unescaped) string from a HyperAPI Name/SchemaName."""
    s = str(n)
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1].replace('""', '"')
    return s


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

class HyperData:
    """All extracted data from one .hyper file."""

    def __init__(self) -> None:
        # "Schema.Table" -> pandas DataFrame
        self.frames:   Dict[str, Any]        = {}
        # "Schema.Table" -> [{"name", "type", "tmdlType"}, ...]
        self.metadata: Dict[str, List[Dict]] = {}

    @property
    def available(self) -> bool:
        return bool(self.frames)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _parse_tname(tname: Any) -> Tuple[str, str]:
    """Extract (schema_str, table_str) from a pantab frame key.

    pantab returns either tableauhyperapi.TableName objects or raw tuples
    depending on the installed version.  Both cases are handled here.
    """
    if isinstance(tname, tuple) and len(tname) >= 2:
        return str(tname[0]), str(tname[1])
    if hasattr(tname, "schema_name") and hasattr(tname, "name"):
        schema = (_name_str(tname.schema_name)
                  if tname.schema_name is not None else "Extract")
        return schema, _name_str(tname.name)
    return "Extract", str(tname)


def load_hyper(hyper_path: Path) -> HyperData:
    """Read data frames and catalog metadata from a .hyper file.

    The catalog is loaded first to establish canonical keys; pantab frames
    are then matched to those same keys so that hd.frames and hd.metadata
    always share identical dict keys.

    Returns an empty HyperData if the libraries are missing or the file
    cannot be read -- callers must check .available before using the result.
    """
    hd = HyperData()

    if not _HYPER_OK:
        print("[HYPER] pantab / tableauhyperapi not installed — hyper data skipped.")
        return hd

    # ── Catalog metadata (establishes canonical keys) ────────────────────────
    # canonical_index: (schema_str, table_str) -> canonical_key
    canonical_index: Dict[Tuple[str, str], str] = {}
    try:
        with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyp:
            with Connection(endpoint=hyp.endpoint, database=hyper_path) as conn:
                for schema in conn.catalog.get_schema_names():
                    schema_str = _name_str(schema)
                    for tbl in conn.catalog.get_table_names(schema):
                        table_str = _name_str(tbl.name)
                        key       = f"{schema_str}.{table_str}"
                        canonical_index[(schema_str, table_str)] = key
                        tbl_def   = conn.catalog.get_table_definition(tbl)
                        cols: List[Dict] = []
                        for col in tbl_def.columns:
                            col_name = _name_str(col.name)
                            type_str = str(col.type)
                            cols.append({
                                "name":     col_name,
                                "type":     type_str,
                                "tmdlType": hyper_type_to_tmdl(type_str),
                            })
                        hd.metadata[key] = cols
    except Exception as exc:
        print(f"[HYPER] Could not read catalog from {hyper_path.name}: {exc}")
        return hd

    # ── DataFrame frames via pantab ──────────────────────────────────────────
    try:
        raw = pt.frames_from_hyper(hyper_path)
        for tname, df in raw.items():
            schema_str, table_str = _parse_tname(tname)
            # Use the canonical key so frames and metadata share the same keys.
            key = canonical_index.get(
                (schema_str, table_str),
                f"{schema_str}.{table_str}",
            )
            hd.frames[key] = df
        print(f"[HYPER] Loaded {len(hd.frames)} table(s) from {hyper_path.name}")
    except Exception as exc:
        print(f"[HYPER] Could not load frames from {hyper_path.name}: {exc}")

    return hd


# ---------------------------------------------------------------------------
# Datasource <-> hyper file registry
# ---------------------------------------------------------------------------

class HyperRegistry:
    """Pairs each datasource with the .hyper file it actually owns.

    Tableau records the authoritative datasource->hyper binding inside
    the .twb XML — every <datasource> wraps a <connection class='hyper'
    dbname='Data/TableauTemp/TEMP_xxx.hyper'> entry that names the hyper
    file produced for that datasource. The parser captures those dbnames
    in `ds["extracts"]`. This registry resolves them to the absolute
    paths produced by extracting the .twbx archive and lazily loads each
    hyper file on demand.

    Centralising the resolution here gives us a single place to grow
    (multi-extract datasources, alternative path layouts, deferred
    loading) without spreading hyper concerns across `converter.py` and
    `model.py`.
    """

    def __init__(self, hyper_paths: List[str]) -> None:
        self._paths_in:  List[str]      = list(hyper_paths or [])
        self._loaded:    Dict[str, HyperData] = {}
        # Index extracted files by (a) basename and (b) every
        # parent-relative path. The parser preserves the dbname exactly
        # as Tableau wrote it (relative to the .twbx root), and
        # extract_twbx() preserves the same relative layout under the
        # work directory, so either lookup form will hit.
        self._by_basename: Dict[str, str] = {}
        self._by_relpath:  Dict[str, str] = {}
        for hp in self._paths_in:
            p = Path(hp)
            self._by_basename[p.name.lower()] = hp
            for parent in p.parents:
                rel = p.relative_to(parent).as_posix().lower()
                self._by_relpath[rel] = hp

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _resolve_path(self, dbname: str) -> Optional[str]:
        """Return the absolute hyper-file path for a Tableau dbname,
        or None when nothing in the archive matches.
        """
        rel = (dbname or "").replace("\\", "/").lower()
        if not rel:
            return None
        return self._by_relpath.get(rel) or self._by_basename.get(Path(rel).name)

    def _load(self, abs_path: str) -> Optional[HyperData]:
        """Load and cache a hyper file. Returns None when unavailable."""
        cached = self._loaded.get(abs_path)
        if cached is not None:
            return cached if cached.available else None
        hd = load_hyper(Path(abs_path))
        self._loaded[abs_path] = hd
        return hd if hd.available else None

    def bind(
        self, datasources: List[Dict[str, Any]],
    ) -> Dict[str, HyperData]:
        """Return {ds_name -> HyperData} for the given datasources.

        Datasources whose `extracts` reference a hyper file we couldn't
        locate or load are simply absent from the result. As a fallback
        for legacy workbooks that omit the <extract> binding entirely,
        the FIRST extracted hyper file is bound to the FIRST datasource
        — preserves data while still avoiding cross-datasource leaks.

        Side effect: `self.bound_paths_by_ds` is populated with the
        absolute hyper-file path each datasource ended up bound to,
        which the converter uses to write a human-readable mapping
        report.
        """
        out: Dict[str, HyperData] = {}
        self.bound_paths_by_ds: Dict[str, str] = {}
        for ds in datasources:
            for dbname in ds.get("extracts", []) or []:
                abs_path = self._resolve_path(dbname)
                if abs_path is None:
                    print(f"[HYPER] Datasource '{ds['name']}' references "
                          f"'{dbname}' but no matching hyper file was "
                          f"extracted; skipping.")
                    continue
                hd = self._load(abs_path)
                if hd is None:
                    continue
                out[ds["name"]] = hd
                self.bound_paths_by_ds[ds["name"]] = abs_path
                print(f"[HYPER] Datasource '{ds['name']}' (caption "
                      f"'{ds.get('caption', '')}') -> {Path(abs_path).name}")
                # First successful extract wins for this datasource.
                break

        if not out and self._paths_in and datasources:
            hp = self._paths_in[0]
            hd = self._load(hp)
            if hd is not None:
                out[datasources[0]["name"]] = hd
                self.bound_paths_by_ds[datasources[0]["name"]] = hp
                print(f"[HYPER] No <extract> mapping found; falling back "
                      f"to '{Path(hp).name}' -> datasource "
                      f"'{datasources[0]['name']}'.")
        return out


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def write_csv(hd: HyperData, data_dir: Path) -> Dict[str, Path]:
    """Write each DataFrame to a UTF-8-BOM CSV under data_dir.

    Returns {hyper_key -> absolute Path}.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, Path] = {}

    from .utils import safe_filename

    for key, df in hd.frames.items():
        # safe_filename also truncates names that would push the full path
        # past Windows' 260-char MAX_PATH. Tableau extract keys like
        # 'Extract.MATILLION_X (COMM_DEV.MATILLION_X)_<32-char-GUID>' are
        # ~120 chars on their own — without truncation, the deeply-nested
        # PBIP path (.../<ds>.SemanticModel/data/<federated.GUID>/<key>.csv)
        # blows past the limit and the converter aborts mid-write.
        safe_key = safe_filename(key)
        csv_path = data_dir / f"{safe_key}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        paths[key] = csv_path
        print(f"[HYPER] {len(df):,} rows  ->  {csv_path.name}")

    return paths


# ---------------------------------------------------------------------------
# Table-matching helper
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """Strip GUID suffixes and normalize spaces/special chars for fuzzy matching."""
    # Remove trailing GUIDs like _33E141CEBC4648AB952C00FDC179E092
    s = re.sub(r"_[0-9A-Fa-f]{32}$", "", name)
    # Replace ! and _ with space (Tableau uses both in hyper names)
    s = s.replace("!", " ").replace("_", " ")
    # Normalize multiple spaces to one
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower()


def match_hyper_to_tmdl(
    hyper_key:   str,
    hyper_cols:  List[str],
    tmdl_tables: List[Dict[str, Any]],
) -> Optional[str]:
    """Return the name of the best-matching TMDL table for a hyper table.

    Scoring (higher = better match):
        * +10000 if the TMDL table name / caption EXACTLY matches the hyper
                 table name (after normalization). Dominates everything else
                 so identically-named tables always win.
        * +50    if the hyper name is a substring of the TMDL name (or vice
                 versa) but not an exact match.
        * +100   if all hyper columns are present in the TMDL table.
        * +10    per overlapping column (case- and underscore-insensitive).
        * -1     per "extra" TMDL column beyond the hyper column count, so
                 that a tightly-fitting table beats a merged superset table
                 when both contain the hyper columns.

    Returns None when no table scores above zero.
    """
    if not tmdl_tables:
        return None

    # Normalize hyper column names for fuzzy matching (spaces vs underscores)
    hyper_set       = {c.lower() for c in hyper_cols}
    hyper_set_norm  = {c.lower().replace("_", " ").replace("-", " ") for c in hyper_cols}
    hyper_tname_raw = hyper_key.split(".")[-1]
    hyper_tname_low = _normalize_name(hyper_tname_raw)

    best_name:  Optional[str] = None
    best_score: int           = -10**9

    for tbl in tmdl_tables:
        tbl_src_cols = {c["sourceCol"].lower() for c in tbl.get("columns", [])}
        tbl_src_norm = {c.lower().replace("_", " ").replace("-", " ") for c in tbl_src_cols}
        # Count overlap using both exact and normalized names
        overlap_exact = len(hyper_set & tbl_src_cols)
        overlap_norm  = len(hyper_set_norm & tbl_src_norm)
        overlap       = max(overlap_exact, overlap_norm)
        # Try both raw and normalized matching for table names
        tbl_name_low  = _normalize_name(tbl["name"])
        tbl_cap_low   = _normalize_name(tbl.get("caption", "") or "")

        exact_name = (
            (tbl_name_low and tbl_name_low == hyper_tname_low)
            or (tbl_cap_low and tbl_cap_low == hyper_tname_low)
        )
        substring_name = (not exact_name) and bool(hyper_tname_low) and (
            (tbl_name_low and (
                hyper_tname_low in tbl_name_low or tbl_name_low in hyper_tname_low
            ))
            or (tbl_cap_low and (
                hyper_tname_low in tbl_cap_low or tbl_cap_low in hyper_tname_low
            ))
        )

        # All hyper cols present in TMDL table?
        contains_all = (hyper_set <= tbl_src_cols) or (hyper_set_norm <= tbl_src_norm)
        # "Extra" TMDL cols vs hyper count — prefers tighter fit on ties.
        extra_cols = max(0, len(tbl_src_cols) - len(hyper_set))

        score = overlap * 10
        if exact_name:
            score += 10000
        elif substring_name:
            score += 50
        if contains_all:
            score += 100
        score -= extra_cols

        if score > best_score:
            best_score = score
            best_name  = tbl["name"]

    # Debug: print matching attempt details
    print(f"[HYPER-MATCH] '{hyper_key}' (norm='{hyper_tname_low}') -> "
          f"best='{best_name}' score={best_score} cols={len(hyper_set)}")

    # Accept if we got any positive signal. With overlap=0 and no name match,
    # the score will be <= 0 (only -extra_cols penalties), and we treat that
    # as "no match" so a hyper table with no useful target is skipped.
    if best_score <= 0:
        return None
    return best_name
