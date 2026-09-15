# Make-on-whale, take-on-noise (Kalshi tennis) — runbook

Paper-only. Nothing here places an order. The track reads four unauthenticated
Kalshi endpoints (`/markets`, `/markets/{ticker}/orderbook`, `/markets/trades`,
`/series/{ticker}`, plus `/events/{ticker}` through the shared adapter) and the
self-logged archive written by `apps.book_logger`. Every entry point refuses to
start if `TRADING_MODE=live` or `ENABLE_LIVE_TRADING=true`.

## The experiment

> When a known +EV tennis whale lifts the offer, rest maker quotes one tick
> behind (inventory-capped). When only retail longshot flow hits, take the fade.
> Combine both legs. **Pass:** combined paper PnL > either leg alone; log fill
> rate + toxicity. **Fail risk:** queue priority / whale cancel.

Three tracks replay the *same* per-market event stream (polled L2 books plus
public trade prints) with the same caps, fees and marks, so the comparison is
apples to apples:

| Track | Legs | Purpose |
| --- | --- | --- |
| `kalshi_whale_noise_combined` (primary) | maker + taker in one ledger | the experiment |
| `kalshi_whale_maker_leg` | maker only | control |
| `kalshi_noise_taker_leg` | taker only | control |

`interaction_pnl = combined − (maker + taker)` isolates what sharing one
inventory book changes (a quote refused because the other leg already holds the
per-market cap, collateral reserved by resting quotes, etc.).

### What "known +EV whale" means on public data

Kalshi's public tape carries `taker_side`, `yes_price`, `count`, `created_time`
and `is_block_trade` — **no trader identity**. A whale is therefore a *size
class*, not a person (`strategies/whale_noise.py::WhaleRule`):

* not a block trade (negotiated off-book, never "lifts the offer"),
* `count ≥ 1,000` contracts **and** notional `≥ $500` **and**, once five earlier
  prints exist in the market, `count ≥ 10 ×` the median of the market's last 50
  prints.

The defaults were calibrated on the 2026-09-14 public tape of 150 open tennis
markets (28,174 prints): median print 21 contracts / $9, p90 259, p98 1,139,
p99 2,062 contracts; p99 notional $1,010. The rule selects ≈2% of prints.

Whether that size class is +EV is a separate, *measured* question:
`whale_flow_ev_positive` classifies every print in settled tennis markets with
the same rule and asks whether whale-class takers earned a positive net return
after fees (market-clustered `t ≥ 2`). Only that check can move the rule's
`ev_status` from `unverified` to `verified_expost`. An operator may also assert
it in a registry (`--whale-registry`, `--assume-whale-ev`), which the report
labels `operator_asserted`. `--require-ev-verified` makes the maker leg refuse
to quote unless the status is verified or asserted.

### Maker leg: one tick behind, conservative fills

After a whale buys outcome *X*, the leg rests a bid for *X* one tick **behind**
*X*'s best bid in the latest book *before* the print (pre-impact touch). Whale
buys YES at the ask → YES bid at `best_bid − 0.01`. Whale buys NO → NO bid one
tick behind the NO bid, i.e. a YES ask at `best_ask + 0.01`. The quote never
joins or improves the touch and never sits at the whale's price; if the whale's
own print already sits on our side of the quote the book is stale and the quote
is refused (`stale_book_crossed`).

Fills come from `ConservativeQueueModel` — explicit, and pessimistic on purpose:

| Rule | Effect |
| --- | --- |
| `queue_ahead` = every contract displayed at our level in the book at placement | we are last in line behind all visible size |
| a later book showing *more* size at our level raises `queue_ahead` (never lowers it) | arrivals between polls are assumed ahead of us; cancels ahead are never assumed |
| a print **at** our level consumes `queue_ahead` first, then fills us with the remainder (whole contracts, floored) | pro-rata fantasy is excluded |
| a print **through** our level fills at most **its printed size** | price priority says a taker printing at a worse price took our better level first, but only for what it printed; the level is never assumed to be ours |
| nothing fills inside `reaction_latency_seconds` (2 s) of the trigger — including the whale's own print — or after `quote_ttl_seconds` (300 s) | no look-ahead, bounded exposure |
| without a forward tape (single network snapshot) **no** maker fill is simulated | quotes are recorded as resting; the verdict is `NOT_SIMULATED` |

