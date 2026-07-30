#!/usr/bin/env python3
"""
run_shadow.py — Session 1 runner: SHADOW MODE, ZERO ORDERS (scope §5 / spec §5 mode 1).

What it does:
  1. Loads quoter_config.yaml (must be mode: shadow) and fair_inputs.json.
  2. Reveal-calendar guard: if within reveal_pull_lead_min of a film's scheduled reveal,
     stand down (don't quote) — RT resolves AT reveal (scope §1).
  3. Pre-flight feed check (live only): exchange status + a gap sample on the ladder.
  4. Computes the full ladder of WOULD-BE quotes (fair-value → quoting engine) and prints
     them. Places nothing.
  5. Kill switch: Ctrl+C → cancel-all → print → exit (spec §3.3). Proven this session.

Data source:
  --synthetic   built-in AVE-shaped book; runs anywhere (used in the Cowork sandbox,
                which is firewalled from Kalshi). Exercises the full engine offline.
  (default)     live pull via kalshi_client (Mac / VPS only).

  --demo-kill   after printing the ladder, fire the kill-switch path programmatically
                (non-interactive proof it wires through to cancel-all).

This runner NEVER calls any order-placement endpoint. There is no order-placement code
in Phase 1 at all (scope §7 / spec §8).
"""

from __future__ import annotations

import argparse
import datetime as D
import signal
import sys

from fairvalue import FairModel, DEFAULT_STRIKES
from quoting_engine import QuotingEngine, StrikeInput, load_config

STATUS_STYLE = {
    "post": "POST ",
    "skip_edge": "  ·  ",
    "skip_cross": "  ·  ",
    "skip_maxstrikes": "  ·  ",
    "skip_inv_cap": "CAP  ",
}


# ── kill switch ──────────────────────────────────────────────────────────────
class KillSwitch:
    """Ctrl+C → cancel-all → summary → exit. In shadow there are no live orders,
    but the path is exercised end-to-end so we KNOW it fires (spec §3.3 / §6 graduation)."""

    def __init__(self, client, live: bool):
        self.client = client
        self.live = live
        self.fired = False

    def fire(self, reason="Ctrl+C"):
        if self.fired:
            return
        self.fired = True
        print(f"\n── KILL SWITCH ({reason}) ─────────────────────────────")
        if self.live and self.client is not None:
            try:
                n = self.client.cancel_all(verbose=True)
                print(f"cancel-all complete: {n} order(s).")
            except Exception as e:
                print(f"cancel-all FAILED ({e}). Run emergency_cancel.py NOW.")
        else:
            print("[SHADOW] no live client — 0 orders resting by construction.")
            print("[SHADOW] live panic button is emergency_cancel.py (standalone).")
        print("session P&L: $0.00 (shadow, no fills). exiting.")
        sys.exit(0)


# ── synthetic book (sandbox / offline) ───────────────────────────────────────
def synthetic_ladder(model: FairModel, ticker: str, strikes):
    """Build a plausible pre-reveal book: ~4-5¢ spread straddling fair, with a couple
    of deliberately tight/crossed strikes so the never-cross + edge gates are visible."""
    book = {}
    for k in strikes:
        fair = model.fair_cents(ticker, k)
        # market sits a touch below fair on interior strikes (rec flow), wider in tails
        half = 2.5 if 5 < fair < 95 else 4.0
        mid = fair - 1.0
        yb = max(1, round(mid - half))
        ya = min(99, round(mid + half))
        book[k] = {"yes_bid": float(yb), "yes_ask": float(ya)}
    return book


# ── reveal guard ─────────────────────────────────────────────────────────────
def reveal_blocked(film: dict) -> str | None:
    rd = film.get("reveal_datetime_utc")
    lead = film.get("reveal_pull_lead_min")
    if not rd or lead is None:
        return None
    try:
        reveal = D.datetime.fromisoformat(rd.replace("Z", "+00:00"))
    except ValueError:
        return f"unparseable reveal_datetime_utc={rd!r}"
    now = D.datetime.now(D.timezone.utc)
    if now >= reveal - D.timedelta(minutes=lead):
        return (f"within {lead}min of reveal ({reveal.isoformat()}) — STAND DOWN, "
                f"do not quote through the score drop")
    return None


