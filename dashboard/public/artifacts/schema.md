# Scoreboard artifact schema

Versioned JSON consumed by the static Vercel dashboard under `public/artifacts/`.

## Loader preference

The React app tries these URLs in order and uses the first successful JSON response:

1. `/artifacts/scoreboard_latest.json` — synced from a fresh `measure_all` / paper-loop run
2. `/artifacts/scoreboard_network.json` — committed network snapshot (may be a SAMPLE)
3. `/artifacts/scoreboard_sample.json` — explicit sample fallback

## Root object

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | string | Semver for this document shape (currently `1.3.0`; `1.1.0`/`1.2.0` readers remain compatible) |
| `meta` | object | Provenance, paper-only flag, timestamps |
| `findings` | object | ArbAI / settlement research headlines |
| `totals` | object | Aggregate KPI strip |
| `tracks` | Track[] | Per-track comparison rows |
| `top_fills` | FillRow[] | Ranked paper fills |
| `top_edges` | EdgeRow[] | Ranked edges (filled or not) |
| `portfolio` | object | Paper portfolio & risk summary |
| `charts` | object | Simple series for SVG/CSS bar charts |
| `gate_report` | `{ file, totals }` | 1.3.0: pointer to the per-pair `gate_report_<mode>.json` written with this run plus its `totals` (a `GateSummary`, below) |

## `meta`

| Field | Type | Notes |
| --- | --- | --- |
| `source` | `"sample"` \| `"measured"` \| `"synced"` | UI shows SAMPLE banner when `sample`. The Python writer refuses to emit `sample`; only hand-written files carry it |
| `label` | string | Human banner, e.g. `SAMPLE / LAST RUN SNAPSHOT` |
| `paper_only` | boolean | Always `true` for public artifacts |
| `mode` | `"network"` \| `"fixtures"` | Measurement mode |
| `measured_at` | ISO-8601 | When the paper measurement finished |
| `generated_at` | ISO-8601 | When this JSON was written |
| `venues` | string[] | Usually `["kalshi","polymarket"]` |
| `markets_per_venue` | number | Cap / sample size per venue |
| `primary_track` | string | Typically `single_venue_fair_value` |
| `refresh` | string | Operator hint for regenerating |
| `pnl_source` | string | `core.ledger.PaperLedger` on every generated artifact. The UI shows PnL only when present |
| `venue_focus` | string | `kalshi` |
| `kalshi_env` | `"demo"` \| `"prod"` \| null | Public API host used for network reads |
| `run_id`, `cycle` | string, number \| null | Run identity; `cycle` set by the paper loop |
| `track_family` | string \| null | `polymarket_arb` on boards written by `apps.measure_polymarket_arb`; absent/null on the full board |
| `note` | string | Sample files only: explains that the numbers are placeholders |

## `findings`

Captures research headlines that explain empty cross-venue panels:

- `fed_exact_divergences`: `{ observed, sample_size, label, note }` — e.g. Fed EXACT 0/3
- `macro_admitted_bucket_divergences`: `{ observed, sample_size, label, note }` — e.g. macro 0/20
- `live_network_cross_venue_candidates`: number (often `0`); from 1.3.0 this is the
  `gated_cross_venue` candidate count (every matched pair, all categories)
- `arbai_summary`: short educational string for empty states
- `news_underreaction`: `{ status, signal_source, signals, mapped, unmapped, paper_fills, reaction_ratio_observed_mean, reaction_ratio_literature, literature_reference, mapping_validated: false, note }`. `status` is one of `no_signal_source`, `fixture_synthetic`, `signal_source_errors`, `signals_unmapped`, `no_signals_matched`, `operator_mapped_signals`. Present on every generated artifact; see `docs/NEWS_UNDERREACTION.md`.
- `gated_cross_venue` (1.3.0): a `GateSummary` — `candidates`, `gate_admitted` (passed all
  eight gate stages), `gate_refused`, `priced_but_no_edge`, `traded`, `paper_fills`,
  `primary_reject_reasons`, `all_stage_reject_reasons`, `policy`, `status`
  (`zero_admits_expected` | `admits_present_verify_fingerprints`)

