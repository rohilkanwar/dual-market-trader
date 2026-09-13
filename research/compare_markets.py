"""Compare top markets from both venues using fixtures or public APIs."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from decimal import Decimal

from core.venue import VenueClient
from venues.kalshi import KalshiClient
from venues.polymarket import PolymarketClient


@dataclass(frozen=True, slots=True)
class Row:
    venue: str
    market: str
    mid: Decimal | None
    spread: Decimal | None
    liquidity: Decimal
    volume: Decimal


async def fetch_rows(client: VenueClient, limit: int) -> list[Row]:
    markets = await client.list_markets(limit=limit)
    rows: list[Row] = []
    for market in markets:
        book = await client.get_order_book(market)
        rows.append(
            Row(
                venue=market.venue.value,
                market=market.title,
                mid=book.mid_price,
                spread=book.spread,
                liquidity=market.liquidity,
                volume=market.volume,
            )
        )
    return rows


def render(rows: list[Row]) -> str:
    headers = ("venue", "market", "mid", "spread", "liquidity", "volume")
    values = [
        (
            row.venue,
            row.market[:52],
            f"{row.mid:.3f}" if row.mid is not None else "n/a",
            f"{row.spread:.3f}" if row.spread is not None else "n/a",
            f"{row.liquidity:,.0f}",
            f"{row.volume:,.0f}",
        )
        for row in sorted(rows, key=lambda item: item.liquidity, reverse=True)
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in values))
        for index in range(len(headers))
    ]
    lines = [
        " | ".join(value.ljust(widths[index]) for index, value in enumerate(headers)),
        "-+-".join("-" * width for width in widths),
    ]
    lines.extend(
        " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in values
    )
    return "\n".join(lines)


async def run(*, network: bool, limit: int) -> None:
    clients: list[VenueClient] = [
        KalshiClient(paper=True, use_fixtures=not network),
        PolymarketClient(paper=True, use_fixtures=not network),
    ]
    try:
        grouped = await asyncio.gather(*(fetch_rows(client, limit) for client in clients))
        print(render([row for rows in grouped for row in rows]))
    finally:
        await asyncio.gather(*(client.close() for client in clients))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="query public APIs")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(run(network=args.network, limit=args.limit))


if __name__ == "__main__":
    main()
