# Tennis whale copy with lag — paper measurement track

Paper-only. Nothing here places an order. The track reads three unauthenticated
endpoints — Polymarket Gamma `/markets?tag_id=864` (Tennis), Polymarket Data API
`/trades?market=<conditionId>&takerOnly=true`, and Kalshi's public `/series` list —
and replays the tape into three `PaperLedger`s. `apps.measure_tennis_whale` refuses to
start if `TRADING_MODE=live` or `ENABLE_LIVE_TRADING=true`. $0 data.

## Question

Do wallets that repeatedly take size on Polymarket tennis carry information, and can a
follower capture it by copying their **next** fill 30 s, 2 min or 10 min later?

Kalshi lists tennis series (`KXATPMATCH`, `KXWTAGAME`, … — 3,778 sports series were
scanned, 113 tennis-like), but its public trade tape (`/markets/trades`) has no account
identity, so a Kalshi leg of this experiment is **NOT_IDENTIFIABLE** from public data.
The report records both facts and nothing more.

## Pre-registration (fixed before the first network run)

Everything below is written into `tennis_whale_report_latest.json → pre_registration`
on every run so the artifact carries its own rules.

### Whale definition (walk-forward)

A wallet is a whale from the first moment its history *strictly before the print being
evaluated* contains

* `min_large_fills = 3` taker prints of at least `large_fill_notional = 500` USDC,
* across `min_markets = 2` distinct tennis markets.

Only prints **after** qualification are signals, so whale selection never sees the fill
it is scored on (no look-ahead in the wallet ranking).

**Farmer / maker filter.** `two_sided_share` = share of the wallet's markets in which it
ended up long *both* outcomes. Above `max_two_sided_share = 0.5` the wallet's prints are
refused `two_sided_flow`. Volume farmers, hedgers and market makers churn both ways; their
prints carry no direction. (Walk-forward too: a wallet can be copied early and refused
later as its two-sided share grows.)

### Signal and copy

* Signal: a whale's taker print of at least `signal_min_notional = 200` USDC.
* Copy at lag *L* ∈ {30 s, 120 s, 600 s}: buy the outcome the whale ended up long, at the
  **first executed print on the same market at or after `signal + L`** (a real price, not
  a mid), plus `slippage_ticks = 1` tick against us, paying the venue taker fee
  (`C·rate·p·(1−p)`, sports 5 %).
* Size: `stake_per_copy = 10` USDC of contracts, whole contracts, **capped by the reference
  print's size** — the tape shows no depth, so the print is the only evidence liquidity
  existed. Risk rails through the normal `RiskManager`: $25/order, 100 contracts/market,
  $75 daily loss.
* Refusals (all counted per lag): `no_print_within_window` (no print within
  `max_wait_seconds = 1800` after `signal + L`), `market_closed_before_copy`,
  `price_out_of_bounds` (copy price outside [0.02, 0.98]), `size_below_one_contract`,
  `cooldown` (one copy per wallet × market × direction per `cooldown_seconds = 600`),
  `risk_*` (RiskManager).
* Settlement: markets with `umaResolutionStatus = resolved` and `outcomePrices` 1/0
  settle in the ledger at 1/0. Open positions are marked at the last print's YES-equivalent
  price and reported as unrealized.

### Statistics

Per lag, two statistics over copies **with an outcome**, each as return on stake per copy,
equal-weighted across copies:

* **Settlement ROI** after fees: `(signed·(settle − entry)·qty − fee) / stake`.
* **CLV ROI** after fees: same with the closing line — the YES-equivalent price of the last
  print strictly before Gamma `closedTime` — in place of settlement.

Inference is a **market-clustered bootstrap** (resample markets with replacement, 2,000
draws, fixed seed 20260914): every copy in a market shares one outcome and one closing
line, so copies are not independent. Confidence intervals are Bonferroni-corrected
across the three lags (`alpha_per_lag = 0.05 / 3`). A lag needs `min_copies = 30` copies
across `min_markets = 10` markets with outcomes for a verdict; otherwise `INSUFFICIENT_DATA`.

The **whale-own benchmark** scores the whales' own signal fills the same way at lag 0
(their price, taker fee). If that is not positive, no lag can be.

### Pass rule

**PASS** when, at one or more lags, the CI lower bound of settlement ROI *or* CLV ROI is
above zero. Otherwise **FAIL** (or `INSUFFICIENT_DATA` when no lag reaches the minimums).

### Kill rule

Kill the track when either

