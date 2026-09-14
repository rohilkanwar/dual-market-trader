# Kalshi favorite–longshot bias (FLB) paper track — runbook

Paper-only. Nothing here places an order; the Kalshi adapter reads four
unauthenticated endpoints (`/markets`, `/markets/{ticker}/orderbook`,
`/events/{ticker}`, `/series/{ticker}`) plus `/markets/trades` for the ex-post
harvest. Demo keys are not needed and not used. Every entry point refuses to
start if `TRADING_MODE=live` or `ENABLE_LIVE_TRADING=true`.

## What it measures

Two questions, kept separate on purpose:

1. **Does FLB appear in the current public books?** No snapshot can answer this:
   FLB is realised return versus price, and a snapshot has prices but no outcomes.
   The report says so explicitly (`flb_identifiable_from_snapshot = NOT_IDENTIFIABLE`)
   and measures only what a snapshot *can* show — the cost of taking each price band
   and the overround of mutually exclusive events.
2. **Do longshot takers lose ex post, and does the maker side earn it?** Kalshi's
   public trade tape carries `taker_side`; settled markets carry `result`. Bucketing
   trades by the price the taker paid gives taker and maker returns per band after
   the venue fee schedule — the Bürgi/Deng/Whelan design. This is where PASS/FAIL
   for FLB is decided.

Alongside, two paper tracks simulate being the counterparty to longshot buyers on the
live snapshot, through the same risk-gated engine and `PaperLedger` as every other
track:

| Track | Simulates | Fees | Marks | Capital |
| --- | --- | --- | --- | --- |
| `kalshi_longshot_fade` | Taker fade: when one side is priced below 20¢, buy the favourite at the touch | taker `round_up(M·0.07·C·P·(1−P))` | mid | $25/order, $75/market, $75 daily loss, $1,000 total collateral |
| `kalshi_maker_quote` | Resting fade one tick inside the spread (or joining the touch); expected-value fills | maker `round_up(M·0.0175·C·P·(1−P))` where the series charges makers | conservative (longs at bid, shorts at ask) | same; unfilled resting orders reserve collateral |

`kalshi_longshot_fade` also books the mirror **shadow longshot buyer** (buy the
longshot at its ask with the same caps) so the artifact shows both sides of the trade.

Literature: Bürgi, Deng, Whelan — Kalshi makers vs takers and the favorite-longshot
bias (UCD WP / SSRN 5502658); favorite-longshot bias on Polymarket (arXiv 2609.12878).

## Run it

```bash
uv sync --extra dev

# Deterministic fixture run (no network): exercises every path incl. a synthetic ex-post PASS
uv run python -m apps.measure_flb --artifact-dir artifacts

# Network: open-book snapshot of the macro canary series + paper tracks. Ex-post is
# "not_measured" until a settled-trade harvest exists in --harvest-dir.
uv run python -m apps.measure_flb --network --kalshi-env prod --artifact-dir artifacts

# Network + harvest settled markets and their public trades (≈30 s for the defaults),
# then compute the ex-post band table and verdicts. The harvest is written to
# data/harvests/kalshi_settled_trades.json (git-ignored) and re-used by later runs.
uv run python -m apps.measure_flb --network --kalshi-env prod --harvest-trades \
    --settled-per-series 30 --max-trades-per-market 4000 --artifact-dir artifacts
```

Useful flags: `--series` (open-book universe; default `DEFAULT_MACRO_SERIES`),
`--expost-series` (harvest universe; default macro + `KXMLBGAME KXNFLGAME KXNCAAFGAME`),
`--longshot-threshold`, `--join-fill-probability`, `--improve-fill-probability`,
`--adverse-selection-haircut`, `--max-total-cash-at-risk`, `--min-markets`,
`--min-contracts`, `--exclude-final-minutes`, `--no-persist`, `--reset-ledgers`, `--no-fees`.

The two tracks also run inside every `apps.measure_all` / `apps.paper_loop` cycle on the
shared snapshot (tracks 6 and 7), so the main scoreboard carries them too.