Maker fills book at the quote price with the maker fee
`round_up(M·0.0175·C·P·(1−P))` where the series charges makers
(`KXATPMATCH`, `KXWTAMATCH`; `quadratic` series charge makers nothing; unknown
metadata assumes maker fees). Unfilled resting quantity reserves collateral
against the $1,000 total cap while it rests.

### Taker leg: fade retail longshot flow

A non-whale print buying a side priced **below 20¢** (the price the taker paid,
on either side) is retail longshot flow. If no whale print hit the market in the
last `whale_lookback_seconds` (120 s) and the market's `taker_cooldown_seconds`
(60 s) has passed, the leg evaluates `strategies.flb.LongshotFadeStrategy`
against the latest book before the print: buy the favourite at the touch, taker
fee `round_up(M·0.07·C·P·(1−P))`, whole contracts, same caps. Refusals are
explicit (`whale_flow_in_lookback`, `taker_cooldown`, `fade_not_longshot`,
`fade_one_sided_book`, `fade_risk_position_cap_reached`, …).

### Caps (compatible with the 25/75/75 paper defaults)

`strategies.flb.FLB_RISK_LIMITS` — $25 notional per order, 75 contracts per
market, $75 daily loss — is the `RiskManager` for all three legs; the strategy
additionally enforces $75 cash at risk per market and $1,000 total collateral
including resting quotes. Whole contracts only. Every maker fill re-validates
against the risk gate at fill time (a fill the gate refuses is counted as
`fills_dropped_by_risk_at_fill_time`, never booked).

### Fill rate, toxicity, follow-through

* **Fill rate:** `contracts_filled / contracts_quoted` and `quotes with any fill /
  quotes placed`, plus `trade_through_fills`, `queue_raised_by_later_books`,
  `seconds_to_first_fill_mean`, and the taker leg's `fill_rate_orders`.
* **Toxicity:** markout = `direction × (book mid at t+h − fill price)` for
  `h ∈ {60, 300}` s using the first book at or after `t+h`; a fill is *toxic*
  when the markout is negative. Reported per leg and role: `toxicity_rate`,
  `mean_markout_cents`, `contract_weighted_markout_cents`.
  `maker_fills_not_adversely_selected` is FAIL when the contract-weighted 300 s
  markout of maker fills is negative.
* **Whale follow-through:** the same drift after every whale print, in the
  whale's direction; `reversal_rate` is the share of whale lifts the market
  faded within `h` — the public-data proxy for "whale cancel".

### Verdicts

| Check | Scope | PASS means |
| --- | --- | --- |
| `combined_beats_each_leg` | replay | `combined_pnl > max(maker_leg_pnl, taker_leg_pnl)` with ≥ `min_fills_for_verdict` (5) combined fills and ≥ 1 order on each leg; `INSUFFICIENT_DATA` below that; `NOT_SIMULATED` on a single snapshot |
| `maker_fills_not_adversely_selected` | replay | contract-weighted 300 s markout of maker fills ≥ 0 (≥ 5 fills with a markout) |
| `whale_flow_ev_positive` | ex post | whale-class takers: net return per contract > 0, market-clustered `t ≥ 2`, ≥ 10 markets, ≥ 1,000 contracts → `ev_status = verified_expost`; a significant loss → `refuted_expost` |
| `retail_longshot_fade_ev_positive` | ex post | the maker side of retail longshot prints earned a positive net return after maker fees, `t ≥ 2` |
| `*_excluding_final_minutes` | ex post | the same two checks after dropping prints within 60 minutes of close (end-game in-play flow) |

All PnL is `core.ledger.PaperLedger` with **conservative marks** (longs at bid,
shorts at ask) from the last book of each market.

## Run it

