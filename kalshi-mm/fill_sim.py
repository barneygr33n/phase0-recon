#!/usr/bin/env python3
"""
fill_sim.py — shadow fill simulation (design spec §5 mode 1; Sessions 2-3 scope §3).

A simulated fill happens ONLY when a real trade prints STRICTLY THROUGH one of our
resting would-be quotes — not merely at it. "At" would assume queue priority we don't
have; "through" conservatively assumes we're behind the queue and only fill when the
market trades past our price.

Per resting quote:
  - YES buy at b : fills when a trade prints yes_price_c < b  (a seller traded through us)
  - YES sell at s: fills when a trade prints yes_price_c > s  (a buyer traded through us)
Fill count = min(trade count, remaining size on our quote). Partial fills allowed; a
resting quote is consumed once its remaining size hits 0, until the engine reprices it.

Own (simulated) fills update simulated inventory but NEVER move the belief fair
(spec §1.5 — updating beliefs on your own fills invites adverse selection).

Pure and deterministic — no network, no orders. Fully unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RestingQuote:
    ticker: str
    strike: int
    side: str            # "yes_buy" | "yes_sell"
    price: int           # cents
    size: int            # original size
    remaining: int       # decremented as simulated fills consume it
    placed_ms: int
    # market state captured at placement (for the fill logger / markouts)
    fair_at_place: float = 0.0
    mid_at_place: float = 0.0
    spread_at_place: float = 0.0
    hw_at_place: float = 0.0
    skew_at_place: float = 0.0
    sigma_60s_at_place: float | None = None
    queue_depth_ahead: float | None = None

    @property
    def active(self) -> bool:
        return self.remaining > 0


@dataclass
class SimFill:
    ts_ms: int
    ticker: str
    strike: int
    maker_side: str      # "yes_buy" | "yes_sell"
    price: int
    count: int
    trade_price: int     # the print that triggered us (for markout / audit)
    taker_side: str
    inv_before: int
    inv_after: int
    resting: RestingQuote = field(repr=False, default=None)


@dataclass
class Trade:
    ts_ms: int
    ticker: str
    yes_price_c: int
    count: int
    taker_side: str = ""   # "yes"/"no" if the feed provides it; stored, not required


def _signed(side: str, count: int) -> int:
    """YES buy adds long inventory (+), YES sell adds short (−)."""
    return count if side == "yes_buy" else -count


def simulate_trade(resting: list[RestingQuote], trade: Trade, inv: dict[str, int]) -> list[SimFill]:
    """Given the currently-resting quotes on trade.ticker and an incoming trade, return
    any simulated fills. Mutates `remaining` on filled quotes and `inv` (net per market).

    `inv` maps market ticker -> net contracts (YES-positive). Each strike is its own
    Kalshi market, so the per-market cap (max_inv_market) keys on ticker. Caller enforces
    risk caps BEFORE placing quotes; here we just book the fills the market gives us.
    """
    fills: list[SimFill] = []
    for q in resting:
        if q.ticker != trade.ticker or not q.active:
            continue
        through = (q.side == "yes_buy" and trade.yes_price_c < q.price) or \
                  (q.side == "yes_sell" and trade.yes_price_c > q.price)
        if not through:
            continue
        n = min(q.remaining, trade.count)
        if n <= 0:
            continue
        before = inv.get(q.ticker, 0)
        after = before + _signed(q.side, n)
        q.remaining -= n
        inv[q.ticker] = after
        fills.append(SimFill(
            ts_ms=trade.ts_ms, ticker=q.ticker, strike=q.strike, maker_side=q.side,
            price=q.price, count=n, trade_price=trade.yes_price_c,
            taker_side=trade.taker_side, inv_before=before, inv_after=after, resting=q))
    return fills
