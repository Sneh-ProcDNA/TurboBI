"""Lightweight tag-based logging, mirroring tableau_to_pbi/_logging.py."""

import logging
import os
import sys
from typing import Dict

_LOGGER_PREFIX = "qlik_to_pbi"
_configured = False
_loggers: Dict[str, logging.Logger] = {}


class _TagFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        tag = record.name.split(".")[-1].upper()
        return f"[{tag}] {record.getMessage()}"


def get_logger(tag: str) -> logging.Logger:
    key = tag.lower()
    if key not in _loggers:
        _loggers[key] = logging.getLogger(f"{_LOGGER_PREFIX}.{key}")
    return _loggers[key]


def configure_default() -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger(_LOGGER_PREFIX)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_TagFormatter())
    root.addHandler(handler)
    level = os.environ.get("QLIK_LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False
    _configured = True
