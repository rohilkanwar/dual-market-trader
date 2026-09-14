# dual-market-trader

Paper-only measurement scaffold for Kalshi and Polymarket prediction markets
with a mark-to-market **paper PnL ledger**. Nothing in this repository places a
live order: venue adapters only read public market data, paper fills are
simulated locally against the observed book, and every PnL number on the
dashboard is read from the ledger.

Venue focus is **Kalshi single-venue fair value** (the primary track). The
cross-venue tracks exist to *measure* how often settlement rules diverge, not
to trade them; see `docs/ASSUMPTIONS.md` for what has been validated.

## Quick start

```bash
# Python 3.12+. uv is preferred; plain pip works too.
uv sync --extra dev                      # or: python -m pip install -e ".[dev]"
uv run pytest -q                         # or: python -m pytest -q

# One paper cycle on committed fixtures (deterministic, no network)
uv run python -m apps.paper_loop --once --artifact-dir artifacts

# One read-only network canary against public Kalshi macro series + Polymarket
uv run python -m apps.measure_all --network --limit 15 --kalshi-env prod --artifact-dir artifacts
```

`TRADING_MODE=paper` and `ENABLE_LIVE_TRADING=false` are the defaults. Every
entry point refuses to start if either variable requests live operation.

### Where artifacts land

| Path | Contents |
| --- | --- |
| `artifacts/scoreboard_latest.json` | Dashboard artifact (schema 1.2.0, `meta.source=measured`, `meta.pnl_source=core.ledger.PaperLedger`) |
| `artifacts/scoreboard_<fixtures\|network>.json` | Same document, kept per mode |
| `artifacts/paper/ledger_<track>.json` | Full ledger per track: cash, fills (with fees), marks, positions, equity curve, max drawdown |
| `artifacts/paper/equity_curve_<track>.jsonl` | One appended equity point per run/cycle |
| `artifacts/paper/runs/<run_id>.json` | Raw track summaries for the run |
| `artifacts/paper_loop_latest.json`, `paper_loop_history.jsonl` | Paper-loop cycle payloads (append-only history) |

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
  (`--no-fees` disables it); Polymarket fills are modeled fee-free
* `settle(venue, market, outcome)` closes a position at 1/0

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

### Other entry points

```bash
uv run python -m apps.measure_all --help            # one-shot measurement, all flags
uv run python -m apps.paper_runner --strategy both  # strategy runner with logging event sink
uv run python -m research.compare_markets --network # top markets per venue
uv run python -m research.cross_venue_edges         # matched pairs and executable edges
uv run python -m research.harvest_public            # harvest resolved macro markets (public data)
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
npm run sync-artifacts     # copies ../artifacts/scoreboard_*.json when they exist
```

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
strategies/    single-venue fair value (primary), cross-venue mispricing, market matching
settlement/    clause extraction, resolution fingerprints, host tiers, Fed/CPI bucket matching
research/      scoreboard (5 isolated tracks over one shared snapshot), artifact writer, harvest analysis
apps/          measure_all, paper_loop, paper_runner, dashboard_api
dashboard/     Vite + React static scoreboard reading public/artifacts/*.json
docs/          ASSUMPTIONS.md audit
```

### Tracks

| Track | Gate | Purpose |
| --- | --- | --- |
| `single_venue_fair_value` (primary) | explicit priors, risk rails | Kalshi canary; no cross-venue settlement exposure |
| `gated_cross_venue_macro` | clauses → fingerprint → hosts, fail-closed | how many macro pairs are admissible at all |
| `ungated_cross_venue_macro` | none, flagged | what the gate refused; settlement-risk flagged when it would have refused |
| `sports_cross_venue` | none, host conflicts counted | settlement-source disagreement on sports |
| `small_deliberate_bet` | gated, 2-contract cap | process probe |

Each track has its own risk manager, execution engine, portfolio and ledger;
all tracks see the same frozen market snapshot for a run.
