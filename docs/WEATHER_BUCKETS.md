# `weather_bucket_edge` — Polymarket temperature buckets vs. a free ensemble

Paper-only measurement track. It asks one pre-registered question: **does a free
multi-model weather ensemble, evaluated at the exact settlement station and
rounded to the settlement precision, price Polymarket's daily highest / lowest
temperature buckets better than the CLOB does — by enough to clear the taker
fee?** The track paper-buys the bucket side whose model edge clears the fee
plus 3¢, holds it to the venue's resolution and scores the realised net PnL per
contract across settled city-days.

$0 and paper-only by construction: Open-Meteo (free, keyless) for the
ensemble, NOAA/NWS `aviationweather.gov` (free, keyless) for METARs and station
coordinates, public Gamma/CLOB reads for the markets. No paid or keyed vendor is
implemented; `paid_sources` in every report records whether such a key was even
present in the environment (it changes nothing). No order ever leaves the
process. Status legend follows `docs/ASSUMPTIONS.md` (**PASS** = code + test
or reproducible run here; **UNKNOWN** = cannot be validated from this
repository, evidence needed stated).

## Why a separate CLI family (not a `WEATHER_TRACKS` entry in `measure_all`)

The measurement is **stateful across days**: a city-day is entered while the
day is open, observed on later runs, settled by the venue a day later and only
then scored. That needs a persistent register and settlement lookups against
events that have already left the active listing — the same shape as
`tennis_basis`, not the single-snapshot FLB tracks. It also needs its own
Polymarket universe (Gamma tag `103040`, YES **and** NO book per bucket) and
two external feeds per run, which would make the thirteen-track full board
depend on Open-Meteo. So the track ships as its own family (`weather`) with its
own CLI, scoreboard and report, and is pinned as a **lane** on the dashboard.
`research/weather_buckets.py::WEATHER_TRACKS` is the extension point for the
sister tracks (late-day METAR dead-bucket, per-city calibration); they share the
register's `(station, city, date, kind, bucket bounds)` interface.

## Pre-registration

