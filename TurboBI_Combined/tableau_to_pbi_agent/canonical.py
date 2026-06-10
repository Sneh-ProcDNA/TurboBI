"""Tiny helper module — strip a Tableau (Object!Suffix) disambiguator.

Imported by context_builder so we don't pull in the whole
tableau_to_pbi.model module just for one regex.
"""

from __future__ import annotations

import re


_OBJ_SUFFIX_RE = re.compile(r"\s*\([^()]+!.+?\)\s*$")


def canonical_name(name: str) -> str:
    return _OBJ_SUFFIX_RE.sub("", name or "").strip()