## `totals`

| Field | Type |
| --- | --- |
| `candidates` | number |
| `admitted` | number |
| `rejects` | number |
| `paper_fills` | number |
| `fill_rate` | number \| null (0–1) |
| `settlement_risk_pairs` | number |
| `paper_pnl` | number | Sum of every track ledger's realized + unrealized PnL (USD, fees deducted) |
| `realized_pnl`, `unrealized_pnl`, `fees_paid` | number | Ledger aggregates (1.2.0) |
| `proposed_orders` | number | Denominator of `fill_rate` |
| `avg_edge_bps` | number \| null |

## `tracks[]`

| Field | Type | Notes |
| --- | --- | --- |
| `track` | string | Stable id: `gated_cross_venue` (1.3.0, settlement-safe), `gated_cross_venue_macro`, `ungated_cross_venue_macro` (control), `single_venue_fair_value`, `sports_cross_venue`, `small_deliberate_bet`, `news_underreaction` (optional lane; empty on network without a signal source), `polymarket_rebalancing_arb`, `polymarket_negrisk_arb`, `polymarket_combinatorial_arb`, `kalshi_longshot_fade`, `kalshi_maker_quote` (Kalshi FLB lane). Grouping: see [Track ids and families](#track-ids-and-families) |
| `label` | string | Display name |
| `family` | string | Optional. Strategy family id from the registry below; when absent the index builder derives it from the track id |
| `candidates` | number | |
| `admitted` | number | |
| `rejects` | number | |
| `paper_fills` | number | |
| `fill_rate` | number \| null | `fills / admitted` when admitted > 0 |
| `edge_bps` | number \| null | Average admitted edge in basis points |
| `settlement_risk` / `settlement_risk_flag` | boolean | Either key accepted; UI normalizes |
| `proposed_orders` | number | |
| `estimated_fees_buffer` | number | |
| `notes` | string | Operator-facing explanation |
| `reject_reasons` | Record<string, number> | Histogram |
| `metrics` | object | Track-specific extras (venue breakdown, host conflicts, …). Polymarket arb tracks add `mirror_consistent` / `mirror_inconsistent`, `top_of_book_ask_sum`, `groups[]` (per-event sums, gross / fee / slippage per set, reason), `conversions[]`, `holdings[]`, `locked_capital`, `parameters` |

## Track ids and families

Track ids are stable snake_case strings chosen by the Python scoreboard
(`research/scoreboard.py` `TRACKS` / `TRACK_LABELS`). The dashboard groups them into
**families** (strategy lanes) using `dashboard/scripts/track-families.mjs`; that file is
the only place the mapping lives. Nothing else in the UI needs to know a track id.

| Family id | Lane label | Pinned lane | Tracks emitted today | Matches new ids containing |
| --- | --- | --- | --- | --- |
| `negrisk` | NegRisk | yes | `polymarket_rebalancing_arb`, `polymarket_negrisk_arb`, `polymarket_combinatorial_arb` | `negrisk`, `neg_risk`, `combinatorial`, `combo` |
| `kalshi_flb` | Kalshi FLB | yes | `kalshi_longshot_fade`, `kalshi_maker_quote` | `flb`, `maker`, `longshot` |
| `xv_gated` | XV gated | yes | `gated_cross_venue_macro`, `small_deliberate_bet` | `gated` + (`cross_venue` \| `xv`) |
| `xv_ungated` | XV ungated | no | `ungated_cross_venue_macro`, `sports_cross_venue` | `ungated` + (`cross_venue` \| `xv`) |
| `cross_venue` | Cross-venue | no | — | `cross_venue` \| `xv` without gated/ungated |
| `single_venue` | Single venue | no | `single_venue_fair_value` | `single_venue`, `fair_value` |
| `news` | News | no | `news_underreaction` | `news`, `underreaction`, `headline` |
| `other` | Other | no | — | anything else |

Resolution order for a track row: explicit `family` (or `metrics.family` /
`metrics.track_family`) naming a registered family → the known-id table → keyword match
on the id → `other`. An unknown id therefore never breaks the index; it lands in `other`
until either the id is added to `KNOWN_TRACKS` or the artifact declares its family.

Conventions for the parallel tracks (ids are the sister branches' choice; these are the
patterns the matcher already recognises, not a claim that any of them has run):

- Polymarket NegRisk / combinatorial / rebalancing: the three `polymarket_*_arb` ids above (registered in `KNOWN_TRACKS`)
- Kalshi maker / FLB: `kalshi_longshot_fade`, `kalshi_maker_quote` (registered in `KNOWN_TRACKS`), `kalshi_maker_flb`, `kalshi_flb`, …
- Gated cross-venue variants: `gated_cross_venue_<scope>`

To add a track to a lane explicitly, either extend `KNOWN_TRACKS` in
`track-families.mjs` or emit `"family": "<family id>"` on the track row (the sync script
copies it into the run record). The three pinned lanes render even when they have no
artifacts yet, with the copy "Not measured yet".

## `top_fills[]` / `top_edges[]`

Fills: `rank`, `track`, `venue`, `market`, `side`, `outcome`, `qty`, `price`, `edge_bps`, `paper_pnl`, `filled_at`.

Edges: `rank`, `track`, `venue`, `market`, `edge_bps`, `admitted`, `filled`, `fair_value`, `mid`.

## `portfolio`

`open_positions`, `gross_notional`, `net_exposure`, `realized_pnl`, `unrealized_pnl`, `max_drawdown`, `settlement_risk_pairs`, `concentration[]`, `risk_flags[]`.

`risk_flags` may include `combinatorial_positions_marked_at_mid_not_resolution` when the buy-all-YES track holds positions (their payoff arrives at resolution, the board shows the mid-mark).

Ledger-backed (1.2.0) additions: `source: "ledger_aggregate"`, `starting_cash`, `cash`, `equity`, `total_pnl`, `fees_paid`, `primary_track`, `primary` (the primary track's full `PaperLedger.summary()` including `positions[]`), and `by_track` (per-track cash/equity/PnL/drawdown). `max_drawdown` is the maximum across track ledgers because tracks are independent books. `risk_flags` always includes `pnl_from_ledger_not_placeholder` on generated artifacts. `news_signal_mapping_unvalidated` is added whenever the `news_underreaction` lane booked a paper fill, because its signal→probability mapping is not validated.

## `findings`

`divergence_findings_status` is `not_measured_in_this_run` unless `--harvest-dir` pointed at harvested resolved markets, in which case `macro_admitted_bucket_divergences` (and `fed_exact_divergences` when Fed data exists) are computed by `research/harvest_scoreboard.py`. Counts are never defaulted.

## `gate_report_<mode>.json` — per-pair admissibility (schema `1.0.0`)

Written by `apps.measure_all` / the paper loop on **every** run next to the scoreboard,
also as `gate_report_latest.json`. It is the measured artifact of the settlement-safe
`gated_cross_venue` track and is emitted even when there are zero candidates or zero
admits: an empty `pairs[]` with a populated `totals` block is the finding.

| Field | Type | Notes |
| --- | --- | --- |
| `kind` | `"gate_report"` | |
| `meta` | object | `source` (`measured`/`synced`, never `sample`), `paper_only`, `mode`, `measured_at`, `run_id`, `track`, `control_track`, `policy` (name, `minimum_heuristic_confidence`, `allowed_host_tiers`, `require_same_publisher`, `expiry_tolerance_hours`), `parameters` (depth-aware sizing), `fee_model` per venue, `stage_order`, `pnl_source` |
| `totals` | GateSummary | Same shape as `findings.gated_cross_venue`, plus `expected_locked_pnl_if_settlement_equivalent`, `ledger_total_pnl`, `vetoed_candidates` |
| `stage_failures` | Record<stage, number> | How many pairs failed each stage (a pair can fail several) |
| `vetoed_candidates[]` | `{ kalshi, polymarket, confidence, reason }` | Heuristic candidates the matcher refused for `period_disagrees` / `threshold_disagrees` |
| `pairs[]` | PairVerdict[] | One per matched pair, sorted by `pair_id` |
| `control` | object \| null | `ungated_cross_venue_macro` counts for the same snapshot: `traded`, `paper_fills`, `settlement_risk_flag`, `gate_would_have_refused_traded_pairs`, `ledger_total_pnl` (not realisable) |

`pairs[]` entries: `pair_id`, `kalshi`, `polymarket` (market ids), titles, `category`,
`admitted`, `reason` (first failure in stage order, or `admitted`), `reasons[]` (**every**
failing stage), `checks[]` (`stage`, `passed`, `reason`, `details` for all eight stages:
`match`, `clauses`, `fingerprint`, `polarity`, `fed_bucket`, `interval`, `hosts`, `expiry`),
`stages_passed[]`, `stages_failed[]`, the legacy detail keys (`clause_verdict`,
`fingerprint_relation`, `kalshi_host_tier`, `polymarket_host_tier`, `host_conflict`, …) and,
only for admitted pairs, `edge` (depth-aware pricing: `reason`, `quantity`, `net_edge`,
`fees_per_contract`, `paper_edge.yes_leg/no_leg` with the consumed levels).

`npm run sync-artifacts` copies `gate_report_{latest,network,fixtures}.json` when present
(never a `sample`), and the experiments index joins each report to its run by `meta.run_id`
(`runs[].gate`, `runs[].gate_report`) and lists them under `gate_reports[]`.

## `scoreboard_flb.json` and `flb_report_latest.json` — Kalshi FLB run

`apps.measure_flb` writes a normal scoreboard artifact for the two FLB tracks only
(`meta.kind = "kalshi_flb"`, `meta.venues = ["kalshi"]`, `meta.primary_track =
"kalshi_maker_quote"`, `meta.label` ends in `/ KALSHI FLB`) whose `findings.kalshi_flb`
carries the verdict map and headline, plus the full report:

| Field | Notes |
| --- | --- |
| `kind` | `kalshi_flb_report`; `paper_only: true`; `pnl_source: core.ledger.PaperLedger` |
| `headline`, `verdict_table[]` | `{scope: snapshot \| ex_post, check, verdict: PASS \| FAIL \| INSUFFICIENT_DATA \| NOT_IDENTIFIABLE, detail}` |
| `snapshot` | `band_table` by YES price band (`<10c` … `>=90c`: markets, spread, taker fee and take cost as a fraction of price, depth), `event_overround` for mutually exclusive events, `verdicts` |
| `tracks` | per track: counts, refusals, ledger totals, `longshot_bands`, `paper_pnl_by_longshot_band`, `paper_pnl_by_price_paid_band`, `shadow_longshot_buyer` (fade track only) |
| `ex_post` | `status` (`measured_from_settled_trades` \| `not_measured`), `band_table` by taker purchase price (taker/maker gross and net per contract, `taker_roi`, `maker_roi_net`, market-clustered `t_stat_taker_gross`, `effective_n_markets`, equal-weighted mean and t), `band_table_excluding_final_minutes`, `by_category`, `verdicts` |
| `assumptions` | fee model, fill probability, queue, adverse selection, marks, sizing, longshot definition, snapshot and ex-post limits |

`flb_identifiable_from_snapshot` is `NOT_IDENTIFIABLE` on every non-empty snapshot by
design: prices without outcomes cannot establish favorite–longshot bias. The synced
`paper_ledger_kalshi_longshot_fade.json` / `paper_ledger_kalshi_maker_quote.json` are the
two FLB ledgers. Run records from `measure_flb` carry `run_kind: "kalshi_flb"`,
`primary_track: "kalshi_maker_quote"` and the `flb` verdict map. See `docs/FLB_RUNBOOK.md`.

## `experiments_index.json` — run history

Generated by `scripts/build-experiments-index.mjs` (runs on `npm run build` via `prebuild`
and at the end of `npm run sync-artifacts`). It lists **only** runs that exist as files in
this directory; nothing is measured or inferred. Rewritten only when its content changes.

Inputs, in this directory:

- `scoreboard_*.json` — one run each, deduped by `meta.run_id` (`scoreboard_latest.json`
  and `scoreboard_network.json` usually describe the same run; the entry keeps both
  filenames and points `detail` at the mode-specific file). `scoreboard_polymarket_arb.json`
  is the arb-only board (`meta.track_family = "polymarket_arb"`, primary track
  `polymarket_negrisk_arb`) and is its own run.
- `polymarket_arb_latest.json` — opportunity report for the last arb run (not indexed as a run;
  linked from `manifest.json`). Fields: `snapshot`, `parameters`, `rebalancing` (mirror
  statistics, `opportunities[]`, `executions[]`), `negrisk` (`groups[]`, `conversions[]`),
  `combinatorial` (`groups[]`, `holdings[]`, `locked_capital`, `lockup_until`), `ledgers`.
- `runs/<run_id>.json` — compact run records written by the sync script (below).
- `paper_ledger_*.json` — ledger snapshots, listed under `ledgers[]` (they carry no run id).

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | string | `1.2.0`. Additive over `1.0.0`: `families`, `lanes`, `track.family`, `run.families`, `ledger.track/family` (1.1.0); `run.gate`, `run.gate_report`, `gate_reports[]` (1.2.0) |
| `counts` | `{ total, measured, sample, backtest }` | `measured` = everything that is not a sample |
| `modes` | Record<string, number> | Runs per `meta.mode` |
| `latest_run_id` | string \| null | Run id found in `scoreboard_latest.json` |
| `families[]` | TrackFamilySummary[] | Every registered family, in registry order, including those with zero runs |
| `lanes[]` | LaneSummary[] | The pinned lanes (`negrisk`, `kalshi_flb`, `xv_gated`) |
| `runs[]` | ExperimentEntry[] | Newest `measured_at` first |
| `ledgers[]` | LedgerSnapshotEntry[] | `ledger_id`, `track`, `family`, `updated_at`, `fills`, `equity`, `total_pnl`, … |

`runs[]` entries: `run_id`, `kind` (`paper_run` \| `sample` \| `backtest`), `source`, `label`,
`mode`, `cycle`, `measured_at`, `venues`, `venue_focus`, `kalshi_env`, `primary_track`, `track_family`,
`pnl_source`, `totals` (`candidates`, `admitted`, `rejects`, `paper_fills`, `paper_pnl`,
`realized_pnl`, `unrealized_pnl`, `fees_paid`), `tracks[]` (`track`, `label`, `family`,
`candidates`, `admitted`, `paper_fills`, `edge_bps`, `settlement_risk`, `paper_pnl`),
`families[]` (distinct families in `tracks`), `gate` (GateSummary \| null; index schema
1.2.0), `gate_report` (URL, when the report is on disk), `artifacts[]`, `detail` (URL of the
richest file), `is_latest`.

`families[]` entries: `id`, `label`, `description`, `lane` (pinned in the strip), `tracks[]`
(ids seen under the family across all runs), `runs`, `measured_runs`, `sample_runs`.

`lanes[]` entries: `family`, `label`, `description`, `status`, `run_id`, `measured_at`,
`mode`, `pnl_source`, `tracks[]`, `candidates`, `admitted`, `paper_fills`, `paper_pnl`,
`detail`. A lane is the **newest measured run that carries at least one track of the
family**, reduced to that family's tracks. `status` is:

- `measured` — numbers are that run's per-family sums; `paper_pnl` is `null` unless the run
  has `pnl_source` and every family track carries a ledger PnL.
- `sample_only` — only sample files carry the family; every number is `null`.
- `missing` — no file in this directory has a track in the family; every number is `null`.
  This is the state of Kalshi FLB until its branch merges and a run is synced.

Honesty rules baked into the builder:

- `kind` is `backtest` only when the artifact itself says `meta.kind: "backtest"` or
  `meta.backtest: true`. Nothing in the repo emits that yet, so the UI says "Experiments".
- Sample files (`meta.source: "sample"`) are indexed as `kind: "sample"` with every PnL
  field set to `null`; the UI shows a SAMPLE pill and `—` for PnL.
- PnL is copied only when `meta.pnl_source` is present (ledger-backed artifacts).
- Lanes and family filters only ever sum numbers already present in a run; there is no
  backfill, interpolation or backtest.
- A track row without a string `track` id is dropped with a warning; the rest of the run
  is still indexed.

### How a new track appears

1. A branch adds the track to `TRACKS` / `TRACK_LABELS` in `research/scoreboard.py` and it
   is measured by `measure_all` (or the paper loop). The run writes
   `artifacts/scoreboard_<mode>.json`, `artifacts/scoreboard_latest.json`,
   `artifacts/paper/ledger_<track>.json` and `artifacts/paper/runs/<run_id>.json`.
2. `cd dashboard && npm run sync-artifacts` discovers every `scoreboard_*.json` and
   `paper/ledger_*.json` (no file list to edit), writes `runs/<run_id>.json` and rebuilds
   this index. The new track row is stamped with a `family` by `track-families.mjs`.
3. In the UI the track shows up inside its run's detail table under its family heading;
   the family appears as a filter pill; if the family is a pinned lane its tile switches
   from "Not measured yet" to fills / paper PnL / date. If the id matched nothing it shows
   under **Other** — add it to `KNOWN_TRACKS` or emit `family` on the row to place it.
4. Commit `public/artifacts` (CI fails if the committed index is stale) and push.

## `runs/<run_id>.json` — compact run record

Written by `npm run sync-artifacts` from `../artifacts/paper/runs/<run_id>.json`, joined
with `../artifacts/paper_loop_history.jsonl` for `cycle` / `completed_at` when the run came
from the paper loop. Per-fill and per-edge rows are dropped; per-track counts and the
`PaperLedger.summary()` totals are kept, so the record stays ~2 KB. Fields: `run_id`,
`mode`, `measured_at`, `completed_at`, `cycle`, `duration_seconds`, `primary_track`,
`pnl_source`, `totals`, `gate` (GateSummary of `gated_cross_venue`, read from the track's
own metrics so it survives the next run overwriting `gate_report_latest.json`), `tracks[]`
(with a `ledger` block: `equity`, `realized_pnl`,
`unrealized_pnl`, `total_pnl`, `fees_paid`, `fills`, `open_positions`, `max_drawdown`, and
`family` when the manifest row declared one), `derived_from[]`, plus `label`, `venues`,
`venue_focus`, `kalshi_env`, `track_family` copied from the run manifest. `primary_track` is
taken from the manifest, then the loop history row, then `single_venue_fair_value`. The
newest 200 manifests are kept (`MAX_RUN_RECORDS`).

These records are what keeps a run visible in the history after the next run overwrites
`scoreboard_latest.json` / `scoreboard_<mode>.json`.

## How to refresh (measure → sync → deploy)

Paper only. Do not enable live trading.

```bash
# From repo root — network measurement writes runtime artifacts/
uv run python -m apps.measure_all --network --kalshi-env prod --harvest-dir data/harvests
uv run python -m apps.measure_polymarket_arb --network --limit 40   # arb-only board + report
uv run python -m apps.paper_loop --once
uv run python -m apps.measure_flb --network --kalshi-env prod --harvest-trades   # Kalshi FLB report

# Copy runtime JSON into the dashboard public tree
cd dashboard
npm run sync-artifacts   # copies every scoreboard_*.json, polymarket_arb_latest.json,
                         # paper_loop_latest.json and every paper/ledger_<track>.json, writes
                         # runs/<run_id>.json records and rebuilds experiments_index.json
                         # (families + lanes included)

# Commit snapshots for static Vercel (parent / operator)
git add public/artifacts
git commit -m "Refresh public scoreboard snapshots"
git push origin main
```

The Python writers already emit `scoreboard_latest.json` with `meta.source="measured"`; the sync script refuses to copy any file whose `meta.source` is `sample`.

Vercel root directory: `dashboard`. Build: `npm run build`. Output: `dist`. Keep `vercel.json` SPA rewrites.
