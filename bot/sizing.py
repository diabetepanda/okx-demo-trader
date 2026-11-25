"""Position sizing and risk allocation matching Signal Desk rules."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Optional


@dataclass(frozen=True)
class ExistingPositionRisk:
    strategy: str
    inst_id: str
    pos_side: str  # "long" or "short"
    notional: float
    planned_risk: float


@dataclass(frozen=True)
class SizingResult:
    approved: bool
    sz: float = 0.0
    notional: float = 0.0
    planned_risk: float = 0.0
    leverage: int = 5
    reason: str = ""


def round_size_to_lot(raw_sz: float, lot_sz: float) -> float:
    """Floor contract size to an exact multiple of lot_sz."""
    if raw_sz <= 0 or lot_sz <= 0:
        return 0.0
    d_raw = Decimal(str(raw_sz))
    d_lot = Decimal(str(lot_sz))
    units = (d_raw / d_lot).to_integral_value(rounding=ROUND_DOWN)
    return float(units * d_lot)


def round_price_to_tick(price: float, tick_sz: float, direction: str) -> float:
    """Round stop-loss price to tickSz: long SL floors, short SL ceils."""
    if price <= 0 or tick_sz <= 0:
        return price
    d_px = Decimal(str(price))
    d_tick = Decimal(str(tick_sz))
    rounding = ROUND_DOWN if direction == "long" else ROUND_UP
    units = (d_px / d_tick).to_integral_value(rounding=rounding)
    return float(units * d_tick)


def get_leverage_cap(inst_id: str, btc_eth_lev: int = 10, default_lev: int = 5) -> int:
    if inst_id.startswith("BTC-") or inst_id.startswith("ETH-"):
        return btc_eth_lev
    return default_lev


def allocate_subaccount_position(
    strategy: str,
    inst_id: str,
    pos_side: str,
    ref_price: float,
    stop_loss: float,
    risk_factor: float,
    initial_sub_equity: float,
    sub_realized_pnl: float,
    sub_positions: list[ExistingPositionRisk],
    all_open_positions: list[ExistingPositionRisk],
    initial_total_equity: float,
    total_realized_pnl: float,
    inst_meta: dict[str, Any],
    account_cfg: Optional[dict[str, Any]] = None,
) -> SizingResult:
    """Compute contract size and enforce subaccount & account-wide risk caps."""
    cfg = account_cfg or {}
    max_positions = int(cfg.get("subaccount_max_positions", 10))
    sub_risk_pct = float(cfg.get("subaccount_risk_pct", 0.01))
    sub_max_risk_pct = float(cfg.get("subaccount_max_risk_pct", 0.10))
    sub_max_notional_mult = float(cfg.get("subaccount_max_notional_mult", 5.0))
    same_dir_max_risk_pct = float(cfg.get("account_same_dir_max_risk_pct", 0.20))
    taker_fee = float(cfg.get("taker_fee_rate", 0.0005))
    slippage_bps = float(cfg.get("slippage_bps", 1.0))
    btc_eth_lev = int(cfg.get("btc_eth_leverage", 10))
    default_lev = int(cfg.get("default_leverage", 5))

    leverage_cap = get_leverage_cap(inst_id, btc_eth_lev=btc_eth_lev, default_lev=default_lev)

    if ref_price <= 0 or stop_loss <= 0:
        return SizingResult(False, leverage=leverage_cap, reason="參考價或止損價無效（<= 0）")

    # Step 10: Check stop-loss side validity
    if pos_side == "long" and ref_price <= stop_loss:
        return SizingResult(
            False, leverage=leverage_cap,
            reason=f"目前市價 {ref_price:.6g} 已低於或等於多單止損價 {stop_loss:.6g}，拒絕進場",
        )
    if pos_side == "short" and ref_price >= stop_loss:
        return SizingResult(
            False, leverage=leverage_cap,
            reason=f"目前市價 {ref_price:.6g} 已高於或等於空單止損價 {stop_loss:.6g}，拒絕進場",
        )

    # Step 1: Subaccount equity
    sub_equity = max(0.01, initial_sub_equity + sub_realized_pnl)

    # Step 2: Max open positions per subaccount
    if len(sub_positions) >= max_positions:
        return SizingResult(
            False, leverage=leverage_cap,
            reason=f"{strategy} 子帳戶持倉數已達上限 {max_positions} 檔，拒絕進場",
        )

    # Step 3: Subaccount total risk cap (10%)
    sub_max_risk = sub_equity * sub_max_risk_pct
    used_sub_risk = sum(p.planned_risk for p in sub_positions)
    remaining_risk = sub_max_risk - used_sub_risk
    if remaining_risk <= 0:
        return SizingResult(
            False, leverage=leverage_cap,
            reason=f"{strategy} 子帳戶剩餘風險額度不足（已達 10% 上限），拒絕進場",
        )

    # Step 4: Desired risk & budget
    desired_risk = sub_equity * sub_risk_pct * risk_factor
    risk_budget = min(desired_risk, remaining_risk)

    # Step 5: Effective stop-loss %
    sl_dist_pct = max(0.002, abs(ref_price - stop_loss) / ref_price)
    cost_pct = 2.0 * (taker_fee + slippage_bps / 10000.0)
    effective_sl_pct = sl_dist_pct + cost_pct

    # Step 6: Notional & caps
    notional = risk_budget / effective_sl_pct
    current_sub_notional = sum(p.notional for p in sub_positions)
    rem_sub_notional = max(0.0, sub_equity * sub_max_notional_mult - current_sub_notional)
    notional = min(
        notional,
        sub_equity * sub_max_notional_mult,
        rem_sub_notional,
        sub_equity * leverage_cap,
    )
    if notional <= 0:
        return SizingResult(
            False, leverage=leverage_cap,
            reason=f"{strategy} 子帳戶總名目已達 5 倍上限，拒絕進場",
        )

    # Step 7: Ensure actual_risk <= remaining_risk
    actual_risk = notional * effective_sl_pct
    if actual_risk > remaining_risk:
        actual_risk = remaining_risk
        notional = actual_risk / effective_sl_pct

    # Step 8: Account-wide same-direction risk <= 20%
    account_equity = max(0.01, initial_total_equity + total_realized_pnl)
    account_dir_max_risk = account_equity * same_dir_max_risk_pct
    used_dir_risk = sum(
        p.planned_risk for p in all_open_positions if p.pos_side == pos_side
    )
    avail_dir_risk = account_dir_max_risk - used_dir_risk
    if avail_dir_risk <= 0:
        dir_zh = "多單" if pos_side == "long" else "空單"
        return SizingResult(
            False, leverage=leverage_cap,
            reason=f"全帳戶同方向（{dir_zh}）總風險已達 20% 上限，拒絕進場",
        )
    if actual_risk > avail_dir_risk:
        actual_risk = avail_dir_risk
        notional = actual_risk / effective_sl_pct

    # Step 9: Convert notional to contracts using ctVal, lotSz, minSz
    ct_val = float(inst_meta.get("ctVal") or 1.0)
    lot_sz = float(inst_meta.get("lotSz") or 1.0)
    min_sz = float(inst_meta.get("minSz") or 1.0)
    if ct_val <= 0 or math.isnan(notional):
        return SizingResult(False, leverage=leverage_cap, reason="合約面值無效")

    raw_sz = notional / (ref_price * ct_val)
    sz = round_size_to_lot(raw_sz, lot_sz)
    if sz < min_sz or sz <= 0:
        return SizingResult(
            False, leverage=leverage_cap,
            reason=f"換算張數 {sz:g} 小於最小下單量 {min_sz:g}，拒絕進場",
        )

    final_notional = sz * ref_price * ct_val
    final_risk = final_notional * effective_sl_pct
    return SizingResult(
        approved=True,
        sz=sz,
        notional=final_notional,
        planned_risk=final_risk,
        leverage=leverage_cap,
    )
