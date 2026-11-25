"""Read-only local connection & safety verification script."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config
from bot.okx_client import OKXAPIError, OKXClient
from bot.safety import (
    SIMULATED_TRADING_HEADERS,
    configure_logging,
    enforce_demo_mode,
    mask_secrets,
)
from bot.universe import build_universe


def main() -> int:
    cfg = load_config()
    enforce_demo_mode({"TRADING_MODE": cfg.trading_mode})
    configure_logging(extra_secrets=[cfg.api_key, cfg.api_secret, cfg.api_passphrase])

    print(f"✅ 安全檢查：TRADING_MODE=demo，模擬盤標頭 {SIMULATED_TRADING_HEADERS} 已啟用")

    if not cfg.has_credentials():
        print("❌ 尚未設定 OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE，請先填寫 .env")
        return 1

    client = OKXClient(
        api_key=cfg.api_key,
        api_secret=cfg.api_secret,
        api_passphrase=cfg.api_passphrase,
        base_url=cfg.base_url,
        dry_run=True,
    )

    try:
        bal = client.get_balance("USDT")
        total_eq = 0.0
        if bal:
            total_eq = float(bal[0].get("totalEq") or 0.0)
        print(f"✅ 模擬盤連線成功，USDT 權益：{total_eq:.2f} USDT")

        u_cfg = cfg.settings.get("universe", {})
        universe = build_universe(
            client=client,
            top_n=int(u_cfg.get("top_n", 100)),
            min_listed_days=int(u_cfg.get("min_listed_days", 30)),
            l2_symbols=u_cfg.get("l2_symbols"),
            l4_symbols=u_cfg.get("l4_symbols"),
        )
        print(f"✅ 前 100 名清單：{len(universe['top_100'])} 檔（L2={len(universe['l2'])} 檔、L4={len(universe['l4'])} 檔）")
        return 0
    except OKXAPIError as exc:
        safe_err = mask_secrets(str(exc), [cfg.api_key, cfg.api_secret, cfg.api_passphrase])
        print(f"❌ 連線或查詢失敗：{safe_err}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
