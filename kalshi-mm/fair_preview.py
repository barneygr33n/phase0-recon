#!/usr/bin/env python3
"""
fair_preview.py — read-only sanity check on fair_inputs.json (Ben's chosen workflow).

You edit fair_inputs.json before a session, then run this. It:
  1. Validates every active film's mixture (weights, μ in 0-100, σ > 0 and not absurdly
     tight given we have NO RT σ history yet).
  2. Prints the resulting ladder — P(final >= K) and fair cents per strike — so you can
     eyeball whether σ and the tail mass match your read BEFORE any quote goes out.

Places no orders. Touches no network. fair_inputs.json stays the single source of truth;
this only reads it.

    python3 fair_preview.py                 # all active films, default strike grid
    python3 fair_preview.py KXRT-AVE        # one film
    python3 fair_preview.py KXRT-AVE 60 65 70 75 80 85 90   # explicit strikes
"""

import sys

from fairvalue import FairModel, DEFAULT_STRIKES, mixture_mean


def preview(model: FairModel, ticker: str, strikes: list[int]) -> bool:
    film = model.film(ticker)
    problems = model.validate(ticker)

    print("=" * 64)
    title = film.get("title", ticker)
    print(f"{ticker}  —  {title}")
    if film.get("note"):
        print(f"  NOTE: {film['note']}")
    print(f"  fair_version: {film.get('fair_version', '—')}   "
          f"active: {film.get('active')}   "
          f"bounds: [{film.get('floor', 0)}, {film.get('ceiling', 100)}]")

    # mixture components
    print("  mixture:")
    tw = sum(c["weight"] for c in film["mixture"])
    for c in film["mixture"]:
        print(f"    w={c['weight']:.3f} (={c['weight']/tw:6.1%})  "
              f"μ={c['mu']:5.1f}  σ={c['sigma']:5.2f}   {c.get('label', '')}")
    print(f"  weighted mean μ ≈ {mixture_mean(film['mixture']):.1f}")

    # reveal handling
    rd = film.get("reveal_datetime_utc")
    print(f"  reveal: {rd or 'UNKNOWN (set before live quoting)'}   "
          f"pull_lead: {film.get('reveal_pull_lead_min', '—')} min")

    # validation verdict
    if problems:
        print("  VALIDATION: ✗ PROBLEMS")
        for p in problems:
            print("    -", p)
    else:
        print("  VALIDATION: ✓ clean")

    # ladder
    print(f"\n  {'strike':>6} {'P(>=K)':>9} {'fair¢':>8}")
    print(f"  {'-'*6} {'-'*9} {'-'*8}")
    lad_ge = {k: model.fair_ge(ticker, k) for k in strikes}
    for k in strikes:
        print(f"  {k:>6} {lad_ge[k]:>8.1%} {lad_ge[k]*100:>7.1f}")
    print()
    return not problems


def main():
    args = sys.argv[1:]
    # split into ticker(s) and optional trailing integer strikes
    tickers = [a for a in args if not a.lstrip("-").isdigit()]
    strikes = [int(a) for a in args if a.lstrip("-").isdigit()] or DEFAULT_STRIKES

    model = FairModel()
    if not tickers:
        tickers = model.active_tickers()
    if not tickers:
        print("No active films in fair_inputs.json (set \"active\": true).")
        return

    all_ok = True
    for t in tickers:
        try:
            ok = preview(model, t, sorted(strikes))
            all_ok = all_ok and ok
        except KeyError as e:
            print(f"✗ {e}")
            all_ok = False

    print("=" * 64)
    print("READY to quote (validation clean)." if all_ok
          else "FIX fair_inputs.json before quoting (see problems above).")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
