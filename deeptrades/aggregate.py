"""Aggregation of individual fills into "Deep Trades".

A deep trade is the set of fills that share coin, taker and side and whose
timestamps lie within `window_ms` of the group's *first* fill
(``fill.time - first.time <= window_ms``, i.e. the boundary is inclusive).
The window is anchored at the first fill so a continuously trading taker is
split into consecutive deep trades instead of one unbounded chain. Fills of
other takers in between do not interrupt a group.

Taker attribution: for side "B" the buyer is the taker, otherwise the seller.
"""

from __future__ import annotations

import bisect
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Iterable

from .models import Fill


@dataclass
class DeepTrade:
    id: str
    coin: str
    side: str  # "B" (taker bought) or "A" (taker sold)
    taker: str
    start_time: int
    end_time: int
    vwap: float
    sz: float
    notional: float
    fills: int
    makers: int
    px_min: float
    px_max: float
    px_first: float
    px_last: float
    absorption: bool | None = None  # None = not evaluated / not yet decidable

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Builder:
    coin: str
    side: str
    taker: str
    start_time: int
    first_tid: int
    end_time: int = 0
    sz: float = 0.0
    notional: float = 0.0
    fills: int = 0
    px_min: float = float("inf")
    px_max: float = float("-inf")
    px_first: float = 0.0
    px_last: float = 0.0
    makers: set[str] = field(default_factory=set)
    closed: bool = False

    @classmethod
    def start(cls, f: Fill) -> "_Builder":
        b = cls(coin=f.coin, side=f.side, taker=f.taker, start_time=f.time, first_tid=f.tid,
                px_first=f.px)
        b.add(f)
        return b

    def add(self, f: Fill) -> None:
        self.end_time = max(self.end_time, f.time)
        self.sz += f.sz
        self.notional += f.px * f.sz
        self.fills += 1
        self.px_min = min(self.px_min, f.px)
        self.px_max = max(self.px_max, f.px)
        self.px_last = f.px
        self.makers.add(f.maker)

    def build(self) -> DeepTrade:
        return DeepTrade(
            id=f"{self.coin}-{self.first_tid}",
            coin=self.coin,
            side=self.side,
            taker=self.taker,
            start_time=self.start_time,
            end_time=self.end_time,
            vwap=self.notional / self.sz if self.sz else self.px_last,
            sz=self.sz,
            notional=self.notional,
            fills=self.fills,
            makers=len(self.makers),
            px_min=self.px_min,
            px_max=self.px_max,
            px_first=self.px_first,
            px_last=self.px_last,
        )


class StreamingAggregator:
    """Incremental aggregator. Feed fills in (roughly) time order."""

    def __init__(self, window_ms: int = 500):
        self.window_ms = int(window_ms)
        self._open: dict[tuple[str, str, str], _Builder] = {}
        self._order: deque[_Builder] = deque()

    def add(self, f: Fill) -> list[DeepTrade]:
        """Add a fill; returns deep trades closed as a consequence."""
        out = self._expire(f.time)
        key = (f.coin, f.taker, f.side)
        b = self._open.get(key)
        if b is not None and f.time - b.start_time <= self.window_ms:
            b.add(f)
            return out
        if b is not None:
            b.closed = True
            out.append(b.build())
        nb = _Builder.start(f)
        self._open[key] = nb
        self._order.append(nb)
        return out

    def _expire(self, now_ms: int) -> list[DeepTrade]:
        out: list[DeepTrade] = []
        while self._order and (self._order[0].closed
                               or now_ms - self._order[0].start_time > self.window_ms):
            b = self._order.popleft()
            if b.closed:
                continue
            b.closed = True
            self._open.pop((b.coin, b.taker, b.side), None)
            out.append(b.build())
        return out

    def flush(self, now_ms: int | None = None) -> list[DeepTrade]:
        """Close groups whose window ended before `now_ms` (all groups if None)."""
        if now_ms is not None:
            return self._expire(now_ms)
        out = [b.build() for b in self._order if not b.closed]
        self._order.clear()
        self._open.clear()
        return out

    def open_trades(self) -> list[DeepTrade]:
        return [b.build() for b in self._order if not b.closed]


def aggregate(fills: Iterable[Fill], window_ms: int = 500) -> list[DeepTrade]:
    """Aggregate fills (any order) into deep trades, sorted by start time."""
    agg = StreamingAggregator(window_ms)
    out: list[DeepTrade] = []
    for f in sorted(fills, key=lambda x: (x.time, x.tid)):
        out.extend(agg.add(f))
    out.extend(agg.flush())
    out.sort(key=lambda d: (d.start_time, d.id))
    return out


def absorption_decision(
    dt: DeepTrade, price_at_horizon: float | None, tolerance_bps: float = 0.0
) -> bool:
    """True if price after the horizon did not move beyond the trade's extreme.

    Buy: absorbed if price_at_horizon <= px_max * (1 + tol).
    Sell: absorbed if price_at_horizon >= px_min * (1 - tol).
    Without any later trade the price is taken as unchanged (px_last).
    """
    px = dt.px_last if price_at_horizon is None else price_at_horizon
    tol = tolerance_bps / 10_000.0
    if dt.side == "B":
        return px <= dt.px_max * (1 + tol)
    return px >= dt.px_min * (1 - tol)


class FillSeries(list):
    """List of fills sorted by (time, tid) with a parallel `times` list for bisecting."""

    def __init__(self, fills: Iterable[Fill] = ()):
        super().__init__(sorted(fills, key=lambda x: (x.time, x.tid)))
        self.times = [f.time for f in self]


def price_at(fills: FillSeries, after_ms: int, at_ms: int) -> float | None:
    """Price of the last fill with after_ms < time <= at_ms, or None."""
    hi = bisect.bisect_right(fills.times, at_ms)
    if hi == 0:
        return None
    f = fills[hi - 1]
    return f.px if f.time > after_ms else None


def evaluate_absorption(
    trades: list[DeepTrade],
    fills: FillSeries,
    *,
    min_notional: float,
    horizon_s: float,
    tolerance_bps: float = 0.0,
    data_until_ms: int | None = None,
) -> None:
    """Set `absorption` in-place for trades with notional >= min_notional.

    Trades below the threshold get False. Trades whose horizon lies beyond
    `data_until_ms` (end of known data) stay None (undecidable yet).
    """
    horizon_ms = int(horizon_s * 1000)
    for dt in trades:
        if dt.notional < min_notional:
            dt.absorption = False
            continue
        at = dt.end_time + horizon_ms
        if data_until_ms is not None and at > data_until_ms:
            dt.absorption = None
            continue
        dt.absorption = absorption_decision(dt, price_at(fills, dt.end_time, at), tolerance_bps)
