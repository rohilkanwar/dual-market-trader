"""Kalshi adapter: fixture books, public read-only market data, paper fills.

Only unauthenticated endpoints are called (``GET /markets`` and
``GET /markets/{ticker}/orderbook``). No API key is required for those. The
authenticated signer and live order routing are deliberately stubs that raise;
see ``docs/ASSUMPTIONS.md``.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from core.types import ONE, Market, OrderBook, PriceLevel, Venue
from core.venue import VenueClient
from venues.fixtures import decimal_or_zero, load_fixture
from venues.paper import FeeSchedule, PaperExecutionMixin, kalshi_fee

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "markets.json"
HOSTS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
}
CENTS = Decimal("100")
_MACRO_HINTS = ("fed", "cpi", "inflation", "rate", "gdp", "unemployment", "payroll", "econom")
_SPORTS_HINTS = ("nba", "nfl", "mlb", "nhl", "wnba", "game", "beat", "win", "sport")


class KalshiSigner:
    """Placeholder for RSA-PSS request signing. Intentionally not implemented.

    Read-only market data needs no signature. Implementing this is the first
    step of the live/demo-authenticated path and must wait for an explicit go
    plus demo credentials supplied through the environment, never in code.
    """

    def __init__(self, access_key_id: str = "", private_key_path: str = "") -> None:
        self.access_key_id = access_key_id
        self.private_key_path = private_key_path

    @property
    def configured(self) -> bool:
        return bool(self.access_key_id and self.private_key_path)

    def headers(self, method: str, path: str) -> dict[str, str]:
        del method, path
        raise NotImplementedError(
            "Kalshi authenticated requests are stubbed in this paper-only build"
        )


class KalshiWebSocketClient:
    """Placeholder streaming client; polling REST is the only implemented path."""

    def __init__(self, environment: str = "demo") -> None:
        self.environment = environment

    async def connect(self) -> None:
        raise NotImplementedError("Kalshi websocket streaming is not implemented")


def infer_category(item: dict[str, Any]) -> str:
    explicit = str(item.get("category") or "").strip().lower()
    if explicit:
        if any(h in explicit for h in ("econom", "financ", "politic", "fed")):
            return "macro"
        if "sport" in explicit:
            return "sports"
        return explicit
    text = " ".join(
        str(item.get(k) or "") for k in ("ticker", "event_ticker", "title", "subtitle")
    ).lower()
    if any(h in text for h in _SPORTS_HINTS):
        return "sports"
    if any(h in text for h in _MACRO_HINTS):
        return "macro"
    return ""


def _cents_to_probability(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        price = Decimal(str(value)) / CENTS
    except ArithmeticError:
        return None
    return price if Decimal("0") <= price <= ONE else None


class KalshiClient(PaperExecutionMixin, VenueClient):
    venue = Venue.KALSHI

    def __init__(
        self,
        *,
        paper: bool = True,
        use_fixtures: bool = True,
        environment: str | None = None,
        http: httpx.AsyncClient | None = None,
        fee_schedule: FeeSchedule | None = None,
        signer: KalshiSigner | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.paper = paper
        self.use_fixtures = use_fixtures
        self.environment = (environment or os.getenv("KALSHI_ENV") or "demo").lower()
        if self.environment not in HOSTS:
            raise ValueError(f"KALSHI_ENV must be one of {sorted(HOSTS)}")
        self.base_url = HOSTS[self.environment]
        self.signer = signer
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._fixture_books: dict[str, OrderBook] = {}
        self._init_paper(fee_schedule or kalshi_fee)

    # ------------------------------------------------------------ market data
    async def list_markets(self, *, limit: int = 20) -> list[Market]:
        if self.use_fixtures:
            markets, books = load_fixture(FIXTURE_PATH, Venue.KALSHI)
            self._fixture_books = books
            markets = markets[:limit]
        else:
            response = await self._http.get(
                f"{self.base_url}/markets",
                params={"limit": min(max(limit, 1), 1000), "status": "open"},
            )
            response.raise_for_status()
            payload = response.json()
            markets = [
                self._market_from_api(item)
                for item in payload.get("markets", [])
                if isinstance(item, dict) and item.get("ticker")
            ]
            markets.sort(key=lambda m: (m.liquidity, m.volume), reverse=True)
            markets = markets[:limit]
        self._market_cache.update({market.market_id: market for market in markets})
        return markets

    def _market_from_api(self, item: dict[str, Any]) -> Market:
        return Market(
            venue=Venue.KALSHI,
            market_id=str(item["ticker"]),
            title=str(item.get("title") or item.get("ticker")),
            active=str(item.get("status", "open")).lower() in {"open", "active"},
            liquidity=decimal_or_zero(item.get("liquidity")) / CENTS,
            volume=decimal_or_zero(item.get("volume")),
            metadata={
                "source": "network",
                "environment": self.environment,
                "category": infer_category(item),
                "event_ticker": item.get("event_ticker"),
                "event_title": item.get("event_title"),
                "subtitle": item.get("subtitle"),
                "resolution_text": " ".join(
                    str(item.get(field, ""))
                    for field in ("rules_primary", "rules_secondary")
                    if item.get(field)
                ),
                "yes_bid": item.get("yes_bid"),
                "yes_ask": item.get("yes_ask"),
                "raw": item,
            },
        )

    async def get_order_book(self, market: Market) -> OrderBook:
        if self.use_fixtures:
            if not self._fixture_books:
                _, self._fixture_books = load_fixture(FIXTURE_PATH, Venue.KALSHI)
            return self._fixture_books.get(market.market_id, OrderBook(market_id=market.market_id))
        response = await self._http.get(
            f"{self.base_url}/markets/{market.market_id}/orderbook",
            params={"depth": 10},
        )
        response.raise_for_status()
        book = response.json().get("orderbook") or {}
        # Kalshi returns two *bid* ladders in cents: YES bids and NO bids.
        # A NO bid at c cents is a YES ask at (100 - c) cents.
        bids: list[PriceLevel] = []
        asks: list[PriceLevel] = []
        for price_cents, size in book.get("yes") or []:
            price = _cents_to_probability(price_cents)
            if price is not None:
                bids.append(PriceLevel(price, decimal_or_zero(size)))
        for price_cents, size in book.get("no") or []:
            price = _cents_to_probability(price_cents)
            if price is not None:
                asks.append(PriceLevel(ONE - price, decimal_or_zero(size)))
        return OrderBook(market_id=market.market_id, bids=tuple(bids), asks=tuple(asks))

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()
