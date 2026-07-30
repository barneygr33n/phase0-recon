#!/usr/bin/env python3
"""
kalshi_client.py — thin REST client for the quoter. LIVE-ONLY (needs creds + network).

Reuses phase0_logger's signing (load_credentials / rest_headers) so there is ONE
signing implementation in the project — no duplicated auth (same pattern as
ave_fair_tracker). Runs where the creds live: Ben's Mac or the DigitalOcean VPS.
It will NOT work from the Cowork sandbox (Kalshi is firewalled there).

Read methods (exchange status, market ladder, orderbook, resting orders) and ONE
write method (cancel). This module places NO orders — order placement is deliberately
not implemented in Phase 1 (spec §8: no taking, and shadow session = zero orders).
Cancel exists only for the kill switch / emergency script.
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

# Reuse the shared auth from the project root (parent of kalshi-mm/). phase0_logger
# imports `websockets` at module load (for the WS recon logger) — we don't need it
# here, so if it's absent we fall back to an INLINE COPY of the exact same signing.
# Keep the fallback byte-for-byte identical to phase0_logger.load_credentials /
# rest_headers so there is never a signing divergence.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    import phase0_logger as L  # noqa: E402  load_credentials(), rest_headers()
    _load_credentials, _rest_headers = L.load_credentials, L.rest_headers
except (Exception, SystemExit):  # phase0_logger sys.exit()s if websockets is absent; REST auth needs none of it
    import base64
    import json
    import time
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    _CREDS = ROOT / "kalshi_credentials.json"

    def _load_credentials():
        with open(_CREDS) as f:
            creds = json.load(f)
        with open(ROOT / creds["private_key_path"], "rb") as f:
            pk = serialization.load_pem_private_key(f.read(), password=None)
        return creds["api_key_id"], pk

    def _rest_headers(api_key_id, private_key, method, full_path):
        ts = str(int(time.time() * 1000))
        sig = private_key.sign(
            (ts + method.upper() + full_path).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

DEFAULT_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SIGN_PREFIX = "/trade-api/v2"


class KalshiClient:
    def __init__(self, rest_base: str = DEFAULT_BASE, sign_prefix: str = SIGN_PREFIX):
        self.base = rest_base.rstrip("/")
        self.prefix = sign_prefix
        self.kid, self.pk = _load_credentials()

    def _req(self, method: str, path: str, params=None, body=None):
        headers = _rest_headers(self.kid, self.pk, method, self.prefix + path)
        url = self.base + path
        r = requests.request(method, url, headers=headers, params=params or {},
                             json=body, timeout=20)
        r.raise_for_status()
        return r.json() if r.text else {}

    # ── reads ────────────────────────────────────────────────────────────────
    def exchange_status(self) -> dict:
        return self._req("GET", "/exchange/status")

    def ladder(self, event_ticker: str) -> dict[int, dict]:
        """{strike:int -> {yes_bid, yes_ask, volume_24h}} for open markets of an event."""
        ms = self._req("GET", "/markets",
                       {"event_ticker": event_ticker, "status": "open", "limit": 100}
                       ).get("markets", [])
        out: dict[int, dict] = {}
        for m in ms:
            t = m.get("ticker", "")
            strike = t.split("-")[-1]
            if not strike.isdigit():
                continue
            d = self._req("GET", f"/markets/{t}").get("market", {})
            yb, ya = d.get("yes_bid_dollars"), d.get("yes_ask_dollars")
            if yb is None or ya is None:
                continue
            out[int(strike)] = {
                "ticker": t,
                "yes_bid": round(float(yb) * 100, 1),
                "yes_ask": round(float(ya) * 100, 1),
                "volume_24h": float(d.get("volume_24h_fp") or 0),
            }
        return out

    def resting_orders(self) -> list[dict]:
        """All resting (open) orders on the account, paginated."""
        orders, cursor = [], None
        while True:
            params = {"status": "resting", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._req("GET", "/portfolio/orders", params)
            batch = data.get("orders", [])
            orders.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return orders

    def positions(self) -> list[dict]:
        data = self._req("GET", "/portfolio/positions", {"limit": 200})
        return data.get("market_positions", []) or data.get("positions", [])

    # ── the ONLY write: cancel (kill switch / emergency) ─────────────────────
    def cancel_order(self, order_id: str) -> dict:
        return self._req("DELETE", f"/portfolio/orders/{order_id}")

    def cancel_all(self, verbose: bool = True) -> int:
        """Cancel every resting order. Returns count cancelled. Safe when there are none."""
        orders = self.resting_orders()
        if verbose:
            print(f"cancel_all: {len(orders)} resting order(s) found.")
        n = 0
        for o in orders:
            oid = o.get("order_id") or o.get("id")
            if not oid:
                continue
            try:
                self.cancel_order(oid)
                n += 1
                if verbose:
                    print(f"  cancelled {oid}  ({o.get('ticker','?')} "
                          f"{o.get('side','?')} {o.get('yes_price','?')}¢)")
            except Exception as e:  # keep going — cancel is best-effort panic
                if verbose:
                    print(f"  FAILED to cancel {oid}: {e}")
        if verbose:
            print(f"cancel_all: {n}/{len(orders)} cancelled.")
        return n
