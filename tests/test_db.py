import time

from deeptrades import db
from deeptrades.models import Fill


def make(tid, t, coin="BTC"):
    return Fill(coin, t, 100.0, 1.0, "B", "0xb", "0xs", "0xh", tid)


def test_wal_dedup_and_query(tmp_path):
    conn = db.connect(str(tmp_path / "t.sqlite"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.insert_fills(conn, [make(1, 1000), make(2, 2000)]) == 2
    assert db.insert_fills(conn, [make(2, 2000), make(3, 3000)]) == 1  # tid 2 deduplicated
    assert db.insert_fills(conn, [make(3, 3000, coin="ETH")]) == 1     # other coin
    assert [f.tid for f in db.fetch_fills(conn, "BTC", 1500, 3000)] == [2, 3]


def test_retention(tmp_path):
    conn = db.connect(str(tmp_path / "t.sqlite"))
    now = int(time.time() * 1000)
    db.insert_fills(conn, [make(1, now - 40 * 86400_000), make(2, now)])
    assert db.delete_older_than(conn, 30) == 1
    assert [f.tid for f in db.fetch_fills(conn, "BTC", 0, now)] == [2]
