"""FastAPI service: candles proxy, historical deep trades and live stream.

Run: uvicorn deeptrades.api:app   (or python -m deeptrades.api)
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import db
from .aggregate import (DeepTrade, FillSeries, StreamingAggregator, absorption_decision,
                        aggregate, evaluate_absorption, price_at)
from .config import Config, load_config
from .models import Fill

log = logging.getLogger("deeptrades.api")

# Intervals supported by Hyperliquid's candleSnapshot.
INTERVALS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
    "4h": 14400, "8h": 28800, "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
    "1M": 2592000,
}
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
FRONTEND_FILE = WEB_DIR / "deep-trades-hyperliquid.html"


def now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    """Read-only access to the collector's SQLite file (one connection per thread call)."""

    def __init__(self, path: str):
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    def _get(self) -> sqlite3.Connection:
        if self._conn is None:
            if not Path(self.path).exists():
                raise HTTPException(503, f"database {self.path} not found (collector running?)")
            self._conn = db.connect(self.path, readonly=True)
        return self._conn

    async def call(self, fn, *args):
        async with self._lock:
            conn = self._get()
            return await asyncio.to_thread(fn, conn, *args)


def build_deep_trades(cfg: Config, fills: list[Fill], start: int, end: int,
                      min_notional: float, data_until: int) -> list[DeepTrade]:
    acfg = cfg.aggregation
    series = FillSeries(fills)
    trades = [t for t in aggregate(series, acfg.window_ms) if start <= t.start_time <= end]
    if acfg.absorption_enabled and trades:
        evaluate_absorption(
            trades, series, min_notional=acfg.absorption_threshold(trades[0].coin),
            horizon_s=acfg.absorption_horizon_s, tolerance_bps=acfg.absorption_tolerance_bps,
            data_until_ms=data_until)
    return [t for t in trades if t.notional >= min_notional]


