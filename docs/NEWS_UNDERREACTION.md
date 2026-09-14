# `news_underreaction` — optional paper lane

Paper-only measurement scaffold for the hypothesis that prediction-market
prices underreact to public signals and drift toward the signal-implied
probability over the following minutes. Nothing in this lane places, signs or
routes an order; paper fills are simulated against the frozen snapshot book by
the same engine as every other track and booked on the lane's own
`PaperLedger`.

Status legend follows `docs/ASSUMPTIONS.md`: **PASS** = validated by code plus
a test or reproducible run here; **UNKNOWN** = cannot be validated from this
repository (what evidence would be needed is stated).

## Literature anchor

arXiv:2606.07811, *When Do Markets Fully Process Public Information? Evidence
from Real-Time Prediction Markets*. The abstract reports that a one-minute
change in an out-of-sample benchmark probability is matched by only about a
**0.64-for-one** contemporaneous change in market prices, that the missing
adjustment **predicts drift over the following several minutes**, and that
underreaction is larger when liquidity is low.

What this repository takes from it: the *shape* of the measurement (benchmark
probability vs. observed price move; residual as the tradeable quantity) and
the `0.64` constant as a comparison value. What it does **not** take: any
claim that the constant, the drift horizon or the mechanism transfer to
headline-driven Kalshi/Polymarket macro or sports markets. The paper's setting
has *precisely observed public signals* and a *benchmark probability model*;
this lane has neither on network data.

## Measurement

All prices are YES probabilities. For one signal on one market:

| Symbol | Meaning | Where it comes from |
| --- | --- | --- |
| `p0` | pre-signal mid | fixture / operator file (`pre_signal_mid`); **absent on network** |
| `p1` | current mid | frozen snapshot book (`mid`, else the one available touch) |
| `p_fair` | signal-implied probability | fixture / operator file (`implied_probability`); **never computed here** |

```text
full_move           = p_fair - p0
observed_move       = p1 - p0
reaction_ratio      = observed_move / full_move            # paper: ~0.64
residual_to_fair    = p_fair - p1                          # what full convergence would capture
literature_residual = (1 - literature_beta) * full_move    # what the paper's average implies
target_price        = p1 + convergence_fraction * residual_to_fair
```

Code: `strategies/news_underreaction.py::measure_underreaction`. Every ratio the
inputs allow is computed even when a rail later refuses the signal, so a stale
signal still reports its `reaction_ratio` for the record.

### Rails (first blocking reason wins)

| Reason | Condition |
| --- | --- |
| `market_not_in_snapshot` | signal's venue/market is not in this run's snapshot |
| `market_inactive` | market flagged inactive |
| `no_implied_probability` | `p_fair` is `None` (the mapping is unknown) — checked before staleness because nothing else matters without it |
| `signal_in_future` / `signal_stale` | age outside `[0, max_signal_age_seconds]` (default 900 s) |
| `low_confidence` | `confidence < minimum_confidence` (default 0.5) |
| `empty_book` | no bid and no ask |
| `below_residual_threshold` | `abs(residual_to_fair) < minimum_residual` (default 0.03) |
| `no_edge`, `below_edge_threshold`, `insufficient_touch_depth`, `target_position_reached`, `no_position_headroom`, `touch_at_bound` | inherited from the fair-value engine |
| `risk_*` | `RiskManager` refusal on submit |

### Hypothetical paper trade

`NewsUnderreactionStrategy.evaluate` hands `target_price` to
`CalibratedFairValueStrategy` as a one-market prior. The order therefore uses
the primary track's touch selection, fee buffer (Kalshi 0.02 / Polymarket
0.01), 10-contract cap, target-position stop and `RiskManager` capacity
check, with `minimum_edge` set to `minimum_residual`. Orders are tagged
`strategy=news_underreaction`, `price_signal_status=news_signal_implied_probability`,
plus the signal id, `reaction_ratio`, `residual_to_fair`, `literature_residual`
and the literature reference.

## Signal interface

`research/news_signals.py`

