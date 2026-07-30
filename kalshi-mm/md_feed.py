#!/usr/bin/env python3
"""
md_feed.py — fresh, minimal market-data WebSocket consumer for the quoter.

"Own feed, built fresh" (Ben's call, 2026-07-27): a small WS consumer separate from the
Phase 0 recon logger, so we don't touch the process that's been clean since the 7/26
resync fix. It REUSES phase0_logger's proven pieces read-only — `Book` (orderbook +
BBO math), `to_cents`, `fp`, `now_ms`, `load_credentials`, `ws_headers` — so there is
no second orderbook/signing implementation to drift.

It subscribes to `orderbook_delta` (one sub per strike market) + `trade` (one sub for
all), maintains a Book per market, and applies the CORRECTED resync (unsubscribe +
resubscribe on a sequence gap — NOT the `get_snapshot` call that caused the code-14
loop). On any gap it flags the market so the session loop can PULL quotes there
(scope §3 pull-on-gap) until a clean snapshot returns.

Split:
  - `MarketData`   pure message-handler core (dict in → books/trades/gaps). Unit-tested.
  - `WSFeed`       live asyncio connection loop. LIVE-ONLY (VPS); needs websockets + net.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from phase0_logger import Book, to_cents, fp, now_ms  # noqa: E402  reuse proven book/helpers


class MarketData:
    """Pure core: feed it decoded WS message dicts; it maintains books, queues trades,
    and records sequence gaps. No I/O — testable offline with hand-built messages."""

    def __init__(self, tickers: list[str]):
        self.books: dict[str, Book] = {t: Book() for t in tickers}
        self.sid_to_ticker: dict[int, str] = {}
        self.ticker_to_sid: dict[str, int] = {}
        self._pending_sub: dict[int, str] = {}   # cmd id -> ticker (resolved on 'subscribed')
        self._trades: list = []                  # drained by pop_trades()
        self._gapped: set[str] = set()           # markets needing resync / quote-pull
        self.errors: list = []

    # ── message handling (mirrors phase0_logger, trimmed to what the quoter needs) ──
    def handle(self, data: dict):
        t = data.get("type")
        if t == "subscribed":
            cid = data.get("id")
            msg = data.get("msg", {})
            if cid in self._pending_sub and msg.get("channel") == "orderbook_delta":
                tk = self._pending_sub.pop(cid)
                sid = msg.get("sid")
                self.sid_to_ticker[sid] = tk
                self.ticker_to_sid[tk] = sid
        elif t == "orderbook_snapshot":
            self._on_snapshot(data)
        elif t == "orderbook_delta":
            self._on_delta(data)
        elif t == "trade":
            self._on_trade(data)
        elif t == "error":
            self.errors.append(data.get("msg", {}))

    def _on_snapshot(self, data):
        sid, seq = data.get("sid"), data.get("seq")
        msg = data.get("msg", {})
        tk = msg.get("market_ticker") or self.sid_to_ticker.get(sid)
        if not tk:
            return
        self.sid_to_ticker[sid] = tk
        self.ticker_to_sid[tk] = sid
        bk = self.books.setdefault(tk, Book())
        bk.load_snapshot(msg.get("yes_dollars_fp"), msg.get("no_dollars_fp"))
        bk.last_seq = seq
        self._gapped.discard(tk)   # clean baseline restored

    def _on_delta(self, data):
        sid, seq = data.get("sid"), data.get("seq")
        msg = data.get("msg", {})
        tk = msg.get("market_ticker") or self.sid_to_ticker.get(sid)
        if not tk:
            return
        bk = self.books.setdefault(tk, Book())
        if bk.last_seq is not None and seq is not None and seq != bk.last_seq + 1:
            # sequence gap → flag for resync + quote-pull; don't trust the book until resync
            self._gapped.add(tk)
            bk.last_seq = seq
            return
        bk.last_seq = seq
        price_c = to_cents(msg.get("price_dollars"))
        side = msg.get("side")
        if price_c is None or side not in ("yes", "no"):
            return
        bk.apply(side, price_c, fp(msg.get("delta_fp")))

    def _on_trade(self, data):
        from fill_sim import Trade
        msg = data.get("msg", {})
        tk = msg.get("market_ticker")
        if not tk:
            return
        yp = to_cents(msg.get("yes_price_dollars"))
        if yp is None:
            return
        self._trades.append(Trade(ts_ms=msg.get("ts_ms") or now_ms(), ticker=tk,
                                  yes_price_c=int(round(yp)), count=int(fp(msg.get("count_fp"))),
                                  taker_side=msg.get("taker_side", "")))

    # ── accessors for the session loop ──────────────────────────────────────
    def bbo(self, ticker: str):
        bk = self.books.get(ticker)
        return bk.bbo() if bk else None

    def pop_trades(self):
        out, self._trades = self._trades, []
        return out

    def gapped(self) -> set:
        return set(self._gapped)


class WSFeed:
    """LIVE connection loop (VPS-only). Owns a MarketData core and drives it from a real
    Kalshi WS. Reuses phase0_logger auth (ws_headers) + the corrected resync."""

    def __init__(self, tickers: list[str], ws_url: str, ws_sign_path: str):
        from phase0_logger import load_credentials
        self.tickers = tickers
        self.ws_url = ws_url
        self.ws_sign_path = ws_sign_path
        self.kid, self.pk = load_credentials()
        self.md = MarketData(tickers)
        self._cmd_id = 0
        self.stop = False

    def _next_id(self):
        self._cmd_id += 1
        return self._cmd_id

    async def _subscribe_all(self, ws):
        for tk in self.tickers:
            cid = self._next_id()
            self.md._pending_sub[cid] = tk
            await ws.send(json.dumps({"id": cid, "cmd": "subscribe",
                                      "params": {"channels": ["orderbook_delta"],
                                                 "market_ticker": tk}}))
            await asyncio.sleep(0.02)
        await ws.send(json.dumps({"id": self._next_id(), "cmd": "subscribe",
                                  "params": {"channels": ["trade"],
                                             "market_tickers": self.tickers}}))

    async def _resync(self, ws, tk):
        """Corrected resync: unsubscribe the sid, then resubscribe by market_ticker to
        trigger a fresh orderbook_snapshot (the code-14 fix from 2026-07-26)."""
        sid = self.md.ticker_to_sid.get(tk)
        if sid is not None:
            await ws.send(json.dumps({"id": self._next_id(), "cmd": "unsubscribe",
                                      "params": {"sids": [sid]}}))
            self.md.sid_to_ticker.pop(sid, None)
            self.md.ticker_to_sid.pop(tk, None)
        cid = self._next_id()
        self.md._pending_sub[cid] = tk
        await ws.send(json.dumps({"id": cid, "cmd": "subscribe",
                                  "params": {"channels": ["orderbook_delta"],
                                             "market_ticker": tk}}))

    async def run(self, on_tick=None, tick_s: float = 1.0):
        import websockets
        from phase0_logger import ws_headers
        async with websockets.connect(
                self.ws_url, additional_headers=ws_headers(self.kid, self.pk, self.ws_sign_path),
                ping_interval=10, ping_timeout=30, max_size=2 ** 22) as ws:
            await self._subscribe_all(ws)
            last_tick = 0.0
            while not self.stop:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=tick_s)
                    self.md.handle(json.loads(raw))
                except asyncio.TimeoutError:
                    pass
                # resync any gapped markets (also triggers the session's quote-pull)
                for tk in list(self.md.gapped()):
                    await self._resync(ws, tk)
                if on_tick:
                    loop_now = asyncio.get_event_loop().time()
                    if loop_now - last_tick >= tick_s:
                        last_tick = loop_now
                        on_tick(self.md)
