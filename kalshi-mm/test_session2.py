#!/usr/bin/env python3
"""
test_session2.py — offline unit tests for the Sessions 2-3 pieces: strictly-through fill
simulation, the fill logger schema, and the WS message-handler core. No network.

    python3 test_session2.py
"""

import csv
import sys
import tempfile
from pathlib import Path

from fill_sim import RestingQuote, Trade, simulate_trade
from fill_logger import FillLogger, FIELDS
from md_feed import MarketData

FAILS = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def rq(ticker, side, price, size=5):
    return RestingQuote(ticker=ticker, strike=80, side=side, price=price,
                        size=size, remaining=size, placed_ms=0)


def test_fill_sim():
    print("fill_sim (strictly-through):")
    # YES buy @60 fills only when a print is strictly BELOW 60
    q = rq("T", "yes_buy", 60)
    inv = {}
    fills = simulate_trade([q], Trade(1, "T", 59, 2, "no"), inv)
    check("through below → fill", len(fills) == 1 and fills[0].count == 2)
    check("inventory long +2", inv["T"] == 2)

    q2 = rq("T", "yes_buy", 60)
    fills2 = simulate_trade([q2], Trade(1, "T", 60, 2, "no"), {})
    check("AT price → no fill (no queue priority)", fills2 == [])

    q3 = rq("T", "yes_buy", 60)
    fills3 = simulate_trade([q3], Trade(1, "T", 61, 2, "no"), {})
    check("above → no fill", fills3 == [])

    # YES sell @40 fills only when a print is strictly ABOVE 40
    qs = rq("T", "yes_sell", 40)
    invs = {}
    fs = simulate_trade([qs], Trade(1, "T", 41, 3, "yes"), invs)
    check("sell: through above → fill", len(fs) == 1 and fs[0].count == 3)
    check("sell inventory short −3", invs["T"] == -3)

    # partial fills: size 5, two through-trades of 3 then 4 → 3 then 2, then exhausted
    qp = rq("T", "yes_buy", 60, size=5)
    inv2 = {}
    f1 = simulate_trade([qp], Trade(1, "T", 58, 3, "no"), inv2)
    f2 = simulate_trade([qp], Trade(2, "T", 58, 4, "no"), inv2)
    f3 = simulate_trade([qp], Trade(3, "T", 58, 4, "no"), inv2)
    check("partial: first fill 3", f1[0].count == 3)
    check("partial: second fill capped at remaining 2", f2[0].count == 2)
    check("exhausted quote → no more fills", f3 == [] and not qp.active)

    # wrong ticker ignored
    check("other ticker ignored", simulate_trade([rq("A", "yes_buy", 60)],
                                                 Trade(1, "B", 50, 2, "no"), {}) == [])


def test_fill_logger():
    print("fill_logger (§4 schema):")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "f.csv"
        lg = FillLogger(path, "sess1")
        q = rq("KXRT-AVE-80", "yes_buy", 59)
        fills = simulate_trade([q], Trade(123, "KXRT-AVE-80", 58, 1, "no"), {})
        lg.log(fills[0], family="rotten_tomatoes", fair_mode="model", fair_at_fill=62.5,
               mid_at_fill=61.5, spread_at_fill=5, hw_at_fill=3, skew_at_fill=0,
               sigma_60s=None, trades_prev_60s=1, mid_move_prev_60s=0.0,
               queue_depth_ahead=None, seconds_resting=2.0, is_stress_harness=False)
        rows = list(csv.DictReader(open(path)))
        check("one row written", len(rows) == 1)
        check("header matches §4 schema", list(rows[0].keys()) == FIELDS)
        check("source=kalshi_mm", rows[0]["source"] == "kalshi_mm")
        check("markout columns blank for post-processing", rows[0]["markout_5m"] == "")
        check("operator_note blank for post-hoc tagging", rows[0]["operator_note"] == "")
        check("is_stress_harness recorded", rows[0]["is_stress_harness"] == "0")


def test_md_feed():
    print("md_feed (message-handler core):")
    md = MarketData(["KXRT-AVE-80"])
    # subscribe → snapshot establishes book + seq
    md.handle({"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 7}})
    md.handle({"type": "orderbook_snapshot", "sid": 7, "seq": 10,
               "msg": {"market_ticker": "KXRT-AVE-80",
                       "yes_dollars_fp": [["0.59", "100"]], "no_dollars_fp": [["0.36", "100"]]}})
    bbo = md.bbo("KXRT-AVE-80")
    check("snapshot builds bbo (bid 59, ask 64)", bbo and round(bbo[0]) == 59 and round(bbo[1]) == 64)

    # in-sequence delta applies, no gap
    md.handle({"type": "orderbook_delta", "sid": 7, "seq": 11,
               "msg": {"market_ticker": "KXRT-AVE-80", "price_dollars": "0.60",
                       "delta_fp": "50", "side": "yes"}})
    check("in-seq delta → no gap", md.gapped() == set())

    # out-of-sequence delta flags a gap (pull-on-gap trigger)
    md.handle({"type": "orderbook_delta", "sid": 7, "seq": 20,
               "msg": {"market_ticker": "KXRT-AVE-80", "price_dollars": "0.60",
                       "delta_fp": "1", "side": "yes"}})
    check("seq jump → market flagged gapped", "KXRT-AVE-80" in md.gapped())

    # fresh snapshot clears the gap
    md.handle({"type": "orderbook_snapshot", "sid": 7, "seq": 21,
               "msg": {"market_ticker": "KXRT-AVE-80",
                       "yes_dollars_fp": [["0.59", "100"]], "no_dollars_fp": [["0.36", "100"]]}})
    check("resync snapshot clears gap", md.gapped() == set())

    # trade queued with price + taker side
    md.handle({"type": "trade", "msg": {"market_ticker": "KXRT-AVE-80",
                                        "yes_price_dollars": "0.58", "count_fp": "3",
                                        "taker_side": "no", "ts_ms": 999}})
    tr = md.pop_trades()
    check("trade parsed (price 58, count 3)", len(tr) == 1 and tr[0].yes_price_c == 58 and tr[0].count == 3)
    check("trades drained after pop", md.pop_trades() == [])


def main():
    test_fill_sim()
    test_fill_logger()
    test_md_feed()
    print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
