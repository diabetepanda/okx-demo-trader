from __future__ import annotations

from datetime import datetime, timezone

from bot.candles import Candle
from bot.strategies import (
    OpenPositionView,
    evaluate_l1,
    evaluate_l2,
    evaluate_l3_portfolio,
    evaluate_l4,
    evaluate_s2,
    evaluate_s3,
)

DAY_MS = 86_400_000
H4_MS = 14_400_000


def make_candles(closes: list[float], start_ts: int, step_ms: int, spread: float = 1.0) -> list[Candle]:
    return [
        Candle(
            ts=start_ts + idx * step_ms,
            open=c,
            high=c + spread,
            low=c - spread,
            close=c,
            vol=100.0,
            confirm="1",
        )
        for idx, c in enumerate(closes)
    ]


def test_s2_long_short_and_exits():
    base_ts = 1_700_000_000_000
    # Flat 40 bars at 100, then breakout to 110
    cs = make_candles([100.0] * 40 + [110.0], base_ts, H4_MS, spread=1.0)
    dec = evaluate_s2("BTC-USDT-SWAP", cs, None)
    assert dec.action == "ENTER"
    assert dec.pos_side == "long"
    assert dec.stop_loss is not None and dec.stop_loss < 110.0

    # No breakout
    cs_flat = make_candles([100.0] * 41, base_ts, H4_MS, spread=1.0)
    assert evaluate_s2("BTC-USDT-SWAP", cs_flat, None).action == "NONE"

    # Short breakout
    cs_short = make_candles([100.0] * 40 + [90.0], base_ts, H4_MS, spread=1.0)
    dec_short = evaluate_s2("BTC-USDT-SWAP", cs_short, None)
    assert dec_short.action == "ENTER"
    assert dec_short.pos_side == "short"

    # Channel exit for long (close < prior 10 low)
    cs_exit = make_candles([100.0] * 40 + [95.0], base_ts, H4_MS, spread=1.0)
    pos = OpenPositionView("BTC-USDT-SWAP", "long", entry_ts=base_ts + 35 * H4_MS, stop_loss=90.0)
    assert evaluate_s2("BTC-USDT-SWAP", cs_exit, pos).action == "EXIT"

    # Time exit (>= 30 bars held)
    cs_time = make_candles([105.0] * 40, base_ts, H4_MS, spread=1.0)
    pos_old = OpenPositionView("BTC-USDT-SWAP", "long", entry_ts=base_ts + 5 * H4_MS, stop_loss=90.0)
    dec_time = evaluate_s2("BTC-USDT-SWAP", cs_time, pos_old)
    assert dec_time.action == "EXIT"
    assert dec_time.bars_held >= 30


def test_s3_rsi_pullback_and_exits():
    base_ts = 1_700_000_000_000
    # 210 bars rising to stay above SMA200, then sharp 3-bar pullback so RSI(3) < 10 but close still > SMA200
    closes = [100.0 + i * 0.5 for i in range(210)] + [200.0, 195.0, 190.0]
    cs = make_candles(closes, base_ts, DAY_MS, spread=1.0)
    dec = evaluate_s3("ETH-USDT-SWAP", cs, None)
    assert dec.action == "ENTER"
    assert dec.pos_side == "long"

    # No entry when rising (RSI high)
    cs_up = make_candles([100.0 + i * 0.5 for i in range(215)], base_ts, DAY_MS, spread=1.0)
    assert evaluate_s3("ETH-USDT-SWAP", cs_up, None).action == "NONE"

    # SMA5 exit
    pos = OpenPositionView("ETH-USDT-SWAP", "long", entry_ts=cs_up[-5].ts, stop_loss=150.0)
    assert evaluate_s3("ETH-USDT-SWAP", cs_up, pos).action == "EXIT"

    # Time exit (10 days)
    pos_old = OpenPositionView("ETH-USDT-SWAP", "long", entry_ts=cs[-15].ts, stop_loss=100.0)
    assert evaluate_s3("ETH-USDT-SWAP", cs, pos_old).action == "EXIT"


