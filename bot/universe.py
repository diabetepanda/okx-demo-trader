"""Universe builder: top-100 USDT linear perpetual swaps on OKX Demo Trading."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from bot.okx_client import OKXClient

logger = logging.getLogger(__name__)


def _utc_today_str(now: Optional[datetime] = None) -> str:
    dt = now or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _extract_inst_meta(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "instId": item.get("instId", ""),
        "ctVal": float(item.get("ctVal") or 1.0),
        "lotSz": float(item.get("lotSz") or 1.0),
        "minSz": float(item.get("minSz") or 1.0),
        "tickSz": float(item.get("tickSz") or 0.0001),
        "listTime": int(item.get("listTime") or 0),
    }


def build_universe(
    client: OKXClient,
    top_n: int = 100,
    min_listed_days: int = 30,
    l2_symbols: Optional[list[str]] = None,
    l4_symbols: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Fetch and filter OKX SWAP instruments into today's trading universe."""
    dt_now = now or datetime.now(timezone.utc)
    now_ms = int(dt_now.timestamp() * 1000)
    min_list_ms = now_ms - min_listed_days * 86400 * 1000

    public_insts = client.get_instruments("SWAP")
    account_insts = client.get_account_instruments("SWAP")
    tickers = client.get_tickers("SWAP")

    demo_tradable_map: dict[str, dict[str, Any]] = {}
    for item in account_insts:
        inst_id = item.get("instId", "")
        if (
            item.get("settleCcy") == "USDT"
            and item.get("ctType") == "linear"
            and item.get("state") == "live"
            and inst_id.endswith("-USDT-SWAP")
        ):
            demo_tradable_map[inst_id] = _extract_inst_meta(item)

    ticker_vol_usdt: dict[str, float] = {}
    ticker_last: dict[str, float] = {}
    for t in tickers:
        inst_id = t.get("instId", "")
        try:
            last_px = float(t.get("last") or 0.0)
            vol_ccy_24h = float(t.get("volCcy24h") or 0.0)
        except (TypeError, ValueError):
            continue
        ticker_last[inst_id] = last_px
        ticker_vol_usdt[inst_id] = vol_ccy_24h * last_px

    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for item in public_insts:
        inst_id = item.get("instId", "")
        if not (
            item.get("settleCcy") == "USDT"
            and item.get("ctType") == "linear"
            and item.get("state") == "live"
            and inst_id.endswith("-USDT-SWAP")
        ):
            continue
        try:
            list_time = int(item.get("listTime") or 0)
        except (TypeError, ValueError):
            list_time = 0
        if list_time <= 0 or list_time > min_list_ms:
            continue
        vol_usdt = ticker_vol_usdt.get(inst_id, 0.0)
        if vol_usdt <= 0:
            continue
        candidates.append((vol_usdt, inst_id, _extract_inst_meta(item)))

    candidates.sort(key=lambda x: (-x[0], x[1]))

    top_list: list[str] = []
    instruments_meta: dict[str, dict[str, Any]] = {}
    skipped_not_demo: list[str] = []

    for vol_usdt, inst_id, pub_meta in candidates:
        if inst_id not in demo_tradable_map:
            skipped_not_demo.append(inst_id)
            logger.info("標的 %s 不在模擬盤可交易清單，已剔除", inst_id)
            continue
        top_list.append(inst_id)
        meta = dict(demo_tradable_map[inst_id])
        meta["vol24hUsdt"] = vol_usdt
        meta["last"] = ticker_last.get(inst_id, 0.0)
        instruments_meta[inst_id] = meta
        if len(top_list) >= top_n:
            break

    # Resolve fixed L2 & L4 lists against demo tradable instruments
    l2_raw = l2_symbols or ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC"]
    l4_raw = l4_symbols or ["BTC", "ETH"]

    def _resolve_fixed(symbols: list[str], name: str) -> list[str]:
        resolved: list[str] = []
        for sym in symbols:
            inst_id = sym if sym.endswith("-USDT-SWAP") else f"{sym}-USDT-SWAP"
            if inst_id in demo_tradable_map:
                resolved.append(inst_id)
                if inst_id not in instruments_meta:
                    meta = dict(demo_tradable_map[inst_id])
                    meta["vol24hUsdt"] = ticker_vol_usdt.get(inst_id, 0.0)
                    meta["last"] = ticker_last.get(inst_id, 0.0)
                    instruments_meta[inst_id] = meta
            else:
                logger.warning("%s 固定標的 %s 不在模擬盤可交易清單，略過", name, inst_id)
        return resolved

    l2_list = _resolve_fixed(l2_raw, "L2")
    l4_list = _resolve_fixed(l4_raw, "L4")

    return {
        "date": _utc_today_str(dt_now),
        "updated_at": dt_now.isoformat(),
        "top_100": top_list,
        "l2": l2_list,
        "l4": l4_list,
        "instruments": instruments_meta,
        "skipped_not_demo": skipped_not_demo,
    }


def ensure_universe(
    client: OKXClient,
    existing_universe: Optional[dict[str, Any]],
    top_n: int = 100,
    min_listed_days: int = 30,
    l2_symbols: Optional[list[str]] = None,
    l4_symbols: Optional[list[str]] = None,
    now: Optional[datetime] = None,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Return today's universe, updating only once per UTC date unless forced."""
    today = _utc_today_str(now)
    if (
        not force_refresh
        and existing_universe
        and existing_universe.get("date") == today
        and existing_universe.get("top_100")
    ):
        return existing_universe, False

    universe = build_universe(
        client=client,
        top_n=top_n,
        min_listed_days=min_listed_days,
        l2_symbols=l2_symbols,
        l4_symbols=l4_symbols,
        now=now,
    )
    return universe, True
