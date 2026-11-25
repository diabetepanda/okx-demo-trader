"""Candle fetching, closed-bar filtering, caching, and bar counting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from bot.okx_client import OKXClient


@dataclass(frozen=True)
class Candle:
    ts: int  # milliseconds UTC open timestamp
    open: float
    high: float
    low: float
    close: float
    vol: float
    confirm: str


class CandleCache:
    """Per-run in-memory cache for closed candles."""

    def __init__(self, client: OKXClient) -> None:
        self._client = client
        self._cache: dict[tuple[str, str], list[Candle]] = {}

    def get_closed_candles(
        self,
        inst_id: str,
        bar: str,
        min_bars: int,
        max_bars: int = 1440,
    ) -> list[Candle]:
        key = (inst_id, bar)
        if key in self._cache:
            return self._cache[key]

        raw_rows: list[list[str]] = []
        after: Optional[str] = None
        target = min(max_bars, max(min_bars + 10, 300))

        while len(raw_rows) < target:
            batch = self._client.get_candles(
                inst_id=inst_id,
                bar=bar,
                limit=300,
                after=after,
            )
            if not batch:
                break
            raw_rows.extend(batch)
            if len(batch) < 300:
                break
            oldest_ts = batch[-1][0]
            if oldest_ts == after:
                break
            after = oldest_ts

        # Filter only confirmed (closed) candles and deduplicate by ts
        by_ts: dict[int, Candle] = {}
        for row in raw_rows:
            if len(row) < 9:
                continue
            confirm = str(row[8])
            if confirm != "1":
                continue
            ts = int(row[0])
            by_ts[ts] = Candle(
                ts=ts,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                vol=float(row[5]),
                confirm=confirm,
            )

        # Sort oldest-to-newest
        candles = [by_ts[ts] for ts in sorted(by_ts.keys())]
        self._cache[key] = candles
        return candles


def count_bars_held(candles: list[Candle], entry_ts_ms: int) -> int:
    """Count closed candles whose open timestamp is strictly after entry_ts_ms."""
    if not candles:
        return 0
    return sum(1 for c in candles if c.ts > entry_ts_ms)


def is_utc_sunday_candle(candle: Candle) -> bool:
    """Return True if the 1Dutc candle's UTC date is Sunday (weekday == 6)."""
    dt = datetime.fromtimestamp(candle.ts / 1000.0, tz=timezone.utc)
    return dt.weekday() == 6
