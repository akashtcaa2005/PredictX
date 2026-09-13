"""Technical indicators computed over the dashboard's own candle series.

Polymarket does not publish OHLCV for its markets, so the dashboard builds
candles itself from the mid price of each tracked market (see
:mod:`dashboard.market_feed`) and computes indicators over that series. Every
number here is therefore derived from observed order-book prices and traded
size — nothing is imputed or back-filled.

All functions take a plain list of floats (oldest first) and return either a
series of the same length (``None`` where the window is not yet full) or the
latest value only. Pure Python, no numpy: the whole point is to stay cheap on a
2 vCPU box.
"""
from __future__ import annotations

from typing import Any, Sequence

Number = float | None


# ======================================================================
# Moving averages
# ======================================================================
def sma(values: Sequence[float], period: int) -> list[Number]:
    """Simple moving average."""
    out: list[Number] = []
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= period:
            total -= values[i - period]
        out.append(total / period if i >= period - 1 else None)
    return out


def ema(values: Sequence[float], period: int) -> list[Number]:
    """Exponential moving average, seeded with the first ``period`` SMA."""
    out: list[Number] = [None] * len(values)
    if len(values) < period or period <= 0:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def wilder_smooth(values: Sequence[float], period: int) -> list[Number]:
    """Wilder's smoothing (used by RSI/ATR/ADX): an EMA with k = 1/period."""
    out: list[Number] = [None] * len(values)
    if len(values) < period or period <= 0:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


# ======================================================================
# Oscillators
# ======================================================================
def rsi(closes: Sequence[float], period: int = 14) -> list[Number]:
    """Relative Strength Index (Wilder)."""
    out: list[Number] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = wilder_smooth(gains[1:], period)
    avg_loss = wilder_smooth(losses[1:], period)
    for i, (g, l) in enumerate(zip(avg_gain, avg_loss), start=1):
        if g is None or l is None:
            continue
        out[i] = 100.0 if l == 0 else 100.0 - (100.0 / (1.0 + g / l))
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> dict[str, list[Number]]:
    """MACD line, signal line and histogram."""
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    line: list[Number] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)
    ]
    defined = [(i, v) for i, v in enumerate(line) if v is not None]
    signal_line: list[Number] = [None] * len(closes)
    hist: list[Number] = [None] * len(closes)
    if len(defined) >= signal:
        values = [v for _, v in defined]
        sig = ema(values, signal)  # type: ignore[arg-type]
        for (idx, _), s in zip(defined, sig):
            signal_line[idx] = s
            if s is not None and line[idx] is not None:
                hist[idx] = line[idx] - s  # type: ignore[operator]
    return {"macd": line, "signal": signal_line, "histogram": hist}


# ======================================================================
# Volatility
# ======================================================================
def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    out = [highs[0] - lows[0]] if highs else []
    for i in range(1, len(highs)):
        out.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int = 14) -> list[Number]:
    """Average True Range (Wilder)."""
    if not highs:
        return []
    return wilder_smooth(true_range(highs, lows, closes), period)


def bollinger(closes: Sequence[float], period: int = 20, std_mult: float = 2.0
              ) -> dict[str, list[Number]]:
    """Bollinger Bands with %B and bandwidth."""
    middle = sma(closes, period)
    upper: list[Number] = [None] * len(closes)
    lower: list[Number] = [None] * len(closes)
    percent_b: list[Number] = [None] * len(closes)
    bandwidth: list[Number] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1: i + 1]
        mean = middle[i]
        if mean is None:
            continue
        variance = sum((x - mean) ** 2 for x in window) / period
        sd = variance ** 0.5
        upper[i] = mean + std_mult * sd
        lower[i] = mean - std_mult * sd
        span = upper[i] - lower[i]  # type: ignore[operator]
        percent_b[i] = ((closes[i] - lower[i]) / span) if span else 0.5  # type: ignore[operator]
        bandwidth[i] = (span / mean) if mean else 0.0
    return {"middle": middle, "upper": upper, "lower": lower,
            "percent_b": percent_b, "bandwidth": bandwidth}


