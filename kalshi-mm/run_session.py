#!/usr/bin/env python3
"""
run_session.py — Sessions 2-3 live-shadow session loop. ZERO ORDERS.

Wires: md_feed (live book + trades) → FairModel → QuotingEngine → fill_sim → fill_logger,
with the reveal-guard, pull-on-gap, session timer, and risk caps from the design spec.
Runs unattended on the VPS (Ben's call); operator tagging is post-hoc in the CSV.

Data source:
  (default)     LIVE — pull each film's ladder (REST) for tickers/strikes, then a fresh
                WebSocket feed drives the loop. VPS-only (Kalshi is firewalled elsewhere).
  --synthetic   replay a scripted book + trades offline, so the WHOLE pipeline (quote
                maintenance → strictly-through fills → logging → summary) is provable
                without a live feed. Used to validate logic in the sandbox.

No order-placement code exists anywhere in this package (scope §8 / spec §8).
"""

from __future__ import annotations

import argparse
import datetime as D
import signal
import sys
import time
from collections import deque

from fairvalue import FairModel
from quoting_engine import QuotingEngine, StrikeInput, load_config
from fill_sim import RestingQuote, Trade, simulate_trade
from fill_logger import FillLogger


def now_ms() -> int:
    return int(time.time() * 1000)


