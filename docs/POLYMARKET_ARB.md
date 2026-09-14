# Polymarket intra-venue arbitrage: paper measurement tracks

Paper-only. Nothing here places an order; every leg is simulated against a
frozen public book and booked into a `PaperLedger`. Live flags are refused.

## What is measured

Three tracks read one frozen snapshot of Polymarket events (top-N by liquidity
from Gamma `/events`) with **both** the YES and the NO CLOB book for every leg
(`POST /books`, batched). Each track sizes depth-aware, deducts the published
taker fee and a per-leg slippage buffer, and only then paper-fills.

| Track | Structure | Payoff | Executable now? | Literature |
| --- | --- | --- | --- | --- |
| `polymarket_rebalancing_arb` | binary: buy YES + buy NO < 1 (**merge**), or split 1 USDC and sell YES + NO > 1 (**split**) | 1 USDC per pair via CTF `mergePositions` / `splitPosition` | yes | arXiv:2508.03474 (rebalancing) |
| `polymarket_negrisk_arb` | NegRisk event with K visible legs: buy one NO on every leg for < K−1 | K−1 USDC per set via `NegRiskAdapter.convertPositions` | yes (converter is one-way NO→collateral) | arXiv:2608.00666 |
| `polymarket_combinatorial_arb` | exclusive event: buy one YES on every leg for < 1 | 1 USDC per set **at resolution** | no — held until `end_date` | arXiv:2508.03474 (sum-to-one) |

Refusal reasons are stable strings and counted per track: `no_edge`,
`fees_exceed_edge`, `slippage_exceeds_edge`, `empty_book`, `below_min_order_size`,
`capital_cap`, `converter_unavailable` (not a NegRisk event),
`exclusivity_unverified` (not NegRisk and not declared exclusive),
`hidden_outcome_risk` (augmented event: hidden placeholder can win, zeroing every
visible YES), `risk_capacity_below_min_order_size`.

## How to run

```bash
# Fixtures (deterministic; the fixture events are documented scenarios)
python -m apps.measure_polymarket_arb --artifact-dir artifacts

# Public network read (no keys): top 40 events by liquidity
python -m apps.measure_polymarket_arb --network --limit 40 --artifact-dir artifacts

# Same tracks as part of the full 8-track board
python -m apps.measure_all --network --kalshi-env prod --event-limit 40

# Sensitivity: book-frozen view without fees or slippage (what "gross" would admit)
python -m apps.measure_polymarket_arb --network --limit 40 --no-fees --slippage-ticks 0 --artifact-dir /tmp/arb-gross
```

Flags shared by both CLIs: `--slippage-ticks` (default 1 per leg), `--max-sets`
(100), `--max-capital` (250 USDC per opportunity), `--min-net-edge` (0.001 per
set), `--converter-fee-bps` (0), `--no-fees`. Ledgers persist under
`artifacts/paper/ledger_<track>.json` unless `--no-persist` / `--reset-ledgers`.

Outputs of the dedicated CLI:

| Path | Contents |
| --- | --- |
| `artifacts/scoreboard_polymarket_arb.json` | dashboard artifact, `meta.source=measured`, `meta.track_family=polymarket_arb`, `meta.primary_track=polymarket_negrisk_arb` |
| `artifacts/polymarket_arb_latest.json` | full opportunity report: every group (sums of YES bids / YES asks / NO asks, gross, fee and slippage per set, reason), every binary market with gross > 0, mirror statistics, conversions, holdings, ledgers |
| `artifacts/paper/ledger_<track>.json`, `paper/runs/<run_id>.json` | ledgers and the run manifest that the dashboard history indexes |

`scoreboard_latest.json` is **not** overwritten by the dedicated CLI (pass
`--publish-latest` to do so); `measure_all` writes it as usual with all 8 tracks.

Publish to the dashboard:

```bash
cd dashboard && npm run sync-artifacts   # copies the arb scoreboard + report + NegRisk ledger,
                                         # writes runs/<run_id>.json, rebuilds experiments_index.json
git add public/artifacts && git commit -m "Refresh Polymarket arb snapshots"
```

## Accounting

* **Merge / split** legs are two fills in the same market with opposite signed
  quantity, so the position nets to flat and realized PnL is
  `sets × (1 − YES_ask − NO_ask) − fees` (or `sets × (YES_bid + NO_bid − 1) − fees`).