def realised_volatility(closes: Sequence[float], period: int = 20) -> Number:
    """Standard deviation of log returns over the window, as a percentage."""
    if len(closes) < period + 1:
        return None
    import math

    returns = []
    for i in range(len(closes) - period, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev > 0 and cur > 0:
            returns.append(math.log(cur / prev))
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return round(variance ** 0.5 * 100.0, 4)


# ======================================================================
# Volume
# ======================================================================
def vwap(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
         volumes: Sequence[float]) -> list[Number]:
    """Session VWAP over the supplied candles (cumulative from the first bar)."""
    out: list[Number] = []
    cum_pv = 0.0
    cum_v = 0.0
    for h, l, c, v in zip(highs, lows, closes, volumes):
        typical = (h + l + c) / 3.0
        cum_pv += typical * v
        cum_v += v
        out.append((cum_pv / cum_v) if cum_v > 0 else None)
    return out


def volume_delta(buy_volumes: Sequence[float], sell_volumes: Sequence[float],
                 period: int = 20) -> dict[str, Number]:
    """Net aggressive buy volume over the window, plus its ratio."""
    buys = sum(buy_volumes[-period:])
    sells = sum(sell_volumes[-period:])
    total = buys + sells
    return {
        "buy_volume": round(buys, 4),
        "sell_volume": round(sells, 4),
        "delta": round(buys - sells, 4),
        "ratio": round((buys - sells) / total, 4) if total else None,
    }


# ======================================================================
# Trend
# ======================================================================
def momentum(closes: Sequence[float], period: int = 10) -> Number:
    """Percentage change over ``period`` bars."""
    if len(closes) <= period or closes[-period - 1] == 0:
        return None
    return round((closes[-1] / closes[-period - 1] - 1.0) * 100.0, 4)


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int = 14) -> dict[str, Number]:
    """Average Directional Index — the trend-strength reading (0-100)."""
    n = len(highs)
    if n < period * 2 + 1:
        return {"adx": None, "plus_di": None, "minus_di": None}
    plus_dm, minus_dm = [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    tr = true_range(highs, lows, closes)[1:]
    atr_s = wilder_smooth(tr, period)
    plus_s = wilder_smooth(plus_dm, period)
    minus_s = wilder_smooth(minus_dm, period)

    dx: list[float] = []
    plus_di = minus_di = None
    for a, p, m in zip(atr_s, plus_s, minus_s):
        if a in (None, 0) or p is None or m is None:
            continue
        plus_di = 100.0 * p / a
        minus_di = 100.0 * m / a
        total = plus_di + minus_di
        dx.append(100.0 * abs(plus_di - minus_di) / total if total else 0.0)
    if len(dx) < period:
        return {"adx": None, "plus_di": _r(plus_di), "minus_di": _r(minus_di)}
    smoothed = wilder_smooth(dx, period)
    latest = next((v for v in reversed(smoothed) if v is not None), None)
    return {"adx": _r(latest), "plus_di": _r(plus_di), "minus_di": _r(minus_di)}


# ======================================================================
# Aggregate
# ======================================================================
def compute_all(candles: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Every indicator the dashboard displays, for one market's candle series.

    Returns latest values plus short tails of the main series for charting.
    ``None`` means "not enough history yet" and is rendered as ``—``.
    """
    if not candles:
        return {"ready": False, "bars": 0}

    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    volumes = [float(c.get("volume") or 0.0) for c in candles]
    buys = [float(c.get("buy_volume") or 0.0) for c in candles]
    sells = [float(c.get("sell_volume") or 0.0) for c in candles]

    ema9, ema21, ema50 = ema(closes, 9), ema(closes, 21), ema(closes, 50)
    macd_data = macd(closes)
    bands = bollinger(closes)
    rsi_series = rsi(closes)
    atr_series = atr(highs, lows, closes)
    vwap_series = vwap(highs, lows, closes, volumes)
    trend = adx(highs, lows, closes)

    return {
        "ready": True,
        "bars": len(candles),
        "last_close": round(closes[-1], 6),
        "ema9": _last(ema9),
        "ema21": _last(ema21),
        "ema50": _last(ema50),
        "ema_alignment": _alignment(_last(ema9), _last(ema21), _last(ema50)),
        "rsi": _last(rsi_series),
        "macd": _last(macd_data["macd"]),
        "macd_signal": _last(macd_data["signal"]),
        "macd_histogram": _last(macd_data["histogram"]),
        "atr": _last(atr_series),
        "atr_pct": _pct(_last(atr_series), closes[-1]),
        "vwap": _last(vwap_series),
        "vwap_distance_pct": _pct_diff(closes[-1], _last(vwap_series)),
        "bb_upper": _last(bands["upper"]),
        "bb_middle": _last(bands["middle"]),
        "bb_lower": _last(bands["lower"]),
        "bb_percent_b": _last(bands["percent_b"]),
        "bb_bandwidth": _last(bands["bandwidth"]),
        "volatility_pct": realised_volatility(closes),
        "volume_delta": volume_delta(buys, sells),
        "momentum_pct": momentum(closes),
        "trend_strength": trend["adx"],
        "plus_di": trend["plus_di"],
        "minus_di": trend["minus_di"],
        "series": {
            "ts": [int(c["ts"]) for c in candles[-180:]],
            "close": [round(v, 6) for v in closes[-180:]],
            "ema9": _tail(ema9, 180),
            "ema21": _tail(ema21, 180),
            "ema50": _tail(ema50, 180),
            "bb_upper": _tail(bands["upper"], 180),
            "bb_lower": _tail(bands["lower"], 180),
            "vwap": _tail(vwap_series, 180),
            "rsi": _tail(rsi_series, 180),
            "macd": _tail(macd_data["macd"], 180),
            "macd_signal": _tail(macd_data["signal"], 180),
            "macd_histogram": _tail(macd_data["histogram"], 180),
            "volume": [round(v, 4) for v in volumes[-180:]],
        },
    }


# -- small helpers ----------------------------------------------------------
def _last(series: Sequence[Number]) -> Number:
    return _r(next((v for v in reversed(series) if v is not None), None))


def _tail(series: Sequence[Number], count: int) -> list[Number]:
    return [_r(v) for v in series[-count:]]


def _r(value: Number, digits: int = 6) -> Number:
    return None if value is None else round(float(value), digits)


def _pct(value: Number, reference: float) -> Number:
    if value is None or not reference:
        return None
    return round(value / reference * 100.0, 4)


def _pct_diff(value: float, reference: Number) -> Number:
    if reference in (None, 0):
        return None
    return round((value / reference - 1.0) * 100.0, 4)  # type: ignore[operator]


def _alignment(fast: Number, mid: Number, slow: Number) -> str | None:
    """Human-readable EMA stack: bullish, bearish or mixed."""
    if None in (fast, mid, slow):
        return None
    if fast > mid > slow:      # type: ignore[operator]
        return "bullish"
    if fast < mid < slow:      # type: ignore[operator]
        return "bearish"
    return "mixed"
