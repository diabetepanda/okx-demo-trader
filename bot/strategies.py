"""Pure-function strategy evaluators for S2, S3, L1, L2, L3, and L4."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from bot.candles import Candle, count_bars_held, is_utc_sunday_candle
from bot.indicators import (
    channel_high,
    channel_low,
    highest_close,
    sma,
    vol_risk_factor,
    wilder_atr,
    wilder_rsi,
)


@dataclass(frozen=True)
class OpenPositionView:
    inst_id: str
    pos_side: str  # "long" or "short"
    entry_ts: int  # ms UTC
    stop_loss: float


@dataclass(frozen=True)
class StrategyDecision:
    strategy: str
    inst_id: str
    action: str  # "ENTER", "EXIT", "HOLD", "UPDATE_SL", "NONE"
    pos_side: str  # "long" or "short" or ""
    stop_loss: Optional[float] = None
    risk_factor: float = 1.0
    bars_held: int = 0
    bar_ts: int = 0
    reason: str = ""


def _ohlc(candles: list[Candle]) -> tuple[list[float], list[float], list[float]]:
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    return highs, lows, closes


def evaluate_s2(
    inst_id: str,
    candles: list[Candle],
    position: Optional[OpenPositionView],
    params: Optional[dict[str, Any]] = None,
) -> StrategyDecision:
    """S2: 4H Breakout (long & short)."""
    cfg = params or {}
    entry_lb = int(cfg.get("entry_lookback", 30))
    exit_lb = int(cfg.get("exit_lookback", 10))
    atr_p = int(cfg.get("atr_period", 14))
    atr_mult = float(cfg.get("atr_mult", 2.5))
    max_hold = int(cfg.get("max_hold_bars", 30))

    min_req = max(entry_lb + 1, atr_p + 1)
    if len(candles) < min_req:
        return StrategyDecision("S2", inst_id, "NONE", "", reason="4H K 線根數不足")

    highs, lows, closes = _ohlc(candles)
    atrs = wilder_atr(highs, lows, closes, atr_p)
    en_highs = channel_high(highs, entry_lb)
    en_lows = channel_low(lows, entry_lb)
    ex_highs = channel_high(highs, exit_lb)
    ex_lows = channel_low(lows, exit_lb)

    i = len(candles) - 1
    bar_ts = candles[i].ts
    close = closes[i]
    atr_val = atrs[i]

    if position is not None:
        held = count_bars_held(candles, position.entry_ts)
        if held >= max_hold:
            return StrategyDecision(
                "S2", inst_id, "EXIT", position.pos_side, bars_held=held, bar_ts=bar_ts,
                reason=f"持有達 {held} 根 4H K 線（上限 {max_hold} 根），時間出場",
            )
        if position.pos_side == "long" and ex_lows[i] is not None and close < ex_lows[i]:
            return StrategyDecision(
                "S2", inst_id, "EXIT", "long", bars_held=held, bar_ts=bar_ts,
                reason=f"收盤 {close:.6g} 跌破前 {exit_lb} 根最低點 {ex_lows[i]:.6g}，移動出場",
            )
        if position.pos_side == "short" and ex_highs[i] is not None and close > ex_highs[i]:
            return StrategyDecision(
                "S2", inst_id, "EXIT", "short", bars_held=held, bar_ts=bar_ts,
                reason=f"收盤 {close:.6g} 突破前 {exit_lb} 根最高點 {ex_highs[i]:.6g}，移動出場",
            )
        return StrategyDecision("S2", inst_id, "HOLD", position.pos_side, bars_held=held, bar_ts=bar_ts)

    if atr_val is None or atr_val <= 0:
        return StrategyDecision("S2", inst_id, "NONE", "", bar_ts=bar_ts, reason="ATR 尚未就緒")

    if en_highs[i] is not None and close > en_highs[i]:
        sl = close - atr_mult * atr_val
        if sl <= 0:
            return StrategyDecision("S2", inst_id, "NONE", "", bar_ts=bar_ts, reason="計算止損 <= 0")
        return StrategyDecision(
            "S2", inst_id, "ENTER", "long", stop_loss=sl, risk_factor=1.0, bar_ts=bar_ts,
            reason=f"4H 收盤 {close:.6g} 突破前 {entry_lb} 根高點 {en_highs[i]:.6g}，做多進場",
        )

    if en_lows[i] is not None and close < en_lows[i]:
        sl = close + atr_mult * atr_val
        return StrategyDecision(
            "S2", inst_id, "ENTER", "short", stop_loss=sl, risk_factor=1.0, bar_ts=bar_ts,
            reason=f"4H 收盤 {close:.6g} 跌破前 {entry_lb} 根低點 {en_lows[i]:.6g}，做空進場",
        )

    return StrategyDecision("S2", inst_id, "NONE", "", bar_ts=bar_ts)


def evaluate_s3(
    inst_id: str,
    candles: list[Candle],
    position: Optional[OpenPositionView],
    params: Optional[dict[str, Any]] = None,
) -> StrategyDecision:
    """S3: Daily RSI Pullback (long only)."""
    cfg = params or {}
    sma_trend_p = int(cfg.get("sma_trend", 200))
    rsi_p = int(cfg.get("rsi_period", 3))
    rsi_entry = float(cfg.get("rsi_entry", 10.0))
    sma_exit_p = int(cfg.get("sma_exit", 5))
    atr_p = int(cfg.get("atr_period", 14))
    atr_mult = float(cfg.get("atr_mult", 2.5))
    max_hold = int(cfg.get("max_hold_bars", 10))

    min_req = max(sma_trend_p, atr_p + 1, rsi_p + 1, sma_exit_p)
    if len(candles) < min_req:
        return StrategyDecision("S3", inst_id, "NONE", "", reason="日線 K 線根數不足")

    highs, lows, closes = _ohlc(candles)
    atrs = wilder_atr(highs, lows, closes, atr_p)
    sma_200 = sma(closes, sma_trend_p)
    sma_5 = sma(closes, sma_exit_p)
    rsis = wilder_rsi(closes, rsi_p)

    i = len(candles) - 1
    bar_ts = candles[i].ts
    close = closes[i]

    if position is not None:
        held = count_bars_held(candles, position.entry_ts)
        if held >= max_hold:
            return StrategyDecision(
                "S3", inst_id, "EXIT", "long", bars_held=held, bar_ts=bar_ts,
                reason=f"持有達 {held} 天（上限 {max_hold} 天），時間出場",
            )
        if sma_5[i] is not None and close > sma_5[i]:
            return StrategyDecision(
                "S3", inst_id, "EXIT", "long", bars_held=held, bar_ts=bar_ts,
                reason=f"日線收盤 {close:.6g} 站上 SMA{sma_exit_p} ({sma_5[i]:.6g})，均線出場",
            )
        return StrategyDecision("S3", inst_id, "HOLD", "long", bars_held=held, bar_ts=bar_ts)

    atr_val = atrs[i]
    if atr_val is None or atr_val <= 0 or sma_200[i] is None or rsis[i] is None:
        return StrategyDecision("S3", inst_id, "NONE", "", bar_ts=bar_ts, reason="指標尚未就緒")

    if close > sma_200[i] and rsis[i] < rsi_entry:
        sl = close - atr_mult * atr_val
        if sl <= 0:
            return StrategyDecision("S3", inst_id, "NONE", "", bar_ts=bar_ts, reason="計算止損 <= 0")
        return StrategyDecision(
            "S3", inst_id, "ENTER", "long", stop_loss=sl, risk_factor=1.0, bar_ts=bar_ts,
            reason=f"收盤 {close:.6g} > SMA{sma_trend_p} ({sma_200[i]:.6g}) 且 RSI({rsi_p})={rsis[i]:.2f} < {rsi_entry}，做多進場",
        )

    return StrategyDecision("S3", inst_id, "NONE", "", bar_ts=bar_ts)


def _evaluate_donchian_trend(
    strategy_name: str,
    inst_id: str,
    candles: list[Candle],
    position: Optional[OpenPositionView],
    entry_lb: int,
    exit_lb: int,
    atr_p: int,
    atr_mult: float,
    vol_target: float,
) -> StrategyDecision:
    min_req = max(entry_lb + 1, atr_p + 1)
    if len(candles) < min_req:
        return StrategyDecision(strategy_name, inst_id, "NONE", "", reason="日線 K 線根數不足")

    highs, lows, closes = _ohlc(candles)
    atrs = wilder_atr(highs, lows, closes, atr_p)
    en_highs = channel_high(highs, entry_lb)
    en_lows = channel_low(lows, entry_lb)
    ex_highs = channel_high(highs, exit_lb)
    ex_lows = channel_low(lows, exit_lb)

    i = len(candles) - 1
    bar_ts = candles[i].ts
    close = closes[i]
    atr_val = atrs[i]

    if position is not None:
        held = count_bars_held(candles, position.entry_ts)
        if position.pos_side == "long" and ex_lows[i] is not None and close < ex_lows[i]:
            return StrategyDecision(
                strategy_name, inst_id, "EXIT", "long", bars_held=held, bar_ts=bar_ts,
                reason=f"收盤 {close:.6g} 跌破前 {exit_lb} 日最低點 {ex_lows[i]:.6g}，出場",
            )
        if position.pos_side == "short" and ex_highs[i] is not None and close > ex_highs[i]:
            return StrategyDecision(
                strategy_name, inst_id, "EXIT", "short", bars_held=held, bar_ts=bar_ts,
                reason=f"收盤 {close:.6g} 突破前 {exit_lb} 日最高點 {ex_highs[i]:.6g}，出場",
            )
        return StrategyDecision(strategy_name, inst_id, "HOLD", position.pos_side, bars_held=held, bar_ts=bar_ts)

    if atr_val is None or atr_val <= 0:
        return StrategyDecision(strategy_name, inst_id, "NONE", "", bar_ts=bar_ts, reason="ATR 尚未就緒")

    rf = vol_risk_factor(atr_val, close, vol_target)

    if en_highs[i] is not None and close > en_highs[i]:
        sl = close - atr_mult * atr_val
        if sl <= 0:
            return StrategyDecision(strategy_name, inst_id, "NONE", "", bar_ts=bar_ts, reason="計算止損 <= 0")
        return StrategyDecision(
            strategy_name, inst_id, "ENTER", "long", stop_loss=sl, risk_factor=rf, bar_ts=bar_ts,
            reason=f"日線收盤 {close:.6g} 突破前 {entry_lb} 日高點 {en_highs[i]:.6g}，做多進場",
        )

    if en_lows[i] is not None and close < en_lows[i]:
        sl = close + atr_mult * atr_val
        return StrategyDecision(
            strategy_name, inst_id, "ENTER", "short", stop_loss=sl, risk_factor=rf, bar_ts=bar_ts,
            reason=f"日線收盤 {close:.6g} 跌破前 {entry_lb} 日低點 {en_lows[i]:.6g}，做空進場",
        )

    return StrategyDecision(strategy_name, inst_id, "NONE", "", bar_ts=bar_ts)


def evaluate_l1(
    inst_id: str,
    candles: list[Candle],
    position: Optional[OpenPositionView],
    params: Optional[dict[str, Any]] = None,
) -> StrategyDecision:
    """L1: Volatility-Adjusted Trend Following (daily, long & short)."""
    cfg = params or {}
    return _evaluate_donchian_trend(
        "L1",
        inst_id,
        candles,
        position,
        entry_lb=int(cfg.get("entry_lookback", 55)),
        exit_lb=int(cfg.get("exit_lookback", 10)),
        atr_p=int(cfg.get("atr_period", 14)),
        atr_mult=float(cfg.get("atr_mult", 3.0)),
        vol_target=float(cfg.get("vol_target", 0.012)),
    )


def evaluate_l2(
    inst_id: str,
    candles: list[Candle],
    position: Optional[OpenPositionView],
    params: Optional[dict[str, Any]] = None,
) -> StrategyDecision:
    """L2: Curated Trend Following (daily, long & short, fixed 10 symbols)."""
    cfg = params or {}
    return _evaluate_donchian_trend(
        "L2",
        inst_id,
        candles,
        position,
        entry_lb=int(cfg.get("entry_lookback", 55)),
        exit_lb=int(cfg.get("exit_lookback", 20)),
        atr_p=int(cfg.get("atr_period", 14)),
        atr_mult=float(cfg.get("atr_mult", 3.0)),
        vol_target=float(cfg.get("vol_target", 0.015)),
    )


def evaluate_l3_portfolio(
    candles_by_inst: dict[str, list[Candle]],
    open_positions: dict[str, OpenPositionView],
    params: Optional[dict[str, Any]] = None,
) -> list[StrategyDecision]:
    """L3: Dual Momentum (daily, weekly Sunday rebalance or when 0 positions, long only)."""
    cfg = params or {}
    lookback = int(cfg.get("lookback", 20))
    top_n = int(cfg.get("top_n", 3))
    atr_p = int(cfg.get("atr_period", 14))
    atr_mult = float(cfg.get("atr_mult", 3.0))
    vol_target = float(cfg.get("vol_target", 0.015))

    # Determine latest daily candle across available instruments
    latest_candles = [cs[-1] for cs in candles_by_inst.values() if cs]
    if not latest_candles:
        return []
    ref_latest = max(latest_candles, key=lambda c: c.ts)
    is_sunday = is_utc_sunday_candle(ref_latest)
    is_rebalance = is_sunday or (len(open_positions) == 0)

    decisions: list[StrategyDecision] = []

    if not is_rebalance:
        for inst_id, pos in open_positions.items():
            cs = candles_by_inst.get(inst_id, [])
            held = count_bars_held(cs, pos.entry_ts)
            bar_ts = cs[-1].ts if cs else ref_latest.ts
            decisions.append(
                StrategyDecision("L3", inst_id, "HOLD", "long", bars_held=held, bar_ts=bar_ts)
            )
        return decisions

    ranked: list[tuple[float, str, float, float, int]] = []
    for inst_id, cs in candles_by_inst.items():
        if len(cs) < max(lookback + 1, atr_p + 1):
            continue
        highs, lows, closes = _ohlc(cs)
        atrs = wilder_atr(highs, lows, closes, atr_p)
        atr_val = atrs[-1]
        if atr_val is None or atr_val <= 0:
            continue
        prev_close = closes[-1 - lookback]
        if prev_close <= 0:
            continue
        ret = (closes[-1] / prev_close) - 1.0
        if ret > 0:
            ranked.append((ret, inst_id, closes[-1], atr_val, cs[-1].ts))

    # Sort descending by return, tie-break ascending by inst_id
    ranked.sort(key=lambda x: (-x[0], x[1]))
    selected = ranked[:top_n]
    selected_ids = {item[1] for item in selected}

    # Held but not selected -> EXIT
    for inst_id, pos in open_positions.items():
        cs = candles_by_inst.get(inst_id, [])
        held = count_bars_held(cs, pos.entry_ts)
        bar_ts = cs[-1].ts if cs else ref_latest.ts
        if inst_id not in selected_ids:
            decisions.append(
                StrategyDecision(
                    "L3", inst_id, "EXIT", "long", bars_held=held, bar_ts=bar_ts,
                    reason="週再平衡未入選前 3 名正動能，出場",
                )
            )
        else:
            decisions.append(
                StrategyDecision("L3", inst_id, "HOLD", "long", bars_held=held, bar_ts=bar_ts)
            )

    # Selected but not held -> ENTER
    for ret, inst_id, close, atr_val, bar_ts in selected:
        if inst_id in open_positions:
            continue
        sl = close - atr_mult * atr_val
        if sl <= 0:
            continue
        rf = vol_risk_factor(atr_val, close, vol_target)
        decisions.append(
            StrategyDecision(
                "L3", inst_id, "ENTER", "long", stop_loss=sl, risk_factor=rf, bar_ts=bar_ts,
                reason=f"L3 雙動能入選前 {top_n} 名（20 日報酬 {ret * 100:.2f}%），做多進場",
            )
        )

    return decisions


def evaluate_l4(
    inst_id: str,
    candles: list[Candle],
    position: Optional[OpenPositionView],
    params: Optional[dict[str, Any]] = None,
) -> StrategyDecision:
    """L4: Major Weekly Trend (daily evaluation, Sunday weekly close entry/exit, trailing SL)."""
    cfg = params or {}
    sma_p = int(cfg.get("sma_trend", 100))
    atr_p = int(cfg.get("atr_period", 20))
    atr_mult = float(cfg.get("atr_mult", 5.0))
    hc_lb = int(cfg.get("highest_close_lookback", 60))

    min_req = max(sma_p, atr_p + 1, hc_lb)
    if len(candles) < min_req:
        return StrategyDecision("L4", inst_id, "NONE", "", reason="日線 K 線根數不足")

    highs, lows, closes = _ohlc(candles)
    smas = sma(closes, sma_p)
    atrs = wilder_atr(highs, lows, closes, atr_p)
    hcs = highest_close(closes, hc_lb)

    i = len(candles) - 1
    latest = candles[i]
    bar_ts = latest.ts
    close = closes[i]
    sma_val = smas[i]
    atr_val = atrs[i]
    hc_val = hcs[i]
    is_sunday = is_utc_sunday_candle(latest)

    if position is not None:
        held = count_bars_held(candles, position.entry_ts)
        if is_sunday and sma_val is not None and close < sma_val:
            return StrategyDecision(
                "L4", inst_id, "EXIT", "long", bars_held=held, bar_ts=bar_ts,
                reason=f"週日收盤 {close:.6g} 跌破 SMA{sma_p} ({sma_val:.6g})，週線出場",
            )
        if atr_val is not None and atr_val > 0 and hc_val is not None:
            candidate_sl = hc_val - atr_mult * atr_val
            if candidate_sl > position.stop_loss:
                return StrategyDecision(
                    "L4", inst_id, "UPDATE_SL", "long", stop_loss=candidate_sl,
                    bars_held=held, bar_ts=bar_ts,
                    reason=f"上調移動停損至 {candidate_sl:.6g}（近 {hc_lb} 日最高收盤 {hc_val:.6g} - {atr_mult}×ATR）",
                )
        return StrategyDecision("L4", inst_id, "HOLD", "long", bars_held=held, bar_ts=bar_ts)

    if not is_sunday:
        return StrategyDecision("L4", inst_id, "NONE", "", bar_ts=bar_ts, reason="非週日收盤，L4 不進場")

    if sma_val is None or atr_val is None or atr_val <= 0:
        return StrategyDecision("L4", inst_id, "NONE", "", bar_ts=bar_ts, reason="指標尚未就緒")

    if close > sma_val:
        sl = close - atr_mult * atr_val
        if sl <= 0:
            return StrategyDecision("L4", inst_id, "NONE", "", bar_ts=bar_ts, reason="計算止損 <= 0，不進場")
        return StrategyDecision(
            "L4", inst_id, "ENTER", "long", stop_loss=sl, risk_factor=1.0, bar_ts=bar_ts,
            reason=f"週日收盤 {close:.6g} 站上 SMA{sma_p} ({sma_val:.6g})，做多進場",
        )

    return StrategyDecision("L4", inst_id, "NONE", "", bar_ts=bar_ts)
