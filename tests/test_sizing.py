from __future__ import annotations

import pytest

from bot.sizing import ExistingPositionRisk, allocate_subaccount_position


def _meta(ct_val=0.01, lot_sz=1.0, min_sz=1.0):
    return {"ctVal": ct_val, "lotSz": lot_sz, "minSz": min_sz, "tickSz": 0.1}


def test_sizing_1pct_risk_and_lot_rounding():
    # Subaccount equity = 2000, 1% risk = 20 USDT
    # ref=50000, sl=49000 -> sl_dist=0.02, cost=0.0012 -> eff_sl=0.0212
    # notional = 20 / 0.0212 = 943.396 -> sz = 943.396 / (50000*0.01=500) = 1.88 -> floor to 1
    res = allocate_subaccount_position(
        strategy="L1",
        inst_id="BTC-USDT-SWAP",
        pos_side="long",
        ref_price=50000.0,
        stop_loss=49000.0,
        risk_factor=1.0,
        initial_sub_equity=2000.0,
        sub_realized_pnl=0.0,
        sub_positions=[],
        all_open_positions=[],
        initial_total_equity=10000.0,
        total_realized_pnl=0.0,
        inst_meta=_meta(ct_val=0.01, lot_sz=1.0, min_sz=1.0),
    )
    assert res.approved is True
    assert res.sz == 1.0
    assert res.leverage == 10


def test_sizing_rejects_max_positions():
    sub_pos = [
        ExistingPositionRisk("S2", f"COIN{i}-USDT-SWAP", "long", 100.0, 5.0)
        for i in range(10)
    ]
    res = allocate_subaccount_position(
        strategy="S2",
        inst_id="BTC-USDT-SWAP",
        pos_side="long",
        ref_price=50000.0,
        stop_loss=49000.0,
        risk_factor=1.0,
        initial_sub_equity=1000.0,
        sub_realized_pnl=0.0,
        sub_positions=sub_pos,
        all_open_positions=sub_pos,
        initial_total_equity=10000.0,
        total_realized_pnl=0.0,
        inst_meta=_meta(),
    )
    assert res.approved is False
    assert "上限 10 檔" in res.reason


def test_sizing_rejects_subaccount_10pct_risk_cap():
    sub_pos = [
        ExistingPositionRisk("S2", "ETH-USDT-SWAP", "long", 1000.0, 100.0)
    ]
    res = allocate_subaccount_position(
        strategy="S2",
        inst_id="BTC-USDT-SWAP",
        pos_side="long",
        ref_price=50000.0,
        stop_loss=49000.0,
        risk_factor=1.0,
        initial_sub_equity=1000.0,
        sub_realized_pnl=0.0,
        sub_positions=sub_pos,
        all_open_positions=sub_pos,
        initial_total_equity=10000.0,
        total_realized_pnl=0.0,
        inst_meta=_meta(),
    )
    assert res.approved is False
    assert "10% 上限" in res.reason


def test_sizing_notional_5x_and_leverage_caps():
    # Very tight SL (0.2% + 0.12% = 0.32%) -> raw notional would be 20 / 0.0032 = 6250
    # Capped by 5x sub_equity (2000 * 5 = 10000), or if existing notional = 9500 -> remaining = 500
    sub_pos = [
        ExistingPositionRisk("L1", "ETH-USDT-SWAP", "long", 9500.0, 20.0)
    ]
    res = allocate_subaccount_position(
        strategy="L1",
        inst_id="SOL-USDT-SWAP",
        pos_side="long",
        ref_price=100.0,
        stop_loss=99.0,
        risk_factor=1.0,
        initial_sub_equity=2000.0,
        sub_realized_pnl=0.0,
        sub_positions=sub_pos,
        all_open_positions=sub_pos,
        initial_total_equity=10000.0,
        total_realized_pnl=0.0,
        inst_meta=_meta(ct_val=1.0, lot_sz=1.0, min_sz=1.0),
    )
    assert res.approved is True
    assert res.leverage == 5
    assert res.notional <= 500.0 + 1e-6


def test_sizing_account_same_direction_20pct_cap():
    # Total equity = 10000 -> 20% same-dir cap = 2000
    all_pos = [
        ExistingPositionRisk("L2", "BTC-USDT-SWAP", "long", 5000.0, 2000.0)
    ]
    res = allocate_subaccount_position(
        strategy="L1",
        inst_id="ETH-USDT-SWAP",
        pos_side="long",
        ref_price=3000.0,
        stop_loss=2900.0,
        risk_factor=1.0,
        initial_sub_equity=2000.0,
        sub_realized_pnl=0.0,
        sub_positions=[],
        all_open_positions=all_pos,
        initial_total_equity=10000.0,
        total_realized_pnl=0.0,
        inst_meta=_meta(),
    )
    assert res.approved is False
    assert "20% 上限" in res.reason


def test_sizing_min_sz_and_wrong_side_rejection():
    res_min = allocate_subaccount_position(
        strategy="S2",
        inst_id="BTC-USDT-SWAP",
        pos_side="long",
        ref_price=50000.0,
        stop_loss=40000.0,
        risk_factor=1.0,
        initial_sub_equity=100.0,
        sub_realized_pnl=0.0,
        sub_positions=[],
        all_open_positions=[],
        initial_total_equity=1000.0,
        total_realized_pnl=0.0,
        inst_meta=_meta(ct_val=0.01, lot_sz=1.0, min_sz=1.0),
    )
    assert res_min.approved is False
    assert "最小下單量" in res_min.reason

    res_wrong = allocate_subaccount_position(
        strategy="S2",
        inst_id="BTC-USDT-SWAP",
        pos_side="long",
        ref_price=48000.0,
        stop_loss=49000.0,
        risk_factor=1.0,
        initial_sub_equity=2000.0,
        sub_realized_pnl=0.0,
        sub_positions=[],
        all_open_positions=[],
        initial_total_equity=10000.0,
        total_realized_pnl=0.0,
        inst_meta=_meta(),
    )
    assert res_wrong.approved is False
    assert "止損價" in res_wrong.reason
