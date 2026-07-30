# Kalshi MM — Phase 1, Session 1 (plumbing + safety, ZERO orders)

Built 2026-07-27. Governing docs: `KALSHI_MM_PHASE1_SCOPE.md` (§5 session plan) and
`KALSHI_MM_QUOTER_DESIGN_SPEC.md` (the "brain"). This is the supervised quoter's
skeleton in **shadow mode** — it computes would-be quotes and never places an order.
There is no order-placement code anywhere in this package (scope §7 / spec §8).

## Files
| File | Role |
|---|---|
| `quoter_config.yaml` | Single source of every ⚙ param. RT section seeded from scope §4. `mode: shadow`. |
| `fair_inputs.json` | **You edit this** before a session: per-film μ/σ mixture. Source of truth. |
| `fairvalue.py` | Fair-value module: mixture → truncated-normal ladder, `fair_ge(K)` (spec §1.5). |
| `fair_preview.py` | **Your workflow:** read-only validate + print the ladder before quoting. |
| `quoting_engine.py` | Placement/skew/never-cross/edge-gate/max-strikes (spec §2, §3.2, §1.5). Pure. |
| `kalshi_client.py` | Live REST (reads + cancel only). Reuses `phase0_logger` auth. Live-only. |
| `emergency_cancel.py` | Standalone panic button — cancels all resting orders (spec §3.3). |
| `run_shadow.py` | Session runner: reveal-guard → pre-flight → compute ladder → print. Kill switch. |
| `test_engine.py` | Offline unit tests (20 checks, all passing). |

## The fair-input workflow (your chosen path: JSON + previewer)
1. Edit `fair_inputs.json` — set the film's `mixture` (weights/μ/σ), `reveal_datetime_utc`,
   and `reveal_pull_lead_min`.
2. `python3 fair_preview.py KXRT-XXX` — validates and prints the ladder. Eyeball σ / tail
   mass against your read. It refuses (exit 1) on bad inputs (σ≤0, μ outside 0–100, etc.)
   and warns if σ is suspiciously tight (we have **no RT σ history yet** — keep it wide).

## Run it
**In this sandbox (offline — Kalshi is firewalled here):**
```
python3 fair_preview.py KXRT-AVE
python3 run_shadow.py KXRT-AVE --synthetic            # full would-be-quote ladder
python3 run_shadow.py KXRT-AVE --synthetic --demo-kill   # + fire the kill switch
python3 test_engine.py
```

**On your Mac or the VPS (where creds + Kalshi access live):**
```
python3 run_shadow.py KXRT-AVE           # LIVE ladder pull, still shadow (no orders)
python3 emergency_cancel.py --list       # dry run: list resting orders
python3 emergency_cancel.py              # panic: cancel everything
```
Requires `kalshi_credentials.json` + `kalshi_private_key.pem` in the project root and
the `websockets`/`cryptography`/`requests`/`pyyaml` deps the Phase 0 logger already uses.

## Session-1 safety proofs (done)
- **Kill switch** (`run_shadow --demo-kill`): fires cancel-all path, prints P&L, exits 0.
- **Emergency cancel**: builds the correctly-signed `/portfolio/orders` request; with no
  orders resting it reports `0 resting order(s)` — proving the path with nothing at risk.
  *(Run this on the Mac/VPS to see the live 0-count; the sandbox stops at the network.)*
- **Reveal guard**: stands down when within `reveal_pull_lead_min` of a scheduled reveal.
- **Never-cross / edge-gate / inventory-cap / hw-floor**: covered by `test_engine.py`.
- **Mode gate**: runner refuses to start unless `mode: shadow`.

## Before live minimum (Sessions 4–5) — NOT this session
- Fix the Phase 0 logger resync bug (empty-ticker resubscribe → `code 14`); quoter shares
  resync code (scope §3.2, companion task).
- Pull-on-gap wiring against the real WS feed (currently a hook; shadow can't be picked off).
- Pick the debut film + set its real reveal datetime in `fair_inputs.json`.

## Deliberately NOT built (resist scope creep — scope §7 / spec §8)
No GUI, no order placement, no taking liquidity, no auto re-entry, no multi-film portfolio
logic beyond the total cap, no mention markets, no 24/7. v1's product is clean learning data.
