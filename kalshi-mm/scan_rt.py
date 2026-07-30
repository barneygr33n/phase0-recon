#!/usr/bin/env python3
"""
scan_rt.py — list open Rotten Tomatoes (KXRT) films on Kalshi with reveal dates + ladders.

Used to pick/confirm films: the Session-2 debut (KXRT-AVE, already set) and the Spider-Man
shadow stress harness. Discovers via the EVENTS feed (series KXRT), same pattern as the
Phase 0 logger, then lists each film's close/reveal time and 24h volume. For any film whose
title matches a filter (default "spider"), it also prints the FULL per-strike book so we can
wire fair_inputs.json + the config directly.

LIVE-ONLY (needs creds + Kalshi access). Run on the Mac or VPS:
    python3 scan_rt.py                 # all KXRT films; full ladder for Spider-Man
    python3 scan_rt.py spider          # full ladder for any title matching "spider"
    python3 scan_rt.py ""              # full ladder for EVERY film (slow)
"""

import sys

from kalshi_client import KalshiClient


def _fmt_close(s):
    return (s or "").replace("T", " ").replace("Z", " UTC")


def scan(filter_str: str):
    c = KalshiClient()

    # 1) page open events, keep series KXRT
    events, cursor = [], None
    while True:
        p = {"limit": 200, "status": "open"}
        if cursor:
            p["cursor"] = cursor
        d = c._req("GET", "/events", p)
        for e in d.get("events", []):
            if (e.get("series_ticker") or "").startswith("KXRT"):
                events.append(e)
        cursor = d.get("cursor")
        if not cursor:
            break

    # 2) per event: list markets (title, close, strike count, volume)
    films = []
    for e in events:
        ev = e.get("event_ticker")
        ms = c._req("GET", "/markets", {"limit": 200, "status": "open",
                                        "event_ticker": ev}).get("markets", [])
        strikes = [m for m in ms if (m.get("ticker", "").split("-")[-1]).isdigit()]
        if not strikes:
            continue
        title = strikes[0].get("title") or ev
        close = strikes[0].get("close_time") or strikes[0].get("expected_expiration_time")
        vol24 = sum(float(m.get("volume_24h_fp") or 0) for m in strikes)
        films.append({"event": ev, "title": title, "close": close,
                      "n": len(strikes), "vol24": vol24, "markets": strikes})

    films.sort(key=lambda f: (f["close"] or "9999"))

    print(f"\n{len(films)} open KXRT films (sorted by reveal/close):\n")
    print(f"{'event':<22} {'strikes':>7} {'vol24h':>9}  reveal/close (UTC)   title")
    print("-" * 90)
    for f in films:
        print(f"{f['event']:<22} {f['n']:>7} {f['vol24']:>9.0f}  "
              f"{_fmt_close(f['close']):<20} {f['title'][:34]}")

    # 3) full ladder for filter matches (default: spider)
    matches = [f for f in films if filter_str.lower() in f["title"].lower()] if filter_str \
        else films
    for f in matches:
        print(f"\n=== FULL LADDER: {f['event']}  —  {f['title']} ===")
        print(f"    reveal/close: {_fmt_close(f['close'])}   24h vol: {f['vol24']:.0f}")
        print(f"    {'strike':>6} {'yes_bid':>8} {'yes_ask':>8} {'vol24h':>8}")
        rows = []
        for m in f["markets"]:
            t = m["ticker"]
            k = int(t.split("-")[-1])
            d2 = c._req("GET", f"/markets/{t}").get("market", {})
            yb, ya = d2.get("yes_bid_dollars"), d2.get("yes_ask_dollars")
            vol = float(d2.get("volume_24h_fp") or 0)
            rows.append((k, yb, ya, vol))
        for k, yb, ya, vol in sorted(rows):
            bid = f"{float(yb)*100:.0f}" if yb is not None else "—"
            ask = f"{float(ya)*100:.0f}" if ya is not None else "—"
            print(f"    {k:>6} {bid:>8} {ask:>8} {vol:>8.0f}")
        print(f"    → to wire: set fair_inputs.json '{f['event']}' reveal_datetime_utc + mixture, "
              f"add '{f['event']}' to session.films in quoter_config.yaml")


def main():
    filt = sys.argv[1] if len(sys.argv) > 1 else "spider"
    scan(filt)


if __name__ == "__main__":
    main()