# ── main ─────────────────────────────────────────────────────────────────────
def run(ticker: str, synthetic: bool, demo_kill: bool, strikes):
    cfg = load_config()
    if cfg["runtime"]["mode"] != "shadow":
        print(f"REFUSING: config mode is {cfg['runtime']['mode']!r}, not 'shadow'. "
              f"Session 1 is zero-orders.")
        sys.exit(2)

    model = FairModel()
    problems = model.validate(ticker)
    if problems:
        print(f"fair_inputs.json invalid for {ticker}; run fair_preview.py. Problems:")
        for p in problems:
            print("  -", p)
        sys.exit(1)

    film = model.film(ticker)
    print(f"SHADOW MODE — {ticker} ({film.get('title', ticker)}) — ZERO ORDERS")
    if film.get("note"):
        print(f"  {film['note']}")

    blocked = reveal_blocked(film)
    if blocked:
        print(f"REVEAL GUARD: {blocked}")
        print("No quotes computed. (This guard is exactly what prevents quoting into the drop.)")
        return

    # data source + client
    client, live = None, not synthetic
    if synthetic:
        book = synthetic_ladder(model, ticker, strikes)
        print("data: SYNTHETIC book (offline; sandbox is firewalled from Kalshi)\n")
    else:
        from kalshi_client import KalshiClient
        client = KalshiClient(cfg["runtime"]["rest_base"], cfg["runtime"]["sign_prefix"])
        # pre-flight feed check (scope §3.3)
        st = client.exchange_status()
        print(f"pre-flight: exchange_status={st}")
        raw = client.ladder(ticker)
        if not raw:
            print("no open markets for this event; nothing to quote.")
            return
        book = {k: {"yes_bid": v["yes_bid"], "yes_ask": v["yes_ask"]} for k, v in raw.items()}
        strikes = sorted(book)
        print(f"data: LIVE ladder, {len(book)} strikes\n")

    # arm kill switch
    ks = KillSwitch(client, live)
    signal.signal(signal.SIGINT, lambda *_: ks.fire("Ctrl+C"))

    # compute would-be quotes
    engine = QuotingEngine(cfg, family="rotten_tomatoes")
    inputs = [StrikeInput(strike=k, fair_c=model.fair_cents(ticker, k),
                          yes_bid=book[k]["yes_bid"], yes_ask=book[k]["yes_ask"],
                          net_inv=0)
              for k in sorted(book)]
    quotes = engine.quote_ladder(inputs)

    # print
    print(f"{'K':>4} {'book(bid/ask)':>14} {'fair¢':>7} {'center':>7} {'hw':>5} "
          f"| {'side':<9} {'px':>4} {'edge':>6} {'sz':>3}  status")
    print("-" * 92)
    by_strike: dict[int, list] = {}
    for q in quotes:
        by_strike.setdefault(q.strike, []).append(q)
    n_post = 0
    for k in sorted(by_strike):
        b = book[k]
        book_str = f"{b['yes_bid']:.0f}/{b['yes_ask']:.0f}"
        for i, q in enumerate(by_strike[k]):
            if i == 0:
                head = (f"{k:>4} {book_str:>14} {q.fair_c:>7.1f} "
                        f"{q.center_c:>7.2f} {q.hw_c:>5.2f} |")
            else:
                head = f"{'':>4} {'':>14} {'':>7} {'':>7} {'':>5} |"
            print(f"{head} {q.side:<9} {q.price:>4} {q.edge_c:>6.2f} {q.size:>3}  "
                  f"{STATUS_STYLE.get(q.status, q.status)} {q.reason}")
            if q.status == "post":
                n_post += 1
    print("-" * 92)
    print(f"would-be POST quotes: {n_post}  (max_strikes={engine.fam['max_strikes']}, "
          f"quote_size={engine.fam['quote_size']}, mode=SHADOW → nothing placed)")

    if demo_kill:
        ks.fire("--demo-kill (non-interactive proof)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker", nargs="?", default="KXRT-AVE")
    ap.add_argument("--synthetic", action="store_true", help="offline built-in book")
    ap.add_argument("--demo-kill", action="store_true", help="fire kill switch after printing")
    ap.add_argument("--strikes", type=int, nargs="*", default=None)
    args = ap.parse_args()
    run(args.ticker, args.synthetic, args.demo_kill, args.strikes or DEFAULT_STRIKES)


if __name__ == "__main__":
    main()
