"""Main entrypoint for one full scan, reconcile, strategy run, and report cycle."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bot.candles import Candle, CandleCache, count_bars_held
from bot.config import AppConfig, load_config
from bot.executor import (
    ensure_position_mode,
    execute_amend_sl,
    execute_entry,
    execute_exit,
    reconcile_positions,
)
from bot.ledger import append_run_log, init_ledger, load_json, save_json
from bot.okx_client import OKXAPIError, OKXClient
from bot.report import generate_site_data
from bot.safety import configure_logging, enforce_demo_mode, mask_secrets
from bot.sizing import ExistingPositionRisk, allocate_subaccount_position
from bot.strategies import (
    OpenPositionView,
    StrategyDecision,
    evaluate_l1,
    evaluate_l2,
    evaluate_l3_portfolio,
    evaluate_l4,
    evaluate_s2,
    evaluate_s3,
)
from bot.universe import ensure_universe

logger = logging.getLogger(__name__)


def _extract_usdt_equity_and_upl(balance_data: list[dict[str, Any]]) -> tuple[float, float]:
    """Return strictly USDT equity (eq, upl), excluding BTC/ETH/OKB."""
    if not balance_data:
        return 0.0, 0.0
    for d in balance_data[0].get("details", []):
        if d.get("ccy") == "USDT":
            return float(d.get("eq") or 0.0), float(d.get("upl") or 0.0)
    return 0.0, 0.0


def _extract_usdt_equity(balance_data: list[dict[str, Any]]) -> float:
    """Return strictly USDT equity (details[ccy=='USDT'].eq), excluding BTC/ETH/OKB."""
    eq, _ = _extract_usdt_equity_and_upl(balance_data)
    return eq


def _to_existing_risk(pos: dict[str, Any]) -> ExistingPositionRisk:
    return ExistingPositionRisk(
        strategy=pos["strategy"],
        inst_id=pos["inst_id"],
        pos_side=pos["pos_side"],
        notional=float(pos.get("notional") or 0.0),
        planned_risk=float(pos.get("planned_risk") or 0.0),
    )


def _to_open_view(pos: dict[str, Any]) -> OpenPositionView:
    return OpenPositionView(
        inst_id=pos["inst_id"],
        pos_side=pos["pos_side"],
        entry_ts=int(pos["entry_ts"]),
        stop_loss=float(pos["stop_loss"]),
    )


def run_cycle(
    cfg: AppConfig,
    client: Optional[OKXClient] = None,
    local_mode: bool = False,
    now: Optional[datetime] = None,
    force_rescan: bool = False,
) -> dict[str, Any]:
    """Execute one complete bot cycle. Returns summary dict."""
    enforce_demo_mode({"TRADING_MODE": cfg.trading_mode})
    configure_logging(
        extra_secrets=[cfg.api_key, cfg.api_secret, cfg.api_passphrase]
    )

    dt_now = now or datetime.now(timezone.utc)
    if local_mode:
        state_dir = cfg.base_dir / "local_output" / "state"
        data_dir = cfg.base_dir / "local_output" / "data"
    else:
        state_dir = cfg.base_dir / "state"
        data_dir = cfg.base_dir / "data"

    ledger_path = state_dir / "ledger.json"
    universe_path = state_dir / "universe.json"
    run_log_path = state_dir / "run_log.jsonl"

    okx = client or OKXClient(
        api_key=cfg.api_key,
        api_secret=cfg.api_secret,
        api_passphrase=cfg.api_passphrase,
        base_url=cfg.base_url,
        dry_run=cfg.dry_run,
    )

    errors: list[str] = []
    secrets = [cfg.api_key, cfg.api_secret, cfg.api_passphrase]

    # 1. Load or initialize ledger
    balance_data = okx.get_balance("USDT")
    current_equity, current_upl = _extract_usdt_equity_and_upl(balance_data)
    ledger = load_json(ledger_path)
    if ledger is None:
        init_eq = current_equity if current_equity > 0 else 10000.0
        ledger = init_ledger(init_eq, cfg.settings.get("allocations"))
        logger.info("首次執行：初始化帳本，起始權益=%.2f USDT", init_eq)

    # 2. Reconcile positions with OKX
    recon = reconcile_positions(okx, ledger)

    # 2b. Detect unexplained equity jumps (> 1%) as deposits / withdrawals
    total_realized_now = sum(
        float(s.get("realized_pnl", 0.0)) for s in ledger.get("subaccounts", {}).values()
    )
    snap = ledger.get("last_equity_snapshot")
    if snap and current_equity > 0:
        prev_eq = float(snap.get("equity") or 0.0)
        prev_realized = float(snap.get("realized_pnl") or 0.0)
        prev_upl = float(snap.get("unrealized_pnl") or 0.0)
        prev_ts = int(snap.get("ts") or 0)
        expected_eq = prev_eq + (total_realized_now - prev_realized) + (current_upl - prev_upl)
        unexplained = current_equity - expected_eq
        if prev_eq > 0 and abs(unexplained) / prev_eq > 0.01:
            cf_ts = int(dt_now.timestamp() * 1000)
            cf_amount = round(unexplained, 6)
            cf_source = "inferred"
            cf_note = "系統推算資金異動（無法由交易盈虧解釋）"
            try:
                bills = okx.get_bills("USDT", limit=50)
                for b in bills:
                    b_ts = int(b.get("ts") or 0)
                    b_chg = float(b.get("balChg") or 0.0)
                    b_inst = str(b.get("instId") or "")
                    if b_ts > prev_ts and not b_inst.endswith("-SWAP") and abs(b_chg) > max(1.0, prev_eq * 0.01):
                        cf_ts = b_ts
                        cf_amount = round(b_chg, 6)
                        cf_source = "okx_bills"
                        cf_note = f"OKX 帳單紀錄 ({b_inst or '資金劃轉'})"
                        break
            except Exception:
                pass
            existing_cf_ts = {int(c.get("ts") or 0) for c in ledger.get("cash_flows", [])}
            if cf_ts not in existing_cf_ts:
                ledger.setdefault("cash_flows", []).append(
                    {
                        "ts": cf_ts,
                        "amount_usdt": cf_amount,
                        "type": "deposit" if cf_amount > 0 else "withdrawal",
                        "source": cf_source,
                        "note": cf_note,
                    }
                )
                logger.info("偵測到資金異動：%.2f USDT (%s)", cf_amount, cf_source)

    # 3. Update / load daily universe
    existing_universe = load_json(universe_path)
    u_cfg = cfg.settings.get("universe", {})
    universe, _ = ensure_universe(
        client=okx,
        existing_universe=existing_universe,
        top_n=int(u_cfg.get("top_n", 100)),
        min_listed_days=int(u_cfg.get("min_listed_days", 30)),
        l2_symbols=u_cfg.get("l2_symbols"),
        l4_symbols=u_cfg.get("l4_symbols"),
        now=dt_now,
    )

    # Refresh latest ticker prices
    tickers = okx.get_tickers("SWAP")
    last_prices: dict[str, float] = {}
    for t in tickers:
        inst_id = t.get("instId", "")
        try:
            last_prices[inst_id] = float(t.get("last") or 0.0)
        except (TypeError, ValueError):
            pass

    # 4. Check control.json kill switches
    can_enter = not cfg.control.paused and not cfg.control.close_all
    if cfg.control.close_all:
        logger.warning("control.json close_all=true：市價平掉本機器人所有倉位")
        for pos in list(ledger.get("positions", [])):
            px = last_prices.get(pos["inst_id"], float(pos["entry_price"]))
            execute_exit(
                okx, ledger, pos, px,
                reason="緊急開關 close_all 觸發市價全平",
                dry_run=cfg.dry_run,
            )

    # Check position mode before any new entries
    if can_enter and not ensure_position_mode(okx, ledger, cfg.dry_run):
        can_enter = False
        errors.append("無法切換為雙向持倉 (long_short_mode)，暫停本次新進場")

    # 5. Fetch candles & evaluate strategies
    cache = CandleCache(okx)
    strat_cfg = cfg.settings.get("strategies", {})
    acc_cfg = cfg.settings.get("account", {})

    top_100: list[str] = universe.get("top_100", [])
    l2_list: list[str] = universe.get("l2", [])
    l4_list: list[str] = universe.get("l4", [])
    inst_meta_map: dict[str, dict[str, Any]] = universe.get("instruments", {})

    # Open positions lookup by (strategy, inst_id)
    open_by_key: dict[tuple[str, str], dict[str, Any]] = {
        (p["strategy"], p["inst_id"]): p for p in ledger.get("positions", [])
    }

    # --- 4H Strategy: S2 ---
    s2_targets = sorted(set(top_100) | {p["inst_id"] for p in ledger.get("positions", []) if p["strategy"] == "S2"})
    s2_candles: dict[str, list[Candle]] = {}
    latest_4h_ts = 0
    for inst_id in s2_targets:
        try:
            cs = cache.get_closed_candles(inst_id, "4H", min_bars=120)
            s2_candles[inst_id] = cs
            if cs and cs[-1].ts > latest_4h_ts:
                latest_4h_ts = cs[-1].ts
        except OKXAPIError as exc:
            errors.append(mask_secrets(f"S2 抓取 {inst_id} 4H K 線失敗: {exc}", secrets))

    last_4h_ts = int(ledger.get("last_processed_bar", {}).get("4H", 0))
    new_4h_closed = (latest_4h_ts > 0) if force_rescan else (latest_4h_ts > last_4h_ts)

    signals_summary: dict[str, dict[str, int]] = {
        s: {"enter": 0, "exit": 0, "update_sl": 0, "rejected": 0}
        for s in ("S2", "S3", "L1", "L2", "L3", "L4")
    }

    all_decisions: list[StrategyDecision] = []

    if new_4h_closed:
        for inst_id in s2_targets:
            cs = s2_candles.get(inst_id, [])
            if len(cs) < 120:
                continue
            pos = open_by_key.get(("S2", inst_id))
            pos_view = _to_open_view(pos) if pos else None
            # If falling out of top 100 and not held, do not open new position
            if pos is None and inst_id not in top_100:
                continue
            dec = evaluate_s2(inst_id, cs, pos_view, strat_cfg.get("S2"))
            all_decisions.append(dec)

    # --- 1Dutc Strategies: S3, L1, L2, L3, L4 ---
    daily_held = {p["inst_id"] for p in ledger.get("positions", []) if p["strategy"] != "S2"}
    daily_targets = sorted(set(top_100) | set(l2_list) | set(l4_list) | daily_held)
    daily_candles: dict[str, list[Candle]] = {}
    latest_1d_ts = 0
    for inst_id in daily_targets:
        try:
            cs = cache.get_closed_candles(inst_id, "1Dutc", min_bars=260)
            daily_candles[inst_id] = cs
            if cs and cs[-1].ts > latest_1d_ts:
                latest_1d_ts = cs[-1].ts
        except OKXAPIError as exc:
            errors.append(mask_secrets(f"抓取 {inst_id} 日線失敗: {exc}", secrets))

    last_1d_ts = int(ledger.get("last_processed_bar", {}).get("1Dutc", 0))
    new_1d_closed = (latest_1d_ts > 0) if force_rescan else (latest_1d_ts > last_1d_ts)

    if new_1d_closed:
        # S3 & L1 (top 100 + held)
        for strat_name, eval_fn in (("S3", evaluate_s3), ("L1", evaluate_l1)):
            strat_targets = sorted(
                set(top_100) | {p["inst_id"] for p in ledger.get("positions", []) if p["strategy"] == strat_name}
            )
            for inst_id in strat_targets:
                cs = daily_candles.get(inst_id, [])
                if len(cs) < 260:
                    continue
                pos = open_by_key.get((strat_name, inst_id))
                if pos is None and inst_id not in top_100:
                    continue
                dec = eval_fn(inst_id, cs, _to_open_view(pos) if pos else None, strat_cfg.get(strat_name))
                all_decisions.append(dec)

        # L2 (fixed 10 + held)
        l2_targets = sorted(
            set(l2_list) | {p["inst_id"] for p in ledger.get("positions", []) if p["strategy"] == "L2"}
        )
        for inst_id in l2_targets:
            cs = daily_candles.get(inst_id, [])
            if len(cs) < 260:
                continue
            pos = open_by_key.get(("L2", inst_id))
            if pos is None and inst_id not in l2_list:
                continue
            dec = evaluate_l2(inst_id, cs, _to_open_view(pos) if pos else None, strat_cfg.get("L2"))
            all_decisions.append(dec)

        # L3 (portfolio dual momentum on top 100)
        l3_candles = {
            inst_id: daily_candles[inst_id]
            for inst_id in top_100
            if len(daily_candles.get(inst_id, [])) >= 260
        }
        l3_open = {
            p["inst_id"]: _to_open_view(p)
            for p in ledger.get("positions", [])
            if p["strategy"] == "L3"
        }
        all_decisions.extend(evaluate_l3_portfolio(l3_candles, l3_open, strat_cfg.get("L3")))

        # L4 (fixed BTC/ETH + held)
        l4_targets = sorted(
            set(l4_list) | {p["inst_id"] for p in ledger.get("positions", []) if p["strategy"] == "L4"}
        )
        for inst_id in l4_targets:
            cs = daily_candles.get(inst_id, [])
            if len(cs) < 260:
                continue
            pos = open_by_key.get(("L4", inst_id))
            if pos is None and inst_id not in l4_list:
                continue
            dec = evaluate_l4(inst_id, cs, _to_open_view(pos) if pos else None, strat_cfg.get("L4"))
            all_decisions.append(dec)

    # 6. Process EXIT & UPDATE_SL first (frees up risk & slots), then ENTER
    if not cfg.control.close_all:
        for dec in all_decisions:
            if dec.action == "EXIT":
                pos = open_by_key.get((dec.strategy, dec.inst_id))
                if pos:
                    signals_summary[dec.strategy]["exit"] += 1
                    px = last_prices.get(dec.inst_id, float(pos["entry_price"]))
                    execute_exit(okx, ledger, pos, px, dec.reason, cfg.dry_run)
            elif dec.action == "UPDATE_SL" and dec.stop_loss is not None:
                pos = open_by_key.get((dec.strategy, dec.inst_id))
                if pos:
                    signals_summary[dec.strategy]["update_sl"] += 1
                    meta = inst_meta_map.get(dec.inst_id, {})
                    execute_amend_sl(okx, ledger, pos, dec.stop_loss, meta, dec.reason, cfg.dry_run)

        if can_enter:
            now_ms_entry = int(dt_now.timestamp() * 1000)
            suspended_until = ledger.get("suspended_until", {})
            for dec in all_decisions:
                if dec.action != "ENTER" or dec.stop_loss is None:
                    continue
                if (dec.strategy, dec.inst_id) in open_by_key:
                    continue
                if int(suspended_until.get(dec.inst_id, 0)) > now_ms_entry:
                    logger.info("標的 %s 因流動性不足在 7 天暫停期內，略過進場", dec.inst_id)
                    continue
                signals_summary[dec.strategy]["enter"] += 1
                ref_px = last_prices.get(dec.inst_id, 0.0)
                meta = inst_meta_map.get(dec.inst_id, {})
                sub = ledger["subaccounts"][dec.strategy]
                all_open = [_to_existing_risk(p) for p in ledger.get("positions", [])]
                sub_open = [p for p in all_open if p.strategy == dec.strategy]
                # Dynamic capital allocation based on live OKX USDT equity
                alloc_ratio = float(sub.get("allocation") or 0.0)
                live_total_eq = current_equity if current_equity > 0 else float(ledger["initial_equity_usdt"])
                live_sub_eq = live_total_eq * alloc_ratio
                sizing = allocate_subaccount_position(
                    strategy=dec.strategy,
                    inst_id=dec.inst_id,
                    pos_side=dec.pos_side,
                    ref_price=ref_px,
                    stop_loss=dec.stop_loss,
                    risk_factor=dec.risk_factor,
                    initial_sub_equity=live_sub_eq,
                    sub_realized_pnl=0.0,
                    sub_positions=sub_open,
                    all_open_positions=all_open,
                    initial_total_equity=live_total_eq,
                    total_realized_pnl=0.0,
                    inst_meta=meta,
                    account_cfg=acc_cfg,
                )
                if not sizing.approved:
                    signals_summary[dec.strategy]["rejected"] += 1
                    rej = {
                        "ts": int(dt_now.timestamp() * 1000),
                        "strategy": dec.strategy,
                        "inst_id": dec.inst_id,
                        "pos_side": dec.pos_side,
                        "reason": sizing.reason,
                    }
                    ledger.setdefault("rejected_signals", []).append(rej)
                    ledger["rejected_signals"] = ledger["rejected_signals"][-100:]
                    logger.info("訊號被風控拒絕 %s %s：%s", dec.strategy, dec.inst_id, sizing.reason)
                    continue

                created = execute_entry(
                    client=okx,
                    ledger=ledger,
                    strategy=dec.strategy,
                    inst_id=dec.inst_id,
                    pos_side=dec.pos_side,
                    sz=sizing.sz,
                    ref_price=ref_px,
                    stop_loss=dec.stop_loss,
                    planned_risk=sizing.planned_risk,
                    notional=sizing.notional,
                    leverage=sizing.leverage,
                    inst_meta=meta,
                    bar_ts=dec.bar_ts,
                    reason=dec.reason,
                    dry_run=cfg.dry_run,
                    errors=errors,
                    max_sub_notional=live_sub_eq * float(acc_cfg.get("subaccount_max_notional_mult", 5.0)),
                )
                if created and not cfg.dry_run:
                    open_by_key[(dec.strategy, dec.inst_id)] = created

    # Update last processed bar timestamps
    if new_4h_closed and latest_4h_ts > 0:
        ledger.setdefault("last_processed_bar", {})["4H"] = latest_4h_ts
    if new_1d_closed and latest_1d_ts > 0:
        ledger.setdefault("last_processed_bar", {})["1Dutc"] = latest_1d_ts

    # Record equity history point & latest snapshot
    if current_equity > 0:
        now_ms = int(dt_now.timestamp() * 1000)
        ledger.setdefault("equity_history", []).append(
            {"ts": now_ms, "equity": round(current_equity, 4)}
        )
        ledger["equity_history"] = ledger["equity_history"][-5000:]
        final_realized = sum(
            float(s.get("realized_pnl", 0.0)) for s in ledger.get("subaccounts", {}).values()
        )
        ledger["last_equity_snapshot"] = {
            "ts": now_ms,
            "equity": round(current_equity, 6),
            "realized_pnl": round(final_realized, 6),
            "unrealized_pnl": round(current_upl, 6),
        }

    # Save state & run log
    save_json(ledger_path, ledger)
    save_json(universe_path, universe)

    # Compute bars_held map for open positions
    bars_held_map: dict[str, int] = {}
    for pos in ledger.get("positions", []):
        cs = (
            s2_candles.get(pos["inst_id"], [])
            if pos["strategy"] == "S2"
            else daily_candles.get(pos["inst_id"], [])
        )
        bars_held_map[pos["id"]] = count_bars_held(cs, int(pos["entry_ts"]))

    run_entry = {
        "ts": dt_now.isoformat(),
        "dry_run": cfg.dry_run,
        "paused": cfg.control.paused,
        "close_all": cfg.control.close_all,
        "equity_usdt": current_equity,
        "new_4h_closed": new_4h_closed,
        "new_1d_closed": new_1d_closed,
        "reconciled_closed": recon.get("reconciled_closed", 0),
        "signals": signals_summary,
        "errors": errors,
    }
    append_run_log(run_log_path, run_entry, max_lines=500)

    # Generate website data JSONs
    generate_site_data(
        data_dir=data_dir,
        ledger=ledger,
        current_equity=current_equity,
        last_prices=last_prices,
        bars_held_map=bars_held_map,
        cfg=cfg,
        run_entry=run_entry,
        now=dt_now,
    )

    return run_entry


def main() -> int:
    parser = argparse.ArgumentParser(description="OKX Demo Trading Bot")
    parser.add_argument(
        "--local",
        action="store_true",
        help="Local mode: write outputs to local_output/ without touching state/ or data/",
    )
    parser.add_argument(
        "--force-rescan",
        action="store_true",
        help="Ignore last_processed_bar and re-evaluate latest closed 4H and 1Dutc bars.",
    )
    args = parser.parse_args()

    cfg = load_config()
    enforce_demo_mode({"TRADING_MODE": cfg.trading_mode})
    result = run_cycle(cfg, local_mode=args.local, force_rescan=args.force_rescan)
    logger.info(
        "執行完成：equity=%.2f dry_run=%s signals=%s",
        result["equity_usdt"],
        result["dry_run"],
        result["signals"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