### Outputs (`--artifact-dir`, default `artifacts/`)

| Path | Contents |
| --- | --- |
| `flb_report_latest.json` | Headline, verdict table, snapshot band table + event overround, per-track band PnL, shadow buyer, ex-post band tables (all trades / excluding final minutes / by category), assumptions |
| `scoreboard_flb.json` | Scoreboard artifact (schema 1.2.0, `meta.source=measured`, `meta.kind=kalshi_flb`, `venues=["kalshi"]`, `primary_track=kalshi_maker_quote`) with `findings.kalshi_flb` verdicts |
| `paper/ledger_kalshi_longshot_fade.json`, `paper/ledger_kalshi_maker_quote.json` | Ledgers, carried across runs (also written by `measure_all`) |
| `paper/equity_curve_<track>.jsonl` | One point per run |
| `paper/runs/<run_id>.json` | Run record (`kind: kalshi_flb`) picked up by the dashboard experiments index |

Publish to the dashboard: `cd dashboard && npm run sync-artifacts` copies
`scoreboard_flb.json`, `flb_report_latest.json` and both FLB ledgers into
`public/artifacts/` and rebuilds `experiments_index.json`.

## Reading the verdicts

| Check | Scope | Meaning of PASS |
| --- | --- | --- |
| `flb_identifiable_from_snapshot` | snapshot | never PASS; `NOT_IDENTIFIABLE` (or `INSUFFICIENT_DATA` with no books) |
| `longshot_take_cost_exceeds_favorite` | snapshot | mean (half-spread + taker fee)/ask over `<20¢` legs > over `≥80¢` legs (≥3 markets each side) |
| `event_overround_positive` | snapshot | mutually exclusive events quote `Σ ask − 1 > 0` |
| `ex_post_flb` | ex post | longshot (`<20¢`) takers: contract-weighted gross return < 0, market-clustered t ≤ −2, ROI below favourite (`≥80¢`) takers' ROI; needs ≥10 markets and ≥1,000 contracts |
| `ex_post_flb_equal_weighted_markets` | ex post | same test with one vote per market (a mega-volume market cannot dominate) |
| `ex_post_flb_excluding_final_minutes` | ex post | contract-weighted test after dropping trades in the final 60 minutes before close (end-game trades at 1¢/99¢) |
| `ex_post_favorite_longshot_slope` | ex post | equal-weighted: takers lose below 50¢ (t ≤ −2) and gain above 50¢ (t ≥ 2) |
| `maker_fade_edge_after_fees` | ex post | maker side of longshot trades: net return per contract > 0 with t ≥ 2 |

Statistics: every trade in a market shares one outcome, so **markets are the unit of
inference**. Band means are contract-weighted across markets; the standard error is
clustered by market and `effective_n_markets` shows how concentrated the weights are.
`taker_roi` is return on dollars staked (a 1¢ loser is −100%), `maker_roi_net` is return
on collateral posted.

## Measured on 2026-09-14 (prod public API, no credentials)

Run `20260914T213812Z-flb-f76ab826`; committed as
`dashboard/public/artifacts/flb_report_latest.json` / `scoreboard_flb.json`.

### Snapshot: 200 open markets, 8 macro series

| Band (YES mid) | Markets | Mean spread | Taker fee / price | Take cost / price |
| --- | --- | --- | --- | --- |
| `<10c` | 38 | 1.9¢ | 6.7% | **27.0%** |
| `10-20c` | 29 | 7.1¢ | 5.8% | 24.6% |
| `40-50c` | 11 | 11.9¢ | 3.5% | 14.2% |
| `80-90c` | 21 | 11.1¢ | 0.7% | 6.8% |
| `>=90c` | 37 | 3.0¢ | 0.2% | **1.8%** |

* `longshot_take_cost_exceeds_favorite`: **PASS** — 25.8% vs 4.3% of the price paid.
* `event_overround_positive`: **PASS** — 12 mutually exclusive events, mean `Σ ask − 1 = 0.11`;
  legs under 20¢ carry 21% of the summed asks.