| Item | Value | Where |
| --- | --- | --- |
| Universe | Polymarket NegRisk events "Highest / Lowest temperature in *city* on *date*" (Gamma tag `103040`), one event = one **city-day**, one bucket = one binary market | `research/weather_buckets.py::capture_weather_snapshot` |
| Settlement station | parsed from the rules text: `weather.gov/wrh/timeseries?site=<icao>` (NOAA) or a Weather Underground `/history/daily/<cc>/<city>/<ICAO>` page; **fail-closed** on no station, two different stations (also across legs), a non-ICAO id, or a missing unit / high-low / date | `strategies/weather_buckets.py::parse_settlement_rules`, `parse_city_day` |
| Buckets | every `groupItemTitle` must parse (`"65°F or below"`, `"66-67°F"`, `"27°C"`, `"82°F or higher"`) and the set must tile the line exactly once with open ends; the group must be NegRisk-exclusive and not augmented | `parse_bucket_label`, `buckets_partition_reason` |
| Model | `p(b) = (count_b + α) / (N + α·K)`, `α = 0.5`, over every ensemble member's daily max (high) / min (low) at the station coordinates, in the market's unit, **rounded half-up to whole degrees** (the resolution precision); optional `dispersion_multiplier` (1.0) and `bias_degrees` (0) exist for the calibration track and are echoed in every artifact | `ensemble_bucket_probabilities` |
| Feed gates | ≥ 30 members from ≥ 2 models, forecast fetched ≤ 12 h before pricing, city-day is today or tomorrow at the **station's local date** (`max_lead_days = 1`) | `run_weather_cycle` |
| Edge | buy YES: `p − yes_ask − fee(yes_ask)`; buy NO: `(1 − p) − no_ask − fee(no_ask)` with the NO ask from the venue's NO ladder (else `1 − yes_bid`); `fee = 0.05 · price · (1 − price)` (Polymarket `weather_fees`) | `WeatherBucketEdgeStrategy.evaluate` |
| Enter | better side's net edge ≥ **0.03**, price in `[0.02, 0.95]`, two-sided YES book with spread ≤ 0.10, touch ≥ 5 contracts (venue minimum), whole contracts | `evaluate` reasons `below_edge_threshold`, `price_below_floor`, `price_above_cap`, `one_sided_book`, `wide_spread`, `insufficient_touch_depth` |
| Size | `min(touch, $10 / price, $20 per bucket, $40 per city-day, $1,000 total cost basis, RiskManager capacity)`; `RiskLimits(10, 50 contracts, $250 daily)`; one entry pass per city-day (later runs only observe) | `WEATHER_RISK_LIMITS`, `WeatherEdgeParameters` |
| Settle | after the station's local day ends (+1 h): Gamma `GET /events?slug=` — every bucket `closed` with exactly one `outcomePrices == [1, 0]` → positions close at 1 / 0 on the ledger (`order_id = settlement`); non-binary or two winners → `excluded`; unresolved after 5 days → `excluded` | `classify_venue_resolution`, `PaperLedger.settle` |
| METAR cross-check | the public METAR history for the station, T-group tenths → market unit, round half-up, max / min over reports whose **local** date is the observation date; a complete day (≥ 18 reports, day over) gives a *provisional* outcome and PnL that is reported and compared with the venue but **never booked** | `research/weather_sources.py::observed_extreme` |
| Unit of inference | one venue-settled city-day with ≥ 1 contract (its bucket fills share one outcome) | `verdict` |
| **Pass** | `n ≥ 30` settled city-days and the city-day mean of net PnL per contract ≥ **0.02** with its normal-approximation 95 % lower bound > 0; `strong` when the mean ≥ 0.03; `n ≥ 30` otherwise → **FAIL**; `n < 30` → `insufficient_sample` | `verdict` |
| Secondary | the same rule including METAR-provisional city-days (`verdict_provisional_including_metar`); METAR-vs-venue agreement rate; multi-class Brier of the model vs the market mid on settled city-days | report |

Parameters live in `WeatherEdgeParameters` and are echoed into every artifact
(`metrics.parameters`, `verdict.pre_registered`). The CLI exposes them under
"pre-registered parameters (override only for sensitivity checks)".

## Data sources (all free, all keyless)

| Feed | Endpoint | What is read | Terms / limits |
| --- | --- | --- | --- |
| Polymarket Gamma | `GET /events?tag_id=103040&active=true&closed=false` (paged), `GET /events?slug=` for resolution | event slug/title, `negRisk`, `negRiskAugmented`, per-market `description`, `groupItemTitle`, `clobTokenIds`, `feeType=weather_fees`, `outcomePrices` | public, no key |
| Polymarket CLOB | `POST /books` | YES **and** NO ladders per bucket | public, no key |
| Open-Meteo ensemble | `GET https://ensemble-api.open-meteo.com/v1/ensemble?latitude&longitude&daily=temperature_2m_max|min&models=gfs_seamless,ecmwf_ifs025,icon_seamless&temperature_unit&timezone=auto&start_date&end_date` | one value per member per model for the target date: GEFS 31 + ECMWF IFS 51 + ICON-EPS 40 = **122 members**; the station's IANA timezone | free for non-commercial use, no key, 10,000 calls/day; CC BY 4.0 |
| aviationweather.gov | `GET /api/data/metar?ids=<icao>&format=json&hours=72`, `GET /api/data/stationinfo?ids=` | METAR `rawOb` (T-group tenths of °C), `obsTime`; station `lat`/`lon` | public NOAA/NWS API, no key |

