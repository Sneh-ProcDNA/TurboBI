"""Read tenant + bearer key from the qlik CLI's contexts.yml.

The qlik CLI stores connection contexts at ``~/.qlik/contexts.yml``:

::

    current-context: <name>
    contexts:
        <name>:
            headers:
                Authorization: Bearer <api-key>
            server: https://<tenant-host>
            server-type: cloud

This module is a thin file reader -- no ``qlik`` binary required, no
shelling out. It lets the converter pick up credentials the user has
already configured (``qlik context create ...``) without re-pasting
a long bearer token on the command line.

Privacy: the API key is never logged. We surface only the context
name and the tenant host. The contexts.yml file is treated as a
credential file -- read once, kept in memory, never copied to logs
or temp files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - import-time guard
    yaml = None  # type: ignore

from ._logging import get_logger

_log = get_logger("QLIK-CTX")


@dataclass
class QlikContext:
    """A loaded qlik CLI context.

    Fields:
        name:    The context's key in contexts.yml (e.g. "my-qlik-cloud").
        tenant:  The ``server`` URL (https://...qlikcloud.com).
        api_key: The bearer token (with the "Bearer " prefix stripped).
    """

    name: str
    tenant: str
    api_key: str


def _default_contexts_path() -> Path:
    """Standard location for the qlik CLI's contexts.yml."""
    home = (
        os.environ.get("USERPROFILE")
        or os.environ.get("HOME")
        or str(Path.home())
    )
    return Path(home) / ".qlik" / "contexts.yml"


def load_qlik_context(
    name: Optional[str] = None,
    contexts_path: Optional[Path] = None,
) -> Optional[QlikContext]:
    """Load a qlik CLI context by name (or the active context).

    Parameters
    ----------
    name
        Specific context name to load. When ``None``, falls back to
        the file's ``current-context`` field.
    contexts_path
        Override the contexts.yml path (test hook). When ``None``,
        uses ``~/.qlik/contexts.yml``.

    Returns
    -------
    QlikContext or None
        ``None`` when the file is missing, malformed, or the named
        context lacks a server / bearer token. The function never
        raises -- the CLI is expected to fall back to explicit flags
        on a None return.
    """
    if yaml is None:
        _log.warning(
            "PyYAML not installed -- cannot read qlik CLI context. "
            "Run `pip install PyYAML`."
        )
        return None

    path = contexts_path or _default_contexts_path()
    if not path.is_file():
        _log.info(f"No qlik CLI context file at {path}.")
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        _log.warning(f"Could not parse {path}: {exc}")
        return None

    contexts = data.get("contexts") or {}
    target = name or data.get("current-context") or ""
    if not target or target not in contexts:
        available = ", ".join(contexts.keys()) or "(none)"
        _log.warning(
            f"Qlik context '{target}' not found. Available: {available}"
        )
        return None

    ctx = contexts[target] or {}
    server = (ctx.get("server") or "").strip()
    auth_header = ((ctx.get("headers") or {}).get("Authorization") or "").strip()
    api_key = ""
    if auth_header.lower().startswith("bearer "):
        api_key = auth_header[len("bearer "):].strip()

    if not server or not api_key:
        _log.warning(
            f"Qlik context '{target}' is missing server or bearer token."
        )
        return None

    return QlikContext(name=target, tenant=server, api_key=api_key)
