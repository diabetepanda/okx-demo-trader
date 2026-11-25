# OKX 模擬盤自動交易機器人＋公開績效網站

專為 **OKX Demo Trading（模擬盤）** 設計的全自動交易機器人與 GitHub Pages 公開績效儀表板。

## 安全保證
- 所有請求強制寫死 `x-simulated-trading: 1`。
- 啟動強制檢查 `TRADING_MODE=demo`，否則立即拒絕執行。
- API 網址白名單（僅限 `https://openapi.okx.com`、`https://www.okx.com`），資金／提幣／理財路徑全面黑名單。
- `DRY_RUN=true`（預設）時不送出任何下單／改單／撤單請求。
- 緊急開關 `control.json`：`paused`（暫停新進場）、`close_all`（市價平掉本機器人所有倉位）。

## 本機操作
1. 複製 `.env.example` 為 `.env` 並填入 OKX 模擬盤 API 金鑰。
2. 執行連線測試：
   ```bash
   .venv/bin/python scripts/check_connection.py
   ```
3. 執行本機試跑（不提交 git，結果寫入 `local_output/`）：
   ```bash
   DRY_RUN=true .venv/bin/python -m bot.main --local
   ```
4. 執行自動化測試：
   ```bash
   .venv/bin/pytest -v
   ```
