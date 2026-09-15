# Fade the tourist — paper measurement track (Kalshi tennis; crypto 15-minute windows optional)

Paper-only. Nothing here places an order. The track reads three unauthenticated Kalshi
endpoints (`/markets`, `/markets/{ticker}/orderbook`, `/markets/trades`) plus
`/markets?status=settled` for the ex-post harvest and `/markets/{ticker}` to settle carried
paper positions. No keys, $0 data. Every entry point refuses to start if `TRADING_MODE=live`
or `ENABLE_LIVE_TRADING=true`.

## Hypothesis

> A slice of Kalshi taker flow looks *recreational* — small tickets, longshot buys, and
> buying a side after it has already run up. If that flow is uninformed, the side it piles
> into is over-bought, so paper-fading it (buying the other side) earns a positive expected
> value after fees. The effect should be stronger when the side we buy is already a
> favourite at or above 70c.

**Pass criterion (pre-registered):** `fade_ev_positive_after_fees = PASS` — the replayed
fade's settled net return per contract, after Kalshi taker fees, is positive with an
event-clustered t ≥ 2 over at least 10 events and 20 fades.

**Fail risk (pre-registered): adverse selection.** Flow that *looks* recreational can be
informed. On in-play tennis the fastest small-clip takers are frequently court-siders with a
faster score feed than the public book; on 15-minute crypto windows a small chase into the
moving side often reflects a spot move the book has not yet absorbed. In both cases the
classifier labels informed flow as tourist and the fade becomes its systematic counterparty,
losing the move plus spread and fee. The track measures this rather than assuming it away
(see *Adverse selection* below).

## Design

Everything is a rule with explicit parameters (`strategies/tourist_flow.py::TouristParameters`,
printed into the report). Nothing is fitted.

### 1. Classify every public print

The Kalshi tape gives, per trade, `taker_side`, `yes_price_dollars`, `count_fp`,
`created_time`. A print is **tourist** when at least `min_flags = 2` of three flags are set:

| Flag | Rule (defaults) |
| --- | --- |
| `small_ticket` | taker paid ≤ **$10** (contracts × price of the side bought) |
| `longshot_buy` | the side bought was priced **< 30c** |
| `late_chase` | the bought side is ≥ **5c** above its price **20 prints** earlier *and* the print sits in the last half of the market's open-to-close life |

### 2. Detect a cluster

Per market and side, a rolling **10-minute** window of tourist prints. When it holds
≥ **5** prints carrying ≥ **$50** on one side a `ClusterSignal` fires (then a 5-minute
cooldown per side).

### 3. Fade it

Buy the *other* side at the touch as a taker. **$10** per fade, **×2** when that side is
priced ≥ **70c** (the *strong* regime); whole contracts; `$75` cash-at-risk per market and
`$1,000` total collateral (equal to paper starting cash, so the ledger never borrows); plus
the `RiskManager` rails `$25`/order, 75 contracts/market, `$75` daily loss
(`TOURIST_RISK_LIMITS`). Kalshi taker fee `round_up(M·0.07·C·P·(1−P))`.

Two ways to run the same rules:

| Mode | Data | Fill | Exit |
| --- | --- | --- | --- |
| **Ex-post replay** (`research/tourist_fade.py::replay_market`) | settled markets' full public tape + `result` | the tape has no book, so the fade pays `1 − (last tourist print) + 2 ticks` (documented guess) | settled at the published result via `PaperLedger.settle` |
| **Live track** `fade_the_tourist` (`run_fade_the_tourist_track`) | open markets' current public book + newest ≤1,000 prints | at the current touch through the normal risk-gated `ExecutionEngine`; one position per **event** (both legs of a match see the same flow) | marked at mid, carried across runs, settled on a later run once `GET /markets/{ticker}` reports `yes`/`no` |

### 4. Measure adverse selection

For every print, the bought side is marked out **+5** and **+20** prints later and **at
settlement** (`1[side wins] − price paid`). Positive markouts mean the taker was right.
`tourist_flow_loses` PASSes only when tourist-flagged takers lose significantly at
settlement; a positive markout FAILs it with `adverse_selection_detected = true`.
Markouts are broken down by flag combination, series and category so an informed sub-flow
(for example in-play chasers) is visible even when the pooled tourist flow loses.

Inference: every print in a market shares one outcome, so **events are the cluster unit**
(both legs of a match share an `event_ticker`). Means are contract-weighted with an
event-clustered SE; equal-weighted means and `effective_n_events` are reported alongside.

## Run it