```bash
uv sync --extra dev

# Deterministic fixture replay (no network): six synthetic tennis markets, PASS path exercised
uv run python -m apps.measure_whale_noise --artifact-dir artifacts

# 1) Capture a forward tape of the tennis match series (the $0 data path; run for hours)
uv run python -m apps.book_logger --series KXATPMATCH KXWTAMATCH KXATPCHALLENGERMATCH \
    KXWTACHALLENGERMATCH KXITFMATCH KXITFWMATCH --limit 150 --interval 15 --max-rps 5 --gzip

# 2) Replay the archive through the three legs (the only mode that measures maker fills)
uv run python -m apps.measure_whale_noise --archive artifacts/books --artifact-dir artifacts

# 3) Ex post: harvest settled tennis markets + public trades, verify whether whale-class
#    flow is +EV; also takes one live snapshot (maker fills NOT simulated there)
uv run python -m apps.measure_whale_noise --network --kalshi-env prod --harvest-trades \
    --settled-per-series 30 --max-trades-per-market 4000 --artifact-dir artifacts

# Archive replay re-using the harvest for the ex-post section
uv run python -m apps.measure_whale_noise --archive artifacts/books --harvest-dir data/harvests
```

Useful flags: `--whale-min-contracts / --whale-size-multiple / --whale-min-notional`,
`--whale-registry FILE`, `--assume-whale-ev`, `--require-ev-verified`,
`--ticks-behind`, `--reaction-latency`, `--quote-ttl`,
`--assume-later-arrivals-behind` (less conservative), `--longshot-threshold`,
`--whale-lookback`, `--taker-cooldown`, `--markout-horizons 60 300`,
`--min-fills`, `--tickers` / `--since` / `--until` (archive window),
`--tape-window` (network), `--exclude-final-minutes`, `--no-persist`,
`--reset-ledgers`, `--no-fees`.

The three tracks are **not** part of `apps.measure_all`: they need a tape, and
the shared snapshot has none.

### Outputs (`--artifact-dir`, default `artifacts/`)

| Path | Contents |
| --- | --- |
| `whale_noise_report_latest.json` | Headline, verdict table, experiment statement, event-stream census, per-leg fill rate / toxicity / follow-through, every quote with its queue history and fills, fade evaluations, ex-post class table, assumptions |
| `scoreboard_whale_noise.json` | Scoreboard artifact (`meta.kind=kalshi_whale_noise`, `meta.data_mode=fixtures\|archive\|network`, `track_family=kalshi_flb`, `primary_track=kalshi_whale_noise_combined`) with `findings.kalshi_whale_noise` |
| `paper/ledger_<track>.json`, `paper/equity_curve_<track>.jsonl` | Ledgers, carried across runs |
| `paper/runs/<run_id>.json` | Run record (`kind: kalshi_whale_noise`) picked up by the dashboard experiments index |
| `data/harvests/kalshi_tennis_settled_trades.json` | Settled-trade harvest (git-ignored), re-used by later runs |

Publish: `cd dashboard && npm run sync-artifacts` copies `scoreboard_whale_noise.json`,
`whale_noise_report_latest.json` and the three ledgers; the tracks land in the
**Kalshi FLB** lane (`dashboard/scripts/track-families.mjs`).

## Measured on 2026-09-14 (prod public API, no credentials)

Three runs, every number from the named run's `whale_noise_report_latest.json`:

1. **Forward tape.** `apps.book_logger --series KXATPMATCH KXWTAMATCH
   KXATPCHALLENGERMATCH KXWTACHALLENGERMATCH KXITFMATCH KXITFWMATCH --limit 150
   --interval 15 --max-rps 5`, 22:49–23:29 UTC: 39 cycles (each ≈ 100 s at 300
   requests / cycle, so the 15 s interval overran by design), 5,850 books,
   45,685 prints, 11,707 requests, one `429` (retried), zero errors.
2. **Archive replay** `20260914T232916Z-whale-a6158906` — committed as
   `dashboard/public/artifacts/scoreboard_whale_noise.json` /
   `whale_noise_report_latest.json` and the three `paper_ledger_kalshi_*.json`.
3. **Ex post / network snapshot** `20260914T231606Z-whale-afd670a4`: 170 settled
   tennis markets (newest 30 per series, ≤ 4,000 trades each, 71 truncated),
   419,867 trades, 97.6M contracts; plus one live snapshot of 200 open markets
   (6,960 prints in the last 30 minutes, 110 whale-class lifts, 20 resting
   quotes, `NOT_SIMULATED` by construction). The harvest is re-used by run 2.