def test_l1_and_l2_breakout_and_exit():
    base_ts = 1_700_000_000_000
    cs_enter = make_candles([100.0] * 60 + [115.0], base_ts, DAY_MS, spread=1.0)
    assert evaluate_l1("SOL-USDT-SWAP", cs_enter, None).action == "ENTER"
    assert evaluate_l2("BTC-USDT-SWAP", cs_enter, None).action == "ENTER"

    cs_flat = make_candles([100.0] * 61, base_ts, DAY_MS, spread=1.0)
    assert evaluate_l1("SOL-USDT-SWAP", cs_flat, None).action == "NONE"
    assert evaluate_l2("BTC-USDT-SWAP", cs_flat, None).action == "NONE"

    cs_exit = make_candles([100.0] * 60 + [90.0], base_ts, DAY_MS, spread=1.0)
    pos = OpenPositionView("SOL-USDT-SWAP", "long", entry_ts=base_ts + 40 * DAY_MS, stop_loss=80.0)
    assert evaluate_l1("SOL-USDT-SWAP", cs_exit, pos).action == "EXIT"
    assert evaluate_l2("BTC-USDT-SWAP", cs_exit, pos).action == "EXIT"


def test_l3_rebalance_top3_positive_only():
    # 2026-11-22 is UTC Sunday
    sun_ts = int(datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    start_ts = sun_ts - 29 * DAY_MS

    data = {
        "AAA-USDT-SWAP": make_candles([100.0] * 29 + [130.0], start_ts, DAY_MS),
        "BBB-USDT-SWAP": make_candles([100.0] * 29 + [120.0], start_ts, DAY_MS),
        "CCC-USDT-SWAP": make_candles([100.0] * 29 + [110.0], start_ts, DAY_MS),
        "DDD-USDT-SWAP": make_candles([100.0] * 29 + [105.0], start_ts, DAY_MS),
        "EEE-USDT-SWAP": make_candles([100.0] * 29 + [90.0], start_ts, DAY_MS),
    }
    held = {
        "EEE-USDT-SWAP": OpenPositionView("EEE-USDT-SWAP", "long", entry_ts=start_ts, stop_loss=80.0)
    }
    decs = evaluate_l3_portfolio(data, held)
    enters = {d.inst_id for d in decs if d.action == "ENTER"}
    exits = {d.inst_id for d in decs if d.action == "EXIT"}
    assert enters == {"AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP"}
    assert exits == {"EEE-USDT-SWAP"}

    # Non-Sunday with existing position -> HOLD only
    mon_ts = int(datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    mon_start = mon_ts - 29 * DAY_MS
    mon_data = {
        "AAA-USDT-SWAP": make_candles([100.0] * 29 + [130.0], mon_start, DAY_MS),
        "EEE-USDT-SWAP": make_candles([100.0] * 29 + [90.0], mon_start, DAY_MS),
    }
    decs_mon = evaluate_l3_portfolio(mon_data, held)
    assert len(decs_mon) == 1
    assert decs_mon[0].action == "HOLD"


def test_l4_sunday_only_and_trailing_sl_never_lowers():
    sun_ts = int(datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    mon_ts = int(datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    # On Monday, even above SMA100 -> NONE
    cs_mon = make_candles([100.0] * 105 + [150.0], mon_ts - 105 * DAY_MS, DAY_MS, spread=1.0)
    assert evaluate_l4("BTC-USDT-SWAP", cs_mon, None).action == "NONE"

    # On Sunday above SMA100 -> ENTER
    cs_sun = make_candles([100.0] * 105 + [150.0], sun_ts - 105 * DAY_MS, DAY_MS, spread=1.0)
    dec_enter = evaluate_l4("BTC-USDT-SWAP", cs_sun, None)
    assert dec_enter.action == "ENTER"

    # Trailing SL moves UP when price rises, does NOT move down if current SL is higher
    pos_low_sl = OpenPositionView("BTC-USDT-SWAP", "long", entry_ts=mon_ts - 20 * DAY_MS, stop_loss=50.0)
    dec_up = evaluate_l4("BTC-USDT-SWAP", cs_mon, pos_low_sl)
    assert dec_up.action == "UPDATE_SL"
    assert dec_up.stop_loss is not None and dec_up.stop_loss > 50.0

    pos_high_sl = OpenPositionView("BTC-USDT-SWAP", "long", entry_ts=mon_ts - 20 * DAY_MS, stop_loss=148.0)
    dec_hold = evaluate_l4("BTC-USDT-SWAP", cs_mon, pos_high_sl)
    assert dec_hold.action == "HOLD"

    # Sunday close < SMA100 -> EXIT
    cs_sun_exit = make_candles([150.0] * 105 + [90.0], sun_ts - 105 * DAY_MS, DAY_MS, spread=1.0)
    pos_exit = OpenPositionView("BTC-USDT-SWAP", "long", entry_ts=sun_ts - 20 * DAY_MS, stop_loss=50.0)
    assert evaluate_l4("BTC-USDT-SWAP", cs_sun_exit, pos_exit).action == "EXIT"
