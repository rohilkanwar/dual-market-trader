# Weather per-city model calibration A/B (`weather_naive_ensemble` vs `weather_calibrated_ensemble`)

Paper-only measurement track on Polymarket's daily *Highest temperature in
&lt;city&gt; on &lt;date&gt;?* bucket markets. Two isolated paper lanes price the same
events from the same free forecasts; only the calibration differs. Everything
here reads public data (Gamma, CLOB, Open-Meteo) and books fills into a
`PaperLedger`; nothing places an order. Entry point:
`python -m apps.measure_weather_calibration`.

## 1. Hypothesis and pre-registration

**Hypothesis.** (a) Some free NWP models are systematically better per city;
(b) a per-city bias- and hit-rate-weighted ensemble beats the unweighted
average as a forecast; (c) trading a bucket only when the calibrated
probability differs from the market mid by at least a threshold yields better
*settled* net EV after fees than the same rule on the naive ensemble.

**Pre-registered rule** (`strategies/weather_calibration.py::WeatherParameters`,
frozen before any network run):

| Parameter | Value | Meaning |
| --- | --- | --- |
| `edge_threshold` | 0.05 | a leg is a candidate trade when \|p − mid\| ≥ 5¢ |
| `minimum_edge` / `fee_buffer_per_contract` | 0.02 / 0.01 | the shared fair-value engine must still find a positive cost-adjusted edge at the touch |
| `maximum_order_size` | 10 contracts | per leg, both lanes; rails $100/order, $500/market, $250 daily |
| `min_calibration_samples` | 20 settled city-days | below this the calibrated lane refuses the city (`calibration_underpowered`) |
| `admissible_lead_days` | (1,) | only events whose observation day is city-local *tomorrow* are priced |
| `min_settled_per_lane` | 50 settled admissions | sample floor for PASS / FAIL |
| `margin_vs_naive` | 0.02 / contract | calibrated EV − naive EV must reach this |
| `absolute_min_ev` | 0.025 / contract | calibrated EV must reach this on its own |

**Verdict.** `PASS` only when both lanes have ≥ 50 settled admissions,
`EV_cal − EV_naive ≥ 0.02` and `EV_cal ≥ 0.025` per contract, where
`EV = Σ signed contracts × (settlement − YES-equivalent entry) − taker fees`
over settled admissions. Fewer settled admissions in either lane is
`UNDERPOWERED`; anything else is `FAIL`. A 95 % bootstrap CI over city-day
clusters is reported next to the point estimate but is not part of the rule.

Nothing in the fixture or the network snapshot can produce a PASS: admissions
settle the day after the observation day, so the verdict starts at
`no_settled` and moves through `UNDERPOWERED` only as the loop runs daily.

## 2. Data (all free, no keys)

