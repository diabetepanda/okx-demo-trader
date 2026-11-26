from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bot.config import AppConfig, ControlState
from bot.ledger import init_ledger, load_json, save_json
from bot.main import run_cycle
from bot.report import compute_twr_series
from bot.sizing import allocate_subaccount_position


def test_dynamic_sizing_uses_live_equity_without_touching_existing():
    # Live equity grew from 5,000 to 63,700 -> S2 (10%) = 6,370, 1% risk = 63.70
    live_eq = 63700.0
    sub_eq = live_eq * 0.10
    res = allocate_subaccount_position(
        strategy="S2",
        inst_id="BTC-USDT-SWAP",
        pos_side="long",
        ref_price=50000.0,
        stop_loss=49000.0,
        risk_factor=1.0,
        initial_sub_equity=sub_eq,
        sub_realized_pnl=0.0,
        sub_positions=[],
        all_open_positions=[],
        initial_total_equity=live_eq,
        total_realized_pnl=0.0,
        inst_meta={"ctVal": 0.01, "lotSz": 1.0, "minSz": 1.0, "tickSz": 0.1},
    )
    assert res.approved is True
    # notional ~ 63.70 / 0.0212 = 3004.7 -> sz = floor(3004.7 / 500) = 6 contracts
    assert res.sz == 6.0


def test_deposit_does_not_inflate_return_or_drawdown():
    eq_hist = [
        {"ts": 1000, "equity": 5000.0},
        {"ts": 2000, "equity": 63745.37},
        {"ts": 3000, "equity": 63745.37},
    ]
    cfs = [{"ts": 1500, "amount_usdt": 58745.37, "type": "deposit"}]
    series, ret_pct, mdd_pct = compute_twr_series(5000.0, eq_hist, cfs)
    assert ret_pct == pytest.approx(0.0, abs=0.01)
    assert mdd_pct == pytest.approx(0.0, abs=0.01)
    assert series[-1]["adjusted_equity"] == pytest.approx(5000.0, abs=0.5)


def test_withdrawal_does_not_count_as_loss_or_drawdown():
    eq_hist = [
        {"ts": 1000, "equity": 10000.0},
        {"ts": 2000, "equity": 5000.0},
        {"ts": 3000, "equity": 5500.0},  # +10% on remaining 5,000
    ]
    cfs = [{"ts": 1500, "amount_usdt": -5000.0, "type": "withdrawal"}]
    series, ret_pct, mdd_pct = compute_twr_series(10000.0, eq_hist, cfs)
    assert ret_pct == pytest.approx(10.0, abs=0.01)
    assert mdd_pct == pytest.approx(0.0, abs=0.01)


def test_run_cycle_detects_deposit_and_keeps_existing_positions(tmp_path: Path):
    state_dir = tmp_path / "state"
    ledger = init_ledger(5000.0)
    ledger["last_equity_snapshot"] = {
        "ts": 1000,
        "equity": 5000.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
    }
    ledger["positions"].append(
        {
            "id": "S2_XAG-USDT-SWAP_1000",
            "strategy": "S2",
            "inst_id": "XAG-USDT-SWAP",
            "pos_side": "short",
            "sz": 267.7,
            "entry_price": 40.45,
            "entry_ts": 1000,
            "entry_bar_ts": 1000,
            "stop_loss": 42.26,
            "initial_stop_loss": 42.26,
            "planned_risk": 5.0,
            "notional": 108.28,
            "ct_val": 0.01,
            "ord_id": "ORD1",
            "cl_ord_id": "CL1",
            "algo_id": "ALGO1",
            "algo_cl_ord_id": "SL1",
            "fee": 0.05,
        }
    )
    save_json(state_dir / "ledger.json", ledger)
    save_json(
        state_dir / "universe.json",
        {"date": "2026-11-26", "top_100": [], "l2": [], "l4": [], "instruments": {}},
    )

    mock_client = MagicMock()
    mock_client.get_balance.return_value = [
        {"details": [{"ccy": "USDT", "eq": "63706.50", "upl": "0.0"}]}
    ]
    mock_client.get_positions.return_value = [
        {"instId": "XAG-USDT-SWAP", "posSide": "short", "pos": "267.7"}
    ]
    mock_client.get_pending_algos.return_value = [
        {"algoId": "ALGO1", "algoClOrdId": "SL1", "sz": "267.7"}
    ]
    mock_client.get_fills.return_value = []
    mock_client.get_positions_history.return_value = []
    mock_client.get_tickers.return_value = []
    mock_client.get_bills.return_value = [
        {"ts": "2000", "balChg": "58706.50", "instId": "BTC-USDT"}
    ]

    cfg = AppConfig(
        base_dir=tmp_path,
        api_key="k",
        api_secret="s",
        api_passphrase="p",
        trading_mode="demo",
        dry_run=False,
        settings={},
        control=ControlState(paused=False, close_all=False),
    )

    run_cycle(cfg, client=mock_client, local_mode=False, now=datetime(2026, 11, 26, 5, 0, tzinfo=timezone.utc))
    updated = load_json(state_dir / "ledger.json")
    assert len(updated["cash_flows"]) == 1
    assert updated["cash_flows"][0]["amount_usdt"] == pytest.approx(58706.50)
    assert updated["cash_flows"][0]["source"] == "okx_bills"
    # Existing position size must remain untouched at 267.7
    assert updated["positions"][0]["sz"] == 267.7