* (a) every lag has sufficient data and both settlement ROI and CLV ROI have CI **upper**
  bound below zero, or
* (b) the whale-own benchmark has sufficient data and a settlement-ROI CI upper bound below
  zero — the signal itself loses after fees.

Either trigger ends the track. There is no re-tuning of thresholds after seeing results;
a different rule is a different pre-registration and a new track id.

## Measured result — 2026-09-14 (public tape, run `20260914T230348Z-tennis-63ad64de`)

Committed under `dashboard/public/artifacts/` (`tennis_whale_report_latest.json`,
`scoreboard_tennis_whale.json`, `paper_ledger_tennis_whale_copy_{30s,2m,10m}.json`,
`runs/20260914T230348Z-tennis-63ad64de.json`).

Universe: 127 tennis markets (87 resolved, 40 open; 90 moneyline, 21 completed-match,
16 sets/handicaps/totals), 27,450 taker prints, 2,211 wallets, prints from 2026-08-08 to
2026-09-14, 155 public requests, no market truncated.

Whales: **105 qualified**, 44 of them later refused as two-sided; 548 signals. Signal
reasons over all prints: `not_whale` 20,561, `two_sided_flow` 5,341,
`below_signal_notional` 1,000, `signal` 548.

| Lag | Copies | Markets | Copies settled | Settlement ROI (mean) | CI (98.3 %) | CLV ROI (mean) | CI | Ledger PnL | Fees | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 30 s | 333 | 43 | 266 | −10.2 % | [−25.9 %, +8.9 %] | −10.1 % | [−26.6 %, +7.6 %] | −33.33 | 52.31 | **FAIL** |
| 2 min | 327 | 43 | 262 | −11.1 % | [−26.0 %, +5.3 %] | −11.0 % | [−25.8 %, +5.7 %] | −142.02 | 53.03 | **FAIL** |
| 10 min | 287 | 42 | 228 | −7.2 % | [−25.2 %, +11.3 %] | −7.1 % | [−25.3 %, +11.0 %] | −162.72 | 47.25 | **FAIL** |

Whale-own benchmark (lag 0, n = 460 fills in 47 markets): settlement ROI **−15.3 %**,
CI [−33.7 %, +3.5 %]; share of positive fills 46 %. The whales themselves lose after fees
in this sample; the copy portfolio loses slightly less because the cooldown and size cap
skip part of their flow.

Overall: **FAIL** (no lag with a CI lower bound above zero). Kill rule: **continue** — the
copy upper bounds still cross zero at every lag and the whale-own upper bound is +3.5 %,
so neither trigger fired. Effective cluster count is ~24 markets per lag (a few Guadalajara
/ Guangzhou matches carry many copies), which is why the intervals are wide.

By market type: every settled copy but one is a `moneyline`; the sets/handicap/totals
markets had almost no whale-sized prints.

Refusals: `cooldown` 133–147, `price_out_of_bounds` 39–84 (in-play markets converging
toward 0/1 by the time the lag elapses; largest at 10 min), `no_print_within_window`
15–29, `risk_projected` 11–16 (the 100-contract per-market cap when several whales hit
the same match), `size_below_one_contract` 1–4.

Reading: on this window, size on Polymarket tennis is not informed flow — it looks like
retail-scale conviction or hedging by accounts that also lose. There is no evidence for the
hypothesis, and not yet enough to kill it under the pre-registered rule.

## Run it

```bash
uv sync --extra dev

# Deterministic synthetic fixture (no network): exercises every path; labelled fixture_synthetic
uv run python -m apps.measure_tennis_whale --artifact-dir artifacts

# Public tape: harvest resolved + open tennis markets and their taker prints (~1–2 min,
# ~150 requests), then replay. The tape is written to data/harvests/polymarket_tennis_tape.json
# (git-ignored) and reused by later runs without --harvest.
uv run python -m apps.measure_tennis_whale --network --harvest --artifact-dir artifacts

# Replay an explicit tape, disable fees, change lags (each lag is a track)
uv run python -m apps.measure_tennis_whale --network --tape data/harvests/polymarket_tennis_tape.json --lags 30 120 600 1800

# Publish to the dashboard (copies report, scoreboard, ledgers, run record; rebuilds the index)
cd dashboard && npm run sync-artifacts
```