| Source | Used for | Notes |
| --- | --- | --- |
| Gamma `GET /events?tag_id=104596` (open) | the universe: one NegRisk event per city × day, 11 bucket legs each | `groupItemTitle` carries the bucket label; `feeType=weather_fees` → 5 % taker fee on paper fills |
| CLOB `POST /books` | one YES book per leg, batched | mids and touches; one-sided books are refused (`no_two_sided_mid`) |
| Gamma `GET /events?tag_id=104596&closed=true&end_date_min/max` | **truth**: the leg that settled at `outcomePrices ["1","0"]` | paged by one-week windows; the settled bucket's midpoint is the continuous stand-in for the station reading |
| Open-Meteo `/v1/forecast?models=…&hourly=temperature_2m` | live per-model day-ahead maximum (max over the target day's 24 local hours) | GFS, ECMWF IFS 0.25°, ICON, GEM, Météo-France, UKMO, JMA ("seamless" variants) |
| Open-Meteo `previous-runs-api` `temperature_2m_previous_day1` | archived day-ahead forecasts (the run issued one day earlier) for ≤ 92 past days | fills the calibration store; one call per city |
| `data/weather/cities.json` | 51 cities → resolution station, coordinates, time zone, unit, source precision | read from the rule texts on 2026-09-15; Jinan and Taipei name no station |

Paid Open-Meteo endpoints are **off** by default: they are used only when
`--allow-paid-keys` is passed *and* `OPEN_METEO_API_KEY` is set, and every
report records `paid_key_used`.

### Bucket semantics

US stations report whole °F and the ladders use two-degree buckets
(`65°F or below`, `66-67°F`, …, `84°F or higher`); everywhere else whole °C
with one-degree buckets, except Hong Kong (Observatory, one decimal). A
whole-degree label `74-75°F` is the reading rounded to 74 or 75, i.e. the
continuous interval `[73.5, 75.5)`; a one-decimal `31°C` is `[31.0, 32.0)`.
`strategies/weather_types.py::TemperatureBucket.edges` implements both;
ladders that are not contiguous are refused (`ladder_not_contiguous`).

## 3. The two lanes

Both lanes turn a mean and a sigma into bucket probabilities with a normal
CDF over the event's own ladder (probabilities tile the line exactly).

* **Naive (control).** Mean = equal-weight average of the raw per-model
  maxima. Sigma = pooled standard deviation of `truth − naive mean` over every
  city in the store (3.0 °F when the store is empty), floored at 1 °F.
* **Calibrated.** Per city × model, from settled days strictly before the
  target day and within 120 days: bias `mean(forecast − truth)`, variance of
  the de-biased error, de-biased bucket hit rate. Weights
  `w_m ∝ (hit_m + 0.1) / var_m`, shrunk toward equal weights by 10
  pseudo-samples (`λ = n / (n + 10)`). Mean = `Σ w_m (f_m − bias_m)`; sigma =
  the city's residual std of that mean, floored at 1 °F / 0.56 °C.

Per leg, each lane hands its probability to the primary track's
`CalibratedFairValueStrategy` as the prior; the engine picks the touch,
subtracts the fee buffer, requires ≥ 1 contract of depth and sizes within the
rails. Identical books, thresholds, sizes and rails, so the only difference
between the lanes is the probability.

## 4. One cycle

1. **Settle.** Every pending city-day whose date has passed (UTC) is looked
   up on Gamma. A closed event with exactly one YES leg settles both lanes'
   positions on its legs at 1 / 0 through `PaperLedger.settle`, records the
   net EV of each admission, and adds the city-day — the forecasts recorded
   *at first sight* plus the settled bucket — to the calibration store as a
   live-lead sample (never overwritten by an archive backfill). Closed events
   without a unique winner are `ambiguous`; positions stay open and flagged.
2. **Discover.** Open weather events; keep those whose city is in the
   registry, whose ladder is contiguous and whose observation day is
   city-local tomorrow.
3. **Price.** One forecast call per city-day (cached); both ensembles;
   ladder probabilities; per-leg admission; fills into the lane's ledger and
   the register. Re-runs on the same day observe the same city-days again
   (`observations` increments) and only add to a leg up to the target size
   (`target_position_reached`), never re-open a settled admission.

`--backfill-days N` (network) runs before step 2: closed events for the last
N observation days → per-city settled buckets → one previous-runs call per
city → samples for every day with ≥ 3 model values.

## 5. What was measured (2026-09-15, sandbox with outbound internet, no keys)

`python -m apps.measure_weather_calibration --network --reset --backfill-days 60`

| Item | Result |
| --- | --- |
| Backfill | 3,001 closed events → 3,001 resolved city-days, 51 cities, 2026-07-17 … 2026-09-14, 0 unresolved, 0 missing forecasts, 100 Open-Meteo calls, 0 errors |
| Calibration store | 51 / 51 cities adequate (≥ 20 days; 59–60 each, Jinan/Zhengzhou 39) |
| Best model by city (lowest MAE) | ICON 17, UKMO 15, GFS 6, ECMWF 5, Météo-France 4, GEM 3, JMA 1 — in 31 of 51 cities the best model's MAE is < 0.8 × the median model's |
| Per-city bias | ECMWF from −3.7 °F (San Francisco) to **+10.6 °F (Los Angeles / KLAX)**; GFS +2.2 °C in Lucknow, −3.4 °C in Seoul; Jinan (station unknown) shows +5 to +6 °C on every model and MAE 3.5–5 °C, i.e. the registry coordinate is probably not the resolution station |
| **Walk-forward forecast skill** (1,923 city-days whose city already had ≥ 20 earlier days; statistics from earlier days only; *not* the pre-registered test) | MAE naive 1.92 °F → calibrated **1.41 °F**; top-bucket hit 29.5 % → **43.6 %**; mean p(settled bucket) 0.206 → **0.301**; log score −1.69 → **−1.43**; multiclass Brier 0.785 → **0.690** |
| Live cycle | 117 open events listed, 49 priced (lead 1, 49 cities), 68 refused `lead_not_admissible` (today / day after tomorrow); 1,287 legs per lane, 133 one-sided books |
| Admissions | naive 184 legs (222 below threshold), calibrated 143 legs (263 below threshold); fees $11.9 / $11.0; every position open, marked at mid |
| **Paper A/B verdict** | **`no_settled`** → `UNDERPOWERED` (0 settled admissions in either lane; floor 50) |

Reading: (a) and (b) are supported by the walk-forward skill numbers — model
skill differs by city and the calibrated forecast is materially better than
the naive one *against the truth*. (c) is **not measured**: the market may
already price the same local biases: over the 406 two-sided legs priced in
the live cycle the calibrated probability is on average 5.5¢ from the mid
against 8.2¢ for the naive one (closer on 55 % of legs), i.e. the market
already knows part of what the calibration learns. The paper EV test needs
settled admissions that only a daily loop produces.

Snapshots: `dashboard/public/artifacts/scoreboard_weather_calibration.json`,
`weather_calibration_latest.json`, `paper_ledger_weather_*.json`.

## 6. Running it

```bash
python -m apps.measure_weather_calibration                                   # fixture replay, deterministic, no network
python -m apps.measure_weather_calibration --network --backfill-days 60      # fill the store, price tomorrow, settle yesterday
python -m apps.measure_weather_calibration --network                         # daily thereafter (same --artifact-dir)
```

Artifacts land in `artifacts/`: `scoreboard_weather_calibration.json`
(`meta.track_family=weather_calibration`, both lanes as tracks),
`weather_calibration_latest.json` (full A/B report), `weather_calibration/
register.json` (priced city-days with their first-sight forecasts, every
admission), `weather_calibration/calibration_store.json` (every settled
sample), `paper/ledger_weather_naive_ensemble.json`,
`paper/ledger_weather_calibrated_ensemble.json`. `--no-persist` makes a run
independent; `--reset` starts everything empty. Pre-registered values can be
overridden (`--edge-threshold`, `--min-settled`, …) for sensitivity checks
only; the report records whatever was used.

## 7. Validated / not validated

**Validated (code + tests, `tests/test_weather_calibration.py`)**

* Bucket parsing for every label form, whole vs one-decimal edges, open ends,
  ladder contiguity, title/date/city parsing, the 51-city registry, city-local
  lead computation.
* Ladder probabilities tile the line; the store learns bias, de-biased
  variance and hit rate; weights favour low-variance models and shrink to
  equal on thin history; statistics never include the target day; live
  samples are not overwritten by backfills; the store and register round-trip.
* Both lanes trade toward their probability through the shared engine with
  the venue's 5 % weather taker fee; settlement books net EV that ties to
  the ledger (`realized = settled net EV − fees on open admissions`, equity
  identity asserted); ambiguous / missing settlements leave positions
  untouched; a resumed run re-admits nothing.
* The verdict applies the rule (no_settled / UNDERPOWERED / PASS / FAIL); the
  CLI refuses live flags; paid keys stay off without the flag.
* Live capture: Gamma weather events parse (51 cities, 11 legs, all
  contiguous), settled legs resolve uniquely on 3,001 / 3,001 closed events,
  Open-Meteo returns all seven models for every city, `previous_day1` reaches
  92 days back.

**Not validated / UNKNOWN**

* **The pre-registered paper EV test** — no settled admissions yet.
* Calibration samples come from the previous-runs archive (run issued ~24 h
  before the day); live admissions use the freshest run at ~20:00–08:00 local
  the evening before. The calibration lead is longer than the trading lead;
  live-lead samples replace archive samples as city-days settle, so the store
  converges to the trading lead over time.
* The settled bucket's midpoint stands in for the station reading; the ±1 °F
  / ±0.5 °C quantisation sits inside the residual sigma, not modelled apart.
* Per-model hit rates, weights and the residual sigma are in-sample over the
  city's window when used for the *next* day; only the settled paper EV (and
  the walk-forward table, by construction) is out of sample.
