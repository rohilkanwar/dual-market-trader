# dual-market-trader

Paper-only measurement scaffold for Kalshi and Polymarket prediction markets
with a mark-to-market **paper PnL ledger**. Nothing in this repository places a
live order: venue adapters only read public market data, paper fills are
simulated locally against the observed book, and every PnL number on the
dashboard is read from the ledger.

Venue focus is **Kalshi single-venue fair value** (the primary track). The
cross-venue tracks exist to *measure* how often settlement rules diverge, not
to trade them. Three **Polymarket intra-venue arbitrage** tracks (YES+NO
rebalancing, NegRisk buy-all-NO + convert, buy-all-YES sum-to-one) measure the
structures described in arXiv:2508.03474 and arXiv:2608.00666 against live
public books; see `docs/POLYMARKET_ARB.md`. `docs/ASSUMPTIONS.md` records what
has been validated.
`gated_cross_venue` is the clause-matched, settlement-safe cross-venue track:
a pair is priced only after passing eight fail-closed gate stages, and the
per-pair verdicts are written to `gate_report_<mode>.json` on every run
(`docs/RUNBOOK_gated_cross_venue.md`).

## Quick start

```bash
# Python 3.12+. uv is preferred; plain pip works too.
uv sync --extra dev                      # or: python -m pip install -e ".[dev]"
uv run pytest -q                         # or: python -m pytest -q

# One paper cycle on committed fixtures (deterministic, no network)
uv run python -m apps.paper_loop --once --artifact-dir artifacts

# One read-only network canary against public Kalshi macro series + Polymarket
uv run python -m apps.measure_all --network --limit 15 --kalshi-env prod --artifact-dir artifacts

# Only the Polymarket arbitrage tracks (top 40 events by liquidity, public Gamma/CLOB reads)
uv run python -m apps.measure_polymarket_arb --network --limit 40 --artifact-dir artifacts
```

`TRADING_MODE=paper` and `ENABLE_LIVE_TRADING=false` are the defaults. Every
entry point refuses to start if either variable requests live operation.

### Where artifacts land

| Path | Contents |
| --- | --- |
| `artifacts/scoreboard_latest.json` | Dashboard artifact (schema 1.3.0, `meta.source=measured`, `meta.pnl_source=core.ledger.PaperLedger`) |
| `artifacts/scoreboard_<fixtures\|network>.json` | Same document, kept per mode |
| `artifacts/gate_report_<fixtures\|network>.json`, `gate_report_latest.json` | Per-pair admissibility verdicts of `gated_cross_venue` (every stage, every reason, depth-aware edge for admitted pairs); emitted even with zero candidates |
| `artifacts/scoreboard_polymarket_arb.json` | Arb-only board from `apps.measure_polymarket_arb` (`meta.track_family=polymarket_arb`); does not replace `scoreboard_latest.json` |
| `artifacts/polymarket_arb_latest.json` | Full arb opportunity report: per-group sums, fee/slippage per set, mirror statistics, conversions, holdings |
| `artifacts/paper/ledger_<track>.json` | Full ledger per track (incl. `news_underreaction` and the `polymarket_*_arb` tracks): cash, fills (with fees), marks, positions, equity curve, max drawdown |
| `artifacts/paper/equity_curve_<track>.jsonl` | One appended equity point per run/cycle |
| `artifacts/paper/runs/<run_id>.json` | Raw track summaries for the run |
| `artifacts/paper_loop_latest.json`, `paper_loop_history.jsonl` | Paper-loop cycle payloads (append-only history) |
| `artifacts/flb_report_latest.json`, `scoreboard_flb.json` | Kalshi favorite–longshot bias report + scoreboard from `apps.measure_flb` (see `docs/FLB_RUNBOOK.md`) |

`artifacts/` is git-ignored. The dashboard's committed copies live in
`dashboard/public/artifacts/` and are refreshed with `npm run sync-artifacts`.

### Paper PnL accounting

`core/ledger.py` keeps one `PaperLedger` per track:

