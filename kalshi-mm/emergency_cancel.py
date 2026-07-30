#!/usr/bin/env python3
"""
emergency_cancel.py — standalone panic button (design spec §3.3).

Cancels EVERY resting order on the Kalshi account. Needs only credentials; does not
import the quoter's state, config, or fair model — so it works even if the quoter has
crashed or is wedged. This is the script you run by hand (or a watchdog runs) when
something is wrong.

    python3 emergency_cancel.py            # cancel everything, now
    python3 emergency_cancel.py --list     # dry run: just list resting orders, cancel nothing

LIVE-ONLY: needs creds + Kalshi network access. Run on the Mac or the VPS, not the
Cowork sandbox. Session-1 test: with no orders resting it must report
"0 resting order(s)" and exit 0 — proving the path works with nothing at risk.
"""

import sys

from kalshi_client import KalshiClient


def main():
    dry = "--list" in sys.argv
    client = KalshiClient()

    if dry:
        orders = client.resting_orders()
        print(f"{len(orders)} resting order(s):")
        for o in orders:
            print(f"  {o.get('order_id', o.get('id','?'))}  {o.get('ticker','?')}  "
                  f"{o.get('side','?')}  {o.get('yes_price','?')}¢  x{o.get('remaining_count','?')}")
        if not orders:
            print("  (none — nothing at risk)")
        return 0

    print("EMERGENCY CANCEL — cancelling all resting orders...")
    n = client.cancel_all(verbose=True)
    print(f"Done. {n} order(s) cancelled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
