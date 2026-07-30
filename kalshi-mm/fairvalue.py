#!/usr/bin/env python3
"""
fairvalue.py — Fair-value module for the supervised quoter (design spec §1 / §1.5).

Turns Ben's per-film mixture (μ/σ blend, from fair_inputs.json) into a DISTRIBUTION
over the final Tomatometer, then a fair price per strike for the whole ladder:

    fair_ge(K) = P(final score >= K)   under the truncated-normal mixture
    fair_cents(K) = 100 * fair_ge(K)

One distribution prices every strike, so the ladder is arbitrage-consistent by
construction (fair is monotone non-increasing in K).

Math is lifted from ave_fair_tracker.fair_ge (the daily fair-vs-market tracker),
extended with truncation to [floor, ceiling] per scope §2 ("truncated-normal on
[0,100]"). No network, no orders — pure computation.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import NormalDist

SCRIPT_DIR = Path(__file__).parent
FAIR_INPUTS = SCRIPT_DIR / "fair_inputs.json"

# Standard RT ladder strikes for previews when the live ladder isn't supplied.
DEFAULT_STRIKES = [50, 55, 60, 65, 70, 75, 80, 85, 90, 95]


# ── Core distribution math ───────────────────────────────────────────────────
def trunc_sf(k: float, mu: float, sigma: float, a: float, b: float) -> float:
    """P(X >= k) for Normal(mu, sigma) truncated to [a, b]. Bounds-safe."""
    if sigma <= 0:  # degenerate point mass at mu
        return 1.0 if k <= mu else 0.0
    N = NormalDist(mu, sigma)
    Fa, Fb = N.cdf(a), N.cdf(b)
    denom = Fb - Fa
    if denom <= 0:
        return 1.0 if k <= mu else 0.0
    kk = min(max(k, a), b)          # clamp: k<=a -> 1.0, k>=b -> 0.0
    return (Fb - N.cdf(kk)) / denom


def mixture_ge(k: float, mixture: list[dict], a: float = 0.0, b: float = 100.0) -> float:
    """Ben's fair P(final >= k) as a fraction, weighted truncated-normal mixture."""
    tw = sum(c["weight"] for c in mixture)
    if tw <= 0:
        raise ValueError("mixture weights sum to <= 0")
    return sum(c["weight"] * trunc_sf(k, c["mu"], c["sigma"], a, b) for c in mixture) / tw


def mixture_mean(mixture: list[dict]) -> float:
    """Weighted mean of the component μ's (ignores truncation — a quick reference)."""
    tw = sum(c["weight"] for c in mixture)
    return sum(c["weight"] * c["mu"] for c in mixture) / tw


# ── Per-film model ───────────────────────────────────────────────────────────
class FairModel:
    """Loads fair_inputs.json and prices any strike for any active film."""

    def __init__(self, path: str | Path = FAIR_INPUTS):
        self.path = Path(path)
        with open(self.path) as f:
            self.raw = json.load(f)
        self.films = self.raw.get("films", {})

    def active_tickers(self) -> list[str]:
        return [t for t, f in self.films.items() if f.get("active")]

    def film(self, event_ticker: str) -> dict:
        if event_ticker not in self.films:
            raise KeyError(f"{event_ticker} not in {self.path.name}")
        return self.films[event_ticker]

    def _bounds(self, film: dict) -> tuple[float, float]:
        return float(film.get("floor", 0)), float(film.get("ceiling", 100))

    def fair_ge(self, event_ticker: str, k: float) -> float:
        film = self.film(event_ticker)
        a, b = self._bounds(film)
        return mixture_ge(k, film["mixture"], a, b)

    def fair_cents(self, event_ticker: str, k: float) -> float:
        return round(self.fair_ge(event_ticker, k) * 100.0, 1)

    def ladder(self, event_ticker: str, strikes: list[int] | None = None) -> dict[int, float]:
        """Return {strike: fair_cents} for the requested strikes (or DEFAULT_STRIKES)."""
        strikes = strikes if strikes is not None else DEFAULT_STRIKES
        return {int(k): self.fair_cents(event_ticker, k) for k in sorted(strikes)}

    # ── Validation (used by fair_preview before any session) ─────────────────
    def validate(self, event_ticker: str) -> list[str]:
        """Return a list of human-readable problems; empty list == clean."""
        problems: list[str] = []
        film = self.film(event_ticker)
        mix = film.get("mixture")
        if not mix:
            return [f"{event_ticker}: no mixture components"]
        tw = 0.0
        for i, c in enumerate(mix):
            for key in ("weight", "mu", "sigma"):
                if key not in c:
                    problems.append(f"{event_ticker} component {i}: missing '{key}'")
            w, mu, sg = c.get("weight", 0), c.get("mu", None), c.get("sigma", None)
            if w is not None and w < 0:
                problems.append(f"{event_ticker} component {i}: negative weight {w}")
            tw += (w or 0)
            if mu is not None and not (0 <= mu <= 100):
                problems.append(f"{event_ticker} component {i}: mu {mu} outside 0-100")
            if sg is not None and sg <= 0:
                problems.append(f"{event_ticker} component {i}: sigma {sg} must be > 0")
            if sg is not None and sg < 1.0:
                problems.append(f"{event_ticker} component {i}: sigma {sg} suspiciously tight "
                                f"(<1 pt) — RT has NO σ history yet, keep it wide (scope §2)")
        if tw <= 0:
            problems.append(f"{event_ticker}: weights sum to {tw} (must be > 0)")
        a, b = self._bounds(film)
        if a >= b:
            problems.append(f"{event_ticker}: floor {a} >= ceiling {b}")
        # ladder monotonicity sanity (should always hold; catches numeric surprises)
        lad = self.ladder(event_ticker, DEFAULT_STRIKES)
        vals = [lad[k] for k in sorted(lad)]
        if any(vals[i] < vals[i + 1] - 1e-6 for i in range(len(vals) - 1)):
            problems.append(f"{event_ticker}: ladder not monotone non-increasing (bug)")
        return problems


if __name__ == "__main__":
    # Quick self-check against the seeded AVE film.
    m = FairModel()
    for t in m.active_tickers():
        probs = m.validate(t)
        print(f"{t}: {'OK' if not probs else 'PROBLEMS'}")
        for p in probs:
            print("  -", p)
        print("  ladder:", m.ladder(t))
