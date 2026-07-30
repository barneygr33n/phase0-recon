#!/usr/bin/env python3
"""
quoting_engine.py — the quoting engine (design spec §2, §3.2, §1.5 EV allocator).

Given, per strike:
  - fair cents (from fairvalue.FairModel)
  - the current book (best yes bid / yes ask, in cents)
  - current net inventory
it computes the would-be resting quotes around fair:

    center c = fair − skew                     (skew from §3.2 inventory lean)
    hw = max(hw_min, hw_base × vol_mult)        (§2.1)
    YES buy  at round_down(c − hw)
    YES sell at round_up  (c + hw)

with the hard rules from the spec:
  - NEVER cross the book (this bot never takes liquidity) — cap 1¢ inside or skip.
  - Post a side only where post-fee edge ≥ min_edge_cents (§1.5 EV allocator).
  - At |net_inv| = max: stop the accumulating side, keep the reducing side improved 1¢ (§3.2).
  - Cap simultaneous strikes per film to max_strikes, ranked by edge (§1.5).

This module DOES NOT talk to Kalshi and NEVER places orders. It returns intent objects.
In shadow mode the runner just prints/logs them; in live mode a separate client would
act on them. Keeping placement logic here (pure, testable) is the point of Session 1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict


@dataclass
class Quote:
    strike: int
    side: str            # "yes_buy" | "yes_sell"
    price: int           # cents, 1..99
    size: int
    edge_c: float        # post-fee edge in cents at this price
    fair_c: float
    center_c: float
    hw_c: float
    skew_c: float
    status: str          # "post" | "skip_edge" | "skip_cross" | "skip_inv_cap"
    reason: str = ""

    def as_row(self) -> dict:
        return asdict(self)


@dataclass
class StrikeInput:
    strike: int
    fair_c: float
    yes_bid: float | None = None   # best resting YES bid (cents)
    yes_ask: float | None = None   # best resting YES ask (cents)
    net_inv: int = 0               # +long YES / −short YES, contracts
    sigma_60s: float | None = None # ¢/min rolling; None → vol_mult 1.0


@dataclass
class QuotingEngine:
    cfg: dict
    family: str = "rotten_tomatoes"

    fam: dict = field(init=False)
    risk: dict = field(init=False)

    def __post_init__(self):
        self.fam = self.cfg["families"][self.family]
        self.risk = self.cfg["risk"]

    # ── helpers ──────────────────────────────────────────────────────────────
    def _vol_mult(self, sigma_60s: float | None) -> float:
        if not sigma_60s:
            return 1.0
        return max(1.0, min(3.0, sigma_60s / self.fam["sigma_baseline"]))

    def _hw(self, sigma_60s: float | None) -> float:
        return max(self.fam["hw_min"], self.fam["hw_base"] * self._vol_mult(sigma_60s))

    def _skew(self, net_inv: int) -> float:
        """§3.2: skew = λ × (net_inv / max_inv_market) × hw_base. Positive inv → lower center."""
        cap = self.risk["max_inv_market"]
        return self.risk["lambda_skew"] * (net_inv / cap) * self.fam["hw_base"]

    @staticmethod
    def _clamp_price(p: float) -> int:
        return int(max(1, min(99, p)))

    # ── per-strike quote pair (pre edge/strike-cap filtering) ────────────────
    def quote_strike(self, s: StrikeInput) -> list[Quote]:
        fee = self.fam["maker_fee_c"]
        min_edge = self.fam["min_edge_cents"]
        cap = self.risk["max_inv_market"]
        size = self.fam["quote_size"]

        hw = self._hw(s.sigma_60s)
        skew = self._skew(s.net_inv)
        c = s.fair_c - skew

        raw_buy = math.floor(c - hw)
        raw_sell = math.ceil(c + hw)

        quotes: list[Quote] = []

        # inventory-cap gating (§3.2): at the cap, stop the accumulating side,
        # keep the reducing side improved 1¢.
        at_long_cap = s.net_inv >= cap
        at_short_cap = s.net_inv <= -cap

        # ---- YES buy side (accumulates long) ----
        buy_price = self._clamp_price(raw_buy)
        edge_buy = round(s.fair_c - buy_price - fee, 2)   # buy yes @ p: EV = fair − p − fee
        if at_long_cap:
            quotes.append(Quote(s.strike, "yes_buy", buy_price, size, edge_buy, s.fair_c,
                                round(c, 2), round(hw, 2), round(skew, 2),
                                "skip_inv_cap", "at long cap; stop accumulating side"))
        elif s.yes_ask is not None and buy_price >= s.yes_ask:
            capped = self._clamp_price(s.yes_ask - 1)     # 1¢ inside, never cross/take
            edge_capped = round(s.fair_c - capped - fee, 2)
            st = "post" if edge_capped >= min_edge else "skip_edge"
            quotes.append(Quote(s.strike, "yes_buy", capped, size, edge_capped, s.fair_c,
                                round(c, 2), round(hw, 2), round(skew, 2), st,
                                "would cross ask → capped 1¢ inside"))
        else:
            st = "post" if edge_buy >= min_edge else "skip_edge"
            quotes.append(Quote(s.strike, "yes_buy", buy_price, size, edge_buy, s.fair_c,
                                round(c, 2), round(hw, 2), round(skew, 2), st,
                                "" if st == "post" else f"edge {edge_buy}¢ < min {min_edge}¢"))

        # ---- YES sell side (accumulates short) ----
        sell_price = self._clamp_price(raw_sell)
        edge_sell = round(sell_price - s.fair_c - fee, 2)  # sell yes @ q: EV = q − fair − fee
        if at_short_cap:
            quotes.append(Quote(s.strike, "yes_sell", sell_price, size, edge_sell, s.fair_c,
                                round(c, 2), round(hw, 2), round(skew, 2),
                                "skip_inv_cap", "at short cap; stop accumulating side"))
        elif s.yes_bid is not None and sell_price <= s.yes_bid:
            capped = self._clamp_price(s.yes_bid + 1)      # 1¢ inside, never cross/take
            edge_capped = round(capped - s.fair_c - fee, 2)
            st = "post" if edge_capped >= min_edge else "skip_edge"
            quotes.append(Quote(s.strike, "yes_sell", capped, size, edge_capped, s.fair_c,
                                round(c, 2), round(hw, 2), round(skew, 2), st,
                                "would cross bid → capped 1¢ inside"))
        else:
            st = "post" if edge_sell >= min_edge else "skip_edge"
            quotes.append(Quote(s.strike, "yes_sell", sell_price, size, edge_sell, s.fair_c,
                                round(c, 2), round(hw, 2), round(skew, 2), st,
                                "" if st == "post" else f"edge {edge_sell}¢ < min {min_edge}¢"))

        return quotes

    # ── whole ladder, with max_strikes allocation (§1.5) ─────────────────────
    def quote_ladder(self, strikes: list[StrikeInput]) -> list[Quote]:
        all_q = [q for s in strikes for q in self.quote_strike(s)]

        # Which strikes have at least one postable side? Rank those by best edge,
        # keep top max_strikes; demote the rest to skip_maxstrikes.
        postable = {}
        for q in all_q:
            if q.status == "post":
                postable[q.strike] = max(postable.get(q.strike, -99), q.edge_c)
        keep = set(sorted(postable, key=lambda k: postable[k], reverse=True)
                   [: self.fam["max_strikes"]])

        for q in all_q:
            if q.status == "post" and q.strike not in keep:
                q.status = "skip_maxstrikes"
                q.reason = f"beyond max_strikes={self.fam['max_strikes']} (lower edge)"
        return all_q


def load_config(path=None):
    import yaml
    from pathlib import Path
    path = path or (Path(__file__).parent / "quoter_config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)
