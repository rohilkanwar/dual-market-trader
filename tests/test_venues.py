import asyncio
from decimal import Decimal

import httpx
import pytest

from core.types import Market, Order, OrderStatus, Outcome, Side, Venue
from venues.kalshi import KalshiClient
from venues.polymarket import PolymarketClient


async def test_fixture_clients_serve_both_venues() -> None:
    clients = [
        KalshiClient(paper=True, use_fixtures=True),
        PolymarketClient(paper=True, use_fixtures=True),
    ]
    try:
        grouped = await asyncio.gather(*(client.list_markets() for client in clients))
        books = await asyncio.gather(
            *(
                client.get_order_book(markets[0])
                for client, markets in zip(clients, grouped, strict=True)
            )
        )
        assert {market.venue for markets in grouped for market in markets} == {
            Venue.KALSHI,
            Venue.POLYMARKET,
        }
        assert all(book.best_bid is not None and book.best_ask is not None for book in books)
        assert all(market.metadata["source"] == "fixture" for markets in grouped for market in markets)
    finally:
        await asyncio.gather(*(client.close() for client in clients))


async def test_non_paper_client_place_order_is_fail_closed() -> None:
    client = KalshiClient(paper=False, use_fixtures=True)
    try:
        markets = await client.list_markets()
        order = Order(
            venue=Venue.KALSHI,
            market_id=markets[0].market_id,
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("0.5"),
        )
        with pytest.raises(PermissionError, match="paper-only"):
            await client.place_order(order)
    finally:
        await client.close()


async def test_kalshi_network_book_derives_yes_asks_from_no_bids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook": {"yes": [[52, 120], [51, 200]], "no": [[46, 110]]}})
        return httpx.Response(
            200,
            json={
                "markets": [
                    {
                        "ticker": "KXFED-26SEP",
                        "title": "Fed cut in September?",
                        "status": "open",
                        "liquidity": 4200000,
                        "volume": 185000,
                        "rules_primary": "Resolves per https://www.federalreserve.gov/",
                        "category": "Economics",
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiClient(paper=True, use_fixtures=False, environment="demo", http=http)
    try:
        markets = await client.list_markets(limit=5)
        assert markets[0].market_id == "KXFED-26SEP"
        assert markets[0].metadata["category"] == "macro"
        assert markets[0].liquidity == Decimal("42000")
        book = await client.get_order_book(markets[0])
        assert book.best_bid is not None and book.best_bid.price == Decimal("0.52")
        assert book.best_ask is not None and book.best_ask.price == Decimal("0.54")
        assert book.best_ask.size == Decimal("110")
    finally:
        await client.close()
        await http.aclose()


async def test_polymarket_network_market_parses_token_ids_and_book() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("clob"):
            return httpx.Response(200, json={"bids": [{"price": "0.41", "size": "20"}], "asks": [{"price": "0.43", "size": "16"}]})
        return httpx.Response(
            200,
            json=[
                {
                    "conditionId": "0xabc",
                    "question": "Will the Fed cut rates in September?",
                    "slug": "fed-cut-september",
                    "clobTokenIds": '["111", "222"]',
                    "liquidityNum": 61000,
                    "volumeNum": 402000,
                    "active": True,
                    "closed": False,
                    "description": "Resolves per https://www.federalreserve.gov/",
                }
            ],
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = PolymarketClient(paper=True, use_fixtures=False, http=http)
    try:
        markets = await client.list_markets(limit=5)
        assert markets[0].market_id == "0xabc"
        assert markets[0].yes_token_id == "111" and markets[0].no_token_id == "222"
        assert markets[0].metadata["category"] == "macro"
        book = await client.get_order_book(markets[0])
        assert book.mid_price == Decimal("0.42")
    finally:
        await client.close()
        await http.aclose()


async def test_paper_fill_is_deterministic_and_walks_levels() -> None:
    client = KalshiClient(paper=True, use_fixtures=True)
    try:
        markets = {m.market_id: m for m in await client.list_markets()}
        market: Market = markets["KX-FED-SEP-CUT"]  # asks 0.54x110, 0.55x180
        order = Order(
            venue=Venue.KALSHI,
            market_id=market.market_id,
            side=Side.BUY,
            quantity=Decimal("150"),
            price=Decimal("0.55"),
        )
        first = await client.place_order(order)
        second = await client.place_order(order)
        assert first.order.status is OrderStatus.FILLED
        assert [(f.price, f.quantity) for f in first.fills] == [
            (Decimal("0.54"), Decimal("110")),
            (Decimal("0.55"), Decimal("40")),
        ]
        assert [(f.price, f.quantity, f.fee) for f in first.fills] == [
            (f.price, f.quantity, f.fee) for f in second.fills
        ]
        # Limit below the touch rests unfilled instead of inventing liquidity.
        resting = await client.place_order(
            Order(venue=Venue.KALSHI, market_id=market.market_id, side=Side.BUY, quantity=Decimal("1"), price=Decimal("0.50"))
        )
        assert resting.order.status is OrderStatus.ACCEPTED and not resting.fills
        # NO orders are executed against the complemented book: NO ask = 1 - YES bid.
        no_fill = await client.place_order(
            Order(venue=Venue.KALSHI, market_id=market.market_id, side=Side.BUY, outcome=Outcome.NO, quantity=Decimal("5"), price=Decimal("0.48"))
        )
        assert no_fill.fills[0].price == Decimal("0.48")
    finally:
        await client.close()
