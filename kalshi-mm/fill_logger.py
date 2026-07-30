#!/usr/bin/env python3
"""
fill_logger.py — the fill logger (design spec §4). This data is the PRODUCT of Phase 1.

Writes one CSV row per simulated fill in the §4 schema, with source=kalshi_mm and a
ledger kept completely separate from directional betting (market-categorization
convention). markout_1m/5m/15m are left blank here — a post-processing job fills them
later from the recon logger's bbo feed (keep the Phase 0 logger running).

Because Sessions 2-3 run unattended on the VPS, operator tagging is POST-HOC: review
`fills_shadow.csv` during a supervision window and fill the `operator_note` column
(e.g. "rec-looking flow", "news hit", "thin book"). The `is_stress_harness` column
flags fills from the Spider-Man throughput test so they never pollute the EV-sign read.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

FIELDS = [
    "ts_ms", "session_id", "source", "is_stress_harness",
    "ticker", "family", "strike", "maker_side", "price", "count",
    "order_id", "fair_at_fill", "fair_mode", "mid_at_fill", "spread_at_fill",
    "hw_at_fill", "skew_at_fill", "inv_before", "inv_after",
    "sigma_60s", "trades_prev_60s", "mid_move_prev_60s",
    "queue_depth_ahead_at_place", "seconds_resting",
    "trade_price", "taker_side",
    "markout_1m", "markout_5m", "markout_15m",   # filled by post-processing
    "operator_note",                              # filled post-hoc by Ben
]


class FillLogger:
    def __init__(self, path: str | Path, session_id: str):
        self.path = Path(path)
        self.session_id = session_id
        if not self.path.exists():
            with open(self.path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    def log(self, fill, *, family: str, fair_mode: str, fair_at_fill: float,
            mid_at_fill: float, spread_at_fill: float, hw_at_fill: float,
            skew_at_fill: float, sigma_60s, trades_prev_60s, mid_move_prev_60s,
            queue_depth_ahead, seconds_resting: float, is_stress_harness: bool,
            order_id: str = "SIM"):
        row = {
            "ts_ms": fill.ts_ms, "session_id": self.session_id, "source": "kalshi_mm",
            "is_stress_harness": int(bool(is_stress_harness)),
            "ticker": fill.ticker, "family": family, "strike": fill.strike,
            "maker_side": fill.maker_side, "price": fill.price, "count": fill.count,
            "order_id": order_id, "fair_at_fill": round(fair_at_fill, 2),
            "fair_mode": fair_mode, "mid_at_fill": round(mid_at_fill, 2),
            "spread_at_fill": round(spread_at_fill, 2), "hw_at_fill": round(hw_at_fill, 2),
            "skew_at_fill": round(skew_at_fill, 2), "inv_before": fill.inv_before,
            "inv_after": fill.inv_after, "sigma_60s": sigma_60s,
            "trades_prev_60s": trades_prev_60s, "mid_move_prev_60s": mid_move_prev_60s,
            "queue_depth_ahead_at_place": queue_depth_ahead,
            "seconds_resting": round(seconds_resting, 1),
            "trade_price": fill.trade_price, "taker_side": fill.taker_side,
            "markout_1m": "", "markout_5m": "", "markout_15m": "", "operator_note": "",
        }
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow(row)
        return row
