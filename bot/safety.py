"""Security and safety guardrails for OKX Demo Trading.

All rules in Section 0 are enforced here:
- Simulated trading header ("x-simulated-trading": "1") is a hardcoded constant.
- TRADING_MODE must equal "demo".
- Base URL must be in the strict OKX whitelist.
- Asset / finance / funding endpoints are strictly blacklisted.
- API secrets are masked in all logs and error strings.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Iterable, Mapping, Optional
from urllib.parse import urlparse

SIMULATED_TRADING_HEADER_NAME = "x-simulated-trading"
SIMULATED_TRADING_HEADER_VALUE = "1"
SIMULATED_TRADING_HEADERS: dict[str, str] = {
    SIMULATED_TRADING_HEADER_NAME: SIMULATED_TRADING_HEADER_VALUE,
}

ALLOWED_BASE_URLS: frozenset[str] = frozenset({
    "https://openapi.okx.com",
    "https://www.okx.com",
})

BLOCKED_PATH_PREFIXES: tuple[str, ...] = (
    "/api/v5/asset/",
    "/api/v5/finance/",
    "/api/v5/broker/",
    "/api/v5/subaccount/transfer",
)

SECRET_ENV_KEYS: tuple[str, ...] = (
    "OKX_API_KEY",
    "OKX_API_SECRET",
    "OKX_API_PASSPHRASE",
)


class SafetyError(RuntimeError):
    """Raised when a safety guardrail is violated."""


def enforce_demo_mode(environ: Optional[Mapping[str, str]] = None) -> None:
    """Verify TRADING_MODE == 'demo'. Exit with code 1 if not."""
    env = os.environ if environ is None else environ
    mode = str(env.get("TRADING_MODE", "")).strip()
    if mode != "demo":
        print("拒絕執行：只允許模擬盤", file=sys.stderr)
        raise SystemExit(1)


def validate_base_url(base_url: str) -> str:
    """Ensure base_url is in the strict OKX whitelist."""
    normalized = base_url.strip().rstrip("/")
    if normalized not in ALLOWED_BASE_URLS:
        raise SafetyError(f"拒絕連線：不允許的 API 網址 {normalized}")
    return normalized


def validate_path(path: str) -> str:
    """Ensure request path is not in the asset/finance blacklist."""
    parsed = urlparse(path)
    clean_path = (parsed.path or path).strip()
    if not clean_path.startswith("/"):
        clean_path = "/" + clean_path
    lowered = clean_path.lower()
    for prefix in BLOCKED_PATH_PREFIXES:
        if lowered.startswith(prefix):
            raise SafetyError(f"拒絕執行：禁止存取資金相關路徑 {clean_path}")
    return path


def validate_simulated_headers(headers: Mapping[str, str]) -> None:
    """Ensure x-simulated-trading: 1 is present in headers (case-insensitive key check)."""
    found = None
    for k, v in headers.items():
        if k.lower() == SIMULATED_TRADING_HEADER_NAME:
            found = str(v).strip()
            break
    if found != SIMULATED_TRADING_HEADER_VALUE:
        raise SafetyError("拒絕送出請求：缺少模擬盤標頭 x-simulated-trading: 1")


def validate_request(
    base_url: str,
    path: str,
    headers: Mapping[str, str],
    is_private: bool = False,
) -> None:
    """Run all pre-flight safety checks before any HTTP request."""
    validate_base_url(base_url)
    validate_path(path)
    validate_simulated_headers(headers)
    if is_private:
        # Extra explicit check for private requests
        validate_simulated_headers(headers)


def collect_secrets(extra_secrets: Optional[Iterable[str]] = None) -> list[str]:
    """Collect non-empty secret strings from environment and optional extras."""
    secrets: list[str] = []
    for key in SECRET_ENV_KEYS:
        val = os.environ.get(key, "")
        if val:
            secrets.append(val)
    if extra_secrets:
        for val in extra_secrets:
            if val:
                secrets.append(val)
    # Sort longest first so partial overlaps mask cleanly
    return sorted(set(secrets), key=len, reverse=True)


def mask_secrets(text: str, secrets: Optional[Iterable[str]] = None) -> str:
    """Replace any secret values in text with '***'."""
    if not text:
        return text
    targets = collect_secrets(secrets)
    masked = text
    for secret in targets:
        if secret and secret in masked:
            masked = masked.replace(secret, "***")
    return masked


class SecretMaskingFilter(logging.Filter):
    """Logging filter that masks API credentials in all log records."""

    def __init__(self, extra_secrets: Optional[Iterable[str]] = None) -> None:
        super().__init__()
        self._extra_secrets = list(extra_secrets) if extra_secrets else []

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = collect_secrets(self._extra_secrets)
        try:
            formatted = record.getMessage()
        except Exception:
            formatted = str(record.msg)
        record.msg = mask_secrets(formatted, secrets)
        record.args = None
        return True


def configure_logging(
    level: int = logging.INFO,
    extra_secrets: Optional[Iterable[str]] = None,
) -> logging.Logger:
    """Configure root logger with SecretMaskingFilter on all handlers."""
    root = logging.getLogger()
    root.setLevel(level)
    mask_filter = SecretMaskingFilter(extra_secrets=extra_secrets)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root.addHandler(handler)
    for handler in root.handlers:
        # Avoid duplicate filters of the same type
        handler.filters = [
            f for f in handler.filters if not isinstance(f, SecretMaskingFilter)
        ]
        handler.addFilter(mask_filter)
    return root