* Station coordinates are approximate airport positions; Jinan and Taipei
  have no station in the rule text and Jinan's residuals say the coordinate
  is wrong — the calibrated lane still trades it because its *residuals* are
  what the sigma measures, which is the intended behaviour, but the city is
  a candidate for exclusion once the true station is known.
* Naive sigma is pooled over cities and units (converted to °F), which is a
  design choice for the control, not a claim about the models.
* Whether the market already prices these biases (hypothesis (c)) is the
  open question the paper A/B exists to answer.

## 8. Fail risks the loop should surface

* Calibrated lane keeps admitting legs whose mid already sits at the
  calibrated probability's neighbourhood → few admissions, slow sample.
* Settled EV negative in both lanes: the market prices the day-ahead
  distribution better than any free ensemble (likely for US airports with
  active traders) → FAIL is the honest outcome.
* Gamma stops serving `outcomePrices` for closed events or changes the
  ladder shape → `ambiguous` counts rise; positions stay open and unmarked
  positions are flagged on the board.

## 9. Coordination with sibling weather tracks

`strategies/weather_types.py` (city registry, `TemperatureBucket`,
`WeatherEvent`/`WeatherLeg`, title/date parsing) and
`research/weather_sources.py::PolymarketWeatherUniverse` (discovery, batched
books, settlement lookup) are strategy-neutral so the bucket-edge and late-day
METAR tracks can import them; nothing in them prices or trades. If a sibling
lands its own discovery first, `event_from_gamma` is the only function that
needs to be swapped for the shared one.
