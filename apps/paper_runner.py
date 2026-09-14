"""One-shot paper run of a strategy against fixture or public books.

Paper-only: venue clients are always constructed with ``paper=True``. Even if
``Settings.live_enabled`` were true, the adapters' live order routing raises.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from decimal import Decimal

from core.config import Settings
from core.execution import ExecutionEngine
from core.ledger import PaperLedger
from core.observability import LoggingEventSink
from core.risk import RiskManager
from core.types import Venue
from core.venue import VenueClient
from strategies import (
    CalibratedFairValueStrategy,
    CrossVenueMispricingStrategy,
    CrossVenueParameters,
    CrossVenueStrategyRunner,
    StrategyRunner,
)
from venues.kalshi import KalshiClient
from venues.polymarket import PolymarketClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", choices=("kalshi", "polymarket", "both"), default="both")
    parser.add_argument("--network", action="store_true", help="read public books")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--strategy",
        choices=("cross_venue", "fair_value", "both"),
        default="cross_venue",
    )
    parser.add_argument(
        "--cross-min-edge",
        type=Decimal,
        default=Decimal("0.04"),
        help="minimum normalized midpoint difference (default: 0.04)",
    )
    return parser.parse_args()


async def run(venue: str, strategy: str, cross_min_edge: Decimal, *, network: bool, limit: int) -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(message)s",
    )
    if settings.live_enabled:
        raise SystemExit("paper_runner is paper-only; live settings detected, refusing to start")

    risk = RiskManager(settings.risk_limits, kill_switch=settings.kill_switch)
    ledger = PaperLedger(ledger_id="paper_runner")
    portfolio = ledger.portfolio
    execution = ExecutionEngine(risk=risk, events=LoggingEventSink(), ledger=ledger, live_enabled=False)

    clients: list[VenueClient] = []
    if venue in {"kalshi", "both"}:
        clients.append(KalshiClient(paper=True, use_fixtures=not network, environment=settings.kalshi_env))
    if venue in {"polymarket", "both"}:
        clients.append(PolymarketClient(paper=True, use_fixtures=not network))

    try:
        by_venue = {client.venue: client for client in clients}
        results = []
        if strategy in {"cross_venue", "both"}:
            cross_runner = CrossVenueStrategyRunner(
                CrossVenueMispricingStrategy(
                    risk=risk,
                    portfolio=portfolio,
                    parameters=CrossVenueParameters(minimum_mid_edge=cross_min_edge),
                ),
                execution,
            )
            results.append(
                await cross_runner.run(by_venue[Venue.KALSHI], by_venue[Venue.POLYMARKET], limit=limit)
            )
        if strategy in {"fair_value", "both"}:
            fair_runner = StrategyRunner(
                CalibratedFairValueStrategy(portfolio=portfolio, risk=risk), execution
            )
            for client in clients:
                results.append(await fair_runner.run(client, limit=limit))
        reports = [report for group in results for report in group]
        for position in ledger.open_positions:
            client = by_venue.get(position.venue)
            market = client._market_cache.get(position.market_id) if client else None  # type: ignore[attr-defined]
            if client and market:
                ledger.mark_from_book(position.venue, position.market_id, await client.get_order_book(market))
        ledger.snapshot(label="paper_runner")
        print(f"orders={len(reports)} fills={sum(len(r.fills) for r in reports)}")
        for key, value in ledger.summary().items():
            if key in {"positions", "concentration"}:
                continue
            print(f"{key}: {value}")
    finally:
        await asyncio.gather(*(client.close() for client in clients))


def main() -> None:
    args = parse_args()
    if args.strategy in {"cross_venue", "both"} and args.venue != "both":
        raise SystemExit("cross_venue strategy requires --venue both")
    asyncio.run(
        run(args.venue, args.strategy, args.cross_min_edge, network=args.network, limit=max(1, args.limit))
    )


if __name__ == "__main__":
    main()
