"""State persistence for ledger.json, universe.json, and run_log.jsonl."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STRATEGIES: tuple[str, ...] = ("S2", "S3", "L1", "L2", "L3", "L4")
DEFAULT_ALLOCATIONS: dict[str, float] = {
    "S2": 0.10,
    "S3": 0.10,
    "L1": 0.20,
    "L2": 0.20,
    "L3": 0.20,
    "L4": 0.20,
}


def init_ledger(
    initial_equity_usdt: float,
    allocations: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Create a fresh ledger initialized with OKX demo account equity."""
    allocs = allocations or DEFAULT_ALLOCATIONS
    subaccounts: dict[str, dict[str, Any]] = {}
    for name in STRATEGIES:
        ratio = float(allocs.get(name, DEFAULT_ALLOCATIONS[name]))
        subaccounts[name] = {
            "allocation": ratio,
            "initial_equity": round(initial_equity_usdt * ratio, 6),
            "realized_pnl": 0.0,
        }
    return {
        "version": 1,
        "initial_equity_usdt": float(initial_equity_usdt),
        "position_mode_set": False,
        "last_processed_bar": {"4H": 0, "1Dutc": 0},
        "subaccounts": subaccounts,
        "positions": [],
        "closed_trades": [],
        "processed_cl_ord_ids": [],
        "equity_history": [],
        "dry_run_orders": [],
        "rejected_signals": [],
    }


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def append_run_log(path: Path, entry: dict[str, Any], max_lines: int = 500) -> None:
    """Append one JSONL record and keep only the latest `max_lines` entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.exists():
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    lines.append(json.dumps(entry, ensure_ascii=False))
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def record_cl_ord_id(ledger: dict[str, Any], cl_ord_id: str, max_keep: int = 2000) -> None:
    ids: list[str] = ledger.setdefault("processed_cl_ord_ids", [])
    if cl_ord_id not in ids:
        ids.append(cl_ord_id)
    if len(ids) > max_keep:
        ledger["processed_cl_ord_ids"] = ids[-max_keep:]
