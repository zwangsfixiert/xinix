"""Fill model and parsing of Hyperliquid trade messages.

Hyperliquid's `trades` subscription (and the `recentTrades` info request) deliver
objects of the documented shape (WsTrade)::

    {"coin": "BTC", "side": "B", "px": "62000.0", "sz": "0.01",
     "hash": "0x...", "time": 1718000000000, "tid": 123456789,
     "users": ["0xbuyer...", "0xseller..."]}

There are no separate `buyer`/`seller` fields: `users` is `[buyer, seller]`.
`side` is the aggressor side ("B" = taker bought, "A" = taker sold).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Fill:
    coin: str
    time: int  # ms since epoch
    px: float
    sz: float
    side: str  # "B" or "A"
    buyer: str
    seller: str
    hash: str
    tid: int

    @property
    def taker(self) -> str:
        return self.buyer if self.side == "B" else self.seller

    @property
    def maker(self) -> str:
        return self.seller if self.side == "B" else self.buyer

    @property
    def notional(self) -> float:
        return self.px * self.sz


class ParseError(ValueError):
    pass


def parse_trade(obj: dict) -> Fill:
    """Convert one Hyperliquid trade object into a Fill."""
    try:
        users = obj["users"]
        if not isinstance(users, (list, tuple)) or len(users) != 2:
            raise ParseError(f"unexpected users field: {users!r}")
        side = obj["side"]
        if side not in ("A", "B"):
            raise ParseError(f"unexpected side: {side!r}")
        return Fill(
            coin=str(obj["coin"]),
            time=int(obj["time"]),
            px=float(obj["px"]),
            sz=float(obj["sz"]),
            side=side,
            buyer=str(users[0]).lower(),
            seller=str(users[1]).lower(),
            hash=str(obj.get("hash", "")),
            tid=int(obj["tid"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ParseError):
            raise
        raise ParseError(f"cannot parse trade {obj!r}: {exc}") from exc
