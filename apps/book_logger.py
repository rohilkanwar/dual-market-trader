"""Self-log public order books and trades to a local JSONL archive ($0 path).

Polls unauthenticated Kalshi (and optionally Polymarket) endpoints on an
interval and appends point-in-time L2 snapshots plus public trade prints
under ``artifacts/books/`` (git-ignored). No credentials, no orders. Refuses
to start if the environment requests live trading.

Examples::

    # One Kalshi snapshot of the macro canary series, no trades
    python -m apps.book_logger --once --no-trades

    # Every 30 s for an hour, Kalshi macro series + trades (default universe)
    python -m apps.book_logger --interval 30 --duration 3600

    # Explicit tickers, deeper book, Polymarket books too, gzip on disk
    python -m apps.book_logger --tickers KXFEDDECISION-26SEP-C25 --depth 50 \
        --polymarket --gzip

    # Export the archive to Parquet (needs `uv sync --extra books`)
    python -m apps.book_logger --to-parquet artifacts/books_parquet

See ``docs/BOOK_LOGGER.md`` for scheduling, the record schema and what the
archive does not contain (true L3 / FIFO queue).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from core.config import require_paper_only
from research.book_log import (
    DEFAULT_ROOT,
    BookLogger,
    BookSource,
    Fetcher,
    JsonlArchive,
    KalshiBookSource,
    LoggerConfig,
    PolymarketBookSource,
    RateLimiter,
    export_parquet,
)
from venues.kalshi.client import DEFAULT_MACRO_SERIES, HOSTS
from venues.polymarket.client import DEFAULT_MACRO_SEARCH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    run = parser.add_argument_group("schedule")
    run.add_argument("--interval", type=float, default=30.0, help="seconds between poll cycles (default 30)")
    run.add_argument("--once", action="store_true", help="one cycle and exit")
    run.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    run.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    run.add_argument("--universe-refresh", type=int, default=20, help="re-list markets every N cycles (default 20)")

    out = parser.add_argument_group("output")
    out.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=f"archive root (default {DEFAULT_ROOT})")
    out.add_argument("--gzip", action="store_true", help="append to .jsonl.gz instead of .jsonl")
    out.add_argument("--skip-unchanged", action="store_true", help="do not write a book identical to the previous snapshot")
    out.add_argument("--no-trades", action="store_true", help="books only; skip the public trade tapes")
    out.add_argument("--to-parquet", type=Path, default=None, metavar="DIR", help="convert --root to Parquet under DIR and exit")

    kalshi = parser.add_argument_group("kalshi")
    kalshi.add_argument("--no-kalshi", action="store_true", help="disable the Kalshi source")
    kalshi.add_argument("--kalshi-env", choices=tuple(HOSTS), default="prod", help="public API host (default prod)")
    kalshi.add_argument("--series", nargs="*", default=list(DEFAULT_MACRO_SERIES), help="Kalshi series tickers to list (open markets)")
    kalshi.add_argument("--tickers", nargs="*", default=[], help="explicit Kalshi market tickers (added to the series universe)")
    kalshi.add_argument("--limit", type=int, default=200, help="max Kalshi markets per cycle (default 200)")
    kalshi.add_argument("--depth", type=int, default=0, help="orderbook depth per side; 0 = venue default / full (default 0)")
    kalshi.add_argument("--trade-pages", type=int, default=3, help="max 1000-trade pages per market per cycle after the first (default 3)")

    poly = parser.add_argument_group("polymarket (optional)")
    poly.add_argument("--polymarket", action="store_true", help="also log Polymarket CLOB books (batched POST /books)")
    poly.add_argument("--polymarket-search", nargs="*", default=list(DEFAULT_MACRO_SEARCH), help="Gamma public-search terms for the universe")
    poly.add_argument("--polymarket-condition-ids", nargs="*", default=[], help="explicit condition ids (added to the search universe)")
    poly.add_argument("--polymarket-limit", type=int, default=100, help="max Polymarket markets (default 100)")
    poly.add_argument("--polymarket-trades", action="store_true", help="also poll the public Data-API trade feed (one request per market)")

    net = parser.add_argument_group("rate limiting")
    net.add_argument("--max-rps", type=float, default=4.0, help="max requests per second across all venues (default 4)")
    net.add_argument("--concurrency", type=int, default=2, help="in-flight requests per venue (default 2)")
    net.add_argument("--retries", type=int, default=4, help="retries on 429/5xx/transport errors (default 4)")
    net.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds (default 20)")
    return parser


def build_sources(args: argparse.Namespace, fetcher: Fetcher) -> list[BookSource]:
    sources: list[BookSource] = []
    if not args.no_kalshi:
        sources.append(
            KalshiBookSource(
                fetcher,
                environment=args.kalshi_env,
                series=tuple(args.series),
                tickers=tuple(args.tickers),
                limit=args.limit,
                depth=args.depth,
                trade_pages_max=args.trade_pages,
                concurrency=args.concurrency,
            )
        )
    if args.polymarket:
        sources.append(
            PolymarketBookSource(
                fetcher,
                search_terms=tuple(args.polymarket_search),
                condition_ids=tuple(args.polymarket_condition_ids),
                limit=args.polymarket_limit,
                trades=args.polymarket_trades,
                concurrency=args.concurrency,
            )
        )
    return sources


async def run(args: argparse.Namespace, *, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    require_paper_only("book_logger")
    owns = http is None
    client = http or httpx.AsyncClient(timeout=args.timeout, headers={"User-Agent": "dual-market-trader book_logger (paper-only, read-only)"})
    fetcher = Fetcher(client, RateLimiter(args.max_rps), retries=args.retries)
    sources = build_sources(args, fetcher)
    if not sources:
        raise SystemExit("nothing to log: enable Kalshi (drop --no-kalshi) or pass --polymarket")
    archive = JsonlArchive(args.root, compress=args.gzip)
    config = LoggerConfig(
        interval=max(0.0, args.interval),
        universe_refresh_every=max(1, args.universe_refresh),
        trades=not args.no_trades,
        skip_unchanged=args.skip_unchanged,
    )
    cli_args = {
        "kalshi_env": None if args.no_kalshi else args.kalshi_env,
        "series": None if args.no_kalshi else list(args.series),
        "tickers": None if args.no_kalshi else list(args.tickers),
        "depth": None if args.no_kalshi else args.depth,
        "polymarket": args.polymarket,
        "polymarket_search": list(args.polymarket_search) if args.polymarket else None,
        "polymarket_trades": args.polymarket_trades if args.polymarket else None,
        "max_rps": args.max_rps,
        "concurrency": args.concurrency,
        "gzip": args.gzip,
    }
    logger = BookLogger(sources, archive, config=config, fetcher=fetcher, cli_args=cli_args)
    max_cycles = 1 if args.once else args.cycles
    try:
        return await logger.run(max_cycles=max_cycles, duration=args.duration)
    finally:
        if owns:
            await client.aclose()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.to_parquet is not None:
        counts = export_parquet(args.root, args.to_parquet)
        print(json.dumps({"parquet_dir": str(args.to_parquet), "rows": counts}))
        return
    try:
        session = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("interrupted; session file is up to date", file=sys.stderr)
        return
    counts = session["counts"]
    print(
        json.dumps(
            {
                "run_id": session["run_id"],
                "cycles": session["cycles"],
                "books": counts["books"],
                "trades": counts["trades"],
                "errors": counts["errors"],
                "root": session["archive"]["root"],
            }
        )
    )


if __name__ == "__main__":
    main()
