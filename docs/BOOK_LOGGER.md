# Self-logged order-book archive (`apps.book_logger`) — the $0 path

Paper-only, read-only, no vendors. `apps.book_logger` polls the public,
unauthenticated Kalshi (and optionally Polymarket) endpoints on an interval and
appends point-in-time L2 snapshots plus the public trade prints to JSONL files
under `artifacts/books/` (git-ignored). The goal is to own a forward archive so
later maker / favorite–longshot research has L2 we captured ourselves instead of
buying it. It refuses to start if `TRADING_MODE=live` or `ENABLE_LIVE_TRADING=true`,
and it never calls an authenticated endpoint.

Endpoints polled (all public):

| Venue | Endpoint | Used for |
| --- | --- | --- |
| Kalshi | `GET /markets?series_ticker=&status=open` (paginated) | universe listing, every `--universe-refresh` cycles |
| Kalshi | `GET /markets/{ticker}/orderbook[?depth=N]` | YES-bid and NO-bid ladders |
| Kalshi | `GET /markets/trades?ticker=&limit=1000[&cursor=]` | public tape (newest first, `taker_side` per print) |
| Polymarket | `GET gamma-api/public-search?q=`, `GET gamma-api/markets?condition_ids=` | universe (events → markets → CLOB token ids) |
| Polymarket | `POST clob/books` (≤200 tokens per request) | full aggregated ladder per token, with venue timestamp + hash |
| Polymarket | `GET data-api/trades?market=` (opt-in `--polymarket-trades`) | public fills; no stable trade id (see limits) |

## Run it locally

```bash
uv sync --extra dev

# One snapshot of the Kalshi macro canary series (books + first page of trades)
uv run python -m apps.book_logger --once

# Continuous: every 30 s for an hour, books + incremental trades, gzip on disk
uv run python -m apps.book_logger --interval 30 --duration 3600 --gzip

# Focus: explicit tickers, 50 levels per side, skip books that did not change
uv run python -m apps.book_logger --tickers KXFEDDECISION-26SEP-C25 KXFEDDECISION-26SEP-H \
    --series --depth 50 --skip-unchanged --interval 10

# Add Polymarket books (one batched request per cycle) and its public trade feed
uv run python -m apps.book_logger --polymarket --polymarket-search "Fed decision" "CPI inflation" \
    --polymarket-trades --interval 60
```

Every cycle prints one JSON line (`{"cycle":..,"kalshi":{"books":..,"trades":..,"markets":..,"errors":..}}`)
and rewrites `artifacts/books/sessions/<run_id>.json` with the config, running
counters, fetch statistics (requests, retries, 429s, limiter waits) and the last
errors. `Ctrl-C` finalises the session file and exits 0.

Flags worth knowing (`--help` lists everything):

* Schedule: `--interval`, `--once`, `--cycles N`, `--duration SECONDS`, `--universe-refresh N`.
* Universe: `--series` (default `DEFAULT_MACRO_SERIES`: `KXFEDDECISION KXFED KXCPIYOY KXCPI
  KXCPICORE KXPAYROLLS KXGDP KXU3`; pass an empty `--series` to disable listing),
  `--tickers`, `--limit` (truncates in the venue's listing order, so prefer `--tickers`
  for a hand-picked set), `--no-kalshi`, `--polymarket`, `--polymarket-search`,
  `--polymarket-condition-ids`, `--polymarket-limit`.
* Depth: `--depth 0` (default) omits the parameter and takes what the venue returns
  (10 levels per side observed on Kalshi); `--depth N` asks for N levels per side.
  Polymarket `/books` always returns the full aggregated ladder.
* Output: `--root`, `--gzip`, `--skip-unchanged`, `--no-trades`, `--to-parquet DIR`.
* Rate limiting: `--max-rps` (default 4, process-wide across venues), `--concurrency`
  (default 2 in flight per venue), `--retries` (default 4), `--timeout`.

### Rate-limit safety

