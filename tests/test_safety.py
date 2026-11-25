from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bot.config import AppConfig, ControlState
from bot.main import run_cycle
from bot.okx_client import OKXClient, RateLimiter
from bot.safety import SafetyError, configure_logging, enforce_demo_mode


def test_enforce_demo_mode_rejects_non_demo():
    with pytest.raises(SystemExit) as exc:
        enforce_demo_mode({"TRADING_MODE": "live"})
    assert exc.value.code == 1

    with pytest.raises(SystemExit) as exc2:
        enforce_demo_mode({})
    assert exc2.value.code == 1

    enforce_demo_mode({"TRADING_MODE": "demo"})


def test_secret_masking_in_logs(caplog, monkeypatch):
    fake_key = "FAKE_KEY_998877"
    fake_sec = "FAKE_SECRET_112233"
    fake_pass = "FAKE_PASS_445566"

    monkeypatch.setenv("OKX_API_KEY", fake_key)
    monkeypatch.setenv("OKX_API_SECRET", fake_sec)
    monkeypatch.setenv("OKX_API_PASSPHRASE", fake_pass)

    configure_logging(level=logging.INFO)
    test_logger = logging.getLogger("test_secret_mask")

    with caplog.at_level(logging.INFO):
        test_logger.info("Connecting with key=%s secret=%s pass=%s", fake_key, fake_sec, fake_pass)
        test_logger.error(f"Error header {fake_key} / {fake_sec} / {fake_pass}")

    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert fake_key not in combined
    assert fake_sec not in combined
    assert fake_pass not in combined
    assert "***" in combined


def test_blacklisted_asset_path_never_sends_request():
    mock_session = MagicMock()
    client = OKXClient(
        api_key="k",
        api_secret="s",
        api_passphrase="p",
        dry_run=False,
        session=mock_session,
        rate_limiter=RateLimiter(max_per_sec=1000),
    )
    with pytest.raises(SafetyError):
        client._request("POST", "/api/v5/asset/withdrawal", body={"ccy": "USDT"}, private=True)
    with pytest.raises(SafetyError):
        client._request("GET", "/api/v5/finance/savings/balance", private=True)
    assert mock_session.request.call_count == 0


def test_non_whitelisted_url_rejected():
    mock_session = MagicMock()
    client = OKXClient(
        base_url="https://evil.example.com",
        session=mock_session,
        rate_limiter=RateLimiter(max_per_sec=1000),
    )
    with pytest.raises(SafetyError):
        client.get_tickers("SWAP")
    assert mock_session.request.call_count == 0


def test_every_request_has_simulated_header():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"code": "0", "data": []}

    mock_session = MagicMock()
    mock_session.request.return_value = mock_resp

    client = OKXClient(
        api_key="k",
        api_secret="s",
        api_passphrase="p",
        dry_run=False,
        session=mock_session,
        rate_limiter=RateLimiter(max_per_sec=1000),
    )
    client.get_tickers("SWAP")
    client.get_balance("USDT")

    assert mock_session.request.call_count == 2
    for call in mock_session.request.call_args_list:
        headers = call.kwargs["headers"]
        assert headers.get("x-simulated-trading") == "1"


def test_dry_run_sends_zero_post_orders(tmp_path: Path):
    mock_session = MagicMock()

    def _fake_request(method, url, headers=None, data=None, timeout=None):
        assert headers.get("x-simulated-trading") == "1"
        assert method == "GET"  # Must never send POST in DRY_RUN
        resp = MagicMock()
        resp.status_code = 200
        if "/account/balance" in url:
            resp.json.return_value = {"code": "0", "data": [{"totalEq": "70000", "details": [{"ccy": "USDT", "eq": "10000"}]}]}
        elif "/account/positions-history" in url:
            resp.json.return_value = {"code": "0", "data": []}
        elif "/account/positions" in url:
            resp.json.return_value = {"code": "0", "data": []}
        elif "/trade/orders-algo-pending" in url:
            resp.json.return_value = {"code": "0", "data": []}
        elif "/trade/fills" in url:
            resp.json.return_value = {"code": "0", "data": []}
        elif "/public/instruments" in url or "/account/instruments" in url:
            resp.json.return_value = {
                "code": "0",
                "data": [{
                    "instId": "BTC-USDT-SWAP",
                    "settleCcy": "USDT",
                    "ctType": "linear",
                    "state": "live",
                    "listTime": "1577836800000",
                    "ctVal": "0.01",
                    "lotSz": "1",
                    "minSz": "1",
                    "tickSz": "0.1",
                }],
            }
        elif "/market/tickers" in url:
            resp.json.return_value = {
                "code": "0",
                "data": [{"instId": "BTC-USDT-SWAP", "last": "50000", "volCcy24h": "10000"}],
            }
        elif "/market/candles" in url:
            # Generate 300 upward breakout candles
            base_ts = 1700000000000
            step = 14400000 if "bar=4H" in url else 86400000
            rows = []
            for idx in range(300, 0, -1):
                px = 40000 + idx * 20
                rows.append([
                    str(base_ts + idx * step),
                    str(px - 10),
                    str(px + 15),
                    str(px - 20),
                    str(px),
                    "100", "100", "100", "1",
                ])
            resp.json.return_value = {"code": "0", "data": rows}
        else:
            resp.json.return_value = {"code": "0", "data": []}
        return resp

    mock_session.request.side_effect = _fake_request

    client = OKXClient(
        api_key="k",
        api_secret="s",
        api_passphrase="p",
        dry_run=True,
        session=mock_session,
        rate_limiter=RateLimiter(max_per_sec=1000),
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

    run_cycle(cfg, client=client, local_mode=False, now=datetime(2026, 11, 25, 0, 3, tzinfo=timezone.utc))

    post_calls = [
        c for c in mock_session.request.call_args_list
        if (c.kwargs.get("method") or (c.args[0] if c.args else "")) == "POST"
    ]
    assert len(post_calls) == 0
