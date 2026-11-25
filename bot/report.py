"""Generate public GitHub Pages JSON data (Taipei time, zero secrets/account IDs)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bot.config import AppConfig
from bot.ledger import STRATEGIES, save_json
from bot.safety import mask_secrets

TAIPEI_TZ = timezone(timedelta(hours=8))

EXIT_RULE_DESCRIPTIONS: dict[str, str] = {
    "S2": "持有滿 30 根 4H 或收盤反向突破前 10 根通道出場；附 2.5×ATR(14) 止損",
    "S3": "持有滿 10 天或日線收盤站上 SMA5 出場；附 2.5×ATR(14) 止損",
    "L1": "日線收盤反向突破前 10 日通道出場；附 3.0×ATR(14) 止損",
    "L2": "日線收盤反向突破前 20 日通道出場；附 3.0×ATR(14) 止損",
    "L3": "每週日收盤再平衡未入選前 3 名正動能出場；附 3.0×ATR(14) 止損",
    "L4": "週日收盤跌破 SMA100 出場；每日依近 60 日最高收盤 - 5×ATR(20) 只升不降移動止損",
}


def format_taipei_ts(ts_ms: int) -> str:
    if not ts_ms:
        return "-"
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=TAIPEI_TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


def format_taipei_dt(dt: datetime) -> str:
    return dt.astimezone(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S (台北時間)")


def compute_max_drawdown_pct(equities: list[float]) -> float:
    if not equities:
        return 0.0
    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return round(max_dd, 2)


def generate_site_data(
    data_dir: Path,
    ledger: dict[str, Any],
    current_equity: float,
    last_prices: dict[str, float],
    bars_held_map: dict[str, int],
    cfg: AppConfig,
    run_entry: dict[str, Any],
    now: datetime,
) -> None:
    """Write all JSON files consumed by the static GitHub Pages frontend."""
    secrets = [cfg.api_key, cfg.api_secret, cfg.api_passphrase]

    initial_eq = float(ledger.get("initial_equity_usdt") or 0.0)
    total_return_pct = (
        ((current_equity - initial_eq) / initial_eq * 100.0)
        if initial_eq > 0 and current_equity > 0
        else 0.0
    )

    eq_history = ledger.get("equity_history", [])
    eq_values = [float(pt["equity"]) for pt in eq_history if pt.get("equity")]
    max_dd_pct = compute_max_drawdown_pct(eq_values)

    if cfg.control.close_all:
        mode_label = "緊急全平 (close_all)"
    elif cfg.control.paused:
        mode_label = "已暫停進場 (paused)"
    elif cfg.dry_run:
        mode_label = "試跑模式 (DRY_RUN)"
    else:
        mode_label = "模擬盤實單"

    safe_errors = [mask_secrets(str(e), secrets) for e in run_entry.get("errors", [])]

    summary = {
        "total_equity_usdt": round(current_equity, 2),
        "initial_equity_usdt": round(initial_eq, 2),
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": max_dd_pct,
        "mode": mode_label,
        "dry_run": cfg.dry_run,
        "paused": cfg.control.paused,
        "close_all": cfg.control.close_all,
        "open_positions_count": len(ledger.get("positions", [])),
        "closed_trades_count": len(ledger.get("closed_trades", [])),
        "last_updated_tpe": format_taipei_dt(now),
        "latest_run": {
            "time_tpe": format_taipei_dt(now),
            "new_4h_closed": run_entry.get("new_4h_closed", False),
            "new_1d_closed": run_entry.get("new_1d_closed", False),
            "reconciled_closed": run_entry.get("reconciled_closed", 0),
            "signals": run_entry.get("signals", {}),
            "errors": safe_errors,
        },
    }
    save_json(data_dir / "summary.json", summary)

    # equity.json
    equity_out = [
        {
            "ts": int(pt["ts"]),
            "time_tpe": format_taipei_ts(int(pt["ts"])),
            "equity": float(pt["equity"]),
        }
        for pt in eq_history
    ]
    save_json(data_dir / "equity.json", equity_out)

    # positions.json
    positions_out: list[dict[str, Any]] = []
    for pos in ledger.get("positions", []):
        inst_id = pos["inst_id"]
        entry_px = float(pos["entry_price"])
        cur_px = last_prices.get(inst_id, entry_px)
        sz = float(pos["sz"])
        ct_val = float(pos.get("ct_val") or 1.0)
        if pos["pos_side"] == "long":
            unrealized = (cur_px - entry_px) * sz * ct_val
        else:
            unrealized = (entry_px - cur_px) * sz * ct_val

        positions_out.append(
            {
                "strategy": pos["strategy"],
                "inst_id": inst_id,
                "pos_side": pos["pos_side"],
                "entry_price": round(entry_px, 6),
                "current_price": round(cur_px, 6),
                "sz": sz,
                "stop_loss": round(float(pos["stop_loss"]), 6),
                "unrealized_pnl": round(unrealized, 2),
                "bars_held": bars_held_map.get(pos.get("id", ""), 0),
                "entry_time_tpe": format_taipei_ts(int(pos.get("entry_ts") or 0)),
                "exit_rule_desc": EXIT_RULE_DESCRIPTIONS.get(pos["strategy"], ""),
            }
        )
    save_json(data_dir / "positions.json", positions_out)

    # trades.json (most recent 100, newest first)
    closed_trades = ledger.get("closed_trades", [])
    trades_out = []
    for t in reversed(closed_trades[-100:]):
        trades_out.append(
            {
                "strategy": t["strategy"],
                "inst_id": t["inst_id"],
                "pos_side": t["pos_side"],
                "entry_time_tpe": format_taipei_ts(int(t.get("entry_ts") or 0)),
                "exit_time_tpe": format_taipei_ts(int(t.get("exit_ts") or 0)),
                "entry_price": round(float(t["entry_price"]), 6),
                "exit_price": round(float(t["exit_price"]), 6),
                "pnl_usdt": round(float(t["pnl_usdt"]), 2),
                "r_multiple": round(float(t.get("r_multiple") or 0.0), 2),
                "exit_reason": mask_secrets(str(t.get("exit_reason", "")), secrets),
            }
        )
    save_json(data_dir / "trades.json", trades_out)

    # strategies.json
    subaccounts = ledger.get("subaccounts", {})
    strategies_out = []
    for s in STRATEGIES:
        sub = subaccounts.get(s, {})
        sub_init = float(sub.get("initial_equity") or 0.0)
        sub_realized = float(sub.get("realized_pnl") or 0.0)
        sub_equity = sub_init + sub_realized
        s_trades = [t for t in closed_trades if t.get("strategy") == s]
        wins = [t for t in s_trades if float(t.get("pnl_usdt") or 0.0) > 0]
        losses = [t for t in s_trades if float(t.get("pnl_usdt") or 0.0) < 0]
        win_rate = (len(wins) / len(s_trades) * 100.0) if s_trades else 0.0
        gross_win = sum(float(t["pnl_usdt"]) for t in wins)
        gross_loss = abs(sum(float(t["pnl_usdt"]) for t in losses))
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (99.99 if gross_win > 0 else 0.0)
        avg_r = (
            sum(float(t.get("r_multiple") or 0.0) for t in s_trades) / len(s_trades)
            if s_trades
            else 0.0
        )

        # Subaccount equity curve for drawdown
        curve = [sub_init]
        running = sub_init
        for t in s_trades:
            running += float(t.get("pnl_usdt") or 0.0)
            curve.append(running)
        sub_mdd = compute_max_drawdown_pct(curve)

        strategies_out.append(
            {
                "strategy": s,
                "allocation_pct": round(float(sub.get("allocation") or 0.0) * 100, 1),
                "initial_equity": round(sub_init, 2),
                "equity": round(sub_equity, 2),
                "realized_pnl": round(sub_realized, 2),
                "trades_count": len(s_trades),
                "win_rate_pct": round(win_rate, 1),
                "profit_factor": round(profit_factor, 2),
                "avg_r": round(avg_r, 2),
                "max_drawdown_pct": sub_mdd,
            }
        )
    save_json(data_dir / "strategies.json", strategies_out)

    # dry_run_orders.json
    dry_orders = []
    for o in reversed(ledger.get("dry_run_orders", [])[-100:]):
        dry_orders.append(
            {
                "time_tpe": format_taipei_ts(int(o.get("ts") or 0)),
                "action": o.get("action", ""),
                "strategy": o.get("strategy", ""),
                "inst_id": o.get("inst_id", ""),
                "pos_side": o.get("pos_side", ""),
                "sz": o.get("sz", 0),
                "ref_price": o.get("ref_price", 0),
                "stop_loss": o.get("stop_loss", o.get("new_sl", 0)),
                "reason": mask_secrets(str(o.get("reason", "")), secrets),
            }
        )
    save_json(data_dir / "dry_run_orders.json", dry_orders)
