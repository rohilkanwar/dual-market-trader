# `weather_dead_bucket` — late-day METAR / running-high dead buckets on Polymarket

Paper-only measurement track (own CLI: `apps.measure_weather`; family `weather`,
track id `weather_dead_bucket` as reserved by the dashboard-wiring contract in
`research/weather_tracks.py`; sisters `weather_bucket_edge`,
`weather_calibrated_ensemble`). It asks one pre-registered
question: **once the settlement station's running daily high is in — and, late
in the day, the temperature is falling — do Polymarket daily-temperature
buckets that can no longer contain the high still quote enough leftover
probability that buying NO on them (and YES on the one certain bucket) earns
≥ 2¢ net per contract after the taker fee on settled days?**

$0 and paper-only by construction: free public station observations only
(aviationweather.gov METAR history; api.weather.gov as fallback), public Gamma /
CLOB reads, no venue credentials, no order ever leaves the process. Status legend
follows `docs/ASSUMPTIONS.md` (**PASS** = code + test or reproducible run here;
**FAIL** = falsified; **UNKNOWN** = cannot be validated from this repository,
evidence needed stated).

## What the markets are

Polymarket lists one NegRisk event per city per local day, "Highest temperature
in NYC on September 15?", tagged `Highest temperature` (Gamma tag `104596`) and
`Daily Temperature` (`103040`). Each leg is an integer bucket in the market's
unit — `65°F or below`, `66-67°F`, …, `84°F or higher` (2° wide, 11 legs) — and
the description fixes the settlement (verbatim, 2026-09-15):

> This market will resolve to the temperature range that contains the highest
> temperature recorded by NOAA at the **LaGuardia Airport Station** in degrees
> **Fahrenheit** on **14 Sep '26**. The resolution source … specifically the
> highest reading under the "Temp" column for all times on this day, available
> here: `https://www.weather.gov/wrh/timeseries?site=klga` … This market will
> resolve off of the **Hourly Data** … whole degrees Fahrenheit … Revisions …
> will be considered until the first datapoint for the following date has been
> published.

54 of the 56 live events on 2026-09-15 used that NOAA WRH shape (US stations
plus SAEZ, SBGR, CYYZ, MMMX, MPMG, EGLC, LFPB, EDDM, LIMC, LEMD, EPWA, LTAC,
LTFM, UUWW, LLBG, RKSI, RKPK, RJTT, ZBAA, ZSPD, ZGSZ, ZHHH, ZUUU, ZUCK, WSSS,
VILK, NZWN, RCSS); older markets point at Weather Underground history pages
ending in the ICAO; Hong Kong settles on the Hong Kong Observatory table (no
station → refused).

## Shared types (for the sibling weather bucket-edge track)

Defined once in `strategies/weather_dead_bucket.py`, documented there:

| Type | Fields | Notes |
| --- | --- | --- |
| `TemperatureUnit` | `F` \| `C` | the unit the market settles in |
| `TemperatureBucket` | `lo: int \| None`, `hi: int \| None`, `label` | closed integer interval; `None` = open end; `parse_bucket_label("66-67°F", unit)` refuses unit mismatches and inverted ranges |
| `WeatherMarketSpec` | `station_icao`, `city`, `local_date`, `unit`, `kind` (`highest`), `resolution_source` (`noaa_wrh_timeseries` \| `wunderground_history` \| `unsupported`), `source_url`, `timezone` (IANA), `hourly_only` | `parse_weather_market(event_title, description, resolution_source_url)` — station **only** from a recognised URL, date/unit **only** from the resolution sentence |
| `StationObservation` | `station_icao`, `observed_at` (UTC), `temp_tenths_c` (METAR `T` group), `report_type` (`METAR` \| `SPECI`), `raw` | produced by `research/weather_obs.py` from aviationweather / NWS / fixtures |
| `RunningHigh` | hourly `high_low`/`high_high` (both rounding candidates), `all_high_high` (incl. SPECI), latest temp, trend, `day_complete`, … | `running_high(observations, spec)` |

Station → timezone lives in `STATION_TIMEZONES` (read from the live
descriptions; unknown stations are refused, nothing is looked up online).

### Track-id contract (`research/weather_tracks.py`)

`research/weather_dead_bucket.py` imports the contract when that module is
present and mirrors it otherwise, so nothing in `research/scoreboard.py` is
touched by this branch:

