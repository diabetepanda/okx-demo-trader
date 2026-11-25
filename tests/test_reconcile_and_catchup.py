from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bot.config import AppConfig, ControlState
from bot.executor import execute_entry, reconcile_positions
from bot.ledger import init_ledger, load_json, save_json
from bot.main import _extract_usdt_equity, run_cycle


def test_reconcile_stop_loss_triggered():
    ledger = init_ledger(10000.0)
    ledger["positions"].append(
        {
            "id": "L1_BTC-USDT-SWAP_1700000000000",
            "strategy": "L1",
            "inst_id": "BTC-USDT-SWAP",
            "pos_side": "long",
            "sz": 2.0,
            "entry_price": 50000.0,
            "entry_ts": 1700000000000,
            "entry_bar_ts": 1700000000000,
            "stop_loss": 49000.0,
            "initial_stop_loss": 49000.0,
            "planned_risk": 20.0,
            "notional": 1000.0,
            "ct_val": 0.01,
            "ord_id": "12345",
            "cl_ord_id": "L1BTC1700000000000",
            "algo_id": "ALGO1",
            "algo_cl_ord_id": "SLL1BTC1700000000000",
            "fee": 0.5,
        }
    )

    mock_client = MagicMock()
    # Position & algo gone on OKX, plus an unrelated foreign ETH short position
    mock_client.get_positions.return_value = [
        {"instId": "ETH-USDT-SWAP", "posSide": "short", "pos": "5"}
    ]
    mock_client.get_pending_algos.return_value = []
    mock_client.get_fills.return_value = [
        {
            "instId": "BTC-USDT-SWAP",
            "side": "sell",
            "posSide": "long",
            "fillPx": "48950",
            "fee": "-0.49",
            "ts": "1700010000000",
        }
    ]
    mock_client.get_positions_history.return_value = []

    res = reconcile_positions(mock_client, ledger)
    assert res["reconciled_closed"] == 1
    assert len(ledger["positions"]) == 0
    assert len(ledger["closed_trades"]) == 1

    trade = ledger["closed_trades"][0]
    assert trade["exit_price"] == 48950.0
    # Gross = (48950 - 50000) * 2 * 0.01 = -21.0, fees = 0.5 + 0.49 = 0.99 -> -21.99
    assert trade["pnl_usdt"] == pytest.approx(-21.99)
    assert ledger["subaccounts"]["L1"]["realized_pnl"] == pytest.approx(-21.99)


