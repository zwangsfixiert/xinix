"""Hyperliquid trades collector.

Subscribes to the public `trades` channel for the configured coins and writes
every fill into SQLite. Read-only, no API keys.

Run: python -m deeptrades.collector [--config config.toml]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import sqlite3
import time
import urllib.request

import websockets

from . import db
from .config import Config, load_config
from .models import Fill, ParseError, parse_trade

log = logging.getLogger("deeptrades.collector")


class Collector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ccfg = cfg.collector
        self.queue: asyncio.Queue[Fill] = asyncio.Queue(maxsize=200_000)
        self.conn: sqlite3.Connection | None = None
        self.stop_event = asyncio.Event()
        self.last_msg_at = 0.0
        self.stats = {"received": 0, "inserted": 0, "parse_errors": 0, "reconnects": 0}

    # ------------------------------------------------------------------ ws
    async def run_ws(self) -> None:
        backoff = self.ccfg.backoff_initial_s
        while not self.stop_event.is_set():
            connected_at = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                log.warning("websocket session ended: %s: %s", type(exc).__name__, exc)
            if self.stop_event.is_set():
                break
            if time.monotonic() - connected_at > 60:
                backoff = self.ccfg.backoff_initial_s  # was stable, reset backoff
            delay = min(backoff, self.ccfg.backoff_max_s) * (0.5 + random.random() / 2)
            self.stats["reconnects"] += 1
            log.info("reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.ccfg.backoff_max_s)

    async def _session(self) -> None:
        url = self.ccfg.ws_url
        log.info("connecting to %s", url)
        # Hyperliquid expects application-level {"method":"ping"}; disable protocol pings.
        async with websockets.connect(url, ping_interval=None, max_size=2**24,
                                      open_timeout=20, close_timeout=5) as ws:
            for coin in self.ccfg.coins:
                await ws.send(json.dumps(
                    {"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}))
            log.info("connected, subscribed to trades for %s", ", ".join(self.ccfg.coins))
            self.last_msg_at = time.monotonic()
            if self.ccfg.backfill_on_connect:
                asyncio.create_task(self._backfill())
            pinger = asyncio.create_task(self._pinger(ws))
            try:
                async for raw in ws:
                    self.last_msg_at = time.monotonic()
                    self._handle(raw)
                    if self.stop_event.is_set():
                        break
            finally:
                pinger.cancel()

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(self.ccfg.ping_interval_s)
            idle = time.monotonic() - self.last_msg_at
            if idle > self.ccfg.stale_timeout_s:
                log.warning("no message for %.0fs, forcing reconnect", idle)
                await ws.close(code=4000, reason="stale")
                return
            await ws.send(json.dumps({"method": "ping"}))

    def _handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("non-JSON message: %.200r", raw)
            return
        channel = msg.get("channel")
        if channel == "trades":
            self._enqueue(msg.get("data") or [])
        elif channel == "pong":
            log.debug("pong")
        elif channel == "subscriptionResponse":
            log.debug("subscription ack: %s", msg.get("data"))
        elif channel == "error":
            log.error("server error: %s", msg.get("data"))
        else:
            log.debug("ignored message on channel %r", channel)

    def _enqueue(self, items: list) -> None:
        for obj in items:
            try:
                fill = parse_trade(obj)
            except ParseError as exc:
                self.stats["parse_errors"] += 1
                log.warning("%s", exc)
                continue
            self.stats["received"] += 1
            try:
                self.queue.put_nowait(fill)
            except asyncio.QueueFull:
                log.error("write queue full, dropping fill %s", fill.tid)

    async def _backfill(self) -> None:
        """Insert recentTrades for each coin (dedup via UNIQUE(coin, tid))."""
        info_url = self.cfg.api.info_url
        for coin in self.ccfg.coins:
            try:
                data = await asyncio.to_thread(_post_json, info_url,
                                               {"type": "recentTrades", "coin": coin})
                self._enqueue(data if isinstance(data, list) else [])
                log.info("backfill %s: %d recent trades", coin, len(data or []))
            except Exception as exc:  # noqa: BLE001
                log.warning("backfill %s failed: %s", coin, exc)

    # -------------------------------------------------------------- writer
    async def run_writer(self) -> None:
        assert self.conn is not None
        batch: list[Fill] = []
        while True:
            try:
                fill = await asyncio.wait_for(self.queue.get(), timeout=self.ccfg.flush_interval_s)
                batch.append(fill)
                while len(batch) < self.ccfg.flush_max_rows and not self.queue.empty():
                    batch.append(self.queue.get_nowait())
            except asyncio.TimeoutError:
                pass
            if batch and (len(batch) >= self.ccfg.flush_max_rows or self.queue.empty()):
                await self._flush(batch)
                batch = []
            if self.stop_event.is_set() and self.queue.empty():
                if batch:
                    await self._flush(batch)
                return

    async def _flush(self, batch: list[Fill]) -> None:
        for attempt in range(5):
            try:
                n = await asyncio.to_thread(db.insert_fills, self.conn, batch)
                self.stats["inserted"] += n
                return
            except sqlite3.OperationalError as exc:
                log.warning("db write failed (%s), retry %d", exc, attempt + 1)
                await asyncio.sleep(0.5 * (attempt + 1))
        log.error("dropping batch of %d fills after repeated db errors", len(batch))

    # ----------------------------------------------------------- retention
    async def run_retention(self) -> None:
        while not self.stop_event.is_set():
            try:
                n = await asyncio.to_thread(db.delete_older_than, self.conn,
                                            self.cfg.retention_days)
                if n:
                    log.info("retention: deleted %d fills older than %s days",
                             n, self.cfg.retention_days)
            except sqlite3.Error as exc:
                log.warning("retention failed: %s", exc)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=3600)
            except asyncio.TimeoutError:
                pass

    async def run_stats(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            log.info("stats: %s queue=%d", self.stats, self.queue.qsize())

    # ---------------------------------------------------------------- main
    async def run(self) -> None:
        self.conn = db.connect(self.cfg.db_path)
        log.info("database %s (WAL), retention %s days", self.cfg.db_path, self.cfg.retention_days)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass
        writer = asyncio.create_task(self.run_writer())
        aux = [asyncio.create_task(self.run_retention()), asyncio.create_task(self.run_stats())]
        ws_task = asyncio.create_task(self.run_ws())
        await self.stop_event.wait()
        log.info("shutting down")
        ws_task.cancel()
        for t in aux:
            t.cancel()
        await asyncio.gather(ws_task, *aux, return_exceptions=True)
        await writer  # drains the queue
        self.conn.close()
        log.info("stopped: %s", self.stats)


def _post_json(url: str, payload: dict, timeout: float = 15.0):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)


def main() -> None:
    ap = argparse.ArgumentParser(description="Hyperliquid trades collector")
    ap.add_argument("--config", default=None, help="path to config.toml")
    args = ap.parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg.log_level)
    asyncio.run(Collector(cfg).run())


if __name__ == "__main__":
    main()
