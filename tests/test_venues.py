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


async def test_kalshi_network_current_payload_format() -> None:
    """Dollar-string prices, orderbook_fp ladders and event enrichment (2026 API)."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/orderbook"):
            # Both ladders are bids, ascending; NO bid 0.46 => YES ask 0.54.
            return httpx.Response(
                200,
                json={"orderbook_fp": {"yes_dollars": [["0.5100", "200.00"], ["0.5200", "120.00"]], "no_dollars": [["0.4600", "110.00"]]}},
            )
        if "/events/" in request.url.path:
            return httpx.Response(
                200,
                json={"event": {"category": "Economics", "title": "Fed decision in Sep 2026?", "settlement_sources": [{"name": "Federal Reserve", "url": "https://www.federalreserve.gov"}]}},
            )
        assert request.url.params["series_ticker"] == "KXFEDDECISION"
        return httpx.Response(
            200,
            json={
                "markets": [
                    {
                        "ticker": "KXFEDDECISION-26SEP-C25",
                        "event_ticker": "KXFEDDECISION-26SEP",
                        "title": "Fed cuts 25bps in September?",
                        "status": "active",
                        "liquidity_dollars": "0.0000",
                        "volume_fp": "185000.00",
                        "yes_bid_dollars": "0.5200",
                        "yes_ask_dollars": "0.5400",
                        "rules_primary": "If the Federal Reserve cuts by 25bps, resolves Yes.",
                    },
                    {
                        "ticker": "KXFEDDECISION-26SEP-DEAD",
                        "event_ticker": "KXFEDDECISION-26SEP",
                        "title": "one-sided quote ranks below",
                        "status": "active",
                        "volume_fp": "999999.00",
                        "yes_bid_dollars": "0.0000",
                        "yes_ask_dollars": "0.0100",
                    },
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiClient(paper=True, use_fixtures=False, environment="prod", http=http, series_tickers=("KXFEDDECISION",))
    try:
        markets = await client.list_markets(limit=5)
        assert [m.market_id for m in markets] == ["KXFEDDECISION-26SEP-C25", "KXFEDDECISION-26SEP-DEAD"]
        top = markets[0]
        assert top.metadata["category"] == "macro"
        assert top.metadata["source_url"] == "https://www.federalreserve.gov"
        assert "Settlement source: Federal Reserve" in top.metadata["resolution_text"]
        assert top.volume == Decimal("185000.00")
        assert calls.count("/trade-api/v2/events/KXFEDDECISION-26SEP") == 1  # cached per event
        book = await client.get_order_book(top)
        assert book.best_bid is not None and book.best_bid.price == Decimal("0.52")
        assert book.best_ask is not None and book.best_ask.price == Decimal("0.54")
        assert book.best_ask.size == Decimal("110")
    finally:
        await client.close()
        await http.aclose()


async def test_kalshi_network_legacy_cents_format_still_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook": {"yes": [[52, 120], [51, 200]], "no": [[46, 110]]}})
        if "/events/" in request.url.path:
            return httpx.Response(404, json={})
        return httpx.Response(
            200,
            json={"markets": [{"ticker": "KXFED-26SEP", "title": "Fed cut in September?", "status": "open", "liquidity": 4200000, "volume": 185000, "yes_bid": 52, "yes_ask": 54, "category": "Economics"}]},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KalshiClient(paper=True, use_fixtures=False, environment="demo", http=http, series_tickers=None)
    try:
        markets = await client.list_markets(limit=5)
        assert markets[0].metadata["category"] == "macro"
        assert markets[0].liquidity == Decimal("42000")
        assert markets[0].metadata["yes_bid"] == Decimal("0.52")
        book = await client.get_order_book(markets[0])
        assert book.best_bid is not None and book.best_bid.price == Decimal("0.52")
        assert book.best_ask is not None and book.best_ask.price == Decimal("0.54")
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
