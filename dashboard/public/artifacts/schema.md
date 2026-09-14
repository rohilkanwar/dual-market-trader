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
| `schema_version` | string | Semver for this document shape (currently `1.2.0`; `1.1.0` readers remain compatible) |
| `meta` | object | Provenance, paper-only flag, timestamps |
| `findings` | object | ArbAI / settlement research headlines |
| `totals` | object | Aggregate KPI strip |
| `tracks` | Track[] | Per-track comparison rows |
| `top_fills` | FillRow[] | Ranked paper fills |
| `top_edges` | EdgeRow[] | Ranked edges (filled or not) |
| `portfolio` | object | Paper portfolio & risk summary |
| `charts` | object | Simple series for SVG/CSS bar charts |

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
| `note` | string | Sample files only: explains that the numbers are placeholders |

## `findings`

Captures research headlines that explain empty cross-venue panels:

- `fed_exact_divergences`: `{ observed, sample_size, label, note }` — e.g. Fed EXACT 0/3
- `macro_admitted_bucket_divergences`: `{ observed, sample_size, label, note }` — e.g. macro 0/20
- `live_network_cross_venue_candidates`: number (often `0`)
- `arbai_summary`: short educational string for empty states

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
| `track` | string | Stable id: `gated_cross_venue_macro`, `ungated_cross_venue_macro`, `single_venue_fair_value`, `sports_cross_venue`, `small_deliberate_bet` |
| `label` | string | Display name |
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
| `metrics` | object | Track-specific extras (venue breakdown, host conflicts, …) |

## `top_fills[]` / `top_edges[]`

Fills: `rank`, `track`, `venue`, `market`, `side`, `outcome`, `qty`, `price`, `edge_bps`, `paper_pnl`, `filled_at`.

Edges: `rank`, `track`, `venue`, `market`, `edge_bps`, `admitted`, `filled`, `fair_value`, `mid`.

## `portfolio`

`open_positions`, `gross_notional`, `net_exposure`, `realized_pnl`, `unrealized_pnl`, `max_drawdown`, `settlement_risk_pairs`, `concentration[]`, `risk_flags[]`.

Ledger-backed (1.2.0) additions: `source: "ledger_aggregate"`, `starting_cash`, `cash`, `equity`, `total_pnl`, `fees_paid`, `primary_track`, `primary` (the primary track's full `PaperLedger.summary()` including `positions[]`), and `by_track` (per-track cash/equity/PnL/drawdown). `max_drawdown` is the maximum across track ledgers because tracks are independent books. `risk_flags` always includes `pnl_from_ledger_not_placeholder` on generated artifacts.

## `findings`

`divergence_findings_status` is `not_measured_in_this_run` unless `--harvest-dir` pointed at harvested resolved markets, in which case `macro_admitted_bucket_divergences` (and `fed_exact_divergences` when Fed data exists) are computed by `research/harvest_scoreboard.py`. Counts are never defaulted.

## How to refresh (measure → sync → deploy)

Paper only. Do not enable live trading.

```bash
# From repo root — network measurement writes runtime artifacts/
uv run python -m apps.measure_all --network --kalshi-env prod --harvest-dir data/harvests
uv run python -m apps.paper_loop --once

# Copy runtime JSON into the dashboard public tree
cd dashboard
npm run sync-artifacts   # copies scoreboard_*.json, paper_loop_latest.json and the primary ledger

# Commit snapshots for static Vercel (parent / operator)
git add public/artifacts
git commit -m "Refresh public scoreboard snapshots"
git push origin main
```

The Python writers already emit `scoreboard_latest.json` with `meta.source="measured"`; the sync script refuses to copy any file whose `meta.source` is `sample`.

Vercel root directory: `dashboard`. Build: `npm run build`. Output: `dist`. Keep `vercel.json` SPA rewrites.
