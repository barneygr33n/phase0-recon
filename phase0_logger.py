#!/usr/bin/env python3
"""
Phase 0 Orderbook Recon Logger — Kalshi automated MM project.

Streams the `orderbook_delta` and `trade` WebSocket channels across candidate
market families and writes raw data to a SQLite file for offline analysis by
phase0_analyze.py. Measurement only — this script NEVER places, amends, or
cancels an order.

Reuses auth from kalshi_credentials.json / kalshi_private_key.pem (same signing
scheme as kalshi_fetch.py).

Design notes (see KALSHI_MM_PHASE0_RECON_SPEC.md §2):
  * One orderbook_delta subscription PER MARKET, so each market gets its own
    `sid` and therefore an unambiguous per-market `seq` stream. Gap detection and
    snapshot resync are then per-market.
  * One trade subscription covering all markets (trades carry no seq).
  * Prices are handled in CENTS as floats. The current API returns fixed-point
    DOLLAR strings (e.g. "0.0800", and subpenny like "0.0550"); we multiply by
    100. See DEVIATIONS in PHASE0_RESULTS.md.

Usage:
    python3 phase0_logger.py                 # run until Ctrl+C
    python3 phase0_logger.py --minutes 180   # run for a fixed window then exit
    python3 phase0_logger.py --selftest      # connect, one snapshot, limits, exit
"""

import argparse
import asyncio
import base64
import json
import os
import random
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

try:
    import websockets
except ImportError:
    sys.exit("Missing dependency: pip install websockets")

SCRIPT_DIR = Path(__file__).parent
CREDS_FILE = SCRIPT_DIR / "kalshi_credentials.json"
CONFIG_FILE = SCRIPT_DIR / "phase0_config.yaml"