def test_catchup_missed_4h_bars_and_clordid_dedup(tmp_path: Path):
    h4_ms = 14_400_000
    day_ms = 86_400_000
    base_4h = 1_700_000_000_000
    # 130 4H bars: flat 125 bars at 100, then breakout at bar 127, 128, 129, 130
    candles_4h = []
    for idx in range(130, 0, -1):
        px = 100.0 if idx < 126 else 110.0 + (idx - 126) * 3.0
        candles_4h.append([
            str(base_4h + idx * h4_ms),
            str(px), str(px + 1.0), str(px - 1.0), str(px),
            "100", "100", "100", "1",
        ])

    # 270 daily bars flat (no daily signals)
    base_1d = 1_690_000_000_000
    candles_1d = [
        [str(base_1d + idx * day_ms), "100", "101", "99", "100", "100", "100", "100", "1"]
        for idx in range(270, 0, -1)
    ]

    # Pre-seed ledger with last_processed_bar["4H"] 3 bars behind, plus an existing S2 open position
    state_dir = tmp_path / "state"
    ledger = init_ledger(10000.0)
    ledger["last_processed_bar"]["4H"] = base_4h + 127 * h4_ms
    ledger["last_processed_bar"]["1Dutc"] = base_1d + 270 * day_ms
    ledger["positions"].append(
        {
            "id": "S2_ETH-USDT-SWAP_1700000000000",
            "strategy": "S2",
            "inst_id": "ETH-USDT-SWAP",
            "pos_side": "long",
            "sz": 1.0,
            "entry_price": 100.0,
            "entry_ts": base_4h + 125 * h4_ms,
            "entry_bar_ts": base_4h + 125 * h4_ms,
            "stop_loss": 90.0,
            "initial_stop_loss": 90.0,
            "planned_risk": 10.0,
            "notional": 100.0,
            "ct_val": 1.0,
            "ord_id": "ORD_ETH",
            "cl_ord_id": "S2ETH1",
            "algo_id": "ALGO_ETH",
            "algo_cl_ord_id": "SLS2ETH1",
            "fee": 0.1,
        }
    )
    save_json(state_dir / "ledger.json", ledger)

    mock_client = MagicMock()
    mock_client.get_balance.return_value = [{"totalEq": "73319.81", "details": [{"ccy": "USDT", "eq": "10000"}]}]
    mock_client.get_positions.return_value = [{"instId": "ETH-USDT-SWAP", "posSide": "long", "pos": "1"}]
    mock_client.get_pending_algos.return_value = [{"algoId": "ALGO_ETH", "algoClOrdId": "SLS2ETH1", "sz": "1"}]
    mock_client.get_fills.return_value = []
    mock_client.get_positions_history.return_value = []
    mock_client.get_instruments.return_value = [
        {
            "instId": "BTC-USDT-SWAP", "settleCcy": "USDT", "ctType": "linear",
            "state": "live", "listTime": "1577836800000", "ctVal": "0.01",
            "lotSz": "1", "minSz": "1", "tickSz": "0.1",
        },
        {
            "instId": "ETH-USDT-SWAP", "settleCcy": "USDT", "ctType": "linear",
            "state": "live", "listTime": "1577836800000", "ctVal": "1",
            "lotSz": "1", "minSz": "1", "tickSz": "0.1",
        },
    ]
    mock_client.get_account_instruments.return_value = mock_client.get_instruments.return_value
    mock_client.get_tickers.return_value = [
        {"instId": "BTC-USDT-SWAP", "last": "123", "volCcy24h": "100000"},
        {"instId": "ETH-USDT-SWAP", "last": "122", "volCcy24h": "100000"},
    ]
    mock_client.get_candles.side_effect = (
        lambda inst_id, bar, limit=300, after=None: candles_4h if bar == "4H" else candles_1d
    )

    cfg = AppConfig(
        base_dir=tmp_path,
        api_key="k",
        api_secret="s",
        api_passphrase="p",
        trading_mode="demo",
        dry_run=True,
        settings={},
        control=ControlState(paused=False, close_all=False),
    )

    # First run: catches up 3 missed 4H bars, evaluates latest bar once for BTC-USDT-SWAP
    run_cycle(cfg, client=mock_client, local_mode=False, now=datetime(2026, 11, 25, 0, 3, tzinfo=timezone.utc))
    after_1 = load_json(state_dir / "ledger.json")
    assert len(after_1["dry_run_orders"]) == 1
    assert after_1["dry_run_orders"][0]["inst_id"] == "BTC-USDT-SWAP"

    # Check bars_held on ETH position (126..130 = 5 bars after bar 125)
    positions_json = load_json(tmp_path / "data" / "positions.json")
    assert positions_json[0]["bars_held"] == 5

    # Second run with same candles: must NOT duplicate order
    run_cycle(cfg, client=mock_client, local_mode=False, now=datetime(2026, 11, 25, 0, 18, tzinfo=timezone.utc))
    after_2 = load_json(state_dir / "ledger.json")
    assert len(after_2["dry_run_orders"]) == 1

    # Third run with force_rescan=True: re-evaluates bars (new_4h_closed=True) but clOrdId dedup still prevents duplicate
    res_force = run_cycle(
        cfg, client=mock_client, local_mode=False,
        now=datetime(2026, 11, 25, 0, 20, tzinfo=timezone.utc),
        force_rescan=True,
    )
    assert res_force["new_4h_closed"] is True
    assert res_force["new_1d_closed"] is True
    after_3 = load_json(state_dir / "ledger.json")
    assert len(after_3["dry_run_orders"]) == 1
    # DRY_RUN must only record into dry_run_cl_ord_ids, never into processed_cl_ord_ids
    assert len(after_3["dry_run_cl_ord_ids"]) == 1
    assert after_3["processed_cl_ord_ids"] == []


def test_extract_usdt_equity_ignores_non_usdt_assets():
    bal = [
        {
            "totalEq": "73319.81",
            "details": [
                {"ccy": "BTC", "eq": "1", "eqUsd": "58805.6"},
                {"ccy": "OKB", "eq": "100", "eqUsd": "7884.6"},
                {"ccy": "USDT", "eq": "5000", "eqUsd": "4993.1"},
                {"ccy": "ETH", "eq": "1", "eqUsd": "1636.51"},
            ],
        }
    ]
    assert _extract_usdt_equity(bal) == pytest.approx(5000.0)
    assert _extract_usdt_equity([]) == 0.0


def test_execute_entry_skips_canceled_or_zero_fill_order():
    ledger = init_ledger(5000.0)
    errors: list[str] = []
    mock_client = MagicMock()
    mock_client.place_order.return_value = [{"ordId": "ORD_ZERO"}]
    mock_client.get_order.return_value = [{"ordId": "ORD_ZERO", "state": "canceled", "accFillSz": "0"}]

    res = execute_entry(
        client=mock_client,
        ledger=ledger,
        strategy="S2",
        inst_id="H-USDT-SWAP",
        pos_side="short",
        sz=10.0,
        ref_price=0.1,
        stop_loss=0.12,
        planned_risk=5.0,
        notional=100.0,
        leverage=5,
        inst_meta={"ctVal": 1.0, "tickSz": 0.0001},
        bar_ts=1795579200000,
        reason="test",
        dry_run=False,
        errors=errors,
    )
    assert res is None
    assert len(ledger["positions"]) == 0
    assert mock_client.place_algo_order.call_count == 0
    assert mock_client.place_order.call_count == 1
    assert errors == ["S2 H-USDT-SWAP：模擬盤流動性不足，市價單未成交"]