`data/weather/stations.json` holds the coordinates of the 49 settlement
stations seen on 2026-09-15 (from `/stationinfo`); an unseen station is looked
up live and refused (`station_unknown`) when the lookup fails. The feed caps
itself at `--max-requests` (400) per run; one full run on 2026-09-15 used 119
requests (1 Gamma events page, 1 CLOB books batch, 118 Open-Meteo calls, no
METAR because no record was due).

**Why these feeds map to the settlement source.** The Polymarket rules resolve
on NOAA's `weather.gov/wrh/timeseries` page, whose hourly "Temp" column is the
station's METAR temperature rendered in whole degrees. The same METARs are
public on `aviationweather.gov` with the tenths-of-a-degree remark group, so the
day's hourly maximum / minimum can be reconstructed — that is the provisional
check. Open-Meteo's daily max / min is also the max / min of *hourly* model
values, so model and settlement are on the same footing.

## Live universe (public reads, 2026-09-15)

100 active daily-temperature events (highest + lowest for ~50 cities), 11
buckets each, `negRisk = true`, `feeType = weather_fees`, tick 0.001, minimum
order 5. Stations are ICAO airports in 94 of them (`site=klga`, `kbkf`, `eglc`,
`LLBG`, `nzwn`, …); Hong Kong resolves on the Hong Kong Observatory climate
table (no station id → `no_station`), Taipei on a Weather Underground page with
an ICAO id (`RCSS`, admitted with `source = wunderground`). Units are °F for US
cities and °C elsewhere. The Denver station is Buckley SFB (`KBKF`), not
`KDEN` — exactly the mismatch the station parser exists to catch.