```bash
uv sync --extra dev

# Deterministic fixture run (no network): synthetic tapes exercise PASS / FAIL / adverse-selection paths
uv run python -m apps.measure_tourist_fade --artifact-dir artifacts

# Network, tennis (default universe): live track on open KXATPMATCH / KXWTAMATCH / challenger books
# + harvest the newest 60 settled markets per series with up to 10k prints each, replay them
uv run python -m apps.measure_tourist_fade --network --kalshi-env prod --harvest-trades \
    --settled-per-series 60 --max-trades-per-market 10000 --artifact-dir artifacts

# Later runs reuse data/harvests/kalshi_tourist_settled_trades.json (git-ignored) and settle carried positions
uv run python -m apps.measure_tourist_fade --network --kalshi-env prod --artifact-dir artifacts

# Optional crypto universe (KXBTC15M / KXETH15M) into its own artifact dir; 30-minute exclusion is
# meaningless on a 15-minute market, so shorten it
uv run python -m apps.measure_tourist_fade --network --kalshi-env prod --universe crypto --harvest-trades \
    --exclude-final-minutes 3 --artifact-dir artifacts/tourist-crypto --harvest-dir artifacts/tourist-crypto/harvests
```

Flags: `--universe tennis|crypto|both` or `--series ...`, `--limit`, `--tape-limit`,
`--settled-per-series`, `--max-trades-per-market`, every classifier / cluster / fade
parameter (`--small-ticket-notional`, `--longshot-threshold`, `--chase-move`,
`--chase-lookback-trades`, `--late-fraction`, `--min-flags`, `--cluster-window-seconds`,
`--cluster-min-trades`, `--cluster-min-notional`, `--cooldown-seconds`,
`--strong-favorite-price`, `--base-notional`, `--strong-multiplier`, `--assumed-spread-ticks`,
`--max-total-cash-at-risk`), the verdict thresholds (`--min-events`, `--min-fades`,
`--exclude-final-minutes`), `--no-persist`, `--reset-ledgers`, `--no-fees`.

### Outputs (`--artifact-dir`, default `artifacts/`)

| Path | Contents |
| --- | --- |
| `tourist_fade_report_latest.json` | Hypothesis, pass criterion, fail risk, headline, verdict table, live-track section, ex-post replay (tape shares, clusters, fades by regime / fade-price band / series / category, `*_excluding_final_minutes`, markouts, `adverse_selection`), assumptions, parameters; the first 100 fade rows |
| `tourist_fade_rows_latest.json` | Every replayed fade (market, event, time, sides, price, regime, qty, fee, cluster stats, result, settled PnL); stays in the git-ignored `artifacts/` |
| `scoreboard_tourist_fade.json` | Scoreboard artifact for the live track (schema 1.3.0, `meta.source=measured`, `meta.kind=fade_the_tourist`, `meta.track_family=tourist_fade`, `findings.fade_the_tourist`) |
| `paper/ledger_fade_the_tourist.json`, `paper/equity_curve_fade_the_tourist.jsonl` | Live-track ledger (carried across runs) and one equity point per run |
| `paper/runs/<run_id>.json` | Run record (`kind: fade_the_tourist`) picked up by the dashboard experiments index |

`cd dashboard && npm run sync-artifacts` publishes the scoreboard, the report and the ledger
into `public/artifacts/`; the track lands in the **Tourist fade** family.

## Reading the verdicts

| Check | PASS means |
| --- | --- |
| `fade_ev_positive_after_fees` | **the pass criterion**: all fades, net per contract > 0, event-clustered t ≥ 2, ≥ 10 events, ≥ 20 fades |
| `strong_regime_fade_ev_positive` | same, restricted to fades where the side bought was ≥ 70c |
| `strong_regime_beats_weak` | strong-regime net per contract > weak-regime net per contract (≥ 5 events each) |
| `fade_ev_positive_excluding_final_minutes`, `strong_regime_…_excluding_final_minutes` | same tests after dropping fades whose cluster fired within `--exclude-final-minutes` (30) of close: the newest-first tape over-represents end-game prints |
| `tourist_flow_loses` | tourist-flagged takers' settlement markout < 0 with t ≤ −2 (uninformed, as hypothesised). **FAIL with a positive markout = adverse selection detected** |
| `tourist_worse_than_other_takers` | tourist prints earn less at settlement than the rest of the tape (equal-weighted by event) |

## Measured on 2026-09-14 (prod public API, no credentials)

### Tennis — run `20260914T231958Z-tourist-591eb8cd`, committed to `dashboard/public/artifacts/`

Harvest: newest 60 settled markets each of `KXATPMATCH`, `KXWTAMATCH`, `KXATPCHALLENGERMATCH`,
`KXWTACHALLENGERMATCH` → **230 markets / 115 matches, 477,917 public prints** (1 market hit the
10,000-print cap). Tourist-flagged prints: **32.0% of prints, 0.76% of taker notional**
(134,827 `small+longshot`, 12,959 `small+chase`, 3,754 `small+longshot+chase`, 1,477
`longshot+chase`). 1,950 clusters → **1,229 replayed fades** ($19,504 notional, $273.54 fees;
700 clusters refused by the 75-contract market cap, 21 by the $75 cash cap).

