"""Tableau (.twb / .twbx) -> Power BI Project (.pbip) converter.

The package splits the conversion into four narrow stages:

    parser   — read the .twb XML and produce simple dicts
    model    — build a TMDL semantic model from those dicts
    report   — build per-page visual definitions
    writer   — drop the whole thing onto disk in PBIP layout

Each stage knows nothing about the others' internals; they communicate
through plain dicts/lists that are easy to inspect during debugging.
"""

from .converter import Converter, run

__all__ = ["Converter", "run"]