Measured 2026-09-15 01:32 UTC (`apps.measure_weather_buckets --network --limit 120`,
run `20260915T013332Z-5149e6bf`, committed under `dashboard/public/artifacts/`):
120 events read, **118 stations parsed** (2 `no_station`, both Hong Kong), 86
city-days priced (32 refused `too_far_ahead` at the station's local date), **51
city-days entered**, 172 paper fills, $1,000 cost basis reached on the last
entry, 119 free-feed requests, 0 feed errors, 0 settled (a first run cannot
settle anything). Bucket-level refusals: 422 `one_sided_book`, 140
`wide_spread`, 110 `no_edge`, 88 `below_edge_threshold`, 7 `price_below_floor`,
4 `price_above_cap`. Ledger: fees $23.99, unrealised −$47.90 at mid marks after
crossing the spread. The verdict is therefore `insufficient_sample`; the edge is
**UNKNOWN** until the register accumulates settled city-days (see "Run it").

## Lifecycle of a record

```text
run k   : event parsed (station, unit, kind, date, buckets) -> local lead 0 or 1 day -> ensemble fetched
          -> bucket probabilities -> per-bucket edge -> paper fills booked -> record opened (also when no bucket
          cleared the threshold: "priced_no_edge" records are kept for calibration / Brier)
run k+1…: mids observed; nothing re-entered ("already_entered")
day end : first run >= local midnight + 1 h -> Gamma resolution: resolved -> ledger settles every held bucket
          at 1 / 0, Brier scored, METAR agreement recorded; pending -> METAR provisional outcome / PnL reported;
          non-binary -> excluded; unresolved after 5 days -> excluded
```

The register is persisted at `artifacts/weather_buckets/register.json` and
reloaded before every run; the ledger at
`artifacts/paper/ledger_weather_bucket_edge.json`. `--reset` starts both fresh.

## Run it

```bash
uv run python -m apps.measure_weather_buckets                      # fixture replay (deterministic, no network)
uv run python -m apps.measure_weather_buckets --network            # public Gamma/CLOB + Open-Meteo + METAR
uv run python -m apps.measure_weather_buckets --network --limit 120 --max-requests 400 --artifact-dir artifacts
uv run python -m apps.measure_weather_buckets --network --dispersion 1.3 --bias-degrees -0.5   # sensitivity only
uv run python -m apps.measure_weather_buckets --help
```

A measurement needs **repeated runs**: one enters today's and tomorrow's
city-days, the first run after each station's local midnight settles them once
the venue resolves (usually within a day). Schedule it every 6–12 h with the
same `--artifact-dir`; at ~50 entered city-days per day the pre-registered
`n ≥ 30` is reachable in a few days, and the $1,000 cost-basis cap recycles as
records settle.

## Artifacts

| Path | Contents |
| --- | --- |
| `artifacts/scoreboard_weather_buckets.json` | dashboard artifact (schema 1.3.0, `meta.source=measured`, `meta.track_family=weather`, `pnl_source=core.ledger.PaperLedger`); `scoreboard_latest.json` only with `--publish-latest` |
| `artifacts/weather_buckets_latest.json` | full report: pre-registered rule, `verdict` (venue-settled) and `verdict_provisional_including_metar`, `station_parse` success rate, `feed` (requests, errors, freshness), `paid_sources`, per-city-day `measurements[]` with the refusal reason, every register `records[]` with model probabilities, per-bucket edges, fills, settlement, METAR check and Brier, ledger, `not_validated[]` |
| `artifacts/weather_buckets/register.json` | the city-day register |
| `artifacts/paper/ledger_weather_bucket_edge.json`, `equity_curve_weather_bucket_edge.jsonl`, `paper/runs/<run_id>.json` | ledger and run manifest |

`npm run sync-artifacts` copies the scoreboard, the report (network runs only;
a `fixture_synthetic` report is refused) and the ledger into
`dashboard/public/artifacts/`; the track is registered in the `weather` family
(`dashboard/scripts/track-families.mjs`, pinned lane).

## Fixture replay (`research/fixtures/weather_buckets_replay.json`)

Four synthetic steps (2026-09-15 14:00Z, 16th 06:00Z, 17th 06:00Z, 18th 12:00Z)
replayed through the live code path. Events mirror the Gamma shape of the live
markets (descriptions in the live wording); books, members, METARs and
resolutions are hand-designed. 36 members from three models per forecast.

| Record | Step 0 | Later | Exercises |
| --- | --- | --- | --- |
| NYC high 15 Sep (`KLGA`, °F) | model 25.5/41.5 = 0.614 on 72-73°F vs ask 0.52 → buy YES 19 @ 0.52 (fee 0.23712); model 0.157 on 70-71°F vs 0.24/0.27 → buy NO 13 @ 0.76 | t1: day over, venue pending, METAR peak 22.8 °C → 73 °F → provisional +11.88; t2: venue resolves 72-73°F → realised **+11.8843**, METAR agrees, Brier model 0.19 < market 0.32 | YES and NO entries, $10/order cap, provisional then venue settlement, agreement |
| London low 16 Sep (`EGLC`, °C) | model 0.494 on 13 °C vs 0.31 → buy YES 32 @ 0.31 (position/market caps); NO on 14 °C @ 0.70 and 15 °C @ 0.85 | t2: METAR trough 13.4 → 13 °C provisional +27.37; t3: venue resolves **14 °C** → realised **−18.6293**, `agrees_with_venue = false` | lead-1 entry, Celsius, METAR/venue disagreement, losing settlement |
| Chicago high 15 Sep (`KORD`) | model agrees with the market → `priced_no_edge`, record kept with 0 contracts | pending forever in the fixture | calibration-only record, never enters the verdict |
| NYC high 17 Sep | t2: buy YES 70-71°F @ 0.43, buy NO 74-75°F @ 0.70 | t3: METAR 71 °F → provisional +16.88, venue pending | open record at the end |

Refused at step 0: Hong Kong (`no_station`), Denver with an "Other" bucket
(`bucket_parse_failed`), Miami without a forecast (`forecast_unavailable`),
Seoul on the 18th (`too_far_ahead`), NYC low with 10 members
(`insufficient_members`), Chicago low with one leg citing `kmdw`
(`station_ambiguous_across_legs`), and a Fed event (`not_a_temperature_event`).
Result: 15 city-day evaluations over four steps, 3 entered, 7 fills, 5
settlement fills, verdict `n = 2 → insufficient_sample`, provisional `n = 3`,
METAR agreement 1 / 2, `equity == 1000 + realized + unrealized`. Asserted in
`tests/test_weather_buckets.py`. These numbers are hand-written and say nothing
about the hypothesis.

## Assumption audit

| # | Assumption | Status | Evidence / what would be needed |
| --- | --- | --- | --- |
| W.1 | Bucket labels, the partition check, station / unit / kind / date parsing and the slug parser are implemented as stated and fail closed | **PASS** | `test_bucket_labels_parse_every_live_shape_and_reject_garbage`, `test_bucket_partition_check_is_fail_closed`, `test_settlement_rules_parse_live_texts_and_fail_closed` (verbatim NYC / Tel Aviv / Taipei / London / Hong Kong texts), `test_city_day_parsing_from_replay_events_is_fail_closed`; live 2026-09-15: 118 / 120 parsed, both refusals are the Hong Kong Observatory events. |
| W.2 | Ensemble → probability arithmetic (rounding, smoothing, dispersion, bias) matches the pre-registration | **PASS** | `test_ensemble_bucket_probabilities_match_hand_calculation`. |
| W.3 | Edge, fee and sizing math; every refusal reason is a pre-registered gate; caps per order / market / city-day / total cost basis and the RiskManager rails bind | **PASS** | `test_fee_formula_and_edge_math_with_caps`, `test_evaluate_refusal_reasons_are_pre_registered_gates`, `test_caps_per_market_city_day_and_total_cash_at_risk_bind`; replay: no fill above $10, no position above 50 contracts. Without the total cap the first network run tied up ~$3,600 of a $1,000 book (fixed before commit). |
| W.4 | The pre-registered verdict is applied exactly and never declares PASS / FAIL below n = 30 | **PASS** | `test_verdict_applies_the_pre_registered_rule` (PASS at 30 tight, FAIL at 30 weak or noisy, `insufficient_sample` at 29, zero-contract city-days excluded, pooled figure reported not judged). |
| W.5 | Venue resolution is the only thing that books settlement; METAR outcomes are provisional and compared | **PASS** | replay: NYC provisional at t1 equals the venue-settled realised at t2; London provisional +27.37 vs venue −18.63 recorded as a disagreement, ledger follows the venue; `test_classify_venue_resolution`. Live 2026-09-15: Gamma `GET /events?slug=` on the resolved Wellington 14 Sep event returns 11 closed legs with exactly one `[1, 0]` → `resolved`; the still-open NYC 14 Sep event → `pending (closed_0_of_11)`. |
| W.6 | Open-Meteo and aviationweather.gov payloads are parsed as documented and both APIs are readable without keys | **PASS** | `test_open_meteo_ensemble_payload_parses_per_model_members`, `test_metar_temperature_prefers_the_tenths_group_and_day_extreme_uses_the_local_day`, `test_public_feed_calls_the_documented_endpoints_and_survives_failures`; live 2026-09-15: 122 members per station-day in °F and °C, `timezone=auto` → `America/New_York` / `Europe/London`, 57 METARs in 48 h for KLGA with T-groups. |
| W.7 | A run without a feed is an honest empty and a failing feed or resolution endpoint cannot kill a run | **PASS** | `test_network_path_without_a_feed_is_an_honest_empty`, `test_exploding_feed_and_lookup_do_not_take_the_run_down`, `test_empty_snapshot_reports_no_weather_markets`. |
| W.8 | The register survives a restart and a resumed replay equals a straight one; a second run over a closed register opens nothing | **PASS** | `test_register_round_trips_and_a_resumed_replay_matches_a_straight_one`, `test_cli_fixture_run_writes_report_register_and_ledger`. |
| W.9 | Paid vendors are gated OFF | **PASS (by construction)** | none implemented; `paid_source_policy` reports `enabled = false, implemented = false` even with `VISUAL_CROSSING_KEY` and `WEATHER_ALLOW_PAID_SOURCES=true` set (`test_paid_sources_are_reported_and_never_enabled`). |
| W.10 | **The edge itself** (≥ 2¢ / contract net of fees on venue-settled city-days) | **UNKNOWN** | The committed network run entered 51 city-days and settled none (a first run cannot). Needs the CLI scheduled every 6–12 h with the same `--artifact-dir` until `verdict.n ≥ 30`. |
| W.11 | Raw ensemble frequencies are calibrated probabilities at 0–1 day lead | **UNKNOWN** | Day-ahead ensembles are typically under-dispersed and station-biased; `dispersion_multiplier` and `bias_degrees` exist but are 1 / 0 until the calibration track fits them from the register's Brier records. |
| W.12 | The grid-interpolated forecast represents the station | **UNKNOWN** | Open-Meteo interpolates 0.25–0.4° global models to the coordinates; airports on coasts or at altitude (Buckley SFB 1,703 m) can differ from the cell by a bucket. The METAR agreement rate and per-station Brier are the diagnostics. |
| W.13 | METAR reconstruction equals NOAA's "Temp" column | **UNKNOWN / one live agreement** | Round-half-up of the T-group in the market unit; NOAA's own rounding, SPECI inclusion and late corrections are not verified, hence provisional only. One live check on 2026-09-15: the resolved `highest-temperature-in-wellington-on-september-14-2026` event (venue winner **16 °C**) vs 48 public `NZWN` METARs on the Pacific/Auckland 14 Sep day → reconstructed high 16 °C, same bucket. The track records this agreement rate on every settled city-day. |
| W.14 | Polymarket `weather_fees` = 5 % · p · (1 − p) taker | **PASS (formula) / UNKNOWN (rebates, changes)** | `venues/polymarket/fees.py` table read 2026-09-14; makers pay nothing; no rebate programme modelled. |
| W.15 | Taker fills at the displayed touch of a frozen snapshot | **UNKNOWN / conservative** | queue, latency and adverse selection are not modelled; mid marks after crossing the spread show as an immediate unrealised loss (−$47.90 on 172 fills in the committed run) until settlement. |
| W.16 | Fixture numbers are evidence | **FAIL as evidence** | hand-written; they prove branches, not the hypothesis. `sync-artifacts` refuses to publish a `fixture_synthetic` report. |

## Limitations (deliberate, v1)

* **Station mismatch.** The parser admits only an unambiguous ICAO id from the
  rules text and forecasts at *that* station's coordinates; a city whose rules
  cite a non-airport source (Hong Kong Observatory) is refused, not
  approximated. Whether the grid cell represents the runway sensor is W.12.
* **Model lag.** Open-Meteo does not expose the model run time on the ensemble
  endpoint; freshness is measured as fetch age (`forecast_age_hours_max`, gate
  12 h) and the lead in local days, not as model initialisation time.
* **Fees.** Only the published taker formula; entering at the touch pays the
  spread on top, which is why the entry threshold is net of fees and the mid
  marks look negative until settlement.
* **NegRisk multi-bucket.** Buckets of one event are one outcome; buying YES on
  one bucket and NO on a neighbour is correlated exposure, capped per city-day
  and scored as one city-day. The NegRisk NO→collateral conversion and
  sum-of-mids overround are reported (`sum_of_mids`) but not traded.
* **One entry per city-day.** Later runs observe and settle; they never add to
  or hedge a position, so intraday model updates are not exploited.
* Not part of `apps.measure_all`'s thirteen-track board; its own family with
  its own CLI, like `tennis_basis` and the Polymarket arb tracks.