One `RateLimiter` serialises every request start to at most `--max-rps` per second
across all venues. A `429` or `5xx` (or transport error) is retried with
exponential backoff, honouring `Retry-After` when present, and the limiter is
penalised so **every** in-flight task pauses rather than piling on. Other `4xx`
responses are not retried; a failing market is recorded in the cycle's `errors`
and the cycle continues. Cycle time is roughly `requests / max_rps`: Kalshi costs one
request per market for books plus one per market for trades (plus listing pages on
refresh), Polymarket costs one `POST /books` per 200 tokens plus, if enabled, one
trade request per market. If a cycle takes longer than `--interval`, the next one
starts immediately and `overruns` is incremented in the session file — raise
`--interval`, drop `--limit`, or pass `--no-trades`. At the default 4 rps, 200 Kalshi
markets with trades need ~100 s per cycle; 12 markets need ~6 s.

Measured 2026-09-14 (prod public API, no credentials): 36 requests at 3 rps, 0 retries,
0 `429`s, book latency 350–690 ms (Kalshi) / 200 ms (Polymarket batch of 16 tokens).

### Storage

Measured per record on 2026-09-14: Kalshi book at depth 10 ≈ 760 bytes (~18 levels
including the raw ladders), Polymarket book ≈ 1.9 KB (~80 levels), trade ≈ 250–450 bytes.
Books dominate. At a 30 s interval (2,880 cycles/day):

| Universe | Uncompressed / day | With `--gzip` (≈8–10×) |
| --- | --- | --- |
| 12 Kalshi markets | ≈ 26 MB | ≈ 3 MB |
| 200 Kalshi markets | ≈ 440 MB | ≈ 50 MB |
| + 100 Polymarket markets (200 tokens) | + ≈ 1.1 GB | + ≈ 120 MB |

`--skip-unchanged` writes a book only when its ladder differs from the previous
snapshot for that market/token; macro books are quiet between prints, so this
typically cuts book volume by an order of magnitude at the cost of losing the
explicit "confirmed unchanged at *t*" observations (recoverable: an unchanged book
is implied until the next record). Trades are incremental by construction.

Do not commit the archive. `.gitignore` covers `artifacts/`, `artifacts/books/`,
`*.jsonl.gz` and `*.parquet`; if you need a sample in a PR, paste a few lines into the
description.

## Run it on a schedule

The trade de-duplication state (seen `trade_id`s per market) lives in the process,
so **one long-running process is the recommended shape**. Cron-style `--once` runs
work for books but will re-fetch the newest trade page each run; de-duplicate on
`trade_id` (Kalshi) / `tx`+`token_id`+`price`+`size`+`ts_venue` (Polymarket) at read
time if you go that way.

**tmux / nohup (simplest):**

```bash
tmux new -d -s books 'cd ~/dual-market-trader && uv run python -m apps.book_logger --interval 30 --gzip >> artifacts/books/logger.out 2>&1'
```

**systemd user service (restarts on failure, survives logout with `loginctl enable-linger`):**

```ini
# ~/.config/systemd/user/book-logger.service
[Unit]
Description=dual-market-trader public order-book logger (paper-only)

[Service]
WorkingDirectory=%h/dual-market-trader
Environment=TRADING_MODE=paper ENABLE_LIVE_TRADING=false
ExecStart=%h/.local/bin/uv run python -m apps.book_logger --interval 30 --gzip --skip-unchanged
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now book-logger
journalctl --user -u book-logger -f
```

**cron, books only, every 5 minutes:**

```cron
*/5 * * * * cd $HOME/dual-market-trader && $HOME/.local/bin/uv run python -m apps.book_logger --once --no-trades --gzip >> artifacts/books/cron.out 2>&1
```

A daily `find artifacts/books -name '*.jsonl' -mtime +1 -exec gzip {} \;` keeps
uncompressed runs small if you did not pass `--gzip`.

## Reading the archive

```python
from pathlib import Path
from research.book_log import JsonlArchive, iter_records

archive = JsonlArchive(Path("artifacts/books"), compress=True)
books = iter_records(archive.files("kalshi", "books"))      # generator of dicts
trades = iter_records(archive.files("kalshi", "trades"))
```

```bash
uv sync --extra books                       # pyarrow
uv run python -m apps.book_logger --to-parquet artifacts/books_parquet
```

The Parquet export writes one file per `(venue, kind)`; nested ladders are stored as
JSON strings so the schema is flat and identical across venues.

### Record schema (`v: 1`)

Common: `kind` (`book` | `trade` | `market`), `v`, `venue`, `market_id`
(Kalshi ticker / Polymarket condition id), `ts` (capture time, UTC, ms, `Z`),
`cycle`. Prices and sizes are canonical decimal **strings** (`"0.62"`, `"120"`),
never floats; Kalshi legacy integer cents are converted to dollars.

