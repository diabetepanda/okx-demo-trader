from __future__ import annotations

import pytest

from bot.indicators import (
    channel_high,
    channel_low,
    highest_close,
    sma,
    vol_risk_factor,
    wilder_atr,
    wilder_rsi,
)


def test_sma_hand_calculated():
    vals = [10.0, 12.0, 14.0, 16.0, 18.0]
    res = sma(vals, 3)
    assert res[:2] == [None, None]
    assert res[2] == pytest.approx(12.0)
    assert res[3] == pytest.approx(14.0)
    assert res[4] == pytest.approx(16.0)


def test_wilder_atr_hand_calculated():
    highs = [10.0, 12.0, 13.0, 15.0, 14.0]
    lows = [8.0, 9.0, 10.0, 11.0, 10.0]
    closes = [9.0, 11.0, 12.0, 13.0, 12.0]
    # TR[1] = max(12-9, |12-9|, |9-9|) = 3
    # TR[2] = max(13-10, |13-11|, |10-11|) = 3
    # TR[3] = max(15-11, |15-12|, |11-12|) = 4
    # TR[4] = max(14-10, |14-13|, |10-13|) = 4
    # First ATR(2) at idx 2 = (3 + 3)/2 = 3.0
    # ATR[3] = (3.0*1 + 4)/2 = 3.5
    # ATR[4] = (3.5*1 + 4)/2 = 3.75
    res = wilder_atr(highs, lows, closes, period=2)
    assert res[:2] == [None, None]
    assert res[2] == pytest.approx(3.0)
    assert res[3] == pytest.approx(3.5)
    assert res[4] == pytest.approx(3.75)


def test_wilder_rsi_hand_calculated():
    closes = [10.0, 12.0, 11.0, 13.0, 13.0]
    # gains: [0, 2, 0, 2, 0], losses: [0, 0, 1, 0, 0]
    # period=2 -> idx 2: avg_gain=(2+0)/2=1.0, avg_loss=(0+1)/2=0.5 -> RS=2 -> RSI=66.6667
    # idx 3: avg_gain=(1*1+2)/2=1.5, avg_loss=(0.5*1+0)/2=0.25 -> RS=6 -> RSI=85.7142857
    res = wilder_rsi(closes, period=2)
    assert res[:2] == [None, None]
    assert res[2] == pytest.approx(100.0 - 100.0 / 3.0)
    assert res[3] == pytest.approx(100.0 - 100.0 / 7.0)

    # Edge case: zero losses with positive gain -> RSI = 100
    all_up = [10.0, 11.0, 12.0, 13.0]
    assert wilder_rsi(all_up, period=2)[2] == pytest.approx(100.0)


def test_channels_and_highest_close():
    highs = [10.0, 15.0, 12.0, 18.0, 14.0]
    lows = [5.0, 3.0, 6.0, 4.0, 7.0]
    closes = [8.0, 14.0, 11.0, 16.0, 13.0]

    ch = channel_high(highs, 2)
    cl = channel_low(lows, 2)
    hc = highest_close(closes, 3)

    # Excluding current bar
    assert ch[:2] == [None, None]
    assert ch[2] == pytest.approx(15.0)
    assert ch[3] == pytest.approx(15.0)
    assert ch[4] == pytest.approx(18.0)

    assert cl[2] == pytest.approx(3.0)
    assert cl[3] == pytest.approx(3.0)
    assert cl[4] == pytest.approx(4.0)

    # Inclusive of current bar
    assert hc[:2] == [None, None]
    assert hc[2] == pytest.approx(14.0)
    assert hc[3] == pytest.approx(16.0)
    assert hc[4] == pytest.approx(16.0)


def test_vol_risk_factor():
    # atr_pct = 2 / 100 = 0.02, vol_target = 0.01 -> 0.5
    assert vol_risk_factor(2.0, 100.0, 0.01) == pytest.approx(0.5)
    # Clamped to [0.2, 1.0]
    assert vol_risk_factor(20.0, 100.0, 0.01) == pytest.approx(0.2)
    assert vol_risk_factor(0.1, 100.0, 0.01) == pytest.approx(1.0)
    assert vol_risk_factor(0.0, 100.0, 0.01) == pytest.approx(1.0)
