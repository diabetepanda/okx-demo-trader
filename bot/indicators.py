"""Pure-Python technical indicators matching Signal Desk definitions."""

from __future__ import annotations

from typing import Optional, Sequence


def wilder_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[Optional[float]]:
    """Compute Wilder's ATR. First valid value is at index `period`."""
    n = len(closes)
    result: list[Optional[float]] = [None] * n
    if period <= 0 or n <= period or len(highs) != n or len(lows) != n:
        return result

    trs: list[float] = [0.0] * n
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        trs[i] = max(hl, hc, lc)

    first_atr = sum(trs[1 : period + 1]) / period
    result[period] = first_atr

    prev_atr = first_atr
    for i in range(period + 1, n):
        curr_atr = (prev_atr * (period - 1) + trs[i]) / period
        result[i] = curr_atr
        prev_atr = curr_atr

    return result


def sma(values: Sequence[float], period: int) -> list[Optional[float]]:
    """Compute Simple Moving Average (inclusive of current bar)."""
    n = len(values)
    result: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return result

    window_sum = sum(values[:period])
    result[period - 1] = window_sum / period
    for i in range(period, n):
        window_sum += values[i] - values[i - period]
        result[i] = window_sum / period

    return result


def wilder_rsi(closes: Sequence[float], period: int = 14) -> list[Optional[float]]:
    """Compute Wilder's RSI. First valid value is at index `period`."""
    n = len(closes)
    result: list[Optional[float]] = [None] * n
    if period <= 0 or n <= period:
        return result

    gains: list[float] = [0.0] * n
    losses: list[float] = [0.0] * n
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains[i] = diff
        elif diff < 0:
            losses[i] = -diff

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period

    def _calc_rsi(ag: float, al: float) -> float:
        if al <= 1e-12:
            return 100.0 if ag > 0 else 50.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = _calc_rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i] = _calc_rsi(avg_gain, avg_loss)

    return result


def channel_high(highs: Sequence[float], period: int) -> list[Optional[float]]:
    """Highest high of prior `period` bars (excluding current bar i)."""
    n = len(highs)
    result: list[Optional[float]] = [None] * n
    if period <= 0 or n <= period:
        return result
    for i in range(period, n):
        result[i] = max(highs[i - period : i])
    return result


def channel_low(lows: Sequence[float], period: int) -> list[Optional[float]]:
    """Lowest low of prior `period` bars (excluding current bar i)."""
    n = len(lows)
    result: list[Optional[float]] = [None] * n
    if period <= 0 or n <= period:
        return result
    for i in range(period, n):
        result[i] = min(lows[i - period : i])
    return result


def highest_close(closes: Sequence[float], period: int) -> list[Optional[float]]:
    """Highest close of last `period` bars (inclusive of current bar i)."""
    n = len(closes)
    result: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return result
    for i in range(period - 1, n):
        result[i] = max(closes[i - period + 1 : i + 1])
    return result


def vol_risk_factor(
    atr_val: Optional[float],
    close_val: float,
    vol_target: Optional[float],
) -> float:
    """Volatility risk factor: min(1, max(0.2, vol_target / (ATR / close)))."""
    if (
        atr_val is None
        or atr_val <= 0
        or close_val <= 0
        or vol_target is None
        or vol_target <= 0
    ):
        return 1.0
    atr_pct = atr_val / close_val
    return min(1.0, max(0.2, vol_target / atr_pct))