| Fades | n | Events | Win | Net / contract | t (contract-w., event-clustered) | t (equal-w.) | Return on stake |
| --- | --- | --- | --- | --- | --- | --- | --- |
| all | 1,229 | 115 | 77% | **−$0.0008** | −0.09 | +3.05 | −0.1% |
| strong (bought side ≥ 70c) | 898 | 115 | 97% | **+$0.081** | **+6.13** | +7.87 | **+9.3%** |
| weak (< 70c) | 331 | 72 | 25% | **−$0.124** | −9.33 | −10.78 | −50.7% |
| all, clusters ≥ 30 min before close | 592 | 88 | 80% | +$0.024 | +1.32 | +2.09 | +3.5% |
| strong, ≥ 30 min before close | 439 | 85 | 95% | +$0.080 | +3.30 | — | — |

By the price the fade paid (the favourite's ask): `0.7–0.8` +$0.130/ct (185 fades, t = 3.6),
`0.8–0.9` +$0.114 (262, t = 6.5), `0.9–1.0` +$0.033 (451, t = 3.4); every band **below 0.4**
lost 7–23¢ per contract with a 0–21% win rate (202 fades). By series: `KXATPMATCH` +$0.029
(t = 1.9), `KXWTAMATCH` +$0.002, both challenger series slightly negative. Median cluster
fired 28 minutes before close (quartiles 13 / 28 / 53 min): this is **in-play** flow.

Markouts of the side the taker bought (contract-weighted, event-clustered t):

| Group | +5 prints | +20 prints | Settlement |
| --- | --- | --- | --- |
| tourist (153k prints, 8.4M contracts) | −0.0020 (t −7.2) | −0.0025 (t −3.8) | **−0.048 (t −6.2)** |
| other takers (325k prints, 129M contracts) | −0.0016 (t −7.8) | −0.0009 (t −2.3) | −0.005 (t −1.0) |
| `small+longshot` | | | −0.043 (t −8.1) |
| `small+longshot+chase` | | | −0.130 (t −5.3) |
| `longshot+chase` | | | −0.114 (t −2.9) |
| **`small+chase`** | | | **+0.009 (t +0.6)** |

Verdicts: `fade_ev_positive_after_fees` **FAIL**, `fade_ev_positive_excluding_final_minutes`
**FAIL** (positive, t = 1.32), `strong_regime_fade_ev_positive` **PASS**,
`strong_regime_fade_ev_positive_excluding_final_minutes` **PASS**, `strong_regime_beats_weak`
**PASS**, `tourist_flow_loses` **PASS**, `tourist_worse_than_other_takers` **PASS**;
`adverse_selection_detected = false` for the pooled flow.

**Reading.** The classifier does isolate flow that loses: tourist prints lose 4.8¢ per
contract at settlement versus 0.5¢ for everyone else, and small longshot buyers in the
in-play tail lose most. Fading that flow when the favourite is already ≥ 70c earned
+8.1¢ per contract after fees across 115 matches (t = 6.1) and still +8.0¢ (t = 3.3) when
end-game clusters are dropped. But the **pooled pass criterion fails**: the `late_chase`
flag catches in-play buyers of a side that has just moved — a break of serve, a set — and
those takers are *not* losing (`small+chase` settlement markout +0.9¢, the only positive
combination). Fading them means buying the side that just lost the point at 10–40c, and it
lost 15–23¢ per contract. That is the adverse-selection failure mode in miniature: it did
not show up in the pooled markout because small longshot buyers outnumber chasers 10:1,
but it wiped out the strong-regime profit at the portfolio level (+$1,524 strong vs
−$1,548 weak on the replayed notional).

Live track on the same day (60 open markets, 9,346 prints): 5 clusters, **3 paper fades**
(one per match; 3 second legs refused as `already_positioned_event`, 2 clusters stale, 7
finished markets with empty books), all strong regime, held open at mid (−$0.80 fees,
−$0.38 unrealized); they settle on the next run.

### Crypto (optional universe) — run `20260914T231728Z-tourist-30f52665`, not committed (own artifact dir)

Newest 60 settled `KXBTC15M` + 60 `KXETH15M` windows → 120 markets, **615,353 prints** (16
windows hit the 10k cap: these markets print 2–3M contracts in 15 minutes). Tourist share
42.9% of prints, 1.3% of notional (almost all `small+longshot`). 270 clusters → 270 fades.

| Fades | n | Events | Win | Net / contract | t | Return on stake |
| --- | --- | --- | --- | --- | --- | --- |
| all | 270 | 120 | 77% | −$0.011 | −0.78 | −1.7% |
| strong | 206 | 116 | 92% | +$0.029 | +1.21 | +3.3% |
| weak | 64 | 46 | 30% | −$0.081 | −2.05 | −33% |

Markouts: tourist −0.010 at settlement (t −2.7) — but **other takers +0.007 (t +2.2)**: on
15-minute windows the large, unflagged flow is the informed side. Verdicts: fade EV **FAIL**
(all regimes and strong), `strong_regime_beats_weak` PASS, `tourist_flow_loses` PASS,
`tourist_worse_than_other_takers` PASS. Nothing here is significant enough to act on, and
the 3-minute exclusion (`--exclude-final-minutes 3`) does not change the sign.

### Bottom line

*Fade EV > 0 after fees* is **not established** for the rule as specified. The strong-regime
sub-rule (fade clustered longshot buyers only when the favourite is ≥ 70c) is positive and
significant on 115 tennis matches, which is exactly the "stronger when favourite ≥ 70c"
prior — but it is also the part of the rule closest to a plain in-play favourite-longshot
bias trade, and its +8¢/contract rests on an assumed 2-tick spread against a tape that
carries no book. The `late_chase` flag is adverse-selected on in-play tennis and should be
dropped or inverted before anything else is tried. A live canary is not warranted on this
evidence; a longer replay (more tournaments, both tours) with a self-logged book
(`apps.book_logger`) to replace the spread assumption is the next honest step.

## Adverse selection — the documented fail risk

* **Court-siders.** In-play tennis prices move on every point. A taker who knows the point
  before the book does trades small and fast — indistinguishable from a tourist by ticket
  size. The `late_chase` markout above is the fingerprint: chasers in this sample are not
  losing, and fading them lost 50% of stake.
* **Slices.** A $10 print may be one slice of a large order worked across the book; the
  public tape cannot tell. `small_ticket` therefore over-counts "recreational".
* **Crypto windows.** 15-minute BTC/ETH markets settle on a spot print; the informed side is
  whoever sees spot first. Unflagged takers earned a positive settlement markout there.
* **Spread.** The replay pays `1 − p + 2 ticks`; real in-play spreads are often wider and a
  fade at the touch may not fill for the assumed size. The live track measures this
  honestly (it takes the displayed touch) but has only three fills so far.
* **Newest-first tape.** `/markets/trades` pages newest-first with a per-market cap, so
  replays over-represent end-game prints; the `*_excluding_final_minutes` variants exist
  for that reason and the strong regime survives them.
* **What would flip the verdict.** If the flagged flow's settlement markout turns positive
  on a fresh sample (`tourist_flow_loses = FAIL`, `adverse_selection_detected = true`), the
  hypothesis is falsified for that universe regardless of what the fade table shows.

## Assumptions (all in `tourist_fade_report_latest.json → assumptions`)

* **Classifier** — three rules, two needed; thresholds are guesses, not fitted.
* **Cluster** — ≥ 5 prints / ≥ $50 / 10 minutes / one side; 5-minute cooldown.
* **Fade** — taker at the touch; $10 (×2 ≥ 70c); `$75`/market, `$1,000` total; `RiskManager`
  `$25`/order, 75 contracts/market, `$75` daily loss. Replay resets the daily-loss rail per
  market because the sample spans months; per-order and per-market caps apply on every fade.
* **Replay fill** — `1 − (last tourist print) + assumed_spread_ticks (2)`. Not measured.
* **Fees** — taker `round_up(M·0.07·C·P·(1−P))`, `M` from `/series/{ticker}` (1 on every
  series here). No maker fees: the fade takes.
* **Settlement** — replay at the published `result`; live positions settled when
  `GET /markets/{ticker}` reports `yes`/`no`, otherwise carried and marked at mid.
* **Inference** — events are clusters; contract-weighted means with clustered SE plus an
  equal-weighted view; `effective_n_events` reported.
* **Tape limits** — newest-first paging with a cap; opening and closing trades
  indistinguishable; small prints may be slices.

## Tests

`tests/test_tourist_flow.py` (tape types, classifier incl. late/chase edge cases, cluster
window/cooldown, regime and sizing caps, replay price, live strategy fills and every refusal),
`tests/test_tourist_fade.py` (replay arithmetic against the ledger, markouts, per-market caps,
event-clustered stats, fixture PASS/FAIL paths, adverse-selection detection on informed
synthetic flow, INSUFFICIENT on small samples, live runner incl. one-per-event, settlement of
carried positions, public-endpoint mocks), `tests/test_measure_tourist_fade.py` (CLI artifacts,
ledger carry-over + fixture settlement, `--no-fees`, live-flag refusal, network settlement mock).
`uv run pytest -q`.