* `flb_identifiable_from_snapshot`: **NOT_IDENTIFIABLE** (by construction).

### Paper tracks on that snapshot (ledgers carried from the same-day 15-market canary; the tracks also run inside the eleven-track `measure_all` scoreboard)

| Track | Longshot candidates | Admitted | Fills | Realized | Unrealized | Fees | Refusals |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `kalshi_longshot_fade` | 104 | 37 | 37 | −3.98 | −12.84 | 3.98 | `capital_cap_reached` 67 |
| `kalshi_maker_quote` | 104 | 40 | 25 | −0.51 | −2.97 | 0.51 | `capital_cap_reached` 64 |
| shadow longshot buyer | — | — | 37 | — | — | 35.98 | total −144.73 |

Per longshot band (paper PnL on notional, marked from the same snapshot): fade
`<10c` −118 bps / `10-20c` −280 bps; maker `<10c` −112 bps / `10-20c` −134 bps; shadow
longshot buyer `<10c` **−2,909 bps** / `10-20c` −2,157 bps. The fade pays the half-spread
plus taker fee; the maker shows one tick (conservative mark) plus a quarter of the taker
fee; the longshot buyer pays the same half-spread on many more contracts per dollar.
These are entry costs against the snapshot, not outcomes.

### Ex-post: 261 settled markets, 317,322 public trades, 86.4M contracts

Series: KXFEDDECISION, KXFED, KXCPIYOY, KXCPI, KXCPICORE, KXPAYROLLS, KXGDP, KXU3
(economics, 173 markets) and KXMLBGAME, KXNFLGAME, KXNCAAFGAME (sports, 88 markets);
newest 30 settled per series, up to 4,000 trades each (51 markets truncated).

| Band (taker price) | Markets | n_eff | Contracts | Taker gross / ct | Taker ROI | Maker net / ct | t (contract-w.) | t (equal-w.) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `<10c` | 213 | 7.2 | 34.2M | −0.014 | −68% | +0.014 | −0.55 | −1.06 |
| `10-20c` | 168 | 29.5 | 5.3M | −0.046 | −33% | +0.044 | −0.92 | −1.60 |
| `20-30c` | 141 | 4.9 | 7.5M | −0.203 | −80% | +0.200 | −1.95 | −2.74 |
| `30-40c` | 128 | 31.5 | 2.5M | −0.150 | −43% | +0.147 | −2.57 | −4.00 |
| `40-50c` | 95 | 18.4 | 2.9M | −0.163 | −37% | +0.159 | −2.68 | −4.85 |
| `50-60c` | 89 | 20.1 | 2.7M | +0.161 | +29% | −0.164 | +2.61 | +0.53 |
| `60-70c` | 111 | 32.9 | 3.2M | +0.143 | +22% | −0.146 | +2.42 | +2.08 |
| `70-80c` | 127 | 4.5 | 7.8M | +0.217 | +29% | −0.220 | +2.27 | +2.86 |
| `80-90c` | 153 | 42.1 | 4.2M | +0.071 | +8% | −0.073 | +1.73 | +0.83 |
| `>=90c` | 204 | 25.0 | 16.2M | +0.022 | +2% | −0.022 | +1.22 | −1.44 |

Verdicts — all **FAIL**:

* `ex_post_flb` (contract-weighted): longshot takers lost 1.8¢/contract (ROI −51%,
  −56% after fees) but with `effective_n_markets = 9.4` out of 234 the clustered t is −0.51.
  One Fed bucket (`KXFEDDECISION-26JUL-H26`, 11.6M contracts at ~1¢) dominates the weights.
* `ex_post_flb_equal_weighted_markets`: the typical market's longshot takers did **not**
  lose (+0.2¢/contract, t = 0.16) — markets where the longshot paid off offset the many
  small losses.
