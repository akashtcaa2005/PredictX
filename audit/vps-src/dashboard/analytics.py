"""Trade and portfolio analytics.

Everything here is derived from two engine tables: ``trades`` (completed round
trips assembled from the fill stream) and ``equity`` (periodic portfolio
samples). Ratios that need more history than exists return ``None`` rather than
a misleading zero — an empty account should read "—", not "0.00 Sharpe".
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

#: Equity is sampled far more often than daily; returns are bucketed to days and
#: annualised with this factor (crypto markets trade every day).
_PERIODS_PER_YEAR = 365.0

#: Minimum number of daily returns before an *annualised* Sharpe/Sortino/volatility
#: is meaningful. Annualising ×√365 from 2-3 samples produces wild, misleading
#: figures, so below this the ratios read "—" (insufficient data) instead.
_MIN_DAILY_RETURNS_FOR_RATIO = 20


# ======================================================================
# Trade statistics
# ======================================================================
def trade_stats(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The full trade-analytics block."""
    if not trades:
        return _empty_trade_stats()

    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    scratches = len(pnls) - len(wins) - len(losses)
    durations = [float(t.get("duration_s") or 0.0) for t in trades]
    r_multiples = [float(t["r_multiple"]) for t in trades if t.get("r_multiple") is not None]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = len(wins) / len(pnls) * 100.0
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0

    current_win, current_loss = _current_streaks(pnls)
    longest_win, longest_loss = _longest_streaks(pnls)

    return {
        "total_trades": len(pnls),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "scratch_trades": scratches,
        "win_rate": round(win_rate, 2),
        "loss_rate": round(len(losses) / len(pnls) * 100.0, 2),
        "net_pnl": round(sum(pnls), 6),
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "total_fees": round(sum(float(t.get("fees") or 0.0) for t in trades), 6),
        "average_trade": round(sum(pnls) / len(pnls), 6),
        "average_win": round(avg_win, 6),
        "average_loss": round(avg_loss, 6),
        "average_holding_time_s": round(sum(durations) / len(durations), 1) if durations else 0.0,
        "median_holding_time_s": round(_median(durations), 1) if durations else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        "largest_win": round(max(pnls), 6),
        "largest_loss": round(min(pnls), 6),
        "current_win_streak": current_win,
        "current_loss_streak": current_loss,
        "longest_win_streak": longest_win,
        "longest_loss_streak": longest_loss,
        # Expectancy: average PnL per trade, expressed two ways.
        "expectancy": round(sum(pnls) / len(pnls), 6),
        "expectancy_pct": round(
            (win_rate / 100.0 * avg_win - (1 - win_rate / 100.0) * avg_loss), 6),
        "average_r_multiple": round(sum(r_multiples) / len(r_multiples), 4) if r_multiples else None,
        "risk_reward_ratio": round(avg_win / avg_loss, 4) if avg_loss > 0 else None,
        "payoff_ratio": round(avg_win / avg_loss, 4) if avg_loss > 0 else None,
        "best_market": _best_group(trades, "market_label") or _best_group(trades, "market_id"),
        "worst_market": _worst_group(trades, "market_label") or _worst_group(trades, "market_id"),
    }


def _empty_trade_stats() -> dict[str, Any]:
    # With zero completed trades, rate/average/extremum metrics are UNDEFINED, not
    # zero. Returning None makes the UI render "—"/N/A instead of a fabricated
    # "0.00% win rate". Only genuinely-zero counters and sums stay numeric.
    return {
        "total_trades": 0, "winning_trades": 0, "losing_trades": 0, "scratch_trades": 0,
        "win_rate": None, "loss_rate": None, "net_pnl": 0.0, "gross_profit": 0.0,
        "gross_loss": 0.0, "total_fees": 0.0, "average_trade": None, "average_win": None,
        "average_loss": None, "average_holding_time_s": None, "median_holding_time_s": None,
        "profit_factor": None, "largest_win": None, "largest_loss": None,
        "current_win_streak": 0, "current_loss_streak": 0, "longest_win_streak": 0,
        "longest_loss_streak": 0, "expectancy": None, "expectancy_pct": None,
        "average_r_multiple": None, "risk_reward_ratio": None, "payoff_ratio": None,
        "best_market": None, "worst_market": None,
    }


def _current_streaks(pnls: Sequence[float]) -> tuple[int, int]:
    """Streaks running up to the most recent trade."""
    win = loss = 0
    for pnl in reversed(pnls):
        if pnl > 0 and loss == 0:
            win += 1
        elif pnl < 0 and win == 0:
            loss += 1
        else:
            break
    return win, loss


def _longest_streaks(pnls: Sequence[float]) -> tuple[int, int]:
    best_win = best_loss = run_win = run_loss = 0
    for pnl in pnls:
        if pnl > 0:
            run_win += 1
            run_loss = 0
        elif pnl < 0:
            run_loss += 1
            run_win = 0
        else:
            run_win = run_loss = 0
        best_win = max(best_win, run_win)
        best_loss = max(best_loss, run_loss)
    return best_win, best_loss


