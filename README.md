`ENABLE_LIVE_TRADING`, creates venue clients only through the paper scoreboard,
and refuses to start if either environment variable requests live operation.

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
there is no API. In this mode the page labels itself **Static snapshot** and
disables the Run buttons because Vercel is not executing the Python
measurement pipeline.

To publish newer measured values:

```bash
uv run python -m apps.measure_all --network --harvest-dir data/harvests
uv run python -m apps.paper_loop --once
cd dashboard && npm run sync-artifacts
git add public/artifacts
git commit -m "Refresh public scoreboard snapshots"
git push origin main
```

The sync script copies valid local JSON artifacts when they exist. During a
clean Vercel build those ignored runtime files are absent, so it preserves the
committed snapshots and their original measurement timestamp.

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
