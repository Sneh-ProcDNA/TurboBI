"""Tiny helpers shared across stages."""

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any


def hex_id(*parts: str) -> str:
    """Stable 20-char hex id; matches the format Power BI uses for visual
    and page folder names. Same input always yields the same id, which
    lets us rerun the converter without reshuffling the output on disk."""
    h = hashlib.md5("||".join(parts).encode("utf-8", errors="ignore")).hexdigest()
    return h[:20]


def lineage_tag(*parts: str) -> str:
    """Stable UUID-style lineage tag; deterministic for repeatability."""
    h = hashlib.md5("||".join(parts).encode("utf-8", errors="ignore")).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def new_logical_id() -> str:
    return str(uuid.uuid4())


def safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(float(str(val)))
    except (ValueError, TypeError):
        return default


def clean_bracket(s: str) -> str:
    return re.sub(r"^\[|\]$", "", (s or "").strip())


def tmdl_quote(name: str) -> str:
    """TMDL identifier quoting. Names containing anything other than
    alphanumerics or underscore must be wrapped in single quotes; embedded
    single quotes are doubled."""
    if not name:
        return "''"
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return name
    return "'" + name.replace("'", "''") + "'"


def safe_filename(name: str, max_len: int = 80) -> str:
    """Strip characters that are invalid in Windows file names; preserve
    spaces and other readable bits where possible. When the result is
    longer than ``max_len`` characters (default 80), truncate it and
    append a short hash so the full original name still distinguishes
    siblings — Tableau hyper extract names like ``Extract.MATILLION_X
    (COMM_DEV.MATILLION_X)_<32-char-GUID>.csv`` blow past Windows'
    260-char MAX_PATH when nested in deep PBIP folders.
    """
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    if len(cleaned) <= max_len:
        return cleaned
    import hashlib
    suffix = "_" + hashlib.md5(cleaned.encode("utf-8")).hexdigest()[:8]
    keep = max_len - len(suffix)
    return cleaned[:keep] + suffix


def _long_path(p: Any) -> str:
    """Return a path string with the Windows `\\\\?\\` long-path prefix when
    running on Windows. The prefix bypasses MAX_PATH (260 chars) by
    opting the call into the Win32 wide-char "extended-length" API.
    No-op on POSIX. Used by the writer when emitting PBIP folders whose
    workbook name + page-id + visual-id chain crosses the legacy limit.
    """
    import os
    abs_str = str(Path(p).resolve())
    if os.name == "nt" and not abs_str.startswith("\\\\?\\"):
        return "\\\\?\\" + abs_str
    return abs_str


def write_json(path: Path, data: Any) -> None:
    import os
    parent = path.parent
    if os.name == "nt":
        os.makedirs(_long_path(parent), exist_ok=True)
    else:
        parent.mkdir(parents=True, exist_ok=True)
    open_path = _long_path(path) if os.name == "nt" else path
    with open(open_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def escape_m_string(s: str) -> str:
    """Escape a Python string for inclusion inside an M-language literal.

    Power Query M strings:
      "  -> ""
      newline -> #(lf)
      tab     -> #(tab)

    Used by partition-M emit when the source carries a custom SQL fragment
    (Tableau `<relation type='text'>` / `type='query'`) that must be embedded
    inside an `Sql.Database(..., [Query="..."])` or `Value.NativeQuery(...)`
    expression.
    """
    if s is None:
        return ""
    out = str(s)
    out = out.replace('"', '""')
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = out.replace("\n", "#(lf)")
    out = out.replace("\t", "#(tab)")
    return out
