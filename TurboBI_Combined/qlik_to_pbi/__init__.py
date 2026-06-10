"""Qlik (QVF) -> Power BI Project (PBIP) converter.

Pipeline mirrors the sibling `tableau_to_pbi` package:

    qlik app unbuild   -> JSON output directory (done outside this package by
                          the `qlik` CLI)
    parser             -> reads that directory into IR dicts
    dax_translator     -> Qlik expression -> DAX (best-effort)
    model              -> builds the TMDL semantic model
    report             -> turns sheet/cells into PBI pages/visuals
    writer             -> drops the PBIP folder layout on disk
    converter          -> orchestrator
"""

from .converter import Converter

__all__ = ["Converter"]
