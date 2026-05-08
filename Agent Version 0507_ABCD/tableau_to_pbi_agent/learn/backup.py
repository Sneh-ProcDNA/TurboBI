"""Snapshot/restore for files the learner is about to modify.

The autonomous-merge model only works if rollback is mechanical:
before any edit, copy the file to a timestamped backup directory.
On regression failure, copy it back. No git, no patches — just file
copies, so the safety net works regardless of whether the user has
a clean working tree.

Layout on disk:

    tableau_to_pbi_agent/.backups/
        2026-05-01T13-45-22/
            tableau_to_pbi/visual_rules.json
            tableau_to_pbi/visual_picker.py
            ...
        2026-05-01T14-12-08/
            tableau_to_pbi/visual_rules.json

Each timestamped folder is a complete snapshot of the files the
learner touched in that run. Restore copies them back to their
original paths.
"""

from __future__ import annotations

import datetime as _dt
import shutil
from pathlib import Path
from typing import List, Optional


_BACKUPS_ROOT = (
    Path(__file__).resolve().parent.parent / ".backups"
)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


class BackupSession:
    """Holds a single timestamp under which all files modified in one
    learner pass are snapshotted. Several files can be added; restore
    rolls back the whole set."""

    def __init__(self, stamp: Optional[str] = None) -> None:
        self.stamp = stamp or _now_stamp()
        self.dir = _BACKUPS_ROOT / self.stamp
        self._captured: List[Path] = []

    # ------------------------------------------------------------------
    # capture
    # ------------------------------------------------------------------

    def capture(self, source: Path) -> None:
        """Copy `source` into the backup folder, preserving its repo-
        relative path. No-op if the source doesn't exist (the learner
        may be staging a brand-new file)."""
        source = Path(source).resolve()
        if not source.exists():
            return
        try:
            rel = source.relative_to(_REPO_ROOT)
        except ValueError:
            # File outside the repo — store under absolute-path mirror.
            rel = Path("_external") / source.name
        dest = self.dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        self._captured.append(source)

    # ------------------------------------------------------------------
    # restore
    # ------------------------------------------------------------------

    def restore(self) -> List[Path]:
        """Copy every captured file back to its original location.
        Returns the list of restored paths."""
        restored: List[Path] = []
        if not self.dir.exists():
            return restored
        for cur_root, _, files in self._walk(self.dir):
            for f in files:
                stored = Path(cur_root) / f
                rel = stored.relative_to(self.dir)
                if rel.parts and rel.parts[0] == "_external":
                    target = Path("/") / Path(*rel.parts[1:])
                else:
                    target = _REPO_ROOT / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stored, target)
                restored.append(target)
        return restored

    @staticmethod
    def _walk(root: Path):
        import os
        return os.walk(root)


# ---------------------------------------------------------------------------
# Convenience: list / locate previous sessions
# ---------------------------------------------------------------------------

def list_sessions() -> List[str]:
    """Return all timestamp folders under .backups, oldest -> newest."""
    if not _BACKUPS_ROOT.exists():
        return []
    return sorted(p.name for p in _BACKUPS_ROOT.iterdir() if p.is_dir())


def session_for(stamp: str) -> BackupSession:
    """Return a BackupSession bound to an existing stamp. Used by the
    rollback CLI to revive a past snapshot for restoration."""
    sess = BackupSession.__new__(BackupSession)
    sess.stamp = stamp
    sess.dir = _BACKUPS_ROOT / stamp
    sess._captured = []
    if not sess.dir.exists():
        raise FileNotFoundError(f"backup session not found: {stamp}")
    return sess