### Event stream (archive replay)

150 markets, 5,850 books (≈ 40 minutes of two-sided coverage per market), 45,685
prints (the first poll's tape page reaches back ≈ 2 days, so 549 prints precede
the first book and are refused as `no_book_before_print`). Classes: 790
`whale_lift` (1.7%), 7,137 `retail_longshot`, 37,758 `retail_other`, 0 block
trades. Whale lifts occurred in 31 of the 150 markets.

### Verdicts

| Check | Verdict | Detail |
| --- | --- | --- |
| `combined_beats_each_leg` | **FAIL** | combined −36.60 vs maker leg **+6.67** / taker leg −54.42 (104 combined fills); `interaction_pnl = +11.15` (sharing the book helped, but not enough) |
| `maker_fills_not_adversely_selected` | PASS | contract-weighted 300 s markout of maker fills **+5.2¢**; 30% of fills toxic |
| `whale_flow_ev_positive` | **FAIL** (`ev_status = unverified`) | whale-class takers +1.3¢/contract net, ROI +2.8%, clustered t = 1.27, equal-weighted t = −0.64 (157 markets, 45.7M contracts) |
| `whale_flow_ev_positive_excluding_final_minutes` | FAIL | +2.8¢/contract, t = 0.83 |
| `retail_longshot_fade_ev_positive` | PASS | maker side of retail longshot prints +4.7¢/contract net (taker ROI −87%, win rate 0.7%), t = −4.16 |
| `retail_longshot_fade_ev_positive_excluding_final_minutes` | PASS | +6.8¢/contract, t = −2.62 (equal-weighted t = 0.41: the typical market does not show it) |

### Legs on the same tape

| Leg | Triggers | Orders | Fills | Realized | Unrealized | Fees | Max DD | Open |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `kalshi_whale_noise_combined` | 7,927 | 91 | 104 | −7.07 | −29.53 | 8.62 | 36.60 | 19 |
| `kalshi_whale_maker_leg` | 790 | 73 | 86 | −1.44 | +8.11 | 4.69 | 0.00 | 10 |
| `kalshi_noise_taker_leg` | 7,137 | 28 | 28 | −4.55 | −49.87 | 4.55 | 54.42 | 15 |

Refusals (combined): `whale_flow_in_lookback` 5,923, `no_book_before_print` 549,
`quote_already_resting` 430, `taker_cooldown` 334, `fade_risk_position_cap_reached`
316, `fade_one_sided_book` 103, `fade_not_longshot` 99, `risk_position_cap_reached`
49, `stale_book_crossed` 33.

**Maker leg — fill rate and queue.** 73 quotes, 3,319 contracts quoted, 1,654
filled (**49.8%** of contracts; 55% of quotes touched: 36 filled, 4 partial, 33
expired unfilled), mean 53 s to first fill. Mean displayed size at the quoted level
was **26,244 contracts**, so the queue ahead of us was never consumed: **all 86
fill events were trade-throughs**, capped at the printed size. Later books
lengthened the queue 41 times. In tennis in-play books the "one tick behind" quote
is only ever filled when the market sweeps through it — the queue-priority fail
risk is not a modelling nuisance here, it is the mechanism.

**Maker leg — toxicity.** 60 s markout +1.9¢ contract-weighted (32.5% toxic, 80
fills scored); 300 s markout +4.8¢ (31.9% toxic, 72 fills). Fills are favourable on
average because a sweep through our level in in-play tennis tends to revert.

**Whale follow-through (790 lifts).** 60 s: mean drift +0.8¢ in the whale's
direction, 36% reverted (491 scored). 300 s: mean drift **−0.8¢**, **44% reverted**
(372 scored). Whale-class lifts in this tape did not carry through over five
minutes — consistent with the ex-post finding that the size class is not measurably
+EV, and the public-data face of the "whale cancel" risk.