* fills move cash by `-(signed qty) * yes_price - fee`; a NO buy is a short YES
* positions are marked at the book mid (or best bid/ask with
  `mark_method="conservative"`); unmarked positions are valued at cost and
  counted in `unmarked_positions` rather than hidden
* realized PnL uses average cost (fees deducted immediately, so the daily-loss
  rail sees them); unrealized is `qty * (mark - average_price)`
* invariant checked by tests: `equity == starting_cash + realized + unrealized`
* Kalshi fills carry the published fee formula `ceil(0.07 * C * P * (1-P))`
  (`--no-fees` disables it); Polymarket fills carry `C * rate * P * (1-P)` with
  the rate taken from the market's Gamma `feeType` (fee-free when the venue
  charges none; fixture markets without a rate stay fee-free)
* `settle(venue, market, outcome)` closes a position at 1/0;
  `close_position(..., order_id="negrisk_convert")` books the NegRisk
  NO-set → collateral conversion explicitly

The paper loop reloads `artifacts/paper/ledger_<track>.json` before each cycle,
so cash, positions and drawdown carry across cycles. Strategies stop adding to a
market once the target size is held (`target_position_reached`); use
`--no-persist` for independent cycles or `measure_all --reset-ledgers`.

### Fair values for the Kalshi canary

The fair-value strategy trades only markets with an explicit prior. Committed
defaults cover the fixture markets only, so a network run reports
`no_fair_value` for every market and **0.00 PnL** until you supply priors:

```bash
cat > data/priors/kalshi.json <<'EOF'
{"KXFEDDECISION-26SEP-C25": "0.62"}
EOF
uv run python -m apps.measure_all --network --kalshi-env prod --priors data/priors/kalshi.json
```

Priors are operator inputs, not model output; the repository deliberately
contains no implicit pricing model.

### News / underreaction lane (optional sixth track)

`news_underreaction` measures how far a market has moved toward a public
signal's implied probability and books a hypothetical paper trade on the
residual (literature anchor: arXiv:2606.07811, ~0.64 pass-through, drift over
minutes). The signal→probability mapping is **not** computed here: fixture
signals are synthetic, network runs stay empty unless an operator supplies
signals, and the optional RSS headline hook matches but never maps, so it
never trades.

```bash
python -m research.news_underreaction                 # runnable fixture measurement
python -m apps.measure_all --network --kalshi-env prod --news-signals data/news/signals.json
python -m apps.measure_all --network --kalshi-env prod --news-rss https://www.federalreserve.gov/feeds/press_all.xml
```

`docs/NEWS_UNDERREACTION.md` lists what is validated (arithmetic, rails,
ledger isolation, graceful empty) and what is not (the mapping, pre-signal
price provenance, the drift window, the 0.64 constant on these venues).

### Kalshi favorite–longshot bias (FLB) track

Two paper tracks fade the longshot side (<20¢) of every qualifying Kalshi market —
`kalshi_longshot_fade` takes the favourite at the touch, `kalshi_maker_quote` rests
one tick inside the spread with documented fill-probability assumptions — with paper
caps $25/order, $75/market, $75 daily loss, $1,000 collateral, and Kalshi's fee
schedule (taker `M·0.07·P·(1−P)`, maker `M·0.0175·P·(1−P)`). `apps.measure_flb`
adds a snapshot band table and, from the public settled-trade tape (`taker_side`
per trade), ex-post maker-vs-taker returns by price band with explicit
PASS/FAIL/NOT_IDENTIFIABLE verdicts:

```bash
uv run python -m apps.measure_flb                                   # fixtures, no network
uv run python -m apps.measure_flb --network --kalshi-env prod --harvest-trades   # snapshot + settled trades
```

A snapshot cannot identify FLB (prices without outcomes), and the report says so;
ex-post verdicts need settlement outcomes, which the harvest provides. Measured
results, assumptions and limits: `docs/FLB_RUNBOOK.md`.

### Self-logged order-book archive ($0 path)