| Contract item | Value here |
| --- | --- |
| family | `weather` (`meta.track_family`, `metrics.family`) |
| reserved ids | `weather_bucket_edge`, **`weather_dead_bucket`**, `weather_calibrated_ensemble` (`RESERVED_WEATHER_TRACKS`) |
| label | `Weather dead bucket (late-day METAR)` (set on the summary, matches `WEATHER_TRACK_LABELS`) |
| scoreboard | `scoreboard_weather.json` (`WEATHER_SCOREBOARD_NAME`) |
| report | `weather_report_<mode>.json` + `weather_report_latest.json`, `kind = weather_report`, `paper_only`, `meta.source = measured`, one block per track under `tracks[<id>]` (`build_weather_report`) |
| `METRIC_KEYS` on `summary.metrics` | `status`, `source {name, requests, errors}`, `markets` (events), `buckets` (legs), `cities`, `stations`, `stations_parsed`, `dead_bucket {candidates, kills, certain_yes, kills_without_taker_ask, positions_opened}`, `evaluation {status ∈ not_run / no_candidates / pending_resolutions / underpowered / pass / fail, preregistered_n, n, station_days, verdict, kill_rule_triggered}` |
| runner hook | `run_weather_tracks(snapshots, *, ledgers, starting_cash, model_fees, use_fixtures, cycle_label)` → `([summary], {id: ledger})`; the register travels in `metrics["register_state"]` |

`evaluation.status` is `pass` / `fail` only when the pre-registered verdict says
so, `underpowered` while settled positions exist below the floors,
`pending_resolutions` with open positions and nothing settled,
`no_candidates` when nothing was classified, `not_run` otherwise.

## Pre-registration

