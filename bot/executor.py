"""Order execution, stop-loss protection, and OKX reconciliation."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from bot.ledger import record_cl_ord_id, record_dry_run_cl_ord_id
from bot.okx_client import OKXAPIError, OKXClient
from bot.sizing import round_price_to_tick

logger = logging.getLogger(__name__)


def make_cl_ord_id(strategy: str, inst_id: str, bar_ts: int) -> str:
    """Build deterministic alphanumeric clOrdId <= 32 chars to prevent duplicate orders."""
    coin = inst_id.split("-")[0]
    raw = f"{strategy}{coin}{bar_ts}"
    cleaned = re.sub(r"[^A-Za-z0-9]", "", raw)
    return cleaned[:32]


def make_algo_cl_ord_id(strategy: str, inst_id: str, bar_ts: int) -> str:
    coin = inst_id.split("-")[0]
    raw = f"SL{strategy}{coin}{bar_ts}"
    cleaned = re.sub(r"[^A-Za-z0-9]", "", raw)
    return cleaned[:32]


def _compute_pnl(
    pos_side: str,
    entry_price: float,
    exit_price: float,
    sz: float,
    ct_val: float,
    entry_fee: float,
    exit_fee: float,
) -> float:
    if pos_side == "long":
        gross = (exit_price - entry_price) * sz * ct_val
    else:
        gross = (entry_price - exit_price) * sz * ct_val
    # OKX fees are negative for rebates/costs (e.g. -0.05 means 0.05 USDT cost)
    total_fee_cost = abs(entry_fee) + abs(exit_fee)
    return gross - total_fee_cost


def _find_exit_fill(
    pos: dict[str, Any],
    fills: list[dict[str, Any]],
    positions_history: list[dict[str, Any]],
) -> tuple[float, float, int]:
    """Find best matching exit price, fee, and timestamp from OKX fills or history."""
    inst_id = pos["inst_id"]
    pos_side = pos["pos_side"]
    entry_ts = int(pos.get("entry_ts", 0))
    close_side = "sell" if pos_side == "long" else "buy"

    matching_fills = []
    for f in fills:
        if f.get("instId") != inst_id:
            continue
        if f.get("side") != close_side:
            continue
        if f.get("posSide") and f.get("posSide") != pos_side:
            continue
        ts = int(f.get("ts") or 0)
        if ts >= entry_ts:
            matching_fills.append((ts, f))

    if matching_fills:
        matching_fills.sort(key=lambda x: x[0], reverse=True)
        latest_ts, latest_fill = matching_fills[0]
        px = float(latest_fill.get("fillPx") or pos.get("stop_loss") or pos["entry_price"])
        fee = abs(float(latest_fill.get("fee") or 0.0))
        return px, fee, latest_ts

    for h in positions_history:
        if h.get("instId") == inst_id and h.get("posSide") == pos_side:
            u_time = int(h.get("uTime") or 0)
            if u_time >= entry_ts:
                px = float(h.get("closeAvgPx") or pos.get("stop_loss") or pos["entry_price"])
                return px, 0.0, u_time

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    fallback_px = float(pos.get("stop_loss") or pos["entry_price"])
    return fallback_px, 0.0, now_ms


def reconcile_positions(
    client: OKXClient,
    ledger: dict[str, Any],
) -> dict[str, Any]:
    """Reconcile ledger positions against live OKX positions, pending algos, and fills."""
    okx_positions = client.get_positions("SWAP")
    pending_algos = client.get_pending_algos("conditional", "SWAP")
    fills = client.get_fills("SWAP", limit=100)
    pos_history = client.get_positions_history("SWAP", limit=100)

    live_pos_map: dict[tuple[str, str], float] = {}
    for p in okx_positions:
        inst_id = p.get("instId", "")
        pos_side = p.get("posSide", "")
        sz = abs(float(p.get("pos") or 0.0))
        if sz > 0:
            key = (inst_id, pos_side)
            live_pos_map[key] = live_pos_map.get(key, 0.0) + sz

    pending_algo_ids: set[str] = set()
    for a in pending_algos:
        if a.get("algoId"):
            pending_algo_ids.add(str(a["algoId"]))
        if a.get("algoClOrdId"):
            pending_algo_ids.add(str(a["algoClOrdId"]))

    # Track remaining live size per (instId, posSide) to detect closed or extra positions
    remaining_live = dict(live_pos_map)
    still_open: list[dict[str, Any]] = []
    closed_count = 0

    for pos in ledger.get("positions", []):
        if pos.get("ord_id") == "DRY_RUN":
            still_open.append(pos)
            continue

        key = (pos["inst_id"], pos["pos_side"])
        live_sz = remaining_live.get(key, 0.0)
        pos_sz = float(pos["sz"])
        algo_id = str(pos.get("algo_id") or "")
        algo_cl_id = str(pos.get("algo_cl_ord_id") or "")
        has_pending_sl = (algo_id in pending_algo_ids) or (algo_cl_id in pending_algo_ids)

        if live_sz >= pos_sz - 1e-9 and (has_pending_sl or live_sz > 0):
            # Position still exists on OKX
            remaining_live[key] = max(0.0, live_sz - pos_sz)
            still_open.append(pos)
        else:
            # Position no longer exists on OKX (e.g. SL triggered)
            exit_px, exit_fee, exit_ts = _find_exit_fill(pos, fills, pos_history)
            ct_val = float(pos.get("ct_val") or 1.0)
            entry_fee = float(pos.get("fee") or 0.0)
            pnl = _compute_pnl(
                pos["pos_side"],
                float(pos["entry_price"]),
                exit_px,
                pos_sz,
                ct_val,
                entry_fee,
                exit_fee,
            )
            planned_risk = float(pos.get("planned_risk") or 0.0)
            r_mult = pnl / planned_risk if planned_risk > 0 else 0.0

            strat = pos["strategy"]
            if strat in ledger.get("subaccounts", {}):
                ledger["subaccounts"][strat]["realized_pnl"] = (
                    float(ledger["subaccounts"][strat].get("realized_pnl", 0.0)) + pnl
                )

            ledger.setdefault("closed_trades", []).append(
                {
                    "strategy": strat,
                    "inst_id": pos["inst_id"],
                    "pos_side": pos["pos_side"],
                    "sz": pos_sz,
                    "entry_price": float(pos["entry_price"]),
                    "exit_price": exit_px,
                    "entry_ts": int(pos["entry_ts"]),
                    "exit_ts": exit_ts,
                    "pnl_usdt": round(pnl, 6),
                    "r_multiple": round(r_mult, 4),
                    "fee": round(abs(entry_fee) + abs(exit_fee), 6),
                    "exit_reason": "交易所止損觸發（對帳平倉）",
                }
            )
            closed_count += 1
            logger.info(
                "對帳平倉：%s %s %s 出場價=%.6g 損益=%.2f USDT",
                strat, pos["inst_id"], pos["pos_side"], exit_px, pnl,
            )

    ledger["positions"] = still_open

    # Warn about any OKX positions not opened by this bot
    for (inst_id, pos_side), extra_sz in remaining_live.items():
        if extra_sz > 1e-9:
            logger.warning(
                "偵測到非本機器人開啟的倉位：%s %s (剩餘張數 %.4g)，依規則不予碰觸",
                inst_id, pos_side, extra_sz,
            )

    return {
        "okx_positions": okx_positions,
        "pending_algos": pending_algos,
        "fills": fills,
        "positions_history": pos_history,
        "reconciled_closed": closed_count,
    }


def ensure_position_mode(client: OKXClient, ledger: dict[str, Any], dry_run: bool) -> bool:
    """Ensure OKX account uses long_short_mode. Returns True if ready to trade."""
    if ledger.get("position_mode_set"):
        return True
    if dry_run:
        return True
    try:
        client.set_position_mode("long_short_mode")
        ledger["position_mode_set"] = True
        logger.info("已設定 OKX 持倉模式為雙向持倉 (long_short_mode)")
        return True
    except OKXAPIError as exc:
        logger.error(
            "無法切換為雙向持倉 (long_short_mode)，可能帳戶已有未平倉位或掛單，本次停止下單：%s",
            exc,
        )
        return False


def execute_entry(
    client: OKXClient,
    ledger: dict[str, Any],
    strategy: str,
    inst_id: str,
    pos_side: str,
    sz: float,
    ref_price: float,
    stop_loss: float,
    planned_risk: float,
    notional: float,
    leverage: int,
    inst_meta: dict[str, Any],
    bar_ts: int,
    reason: str,
    dry_run: bool,
) -> Optional[dict[str, Any]]:
    """Place market entry order with attached stop-loss (or record DRY_RUN)."""
    cl_ord_id = make_cl_ord_id(strategy, inst_id, bar_ts)
    if dry_run:
        if cl_ord_id in ledger.get("dry_run_cl_ord_ids", []):
            logger.info("[DRY_RUN] 略過重複訊號 clOrdId=%s", cl_ord_id)
            return None
    else:
        if cl_ord_id in ledger.get("processed_cl_ord_ids", []):
            logger.info("略過重複訊號 clOrdId=%s", cl_ord_id)
            return None

    tick_sz = float(inst_meta.get("tickSz") or 0.0001)
    ct_val = float(inst_meta.get("ctVal") or 1.0)
    sl_px = round_price_to_tick(stop_loss, tick_sz, pos_side)
    side = "buy" if pos_side == "long" else "sell"
    algo_cl_id = make_algo_cl_ord_id(strategy, inst_id, bar_ts)

    if dry_run:
        dry_record = {
            "ts": int(datetime.now(timezone.utc).timestamp() * 1000),
            "action": "ENTER",
            "strategy": strategy,
            "inst_id": inst_id,
            "pos_side": pos_side,
            "side": side,
            "sz": sz,
            "ref_price": ref_price,
            "stop_loss": sl_px,
            "planned_risk": round(planned_risk, 4),
            "notional": round(notional, 4),
            "leverage": leverage,
            "cl_ord_id": cl_ord_id,
            "reason": reason,
        }
        ledger.setdefault("dry_run_orders", []).append(dry_record)
        ledger["dry_run_orders"] = ledger["dry_run_orders"][-200:]
        record_dry_run_cl_ord_id(ledger, cl_ord_id)
        logger.info("[DRY_RUN] 模擬進場：%s %s %s sz=%g sl=%g", strategy, inst_id, pos_side, sz, sl_px)
        return dry_record

    # Live demo order
    try:
        for d in ("long", "short"):
            client.set_leverage(inst_id, leverage, mgn_mode="cross", pos_side=d)
    except OKXAPIError as exc:
        logger.error("設定槓桿失敗 %s %s：%s", strategy, inst_id, exc)
        return None

    order_body = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": side,
        "posSide": pos_side,
        "ordType": "market",
        "sz": str(sz),
        "clOrdId": cl_ord_id,
        "attachAlgoOrds": [
            {
                "attachAlgoClOrdId": algo_cl_id,
                "slTriggerPx": str(sl_px),
                "slOrdPx": "-1",
                "slTriggerPxType": "last",
            }
        ],
    }

    try:
        res = client.place_order(order_body)
    except OKXAPIError as exc:
        logger.error("進場下單失敗 %s %s %s：%s", strategy, inst_id, pos_side, exc)
        return None

    ord_id = str(res[0].get("ordId", "")) if res else ""
    record_cl_ord_id(ledger, cl_ord_id)

    # Query fill details
    avg_px = ref_price
    filled_sz = sz
    fee = 0.0
    fill_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        ord_info = client.get_order(inst_id, ord_id=ord_id or None, cl_ord_id=cl_ord_id)
        if ord_info:
            avg_px = float(ord_info[0].get("avgPx") or ref_price)
            filled_sz = float(ord_info[0].get("accFillSz") or sz)
            fee = abs(float(ord_info[0].get("fee") or 0.0))
            if ord_info[0].get("fillTime"):
                fill_ts = int(ord_info[0]["fillTime"])
    except OKXAPIError as exc:
        logger.warning("查詢成交詳情失敗 %s %s：%s", inst_id, ord_id, exc)

    # Verify stop-loss algo order exists on exchange
    algo_id = ""
    sl_verified = False
    try:
        algos = client.get_pending_algos("conditional", "SWAP")
        for a in algos:
            if a.get("instId") == inst_id and a.get("algoClOrdId") == algo_cl_id:
                algo_sz = float(a.get("sz") or 0.0)
                if abs(algo_sz - filled_sz) <= 1e-6:
                    algo_id = str(a.get("algoId", ""))
                    sl_verified = True
                    break
    except OKXAPIError as exc:
        logger.warning("查詢止損掛單失敗 %s：%s", inst_id, exc)

    # If SL missing, place standalone algo SL; if that fails, immediately market-close!
    if not sl_verified:
        logger.warning("附帶止損單未確認存在，嘗試補掛獨立止損單：%s %s", inst_id, pos_side)
        close_side = "sell" if pos_side == "long" else "buy"
        try:
            algo_res = client.place_algo_order(
                {
                    "instId": inst_id,
                    "tdMode": "cross",
                    "side": close_side,
                    "posSide": pos_side,
                    "ordType": "conditional",
                    "sz": str(filled_sz),
                    "reduceOnly": True,
                    "slTriggerPx": str(sl_px),
                    "slOrdPx": "-1",
                    "slTriggerPxType": "last",
                    "algoClOrdId": algo_cl_id,
                }
            )
            algo_id = str(algo_res[0].get("algoId", "")) if algo_res else ""
            sl_verified = True
        except OKXAPIError as exc:
            logger.error("補掛止損失敗，立即市價平倉保護 %s %s：%s", inst_id, pos_side, exc)
            try:
                client.place_order(
                    {
                        "instId": inst_id,
                        "tdMode": "cross",
                        "side": close_side,
                        "posSide": pos_side,
                        "ordType": "market",
                        "sz": str(filled_sz),
                        "reduceOnly": True,
                    }
                )
            except OKXAPIError as close_exc:
                logger.error("緊急市價平倉也失敗 %s：%s", inst_id, close_exc)
            return None

    pos_record = {
        "id": f"{strategy}_{inst_id}_{bar_ts}",
        "strategy": strategy,
        "inst_id": inst_id,
        "pos_side": pos_side,
        "sz": filled_sz,
        "entry_price": avg_px,
        "entry_ts": fill_ts,
        "entry_bar_ts": bar_ts,
        "stop_loss": sl_px,
        "initial_stop_loss": sl_px,
        "planned_risk": round(planned_risk, 6),
        "notional": round(filled_sz * avg_px * ct_val, 6),
        "ct_val": ct_val,
        "ord_id": ord_id,
        "cl_ord_id": cl_ord_id,
        "algo_id": algo_id,
        "algo_cl_ord_id": algo_cl_id,
        "fee": fee,
    }
    ledger.setdefault("positions", []).append(pos_record)
    logger.info("進場成交：%s %s %s sz=%g px=%g sl=%g", strategy, inst_id, pos_side, filled_sz, avg_px, sl_px)
    return pos_record


def execute_exit(
    client: OKXClient,
    ledger: dict[str, Any],
    pos: dict[str, Any],
    ref_price: float,
    reason: str,
    dry_run: bool,
) -> bool:
    """Cancel this position's stop-loss and market-close only its own sz."""
    strategy = pos["strategy"]
    inst_id = pos["inst_id"]
    pos_side = pos["pos_side"]
    sz = float(pos["sz"])
    close_side = "sell" if pos_side == "long" else "buy"

    if dry_run:
        ledger.setdefault("dry_run_orders", []).append(
            {
                "ts": int(datetime.now(timezone.utc).timestamp() * 1000),
                "action": "EXIT",
                "strategy": strategy,
                "inst_id": inst_id,
                "pos_side": pos_side,
                "side": close_side,
                "sz": sz,
                "ref_price": ref_price,
                "reason": reason,
            }
        )
        ledger["dry_run_orders"] = ledger["dry_run_orders"][-200:]
        logger.info("[DRY_RUN] 模擬出場：%s %s %s sz=%g (%s)", strategy, inst_id, pos_side, sz, reason)
        return True

    # Cancel own SL algo first
    algo_id = pos.get("algo_id")
    if algo_id:
        try:
            client.cancel_algo_orders([{"algoId": str(algo_id), "instId": inst_id}])
        except OKXAPIError as exc:
            logger.warning("撤銷止損單失敗（可能已觸發）%s %s：%s", inst_id, algo_id, exc)

    try:
        res = client.place_order(
            {
                "instId": inst_id,
                "tdMode": "cross",
                "side": close_side,
                "posSide": pos_side,
                "ordType": "market",
                "sz": str(sz),
                "reduceOnly": True,
            }
        )
    except OKXAPIError as exc:
        logger.error("出場平倉失敗 %s %s %s：%s", strategy, inst_id, pos_side, exc)
        return False

    ord_id = str(res[0].get("ordId", "")) if res else ""
    exit_px = ref_price
    exit_fee = 0.0
    exit_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        ord_info = client.get_order(inst_id, ord_id=ord_id or None)
        if ord_info:
            exit_px = float(ord_info[0].get("avgPx") or ref_price)
            exit_fee = abs(float(ord_info[0].get("fee") or 0.0))
            if ord_info[0].get("fillTime"):
                exit_ts = int(ord_info[0]["fillTime"])
    except OKXAPIError as exc:
        logger.warning("查詢出場成交失敗 %s：%s", inst_id, exc)

    ct_val = float(pos.get("ct_val") or 1.0)
    entry_fee = float(pos.get("fee") or 0.0)
    pnl = _compute_pnl(pos_side, float(pos["entry_price"]), exit_px, sz, ct_val, entry_fee, exit_fee)
    planned_risk = float(pos.get("planned_risk") or 0.0)
    r_mult = pnl / planned_risk if planned_risk > 0 else 0.0

    if strategy in ledger.get("subaccounts", {}):
        ledger["subaccounts"][strategy]["realized_pnl"] = (
            float(ledger["subaccounts"][strategy].get("realized_pnl", 0.0)) + pnl
        )

    ledger.setdefault("closed_trades", []).append(
        {
            "strategy": strategy,
            "inst_id": inst_id,
            "pos_side": pos_side,
            "sz": sz,
            "entry_price": float(pos["entry_price"]),
            "exit_price": exit_px,
            "entry_ts": int(pos["entry_ts"]),
            "exit_ts": exit_ts,
            "pnl_usdt": round(pnl, 6),
            "r_multiple": round(r_mult, 4),
            "fee": round(abs(entry_fee) + abs(exit_fee), 6),
            "exit_reason": reason,
        }
    )
    ledger["positions"] = [p for p in ledger.get("positions", []) if p.get("id") != pos.get("id")]
    logger.info("出場完成：%s %s %s 出場價=%.6g 損益=%.2f USDT", strategy, inst_id, pos_side, exit_px, pnl)
    return True