# ======================================================================
# Risk-adjusted metrics (from the equity curve)
# ======================================================================
def equity_stats(equity: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Drawdown and risk-adjusted return metrics from portfolio samples."""
    points = [(float(row["ts"]), float(row["equity"])) for row in equity
              if row.get("equity") is not None]
    if len(points) < 2:
        return {"sharpe_ratio": None, "sortino_ratio": None, "calmar_ratio": None,
                "max_drawdown": 0.0, "max_drawdown_pct": 0.0, "current_drawdown": 0.0,
                "current_drawdown_pct": 0.0, "volatility_pct": None, "samples": len(points),
                "daily_returns": 0, "min_daily_returns": _MIN_DAILY_RETURNS_FOR_RATIO,
                "starting_equity": points[0][1] if points else 0.0,
                "current_equity": points[-1][1] if points else 0.0,
                "total_return_pct": 0.0, "drawdown_series": []}

    values = [v for _, v in points]
    drawdown_series, max_dd, max_dd_pct = _drawdown(points)
    peak = max(values)
    current = values[-1]
    current_dd = peak - current
    start = values[0]

    daily = _daily_returns(points)
    sharpe = sortino = volatility = None
    if len(daily) >= _MIN_DAILY_RETURNS_FOR_RATIO:
        mean = sum(daily) / len(daily)
        variance = sum((r - mean) ** 2 for r in daily) / (len(daily) - 1)
        sd = math.sqrt(variance)
        volatility = round(sd * math.sqrt(_PERIODS_PER_YEAR) * 100.0, 4)
        if sd > 0:
            sharpe = round(mean / sd * math.sqrt(_PERIODS_PER_YEAR), 4)
        downside = [r for r in daily if r < 0]
        if downside:
            dsd = math.sqrt(sum(r * r for r in downside) / len(downside))
            if dsd > 0:
                sortino = round(mean / dsd * math.sqrt(_PERIODS_PER_YEAR), 4)
        elif mean > 0:
            sortino = None  # no downside observed yet; a ratio would be meaningless

    total_return_pct = ((current / start - 1.0) * 100.0) if start else 0.0
    span_days = max(1e-9, (points[-1][0] - points[0][0]) / 86400.0)
    annualised = total_return_pct * (365.0 / span_days) if span_days >= 1 else None
    calmar = round(annualised / max_dd_pct, 4) if (annualised and max_dd_pct > 0) else None

    return {
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "max_drawdown": round(max_dd, 6),
        "max_drawdown_pct": round(max_dd_pct, 4),
        "current_drawdown": round(current_dd, 6),
        "current_drawdown_pct": round(current_dd / peak * 100.0, 4) if peak else 0.0,
        "volatility_pct": volatility,
        "samples": len(points),
        "daily_returns": len(daily),
        "min_daily_returns": _MIN_DAILY_RETURNS_FOR_RATIO,
        "starting_equity": round(start, 6),
        "current_equity": round(current, 6),
        "peak_equity": round(peak, 6),
        "total_return_pct": round(total_return_pct, 4),
        "drawdown_series": drawdown_series,
    }


def _drawdown(points: Sequence[tuple[float, float]]) -> tuple[list[dict[str, float]], float, float]:
    series: list[dict[str, float]] = []
    peak = points[0][1]
    max_dd = max_dd_pct = 0.0
    for ts, value in points:
        peak = max(peak, value)
        dd = peak - value
        dd_pct = (dd / peak * 100.0) if peak else 0.0
        max_dd = max(max_dd, dd)
        max_dd_pct = max(max_dd_pct, dd_pct)
        series.append({"ts": ts, "drawdown": round(-dd, 6), "drawdown_pct": round(-dd_pct, 4)})
    return series, max_dd, max_dd_pct


def _daily_returns(points: Sequence[tuple[float, float]]) -> list[float]:
    """Bucket equity samples into UTC days and return day-over-day returns."""
    by_day: dict[str, float] = {}
    for ts, value in points:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day] = value  # last sample of the day wins
    ordered = [by_day[day] for day in sorted(by_day)]
    returns = []
    for prev, cur in zip(ordered, ordered[1:]):
        if prev > 0:
            returns.append(cur / prev - 1.0)
    return returns


# ======================================================================
# Period PnL
# ======================================================================
def period_pnl(trades: Sequence[dict[str, Any]], *, now: float | None = None) -> dict[str, Any]:
    """Realised PnL bucketed by day, ISO week and month, plus rolling totals."""
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc) if now else datetime.now(timezone.utc)
    daily: dict[str, float] = defaultdict(float)
    weekly: dict[str, float] = defaultdict(float)
    monthly: dict[str, float] = defaultdict(float)
    daily_count: dict[str, int] = defaultdict(int)

    for trade in trades:
        when = datetime.fromtimestamp(float(trade["exit_ts"]), tz=timezone.utc)
        pnl = float(trade["pnl"])
        daily[when.strftime("%Y-%m-%d")] += pnl
        daily_count[when.strftime("%Y-%m-%d")] += 1
        iso = when.isocalendar()
        weekly[f"{iso.year}-W{iso.week:02d}"] += pnl
        monthly[when.strftime("%Y-%m")] += pnl

    today = now_dt.strftime("%Y-%m-%d")
    iso_now = now_dt.isocalendar()
    this_week = f"{iso_now.year}-W{iso_now.week:02d}"
    this_month = now_dt.strftime("%Y-%m")

    return {
        "today": round(daily.get(today, 0.0), 6),
        "this_week": round(weekly.get(this_week, 0.0), 6),
        "this_month": round(monthly.get(this_month, 0.0), 6),
        "total": round(sum(float(t["pnl"]) for t in trades), 6),
        "rolling_7d": round(_since(trades, now_dt - timedelta(days=7)), 6),
        "rolling_30d": round(_since(trades, now_dt - timedelta(days=30)), 6),
        "daily": [{"period": k, "pnl": round(v, 6), "trades": daily_count[k]}
                  for k, v in sorted(daily.items())][-90:],
        "weekly": [{"period": k, "pnl": round(v, 6)} for k, v in sorted(weekly.items())][-52:],
        "monthly": [{"period": k, "pnl": round(v, 6)} for k, v in sorted(monthly.items())][-24:],
    }


def _since(trades: Iterable[dict[str, Any]], cutoff: datetime) -> float:
    edge = cutoff.timestamp()
    return sum(float(t["pnl"]) for t in trades if float(t["exit_ts"]) >= edge)


# ======================================================================
# Breakdowns
# ======================================================================
def group_performance(trades: Sequence[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Per-strategy or per-market performance, ranked by net PnL."""
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        buckets[str(trade.get(key) or "unknown")].append(trade)

    rows = []
    for name, group in buckets.items():
        pnls = [float(t["pnl"]) for t in group]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_loss = abs(sum(losses))
        rows.append({
            "name": name,
            "trades": len(group),
            "net_pnl": round(sum(pnls), 6),
            "win_rate": round(len(wins) / len(pnls) * 100.0, 2) if pnls else 0.0,
            "average_win": round(sum(wins) / len(wins), 6) if wins else 0.0,
            "average_loss": round(gross_loss / len(losses), 6) if losses else 0.0,
            "profit_factor": round(sum(wins) / gross_loss, 4) if gross_loss > 0 else None,
            "largest_win": round(max(pnls), 6) if pnls else 0.0,
            "largest_loss": round(min(pnls), 6) if pnls else 0.0,
            "average_trade": round(sum(pnls) / len(pnls), 6) if pnls else 0.0,
        })
    rows.sort(key=lambda r: r["net_pnl"], reverse=True)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def pnl_distribution(trades: Sequence[dict[str, Any]], buckets: int = 21) -> dict[str, Any]:
    """Histogram of trade PnL, for the trade-distribution chart."""
    pnls = [float(t["pnl"]) for t in trades]
    if not pnls:
        return {"bins": [], "counts": [], "min": 0.0, "max": 0.0}
    lo, hi = min(pnls), max(pnls)
    if math.isclose(lo, hi):
        return {"bins": [round(lo, 6)], "counts": [len(pnls)], "min": lo, "max": hi}
    width = (hi - lo) / buckets
    counts = [0] * buckets
    for pnl in pnls:
        index = min(buckets - 1, int((pnl - lo) / width))
        counts[index] += 1
    bins = [round(lo + width * (i + 0.5), 6) for i in range(buckets)]
    return {"bins": bins, "counts": counts, "min": round(lo, 6), "max": round(hi, 6),
            "width": round(width, 6)}


def win_rate_history(trades: Sequence[dict[str, Any]], window: int = 20) -> list[dict[str, Any]]:
    """Rolling win rate over the last ``window`` trades, in trade order."""
    out = []
    recent: list[float] = []
    for trade in trades:
        recent.append(float(trade["pnl"]))
        if len(recent) > window:
            recent.pop(0)
        wins = sum(1 for p in recent if p > 0)
        out.append({"ts": float(trade["exit_ts"]), "trade": len(out) + 1,
                    "win_rate": round(wins / len(recent) * 100.0, 2)})
    return out[-500:]


def _best_group(trades: Sequence[dict[str, Any]], key: str) -> dict[str, Any] | None:
    rows = group_performance(trades, key)
    return rows[0] if rows and rows[0]["name"] != "unknown" else None


def _worst_group(trades: Sequence[dict[str, Any]], key: str) -> dict[str, Any] | None:
    rows = group_performance(trades, key)
    return rows[-1] if rows and rows[-1]["name"] != "unknown" else None


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


# ======================================================================
# Top-level assembly
# ======================================================================
def full_report(trades: Sequence[dict[str, Any]], equity: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The complete analytics payload the dashboard's Analytics tab renders."""
    return {
        "trades": trade_stats(trades),
        "equity": equity_stats(equity),
        "pnl": period_pnl(trades),
        "by_strategy": group_performance(trades, "strategy"),
        "by_market": group_performance(trades, "market_id"),
        "distribution": pnl_distribution(trades),
        "win_rate_history": win_rate_history(trades),
    }
