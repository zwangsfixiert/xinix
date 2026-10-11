"""SQLite storage for raw fills (WAL mode)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable, Sequence

from .models import Fill

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    coin   TEXT    NOT NULL,
    time   INTEGER NOT NULL,
    px     REAL    NOT NULL,
    sz     REAL    NOT NULL,
    side   TEXT    NOT NULL,
    buyer  TEXT    NOT NULL,
    seller TEXT    NOT NULL,
    hash   TEXT    NOT NULL,
    tid    INTEGER NOT NULL,
    UNIQUE (coin, tid)
);
CREATE INDEX IF NOT EXISTS idx_fills_coin_time ON fills (coin, time);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills (time);
"""

FILL_COLUMNS = "coin, time, px, sz, side, buyer, seller, hash, tid"


def connect(db_path: str, readonly: bool = False) -> sqlite3.Connection:
    """Open the database; the writer creates schema and enables WAL."""
    if readonly:
        uri = f"file:{Path(db_path).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        return conn
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def insert_fills(conn: sqlite3.Connection, fills: Iterable[Fill]) -> int:
    """Insert fills, ignoring duplicates (same coin+tid). Returns rows inserted."""
    rows = [(f.coin, f.time, f.px, f.sz, f.side, f.buyer, f.seller, f.hash, f.tid) for f in fills]
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO fills ({FILL_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()
    return conn.total_changes - before


def _row_to_fill(row: Sequence) -> Fill:
    return Fill(*row)


def fetch_fills(conn: sqlite3.Connection, coin: str, start_ms: int, end_ms: int) -> list[Fill]:
    """Fills of `coin` with start_ms <= time <= end_ms, ordered by time then tid."""
    cur = conn.execute(
        f"SELECT {FILL_COLUMNS} FROM fills WHERE coin=? AND time BETWEEN ? AND ? "
        "ORDER BY time, tid",
        (coin, start_ms, end_ms),
    )
    return [_row_to_fill(r) for r in cur]


def fetch_fills_after_rowid(
    conn: sqlite3.Connection, coin: str, after_rowid: int, limit: int = 20000
) -> list[tuple[int, Fill]]:
    cur = conn.execute(
        f"SELECT rowid, {FILL_COLUMNS} FROM fills WHERE coin=? AND rowid>? ORDER BY rowid LIMIT ?",
        (coin, after_rowid, limit),
    )
    return [(r[0], _row_to_fill(r[1:])) for r in cur]


def max_rowid(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM fills").fetchone()
    return int(row[0])


def last_price_before(conn: sqlite3.Connection, coin: str, at_ms: int) -> float | None:
    row = conn.execute(
        "SELECT px FROM fills WHERE coin=? AND time<=? ORDER BY time DESC, tid DESC LIMIT 1",
        (coin, at_ms),
    ).fetchone()
    return None if row is None else float(row[0])


def delete_older_than(
    conn: sqlite3.Connection, retention_days: float, chunk: int = 50_000
) -> int:
    """Delete fills older than the retention period in chunks. Returns rows deleted."""
    cutoff = int((time.time() - retention_days * 86400) * 1000)
    total = 0
    while True:
        cur = conn.execute(
            "DELETE FROM fills WHERE rowid IN (SELECT rowid FROM fills WHERE time<? LIMIT ?)",
            (cutoff, chunk),
        )
        conn.commit()
        total += cur.rowcount
        if cur.rowcount < chunk:
            break
    return total
