"""Structured, read-only views onto one trading engine's telemetry database.

Everything the dashboard shows for a mode (paper or live) is assembled here from
the engine's own records, so the view is identical in shape for both modes and
survives the engine being stopped: the last written state is still there.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from dashboard.analytics import full_report
from dashboard.config import DashboardSettings
from dashboard.db import EngineReader
from polybot.utils.time_utils import now_s

log = logging.getLogger("polybot.dashboard.engine_view")

_ORDER_COLUMNS = (
    "client_id, exchange_order_id, ts_created, ts_updated, ts_submitted, market_id, "
    "market_label, side, token_id, action, order_type, price, size, filled_size, "
    "avg_fill_price, notional, fees, status, last_error, exchange_response, execution_ms, "
    "simulated, strategy"
)

_OPEN_STATUSES = ("pending", "live", "partially_filled", "matched")


class EngineView:
    """Query helpers over one engine database."""

    def __init__(self, mode: str, reader: EngineReader, settings: DashboardSettings) -> None:
        self.mode = mode
        self._reader = reader
        self._s = settings

    # ======================================================================
    # Raw sections
    # ======================================================================
    def heartbeat(self) -> dict[str, Any]:
        row = self._reader.query_one(
            "SELECT value, ts FROM engine_state WHERE key = 'heartbeat'")
        if not row:
            return {}
        data = _json(row["value"])
        data["_written_ts"] = row["ts"]
        return data

    def engine_config(self) -> dict[str, Any]:
        row = self._reader.query_one("SELECT value FROM engine_state WHERE key = 'config'")
        return _json(row["value"]) if row else {}

    def startup_info(self) -> dict[str, Any]:
        row = self._reader.query_one("SELECT value FROM engine_state WHERE key = 'startup'")
        return _json(row["value"]) if row else {}

    def readiness(self) -> dict[str, Any]:
        """The engine's trading-readiness verdict (``state/readiness.json``).

        Written by ``polybot.wallet.readiness.TradingReadiness`` on startup: the
        consolidated auth / maker-wallet / allowance / balance check that says
        whether live trading is actually possible. Absent until the engine has
        run at least once.
        """
        path = self._s.engine_state(self.mode) / "readiness.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if isinstance(data, dict):
            data["_file_ts"] = path.stat().st_mtime
            return data
        return {}

    def orders(self, *, status: str | None = None, limit: int = 200,
               market_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status == "open":
            clauses.append(f"status IN ({','.join('?' * len(_OPEN_STATUSES))})")
            params.extend(_OPEN_STATUSES)
        elif status == "filled":
            clauses.append("status IN ('filled', 'partially_filled')")
        elif status == "cancelled":
            clauses.append("status IN ('canceled', 'expired')")
        elif status == "rejected":
            clauses.append("status IN ('rejected', 'failed')")
        elif status == "pending":
            clauses.append("status = 'pending'")
        if market_id:
            clauses.append("market_id = ?")
            params.append(market_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(2000, limit)))
        rows = self._reader.query(
            f"SELECT {_ORDER_COLUMNS} FROM orders {where} ORDER BY ts_created DESC LIMIT ?",
            params)
        return [_decorate_order(row) for row in rows]

    def order_counts(self) -> dict[str, int]:
        rows = self._reader.query("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")
        counts = {row["status"]: int(row["n"]) for row in rows}
        counts["open"] = sum(counts.get(status, 0) for status in _OPEN_STATUSES)
        counts["total"] = sum(int(row["n"]) for row in rows)
        counts["partial"] = counts.get("partially_filled", 0)
        return counts

    def fills(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._reader.query(
            "SELECT id, ts, client_id, exchange_order_id, market_id, side, action, price, size, "
            "fee, realized_pnl, strategy FROM fills ORDER BY ts DESC LIMIT ?",
            (max(1, min(2000, limit)),))

    def trades(self, limit: int = 1000) -> list[dict[str, Any]]:
        return self._reader.query(
            "SELECT id, market_id, market_label, side, strategy, qty, entry_price, exit_price, "
            "entry_ts, exit_ts, duration_s, gross_pnl, fees, pnl, return_pct, r_multiple, "
            "entry_reason, exit_reason, confidence FROM trades ORDER BY exit_ts DESC LIMIT ?",
            (max(1, min(20_000, limit)),))

    def positions(self) -> list[dict[str, Any]]:
        return self._reader.query(
            "SELECT market_id, market_label, side, token_id, qty, avg_price, realized_pnl, "
            "mark_price, unrealized, ts_updated FROM positions WHERE qty > 0 "
            "ORDER BY qty * avg_price DESC")

    def equity(self, limit: int = 2000) -> list[dict[str, Any]]:
        rows = self._reader.query(
            "SELECT ts, balance, cash, positions_value, equity, realized_pnl, unrealized_pnl, "
            "exposure, open_orders, open_positions FROM equity ORDER BY ts DESC LIMIT ?",
            (max(2, min(20_000, limit)),))
        rows.reverse()
        return rows

    def events(self, limit: int = 200, *, level: str | None = None) -> list[dict[str, Any]]:
        if level:
            return self._reader.query(
                "SELECT id, ts, level, kind, message, data FROM events WHERE level = ? "
                "ORDER BY ts DESC LIMIT ?", (level, max(1, min(2000, limit))))
        return self._reader.query(
            "SELECT id, ts, level, kind, message, data FROM events ORDER BY ts DESC LIMIT ?",
            (max(1, min(2000, limit)),))

    def strategy_states(self) -> list[dict[str, Any]]:
        return self._reader.query(
            "SELECT strategy, market_id, market_label, ts, enabled, running, last_signal, "
            "last_signal_ts, current_signal, confidence, position_qty, entry_price, exit_price, "
            "stop_loss, take_profit, unrealized, ticks, errors FROM strategy_state "
            "ORDER BY strategy, market_id")

    # ======================================================================
    # Composite views
    # ======================================================================
    def strategies(self) -> list[dict[str, Any]]:
        """Per-strategy dashboard rows: live status merged with realised performance."""
        states = self.strategy_states()
        trades = self.trades(limit=20_000)
        performance = {row["name"]: row for row in _by_strategy(trades)}

        grouped: dict[str, dict[str, Any]] = {}
        for state in states:
            name = state["strategy"]
            entry = grouped.setdefault(name, {
                "name": name, "enabled": False, "running": False, "markets": [],
                "ticks": 0, "errors": 0, "current_pnl": 0.0, "position_qty": 0.0,
                "last_signal": None, "last_signal_ts": None, "current_signal": None,
                "confidence": None,
            })
            entry["enabled"] = entry["enabled"] or bool(state["enabled"])
            entry["running"] = entry["running"] or bool(state["running"])
            entry["ticks"] = max(entry["ticks"], int(state["ticks"] or 0))
            entry["errors"] = max(entry["errors"], int(state["errors"] or 0))
            entry["current_pnl"] += float(state["unrealized"] or 0.0)
            entry["position_qty"] += float(state["position_qty"] or 0.0)
            if state["last_signal_ts"] and (entry["last_signal_ts"] or 0) < state["last_signal_ts"]:
                entry["last_signal"] = state["last_signal"]
                entry["last_signal_ts"] = state["last_signal_ts"]
            entry["current_signal"] = state["current_signal"] or entry["current_signal"]
            if state["confidence"] is not None:
                entry["confidence"] = state["confidence"]
            entry["markets"].append({
                "market_id": state["market_id"],
                "label": state["market_label"],
                "position_qty": state["position_qty"],
                "entry_price": state["entry_price"],
                "exit_price": state["exit_price"],
                "stop_loss": state["stop_loss"],
                "take_profit": state["take_profit"],
                "unrealized": state["unrealized"],
                "signal": state["current_signal"] or state["last_signal"],
            })

        rows = []
        for name, entry in grouped.items():
            stats = performance.get(name, {})
            # A strategy with no completed trades has UNDEFINED performance, not
            # zero. Absent stats -> None so the UI shows "—", never a fabricated
            # "0.00%". "accuracy" is deliberately dropped: the Strategy contract
            # (polybot/strategy/base.py) exposes no accuracy metric, so inventing
            # one (it was a duplicate of win_rate) violated the "NOT PROVIDED BY
            # STRATEGY" rule.
            rows.append({
                **entry,
                "total_pnl": stats.get("net_pnl"),
                "win_rate": stats.get("win_rate"),
                "average_win": stats.get("average_win"),
                "average_loss": stats.get("average_loss"),
                "total_trades": stats.get("trades", 0),
                "profit_factor": stats.get("profit_factor"),
                "average_trade": stats.get("average_trade"),
            })
        # total_pnl may be None (no completed trades); rank those last.
        rows.sort(key=lambda r: (r["total_pnl"] is not None, r["total_pnl"] or 0.0),
                  reverse=True)
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        return rows

    def summary(self, service: dict[str, Any] | None = None) -> dict[str, Any]:
        """The compact per-mode block carried in every websocket snapshot."""
        heartbeat = self.heartbeat()
        counts = self.order_counts()
        positions = self.positions()
        written_ts = heartbeat.get("_written_ts") or heartbeat.get("ts_epoch") or 0.0
        age = round(now_s() - written_ts, 1) if written_ts else None

        pnl = heartbeat.get("pnl") or {}
        risk = heartbeat.get("risk") or {}
        equity_tail = self.equity(limit=self._s.history_points)

        return {
            "mode": self.mode,
            "available": self._reader.available,
            "heartbeat": heartbeat,
            "heartbeat_age_s": age,
            "live": bool(age is not None and age < self._s.alert_engine_stale_s),
            "service": service or {},
            "equity": round(float(heartbeat.get("equity") or 0.0), 6),
            "cash": round(float(heartbeat.get("cash") or 0.0), 6),
            "positions_value": round(float(heartbeat.get("positions_value") or 0.0), 6),
            "exposure": round(float(heartbeat.get("exposure") or 0.0), 6),
            "pnl": {
                "realized": float(pnl.get("realized") or 0.0),
                "unrealized": float(pnl.get("unrealized") or 0.0),
                "total": float(pnl.get("total") or 0.0),
            },
            "risk": risk,
            "readiness": self.readiness(),
            "kill_switch": (self._s.engine_state(self.mode) / "KILL_SWITCH").exists(),
            "orders": counts,
            "open_orders": self.orders(status="open", limit=100),
            "positions": positions,
            "open_positions": len(positions),
            "strategies": self.strategies(),
            "recent_trades": self.trades(limit=25),
            "recent_events": self.events(limit=25),
            "equity_series": [
                {"ts": row["ts"], "equity": row["equity"], "cash": row["cash"],
                 "realized": row["realized_pnl"], "unrealized": row["unrealized_pnl"]}
                for row in equity_tail
            ],
            "markets": heartbeat.get("markets") or [],
            "connections": heartbeat.get("connections") or [],
            "db_bytes": self._reader.size_bytes(),
        }

    def analytics(self, *, trade_limit: int = 20_000) -> dict[str, Any]:
        """The full analytics report for this mode."""
        return full_report(self.trades(limit=trade_limit), self.equity(limit=20_000))


# ======================================================================
# Helpers
# ======================================================================
def _decorate_order(row: dict[str, Any]) -> dict[str, Any]:
    size = float(row.get("size") or 0.0)
    filled = float(row.get("filled_size") or 0.0)
    row["remaining_size"] = round(max(0.0, size - filled), 6)
    row["filled_pct"] = round(filled / size * 100.0, 2) if size else 0.0
    row["is_open"] = row.get("status") in _OPEN_STATUSES
    response = row.pop("exchange_response", None)
    row["exchange_response"] = _shorten(response)
    return row


def _shorten(value: Any, limit: int = 600) -> str | None:
    if value in (None, ""):
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _by_strategy(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from dashboard.analytics import group_performance

    return group_performance(trades, "strategy")


# ======================================================================
# Live trading readiness (the "LIVE READINESS" safety panel, Phase 17)
# ======================================================================
#: Every requirement that must pass before live trading is considered READY.
#: The ENABLE_LIVE_TRADING flag is reported but ADVISORY (arming is a deliberate
#: manual step), so it is intentionally NOT in this mandatory set.
LIVE_READINESS_MANDATORY = (
    "authentication", "not_closed_only", "maker_wallet", "usdc_allowance",
    "deposit_wallet_enabled", "usdc_balance", "clob_connectivity",
    "geographic_availability", "market_active", "market_accepting_orders",
    "strategy_loaded", "risk_ok",
)

_NEW_CHECK_BLOCKERS = {
    "clob_connectivity": "CLOB CONNECTIVITY: the live engine is not reporting a live CLOB connection.",
    "geographic_availability": "GEOGRAPHIC AVAILABILITY: CLOB is unreachable or geoblocked (HTTP 403) from this region.",
    "market_active": "MARKET: no configured market is currently active.",
    "market_accepting_orders": "MARKET: no active market is accepting orders right now.",
    "strategy_loaded": "STRATEGY: the engine strategy is not loaded/running.",
    "risk_ok": "RISK: the engine is halted or its risk state is unavailable.",
}


def build_live_readiness(*, base: dict[str, Any], heartbeat: dict[str, Any],
                         heartbeat_age_s: float | None, config: dict[str, Any],
                         markets: dict[str, Any], live_account: dict[str, Any],
                         stale_after_s: float) -> dict[str, Any]:
    """Assemble the COMPLETE live-readiness verdict (Phase 17).

    The engine's ``readiness.json`` covers only the account checks (auth, maker
    wallet, allowance, balance). This augments it — from the live heartbeat, the
    engine config, the market feed and the live-account sync — with the remaining
    mandated requirements (CLOB connectivity, geography, market active/accepting,
    strategy loaded, risk state) and the ENABLE_LIVE_TRADING flag, then computes
    ``live_ready`` over the FULL mandatory set. Conservative: anything that cannot
    be confirmed reads "blocked", so the panel never over-reports readiness.
    """
    checks: dict[str, Any] = dict(base.get("checks") or {})
    missing: list[str] = list(base.get("missing_requirements") or [])
    engine_live = heartbeat_age_s is not None and heartbeat_age_s < stale_after_s

    status = live_account.get("status") or {}
    conns = {c.get("channel"): c for c in (heartbeat.get("connections") or [])}
    market_conn = bool(conns.get("market", {}).get("connected"))
    api_ok = bool(status.get("api_ok"))
    clob_ok = engine_live and (market_conn or api_ok)
    checks["clob_connectivity"] = {
        "ok": clob_ok,
        "detail": "live CLOB connection active" if clob_ok
                  else "no live CLOB connection reported (engine stopped?)"}

    last_err = str(status.get("last_error") or "")
    geoblocked = ("403" in last_err) or any(w in last_err.lower()
                                            for w in ("geo", "restricted", "forbidden"))
    geo_ok = clob_ok and not geoblocked
    checks["geographic_availability"] = {
        "ok": geo_ok,
        "detail": "CLOB reachable from the deployment region; no geoblock seen" if geo_ok
                  else ("geoblock / HTTP 403 detected" if geoblocked
                        else "cannot confirm until the CLOB is reachable")}

    mkts = markets.get("markets") or []
    active = [m for m in mkts if m.get("active")]
    accepting = [m for m in active if m.get("accepting_orders")]
    checks["market_active"] = {
        "ok": bool(active),
        "detail": f"{len(active)}/{len(mkts)} tracked market(s) active"}
    checks["market_accepting_orders"] = {
        "ok": bool(accepting),
        "detail": (f"{len(accepting)} market(s) accepting orders" if accepting
                   else "no active market is accepting orders")}

    strat = heartbeat.get("strategy") or {}
    strat_ok = engine_live and bool(strat.get("name"))
    checks["strategy_loaded"] = {
        "ok": strat_ok,
        "detail": f"{strat.get('name')} loaded" if strat_ok
                  else "engine not running / no strategy loaded"}

    risk = heartbeat.get("risk") or {}
    risk_ok = engine_live and (risk.get("halted") is False)
    if risk.get("halted"):
        risk_detail = f"HALTED: {risk.get('halt_reason') or 'unknown'}"
    elif risk_ok:
        risk_detail = f"active, not halted; daily loss limit {risk.get('daily_loss_limit')}"
    else:
        risk_detail = "engine not running / risk state unavailable"
    checks["risk_ok"] = {"ok": risk_ok, "detail": risk_detail}

    flag_on = bool(config.get("enable_live_trading"))
    checks["live_trading_flag"] = {
        "ok": flag_on, "advisory": True,
        "detail": "ENABLE_LIVE_TRADING=true (ARMED — real funds)" if flag_on
                  else "ENABLE_LIVE_TRADING=false (dry-run) — arming is a deliberate manual step"}

    if not base.get("checks"):
        blocker = ("ENGINE READINESS: no account readiness report yet — the live engine has "
                   "not evaluated wallet / allowance / balance.")
        if blocker not in missing:
            missing.append(blocker)

    for name in LIVE_READINESS_MANDATORY:
        check = checks.get(name)
        if (not check or not check.get("ok")) and name in _NEW_CHECK_BLOCKERS:
            if _NEW_CHECK_BLOCKERS[name] not in missing:
                missing.append(_NEW_CHECK_BLOCKERS[name])

    live_ready = all(bool((checks.get(n) or {}).get("ok")) for n in LIVE_READINESS_MANDATORY)

    out = dict(base)
    out["checks"] = checks
    out["missing_requirements"] = missing
    out["live_ready"] = live_ready
    out["live_trading_armed"] = flag_on
    out["mandatory_checks"] = list(LIVE_READINESS_MANDATORY)
    out["_augmented"] = True
    return out
