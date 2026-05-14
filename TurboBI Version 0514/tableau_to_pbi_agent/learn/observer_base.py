"""Observer protocol — the contract every learn-rule plugin satisfies.

An Observer is a small, focused unit that:

  1. Walks the corpus (worksheets parsed by TWBParser, plus a recent
     snapshot of converter output) and identifies cases where the
     converter's current output is wrong for a specific reason.
  2. Returns BOTH a candidate change to a *data* file (visual_rules.json,
     a synonym table, etc.) AND the list of visuals it expects to flip
     once that change lands. The runner uses the expectation list to
     classify regression diffs as intentional vs accidental.

We deliberately scope observers to data-level edits (JSON / lookup
table modifications). Code-level changes go through a different path
(proposals/, not autonomous).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple


@dataclass
class ChangeProposal:
    """One observer's output for one corpus pass."""
    name: str
    description: str
    # Files the runner should back up before applying the change.
    files_to_modify: List[Path] = field(default_factory=list)
    # Callable that performs the edit. The runner calls it AFTER the
    # backup. Must be idempotent — re-running it on already-edited
    # files produces no change.
    apply: Callable[[], None] = field(default=lambda: None)
    # Visuals the observer expects to flip after `apply()` runs. Each
    # entry: {workbook, visual_path, from, to}.
    expected_diffs: List[Dict[str, Any]] = field(default_factory=list)
    # Free-form summary line for the run log.
    summary: str = ""


class Observer:
    """Subclass per pattern. Keep them small and testable."""

    name: str = "<unnamed>"
    description: str = ""

    def observe(self, corpus_facts: List[Dict[str, Any]]) -> List[ChangeProposal]:
        """Inspect the corpus_facts (one entry per workbook) and return
        zero or more proposals. Returning [] means 'nothing to do'."""
        raise NotImplementedError