| Source | `mapping` | `implied_probability` | Used when |
| --- | --- | --- | --- |
| `FixtureSignalSource` | `fixture_assigned` / `unmapped` | hand-assigned, **synthetic** | default on fixture runs |
| `NullSignalSource` | — | — | default on network runs → `status = no_signal_source`, 0 candidates |
| `JsonSignalSource` (`--news-signals PATH`) | `operator_assigned` | supplied by the operator | operator owns the mapping |
| `RssHeadlineSource` (`--news-rss URL`, repeatable) | `unmapped` | always `None` | public RSS/Atom, stdlib XML parsing, keyword-overlap match to snapshot market titles; **never trades** |

Sources never raise: fetch/parse failures are recorded on the
`SignalBatch.errors` list and the track reports `signal_source_errors` with
zero candidates. A source that raises anyway is caught by the track runner.

Signals file format (both `observed_at` ISO-8601 and `age_seconds` relative to
the run are accepted):

```json
{"signals": [{
  "signal_id": "op-fed-1", "venue": "kalshi", "market_id": "KXFEDDECISION-26SEP-C25",
  "headline": "…", "observed_at": "2026-09-14T13:02:00+00:00",
  "pre_signal_mid": "0.45", "implied_probability": "0.70", "confidence": "0.9"
}]}
```

## Fixtures

`research/fixtures/news_signals.json` holds eight synthetic signals aligned to
`venues/*/fixtures/markets.json`. They exist to prove the arithmetic and every
rail; the file says so in `_note`. Expected result (asserted in
`tests/test_news_underreaction.py`):

| Signal | Outcome | Why |
| --- | --- | --- |
| `fx-fed-sep-cut-kalshi` | buy 10 YES @ 0.54 | ratio 0.32, residual 0.17 (lit. 0.09), edge 0.14 after fee buffer |
| `fx-fed-sep-cut-polymarket` | buy 10 YES @ 0.43 | ratio 0.18, residual 0.28 |
| `fx-cpi-aug-cool-print` | sell 10 YES @ 0.31 | residual −0.275 |
| `fx-cpi-aug-stale` | `signal_stale` | 3600 s old (ratio still recorded) |
| `fx-nba-injury-unmapped` | `no_implied_probability` | headline with no mapping |
| `fx-nba-lineups-fully-priced` | `below_edge_threshold` | residual 0.03, cost-adjusted 0.01 |
| `fx-fed-rumor-low-confidence` | `low_confidence` | confidence 0.2 |
| `fx-gdp-market-missing` | `market_not_in_snapshot` | market not in fixtures |

Ledger after one fixture run: 3 fills, fees `0.18 + 0.15` (Kalshi only),
marked at mid so unrealized is negative, `equity == 1000 + realized +
unrealized`. Fixture `paper_settlement_outcome` scores all three as hits; that
is a property of the hand-written fixtures, not a result.

## Run it

```bash
python -m research.news_underreaction                       # fixture table + ledger line
python -m research.news_underreaction --json /tmp/news.json # full report incl. not_validated[]
python -m apps.measure_all                                  # all six tracks; lane uses fixtures
python -m apps.measure_all --network --kalshi-env prod      # lane empty: no_signal_source
python -m apps.measure_all --network --kalshi-env prod --news-signals data/news/signals.json
python -m apps.measure_all --network --kalshi-env prod \
  --news-rss https://www.federalreserve.gov/feeds/press_all.xml   # headlines matched, 0 trades
```

Artifacts: the lane is a normal track row (`tracks[]`, `portfolio.by_track`,
`charts.*`), its ledger lands in `artifacts/paper/ledger_news_underreaction.json`,
and `findings.news_underreaction` summarises `status`, signal counts,
mapped/unmapped, fills and the observed-vs-literature reaction ratio with
`mapping_validated: false`. Whenever the lane has fills the artifact carries the
risk flag `news_signal_mapping_unvalidated`.

## Assumption audit