# --------------------------------------------------------------------------- live hub
class CoinFeed:
    """Tails the fills table for one coin and fans out live deep trades."""

    GRACE_MS = 1500  # wait for late fills before closing a window

    def __init__(self, coin: str, cfg: Config, database: Database):
        self.coin = coin
        self.cfg = cfg
        self.db = database
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.agg = StreamingAggregator(cfg.aggregation.window_ms)
        self.recent = FillSeries()
        self.pending: list[DeepTrade] = []  # waiting for absorption horizon

    def publish(self, msg: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("slow websocket client on %s, dropping message", self.coin)

    async def run(self) -> None:
        acfg = self.cfg.aggregation
        horizon_ms = int(acfg.absorption_horizon_s * 1000)
        threshold = acfg.absorption_threshold(self.coin)
        last_rowid = await self.db.call(db.max_rowid)
        log.info("live feed %s started at rowid %d", self.coin, last_rowid)
        while True:
            try:
                rows = await self.db.call(db.fetch_fills_after_rowid, self.coin, last_rowid)
            except Exception as exc:  # noqa: BLE001
                log.warning("live feed %s: %s", self.coin, exc)
                await asyncio.sleep(2)
                continue
            closed: list[DeepTrade] = []
            if rows:
                last_rowid = rows[-1][0]
                fills = sorted((f for _, f in rows), key=lambda f: (f.time, f.tid))
                for f in fills:
                    closed.extend(self.agg.add(f))
                    self.recent.append(f)
                    self.recent.times.append(f.time)
                self.publish({"type": "fills", "coin": self.coin,
                              "data": [[f.time, f.px, f.sz, f.side] for f in fills]})
            closed.extend(self.agg.flush(now_ms() - self.GRACE_MS))
            for dt in closed:
                if acfg.absorption_enabled and dt.notional >= threshold:
                    self.pending.append(dt)
                elif acfg.absorption_enabled:
                    dt.absorption = False
                self.publish({"type": "deeptrade", "data": dt.to_dict()})
            # resolve absorption once the horizon has passed (plus grace)
            if self.pending:
                cutoff = now_ms() - self.GRACE_MS
                still = []
                for dt in self.pending:
                    at = dt.end_time + horizon_ms
                    if at > cutoff:
                        still.append(dt)
                        continue
                    dt.absorption = absorption_decision(
                        dt, price_at(self.recent, dt.end_time, at), acfg.absorption_tolerance_bps)
                    self.publish({"type": "deeptrade", "data": dt.to_dict()})
                self.pending = still
            self._trim_recent(horizon_ms)
            await asyncio.sleep(self.cfg.api.live_poll_interval_s)

    def _trim_recent(self, horizon_ms: int) -> None:
        keep_from = now_ms() - horizon_ms - 60_000
        if self.recent.times and self.recent.times[0] < keep_from:
            i = bisect.bisect_left(self.recent.times, keep_from)
            del self.recent[:i]
            del self.recent.times[:i]


class LiveHub:
    def __init__(self, cfg: Config, database: Database):
        self.cfg = cfg
        self.db = database
        self.feeds: dict[str, CoinFeed] = {}

    def subscribe(self, coin: str) -> asyncio.Queue:
        feed = self.feeds.get(coin)
        if feed is None:
            feed = self.feeds[coin] = CoinFeed(coin, self.cfg, self.db)
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        feed.subscribers.add(q)
        if feed.task is None or feed.task.done():
            feed.task = asyncio.create_task(feed.run())
        return q

    def unsubscribe(self, coin: str, q: asyncio.Queue) -> None:
        feed = self.feeds.get(coin)
        if feed is None:
            return
        feed.subscribers.discard(q)
        if not feed.subscribers and feed.task:
            feed.task.cancel()
            del self.feeds[coin]

    async def close(self) -> None:
        for feed in self.feeds.values():
            if feed.task:
                feed.task.cancel()
        self.feeds.clear()


# --------------------------------------------------------------------------- app
def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    database = Database(cfg.db_path)
    hub = LiveHub(cfg, database)
    candle_cache: dict[tuple, tuple[float, list]] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = httpx.AsyncClient(timeout=15.0)
        yield
        await hub.close()
        await app.state.http.aclose()

    app = FastAPI(title="Deep Trades (Hyperliquid)", version="0.1.0", lifespan=lifespan)
    if cfg.api.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=cfg.api.cors_origins,
                           allow_methods=["GET"], allow_headers=["*"])

    def check_coin(coin: str) -> str:
        coin = coin.strip()
        if coin not in cfg.collector.coins:
            raise HTTPException(400, f"coin must be one of {cfg.collector.coins}")
        return coin

    @app.get("/config")
    async def get_config():
        a = cfg.aggregation
        return {"coins": cfg.collector.coins, "intervals": list(INTERVALS),
                "window_ms": a.window_ms, "absorption_enabled": a.absorption_enabled,
                "absorption_horizon_s": a.absorption_horizon_s,
                "absorption_min_notional": {c: a.absorption_threshold(c)
                                            for c in cfg.collector.coins},
                "max_range_hours": cfg.api.max_range_hours}

    @app.get("/health")
    async def health():
        try:
            row = await database.call(
                lambda c: c.execute("SELECT MAX(time), COUNT(*) FROM "
                                    "(SELECT time FROM fills ORDER BY rowid DESC LIMIT 1000)")
                .fetchone())
        except HTTPException as exc:
            return {"ok": False, "error": exc.detail}
        last = row[0]
        return {"ok": True, "last_fill_time": last,
                "last_fill_age_s": None if last is None else round((now_ms() - last) / 1000, 1)}

    @app.get("/candles")
    async def candles(coin: str, interval: str = "1m",
                      start: int = Query(..., description="ms since epoch"),
                      end: int | None = Query(None, description="ms since epoch")):
        """Proxy for Hyperliquid's candleSnapshot (response passed through unchanged)."""
        coin = check_coin(coin)
        if interval not in INTERVALS:
            raise HTTPException(400, f"interval must be one of {list(INTERVALS)}")
        end = end or now_ms()
        if end <= start:
            raise HTTPException(400, "end must be after start")
        key = (coin, interval, start // 1000, end // 5000)
        hit = candle_cache.get(key)
        if hit and time.monotonic() - hit[0] < 2.0:
            return hit[1]
        payload = {"type": "candleSnapshot",
                   "req": {"coin": coin, "interval": interval, "startTime": start, "endTime": end}}
        try:
            r = await app.state.http.post(cfg.api.info_url, json=payload)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"upstream error: {exc}") from exc
        if not isinstance(data, list):
            raise HTTPException(502, f"unexpected upstream response: {str(data)[:200]}")
        if len(candle_cache) > 256:
            candle_cache.clear()
        candle_cache[key] = (time.monotonic(), data)
        return data

    @app.get("/deeptrades")
    async def deeptrades(coin: str,
                         start: int = Query(..., description="ms since epoch"),
                         end: int | None = Query(None, description="ms since epoch"),
                         min_notional: float = Query(0.0, ge=0),
                         taker: str | None = Query(None, description="filter by taker address")):
        coin = check_coin(coin)
        end = end or now_ms()
        if end <= start:
            raise HTTPException(400, "end must be after start")
        if end - start > cfg.api.max_range_hours * 3600_000:
            raise HTTPException(400, f"range exceeds {cfg.api.max_range_hours}h")
        a = cfg.aggregation
        # margins: earlier fills shape group boundaries, later fills decide absorption
        lo = start - 10 * a.window_ms
        hi = end + a.window_ms + int(a.absorption_horizon_s * 1000)
        fills = await database.call(db.fetch_fills, coin, lo, hi)
        data_until = min(hi, now_ms() - CoinFeed.GRACE_MS)
        trades = await asyncio.to_thread(build_deep_trades, cfg, fills, start, end,
                                         min_notional, data_until)
        if taker:
            t = taker.strip().lower()
            trades = [d for d in trades if d.taker == t]
        return {"coin": coin, "start": start, "end": end, "window_ms": a.window_ms,
                "count": len(trades), "data": [d.to_dict() for d in trades]}

    @app.websocket("/stream")
    async def stream(ws: WebSocket, coin: str):
        if coin not in cfg.collector.coins:
            await ws.close(code=4400, reason="unknown coin")
            return
        await ws.accept()
        q = hub.subscribe(coin)
        await ws.send_json({"type": "hello", "coin": coin,
                            "window_ms": cfg.aggregation.window_ms})

        async def drain_client():
            # read and ignore client messages so disconnects are noticed
            while True:
                await ws.receive_text()

        reader = asyncio.create_task(drain_client())
        try:
            while True:
                getter = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({getter, reader}, timeout=20,
                                             return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    getter.cancel()
                    break
                if getter in done:
                    await ws.send_json(getter.result())
                else:
                    getter.cancel()
                    await ws.send_json({"type": "ping", "t": now_ms()})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            reader.cancel()
            hub.unsubscribe(coin, q)

    if cfg.api.serve_frontend:
        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(FRONTEND_FILE)

    return app


def _lazy_app() -> FastAPI:
    from .collector import setup_logging
    cfg = load_config()
    setup_logging(cfg.log_level)
    return create_app(cfg)


app = _lazy_app()


def main() -> None:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="Deep Trades API")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    cfg = load_config(args.config)
    uvicorn.run(create_app(cfg), host=cfg.api.host, port=cfg.api.port,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
