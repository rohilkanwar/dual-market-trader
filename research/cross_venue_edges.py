"""Print matched Kalshi/Polymarket probabilities and executable paper edges."""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from core.portfolio import Portfolio
from core.risk import RiskLimits, RiskManager
from strategies.cross_venue import CrossVenueMispricingStrategy
from strategies.matching import MarketMatcher
from venues.kalshi import KalshiClient
from venues.polymarket import PolymarketClient


def _value(value: Decimal | None) -> str:
    return f"{value:+.3f}" if value is not None else "n/a"


async def run(*, network: bool, limit: int) -> None:
    kalshi = KalshiClient(paper=True, use_fixtures=not network)
    polymarket = PolymarketClient(paper=True, use_fixtures=not network)
    strategy = CrossVenueMispricingStrategy(
        risk=RiskManager(
            RiskLimits(
                max_notional_per_order=Decimal("1000000"),
                max_position_per_market=Decimal("1000000"),
                max_daily_loss=Decimal("1000000"),
            )
        ),
        portfolio=Portfolio(),
    )
    try:
        kalshi_markets, poly_markets = await asyncio.gather(
            kalshi.list_markets(limit=limit),
            polymarket.list_markets(limit=limit),
        )
        pairs = MarketMatcher().match(kalshi_markets, poly_markets)
        headers = ("pair", "match", "polarity", "p_k", "p_poly", "raw", "exec", "qty", "action")
        rows: list[tuple[str, ...]] = []
        for pair in pairs:
            kalshi_book, poly_book = await asyncio.gather(
                kalshi.get_order_book(pair.kalshi),
                polymarket.get_order_book(pair.polymarket),
            )
            evaluation = strategy.evaluate(pair, kalshi_book, poly_book)
            rows.append(
                (
                    pair.pair_id,
                    f"{pair.method}:{pair.confidence:.2f}",
                    "same" if pair.same_polarity else "inverse",
                    _value(evaluation.kalshi_mid),
                    _value(evaluation.polymarket_mid),
                    _value(evaluation.raw_edge),
                    _value(evaluation.executable_edge),
                    str(evaluation.quantity),
                    evaluation.reason,
                )
            )
        if not rows:
            print("No matched markets.")
            return
        widths = [
            max(len(headers[index]), *(len(row[index]) for row in rows))
            for index in range(len(headers))
        ]
        print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(headers)))
        print("-+-".join("-" * width for width in widths))
        for row in rows:
            print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    finally:
        await asyncio.gather(kalshi.close(), polymarket.close())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="query public APIs")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    asyncio.run(run(network=args.network, limit=args.limit))


if __name__ == "__main__":
    main()