* **NegRisk conversion** is booked explicitly: after the K NO legs fill, the
  complete sets (`min` filled across legs) are closed with
  `order_id = "negrisk_convert"` at YES-equivalent prices summing to
  `1 + (K−1)·converter_fee`, which credits exactly `(K−1)·sets·(1 − fee)` USDC
  of collateral. Any legged residual stays open, marked at mid, and is reported
  as `legging_residual_contracts`. Hidden-placeholder YES tokens received on an
  augmented event are valued at zero.
* **Buy-all-YES** positions stay open and are marked at the book mid, so the
  scoreboard shows the mid-mark (a small negative on entry), never the
  resolution payoff. `metrics.holdings[]` carries `cost`, `payoff_at_resolution`,
  `profit_at_resolution`, `lockup_until`; `metrics.locked_capital` is the sum.
  The artifact adds the risk flag `combinatorial_positions_marked_at_mid_not_resolution`.
* Fees: `C × rate × p × (1−p)` per taker leg, rate from Gamma `feeType`
  (politics 0.04, sports/economics/culture 0.05, crypto 0.07, geopolitics 0,
  unknown fee-enabled type → 0.07). `takerBaseFee = 1000` is the on-chain
  ceiling and is ignored. Rounded to 5 dp like the venue.
* Slippage: `slippage_ticks × tick_size` per leg is deducted at admission; the
  paper fill itself walks the frozen book at book prices, so ledger realized PnL
  is the *book-frozen* outcome and `estimated_fees_buffer` holds the buffer.
* Every leg passes the track's `RiskManager` (100 USDC notional per order, 500
  contracts per market, cumulative loss rail). If risk headroom caps the set
  count below the venue minimum order size (5), the opportunity is refused.

## What was validated

Fixtures (`venues/polymarket/fixtures/events.json`, each event carries a
`scenario` string; `tests/test_polymarket_arb.py`, 17 tests):

| Scenario | Expected | Result |
| --- | --- | --- |
| 4-leg NegRisk, NO asks sum 2.95, depth thins on legs B/C | 40 sets (30 at 2.95 + 10 at 2.96, stop when gross 0.03 < fees+slip); fees `1.14556`; 4 `negrisk_convert` fills whose prices sum to 1; collateral 120 vs 118.10 paid | pass |
| 3-leg NegRisk, YES asks sum 0.95, not augmented | 60 sets held; cost 57; unrealized −1.80 at mid; `lockup_until 2027-01-15` | pass |
| augmented NegRisk, visible YES asks sum 0.94 | refused `hidden_outcome_risk` (gross 0.06 reported) | pass |
| 3 related non-NegRisk markets, YES asks sum 0.96 | refused `exclusivity_unverified` / `converter_unavailable` | pass |
| fee-free binary, YES ask 0.45 + NO ask 0.53 | merge 80 pairs (NO depth), realized 1.60, position flat | pass |
| same books, sports 5 % fee | refused `fees_exceed_edge` (fees 0.0248 > gross 0.02) | pass |
| fee-free binary, YES bid 0.52 + NO bid 0.50 | split 60 pairs, realized 1.20 | pass |
| mirror-consistent binary (live shape) | `mirror_consistent`, `no_edge`; ask sum 1.02, bid sum 0.98 | pass |
| capital cap 30 USDC / 10 USDC; set cap 7 | 10 sets / `below_min_order_size` / 7 sets | pass |
| `--no-fees` | detector and fills both fee-free; NO orders walk the real NO ladder (0.80 then 0.81 on leg C) | pass |
| Gamma `/events` + `POST /books` payload parse (MockTransport) | closed legs dropped, `feeType → 0.04` not `takerBaseFee`, one batched request with both tokens per leg, wire order re-sorted, mirror detected | pass |
| dedicated CLI | writes `scoreboard_polymarket_arb.json`, not `scoreboard_latest.json`; refuses `TRADING_MODE=live` | pass |
| ledger identity `equity == starting_cash + realized + unrealized` | asserted after every track | pass |

Network dry-run (public endpoints, no keys, 2026-09-14 21:05 UTC), committed as
`dashboard/public/artifacts/scoreboard_polymarket_arb.json`,
`dashboard/public/artifacts/polymarket_arb_latest.json` and
`dashboard/public/artifacts/runs/20260914T210549Z-519e2135.json`
(run id `20260914T210549Z-519e2135`; the full 8-track board from the same
minute is `runs/20260914T210541Z-cd15a2c5.json`; the current committed `scoreboard_latest.json`
is the 9-track board `runs/20260914T212414Z-70a904d9.json`, which includes the `news_underreaction`
lane and reports the same arb outcome: 0 admitted on all three tracks):

