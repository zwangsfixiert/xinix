import pytest

from deeptrades.aggregate import (FillSeries, StreamingAggregator, absorption_decision,
                                  aggregate, evaluate_absorption, price_at)
from deeptrades.models import Fill, ParseError, parse_trade

TAKER = "0xtaker"


def fill(t, px, sz, side="B", taker=TAKER, maker="0xm1", tid=None, coin="BTC"):
    buyer, seller = (taker, maker) if side == "B" else (maker, taker)
    return Fill(coin=coin, time=t, px=px, sz=sz, side=side, buyer=buyer, seller=seller,
                hash="0xh", tid=tid if tid is not None else t)


# ----------------------------------------------------------------- taker attribution
def test_taker_is_buyer_for_side_b_and_seller_otherwise():
    b = Fill("BTC", 0, 1.0, 1.0, "B", "0xbuyer", "0xseller", "h", 1)
    a = Fill("BTC", 0, 1.0, 1.0, "A", "0xbuyer", "0xseller", "h", 2)
    assert (b.taker, b.maker) == ("0xbuyer", "0xseller")
    assert (a.taker, a.maker) == ("0xseller", "0xbuyer")
    [dt_b] = aggregate([b])
    [dt_a] = aggregate([a])
    assert dt_b.taker == "0xbuyer" and dt_a.taker == "0xseller"


def test_same_address_on_other_side_is_separate_deep_trade():
    fills = [fill(0, 100, 1, "B"), fill(100, 100, 1, "A")]
    trades = aggregate(fills)
    assert len(trades) == 2
    assert {t.side for t in trades} == {"A", "B"}


def test_different_takers_are_not_merged_and_do_not_break_groups():
    fills = [fill(0, 100, 1, taker="0xa", tid=1), fill(10, 100, 1, taker="0xb", tid=2),
             fill(20, 101, 1, taker="0xa", tid=3)]
    trades = aggregate(fills)
    by_taker = {t.taker: t for t in trades}
    assert by_taker["0xa"].fills == 2
    assert by_taker["0xb"].fills == 1


# ------------------------------------------------------------------- window bounds
def test_window_boundary_is_inclusive():
    trades = aggregate([fill(1000, 100, 1), fill(1500, 100, 1)], window_ms=500)
    assert len(trades) == 1 and trades[0].fills == 2


def test_fill_just_outside_window_starts_new_deep_trade():
    trades = aggregate([fill(1000, 100, 1), fill(1501, 100, 1)], window_ms=500)
    assert [t.fills for t in trades] == [1, 1]
    assert trades[1].start_time == 1501


def test_window_is_anchored_at_first_fill_not_chained():
    # gaps of 300 ms each: chaining would merge all, anchoring splits at 600 ms
    trades = aggregate([fill(0, 1, 1), fill(300, 1, 1), fill(600, 1, 1), fill(900, 1, 1)],
                       window_ms=500)
    assert [(t.start_time, t.fills) for t in trades] == [(0, 2), (600, 2)]


def test_input_order_does_not_matter():
    fills = [fill(t, 100 + t, 1, tid=t) for t in (0, 100, 200, 700)]
    assert [t.to_dict() for t in aggregate(fills)] == \
        [t.to_dict() for t in aggregate(list(reversed(fills)))]


def test_streaming_matches_batch_and_flush_by_time():
    agg = StreamingAggregator(500)
    assert agg.add(fill(0, 100, 1)) == []
    assert agg.add(fill(400, 101, 1)) == []
    assert agg.flush(now_ms=500) == []          # still inside the window
    [dt] = agg.flush(now_ms=501)
    assert dt.fills == 2 and agg.open_trades() == []


# --------------------------------------------------------------------------- stats
def test_vwap_notional_and_ranges():
    fills = [fill(0, 100.0, 1.0, maker="0xm1", tid=1), fill(10, 102.0, 3.0, maker="0xm2", tid=2),
             fill(20, 101.0, 1.0, maker="0xm1", tid=3)]
    [dt] = aggregate(fills)
    assert dt.sz == pytest.approx(5.0)
    assert dt.notional == pytest.approx(100 + 306 + 101)
    assert dt.vwap == pytest.approx(507 / 5)
    assert (dt.px_min, dt.px_max, dt.px_first, dt.px_last) == (100.0, 102.0, 100.0, 101.0)
    assert dt.fills == 3 and dt.makers == 2
    assert (dt.start_time, dt.end_time) == (0, 20)
    assert dt.id == "BTC-1"


def test_vwap_differs_from_simple_mean():
    [dt] = aggregate([fill(0, 100.0, 9.0, tid=1), fill(1, 200.0, 1.0, tid=2)])
    assert dt.vwap == pytest.approx(110.0)


# ---------------------------------------------------------------------- absorption
def test_absorption_decision_buy_and_sell():
    [buy] = aggregate([fill(0, 100, 1, "B"), fill(1, 101, 1, "B", tid=2)])
    assert absorption_decision(buy, 101.0) is True        # no new high
    assert absorption_decision(buy, 101.5) is False       # continued up
    assert absorption_decision(buy, 101.05, tolerance_bps=10) is True
    assert absorption_decision(buy, None) is True         # no later trade
    [sell] = aggregate([fill(0, 100, 1, "A"), fill(1, 99, 1, "A", tid=2)])
    assert absorption_decision(sell, 99.5) is True
    assert absorption_decision(sell, 98.0) is False


def test_evaluate_absorption_uses_price_at_horizon():
    big = [fill(0, 100, 5000, "B", tid=1)]
    later = [fill(5_000, 103, 1, "B", taker="0xo", tid=2),
             fill(9_000, 99, 1, "A", taker="0xo", tid=3),
             fill(20_000, 110, 1, "B", taker="0xo", tid=4)]
    series = FillSeries(big + later)
    assert price_at(series, 0, 10_000) == 99
    trades = [t for t in aggregate(series) if t.taker == TAKER]
    evaluate_absorption(trades, series, min_notional=100_000, horizon_s=10)
    assert trades[0].absorption is True  # spike to 103 came back to 99 at +10 s
    evaluate_absorption(trades, series, min_notional=100_000, horizon_s=10, data_until_ms=5_000)
    assert trades[0].absorption is None  # horizon not reached yet
    evaluate_absorption(trades, series, min_notional=10_000_000, horizon_s=10)
    assert trades[0].absorption is False  # below threshold


# ------------------------------------------------------------------------- parsing
def test_parse_trade_documented_shape():
    f = parse_trade({"coin": "BTC", "side": "A", "px": "62000.5", "sz": "0.25",
                     "hash": "0xabc", "time": 1718000000000, "tid": 42,
                     "users": ["0xBuyer", "0xSeller"]})
    assert (f.buyer, f.seller, f.taker) == ("0xbuyer", "0xseller", "0xseller")
    assert f.px == 62000.5 and f.sz == 0.25 and f.tid == 42


def test_parse_trade_rejects_bad_input():
    with pytest.raises(ParseError):
        parse_trade({"coin": "BTC", "side": "X", "px": "1", "sz": "1", "time": 1, "tid": 1,
                     "users": ["a", "b"]})
    with pytest.raises(ParseError):
        parse_trade({"coin": "BTC", "side": "B", "px": "1", "sz": "1", "time": 1, "tid": 1})
