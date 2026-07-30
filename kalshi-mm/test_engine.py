#!/usr/bin/env python3
"""
test_engine.py — deterministic unit tests for the fair-value + quoting logic.
Runs fully offline (no Kalshi). Covers the paths the synthetic runner doesn't force:
never-cross capping, inventory-cap skip, skew direction, hw floor, fair monotonicity.

    python3 test_engine.py     # prints PASS/FAIL per check, exits nonzero on any fail
"""

import sys

from fairvalue import FairModel, trunc_sf, mixture_ge
from quoting_engine import QuotingEngine, StrikeInput, load_config

FAILS = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def approx(a, b, tol=0.05):
    return abs(a - b) <= tol


def test_fairvalue():
    print("fair-value:")
    # truncated SF bounds
    check("trunc_sf below floor == 1", approx(trunc_sf(-5, 80, 10, 0, 100), 1.0))
    check("trunc_sf above ceiling == 0", approx(trunc_sf(105, 80, 10, 0, 100), 0.0))
    check("trunc_sf at mu ~ 0.5-ish (symmetric-ish)", 0.45 <= trunc_sf(80, 80, 10, 0, 100) <= 0.55)
    # mixture monotone non-increasing in K
    mix = [{"weight": 0.5, "mu": 79.4, "sigma": 10.0}, {"weight": 0.5, "mu": 83.5, "sigma": 4.45}]
    vals = [mixture_ge(k, mix) for k in range(50, 100, 5)]
    check("mixture monotone non-increasing", all(vals[i] >= vals[i+1] - 1e-9 for i in range(len(vals)-1)))
    check("mixture prob in [0,1]", all(0 <= v <= 1 for v in vals))
    # model matches ave_fair_tracker ballpark (>=75 ~ 82%, >=85 ~ 32%)
    m = FairModel()
    check("AVE fair(>=75) ~ 82c", approx(m.fair_cents("KXRT-AVE", 75), 82, tol=1.5))
    check("AVE fair(>=85) ~ 32c", approx(m.fair_cents("KXRT-AVE", 85), 32, tol=1.5))
    check("validate seeded AVE clean", m.validate("KXRT-AVE") == [])
    # a deliberately-bad film is caught
    m.films["BAD"] = {"active": True, "mixture": [{"weight": 1, "mu": 120, "sigma": -2}]}
    probs = m.validate("BAD")
    check("validate catches bad mu/sigma", any("mu" in p for p in probs) and any("sigma" in p for p in probs))


def test_engine():
    print("quoting engine:")
    cfg = load_config()
    eng = QuotingEngine(cfg, "rotten_tomatoes")

    # 1) never-cross: fair 99 far above a 95 ask -> raw buy (96) would cross -> cap 1c inside (94)
    q = eng.quote_strike(StrikeInput(strike=80, fair_c=99.0, yes_bid=90, yes_ask=95, net_inv=0))
    buy = [x for x in q if x.side == "yes_buy"][0]
    check("never-cross: buy price < best ask", buy.price < 95)
    check("never-cross: buy capped to 94 (1c inside 95)", buy.price == 94)
    check("never-cross: flagged in reason", "cross" in buy.reason)
    # symmetric sell-side cross: fair 5 far below a 10 bid -> sell would cross -> cap to 11
    q2 = eng.quote_strike(StrikeInput(strike=80, fair_c=5.0, yes_bid=10, yes_ask=15, net_inv=0))
    sell = [x for x in q2 if x.side == "yes_sell"][0]
    check("never-cross: sell price > best bid (capped 11)", sell.price == 11)

    # 2) hw floor: with zero vol, hw == max(hw_min, hw_base)
    calm = eng.quote_strike(StrikeInput(80, 60.0, 55, 65, net_inv=0))[0]
    check("hw uses hw_base when calm", approx(calm.hw_c, cfg["families"]["rotten_tomatoes"]["hw_base"]))
    # hw floor never below hw_min even if hw_base somehow tiny
    eng2 = QuotingEngine({**cfg, "families": {"rotten_tomatoes": {**eng.fam, "hw_base": 0.2}}}, "rotten_tomatoes")
    check("hw floored at hw_min", eng2._hw(None) == eng.fam["hw_min"])

    # 3) skew direction: long inventory lowers center (leans to sell)
    base = eng.quote_strike(StrikeInput(80, 60.0, 55, 65, net_inv=0))[0].center_c
    long = eng.quote_strike(StrikeInput(80, 60.0, 55, 65, net_inv=10))[0].center_c
    check("long inventory lowers center", long < base)

    # 4) inventory cap: at +max_inv, YES buy side is skipped (stop accumulating)
    cap = cfg["risk"]["max_inv_market"]
    qc = eng.quote_strike(StrikeInput(80, 60.0, 55, 65, net_inv=cap))
    buy_c = [x for x in qc if x.side == "yes_buy"][0]
    check("at long cap: buy side skip_inv_cap", buy_c.status == "skip_inv_cap")

    # 5) min-edge gate: a fair sitting inside the spread yields sub-min edge -> skip
    qe = eng.quote_strike(StrikeInput(80, 50.0, 48, 52, net_inv=0))
    # buy would be floor(50-3)=47 < bid... edge = 50-47-0.4=2.6 >= 1 -> post; force a thin one:
    thin = eng.quote_strike(StrikeInput(80, 50.0, 49, 50, net_inv=0))
    buy_thin = [x for x in thin if x.side == "yes_buy"][0]
    check("thin market never crosses ask (<=50)", buy_thin.price <= 49)

    # 6) prices always clamped to 1..99
    q_tail = eng.quote_strike(StrikeInput(95, fair_c=99.9, yes_bid=98, yes_ask=100, net_inv=0))
    check("prices clamped 1..99", all(1 <= x.price <= 99 for x in q_tail))


def main():
    test_fairvalue()
    test_engine()
    print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
