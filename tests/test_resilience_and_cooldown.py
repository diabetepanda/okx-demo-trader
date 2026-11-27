from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from bot.executor import execute_entry
from bot.ledger import init_ledger
from bot.okx_client import OKXAPIError, OKXClient, RateLimiter


def test_partial_fill_emergency_close_records_closed_trade(monkeypatch):
    monkeypatch.setattr("bot.executor.time.sleep", lambda _: None)
    ledger = init_ledger(5000.0)
    mock_client = MagicMock()
    mock_client.place_order.side_effect = [
        [{"ordId": "ORD_ENTRY"}],
        [{"ordId": "ORD_CLOSE"}],
    ]
    mock_client.get_order.side_effect = [
        [{"ordId": "ORD_ENTRY", "state": "canceled", "accFillSz": "3.6", "avgPx": "0.0802", "fee": "-0.0014", "fillTime": "1000"}],
        [{"ordId": "ORD_CLOSE", "state": "filled", "accFillSz": "3.6", "avgPx": "0.0804", "fee": "-0.0014", "fillTime": "2000"}],
    ]
    mock_client.get_pending_algos.return_value = []
    mock_client.place_algo_order.side_effect = OKXAPIError("algo failed", code="51000")

    res = execute_entry(
        client=mock_client,
        ledger=ledger,
        strategy="S2",
        inst_id="ARB-USDT-SWAP",
        pos_side="short",
        sz=20.1,
        ref_price=0.0802,
        stop_loss=0.1035,
        planned_risk=5.0,
        notional=16.12,
        leverage=5,
        inst_meta={"ctVal": 10.0, "tickSz": 0.0001},
        bar_ts=1795651200000,
        reason="test",
        dry_run=False,
    )
    assert res is None
    assert len(ledger["positions"]) == 0
    assert len(ledger["closed_trades"]) == 1
    trade = ledger["closed_trades"][0]
    assert trade["inst_id"] == "ARB-USDT-SWAP"
    assert trade["sz"] == 3.6
    assert trade["pnl_usdt"] < 0


def test_two_unfilled_liquidity_failures_suspend_7_days(monkeypatch):
    monkeypatch.setattr("bot.executor.time.sleep", lambda _: None)
    ledger = init_ledger(5000.0)
    mock_client = MagicMock()
    mock_client.place_order.return_value = [{"ordId": "ORD_ZERO"}]
    mock_client.get_order.return_value = [{"ordId": "ORD_ZERO", "state": "canceled", "accFillSz": "0"}]
    mock_client.get_positions.return_value = []

    for bar_ts in (1000, 2000):
        res = execute_entry(
            client=mock_client,
            ledger=ledger,
            strategy="S2",
            inst_id="ASML-USDT-SWAP",
            pos_side="long",
            sz=10.0,
            ref_price=1400.0,
            stop_loss=1350.0,
            planned_risk=5.0,
            notional=140.0,
            leverage=5,
            inst_meta={"ctVal": 0.001, "tickSz": 0.1},
            bar_ts=bar_ts,
            reason="test",
            dry_run=False,
        )
        assert res is None

    assert ledger["liquidity_failures"]["ASML-USDT-SWAP"] == 2
    assert "ASML-USDT-SWAP" in ledger["suspended_until"]
    assert any("暫停 ASML-USDT-SWAP 進場 7 天" in r["reason"] for r in ledger["rejected_signals"])


def test_okx_client_get_retries_2_4_8_on_503_and_50001(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("bot.okx_client.time.sleep", lambda s: sleeps.append(s))

    resp_503 = MagicMock(status_code=503)
    resp_50001 = MagicMock(status_code=200)
    resp_50001.json.return_value = {"code": "50001", "msg": "Service temporarily unavailable"}
    resp_ok = MagicMock(status_code=200)
    resp_ok.json.return_value = {"code": "0", "data": [{"instId": "BTC-USDT-SWAP"}]}

    mock_session = MagicMock()
    mock_session.request.side_effect = [resp_503, resp_50001, requests.Timeout("timeout"), resp_ok]

    client = OKXClient(session=mock_session, rate_limiter=RateLimiter(max_per_sec=0))
    data = client.get_tickers("SWAP")
    assert data == [{"instId": "BTC-USDT-SWAP"}]
    assert sleeps == [2.0, 4.0, 8.0]


def test_okx_client_order_post_checks_clordid_before_retry(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("bot.okx_client.time.sleep", lambda s: sleeps.append(s))

    resp_503 = MagicMock(status_code=503)
    resp_existing_order = MagicMock(status_code=200)
    resp_existing_order.json.return_value = {
        "code": "0",
        "data": [{"ordId": "EXISTING_123", "clOrdId": "S2BTC1"}],
    }

    mock_session = MagicMock()
    # 1st call: POST /trade/order gets 503; 2nd call: GET /trade/order finds existing order!
    mock_session.request.side_effect = [resp_503, resp_existing_order]

    client = OKXClient(
        api_key="k",
        api_secret="s",
        api_passphrase="p",
        dry_run=False,
        session=mock_session,
        rate_limiter=RateLimiter(max_per_sec=0),
    )
    res = client.place_order({"instId": "BTC-USDT-SWAP", "clOrdId": "S2BTC1", "sz": "1"})
    assert res[0]["ordId"] == "EXISTING_123"
    # Must NOT have sent a second POST!
    post_calls = [c for c in mock_session.request.call_args_list if c.kwargs.get("method") == "POST"]
    assert len(post_calls) == 1
    assert sleeps == []


def test_pre_order_safety_check_blocks_oversized_risk_and_notional():
    ledger = init_ledger(5000.0)
    mock_client = MagicMock()

    # Risk = 100 * 1.0 * |100 - 90| = 1000 > planned_risk (50) * 1.2
    res_risk = execute_entry(
        client=mock_client,
        ledger=ledger,
        strategy="S2",
        inst_id="BTC-USDT-SWAP",
        pos_side="long",
        sz=100.0,
        ref_price=100.0,
        stop_loss=90.0,
        planned_risk=50.0,
        notional=10000.0,
        leverage=5,
        inst_meta={"ctVal": 1.0, "tickSz": 0.1},
        bar_ts=1795651200000,
        reason="test",
        dry_run=False,
        max_sub_notional=25000.0,
    )
    assert res_risk is None
    assert mock_client.place_order.call_count == 0
    assert any("下單前風控攔截" in r["reason"] for r in ledger["rejected_signals"])

    # Notional = 300 * 1.0 * 100 = 30000 > max_sub_notional (25000)
    res_notional = execute_entry(
        client=mock_client,
        ledger=ledger,
        strategy="S2",
        inst_id="ETH-USDT-SWAP",
        pos_side="long",
        sz=300.0,
        ref_price=100.0,
        stop_loss=99.9,
        planned_risk=50.0,
        notional=30000.0,
        leverage=5,
        inst_meta={"ctVal": 1.0, "tickSz": 0.1},
        bar_ts=1795651200001,
        reason="test",
        dry_run=False,
        max_sub_notional=25000.0,
    )
    assert res_notional is None
    assert mock_client.place_order.call_count == 0
