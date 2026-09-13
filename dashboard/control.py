"""Service control, emergency stop, backups and exports.

Every control action is constrained by an allowlist: the caller names a *logical*
target ("paper", "live", "dashboard") and a verb, and this module maps that to a
fixed unit name and a fixed argv. No caller-supplied string ever reaches a shell —
``subprocess`` is invoked with an argument list and ``shell=False`` throughout.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from dashboard.config import DashboardSettings
from dashboard.db import DashboardDB, EngineReader
from polybot.telemetry.schema import connect
from polybot.utils.time_utils import now_s

log = logging.getLogger("polybot.dashboard.control")

#: The only systemd verbs the dashboard may ever issue.
ALLOWED_ACTIONS = ("start", "stop", "restart")

#: Logical target -> the permission required to act on it.
TARGET_PERMISSION = {
    "paper": "control.paper",
    "live": "control.live",
    "dashboard": "control.dashboard",
}


@dataclass
class ControlResult:
    """Outcome of one control action."""

    ok: bool
    action: str
    target: str
    message: str
    detail: str = ""
    data: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "action": self.action, "target": self.target,
                "message": self.message, "detail": self.detail[:2000],
                "data": self.data or {}, "ts": now_s()}


class ControlPlane:
    """Executes privileged operations on behalf of an authorised user."""

    def __init__(self, settings: DashboardSettings, db: DashboardDB,
                 readers: dict[str, EngineReader]) -> None:
        self._s = settings
        self._db = db
        self._readers = readers

    # ======================================================================
    # systemd
    # ======================================================================
    def unit_for(self, target: str) -> str:
        units = {"paper": self._s.paper_unit, "live": self._s.live_unit,
                 "dashboard": self._s.dashboard_unit}
        if target not in units:
            raise ValueError(f"unknown control target {target!r}")
        return units[target]

    async def service_action(self, target: str, action: str) -> ControlResult:
        """Run ``systemctl <action> <unit>`` for an allowlisted target."""
        if action not in ALLOWED_ACTIONS:
            return ControlResult(False, action, target, f"action {action!r} is not permitted")
        if not self._s.control_enabled:
            return ControlResult(False, action, target,
                                 "service control is disabled (DASHBOARD_CONTROL_ENABLED=false)")
        try:
            unit = self.unit_for(target)
        except ValueError as exc:
            return ControlResult(False, action, target, str(exc))

        # Restarting ourselves would kill the response in flight; defer it.
        if target == "dashboard" and action in ("restart", "stop"):
            asyncio.get_running_loop().call_later(
                1.0, lambda: asyncio.ensure_future(self._run_systemctl(action, unit)))
            return ControlResult(True, action, target,
                                 f"{action} scheduled for {unit} in 1s; the page will reconnect")

        return await self._run_systemctl(action, unit)

    async def _run_systemctl(self, action: str, unit: str) -> ControlResult:
        argv = self._systemctl_argv(action, unit)
        try:
            completed = await asyncio.to_thread(
                subprocess.run, argv, capture_output=True, text=True, timeout=45, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("systemctl failed", extra={"unit": unit, "action": action,
                                                 "error": repr(exc)})
            return ControlResult(False, action, unit, "could not invoke systemctl", repr(exc))

        detail = (completed.stdout + completed.stderr).strip()
        if completed.returncode == 0:
            log.info("systemctl ok", extra={"unit": unit, "action": action})
            return ControlResult(True, action, unit, f"{action} {unit} succeeded", detail)
        hint = ""
        if "password" in detail.lower() or "not allowed" in detail.lower():
            hint = (" — the dashboard user needs the sudoers rule from "
                    "deploy/polybot-dashboard.sudoers")
        return ControlResult(False, action, unit,
                             f"{action} {unit} failed (exit {completed.returncode}){hint}", detail)

    def _systemctl_argv(self, action: str, unit: str) -> list[str]:
        argv = [self._s.systemctl_bin, action, unit, "--no-block"]
        if self._s.use_sudo:
            # -n: never prompt. If the sudoers rule is missing this fails loudly
            # instead of hanging the request on a password prompt.
            return ["/usr/bin/sudo", "-n", *argv]
        return argv

    async def journal_tail(self, target: str, lines: int = 200) -> list[str]:
        """Recent journald output for a unit (falls back to empty on any error)."""
        try:
            unit = self.unit_for(target)
        except ValueError:
            return []
        argv = ["/usr/bin/journalctl", "-u", unit, "-n", str(max(1, min(1000, lines))),
                "--no-pager", "-o", "short-iso"]
        if self._s.use_sudo:
            argv = ["/usr/bin/sudo", "-n", *argv]
        try:
            completed = await asyncio.to_thread(
                subprocess.run, argv, capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            return []
        return completed.stdout.splitlines() if completed.returncode == 0 else []

    # ======================================================================
    # Emergency stop
    # ======================================================================
    def kill_switch_path(self, mode: str) -> Path:
        return self._s.engine_state(mode) / "KILL_SWITCH"

    def kill_switch_active(self, mode: str) -> bool:
        return self.kill_switch_path(mode).exists()

    async def emergency_stop(self, mode: str, *, reason: str, actor: str) -> ControlResult:
        """Halt trading immediately: kill switch first, then stop the unit.

        The kill switch is written *before* the service is stopped so the running
        engine cancels its open orders itself; stopping the unit alone would leave
        resting orders on the book.
        """
        if mode not in ("paper", "live"):
            return ControlResult(False, "emergency_stop", mode, "unknown mode")
        path = self.kill_switch_path(mode)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "ts": now_s(),
                "iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "actor": actor,
                "reason": reason or "emergency stop from dashboard",
            }, indent=2), encoding="utf-8")
        except OSError as exc:
            return ControlResult(False, "emergency_stop", mode,
                                 "could not write kill switch", repr(exc))

        log.critical("EMERGENCY STOP engaged", extra={"mode": mode, "actor": actor,
                                                      "reason": reason})
        # Give the engine a moment to see the switch and pull its orders.
        await asyncio.sleep(3.0)
        stop_result = await self.service_action(mode, "stop")
        return ControlResult(
            True, "emergency_stop", mode,
            f"kill switch written for {mode}; open orders cancelled by the engine",
            stop_result.message, {"kill_switch": str(path), "service_stopped": stop_result.ok})

    def clear_kill_switch(self, mode: str) -> ControlResult:
        path = self.kill_switch_path(mode)
        if not path.exists():
            return ControlResult(True, "clear_kill_switch", mode, "no kill switch was set")
        try:
            path.unlink()
        except OSError as exc:
            return ControlResult(False, "clear_kill_switch", mode,
                                 "could not remove kill switch", repr(exc))
        return ControlResult(True, "clear_kill_switch", mode,
                             f"kill switch cleared for {mode}; start the service to resume")

    # ======================================================================
    # Backups
    # ======================================================================
    async def backup(self, scope: str = "all") -> ControlResult:
        """Consistent online backup of the dashboard and/or engine databases."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target_dir = self._s.backup_dir / stamp
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ControlResult(False, "backup", scope, "could not create backup directory",
                                 repr(exc))

        written: dict[str, int] = {}
        errors: list[str] = []
        if scope in ("all", "dashboard"):
            try:
                path = await asyncio.to_thread(self._db.backup_to, target_dir / "dashboard.db")
                written["dashboard"] = path.stat().st_size
            except Exception as exc:  # noqa: BLE001
                errors.append(f"dashboard: {exc!r}")

        for mode in ("paper", "live"):
            if scope not in ("all", mode):
                continue
            source = self._s.engine_db(mode)
            if not source.is_file():
                continue
            try:
                size = await asyncio.to_thread(_sqlite_backup, source, target_dir / f"{mode}.db")
                written[mode] = size
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{mode}: {exc!r}")

        self._prune_backups()
        ok = bool(written) and not errors
        return ControlResult(
            ok, "backup", scope,
            f"backed up {len(written)} database(s) to {target_dir.name}" if written
            else "nothing to back up",
            "; ".join(errors), {"directory": str(target_dir), "files": written})

    def _prune_backups(self, keep: int = 14) -> None:
        """Keep only the newest ``keep`` backup sets; an 80 GB disk is not a vault."""
        try:
            sets = sorted((p for p in self._s.backup_dir.iterdir() if p.is_dir()),
                          key=lambda p: p.name, reverse=True)
        except OSError:
            return
        for stale in sets[keep:]:
            shutil.rmtree(stale, ignore_errors=True)

    def list_backups(self) -> list[dict[str, Any]]:
        try:
            entries = sorted((p for p in self._s.backup_dir.iterdir() if p.is_dir()),
                             key=lambda p: p.name, reverse=True)
        except OSError:
            return []
        rows = []
        for entry in entries:
            files = [f for f in entry.iterdir() if f.is_file()]
            rows.append({
                "name": entry.name,
                "files": [f.name for f in files],
                "bytes": sum(f.stat().st_size for f in files),
                "ts": entry.stat().st_mtime,
            })
        return rows

    # ======================================================================
    # Exports
    # ======================================================================
    def export_trades(self, mode: str, fmt: str = "csv", *, limit: int = 100_000) -> tuple[str, str, str]:
        """Return ``(filename, media_type, body)`` for a trade export."""
        reader = self._readers.get(mode)
        rows = reader.query(
            "SELECT id, market_id, market_label, side, strategy, qty, entry_price, exit_price, "
            "entry_ts, exit_ts, duration_s, gross_pnl, fees, pnl, return_pct, r_multiple "
            "FROM trades ORDER BY exit_ts DESC LIMIT ?", (limit,)) if reader else []
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if fmt == "json":
            return (f"polybot-{mode}-trades-{stamp}.json", "application/json",
                    json.dumps(rows, indent=2, default=str))
        return (f"polybot-{mode}-trades-{stamp}.csv", "text/csv", _to_csv(rows))

    def export_orders(self, mode: str, fmt: str = "csv", *, limit: int = 100_000) -> tuple[str, str, str]:
        reader = self._readers.get(mode)
        rows = reader.query(
            "SELECT client_id, exchange_order_id, ts_created, ts_updated, market_id, side, "
            "action, order_type, price, size, filled_size, avg_fill_price, notional, fees, "
            "status, last_error, execution_ms, simulated, strategy FROM orders "
            "ORDER BY ts_created DESC LIMIT ?", (limit,)) if reader else []
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if fmt == "json":
            return (f"polybot-{mode}-orders-{stamp}.json", "application/json",
                    json.dumps(rows, indent=2, default=str))
        return (f"polybot-{mode}-orders-{stamp}.csv", "text/csv", _to_csv(rows))

    def export_journal(self, mode: str, fmt: str = "csv") -> tuple[str, str, str]:
        rows = self._db.query(
            "SELECT * FROM journal WHERE mode = ? ORDER BY exit_ts DESC", (mode,))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if fmt == "json":
            return (f"polybot-{mode}-journal-{stamp}.json", "application/json",
                    json.dumps(rows, indent=2, default=str))
        return (f"polybot-{mode}-journal-{stamp}.csv", "text/csv", _to_csv(rows))


def _sqlite_backup(source: Path, destination: Path) -> int:
    """Hot-copy a SQLite database using the backup API (WAL-safe)."""
    src = connect(source, read_only=True)
    try:
        import sqlite3

        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return destination.stat().st_size


def _to_csv(rows: Sequence[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def read_log_tail(path: Path, lines: int = 300) -> list[str]:
    """Last ``lines`` lines of a log file, read without loading the whole file."""
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        block = min(size, max(8192, lines * 200))
        with path.open("rb") as handle:
            handle.seek(max(0, size - block))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]


def list_log_files(directory: Path) -> list[dict[str, Any]]:
    try:
        files = sorted(directory.glob("*.log"))
    except OSError:
        return []
    out = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({"name": path.name, "bytes": stat.st_size, "modified": stat.st_mtime})
    return out


def iter_log_names(directories: Iterable[Path]) -> list[str]:
    names: set[str] = set()
    for directory in directories:
        for entry in list_log_files(directory):
            names.add(entry["name"])
    return sorted(names)
