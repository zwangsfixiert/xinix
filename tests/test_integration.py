"""Integration tests with a local stand-in for the Hyperliquid websocket.

The stand-in emits messages in the documented Hyperliquid format; it does not
replace a check against the real API (see README, "Smoke test").
"""

import asyncio
import json
import time

import websockets
from fastapi.testclient import TestClient

from deeptrades import db
from deeptrades.api import create_app
from deeptrades.collector import Collector
from deeptrades.config import Config
from deeptrades.models import Fill


def trade(coin, tid, t, px, sz, side, buyer, seller):
    return {"coin": coin, "side": side, "px": str(px), "sz": str(sz), "hash": "0xh",
            "time": t, "tid": tid, "users": [buyer, seller]}


def test_collector_against_local_ws(tmp_path):
    subs, pings = [], []

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") == "ping":
                pings.append(msg)
                await ws.send(json.dumps({"channel": "pong"}))
            elif msg.get("method") == "subscribe":
                subs.append(msg["subscription"])
                await ws.send(json.dumps({"channel": "subscriptionResponse", "data": msg}))
                coin = msg["subscription"]["coin"]
                now = int(time.time() * 1000)
                data = [trade(coin, 1, now, 10, 1, "B", "0xA", "0xB"),
                        trade(coin, 2, now, 11, 2, "A", "0xC", "0xD")]
                await ws.send(json.dumps({"channel": "trades", "data": data}))
                await ws.send(json.dumps({"channel": "trades", "data": data[:1]}))  # duplicate

    async def scenario():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = Config(db_path=str(tmp_path / "c.sqlite"))
            cfg.collector.ws_url = f"ws://127.0.0.1:{port}"
            cfg.collector.coins = ["BTC", "ETH"]
            cfg.collector.backfill_on_connect = False
            cfg.collector.ping_interval_s = 0.2
            col = Collector(cfg)
            task = asyncio.create_task(col.run())
            await asyncio.sleep(1.0)
            col.stop_event.set()
            await asyncio.wait_for(task, 5)
            return col

    col = asyncio.run(scenario())
    assert [s["coin"] for s in subs] == ["BTC", "ETH"]
    assert all(s["type"] == "trades" for s in subs)
    assert pings, "collector must send application-level pings"
    conn = db.connect(str(tmp_path / "c.sqlite"))
    rows = conn.execute("SELECT coin, tid, buyer, seller, side FROM fills ORDER BY coin, tid").fetchall()
    assert rows == [("BTC", 1, "0xa", "0xb", "B"), ("BTC", 2, "0xc", "0xd", "A"),
                    ("ETH", 1, "0xa", "0xb", "B"), ("ETH", 2, "0xc", "0xd", "A")]
    assert col.stats["inserted"] == 4


def test_api_deeptrades_and_stream(tmp_path):
    path = str(tmp_path / "a.sqlite")
    conn = db.connect(path)
    t0 = 1_700_000_000_000
    fills = [Fill("BTC", t0 + i * 100, 100 + i, 1000.0, "B", "0xtaker", f"0xm{i % 2}", "0xh", i)
             for i in range(4)]  # 4 fills within 300 ms -> one deep trade
    fills.append(Fill("BTC", t0 + 50, 99.0, 1.0, "A", "0xm9", "0xother", "0xh", 99))
    db.insert_fills(conn, fills)

    cfg = Config(db_path=path)
    cfg.aggregation.absorption_min_notional = 100_000
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/deeptrades", params={"coin": "BTC", "start": t0, "end": t0 + 1000})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 2
        big = max(body["data"], key=lambda d: d["notional"])
        assert big["taker"] == "0xtaker" and big["fills"] == 4 and big["makers"] == 2
        assert abs(big["vwap"] - 101.5) < 1e-9
        assert big["absorption"] is True  # no later trades above px_max

        r = client.get("/deeptrades", params={"coin": "BTC", "start": t0, "end": t0 + 1000,
                                              "min_notional": 1000})
        assert r.json()["count"] == 1
        r = client.get("/deeptrades", params={"coin": "BTC", "start": t0, "end": t0 + 1000,
                                              "taker": "0xOTHER"})
        assert [d["taker"] for d in r.json()["data"]] == ["0xother"]
        assert client.get("/deeptrades", params={"coin": "XYZ", "start": t0}).status_code == 400
        assert client.get("/candles", params={"coin": "BTC", "interval": "7m",
                                              "start": t0}).status_code == 400

        with client.websocket_connect("/stream?coin=BTC") as ws:
            assert ws.receive_json()["type"] == "hello"
            now = int(time.time() * 1000)
            db.insert_fills(conn, [Fill("BTC", now, 50.0, 2.0, "A", "0xb", "0xlive", "0xh", 1000)])
            got = {}
            while "deeptrade" not in got:
                msg = ws.receive_json()
                got[msg["type"]] = msg
            assert got["fills"]["data"][0][1:] == [50.0, 2.0, "A"]
            assert got["deeptrade"]["data"]["taker"] == "0xlive"
