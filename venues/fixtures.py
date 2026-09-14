"""Shared loader for the committed venue fixture files."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.types import ZERO, Market, OrderBook, PriceLevel, Venue


def load_fixture(path: Path, venue: Venue) -> tuple[list[Market], dict[str, OrderBook]]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    markets = [
        Market(
            venue=venue,
            market_id=str(item["market_id"]),
            title=str(item.get("title", "")),
            active=bool(item.get("active", True)),
            liquidity=Decimal(str(item.get("liquidity", "0"))),
            volume=Decimal(str(item.get("volume", "0"))),
            yes_token_id=item.get("yes_token_id"),
            no_token_id=item.get("no_token_id"),
            metadata={**item.get("metadata", {}), "source": "fixture"},
        )
        for item in payload.get("markets", [])
    ]
    books = {
        market_id: OrderBook(
            market_id=market_id,
            bids=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in book.get("bids", [])),
            asks=tuple(PriceLevel(Decimal(p), Decimal(s)) for p, s in book.get("asks", [])),
        )
        for market_id, book in payload.get("order_books", {}).items()
    }
    for market in markets:
        books.setdefault(market.market_id, OrderBook(market_id=market.market_id))
    return markets, books


def decimal_or_zero(value: Any) -> Decimal:
    if value is None or value == "":
        return ZERO
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return ZERO
