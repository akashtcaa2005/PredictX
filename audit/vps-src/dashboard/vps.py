"""VPS and service health.

Everything here is read-only observation: psutil for machine vitals and
``systemctl show`` for unit state. Sampling is deliberately cheap — a 5 s cadence
of a handful of /proc reads is a rounding error against 2 vCPU.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

from dashboard.config import DashboardSettings
from polybot.utils.time_utils import now_s

log = logging.getLogger("polybot.dashboard.vps")

#: Unit properties fetched in one ``systemctl show`` call per unit.
_UNIT_PROPERTIES = (
    "LoadState", "ActiveState", "SubState", "UnitFileState", "Result",
    "ExecMainPID", "ExecMainStartTimestampMonotonic", "NRestarts",
    "MemoryCurrent", "CPUUsageNSec", "StatusText",
)


class VpsMonitor:
    """Samples machine metrics and systemd unit state."""

    def __init__(self, settings: DashboardSettings) -> None:
        self._s = settings
        self._last_net: tuple[float, int, int] | None = None
        self._sample: dict[str, Any] = {}
        self._stop = asyncio.Event()
        # Prime psutil's CPU delta so the first reading is not a meaningless 0.
        psutil.cpu_percent(interval=None)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample = await asyncio.to_thread(self.collect)
            except Exception as exc:  # noqa: BLE001 - monitoring must never crash the app
                log.warning("vps sampling failed", extra={"error": repr(exc)})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._s.vps_interval_s)
            except asyncio.TimeoutError:
                pass

    def snapshot(self) -> dict[str, Any]:
        return self._sample or self.collect()

    # ======================================================================
    def collect(self) -> dict[str, Any]:
        """One full sample: CPU, memory, disk, network, services and storage."""
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage(str(self._s.data_root if self._s.data_root.exists() else "/"))
        load1, load5, load15 = psutil.getloadavg()
        boot_ts = psutil.boot_time()

        return {
            "ts": now_s(),
            "cpu": {
                "percent": psutil.cpu_percent(interval=None),
                "per_core": psutil.cpu_percent(interval=None, percpu=True),
                "cores": psutil.cpu_count(logical=True) or 1,
                "physical_cores": psutil.cpu_count(logical=False) or 1,
                "load_1m": round(load1, 2),
                "load_5m": round(load5, 2),
                "load_15m": round(load15, 2),
            },
            "memory": {
                "total": memory.total,
                "used": memory.used,
                "available": memory.available,
                "percent": memory.percent,
                "swap_total": swap.total,
                "swap_used": swap.used,
                "swap_percent": swap.percent,
            },
            "disk": {
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
                "percent": disk.percent,
                "mount": str(self._s.data_root),
            },
            "network": self._network(),
            "uptime": {
                "boot_ts": boot_ts,
                "vps_uptime_s": round(time.time() - boot_ts, 1),
                "dashboard_uptime_s": round(time.time() - psutil.Process().create_time(), 1),
            },
            "process": self._process(),
            "services": self.services(),
            "storage": self.storage(),
        }

    def _network(self) -> dict[str, Any]:
        counters = psutil.net_io_counters()
        now = time.time()
        rx_rate = tx_rate = 0.0
        if self._last_net is not None:
            prev_ts, prev_rx, prev_tx = self._last_net
            elapsed = max(1e-6, now - prev_ts)
            rx_rate = max(0.0, (counters.bytes_recv - prev_rx) / elapsed)
            tx_rate = max(0.0, (counters.bytes_sent - prev_tx) / elapsed)
        self._last_net = (now, counters.bytes_recv, counters.bytes_sent)
        return {
            "bytes_recv": counters.bytes_recv,
            "bytes_sent": counters.bytes_sent,
            "packets_recv": counters.packets_recv,
            "packets_sent": counters.packets_sent,
            "errors_in": counters.errin,
            "errors_out": counters.errout,
            "drop_in": counters.dropin,
            "drop_out": counters.dropout,
            "rx_bytes_per_s": round(rx_rate, 1),
            "tx_bytes_per_s": round(tx_rate, 1),
            "connections": _connection_count(),
        }

    def _process(self) -> dict[str, Any]:
        proc = psutil.Process()
        with proc.oneshot():
            return {
                "pid": proc.pid,
                "cpu_percent": proc.cpu_percent(interval=None),
                "memory_rss": proc.memory_info().rss,
                "memory_percent": round(proc.memory_percent(), 3),
                "threads": proc.num_threads(),
                "open_files": _safe(lambda: proc.num_fds(), 0),
            }

    # ======================================================================
    def services(self) -> list[dict[str, Any]]:
        """State of each unit the dashboard knows about."""
        units = [
            ("paper", self._s.paper_unit),
            ("live", self._s.live_unit),
            ("dashboard", self._s.dashboard_unit),
        ]
        return [self.unit_status(name, unit) for name, unit in units]

    def unit_status(self, name: str, unit: str) -> dict[str, Any]:
        raw = _systemctl_show(self._s.systemctl_bin, unit)
        active = raw.get("ActiveState", "unknown")
        started_mono = _int(raw.get("ExecMainStartTimestampMonotonic"))
        uptime = None
        if started_mono and active == "active":
            uptime = max(0.0, _monotonic_us() / 1e6 - started_mono / 1e6)
        return {
            "name": name,
            "unit": unit,
            "loaded": raw.get("LoadState", "unknown"),
            "active": active,
            "sub": raw.get("SubState", "unknown"),
            "enabled": raw.get("UnitFileState", "unknown"),
            "result": raw.get("Result", ""),
            "pid": _int(raw.get("ExecMainPID")) or None,
            "restarts": _int(raw.get("NRestarts")),
            "memory_bytes": _int(raw.get("MemoryCurrent")) or None,
            "cpu_ns": _int(raw.get("CPUUsageNSec")) or None,
            "uptime_s": round(uptime, 1) if uptime is not None else None,
            "status_text": raw.get("StatusText", ""),
            "healthy": active == "active",
            "failed": active == "failed" or raw.get("Result") not in ("", "success"),
            "available": bool(raw) and raw.get("LoadState") != "not-found",
        }

    # ======================================================================
    def storage(self) -> dict[str, Any]:
        """Sizes of the databases and log trees, so growth is visible early."""
        entry: dict[str, Any] = {}
        for mode in ("paper", "live"):
            entry[f"{mode}_db_bytes"] = _file_size(self._s.engine_db(mode))
            entry[f"{mode}_log_bytes"] = _tree_size(self._s.engine_logs(mode))
        entry["dashboard_db_bytes"] = _file_size(self._s.db_path)
        entry["backup_bytes"] = _tree_size(self._s.backup_dir)
        entry["total_bytes"] = sum(v for v in entry.values() if isinstance(v, int))
        entry["data_root"] = str(self._s.data_root)
        return entry


# ======================================================================
# systemd helpers
# ======================================================================
def _systemctl_show(binary: str, unit: str) -> dict[str, str]:
    """``systemctl show`` for one unit, parsed into a dict. Empty on any failure."""
    if not shutil.which(binary) and not Path(binary).exists():
        return {}
    try:
        completed = subprocess.run(
            [binary, "show", unit, "--no-pager", "--property=" + ",".join(_UNIT_PROPERTIES)],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("systemctl show failed", extra={"unit": unit, "error": repr(exc)})
        return {}
    out: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            out[key.strip()] = value.strip()
    return out


def _monotonic_us() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC) * 1e6


def _connection_count() -> int:
    try:
        return len(psutil.net_connections(kind="inet"))
    except (psutil.AccessDenied, OSError):
        return -1  # unprivileged container: not measurable rather than zero


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _tree_size(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                total += child.stat().st_size
    except OSError:
        pass
    return total


def _int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    # systemd reports "not set" as 2^64-1.
    return 0 if parsed >= 2**63 else parsed


def _safe(fn: Any, default: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default