| Item | Value | Where |
| --- | --- | --- |
| Observations | routine hourly METARs (`report_type = METAR`) of the settlement station inside the market's **local** calendar date; SPECI specials only widen the upper kill; temperature from the `T` group (tenths °C), converted to the market's unit with **both** tie-rounding candidates | `running_high` |
| Rule 1 `dead_below_running_high` | `bucket.hi < high_low` — the high can only rise, so this holds at any hour | `classify_bucket` |
| Rule 2 `dead_above_late_day` | local time ≥ 17:00 on the market date (or later), latest hourly temp ≤ `high_low − 2°F` (`1°C`), last 2 hourly obs non-rising, and `bucket.lo > all_high_high + 1` | `classify_bucket`, `DeadBucketParameters` |
| Rule 3 `certain_yes_late_day` | same gate and the bucket covers `[high_low, all_high_high + 1]` | `classify_bucket` |
| Rule 4 `dead_day_complete` / `certain_yes_day_complete` | an hourly observation dated the **following** local day exists (the market's own resolution trigger); bucket contains neither / both rounding candidates of the final high | `classify_bucket` |
| Refusals | `bucket_edge_ambiguous` when a rounding tie straddles a bucket edge; `too_early_in_day`, `not_falling`, `live`, `insufficient_observations` (< 3 hourly obs) | `classify_bucket` |
| Event-level refusals | `station_unparsed`, `date_unparsed`, `unit_unparsed`, `station_timezone_unknown`, `market_kind_unsupported` (lowest-temperature events), `no_obs`, `station_mismatch` (source answered with another station), `stale_obs` (latest hourly obs > 90 min old while the day is open) | `parse_weather_market`, `run_weather_dead_bucket_cycle` |
| Entry | dead → **buy NO** at the NO ask (venue NO ladder, else the complement of the YES book); certain → **buy YES** at the YES ask; `net = 1 − ask − rate·ask·(1−ask)` with `rate` from Gamma `feeType` (`weather_fees` = 5 %); require `net ≥ 0.02`, ask depth ≥ 5 contracts (venue minimum); whole contracts ≤ `$25` per order, ≤ 75 per market, `$75` daily loss (`WEATHER_RISK_LIMITS`) | `DeadBucketStrategy` |
| Settlement | Gamma `outcomePrices` `[1,0]` / `[0,1]` on a closed market books 1/0 on the `PaperLedger`; the observation-implied outcome (final high vs bucket) is recorded next to it (`agree` / `disagree` / `unknown`) | `_settle`, `obs_implied_outcome` |
| Sample | settled records bought as NO (primary); the certain-YES sample is reported separately | `verdict` |
| **Pass** | `n ≥ 30` settled NO positions across `≥ 10` distinct station-days, mean net PnL per contract `≥ 0.02`, **zero losses** | `verdict` |
| **Fail** | `n ≥ 30` and (mean `< 0.02` or any loss) | `verdict` |
| **Kill** | one settled dead-bucket position that resolved against us (a "certain" leg pays ~97¢ to win ~3¢); flagged at any `n` as `kill_rule_triggered` | `verdict` |
| Otherwise | `INSUFFICIENT_DATA`, never pass/fail | `verdict` |

Parameters live in `DeadBucketParameters` and are echoed into every artifact
(`metrics.parameters`, `verdict.pre_registered`); the CLI exposes them under
"pre-registered parameters (override only for sensitivity checks)".

## Free observation sources

| Source | Endpoint | Used for |
| --- | --- | --- |
| aviationweather.gov Data API (NOAA AWC) | `GET https://aviationweather.gov/api/data/metar?ids=KLGA&format=json&hours=30` — decoded METAR/SPECI history, worldwide, no key (`icaoId`, `obsTime`, `temp`, `metarType`, `rawOb`) | primary; 39 stations in 39 requests on 2026-09-15, 0 errors |
| api.weather.gov (NWS) | `GET /stations/{id}/observations?start=&end=` (`User-Agent` required) | per-station fallback when the AWC answer is empty or errors |
| `--obs-file` | a saved AWC JSON response | operator replay |
| not used | `weather.gov/wrh/timeseries` (the resolution table itself: no documented public JSON API); Weather Underground (not scraped) | — |

Both sources are the same ASOS observations the WRH table is built from. What
this track cannot see is the table's own rounding of tenths to whole degrees and
any late correction: hence both rounding candidates (`bucket_edge_ambiguous`
fail-closed) and the venue-vs-observation `agreement` field on every settled
record.

## Run it

```bash
uv run python -m apps.measure_weather                     # fixture replay (4 steps, deterministic, no network)
uv run python -m apps.measure_weather --network           # public Gamma tag 104596 + CLOB books + station METARs
uv run python -m apps.measure_weather --network --limit 100 --artifact-dir artifacts   # more events (soonest end first)
uv run python -m apps.measure_weather --help
```

A measurement needs **repeated runs against the same `--artifact-dir`**: the
kill rules only fire late in each station's local day (17:00–24:00 local) and
after the next day's first observation, and settlement arrives the following
day, so schedule it hourly (cron / the always-on host from the README). The
register (`artifacts/weather_dead_bucket/register.json`) and the ledger
(`artifacts/paper/ledger_weather_dead_bucket.json`) carry across runs; `--reset`
starts both fresh; `--no-persist` runs a throwaway cycle.

## Artifacts

| Path | Contents |
| --- | --- |
| `artifacts/scoreboard_weather.json` | dashboard artifact (`meta.source=measured`, `meta.track_family=weather`, `pnl_source=core.ledger.PaperLedger`); `scoreboard_latest.json` only with `--publish-latest` |
| `artifacts/weather_report_<mode>.json`, `weather_report_latest.json` | `kind=weather_report`; `tracks.weather_dead_bucket` = pre-registration, `verdict` (dead-bucket NO) and `verdict_certain_yes`, `evaluation`, every event's parsed spec + running high + observation status, every bucket leg's reason and numbers, `resting_only_opportunities` (dead legs with no taker ask and where the resting queue sits), every register record, ledger, sources, `not_validated[]` |
| `artifacts/weather_dead_bucket/register.json` | the position register (open and settled records, venue vs observation agreement) |
| `artifacts/paper/ledger_weather_dead_bucket.json`, `equity_curve_weather_dead_bucket.jsonl`, `paper/runs/<run_id>.json` | ledger and run manifest |

`npm run sync-artifacts` copies the scoreboard and the ledger into
`dashboard/public/artifacts/`; the wiring branch's sync also publishes
`weather_report_<mode>.json` (kind `weather_report`, `paper_only`, never a
`fixture_synthetic` status) and places the track in the **Weather** lane.

## Fixture replay (`research/fixtures/weather_dead_bucket_replay.json`)

Synthetic. Four steps replayed through the live code path:

| Step | `as_of` | What it exercises |
| --- | --- | --- |
| `t0_morning` | 15:00Z (11:00 EDT / 10:00 CDT / 08:00 PDT) | NYC/KLGA high 72 → `66-67`, `68-69`, `70-71` dead-below and bought (25 / 26 / 10 contracts; `63-` and `64-65` `edge_below_threshold` at 0.998 / 0.99); Chicago/KORD high 71 (`68-69` refused `insufficient_depth` at 3 contracts); Dallas/KDAL high **22.5 °C = 72.5 °F** → `71-72` refused `bucket_edge_ambiguous`; Seattle `stale_obs` (last obs 4 h old); Denver without a URL `station_unparsed`; Hong Kong Observatory `station_unparsed`; a "Lowest temperature" event `market_kind_unsupported`; Miami answered with KFLL reports `station_mismatch`; Reykjavik `station_timezone_unknown`; every upper bucket `too_early_in_day` |
| `t1_late_day` | 22:30Z (18:30 EDT / 17:30 CDT / 15:30 PDT / 19:30 ART) | NYC hourly high 75, SPECI 76, latest 68 → ceiling 77: `72-73` dead-below, `78-79` dead-above, `80-81` dead but **no NO ask** (`no_ask`, reported as resting-only), `74-75` / `76-77` `live`; Chicago high 78 latest 75 → full ladder incl. `78-79` **certain YES** at 0.55 (45 contracts) and `80-81`/`82+` dead-above; Dallas high 81 latest 81 → `not_falling` above, dead below; Seattle 15:30 local → `too_early_in_day` above; Buenos Aires (°C) high 20 latest 16 → `20-21` certain YES at 0.70, `22-23` dead-above |
| `t2_day_complete` | 05:30Z next day | NYC and Buenos Aires have a next-day observation: `74-75` certain YES at 0.96, `76-77` dead; Chicago (23:51 CDT, no next-day obs yet) only `already_positioned` / `edge_below_threshold` |
| `t3_settlement` | +1 day | venue resolutions: NYC `74-75`, Chicago `78-79`, Dallas `81-82`, Seattle `68-69` — every position wins; **Buenos Aires is staged to resolve `18-19`** against the observed 20 °C, so `18-19` NO and `20-21` YES lose (`agreement = disagree`) and the kill rule trips |

Result: 140 leg evaluations, 32 positions, 32 settlements, 64 ledger fills,
everything flat, `equity == 1000 + realized + unrealized`; dead-bucket NO
verdict `n = 29`, 5 station-days, mean net 3.6¢/contract, **1 loss →
`INSUFFICIENT_DATA` with `kill_rule_triggered = true`**; certain-YES `n = 3`,
1 loss. These numbers prove branches, not the hypothesis. Asserted in
`tests/test_weather_dead_bucket.py`.

## Network snapshot (2026-09-15 01:39 UTC, committed)

One cycle, run `20260915T013931Z-7ecc35ac`, 56 events / 616 legs, 39 stations
(aviationweather.gov, 0 errors), **0 admits**:

| Reason | Legs | Meaning |
| --- | --- | --- |
| `too_early_in_day` | 382 | the September-15 events: their local day had not started (US) or had only morning observations (Europe / Asia) |
| `no_ask` | 167 | **every** dead leg of the September-14 US/LatAm events (21:00 local, running highs in for hours) had **no NO ask**: the NO ladder was bids only (best bid 0.999, thousands of contracts) with YES asks at 0.001–0.003. Nobody offers NO on a dead bucket; the "leftover probability" is a one-sided book's mid, not a quote you can take |
| `not_falling` | 27 | Europe/Asia mornings with the latest hourly temp at the running high |
| `live` | 18 | buckets overlapping the plausible range (the high on a bucket's top edge cannot certify the bucket with 1° headroom) |
| `station_unparsed` | 22 | Hong Kong (Observatory table, no ICAO) |

`resting_only_best_bid`: `n = 167, min = median = max = 0.999` — the most a
resting NO buyer could earn on those legs is 0.1¢ gross, below the 5 % fee-free
maker economics anyway. The taker version of the hypothesis is therefore
**not supported on fully-dead legs at 21:00 local**; whether the earlier
late-day window (17:00–19:00 local, when the market has just seen the high)
offers takeable NO asks is what the scheduled runs must measure. Nothing was
bought, the ledger is flat, verdict `INSUFFICIENT_DATA`.

## Assumption audit

| # | Assumption | Status | Evidence / what would be needed |
| --- | --- | --- | --- |
| W.1 | Bucket labels, station/date/unit parsing, METAR `T`-group parsing and unit conversion are implemented as stated and fail closed | **PASS** | `test_bucket_labels_parse_ranges_open_ends_and_refuse_unit_mismatch`, `test_weather_market_spec_is_parsed_from_the_resolution_text_only`, `test_spec_parsing_fails_closed` (HKO, lowest, no date, no unit, unknown tz), `test_metar_temperature_prefers_the_t_group_and_falls_back_to_the_body`; live 2026-09-15: 62/100 tagged events parsed, 36 `lowest` refused, 2 Hong Kong refused |
| W.2 | The running high uses hourly reports only, SPECI only widens the upper kill, the next-day observation completes the day, and .5 ties carry both candidates | **PASS** | `test_running_high_uses_hourly_reports_specials_only_widen_and_next_day_completes`, `test_unit_conversion_carries_both_rounding_candidates_at_exact_halves` (22.5 °C → 72/73 °F, −0.5 °C → −1/0) |
| W.3 | Kill rules: below-high at any hour; above-high only late, falling and beyond headroom; day-complete decides everything; ties refuse | **PASS** | `test_bucket_below_the_running_high_is_dead_at_any_hour_and_the_rest_waits`, `test_late_day_falling_kills_above_and_names_the_certain_bucket`, `test_day_complete_decides_every_bucket_and_halves_refuse`, `test_celsius_markets_use_the_one_degree_margin` |
| W.4 | Entry uses the venue's NO ladder (or the YES complement), the published 5 % weather taker fee, whole contracts and the paper caps; no taker ask → `no_ask` with the resting queue reported | **PASS** | `test_strategy_buys_no_at_the_no_ask_sized_by_caps_and_depth`, `test_certain_yes_buys_yes_at_the_yes_ask`; live books verified 2026-09-15 (NO bids 0.997–0.999, no NO asks) |
| W.5 | Positions go through `TrackRuntime.submit` → `ExecutionEngine` → `RiskManager`; PnL is ledger-backed; settlement books 1/0 | **PASS** | `test_replay_measures_every_scripted_branch` (64 fills, identity holds, fees `25·0.05·0.975·0.025`), `test_scoreboard_artifact_is_measured_and_ledger_backed` |
| W.6 | The register survives a restart and a resumed replay equals a straight one; a settled register opens nothing new | **PASS** | `test_register_round_trips_and_a_resumed_replay_matches_a_straight_one`, `test_replaying_a_settled_register_again_opens_nothing_new`, CLI second run `opened=0` |
| W.7 | Observation sources: AWC and NWS payloads normalise identically; per-station fallback; failures are reported, never raised; no source → honest empty | **PASS** | `test_aviationweather_and_nws_payloads_normalise_to_the_same_observation`, `test_chained_source_falls_back_per_station_and_reports_errors`, `test_network_path_without_observations_is_an_honest_empty`, `test_exploding_observation_source_does_not_take_the_run_down`, `test_stale_observations_refuse_the_whole_event` |
| W.8 | Event discovery reads Gamma tag 104596 and both CLOB books; closed legs are skipped; fee metadata lands on the leg | **PASS** | `test_weather_event_discovery_reads_tagged_gamma_events_and_both_books`; live: 56 events, 616 legs, 0 book errors |
| W.9 | The WRH table's whole-degree value equals the METAR `T`-group value rounded | **UNKNOWN / handled** | Same ASOS feed, but the tie rule is undocumented and late corrections are allowed until the next day's first point. Handled fail-closed (`bucket_edge_ambiguous`) and measured after the fact (`agreement` on every settled record; fixture stages one `disagree`). Evidence: settled records with `agreement = disagree` on live data would quantify it |
| W.10 | "Late day and falling" implies no further rise | **UNKNOWN / heuristic** | 17:00 local, 2°F/1°C below the high, two non-rising hourly obs, 1° headroom. Fronts, foehn winds and coastal reversals can break it; that is exactly what the kill rule catches. Evidence: settled `dead_above_late_day` records |
| W.11 | Dead legs quote a takeable NO ask late in the day | **FAIL (on 2026-09-15 21:00 local, 167/167 legs)** | Every dead leg's NO ladder was bids only at 0.999; the mid in `outcomePrices` (0.9995) is not a quote. Whether the 17:00–19:00 window differs is UNKNOWN until scheduled runs cover it |
| W.12 | **The hypothesis** (≥ 2¢ net per contract on settled dead-bucket NO positions, zero losses) | **UNKNOWN** | 0 settled positions; verdict `INSUFFICIENT_DATA`. Needs hourly runs for weeks until `n ≥ 30` over `≥ 10` station-days |
| W.13 | Fixture numbers are evidence | **FAIL as evidence** | Hand-written METAR sequences and books; the Buenos Aires disagreement is staged |

## Not in v1 (deliberately)

* No resting / maker variant (the live finding says that is where the queue
  is; it would need fill-probability assumptions like the FLB maker track).
* "Lowest temperature" events (mirror rules; refused `market_kind_unsupported`).
* Weather Underground and Hong Kong Observatory resolution sources (refused).
* No forecast model of any kind: the only inputs are the station's own
  observations and the clock.
* Not part of `apps.measure_all`'s thirteen-track board; own family and CLI,
  like `tennis_basis` and the Polymarket arb tracks. `run_weather_tracks` has the
  contract's runner shape so a family aggregator (`research.weather_scoreboard`,
  owned by whoever lands it) can delegate to it; this branch does not create that
  module to avoid colliding with the sister tracks.