def execute_amend_sl(
    client: OKXClient,
    ledger: dict[str, Any],
    pos: dict[str, Any],
    new_sl: float,
    inst_meta: dict[str, Any],
    reason: str,
    dry_run: bool,
) -> bool:
    """Amend trailing stop-loss upwards only."""
    tick_sz = float(inst_meta.get("tickSz") or 0.0001)
    rounded_sl = round_price_to_tick(new_sl, tick_sz, pos["pos_side"])
    if rounded_sl <= float(pos["stop_loss"]):
        return False

    if dry_run:
        ledger.setdefault("dry_run_orders", []).append(
            {
                "ts": int(datetime.now(timezone.utc).timestamp() * 1000),
                "action": "AMEND_SL",
                "strategy": pos["strategy"],
                "inst_id": pos["inst_id"],
                "pos_side": pos["pos_side"],
                "old_sl": pos["stop_loss"],
                "new_sl": rounded_sl,
                "reason": reason,
            }
        )
        ledger["dry_run_orders"] = ledger["dry_run_orders"][-200:]
        logger.info("[DRY_RUN] 模擬上調止損：%s %s %g -> %g", pos["strategy"], pos["inst_id"], pos["stop_loss"], rounded_sl)
        return True

    algo_id = pos.get("algo_id")
    if not algo_id:
        return False

    try:
        client.amend_algo_order(
            {
                "instId": pos["inst_id"],
                "algoId": str(algo_id),
                "newSlTriggerPx": str(rounded_sl),
                "newSlOrdPx": "-1",
            }
        )
        pos["stop_loss"] = rounded_sl
        logger.info("已上調止損：%s %s -> %g", pos["strategy"], pos["inst_id"], rounded_sl)
        return True
    except OKXAPIError as exc:
        logger.error("修改止損單失敗 %s %s：%s", pos["inst_id"], algo_id, exc)
        return False
