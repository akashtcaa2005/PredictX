"""Canonical on-disk layout.

Paper and live trading are *physically* separated: different log directories,
different state directories, different databases. Nothing is shared, so a paper
run can never read or write live trading state (and vice versa).

    data/
      paper/      logs/  state/  engine.db
      live/       logs/  state/  engine.db
      dashboard/  dashboard.db  backups/  certs/
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Repo root: .../Bot-s1  (this file is polybot/config/paths.py -> parents[2]).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Overridable so the whole tree can live on a separate volume.
DATA_ROOT = Path(os.environ.get("POLYBOT_DATA_ROOT") or (REPO_ROOT / "data"))

MODES = ("paper", "live")


@dataclass(frozen=True)
class ModePaths:
    """Every path a single trading mode owns."""

    mode: str
    root: Path
    logs: Path
    state: Path
    db: Path
    env_file: Path
    kill_switch: Path

    def ensure(self) -> "ModePaths":
        """Create the directories if they do not exist yet."""
        for directory in (self.root, self.logs, self.state):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def mode_paths(mode: str) -> ModePaths:
    """Return the isolated path set for ``mode`` ('paper' or 'live')."""
    if mode not in MODES:
        raise ValueError(f"unknown trading mode {mode!r}; expected one of {MODES}")
    root = DATA_ROOT / mode
    return ModePaths(
        mode=mode,
        root=root,
        logs=root / "logs",
        state=root / "state",
        db=root / "engine.db",
        # Live keeps using the existing env.txt; paper gets its own file so its
        # limits and markets can be tuned without touching live configuration.
        env_file=REPO_ROOT / ("env.txt" if mode == "live" else "env.paper.txt"),
        kill_switch=root / "state" / "KILL_SWITCH",
    )


@dataclass(frozen=True)
class DashboardPaths:
    """Paths owned by the dashboard service."""

    root: Path
    db: Path
    backups: Path
    certs: Path
    uploads: Path
    env_file: Path

    def ensure(self) -> "DashboardPaths":
        for directory in (self.root, self.backups, self.certs, self.uploads):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def dashboard_paths() -> DashboardPaths:
    root = DATA_ROOT / "dashboard"
    return DashboardPaths(
        root=root,
        db=root / "dashboard.db",
        backups=root / "backups",
        certs=root / "certs",
        uploads=root / "uploads",
        env_file=REPO_ROOT / "env.dashboard.txt",
    )