No paid L2 vendor: `apps.book_logger` polls the public Kalshi endpoints (and,
opt-in, Polymarket's batched CLOB `/books`) on an interval and appends
point-in-time books plus the incremental public trade tape as JSONL under
`artifacts/books/<venue>/<day>/` (git-ignored; `--gzip` and `--to-parquet`
available). Rate-limited process-wide (`--max-rps`, default 4) with `Retry-After`
aware backoff; paper-only guard; a session file tracks counters and errors.

```bash
uv run python -m apps.book_logger --once                                  # one Kalshi macro snapshot
uv run python -m apps.book_logger --interval 30 --duration 3600 --gzip    # an hour of books + trades
uv run python -m apps.book_logger --polymarket --skip-unchanged           # add Polymarket, write only changes
```

The archive is aggregated L2 at poll instants: **no order ids, queue position,
cancels or intra-poll changes (no L3 / FIFO)**. `docs/BOOK_LOGGER.md` covers
scheduling (tmux / systemd / cron), storage, the record schema and the full
list of what is not captured.

### Other entry points

```bash
uv run python -m apps.measure_all --help            # one-shot measurement, all 12 tracks, all flags
uv run python -m apps.book_logger --help            # self-log public books/trades to artifacts/books (see docs/BOOK_LOGGER.md)
uv run python -m apps.measure_polymarket_arb --help # Polymarket arb tracks only (see docs/POLYMARKET_ARB.md)
uv run python -m apps.measure_flb --help            # Kalshi FLB tracks + ex-post band table (see docs/FLB_RUNBOOK.md)
uv run python -m apps.paper_runner --strategy both  # strategy runner with logging event sink
uv run python -m research.compare_markets --network # top markets per venue
uv run python -m research.cross_venue_edges         # matched pairs and executable edges
uv run python -m research.harvest_public            # harvest resolved macro markets (public data)
uv run python -m research.news_underreaction       # news lane fixture measurement (--json for the report)
uv run python -m apps.measure_all --harvest-dir data/harvests   # adds divergence findings when present
uv run uvicorn apps.dashboard_api:app --port 8000   # read artifacts / start paper runs over HTTP
```

## Safety rails

* `Settings.from_env()` requires `TRADING_MODE=live` **and**
  `ENABLE_LIVE_TRADING=true` **and** explicit `MAX_NOTIONAL_PER_ORDER`,
  `MAX_POSITION_PER_MARKET`, `MAX_DAILY_LOSS` before `live_enabled` can be true.
  Any inconsistent combination raises.
* `ExecutionEngine.submit` validates every order against `RiskManager` before
  it reaches a venue and raises `LiveTradingDisabled` for non-paper clients
  unless the engine itself was armed.
* Venue adapters' live order routing, request signing and order signing are
  stubs that raise `NotImplementedError` / `PermissionError`. There is no code
  path that sends an order to Kalshi or Polymarket.
* `measure_all`, `paper_loop`, `paper_runner` and the dashboard API call
  `require_paper_only` / `live_environment_requested` and refuse live flags.
* The scoreboard writer refuses `meta.source="sample"`; the dashboard shows the
  PnL tile only when `meta.pnl_source` is present.

## Dashboard

`dashboard/` is a Vite + React static app that renders the newest artifact it
can fetch: `scoreboard_latest.json` → `scoreboard_network.json` →
`scoreboard_sample.json`. The committed `scoreboard_latest.json` is a measured
Kalshi network canary; `scoreboard_sample.json` is a hand-written schema
example whose numbers (including the old `0.42`) were never measured, and the
UI labels it **Sample** and hides PnL.

```bash
cd dashboard
npm ci
npm run typecheck && npm run build
npm test                   # node --test: experiments index + sync against unknown track ids
npm run sync-artifacts     # copies every ../artifacts/scoreboard_*.json and
                           # paper/ledger_<track>.json when they exist, writes compact
                           # public/artifacts/runs/<run_id>.json records and rebuilds
                           # public/artifacts/experiments_index.json
```

The dashboard's **Experiments** card lists every run found in
`dashboard/public/artifacts/` (scoreboards deduped by `run_id`, plus `runs/*.json`
records). The index is regenerated on every `npm run build`, so it never claims a
run that is not on disk; sample files are marked SAMPLE with PnL hidden, and nothing
is labelled a backtest unless the artifact's `meta` says so.

Tracks are grouped into **families** (strategy lanes) by
`dashboard/scripts/track-families.mjs`: NegRisk (Polymarket NegRisk / combinatorial),
Kalshi FLB (maker / FLB), XV gated, XV ungated, cross-venue, single venue, news, other. The
card shows a thin lanes strip for the three pinned lanes — "Not measured yet" until a
run carrying that family is synced — and filter pills that scope fills / paper PnL to
one family. A track from a parallel branch appears after `measure_all` + `npm run
sync-artifacts` with no dashboard change: the sync discovers the new scoreboard and
ledger files, the index stamps a family on the track (by explicit `family`, known id, or
keyword match on the id; unknown ids land in **Other**), and the UI reads only those
stamps. See `dashboard/public/artifacts/schema.md` for the id conventions and the
step-by-step.

## Public deployment

The recommended public shape is a static Vercel dashboard backed by committed,
precomputed paper artifacts. The FastAPI process and paper loop remain on an
always-on host only if on-demand refresh is needed.

### Static Vercel deployment from Origin

Vercel supports Cursor Origin repositories directly in public beta; no GitHub
mirror is needed. Origin repositories are private and therefore cannot be
deployed from a Vercel Hobby team. Use an eligible paid Vercel team where you
are an Owner or Member:

1. In Vercel, select **New Project** and then **Continue with Origin**.
2. Connect the Origin team and select this repository. The same connection can
   also be initiated from the repository's **Apps → Vercel** page in Origin.
3. Set **Root Directory** to `dashboard`.
4. Select **Vite** as the Framework Preset if it is not detected.
5. Keep **Build Command** as `npm run build` and **Output Directory** as `dist`.
6. Do not set `VITE_API_BASE` for the static-only deployment.
7. Select **Deploy**. The resulting `*.vercel.app` production URL displays the
   committed measured snapshots. Future pushes to `main` trigger production
   deployments through the Origin connection.

`dashboard/vercel.json` contains the SPA rewrite. The committed files under
`dashboard/public/artifacts/` let the page render real scoreboard numbers when
there is no API.

To publish newer measured values:

```bash
uv run python -m apps.measure_all --network --kalshi-env prod --harvest-dir data/harvests
uv run python -m apps.paper_loop --once
cd dashboard && npm run sync-artifacts
git add public/artifacts
git commit -m "Refresh public scoreboard snapshots"
git push origin main
```

The sync script copies valid local JSON artifacts when they exist and never
copies a file whose `meta.source` is `sample`. During a clean Vercel build the
ignored runtime files are absent, so it preserves the committed snapshots.

### Optional hosted measurement API

For on-demand Run buttons, deploy FastAPI to an always-on Python or container
host such as Fly.io, Railway, or Render, then configure:

```text
# Vercel project build environment
VITE_API_BASE=https://your-paper-api.example.com

# FastAPI host environment
TRADING_MODE=paper
ENABLE_LIVE_TRADING=false
DASHBOARD_ALLOWED_ORIGINS=https://your-dashboard.vercel.app
```

The API start command is:

```bash
uv run uvicorn apps.dashboard_api:app --host 0.0.0.0 --port "$PORT"
```

Multiple dashboard origins can be supplied as a comma-separated
`DASHBOARD_ALLOWED_ORIGINS` value. This value is server-side CORS
configuration, not a client secret.

The Python backend is intentionally not packaged as Vercel serverless
functions: network measurements and background jobs can outlive serverless
request limits, while Vercel's filesystem is ephemeral. An external API host
must provide persistent storage for `artifacts/` and `data/harvests/` if job
history and harvested inputs must survive restarts.

The API currently has no authentication because it was designed for local use.
Do not expose its job-start endpoint unrestricted on the public internet;
protect the service at the hosting layer or add server-side authentication and
rate limits first. Static deployment is the safe public-view option and
contains no keys, wallets, live-order routes, or client secrets.

## Architecture

```text
venues/        Kalshi + Polymarket adapters (fixtures | public read-only network), shared paper fill simulator
core/          types, risk rails, portfolio (avg cost), PaperLedger, ExecutionEngine (single risk-gated route), config
strategies/    single-venue fair value (primary), cross-venue mispricing, depth/fee-aware paper edge, market matching, news underreaction, Polymarket arb detectors, FLB fades (taker + maker)
settlement/    clause extraction, resolution fingerprints, host tiers, Fed/CPI bucket matching, eight-stage admissibility gate
research/      scoreboard (12 isolated tracks over shared snapshots), Polymarket arb tracks, artifact + gate-report writers, harvest analysis, news signal stubs,
               flb (bands, fee model, snapshot verdicts), flb_expost (settled-trade harvest + band returns)
apps/          measure_all, measure_polymarket_arb, measure_flb, paper_loop, paper_runner, dashboard_api
dashboard/     Vite + React static scoreboard reading public/artifacts/*.json
docs/          ASSUMPTIONS.md audit, NEWS_UNDERREACTION.md lane audit, POLYMARKET_ARB.md runbook + findings, RUNBOOK_gated_cross_venue.md, FLB_RUNBOOK.md
```

### Tracks

| Track | Gate | Purpose |
| --- | --- | --- |
| `single_venue_fair_value` (primary) | explicit priors, risk rails | Kalshi canary; no cross-venue settlement exposure |
| `gated_cross_venue` | 8 stages (match, clauses, fingerprint, polarity, Fed bucket, interval, hosts, expiry), strict policy, fail-closed; then fee- and depth-aware edge | settlement-safe cross-venue measurement across all categories; `gate_report_<mode>.json` |
| `gated_cross_venue_macro` | same strict gate, macro only, touch-priced | legacy macro view |
| `ungated_cross_venue_macro` (control) | none, flagged | what the gate refused; settlement-risk flagged when it would have refused; PnL not realisable |
| `sports_cross_venue` | none, host conflicts counted | settlement-source disagreement on sports |
| `small_deliberate_bet` | gated, 2-contract cap | process probe |
| `news_underreaction` (optional) | signal freshness/confidence, residual threshold, fair-value engine rails | public signal → implied probability → residual → paper order; empty without a signal source; mapping UNKNOWN |
| `polymarket_rebalancing_arb` | depth-aware, fee + slippage, mirror check | binary YES+NO merge / split; quiet on the live CLOB (books are mirrors) |
| `polymarket_negrisk_arb` | NegRisk only, fee + slippage, capital cap | buy-all-NO + `NegRiskAdapter` conversion (executable, no lockup) |
| `polymarket_combinatorial_arb` | exclusive + non-augmented only | buy-all-YES held to resolution; locked capital reported |

| `kalshi_longshot_fade` | longshot side < 20¢, $25/$75/$75 paper caps, taker fee | taker fade of Kalshi longshots; shadow longshot buyer as benchmark |
| `kalshi_maker_quote` | same trigger, resting order, expected-value fills, maker fee, conservative marks | maker fade of Kalshi longshots |

Each track has its own risk manager, execution engine, portfolio and ledger.
The cross-venue, fair-value, news and Kalshi FLB tracks share one frozen per-venue
snapshot; the three Polymarket arb tracks share one frozen event snapshot (YES
and NO book per leg) captured in the same run. The two FLB tracks use their own
risk defaults ($25/order, $75/market, $75 daily) rather than the $100/$500/$250
defaults of the other tracks.

### Cross-venue admissibility in one paragraph

A Kalshi/Polymarket pair is admitted only when both markets cite the same
official publisher, their rule texts agree on fallback / tie-break / revisions,
both carry operator-authored resolution fingerprints that are equivalent (or a
safe complement with inverse polarity), any FOMC bucket or numeric interval is
identical, and close times agree. Network markets carry no fingerprints, so a
live run is expected to report candidates with `fingerprint_indeterminate`
(plus whatever else disagrees) and **zero admits**; that report is the
measurement. The 2026-09-14 network snapshot found 8 live Fed-decision pairs,
5 of them also refused for a clause mismatch (Polymarket rounds ties up,
Kalshi is silent). Details and the operator path to admit a pair:
`docs/RUNBOOK_gated_cross_venue.md`, `docs/ASSUMPTIONS.md` §4b.