# ── Auth (mirrors kalshi_fetch.py) ────────────────────────────────────────────
def load_credentials():
    with open(CREDS_FILE) as f:
        creds = json.load(f)
    key_path = SCRIPT_DIR / creds["private_key_path"]
    with open(key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    return creds["api_key_id"], private_key


def sign(private_key, msg: str) -> str:
    sig = private_key.sign(
        msg.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def rest_headers(api_key_id, private_key, method, full_path):
    ts = str(int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, ts + method.upper() + full_path),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


def ws_headers(api_key_id, private_key, sign_path):
    ts = str(int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, ts + "GET" + sign_path),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


# ── Small helpers ─────────────────────────────────────────────────────────────
def now_ms() -> int:
    return int(time.time() * 1000)


def to_cents(price_dollars) -> float:
    """'0.0800' -> 8.0 cents ; keeps subpenny precision (e.g. '0.0550' -> 5.5)."""
    try:
        return round(float(price_dollars) * 100.0, 4)
    except (TypeError, ValueError):
        return None


def fp(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ── Database ──────────────────────────────────────────────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS snapshots(
    ts_ms INTEGER, ticker TEXT, family TEXT,
    yes_bids_json TEXT, no_bids_json TEXT);
CREATE TABLE IF NOT EXISTS deltas(
    ts_ms INTEGER, ticker TEXT, price REAL, delta REAL, side TEXT, seq INTEGER);
CREATE TABLE IF NOT EXISTS bbo(
    ts_ms INTEGER, ticker TEXT, best_bid REAL, best_ask REAL, mid REAL, spread REAL);
CREATE TABLE IF NOT EXISTS trades(
    ts_ms INTEGER, ticker TEXT, price REAL, count REAL, taker_side TEXT);
CREATE TABLE IF NOT EXISTS events(
    ts_ms INTEGER, ticker TEXT, type TEXT, detail TEXT);
-- supporting metadata (needed for §3 active-period rules; additive to spec §2)
CREATE TABLE IF NOT EXISTS markets(
    ticker TEXT PRIMARY KEY, family TEXT, series TEXT, event TEXT, title TEXT,
    open_ts INTEGER, close_ts INTEGER, first_seen_ms INTEGER, last_seen_ms INTEGER);
CREATE INDEX IF NOT EXISTS ix_bbo_ticker_ts ON bbo(ticker, ts_ms);
CREATE INDEX IF NOT EXISTS ix_trades_ticker_ts ON trades(ticker, ts_ms);
CREATE INDEX IF NOT EXISTS ix_deltas_ticker_ts ON deltas(ticker, ts_ms);
"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.executescript(DDL)
        self.conn.commit()
        self._buf = {"snapshots": [], "deltas": [], "bbo": [], "trades": [], "events": []}

    def q(self, table, row):
        self._buf[table].append(row)

    def event(self, ticker, typ, detail=""):
        self.q("events", (now_ms(), ticker, typ, str(detail)))

    def upsert_market(self, m):
        self.conn.execute(
            """INSERT INTO markets(ticker,family,series,event,title,open_ts,close_ts,first_seen_ms,last_seen_ms)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET
                 family=excluded.family, series=excluded.series, event=excluded.event,
                 title=excluded.title, open_ts=excluded.open_ts, close_ts=excluded.close_ts,
                 last_seen_ms=excluded.last_seen_ms""",
            (m["ticker"], m["family"], m.get("series"), m.get("event"), m.get("title"),
             m.get("open_ts"), m.get("close_ts"), now_ms(), now_ms()))

    def flush(self):
        c = self.conn
        for t, rows in self._buf.items():
            if not rows:
                continue
            n = len(rows[0])
            c.executemany(f"INSERT INTO {t} VALUES ({','.join('?' * n)})", rows)
            rows.clear()
        c.commit()

    def close(self):
        self.flush()
        self.conn.close()


# ── Market discovery via REST ─────────────────────────────────────────────────
def rfc3339_to_ms(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def assign_family_event(series, category, cfg):
    """Assign a family to an EVENT by category (e.g. Mentions) or, within the
    Entertainment category, by series prefix (RT, awards). Returns name or None."""
    fam_defs = cfg["families"]
    cat = category or ""
    ser = (series or "").upper()
    # category-based families (Mentions)
    for fam, d in fam_defs.items():
        if cat in d.get("categories", []):
            return fam
    # entertainment-restricted series-prefix families (RT first for priority, then awards)
    if cat == "Entertainment":
        for fam, d in fam_defs.items():
            for p in d.get("entertainment_series_prefixes", []):
                if ser.startswith(p.upper()):
                    return fam
    return None


def discover_markets(api_key_id, private_key, cfg):
    """Discover in-family markets via the EVENTS feed (excludes multivariate combos),
    then resolve each in-family event's open markets. Returns list of market dicts."""
    base = cfg["runtime"]["rest_base"]
    rt = cfg["runtime"]

    # 1) page open events, keep the ones in our families
    fam_events = {}  # family -> list of event dicts
    cursor = None
    pages = 0
    while pages < 60:
        params = {"limit": 200, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        h = rest_headers(api_key_id, private_key, "GET", "/trade-api/v2/events")
        r = requests.get(base + "/events", headers=h, params=params, timeout=25)
        r.raise_for_status()
        d = r.json()
        for e in d.get("events", []):
            fam = assign_family_event(e.get("series_ticker"), e.get("category"), cfg)
            if fam:
                fam_events.setdefault(fam, []).append(
                    {"event": e.get("event_ticker"), "series": e.get("series_ticker")})
        cursor = d.get("cursor")
        pages += 1
        if not cursor:
            break

    # 2) cap events per family, then resolve their open markets
    markets = {}
    for fam, evs in fam_events.items():
        for ev in evs[: rt.get("max_events_per_family", 25)]:
            params = {"limit": 200, "status": "open", "event_ticker": ev["event"]}
            h = rest_headers(api_key_id, private_key, "GET", "/trade-api/v2/markets")
            try:
                r = requests.get(base + "/markets", headers=h, params=params, timeout=25)
                r.raise_for_status()
                for m in r.json().get("markets", []):
                    tk = m.get("ticker")
                    if not tk:
                        continue
                    markets[tk] = {
                        "ticker": tk, "family": fam,
                        "series": ev["series"], "event": ev["event"],
                        "title": m.get("title") or m.get("subtitle"),
                        "open_ts": rfc3339_to_ms(m.get("open_time")),
                        "close_ts": rfc3339_to_ms(m.get("close_time")),
                        "volume": fp(m.get("volume")) or fp(m.get("volume_24h")),
                    }
            except Exception:
                continue

    # 3) cap to max_markets with a per-family quota so no family is starved.
    #    Within each family keep highest-volume markets first; leftover slots then
    #    fill by volume across everything.
    by_fam = {}
    for m in markets.values():
        by_fam.setdefault(m["family"], []).append(m)
    for fam in by_fam:
        by_fam[fam].sort(key=lambda x: x.get("volume", 0), reverse=True)
    fam_defs = cfg["families"]
    default_q = rt.get("per_family_cap", 12)
    kept, leftover = [], []
    for fam, ms in by_fam.items():
        q = fam_defs.get(fam, {}).get("market_quota", default_q)
        kept.extend(ms[:q])
        leftover.extend(ms[q:])
    leftover.sort(key=lambda x: x.get("volume", 0), reverse=True)
    kept.extend(leftover[: max(0, rt["max_markets"] - len(kept))])
    return kept[: rt["max_markets"]]


def get_account_limits(api_key_id, private_key, cfg):
    base = cfg["runtime"]["rest_base"]
    h = rest_headers(api_key_id, private_key, "GET", "/trade-api/v2/account/limits")
    r = requests.get(base + "/account/limits", headers=h, timeout=15)
    r.raise_for_status()
    return r.json()


# ── Orderbook state per market ────────────────────────────────────────────────
class Book:
    __slots__ = ("yes", "no", "last_seq", "best_bid", "best_ask")

    def __init__(self):
        self.yes = {}   # price_cents -> qty
        self.no = {}    # price_cents -> qty
        self.last_seq = None
        self.best_bid = None
        self.best_ask = None

    def load_snapshot(self, yes_levels, no_levels):
        self.yes = {to_cents(p): fp(q) for p, q in (yes_levels or []) if fp(q) > 0}
        self.no = {to_cents(p): fp(q) for p, q in (no_levels or []) if fp(q) > 0}

    def apply(self, side, price_c, delta):
        book = self.yes if side == "yes" else self.no
        book[price_c] = book.get(price_c, 0.0) + delta
        if book[price_c] <= 1e-9:
            book.pop(price_c, None)

    def bbo(self):
        """Return (best_bid, best_ask, mid, spread) in YES cents, or None if one-sided."""
        best_yes_bid = max(self.yes) if self.yes else None
        best_no_bid = max(self.no) if self.no else None
        if best_yes_bid is None or best_no_bid is None:
            return None
        best_ask = 100.0 - best_no_bid          # YES ask = 100 - best NO bid
        best_bid = best_yes_bid
        return best_bid, best_ask, (best_bid + best_ask) / 2.0, best_ask - best_bid


# ── Logger core ───────────────────────────────────────────────────────────────
class Phase0Logger:
    def __init__(self, cfg, selftest=False, minutes=None):
        self.cfg = cfg
        self.selftest = selftest
        self.deadline = (time.time() + minutes * 60) if minutes else None
        self.api_key_id, self.private_key = load_credentials()
        self.db = DB(str(SCRIPT_DIR / cfg["runtime"]["db_path"]))
        self.books = {}          # ticker -> Book
        self.sid_to_ticker = {}  # orderbook sid -> ticker
        self.ticker_to_sid = {}
        self.markets = {}        # ticker -> meta
        self.cmd_id = 0
        self.stop = False
        self.selftest_ok = False
        self.last_commit = time.time()
        self.last_cov = 0
        self.last_rediscover = 0

    def next_id(self):
        self.cmd_id += 1
        return self.cmd_id

    async def refresh_markets(self):
        loop = asyncio.get_event_loop()
        found = await loop.run_in_executor(
            None, discover_markets, self.api_key_id, self.private_key, self.cfg)
        by_fam = {}
        for m in found:
            self.markets[m["ticker"]] = m
            self.db.upsert_market(m)
            by_fam.setdefault(m["family"], []).append(m["ticker"])
        self.db.flush()
        print(f"[{datetime.now():%H:%M:%S}] discovery: {len(found)} markets across "
              f"{len(by_fam)} families -> " +
              ", ".join(f"{k}:{len(v)}" for k, v in sorted(by_fam.items())))
        for k, v in sorted(by_fam.items()):
            print(f"    {k}: {', '.join(v[:8])}{' …' if len(v) > 8 else ''}")
        self.db.event("", "discovery", json.dumps({k: len(v) for k, v in by_fam.items()}))
        return found

    async def send(self, ws, obj):
        await ws.send(json.dumps(obj))

    async def subscribe_all(self, ws):
        """One orderbook_delta sub per market (own sid) + one trade sub for all."""
        self.sid_to_ticker.clear()
        self.ticker_to_sid.clear()
        tickers = list(self.markets.keys())
        for tk in tickers:
            self.books.setdefault(tk, Book())
            cid = self.next_id()
            await self.send(ws, {"id": cid, "cmd": "subscribe",
                                 "params": {"channels": ["orderbook_delta"],
                                            "market_ticker": tk}})
            # map the pending cmd id -> ticker; resolved on 'subscribed' reply
            self._pending_sub = getattr(self, "_pending_sub", {})
            self._pending_sub[cid] = tk
            await asyncio.sleep(0.02)  # gentle pacing
        # trade channel: all markets on one subscription
        cid = self.next_id()
        await self.send(ws, {"id": cid, "cmd": "subscribe",
                             "params": {"channels": ["trade"], "market_tickers": tickers}})
        self._trade_cmd_id = cid
        print(f"[{datetime.now():%H:%M:%S}] subscribed: {len(tickers)} orderbook + 1 trade feed")

    async def request_snapshot(self, ws, ticker):
        sid = self.ticker_to_sid.get(ticker)
        if sid is None:
            return
        await self.send(ws, {"id": self.next_id(), "cmd": "update_subscription",
                             "params": {"sids": [sid], "action": "get_snapshot"}})

    def handle_message(self, data):
        t = data.get("type")
        if t == "subscribed":
            cid = data.get("id")
            sid = data.get("msg", {}).get("sid")
            ch = data.get("msg", {}).get("channel")
            pend = getattr(self, "_pending_sub", {})
            if cid in pend and ch == "orderbook_delta":
                tk = pend.pop(cid)
                self.sid_to_ticker[sid] = tk
                self.ticker_to_sid[tk] = sid
            return
        if t == "orderbook_snapshot":
            self._on_snapshot(data)
        elif t == "orderbook_delta":
            self._on_delta(data)
        elif t == "trade":
            self._on_trade(data)
        elif t == "error":
            self.db.event("", "ws_error", json.dumps(data.get("msg", {})))
            print(f"  ! ws error: {data.get('msg')}")

    def _on_snapshot(self, data):
        sid = data.get("sid")
        seq = data.get("seq")
        msg = data.get("msg", {})
        tk = msg.get("market_ticker") or self.sid_to_ticker.get(sid)
        if not tk:
            return
        self.sid_to_ticker[sid] = tk
        self.ticker_to_sid[tk] = sid
        bk = self.books.setdefault(tk, Book())
        bk.load_snapshot(msg.get("yes_dollars_fp"), msg.get("no_dollars_fp"))
        bk.last_seq = seq
        fam = self.markets.get(tk, {}).get("family", "")
        self.db.q("snapshots", (now_ms(), tk, fam,
                                json.dumps(msg.get("yes_dollars_fp") or []),
                                json.dumps(msg.get("no_dollars_fp") or [])))
        self._record_bbo(tk, bk)

    def _on_delta(self, data):
        sid = data.get("sid")
        seq = data.get("seq")
        msg = data.get("msg", {})
        tk = msg.get("market_ticker") or self.sid_to_ticker.get(sid)
        if not tk:
            return
        bk = self.books.setdefault(tk, Book())
        # sequence integrity (per-market/per-sid)
        if bk.last_seq is not None and seq is not None and seq != bk.last_seq + 1:
            self.db.event(tk, "gap", json.dumps({"expected": bk.last_seq + 1, "got": seq}))
            bk.last_seq = seq  # accept; resync snapshot requested by caller loop
            self._need_resync = getattr(self, "_need_resync", set())
            self._need_resync.add(tk)
            return
        bk.last_seq = seq
        price_c = to_cents(msg.get("price_dollars"))
        delta = fp(msg.get("delta_fp"))
        side = msg.get("side")
        if price_c is None or side not in ("yes", "no"):
            return
        ts = msg.get("ts_ms") or now_ms()
        self.db.q("deltas", (ts, tk, price_c, delta, side, seq))
        bk.apply(side, price_c, delta)
        self._record_bbo(tk, bk)

    def _on_trade(self, data):
        msg = data.get("msg", {})
        tk = msg.get("market_ticker")
        if not tk:
            return
        ts = msg.get("ts_ms") or now_ms()
        price_c = to_cents(msg.get("yes_price_dollars"))
        self.db.q("trades", (ts, tk, price_c, fp(msg.get("count_fp")), msg.get("taker_side")))

    def _record_bbo(self, tk, bk):
        b = bk.bbo()
        if b is None:
            return
        best_bid, best_ask, mid, spread = b
        if (best_bid, best_ask) != (bk.best_bid, bk.best_ask):
            bk.best_bid, bk.best_ask = best_bid, best_ask
            self.db.q("bbo", (now_ms(), tk, best_bid, best_ask, mid, spread))

    async def periodic(self, ws):
        nowt = time.time()
        if nowt - self.last_commit >= self.cfg["runtime"]["commit_every_s"]:
            self.db.flush()
            self.last_commit = nowt
        if nowt - self.last_cov >= self.cfg["runtime"]["coverage_ping_s"]:
            self.db.event("", "coverage_ping", str(len(self.markets)))
            self.last_cov = nowt
        if nowt - self.last_rediscover >= self.cfg["runtime"]["rediscover_every_min"] * 60:
            self.last_rediscover = nowt
            try:
                await self.refresh_markets()
                await self.resubscribe_new(ws)
            except Exception as e:
                self.db.event("", "rediscover_error", str(e))
        # resync any markets that had a gap
        for tk in list(getattr(self, "_need_resync", set())):
            await self.request_snapshot(ws, tk)
            self._need_resync.discard(tk)

    async def resubscribe_new(self, ws):
        for tk in self.markets:
            if tk not in self.ticker_to_sid:
                self.books.setdefault(tk, Book())
                cid = self.next_id()
                self._pending_sub = getattr(self, "_pending_sub", {})
                self._pending_sub[cid] = tk
                await self.send(ws, {"id": cid, "cmd": "subscribe",
                                     "params": {"channels": ["orderbook_delta"],
                                                "market_ticker": tk}})
                await asyncio.sleep(0.02)

    def _one_open_market(self):
        """Fetch a single open market ticker (connectivity probe for --selftest)."""
        base = self.cfg["runtime"]["rest_base"]
        h = rest_headers(self.api_key_id, self.private_key, "GET", "/trade-api/v2/markets")
        r = requests.get(base + "/markets", headers=h,
                         params={"limit": 1, "status": "open"}, timeout=20)
        r.raise_for_status()
        ms = r.json().get("markets", [])
        if not ms:
            return None
        m = ms[0]
        return {"ticker": m.get("ticker"), "family": "_probe",
                "series": m.get("series_ticker"), "event": m.get("event_ticker"),
                "title": m.get("title"), "open_ts": None, "close_ts": None}

    async def run(self):
        await self.refresh_markets()
        if self.selftest and not self.markets:
            # family patterns matched nothing (possibly off-season / wrong prefixes).
            # Prove the WS pipe still works against any open market.
            try:
                m = self._one_open_market()
                if m and m["ticker"]:
                    self.markets[m["ticker"]] = m
                    print(f"[selftest] no in-family markets found; probing connectivity "
                          f"with open market {m['ticker']}")
            except Exception as e:
                print(f"[selftest] probe-market fetch failed: {e}")
        if not self.markets:
            print("No markets matched any family. Check phase0_config.yaml patterns "
                  "against the discovery output, then rerun.")
        backoff = 1
        while not self.stop:
            try:
                headers = ws_headers(self.api_key_id, self.private_key,
                                     self.cfg["runtime"]["ws_sign_path"])
                url = self.cfg["runtime"]["ws_url"]
                # websockets>=13 uses additional_headers; older uses extra_headers
                try:
                    ws_ctx = websockets.connect(url, additional_headers=headers,
                                                ping_interval=10, ping_timeout=30,
                                                max_queue=None)
                except TypeError:
                    ws_ctx = websockets.connect(url, extra_headers=headers,
                                                ping_interval=10, ping_timeout=30)
                async with ws_ctx as ws:
                    backoff = 1
                    self.db.event("", "ws_connect", url)
                    print(f"[{datetime.now():%H:%M:%S}] connected -> {url}")
                    await self.subscribe_all(ws)
                    async for raw in ws:
                        self.handle_message(json.loads(raw))
                        await self.periodic(ws)
                        if self.selftest and self._selftest_done():
                            self.selftest_ok = True
                            self.stop = True
                            break
                        if self.deadline and time.time() >= self.deadline:
                            print("Reached --minutes deadline; stopping.")
                            self.stop = True
                            break
            except (websockets.ConnectionClosed, OSError) as e:
                if self.stop or self.selftest:
                    break
                self.db.event("", "reconnect", str(e))
                print(f"  … disconnected ({e}); reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                # reset seq state; fresh snapshots will re-baseline
                for bk in self.books.values():
                    bk.last_seq = None
            except Exception as e:
                self.db.event("", "fatal", str(e))
                print(f"  !! unexpected error: {e}")
                if self.selftest:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        self.db.event("", "session_end", "")
        self.db.close()

    def _selftest_done(self):
        # done once we've stored at least one orderbook snapshot (proves the pipe)
        self.db.flush()
        s = self.db.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        return s >= 1


def run_selftest(cfg):
    """Connectivity + one snapshot + account limits, then exit."""
    api_key_id, private_key = load_credentials()
    print("== Phase 0 self-test ==")
    try:
        limits = get_account_limits(api_key_id, private_key, cfg)
        print(f"account/limits OK: {limits}")
    except Exception as e:
        print(f"account/limits FAILED: {e}")
    lg = Phase0Logger(cfg, selftest=True)
    lg.db.event("", "session_start", "selftest")

    async def _bounded():
        # hard 60s backstop so the self-test can never hang
        try:
            await asyncio.wait_for(lg.run(), timeout=60)
        except asyncio.TimeoutError:
            lg.stop = True
            print("WebSocket self-test timed out after 60s "
                  "(no snapshot received — check family patterns / connectivity).")

    try:
        asyncio.run(_bounded())
        if lg.selftest_ok:
            print("WebSocket snapshot capture OK — connectivity + auth confirmed.")
        else:
            print("WebSocket connected but no snapshot captured — see message above.")
    except Exception as e:
        print(f"WebSocket self-test FAILED: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=None,
                    help="run for this many minutes then exit (default: until Ctrl+C)")
    ap.add_argument("--selftest", action="store_true",
                    help="connect, capture one snapshot, print account limits, exit")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(CONFIG_FILE))

    if args.selftest:
        run_selftest(cfg)
        return

    lg = Phase0Logger(cfg, minutes=args.minutes)
    lg.db.event("", "session_start", "logger")

    def _sigint(*_):
        print("\nCtrl+C — flushing and closing…")
        lg.stop = True
    signal.signal(signal.SIGINT, _sigint)

    try:
        asyncio.run(lg.run())
    except KeyboardInterrupt:
        lg.db.close()
    print("Logger stopped. Data in", cfg["runtime"]["db_path"])


if __name__ == "__main__":
    main()