| # | Assumption | Status | Evidence / what would be needed |
| --- | --- | --- | --- |
| N.1 | The residual arithmetic is implemented as specified | **PASS** | `test_underreaction_math_matches_hand_calculation`, `test_negative_residual_points_to_no_side`, `test_partial_convergence_moves_the_target_toward_the_current_mid`, `test_zero_full_move_has_no_ratio_but_still_a_residual`. |
| N.2 | Each rail refuses for the stated reason, in the stated order | **PASS** | `test_rails_report_the_first_blocking_reason` (8 cases); unmapped RSS headlines are refused `no_implied_probability`, not `low_confidence`/`signal_stale`. |
| N.3 | Hypothetical trades go through the same execution and risk path as the primary track | **PASS** | Orders are built by `CalibratedFairValueStrategy` and submitted through `TrackRuntime.submit` → `ExecutionEngine.submit` → `RiskManager.validate_order`; `test_shared_portfolio_stops_re_entry_like_the_primary_track`, `test_carried_ledger_does_not_re_enter`. |
| N.4 | Lane PnL is ledger-backed and isolated from other tracks | **PASS** | Own `PaperLedger(ledger_id="news_underreaction")`; `test_fixture_track_measures_residuals_and_books_paper_fills` asserts the equity identity and 3 fills while the fair-value track keeps 4. |
| N.5 | A network run without a signal source is an honest empty | **PASS** | `NullSignalSource` default; `test_no_signal_source_is_an_honest_empty_lane`; verified on 2026-09-14 against public Kalshi prod + Polymarket: `status=no_signal_source candidates=0 fills=0 equity=1000`. |
| N.6 | Signal-source failures cannot kill the scoreboard | **PASS** | `test_rss_network_failure_is_reported_not_raised`, `test_raising_source_does_not_take_the_scoreboard_down`. |
| N.7 | The RSS hook is free, unauthenticated and never trades | **PASS (mechanism)** | stdlib `xml.etree` + `httpx` GET, no keys; produces only `mapping="unmapped"` signals; `test_rss_hook_matches_headlines_but_never_maps_a_probability`, `test_rss_signals_in_the_track_are_counted_and_refused`. Verified 2026-09-14 with the Federal Reserve press feed: 100 headline→market matches, 0 trades. |
| N.8 | **Signal → implied probability mapping** | **UNKNOWN** | No component computes it. Fixture values are hand-picked; operator files carry whatever the operator believes; RSS gives `None`. Validating requires a benchmark probability model per market family (e.g. Fed-funds-futures-implied cut odds for `KXFEDDECISION`, consensus-vs-print for CPI) and a resolved-market sample large enough to calibrate it. |
| N.9 | `pre_signal_mid` provenance on network | **UNKNOWN** | No price history is captured; a single snapshot has no `p0`. Would need a rolling pre-signal book capture (the paper loop's per-cycle marks are a possible source) or venue candlestick endpoints. Without `p0`, `reaction_ratio` is `None` and only `residual_to_fair` is available. |
| N.10 | The 900 s staleness window is the right drift horizon for news | **UNKNOWN** | Taken from the paper's "following several minutes" in an in-play sports setting. Macro releases and Fed communications may drift over a different horizon; `--max-age-seconds` / `UnderreactionParameters.max_signal_age_seconds` are operator knobs, not calibrated values. |
| N.11 | 0.64 pass-through holds on Kalshi/Polymarket news markets | **UNKNOWN** | Reported for comparison only (`metrics.literature.validated_here = false`). The fixture mean `0.1750` is synthetic and is labelled as such in the artifact. Evidence would be a sample of timestamped signals with `p0`, `p1`, `p_fair` on live markets. |
| N.12 | Full convergence (`convergence_fraction = 1`) is the right target | **UNKNOWN / conservative option exists** | The paper predicts drift toward the benchmark, not necessarily all of it within the window. Lowering `convergence_fraction` shrinks the target toward `p1`; no value has been calibrated. |
| N.13 | Fixture hit rate 1.0 says anything about edge | **FAIL as evidence** | The fixtures were written so the trades are consistent with their `paper_settlement_outcome`. It proves the scoring plumbing, nothing else. |
| N.14 | RSS keyword matching finds the right market | **UNKNOWN / weak** | ≥2 shared non-stopword tokens between headline and market title. On the Fed feed it matched one headline to every `KXFEDDECISION-*` strike; a real mapping would need market-family aware parsing. Acceptable for v1 because matches never trade. |

## Not in v1 (deliberately)

* No paid or keyed news API. `SignalSource` is the extension point.
* No probability model of any kind; see N.8.
* No pre-signal price capture; see N.9.
* No live or demo-authenticated path; the lane inherits every rail from
  `docs/ASSUMPTIONS.md` §3.