* `ex_post_favorite_longshot_slope`: every band below 50¢ is negative and every band
  above is positive (the FLB shape), and 20–50¢ / 60–80¢ are individually significant
  equal-weighted, but the pooled halves are not (below 50¢ t = −1.19; above 50¢ t = 0.50).
* `maker_fade_edge_after_fees`: +1.8¢/contract (maker ROI on collateral +1.8%) is not
  significant.
* By category: economics FAIL on every variant (longshot ROI −77%, t(eq) = +1.07).
  Sports **PASS** on the equal-weighted tail test and the slope with all trades (ROI −43%,
  t(eq) = −2.25) but **FAIL once the final 60 minutes before close are excluded**
  (t(eq) = −0.80): the sports signal in this sample is end-game, in-play flow, not
  pre-game mispricing. That is the opposite ordering to the Polymarket paper's
  "sports weaker" and is a property of the sample and of in-play trading, not a
  contradiction of it.

**Bottom line:** the snapshot shows why taking longshots is expensive (a quarter of the
price paid, vs 4% for favourites), but FLB itself is not detectable from books. Ex post,
this sample shows the FLB *shape* across bands without reaching the pre-registered
significance thresholds at the market-cluster level, and no positive maker edge after
fees. A longer settled history (more Fed/CPI cycles) is needed before either fade goes
anywhere near a live canary.

## Assumptions (all documented in `flb_report_latest.json → assumptions`)

* **Fee model.** Kalshi July-2026 schedule: taker `round_up(M·0.07·C·P·(1−P))`, maker
  `round_up(M·0.0175·C·P·(1−P))` on `quadratic_with_maker_fees` series, `M` = series
  `fee_multiplier` read from `/series/{ticker}` (1 on the macro series, 0.5 on
  `KXMLBGAME`). Paper fills round up to a centicent; the ex-post table uses the unrounded
  per-contract fee. Markets without fee metadata assume maker fees apply (conservative).
* **Fill probability (maker).** Expected-value fills `floor(qty·p)`: `p = 0.50` when
  improving the touch by one tick, `p = 0.25·own/(own+displayed)` when joining. These are
  operator assumptions, not measurements; a live resting order fills fully or not at all.
* **Queue.** Joining assumes pro-rata position behind the displayed size; improving assumes
  first in queue at the new price. Depth beyond the touch is ignored for resting orders.
* **Adverse selection.** 25% of the half-spread is deducted from the reported expected
  maker edge (`edge_bps`). The maker ledger uses conservative marks, so a resting fill shows
  no spread capture until it can be exited at the far side.
* **Marks.** Fade and shadow at mid; maker conservative. Positions without a two-sided book
  are valued at cost and counted as `unmarked_positions`.
* **Sizing.** Whole contracts. $25 notional per order, $75 cash-at-risk per market and
  $1,000 total collateral (strategy), plus 75 contracts per market and $75 daily loss
  (`RiskManager`). The maker reserves collateral for unfilled resting orders within a run.
* **Longshot definition.** A side priced strictly below 20¢ at the touch (YES ask, or
  `1 − YES bid` for NO). Bands `<10c, 10-20c, …, >=90c` are lower-inclusive; the ex-post
  table buckets by the price the taker paid for the side it bought.
* **Ex-post limits.** `/markets/trades` pages newest-first, so the per-market cap keeps the
  trades closest to settlement (`trades_truncated` is reported); end-game trades are
  handled by the `excluding_final_minutes` variant. Series fee parameters are as of harvest
  time. Closing trades and opening trades are indistinguishable on the public tape (as in
  the literature).

## Tests

`tests/test_flb.py` (bands, fee model, both strategies, caps, expected fills, track
runners, snapshot verdicts), `tests/test_flb_expost.py` (per-trade accounting, fair-price
FAIL, small-sample INSUFFICIENT, clustering, end-game exclusion, slope, harvest against a
mocked public API), `tests/test_measure_flb.py` (CLI persistence, ledger carry-over,
`--no-fees`, live-flag refusal). `uv run pytest -q`.