**Taker leg.** 28 fades (all filled at the touch, 708 contracts, fill rate 1.0);
70% of fills toxic at 60 s and 73% at 300 s (−1.6¢ contract-weighted). The −49.87
unrealised sits on 15 open favourites marked conservatively (shorts at the ask) in
wide in-play books after at most 40 minutes — the ex-post table says fading retail
longshot flow wins **at settlement**, the replay says it looks terrible on a
40-minute mark. Both are true and neither is the other.

### Bottom line

On this sample the maker leg alone was the best book (+6.67), the taker fade lost
on marks, and combining them did not beat the maker leg: **FAIL** against the
pre-registered criterion. Fill rate (≈ 50% of quoted contracts, all by sweep) and
toxicity (≈ 30% of maker fills, ≈ 73% of taker fills) are logged. The whale size
class was not verified as +EV ex post (t = 1.27 over 157 settled markets), so the
premise "known +EV whale" is not established on public data. A longer tape (days,
not 40 minutes, with positions carried to settlement) is needed before either
reading is more than a first measurement.

## Assumptions and limits (all written into `whale_noise_report_latest.json → assumptions`)

* **Anonymous tape.** "Whale" is a size class. The ex-post check measures the
  class, not a trader; a real +EV account that trades small is invisible, and a
  large uninformed account is counted.
* **Polled L2, not L3.** Books are point-in-time snapshots every 15–60 s (the
  archive's cycle time at 150 markets); the book used for a quote is the latest
  one *before* the print, so it can be that stale. The fill model compensates by
  assuming the worst about queue position; `stale_book_crossed` catches the case
  where the whale's own print proves the book is old. Kalshi books carry no venue
  timestamp; prints carry `created_time`. Clock skew between the two is bounded by
  `latency_ms` in the archive.
* **Fills are simulated, never observed.** A live resting order fills fully or
  not at all and can be cancelled or re-priced; the model produces partial fills
  from consecutive prints instead. It never grants priority from cancels ahead of
  us, never fills without a print, and caps trade-through fills at the printed
  size — all of which understate fills relative to a FIFO fantasy and are the
  intended bias.
* **Marks.** Conservative marks from the last polled book of a short capture
  window; in-play tennis spreads are wide, so freshly filled positions carry a
  spread penalty and unrealised PnL over minutes is mostly noise. Fixture markets
  also report a hypothetical settlement PnL (`settlement_preview`); archive and
  network fills are not settled.
* **Ex-post limits.** Same as the FLB track: `/markets/trades` pages newest-first
  (per-market cap keeps end-game prints; `markets_with_truncated_trades` is
  reported), opening and closing trades are indistinguishable, series fee
  parameters are as of harvest time, markets are the inference unit.
* **Fees.** Kalshi July-2026 schedule; `M` from `/series/{ticker}` (1 on every
  tennis series listed); archive replays have no fee metadata and assume maker
  fees apply (conservative).
* **Synthetic fixtures are labelled.** `research/fixtures/whale_noise_tape.json`
  and `venues/kalshi/fixtures/whale_noise_settled_trades.json` carry `_comment`
  / `source: fixture`; the fixture ex-post block carries `note: Synthetic
  fixture…`. Only archive / network runs are evidence.

## Tests

`tests/test_whale_noise.py`: print classification (size class, block exclusion,
both-side longshot detection, registry), quote pricing one tick behind for YES
and NO whales, every refusal reason (bounds, caps, risk gate, EV requirement,
stale book), the queue model (latency, wrong-side prints, queue consumption,
partial fills, trade-through cap, later-book lengthening, TTL expiry, NO-bid
symmetry), the deterministic fixture replay (counts, fill rate, toxicity,
follow-through, verdict, interaction PnL, caps and ledger invariants, fee
regimes), archive replay from a book-logger-shaped JSONL directory (duplicates,
filters, windows), network mode against a mocked public API (only public
endpoints, no fills simulated, `NOT_SIMULATED`), the ex-post check (PASS on the
fixture, not PASS on fair whales, INSUFFICIENT on thin data, final-minutes
exclusion), and the CLI (artifacts, ledger carry-over, `--no-fees`, archive
mode, live-flag refusal, parameter parsing). `uv run pytest -q`.