| Kind | Fields |
| --- | --- |
| `book` | `bids` (YES bids, best first), `asks` (YES asks, best first), `ts_venue` (Polymarket book timestamp; `null` on Kalshi, which publishes none), `latency_ms` (request→response), `depth_requested`. Kalshi adds `raw.yes_bids` / `raw.no_bids` exactly as served (asks are `1 − NO bid`). Polymarket adds `token_id`, `outcome` (`yes`/`no`), `hash`, `tick_size`, `min_order_size`, `neg_risk`. |
| `trade` | `price` (YES price), `size`, `taker_side`, `ts_venue`. Kalshi: `trade_id`. Polymarket: `token_id`, `outcome`, `tx`, `trade_id: null`, `taker_side` is the Data-API `side` (`buy`/`sell` of that token). |
| `market` | Universe listing at refresh: Kalshi `series_ticker`, `event_ticker`, `title`, `status`, `open_time`, `close_time`, `yes_bid`, `yes_ask`, `last_price`, `volume`, `open_interest`; Polymarket `slug`, `question`, `event_slug`, `event_title`, `neg_risk`, `token_ids`, `outcomes`, `tick_size`, `min_order_size`, `end_date`, `liquidity`, `volume`, `fees_enabled`. |

## What is NOT captured (read before using this for queue models)

* **No L3 / FIFO.** The public books are aggregated size per price level. There are
  no order ids, no per-order sizes, no arrival order, no cancels or replaces. Queue
  position, time priority and fill probability for a resting order **cannot** be
  reconstructed from this archive; the maker track's fill assumptions
  (`docs/FLB_RUNBOOK.md`) stay assumptions.
* **Nothing between polls.** A snapshot every `--interval` seconds misses every book
  change and every crossing that occurs and reverts inside the interval. The trade
  tape fills part of that gap (every print is captured incrementally), but the book
  state each print hit is only known to the nearest snapshot.
* **Kalshi books carry no venue timestamp.** `ts` is when *our* response arrived;
  `latency_ms` bounds how stale it can be. Trades do carry `created_time` (`ts_venue`).
* **Depth is what the venue returns.** Kalshi without `--depth` served 10 levels per
  side; deeper levels are simply absent unless requested. Polymarket returns the full
  ladder.
* **Public tape only.** Kalshi trades show `taker_side`, price and count — not who
  traded, not whether a print opened or closed a position. Polymarket Data-API trades
  have no trade id (de-duplicated on a composite key) and may lag the CLOB.
* **Universe as filtered.** Only the listed series / tickers / search hits are
  archived; `--limit` truncates in listing order. Markets that open between refreshes
  are picked up at the next `--universe-refresh`.
* **No sequence numbers, no websocket.** This is polling REST; nothing here proves two
  snapshots are consecutive states of the venue's book.
* **Local clock.** `ts` comes from the logging machine; keep it NTP-synced.
* **Not a settlement record.** Outcomes are harvested separately
  (`apps.measure_flb --harvest-trades`); the archive holds prices, not results.

## Later free context feeds (stub only)

`research/book_log.py::SidecarSource` is a two-member protocol (`name`, `async poll()`)
for cheap public context feeds — an official RSS feed such as the Federal Reserve press
releases, or free weather observations from the NWS API — appended as
`<name>/<day>/<name>.jsonl` alongside the books so a headline or observation can later
be lined up against the book state at the same `ts`. Nothing is implemented and nothing
maps a feed to a probability; that is the (unvalidated) news lane's job
(`docs/NEWS_UNDERREACTION.md`), and this repository deliberately does not rebuild it.

## Tests

`tests/test_book_logger.py` runs the logger against `httpx.MockTransport` serving trimmed
public-API payloads (`research/fixtures/book_logger_public_api.json`): Kalshi listing
pagination, `orderbook_fp` and legacy-cents ladders → YES bids/asks, incremental trades
across cycles (pagination stops at the first seen id, no duplicates), `--skip-unchanged`,
Polymarket batched books + Data-API trades with a closed leg excluded, `429` backoff with
`Retry-After`, no retry on `404`, limiter spacing, per-market error isolation, the run loop,
the CLI `--once` path and the live-flag refusal, Parquet export. `uv run pytest -q`.