Useful flags: `--resolved-markets`, `--open-markets`, `--min-volume` (harvest universe);
`--large-fill-notional`, `--min-large-fills`, `--min-whale-markets`, `--signal-min-notional`,
`--max-two-sided-share`, `--stake`, `--max-wait-seconds`, `--slippage-ticks`,
`--cooldown-seconds` (copy rule — changing them is a new pre-registration);
`--min-copies`, `--min-markets`, `--resamples`, `--seed`, `--alpha` (inference);
`--no-fees`, `--no-persist`, `--no-kalshi-check`.

Ledgers are always rebuilt from the tape (the tape is the state); they are never carried
across runs. Re-running on the same tape reproduces the same fills and the same bootstrap.

## Artifacts

| File | Contents |
| --- | --- |
| `tennis_whale_report_latest.json` | `kind: tennis_whale_copy_report`, `status`, `headline`, `overall_verdict`, `verdict_table[]`, `pre_registration`, `universe`, `whales` (top 25 with `two_sided_share`, `signals`, `own_settlement_roi_mean`), `whale_own_benchmark`, `lags` (per lag: bootstrap blocks, `by_market_type`, verdict), `kill_rule` (with `components`), `kalshi`, `tracks`, `copies_by_market`, `fail_risks`, `assumptions` |
| `tennis_whale_copies_latest.json` | every copy row (wallet, market, lag, signal/copy prices, qty, fee, stake, settlement/CLV PnL and ROI) plus the whale-own rows; not synced to the dashboard (≈1 MB) |
| `scoreboard_tennis_whale.json` | scoreboard artifact for the three lag tracks (`meta.track_family = tennis_copy`, `meta.kind = tennis_whale_copy`, primary `tennis_whale_copy_30s`) |
| `paper/ledger_tennis_whale_copy_<lag>.json`, `paper/equity_curve_…jsonl` | one `PaperLedger` per lag |
| `paper/runs/<run_id>.json` | run record; the dashboard index stamps the `tennis_copy` family |
| `data/harvests/polymarket_tennis_tape.json` | the harvested tape (git-ignored, ≈7 MB) |

Statuses: `measured_from_public_tape`, `fixture_synthetic`, `no_tennis_markets`,
`no_tennis_prints`, `no_tennis_whales_found`, `whales_found_no_signals`. The synthetic fixture
is never published by `sync-artifacts`, and CI rejects a committed report whose `mode` is not
`network`.

## Fail risks (documented, not solved)

* **Exit liquidity.** A copy fills at a *print* plus one tick. The tape shows no depth, so
  fills at size may be worse, and exits before settlement are not modelled at all; every
  copy is held to resolution (or marked at the last print).
* **Farmers and makers.** The two-sided filter removes wallets that visibly take both
  sides *within tennis*. A wallet can farm volume on other sports, or hedge on another venue,
  and still look directional here. 44 of 105 whales were eventually refused; the remainder
  are not certified informed.
* **Selection on notional, not skill.** Whales are chosen by size. The whale-own benchmark
  (−15 %) is the honest read of what that selects.
* **Wallet fragmentation.** One actor, several proxy wallets → repeat-size detection
  undercounts. Only market clustering is modelled as dependence.
* **Universe bias.** Resolved markets are listed newest-closed first; short in-play
  moneylines dominate, long-dated futures are underrepresented. Gamma refuses offsets
  beyond ~2,000, so the resolved universe is the most recent ~2,000 closed tennis markets
  above the volume floor.
* **Endgame flow.** Whale prints late in a match at 0.9+ are common; the price bounds refuse
  the copy but the whale's own print still counts in the benchmark.
* **Fees.** Venue taker fee by Gamma `feeType`; rebates and maker programmes ignored.
* **Three lags are three tests.** Bonferroni on the CIs; the PASS-at-any-lag rule is still a
  disjunction and is stated as such.
* **Not validated at all:** whether Data API `takerOnly=true` is complete (it is the venue's
  own tape; no independent check), and whether `proxyWallet` maps 1:1 to a person.

## Code map

| Piece | Where |
| --- | --- |
| Whale qualification, farmer filter, copy rule, risk limits | `strategies/tennis_whale_copy.py` |
| Tape model, harvest (Gamma + Data API + Kalshi listing), replay client, per-lag runtimes, cluster bootstrap, verdicts, report | `research/tennis_whale.py` |
| CLI and artifact writer | `apps/measure_tennis_whale.py` |
| Synthetic fixture | `research/fixtures/tennis_whale_tape.json` (`_comment` says synthetic) |
| Tests | `tests/test_tennis_whale.py`, `tests/test_measure_tennis_whale.py` |
| Dashboard family `tennis_copy` | `dashboard/scripts/track-families.mjs`, `sync-artifacts.mjs` |
