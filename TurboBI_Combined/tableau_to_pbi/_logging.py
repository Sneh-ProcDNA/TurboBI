"""Structured logging for the converter.

Each ``[TAG]`` previously emitted via ``print()`` now goes through a
Python logger named ``tableau_to_pbi.<tag>``. Configure once at the CLI
entry point via ``configure_default()``; the default handler writes
to stderr with the format ``[<TAG>] <message>``. Existing CLI output
is preserved — just on stderr instead of stdout, so stdout stays clean
for the user-facing progress lines that the converter still emits via
``print()`` (``Tableau -> PBIP | name``, ``[1/4] Parsing...``, etc.).

To silence one category from a calling app:

    logging.getLogger("tableau_to_pbi.hyper").setLevel(logging.WARNING)

To silence everything but errors:

    logging.getLogger("tableau_to_pbi").setLevel(logging.WARNING)

To force a debug-level dump:

    TURBOBI_LOG_LEVEL=DEBUG python -m tableau_to_pbi_agent <workbook>

(The CLI entry point reads ``TURBOBI_LOG_LEVEL`` from the environment.)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional, TextIO


_CONFIGURED = False


def get_logger(category: str) -> logging.Logger:
    """Return a logger named ``tableau_to_pbi.<category>``.

    ``category`` is normalised to a hyphen-safe lowercase form
    (e.g. ``"FIELD-KIND"`` -> ``tableau_to_pbi.field-kind``). The
    default formatter uppercases it back to the Tableau-style tag
    on emission so existing log scrapers keep working.
    """
    safe = category.lower().replace(" ", "_")
    return logging.getLogger(f"tableau_to_pbi.{safe}")


class _TagFormatter(logging.Formatter):
    """Render records as ``[<CATEGORY>] <message>``.

    CATEGORY is the last segment of the logger's name, uppercased
    back to its Tableau-style form. This preserves the prior
    ``print(f"[HYPER] ...")`` look exactly, so any log scrapers
    grepping for ``[HYPER]`` keep matching.
    """

    def format(self, record: logging.LogRecord) -> str:
        tag = record.name.split(".")[-1].upper()
        return f"[{tag}] {record.getMessage()}"


def configure_default(
    stream: Optional[TextIO] = None,
    level: Optional[int] = None,
) -> None:
    """Install the default handler on the ``tableau_to_pbi`` logger.

    Idempotent — safe to call from multiple CLI entry points. Reads
    ``TURBOBI_LOG_LEVEL`` env var (DEBUG / INFO / WARNING / ERROR /
    CRITICAL) when ``level`` is not supplied, defaulting to INFO.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    if stream is None:
        stream = sys.stderr
    if level is None:
        env = (os.environ.get("TURBOBI_LOG_LEVEL") or "INFO").upper()
        level = getattr(logging, env, logging.INFO)
    root = logging.getLogger("tableau_to_pbi")
    root.setLevel(level)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_TagFormatter())
    root.addHandler(handler)
    # Don't propagate to Python's root logger — keeps the converter's
    # output isolated from any host application's logging setup.
    root.propagate = False
    _CONFIGURED = True


def reset_for_tests() -> None:
    """Drop the default handler so tests can configure their own.
    Pytest tests don't typically need to log; this lets the test
    runner stay quiet by default.
    """
    global _CONFIGURED
    root = logging.getLogger("tableau_to_pbi")
    for h in list(root.handlers):
        root.removeHandler(h)
    _CONFIGURED = False
