#!/usr/bin/env python3
"""
Phase 0 offline analyzer — implements KALSHI_MM_PHASE0_RECON_SPEC.md §5 metrics
and §6 ranking rubric EXACTLY as written. Reads phase0_recon.db, writes
phase0_results.csv (per-ticker + per-family rows) and PHASE0_ANALYSIS.md
(rubric table, disqualifications, ranked survivors, origination flag).

All metrics computed on ACTIVE periods only (§3): market two-sided AND >30 min
from close; the final 30 min before close and any seq-gap intervals are excluded.
Prices are in CENTS (the logger already converted from the API's dollar strings).

Usage:
    python3 phase0_analyze.py                      # uses phase0_recon.db
    python3 phase0_analyze.py --db other.db
    python3 phase0_analyze.py --maker-fee-cents 0.5   # override observed maker fee (§7.1)
"""

import argparse
import json
import math
import sqlite3
import statistics as st
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).parent
CENTRAL = ZoneInfo("America/Chicago")
CLOSE_GUARD_MS = 30 * 60 * 1000          # exclude final 30 min before close (§3)
WINDOWS = [(7, 10), (17, 20)]            # Ben's blocks, US Central (§5.3)
MARKOUTS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}
ORIGINATION_FAMILIES = {"rotten_tomatoes", "awards"}   # Ben has a fair-value view here (§6 bonus flag)


