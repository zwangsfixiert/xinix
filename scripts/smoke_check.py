#!/usr/bin/env python3
"""Check the live Hyperliquid API against the field structure this project expects.

Read-only: public endpoints only, no keys. Exit code 0 if everything matches.

    python scripts/smoke_check.py [COIN]
"""

import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deeptrades.models import parse_trade  # noqa: E402

WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"
TRADE_KEYS = {"coin", "side", "px", "sz", "hash", "time", "tid", "users"}
CANDLE_KEYS = {"t", "T", "s", "i", "o", "c", "h", "l", "v", "n"}


def post(payload):
    req = urllib.request.Request(INFO_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def check_keys(name, obj, expected) -> bool:
    keys = set(obj)
    missing, extra = expected - keys, keys - expected
    print(f"  {name}: keys={sorted(keys)}")
    if missing:
        print(f"  !! {name}: missing {sorted(missing)}")
    if extra:
        print(f"  (info) {name}: additional keys {sorted(extra)}")
    return not missing


async def ws_check(coin: str) -> bool:
    async with websockets.connect(WS_URL, ping_interval=None) as ws:
        await ws.send(json.dumps({"method": "subscribe",
                                  "subscription": {"type": "trades", "coin": coin}}))
        await ws.send(json.dumps({"method": "ping"}))
        seen_pong = seen_trades = False
        ok = True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (seen_pong and seen_trades):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
            ch = msg.get("channel")
            if ch == "pong":
                seen_pong = True
                print("  pong ok")
            elif ch == "trades" and msg.get("data"):
                seen_trades = True
                t = msg["data"][0]
                print("  sample trade:", json.dumps(t))
                ok &= check_keys("ws trade", t, TRADE_KEYS)
                f = parse_trade(t)
                print(f"  parsed: side={f.side} taker={f.taker} maker={f.maker}")
            else:
                print(f"  channel {ch!r}")
        return ok and seen_pong and seen_trades


def main() -> int:
    coin = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    ok = True
    print("recentTrades:")
    rt = post({"type": "recentTrades", "coin": coin})
    ok &= isinstance(rt, list) and bool(rt) and check_keys("recentTrades[0]", rt[0], TRADE_KEYS)
    print("candleSnapshot:")
    now = int(time.time() * 1000)
    cs = post({"type": "candleSnapshot",
               "req": {"coin": coin, "interval": "1m", "startTime": now - 600_000, "endTime": now}})
    ok &= isinstance(cs, list) and bool(cs) and check_keys("candle[0]", cs[0], CANDLE_KEYS)
    print("websocket trades:")
    ok &= asyncio.run(ws_check(coin))
    print("RESULT:", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