# ── rolling per-market stats (for §4 schema fields + vol_mult) ────────────────
class MarketStats:
    """Rolling 60s window of mids/trades. sigma_60s here is a v1 volatility PROXY (std of
    mids over 60s, cents) — not a rigorous ¢/min figure; refine in Phase 2. It only feeds
    the clamp(1,3) vol multiplier and the fill-log, so approximate is acceptable."""

    def __init__(self):
        self.mids = deque()      # (ts_ms, mid)
        self.trades = deque()    # ts_ms

    def add_mid(self, ts, mid):
        self.mids.append((ts, mid))
        self._trim(ts)

    def add_trade(self, ts):
        self.trades.append(ts)
        self._trim(ts)

    def _trim(self, ts):
        cut = ts - 60_000
        while self.mids and self.mids[0][0] < cut:
            self.mids.popleft()
        while self.trades and self.trades[0] < cut:
            self.trades.popleft()

    def sigma_60s(self):
        vals = [m for _, m in self.mids]
        if len(vals) < 3:
            return None
        mean = sum(vals) / len(vals)
        return round((sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5, 3)

    def mid_move_60s(self):
        if len(self.mids) < 2:
            return 0.0
        return round(self.mids[-1][1] - self.mids[0][1], 2)

    def trades_prev_60s(self):
        return len(self.trades)


# ── simple mark-to-mid position book (for session-loss stop + summary) ────────
class Position:
    __slots__ = ("net", "cost")   # net contracts (YES+), signed cost basis in cents

    def __init__(self):
        self.net = 0
        self.cost = 0.0


class ShadowSession:
    def __init__(self, cfg, model: FairModel, films: dict, logger: FillLogger, session_id: str):
        self.cfg = cfg
        self.model = model
        self.films = films            # event_ticker -> {"strikes": {strike: mkt_ticker}, "harness": bool}
        self.engine = QuotingEngine(cfg, "rotten_tomatoes")
        self.logger = logger
        self.session_id = session_id
        self.risk = cfg["risk"]
        self.fam = self.engine.fam

        self.resting: dict[tuple, RestingQuote] = {}   # (mkt_ticker, side) -> RestingQuote
        self.inv: dict[str, int] = {}                  # mkt_ticker -> net contracts
        self.pos: dict[str, Position] = {}             # mkt_ticker -> Position (for PnL)
        self.stats: dict[str, MarketStats] = {}
        self.n_fills = 0
        self.stopped = False
        self.stop_reason = ""
        self.start = time.time()

    # ── reveal guard (per film) ──────────────────────────────────────────────
    def _film_blocked(self, event_ticker) -> bool:
        f = self.model.film(event_ticker)
        rd, lead = f.get("reveal_datetime_utc"), f.get("reveal_pull_lead_min")
        if not rd or lead is None:
            return False
        try:
            reveal = D.datetime.fromisoformat(rd.replace("Z", "+00:00"))
        except ValueError:
            return True
        return D.datetime.now(D.timezone.utc) >= reveal - D.timedelta(minutes=lead)

    def _stats(self, tk) -> MarketStats:
        return self.stats.setdefault(tk, MarketStats())

    # ── one tick: reprice all strikes, then process trades ───────────────────
    def step(self, md):
        if self.stopped:
            return
        # session timer
        if (time.time() - self.start) / 60.0 >= self.risk["session_max_min"]:
            self._stop("session timer reached window end")
            return

        gapped = md.gapped()
        ts = now_ms()

        for event_ticker, fdef in self.films.items():
            blocked = self._film_blocked(event_ticker)
            for strike, tk in fdef["strikes"].items():
                # pull-on-gap or reveal-guard → remove any resting quotes here, skip
                if blocked or tk in gapped:
                    self.resting.pop((tk, "yes_buy"), None)
                    self.resting.pop((tk, "yes_sell"), None)
                    continue
                bbo = md.bbo(tk)
                if not bbo:
                    continue
                bid, ask, mid, spread = bbo
                st = self._stats(tk)
                st.add_mid(ts, mid)
                fair_c = self.model.fair_cents(event_ticker, strike)
                si = StrikeInput(strike=strike, fair_c=fair_c, yes_bid=bid, yes_ask=ask,
                                 net_inv=self.inv.get(tk, 0), sigma_60s=st.sigma_60s())
                quotes = self.engine.quote_strike(si)
                # allocator (max_strikes) is per-film; approximate per-strike here by
                # posting any positive-edge side — max_strikes capping is applied by the
                # engine's quote_ladder in live mode; for the loop we cap total exposure.
                for q in quotes:
                    self._apply_quote(tk, strike, q, mid, spread, st, ts)

        for tr in md.pop_trades():
            self._stats(tr.ticker).add_trade(tr.ts_ms)
            self._book_fills(tr)

        # session-loss stop (mark-to-mid)
        if self._session_pnl(md) <= -self.risk["max_session_loss"]:
            self._stop(f"session loss ≤ -${self.risk['max_session_loss']}")

    def _apply_quote(self, tk, strike, q, mid, spread, st, ts):
        key = (tk, q.side)
        if q.status != "post":
            self.resting.pop(key, None)
            return
        # total-exposure cap (spec §3.1 max_inv_total, cost-basis proxy in $)
        if self._total_cost_basis() >= self.risk["max_inv_total"] and \
                self._would_add_exposure(tk, q.side):
            self.resting.pop(key, None)
            return
        existing = self.resting.get(key)
        hw = q.hw_c
        if existing and existing.active and abs(existing.price - q.price) <= self.fam["reprice_frac"] * hw:
            return  # keep queue position (spec §2.2 / §2.3)
        self.resting[key] = RestingQuote(
            ticker=tk, strike=strike, side=q.side, price=q.price,
            size=self.fam["quote_size"], remaining=self.fam["quote_size"], placed_ms=ts,
            fair_at_place=q.fair_c, mid_at_place=mid, spread_at_place=spread,
            hw_at_place=hw, skew_at_place=q.skew_c, sigma_60s_at_place=st.sigma_60s())

    def _book_fills(self, tr: Trade):
        fills = simulate_trade(list(self.resting.values()), tr, self.inv)
        for f in fills:
            self._update_position(f)
            rq = f.resting
            st = self._stats(f.ticker)
            self.logger.log(
                f, family="rotten_tomatoes", fair_mode="model",
                fair_at_fill=rq.fair_at_place, mid_at_fill=rq.mid_at_place,
                spread_at_fill=rq.spread_at_place, hw_at_fill=rq.hw_at_place,
                skew_at_fill=rq.skew_at_place, sigma_60s=rq.sigma_60s_at_place,
                trades_prev_60s=st.trades_prev_60s(), mid_move_prev_60s=st.mid_move_60s(),
                queue_depth_ahead=rq.queue_depth_ahead,
                seconds_resting=(f.ts_ms - rq.placed_ms) / 1000.0,
                is_stress_harness=self.films_of(f.ticker))
            self.n_fills += 1

    def films_of(self, mkt_ticker) -> bool:
        for ev, fdef in self.films.items():
            if mkt_ticker in fdef["strikes"].values():
                return fdef["harness"]
        return False

    # ── position / PnL ───────────────────────────────────────────────────────
    def _update_position(self, f):
        p = self.pos.setdefault(f.ticker, Position())
        signed = f.count if f.maker_side == "yes_buy" else -f.count
        p.net += signed
        p.cost += signed * f.price   # cents

    def _would_add_exposure(self, tk, side):
        cur = self.inv.get(tk, 0)
        return (side == "yes_buy" and cur >= 0) or (side == "yes_sell" and cur <= 0)

    def _total_cost_basis(self):
        return sum(abs(p.cost) for p in self.pos.values()) / 100.0  # $

    def _session_pnl(self, md):
        pnl = 0.0
        for tk, p in self.pos.items():
            bbo = md.bbo(tk)
            mark = bbo[2] if bbo else (p.cost / p.net if p.net else 0)
            pnl += (p.net * mark - p.cost) / 100.0  # $
        return round(pnl, 2)

    def _stop(self, reason):
        self.stopped = True
        self.stop_reason = reason
        self.resting.clear()
        print(f"\n── SESSION STOP: {reason} — quotes stood down ──")

    def summary(self, md=None):
        pnl = self._session_pnl(md) if md else 0.0
        print(f"\nsession {self.session_id}: fills={self.n_fills}  "
              f"open markets w/ inv={sum(1 for p in self.pos.values() if p.net)}  "
              f"mark-to-mid PnL (SIM)=${pnl}  "
              f"{'stopped: ' + self.stop_reason if self.stopped else 'clean'}")


# ── offline replay feed (for --synthetic sandbox proof) ───────────────────────
class FakeFeed:
    """Implements the md interface (bbo/pop_trades/gapped) from a scripted book + trades."""

    def __init__(self, book: dict, trades: list):
        self._book = book           # mkt_ticker -> (bid, ask)
        self._trades = list(trades)
        self._gapped = set()

    def bbo(self, tk):
        if tk not in self._book:
            return None
        b, a = self._book[tk]
        return (b, a, (b + a) / 2.0, a - b)

    def pop_trades(self):
        out, self._trades = self._trades, []
        return out

    def gapped(self):
        return set(self._gapped)


def _resolve_live_films(cfg, model):
    """LIVE: pull each configured film's ladder → {event: {"strikes":{k:mkt_ticker}, "harness":b}}."""
    from kalshi_client import KalshiClient
    client = KalshiClient(cfg["runtime"]["rest_base"], cfg["runtime"]["sign_prefix"])
    print("pre-flight:", client.exchange_status().get("trading_active"))
    films = {}
    for ev in cfg["session"]["films"]:
        lad = client.ladder(ev)
        strikes = {k: v["ticker"] for k, v in lad.items()}
        films[ev] = {"strikes": strikes, "harness": bool(model.film(ev).get("is_stress_harness"))}
        print(f"  {ev}: {len(strikes)} strikes")
    return films, client


def run(synthetic: bool):
    cfg = load_config()
    model = FairModel()
    session_id = D.datetime.now().strftime("%Y%m%dT%H%M%S")
    from pathlib import Path
    logger = FillLogger(Path(__file__).parent / cfg["session"]["fill_log"], session_id)

    if synthetic:
        # scripted AVE-like ladder + a burst of trades, some strictly through our quotes
        ev = "KXRT-AVE"
        strikes = {70: "KXRT-AVE-70", 80: "KXRT-AVE-80", 85: "KXRT-AVE-85"}
        films = {ev: {"strikes": strikes, "harness": False}}
        sess = ShadowSession(cfg, model, films, logger, session_id)
        book = {"KXRT-AVE-70": (88, 93), "KXRT-AVE-80": (59, 64), "KXRT-AVE-85": (29, 34)}
        # trades: some print THROUGH would-be quotes (fills), some don't (no fill)
        trades = [
            Trade(now_ms(), "KXRT-AVE-80", 58, 3, "no"),   # through a yes_buy resting ~59
            Trade(now_ms(), "KXRT-AVE-85", 37, 2, "yes"),  # through a yes_sell resting ~36
            Trade(now_ms(), "KXRT-AVE-70", 90, 5, "no"),   # at/near book, likely no through
        ]
        feed = FakeFeed(book, trades)
        sess.step(feed)                 # tick 1: place quotes + process trades
        sess.step(FakeFeed(book, []))   # tick 2: no new trades
        sess.summary(feed)
        print(f"\nwould-be resting quotes now: {len(sess.resting)}; fill log → {logger.path.name}")
        return

    # LIVE (VPS)
    films, client = _resolve_live_films(cfg, model)
    sess = ShadowSession(cfg, model, films, logger, session_id)

    from md_feed import WSFeed
    tickers = [tk for f in films.values() for tk in f["strikes"].values()]
    feed = WSFeed(tickers, cfg["runtime"]["ws_url"], cfg["runtime"]["ws_sign_path"])

    def on_sigint(*_):
        sess._stop("Ctrl+C kill switch")
        sess.summary(feed.md)
        try:
            client.cancel_all(verbose=True)   # zero orders in shadow, but path is exercised
        except Exception as e:
            print(f"cancel_all note: {e}")
        sys.exit(0)
    signal.signal(signal.SIGINT, on_sigint)

    print(f"LIVE SHADOW — session {session_id} — ZERO ORDERS. Ctrl+C to stop.")
    last_status = time.time()

    def tick(md):
        nonlocal last_status
        sess.step(md)
        if time.time() - last_status >= cfg["session"]["status_every_s"]:
            last_status = time.time()
            sess.summary(md)

    import asyncio
    asyncio.run(feed.run(on_tick=tick, tick_s=cfg["session"]["tick_s"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", help="offline scripted replay")
    args = ap.parse_args()
    run(args.synthetic)


if __name__ == "__main__":
    main()