# ── loading ───────────────────────────────────────────────────────────────────
def load(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    markets = {r["ticker"]: dict(r) for r in c.execute("SELECT * FROM markets")}
    tickers = set(markets) | {r[0] for r in c.execute("SELECT DISTINCT ticker FROM bbo")}
    return c, markets, [t for t in tickers if t]


def rows(c, table, ticker):
    return [dict(r) for r in c.execute(
        f"SELECT * FROM {table} WHERE ticker=? ORDER BY ts_ms", (ticker,))]


def gap_windows(c, ticker):
    """Return list of (start_ms, end_ms) intervals to EXCLUDE: from each seq-gap
    event until the next snapshot for that ticker."""
    gaps = [r["ts_ms"] for r in c.execute(
        "SELECT ts_ms FROM events WHERE ticker=? AND type='gap' ORDER BY ts_ms", (ticker,))]
    snaps = [r["ts_ms"] for r in c.execute(
        "SELECT ts_ms FROM snapshots WHERE ticker=? ORDER BY ts_ms", (ticker,))]
    out = []
    for g in gaps:
        nxt = next((s for s in snaps if s > g), None)
        out.append((g, nxt if nxt else g + 2000))  # if never resynced, small guard
    return out


def in_any(ts, windows):
    return any(a <= ts < b for a, b in windows)


# ── book replay → timeline of (ts, spread, mid, depth_yes, depth_no) ──────────
def replay_timeline(c, ticker):
    """Reconstruct the book from snapshots+deltas and emit a state point whenever
    the book changes. Returns list of dicts with ts, best_bid, best_ask, mid,
    spread, depth3_yes, depth3_no. Book held in cents:qty."""
    snaps = rows(c, "snapshots", ticker)
    deltas = rows(c, "deltas", ticker)
    # merge-sort snapshots and deltas by ts
    events = ([("snap", s["ts_ms"], s) for s in snaps] +
              [("delta", d["ts_ms"], d) for d in deltas])
    events.sort(key=lambda e: (e[1], 0 if e[0] == "snap" else 1))
    yes, no = {}, {}
    tl = []

    def state(ts):
        if not yes or not no:
            return None
        bb = max(yes)                 # best YES bid
        ba = 100.0 - max(no)          # YES ask = 100 - best NO bid
        # guard: a crossed/locked book is a reconstruction artifact (real Kalshi
        # books never rest crossed). Treat as not-active rather than poison medians.
        if ba < bb or not (0 < bb < 100) or not (0 < ba < 100):
            return None
        mid = (bb + ba) / 2.0
        d_yes = sum(q for p, q in yes.items() if abs(p - mid) <= 3.0)
        d_no = sum(q for p, q in no.items() if abs((100.0 - p) - mid) <= 3.0)
        return {"ts": ts, "best_bid": bb, "best_ask": ba, "mid": mid,
                "spread": ba - bb, "depth3_yes": d_yes, "depth3_no": d_no}

    for kind, ts, r in events:
        if kind == "snap":
            yes = {float(p) * 100: float(q) for p, q in json.loads(r["yes_bids_json"] or "[]") if float(q) > 0}
            no = {float(p) * 100: float(q) for p, q in json.loads(r["no_bids_json"] or "[]") if float(q) > 0}
            yes = {round(k, 4): v for k, v in yes.items()}
            no = {round(k, 4): v for k, v in no.items()}
        else:
            book = yes if r["side"] == "yes" else no
            p = round(r["price"], 4)
            book[p] = book.get(p, 0.0) + r["delta"]
            if book[p] <= 1e-9:
                book.pop(p, None)
        s = state(ts)
        if s:
            tl.append(s)
    return tl


def active_intervals(tl, close_ts, gaps):
    """Yield (start, end, state) segments that are ACTIVE: two-sided (implicit in
    tl), >30min from close, not inside a gap window. Each tl point persists until
    the next point."""
    segs = []
    for i, s in enumerate(tl):
        start = s["ts"]
        end = tl[i + 1]["ts"] if i + 1 < len(tl) else start
        if end <= start:
            continue
        # close guard
        if close_ts:
            active_end = close_ts - CLOSE_GUARD_MS
            if start >= active_end:
                continue
            end = min(end, active_end)
        # subtract gap windows
        pieces = [(start, end)]
        for gs, ge in gaps:
            newp = []
            for a, b in pieces:
                if ge <= a or gs >= b:
                    newp.append((a, b))
                else:
                    if a < gs:
                        newp.append((a, gs))
                    if ge < b:
                        newp.append((ge, b))
            pieces = newp
        for a, b in pieces:
            if b > a:
                segs.append((a, b, s))
    return segs


def wmedian(pairs):
    """Weighted median of (value, weight)."""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    tot = sum(w for _, w in pairs)
    if tot <= 0:
        return None
    acc = 0
    for v, w in pairs:
        acc += w
        if acc >= tot / 2:
            return v
    return pairs[-1][0]


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return values[int(k)]
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


# ── per-ticker metrics ────────────────────────────────────────────────────────
def analyze_ticker(c, ticker, meta, maker_fee_override):
    tl = replay_timeline(c, ticker)
    close_ts = (meta or {}).get("close_ts")
    gaps = gap_windows(c, ticker)
    segs = active_intervals(tl, close_ts, gaps)
    active_ms = sum(b - a for a, b, _ in segs)
    if active_ms <= 0:
        return None
    active_h = active_ms / 3_600_000

    # 5.1 spread & depth (time-weighted)
    sp_pairs = [(s["spread"], b - a) for a, b, s in segs]
    spread_mean = sum(v * w for v, w in sp_pairs) / active_ms
    spread_median = wmedian(sp_pairs)
    def pct_le(thr):
        return sum(w for v, w in sp_pairs if v <= thr) / active_ms * 100
    depth_yes = sum(s["depth3_yes"] * (b - a) for a, b, s in segs) / active_ms
    depth_no = sum(s["depth3_no"] * (b - a) for a, b, s in segs) / active_ms

    # active-time mask for trades: build sorted seg list
    seg_sorted = sorted(segs, key=lambda x: x[0])
    def is_active(ts):
        for a, b, _ in seg_sorted:
            if a <= ts < b:
                return True
            if a > ts:
                break
        return False

    # mid lookup over full timeline (for markouts/mid_pre; not restricted to active)
    tl_ts = [s["ts"] for s in tl]
    def mid_at(ts):
        # last state at or before ts
        lo, hi = 0, len(tl_ts) - 1
        if not tl_ts or ts < tl_ts[0]:
            return None
        while lo < hi:
            m = (lo + hi + 1) // 2
            if tl_ts[m] <= ts:
                lo = m
            else:
                hi = m - 1
        return tl[lo]["mid"]

    # 5.2 flow
    trades = rows(c, "trades", ticker)
    atrades = [t for t in trades if is_active(t["ts_ms"])]
    n_tr = len(atrades)
    contracts = sum(t["count"] for t in atrades)
    buy = sum(t["count"] for t in atrades if t["taker_side"] == "yes")
    sell = sum(t["count"] for t in atrades if t["taker_side"] == "no")
    taker_imb = abs(buy - sell) / contracts if contracts else None
    sizes = [t["count"] for t in atrades]
    avg_sz = (sum(sizes) / len(sizes)) if sizes else 0.0
    p90_sz = pct(sizes, 0.90) if sizes else 0.0

    # 5.3 window overlap
    def central_hour(ts):
        return datetime.fromtimestamp(ts / 1000, CENTRAL).hour
    win_contracts = sum(t["count"] for t in atrades
                        if any(a <= central_hour(t["ts_ms"]) < b for a, b in WINDOWS))
    flow_in_windows_pct = (win_contracts / contracts * 100) if contracts else None

    # 5.4 capture & toxicity
    eff_hs, mk = [], {k: [] for k in MARKOUTS}
    for t in atrades:
        sign = 1.0 if t["taker_side"] == "yes" else -1.0
        mid_pre = mid_at(t["ts_ms"] - 1)
        if mid_pre is None:
            continue
        eff_hs.append(sign * (t["price"] - mid_pre))
        for k, dms in MARKOUTS.items():
            mfut = mid_at(t["ts_ms"] + dms)
            if mfut is not None:
                mk[k].append(sign * (t["price"] - mfut))
    price_levels = [t["price"] for t in atrades]
    p_typ = (st.median(price_levels) / 100.0) if price_levels else 0.5
    maker_fee_cents = (maker_fee_override if maker_fee_override is not None
                       else 1.75 * p_typ * (1 - p_typ))  # 0.25 * 7 * p(1-p) cents
    mean_eff = (sum(eff_hs) / len(eff_hs)) if eff_hs else None
    mean_mk5 = (sum(mk["5m"]) / len(mk["5m"])) if mk["5m"] else None
    net_ev = (mean_eff + mean_mk5 - maker_fee_cents) if (mean_eff is not None and mean_mk5 is not None) else None

    # 5.5 gap risk — tumbling 60s bins over active trades' mid series
    mids = [(s["ts"], s["mid"]) for s in tl]
    bins = defaultdict(list)
    for ts, mid in mids:
        bins[ts // 60000].append(mid)
    moves = [max(v) - min(v) for v in bins.values() if len(v) >= 2]
    max1m_p95 = pct(moves, 0.95) if moves else 0.0
    n_days = max(1, active_ms / 86_400_000)
    gap5 = sum(1 for m in moves if m >= 5.0)
    gap_events_per_day = gap5 / n_days
    # scheduled clustering (mention family relevance): are big moves concentrated in few hours?
    gap_hours = Counter()
    for k, v in bins.items():
        if len(v) >= 2 and (max(v) - min(v)) >= 5.0:
            gap_hours[central_hour(k * 60000)] += 1
    scheduled = None
    if sum(gap_hours.values()) >= 5:
        top2 = sum(n for _, n in gap_hours.most_common(2))
        scheduled = top2 / sum(gap_hours.values()) >= 0.6

    # 5.6 competition — requote latency (median time trade -> next bbo change)
    bbo = rows(c, "bbo", ticker)
    bbo_ts = [b["ts_ms"] for b in bbo]
    lat = []
    for t in atrades:
        nxt = next((x for x in bbo_ts if x > t["ts_ms"]), None)
        if nxt:
            lat.append((nxt - t["ts_ms"]) / 1000.0)
    requote_latency = st.median(lat) if lat else None
    two_sided_persist = pct_le(4)  # % active time spread<=4 (two-sided implicit)

    # 5.7 supply handled at family level; per-ticker active lifetime:
    active_lifetime_h = active_h

    return {
        "ticker": ticker, "family": (meta or {}).get("family", "?"),
        "active_hours": round(active_h, 2),
        "spread_mean": rnd(spread_mean), "spread_median": rnd(spread_median),
        "pct_time_spread_le2": rnd(pct_le(2)), "pct_time_spread_le4": rnd(pct_le(4)),
        "pct_time_spread_le6": rnd(pct_le(6)),
        "depth_within_3c_yes": rnd(depth_yes), "depth_within_3c_no": rnd(depth_no),
        "trades_per_active_hour": rnd(n_tr / active_h) if active_h else 0,
        "contracts_per_active_hour": rnd(contracts / active_h) if active_h else 0,
        "taker_imbalance": rnd(taker_imb), "avg_trade_size": rnd(avg_sz),
        "p90_trade_size": rnd(p90_sz),
        "flow_in_windows_pct": rnd(flow_in_windows_pct),
        "effective_half_spread": rnd(mean_eff),
        "markout_1m_mean": rnd(mean_or_none(mk["1m"])),
        "markout_5m_mean": rnd(mean_mk5),
        "markout_5m_median": rnd(st.median(mk["5m"]) if mk["5m"] else None),
        "markout_5m_p10": rnd(pct(mk["5m"], 0.10) if mk["5m"] else None),
        "markout_15m_mean": rnd(mean_or_none(mk["15m"])),
        "maker_fee_cents": rnd(maker_fee_cents),
        "net_ev_per_fill": rnd(net_ev),
        "max_1min_move_p95": rnd(max1m_p95),
        "gap_events_per_day": rnd(gap_events_per_day),
        "gaps_scheduled": scheduled,
        "requote_latency_s": rnd(requote_latency),
        "two_sided_persistence": rnd(two_sided_persist),
        "active_lifetime_h": rnd(active_lifetime_h),
        "n_trades": n_tr, "contracts": rnd(contracts),
        "_win_contracts": win_contracts,
    }


def mean_or_none(xs):
    return (sum(xs) / len(xs)) if xs else None


def rnd(x, n=3):
    return round(x, n) if isinstance(x, (int, float)) else x


# ── family aggregation + rubric (§6) ──────────────────────────────────────────
def aggregate_family(fam, tks, per_ticker, c):
    rs = [per_ticker[t] for t in tks if per_ticker.get(t)]
    if not rs:
        return None
    tot_active_h = sum(r["active_hours"] for r in rs)
    tot_contracts = sum(r["contracts"] for r in rs)
    tot_trades = sum(r["n_trades"] for r in rs)
    tot_win_contracts = sum(r["_win_contracts"] for r in rs)

    def wavg(key):
        num = sum((r[key] or 0) * r["active_hours"] for r in rs if r[key] is not None)
        den = sum(r["active_hours"] for r in rs if r[key] is not None)
        return (num / den) if den else None

    avg_sz = wavg("avg_trade_size") or 0.0
    # window-active hours across the family (approx: active_hours * window fraction of day)
    # trades arriving during Ben's windows, per window-active-hour:
    window_hours_per_day = sum(b - a for a, b in WINDOWS)  # 6h/day
    span_days = max(1, tot_active_h / 24) if tot_active_h else 1
    win_active_h = max(1e-6, (window_hours_per_day / 24) * tot_active_h)
    trades_per_window_hour = tot_win_contracts and (
        _win_trades(rs) / win_active_h) or 0.0
    fills_per_window_hour = trades_per_window_hour  # each trade ~ one potential fill on our side
    capturable = trades_per_window_hour * min(10.0, avg_sz if avg_sz else 0.0)

    net_ev = wavg("net_ev_per_fill")
    max1m_p95 = wavg("max_1min_move_p95") or 0.0
    # scheduled: majority of tickers flagged scheduled?
    sched_flags = [r["gaps_scheduled"] for r in rs if r["gaps_scheduled"] is not None]
    gaps_scheduled = (sum(1 for x in sched_flags if x) > len(sched_flags) / 2) if sched_flags else None

    agg = {
        "family": fam, "n_markets": len(rs),
        "active_hours_total": rnd(tot_active_h),
        "spread_median": rnd(wavg("spread_median")),
        "pct_time_spread_le4": rnd(wavg("pct_time_spread_le4")),
        "depth_within_3c_yes": rnd(wavg("depth_within_3c_yes")),
        "trades_per_active_hour": rnd(tot_trades / tot_active_h) if tot_active_h else 0,
        "contracts_per_active_hour": rnd(tot_contracts / tot_active_h) if tot_active_h else 0,
        "avg_trade_size": rnd(avg_sz), "p90_trade_size": rnd(wavg("p90_trade_size")),
        "taker_imbalance": rnd(wavg("taker_imbalance")),
        "flow_in_windows_pct": rnd(wavg("flow_in_windows_pct")),
        "effective_half_spread": rnd(wavg("effective_half_spread")),
        "markout_5m_mean": rnd(wavg("markout_5m_mean")),
        "markout_5m_p10": rnd(wavg("markout_5m_p10")),
        "net_ev_per_fill": rnd(net_ev),
        "max_1min_move_p95": rnd(max1m_p95),
        "gaps_scheduled": gaps_scheduled,
        "gap_events_per_day": rnd(wavg("gap_events_per_day")),
        "requote_latency_s": rnd(wavg("requote_latency_s")),
        "two_sided_persistence": rnd(wavg("two_sided_persistence")),
        "fills_per_window_hour": rnd(fills_per_window_hour),
        "contracts_per_window_hour_capturable": rnd(capturable),
    }
    # §6 disqualification
    reasons = []
    if net_ev is None:
        reasons.append("insufficient trades to estimate net_ev_per_fill")
    elif net_ev <= 0:
        reasons.append(f"net_ev_per_fill ≤ 0 ({net_ev:.3f}¢)")
    if max1m_p95 > 12 and gaps_scheduled is not True:
        reasons.append(f"max_1min_move_p95 {max1m_p95:.1f}¢ > 12¢ with unscheduled gaps")
    if fills_per_window_hour < 2:
        reasons.append(f"expected fills/window-hour {fills_per_window_hour:.2f} < 2")
    agg["disqualified"] = bool(reasons)
    agg["disqualify_reasons"] = reasons
    agg["score"] = None if (reasons or net_ev is None) else rnd(net_ev * capturable)
    agg["origination_flag"] = fam in ORIGINATION_FAMILIES
    return agg


def _win_trades(rs):
    # approx count of trades in Ben's windows = sum over tickers of n_trades * (win_contracts/contracts)
    n = 0.0
    for r in rs:
        if r["contracts"]:
            n += r["n_trades"] * (r["_win_contracts"] / r["contracts"])
    return n


# ── reporting ─────────────────────────────────────────────────────────────────
def write_csv(path, per_ticker, fam_aggs):
    import csv
    cols = list(next(iter(per_ticker.values())).keys()) if per_ticker else []
    cols = [c for c in cols if not c.startswith("_")]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["row_type"] + cols)
        for r in per_ticker.values():
            w.writerow(["market"] + [r.get(c) for c in cols])
        f.write("\n")
    # family rows appended to a second CSV block-friendly section
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if fam_aggs:
            fcols = list(fam_aggs[0].keys())
            w.writerow([])
            w.writerow(["FAMILY_AGGREGATES"])
            w.writerow(fcols)
            for a in fam_aggs:
                w.writerow([a.get(c) for c in fcols])


def write_report(path, fam_aggs, per_ticker, meta):
    survivors = [a for a in fam_aggs if not a["disqualified"] and a["score"] is not None]
    survivors.sort(key=lambda a: (-a["score"], a["max_1min_move_p95"] or 1e9,
                                  -(a["n_markets"])))
    dq = [a for a in fam_aggs if a["disqualified"]]
    lines = []
    lines.append("# Phase 0 Analysis — auto-generated metrics & rubric\n")
    lines.append(f"*Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by phase0_analyze.py. "
                 "Side-investigation answers, deviations, and the final recommendation live in "
                 "PHASE0_RESULTS.md.*\n")
    days = rnd(sum(a["active_hours_total"] for a in fam_aggs) / 24, 1)
    lines.append(f"\n**Coverage:** {len(per_ticker)} markets, "
                 f"{sum(a['active_hours_total'] for a in fam_aggs):.0f} market-active-hours "
                 f"(~{days} market-days) across {len(fam_aggs)} families.\n")
    if days < 10:
        lines.append("\n> ⚠️ **Under 10 collection days of data — rubric verdicts are PRELIMINARY.** "
                     "The spec requires ≥10 days before the rubric decides.\n")

    lines.append("\n## Ranked survivors\n")
    if survivors:
        lines.append("| Rank | Family | score | net_ev/fill (¢) | capturable/win-hr | "
                     "max_1min_p95 (¢) | fills/win-hr | orig? |")
        lines.append("|---|---|--:|--:|--:|--:|--:|:--:|")
        for i, a in enumerate(survivors, 1):
            lines.append(f"| {i} | {a['family']} | {a['score']} | {a['net_ev_per_fill']} | "
                         f"{a['contracts_per_window_hour_capturable']} | {a['max_1min_move_p95']} | "
                         f"{a['fills_per_window_hour']} | {'🎯' if a['origination_flag'] else ''} |")
    else:
        lines.append("_No family survived disqualification._")

    lines.append("\n## Disqualified\n")
    if dq:
        lines.append("| Family | reasons |")
        lines.append("|---|---|")
        for a in dq:
            lines.append(f"| {a['family']} | {'; '.join(a['disqualify_reasons'])} |")
    else:
        lines.append("_None._")

    # origination flag callout (§6 bonus)
    orig_survivors = [a for a in survivors if a["origination_flag"]]
    if orig_survivors:
        lines.append("\n## 🎯 Origination-model families (flagged per §6 bonus)\n")
        for a in orig_survivors:
            lines.append(f"- **{a['family']}** survived disqualification (rank "
                         f"{survivors.index(a)+1}). Model-anchored MM justifies a lower flow "
                         "rank per the build-scope thesis — weigh prominently.")

    lines.append("\n## Full family metrics\n")
    keys = ["family", "n_markets", "active_hours_total", "spread_median",
            "pct_time_spread_le4", "depth_within_3c_yes", "trades_per_active_hour",
            "contracts_per_active_hour", "avg_trade_size", "p90_trade_size",
            "taker_imbalance", "flow_in_windows_pct", "effective_half_spread",
            "markout_5m_mean", "markout_5m_p10", "net_ev_per_fill",
            "max_1min_move_p95", "gaps_scheduled", "gap_events_per_day",
            "requote_latency_s", "two_sided_persistence",
            "fills_per_window_hour", "contracts_per_window_hour_capturable", "score"]
    lines.append("| " + " | ".join(keys) + " |")
    lines.append("|" + "|".join("---" for _ in keys) + "|")
    for a in fam_aggs:
        lines.append("| " + " | ".join(str(a.get(k)) for k in keys) + " |")

    lines.append("\n## Per-market detail\n")
    lines.append("See `phase0_results.csv` for all per-market rows.\n")
    Path(path).write_text("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(SCRIPT_DIR / "phase0_recon.db"))
    ap.add_argument("--maker-fee-cents", type=float, default=None,
                    help="observed maker fee per contract (¢) from §7.1; overrides formula")
    ap.add_argument("--csv", default=str(SCRIPT_DIR / "phase0_results.csv"))
    ap.add_argument("--report", default=str(SCRIPT_DIR / "PHASE0_ANALYSIS.md"))
    args = ap.parse_args()

    c, markets, tickers = load(args.db)
    per_ticker = {}
    for t in tickers:
        try:
            r = analyze_ticker(c, t, markets.get(t), args.maker_fee_cents)
            if r:
                per_ticker[t] = r
        except Exception as e:
            print(f"  ! {t}: {e}")
    # family grouping
    fam_tks = defaultdict(list)
    for t, r in per_ticker.items():
        fam_tks[r["family"]].append(t)
    fam_aggs = [a for fam, tks in fam_tks.items()
                if (a := aggregate_family(fam, tks, per_ticker, c))]
    write_csv(args.csv, per_ticker, fam_aggs)
    write_report(args.report, fam_aggs, per_ticker, markets)
    print(f"Analyzed {len(per_ticker)} markets / {len(fam_aggs)} families.")
    print(f"  → {args.csv}")
    print(f"  → {args.report}")


if __name__ == "__main__":
    main()