* 40 events, 1319 legs, 2 legs with a missing book (refused `empty_book`).
* **Rebalancing:** 1010 binary markets checked, **0 admitted**. Top-of-book
  `YES_ask + NO_ask`: min 1.001, median 1.01 (never below 1);
  `YES_bid + NO_bid`: max 0.999. 1001/1010 YES and NO books were exact mirrors at
  every level; the 9 exceptions differed on deep levels with a 0–6 ms timestamp
  skew between the two ladders inside one batch response and re-fetching showed
  exact mirrors again. **Finding:** the CLOB matches complementary orders, so the
  resting books cannot show a YES+NO ≠ 1 gap; the rebalancing edge documented in
  arXiv:2508.03474 lives in fills/timing, not in the displayed book.
* **NegRisk convert:** 39 multi-outcome groups (31 NegRisk, 26 augmented),
  **0 admitted**. Two genuine gross edges existed — "Pro Football: 2027 Champion"
  (32 legs, YES bids sum 1.022 → gross 0.022/set) and "UEFA Champions League:
  2027 Champion" (36 legs, 1.002 → 0.002/set) — and both were refused
  `fees_exceed_edge`: the 5 % sports fee costs 0.048 and 0.044 per set. With
  `--no-fees --slippage-ticks 0` they fill and convert for 8 and 7 sets,
  +0.19 USDC realized on 493 USDC deployed (~4 bps). This is the pattern of
  arXiv:2608.00666 (dead-longshot bids at 0.001 push the bid sum over 1),
  measured today as sub-fee.
* **Buy-all-YES:** 39 groups, **0 admitted**; 26 refused `hidden_outcome_risk`
  (augmented), 8 `exclusivity_unverified`, 2 `no_edge`. The lowest visible
  YES-ask sums (Nobel Peace Prize 0.666, Republican nominee 0.895) belong to
  augmented events where the hidden "other" placeholder carries the missing mass.

Ledger PnL for all three network tracks is `0.0000` because nothing was
admitted; the fixture runs in the history (`20260914T210532Z-de84053d`, `20260914T212403Z-a763b42e`)
shows the fills, conversion and lockup accounting on the documented scenarios.

## Known limits

* **One-way converter.** `NegRiskAdapter` converts NO sets to YES + collateral,
  never YES to collateral. Buy-all-YES therefore locks capital until
  resolution (`lockup_until`), and the ledger marks it at mid until then.
* **Capital lockup and rails.** A NegRisk set costs about K−1 USDC (≈31 USDC
  for a 32-leg event), so the default 250 USDC cap admits at most 8 sets and
  the 5-share minimum order size refuses anything below 5 sets.
* **Augmented events.** Visible YES tokens are not exhaustive; buy-all-YES is
  refused (opt in with `allow_hidden_outcome_long_yes`). Hidden-placeholder
  YES received from a conversion is valued at zero (conservative).
* **Full-set conversion only.** Subset conversions (buy NO on S ⊂ legs,
  convert, sell the YES received) are not sized; with mirror books their gross
  equals the full-set gross and they add sell legs and fees.
* **Legging.** Legs are sized on one frozen snapshot. The slippage buffer (1
  tick per leg by default) is a stand-in for the book moving between legs; on
  a 36-leg set it is 0.036–0.099 per set and often the binding constraint.
  `legging_residual_contracts` reports partial sets left open.
* **Fee assumptions.** Rate by `feeType` prefix per the published table; maker
  rebates and taker-rebate tiers are ignored; the NegRisk converter fee is
  assumed 0 (`--converter-fee-bps` to change). No demo-account statement was
  available to reconcile.
* **Snapshot skew.** Batched `/books` responses can carry per-token timestamps
  a few ms apart; `mirror_inconsistent_markets[].book_timestamp_skew_ms` records it.
* **Universe.** Top-N events by Gamma `liquidity`; long-tail events are not
  scanned. Legs without a book in the batch response are refused `empty_book`.
* **Not implemented.** Implication-based combinatorial arbitrage across
  different events (e.g. nominee vs. election winner), cross-event dependency
  graphs, and any live or authenticated path. `place_order` on a non-paper
  client still raises; `PolymarketL2Signer` / `SignedOrderBuilder` remain stubs.
