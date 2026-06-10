"""Small helpers shared across qlik_to_pbi modules."""

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional


def hex_id(*parts: str) -> str:
    h = hashlib.md5("||".join(parts).encode("utf-8", errors="ignore")).hexdigest()
    return h[:20]


def lineage_tag(*parts: str) -> str:
    h = hashlib.md5("||".join(parts).encode("utf-8", errors="ignore")).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def new_logical_id() -> str:
    return str(uuid.uuid4())


def tmdl_quote(name: str) -> str:
    if not name:
        return "''"
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return name
    return "'" + name.replace("'", "''") + "'"


def safe_filename(name: str, max_len: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    cleaned = cleaned.strip() or "unnamed"
    if len(cleaned) <= max_len:
        return cleaned
    suffix = "_" + hashlib.md5(cleaned.encode("utf-8")).hexdigest()[:8]
    keep = max_len - len(suffix)
    return cleaned[:keep] + suffix


def _long_path(p: Any) -> str:
    # Absolutise WITHOUT touching the filesystem. ``os.path.abspath`` is
    # pure string normalisation (joins against cwd, collapses ``..``/``.``)
    # and, unlike ``Path.resolve()``, issues no ``_getfinalpathname`` /
    # ``realpath`` syscall per call. On a large app the writer emits
    # thousands of files; ``resolve()`` here was the single largest cost
    # in the whole pipeline. We only need an absolute path for the
    # ``\\?\`` long-path prefix, which Windows accepts on a
    # non-canonicalised absolute path (our output tree is never a
    # symlink, so symlink resolution is irrelevant).
    abs_str = os.path.abspath(os.fspath(p))
    if os.name == "nt" and not abs_str.startswith("\\\\?\\"):
        return "\\\\?\\" + abs_str
    return abs_str


# Directories we have already created this process. ``write_json`` /
# ``write_text`` call ``mkdir_p(path.parent)`` defensively on every
# write; the callers (writer.py, model.write_tmdl) already mkdir the
# target directory once before emitting a batch of files into it, so the
# parent almost always exists. Caching the absolute dir strings we've
# made turns ~2000 redundant ``os.makedirs`` syscalls per large-app run
# into in-memory set hits. ``exist_ok=True`` keeps correctness if the
# cache is cold or the dir was removed out from under us.
_MKDIR_CACHE: set[str] = set()


def clear_mkdir_cache() -> None:
    """Forget which directories we've created.

    Call this whenever the output tree may have been removed out from
    under the cache (e.g. ``writer.write`` ``shutil.rmtree``s a stale
    PBIP before re-emitting it into the same path). Skipping the
    ``os.makedirs`` after a real deletion would make the subsequent
    ``open(...,"w")`` fail with FileNotFoundError.
    """
    _MKDIR_CACHE.clear()


def mkdir_p(p: Path) -> None:
    key = _long_path(p)
    if key in _MKDIR_CACHE:
        return
    if os.name == "nt":
        os.makedirs(key, exist_ok=True)
    else:
        os.makedirs(key, exist_ok=True)
    _MKDIR_CACHE.add(key)


def write_json(path: Path, data: Any) -> None:
    target = _long_path(path)
    parent = os.path.dirname(target)
    if parent not in _MKDIR_CACHE:
        os.makedirs(parent, exist_ok=True)
        _MKDIR_CACHE.add(parent)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def write_text(path: Path, content: str) -> None:
    target = _long_path(path)
    parent = os.path.dirname(target)
    if parent not in _MKDIR_CACHE:
        os.makedirs(parent, exist_ok=True)
        _MKDIR_CACHE.add(parent)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)


def resolve_qlik_command(qlik_command: str = "qlik") -> Optional[str]:
    """Find an invocable qlik CLI binary.

    Resolution priority:

    1. If ``qlik_command`` itself is an existing file, use it as-is.
       Lets callers pass an absolute path like
       ``C:\\Users\\me\\qlik.exe``.
    2. ``shutil.which(qlik_command)`` — finds it on PATH. Honours
       ``PATHEXT`` on Windows so a bare ``"qlik"`` resolves to
       ``qlik.exe`` / ``qlik.bat``.
    3. Common install locations searched in this order on Windows:
       - ``~/qlik.exe`` (where the official installer drops it for a
         per-user install)
       - ``~/qlik-cli/qlik.exe``
       - ``%LOCALAPPDATA%/Programs/qlik-cli/qlik.exe``
       - ``%LOCALAPPDATA%/qlik-cli/qlik.exe``
       - ``%APPDATA%/qlik-cli/qlik.exe``
       - ``C:/Program Files/qlik-cli/qlik.exe``
       - ``C:/Program Files (x86)/qlik-cli/qlik.exe``

    Returns the absolute path string, or ``None`` if nothing usable
    was found. Callers should surface a clear error to the user
    in the ``None`` case.
    """
    import os

    if qlik_command:
        # Exact path the user gave.
        as_path = Path(qlik_command)
        if as_path.is_file():
            return str(as_path.resolve())

    # On PATH (with PATHEXT-aware lookup on Windows).
    on_path = shutil.which(qlik_command)
    if on_path:
        return on_path

    # Common install locations.
    home = Path.home()
    candidates: list[Path] = [
        home / "qlik.exe",
        home / "qlik-cli" / "qlik.exe",
    ]
    for env_var in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(Path(base) / "Programs" / "qlik-cli" / "qlik.exe")
            candidates.append(Path(base) / "qlik-cli" / "qlik.exe")
    candidates.append(Path("C:/Program Files/qlik-cli/qlik.exe"))
    candidates.append(Path("C:/Program Files (x86)/qlik-cli/qlik.exe"))

    # POSIX-style fallback (in case someone runs this on Mac/Linux via
    # WSL or a portable install).
    candidates.append(home / "bin" / "qlik")
    candidates.append(Path("/usr/local/bin/qlik"))

    for cand in candidates:
        if cand.is_file():
            return str(cand.resolve())

    return None


def clean_label(text: Any) -> str:
    """Strip Qlik label-expression syntax (e.g. `='PNA Readmit %'`) to plain text.

    Qlik can encode a "title" as either a plain string or as an
    expression object -- ``{"qStringExpression": {"qExpr": "..."}}`` or
    ``{"qExpr": "..."}``. We accept both shapes (and silently fall back
    to ``""`` for anything else) so downstream callers can keep using
    ``props.get("title", "")`` without type-checking the result.
    """
    if not text:
        return ""
    if isinstance(text, dict):
        # Common Qlik title-as-expression shapes.
        inner = text.get("qStringExpression")
        if isinstance(inner, dict):
            text = inner.get("qExpr") or ""
        elif isinstance(inner, str):
            text = inner
        else:
            text = text.get("qExpr") or text.get("title") or ""
    if not isinstance(text, str):
        return ""
    s = text.strip()
    if s.startswith("="):
        s = s[1:].strip()
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        s = s[1:-1]
    return s.strip()


def strip_brackets(name: str) -> str:
    s = (name or "").strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return s.strip()
