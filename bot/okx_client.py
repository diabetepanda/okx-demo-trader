"""OKX API v5 client with strict demo-trading guardrails, rate limiting, and retries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from bot.safety import (
    SIMULATED_TRADING_HEADERS,
    SafetyError,
    mask_secrets,
    validate_request,
)

logger = logging.getLogger(__name__)


class OKXAPIError(RuntimeError):
    """Raised when OKX returns a non-zero business error code or HTTP error."""

    def __init__(self, message: str, code: str = "", http_status: int = 0) -> None:
        super().__init__(mask_secrets(message))
        self.code = code
        self.http_status = http_status


class RateLimiter:
    """Thread-safe global rate limiter (default max 8 requests/sec)."""

    def __init__(self, max_per_sec: float = 8.0) -> None:
        self._min_interval = 1.0 / max_per_sec if max_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._last_ts = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_ts
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_ts = time.monotonic()


def utc_iso_timestamp() -> str:
    """Return OKX-compatible UTC ISO8601 timestamp with milliseconds."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sign_request(
    secret: str,
    timestamp: str,
    method: str,
    request_path: str,
    body: str = "",
) -> str:
    """Compute OKX v5 HMAC-SHA256 Base64 signature."""
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    mac = hmac.new(
        secret.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(mac).decode("utf-8")


class OKXClient:
    """Lightweight OKX v5 REST client locked to Demo Trading."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        base_url: str = "https://www.okx.com",
        dry_run: bool = True,
        session: Optional[requests.Session] = None,
        rate_limiter: Optional[RateLimiter] = None,
        timeout: float = 15.0,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.session = session or requests.Session()
        self.rate_limiter = rate_limiter or RateLimiter(max_per_sec=8.0)
        self.timeout = timeout

    def _secrets(self) -> list[str]:
        return [s for s in (self.api_key, self.api_secret, self.api_passphrase) if s]

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        private: bool = False,
    ) -> Any:
        method_upper = method.upper()

        # Hard guard: in DRY_RUN mode, never send any trade or account-mutating POST
        if self.dry_run and method_upper == "POST" and (
            path.startswith("/api/v5/trade/") or path.startswith("/api/v5/account/set-")
        ):
            raise SafetyError(
                f"DRY_RUN=true 模式下禁止送出寫入請求：{method_upper} {path}"
            )

        query_str = ""
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                query_str = "?" + urlencode(filtered)
        request_path = f"{path}{query_str}"
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            **SIMULATED_TRADING_HEADERS,
        }

        if private:
            if not (self.api_key and self.api_secret and self.api_passphrase):
                raise OKXAPIError("缺少 OKX 模擬盤 API 金鑰環境變數")
            ts = utc_iso_timestamp()
            headers["OK-ACCESS-KEY"] = self.api_key
            headers["OK-ACCESS-SIGN"] = sign_request(
                self.api_secret, ts, method_upper, request_path, body_str
            )
            headers["OK-ACCESS-TIMESTAMP"] = ts
            headers["OK-ACCESS-PASSPHRASE"] = self.api_passphrase

        # Mandatory pre-flight safety check
        validate_request(self.base_url, path, headers, is_private=private)

        url = f"{self.base_url}{request_path}"
        max_retries = 5

        for attempt in range(max_retries + 1):
            self.rate_limiter.wait()
            try:
                resp = self.session.request(
                    method=method_upper,
                    url=url,
                    headers=headers,
                    data=body_str if body is not None else None,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                safe_msg = mask_secrets(str(exc), self._secrets())
                if attempt < max_retries:
                    logger.warning("OKX 請求網路異常，2 秒後重試 (%d/%d): %s", attempt + 1, max_retries, safe_msg)
                    time.sleep(2.0)
                    continue
                raise OKXAPIError(f"OKX 連線失敗: {safe_msg}") from exc

            if resp.status_code == 429:
                if attempt < max_retries:
                    logger.warning("OKX HTTP 429 限流，2 秒後重試 (%d/%d)", attempt + 1, max_retries)
                    time.sleep(2.0)
                    continue
                raise OKXAPIError("OKX HTTP 429 超過重試上限", http_status=429)

            if resp.status_code >= 400:
                safe_text = mask_secrets(resp.text[:300], self._secrets())
                raise OKXAPIError(
                    f"OKX HTTP {resp.status_code}: {safe_text}",
                    http_status=resp.status_code,
                )

            try:
                payload = resp.json()
            except ValueError as exc:
                raise OKXAPIError("OKX 回傳非 JSON 格式") from exc

            code = str(payload.get("code", ""))
            if code == "50011":
                if attempt < max_retries:
                    logger.warning("OKX 50011 系統限流，2 秒後重試 (%d/%d)", attempt + 1, max_retries)
                    time.sleep(2.0)
                    continue
                raise OKXAPIError("OKX 50011 超過重試上限", code="50011")

            if code != "0":
                msg = mask_secrets(str(payload.get("msg", "")), self._secrets())
                data = payload.get("data")
                detail = ""
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    s_code = data[0].get("sCode", "")
                    s_msg = mask_secrets(str(data[0].get("sMsg", "")), self._secrets())
                    if s_code or s_msg:
                        detail = f" (sCode={s_code}, sMsg={s_msg})"
                raise OKXAPIError(f"OKX API 錯誤 code={code}: {msg}{detail}", code=code)

            return payload.get("data", [])

        raise OKXAPIError("OKX 請求超過重試上限")

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------
    def get_instruments(self, inst_type: str = "SWAP") -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v5/public/instruments",
            params={"instType": inst_type},
            private=False,
        )

    def get_tickers(self, inst_type: str = "SWAP") -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v5/market/tickers",
            params={"instType": inst_type},
            private=False,
        )

    def get_candles(
        self,
        inst_id: str,
        bar: str,
        limit: int = 300,
        after: Optional[str] = None,
    ) -> list[list[str]]:
        return self._request(
            "GET",
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": limit, "after": after},
            private=False,
        )

    # ------------------------------------------------------------------
    # Private read endpoints
    # ------------------------------------------------------------------
    def get_account_instruments(self, inst_type: str = "SWAP") -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v5/account/instruments",
            params={"instType": inst_type},
            private=True,
        )

    def get_balance(self, ccy: str = "USDT") -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v5/account/balance",
            params={"ccy": ccy} if ccy else None,
            private=True,
        )

    def get_positions(self, inst_type: str = "SWAP") -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v5/account/positions",
            params={"instType": inst_type},
            private=True,
        )

    def get_positions_history(
        self, inst_type: str = "SWAP", limit: int = 100
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v5/account/positions-history",
            params={"instType": inst_type, "limit": limit},
            private=True,
        )

    def get_pending_algos(
        self, ord_type: str = "conditional", inst_type: str = "SWAP"
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v5/trade/orders-algo-pending",
            params={"ordType": ord_type, "instType": inst_type},
            private=True,
        )

    def get_algo_history(
        self,
        ord_type: str = "conditional",
        state: Optional[str] = None,
        inst_type: str = "SWAP",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v5/trade/orders-algo-history",
            params={
                "ordType": ord_type,
                "state": state,
                "instType": inst_type,
                "limit": limit,
            },
            private=True,
        )

    def get_order(
        self,
        inst_id: str,
        ord_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v5/trade/order",
            params={"instId": inst_id, "ordId": ord_id, "clOrdId": cl_ord_id},
            private=True,
        )

    def get_fills(self, inst_type: str = "SWAP", limit: int = 100) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v5/trade/fills",
            params={"instType": inst_type, "limit": limit},
            private=True,
        )

    def get_bills(self, ccy: str = "USDT", limit: int = 100) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v5/account/bills",
            params={"ccy": ccy, "limit": limit},
            private=True,
        )

    # ------------------------------------------------------------------
    # Private write endpoints (blocked when dry_run=True)
    # ------------------------------------------------------------------
    def set_position_mode(self, pos_mode: str = "long_short_mode") -> list[dict[str, Any]]:
        return self._request(
            "POST",
            "/api/v5/account/set-position-mode",
            body={"posMode": pos_mode},
            private=True,
        )

    def set_leverage(
        self,
        inst_id: str,
        lever: int,
        mgn_mode: str = "cross",
        pos_side: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "instId": inst_id,
            "lever": str(lever),
            "mgnMode": mgn_mode,
        }
        if pos_side:
            body["posSide"] = pos_side
        return self._request(
            "POST",
            "/api/v5/account/set-leverage",
            body=body,
            private=True,
        )

    def place_order(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        return self._request(
            "POST",
            "/api/v5/trade/order",
            body=body,
            private=True,
        )

    def place_algo_order(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        return self._request(
            "POST",
            "/api/v5/trade/order-algo",
            body=body,
            private=True,
        )

    def amend_algo_order(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        return self._request(
            "POST",
            "/api/v5/trade/amend-algos",
            body=body,
            private=True,
        )

    def cancel_algo_orders(self, algo_orders: list[dict[str, str]]) -> list[dict[str, Any]]:
        return self._request(
            "POST",
            "/api/v5/trade/cancel-algos",
            body=algo_orders,
            private=True,
        )
