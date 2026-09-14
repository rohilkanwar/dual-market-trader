"""Polymarket adapter: fixture books, public Gamma/CLOB read-only data, paper fills.

Only unauthenticated endpoints are used (Gamma ``/markets`` and CLOB
``/book``). Credentials, L2 signing and signed order construction are
fail-closed stubs; no wallet key is ever read by this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from core.types import Market, OrderBook, PriceLevel, Venue
from core.venue import VenueClient
from venues.fixtures import decimal_or_zero, load_fixture
from venues.paper import FeeSchedule, PaperExecutionMixin, zero_fee

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "markets.json"
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
_SPORTS_HINTS = ("nba", "nfl", "mlb", "nhl", "ufc", "soccer", "premier league", " vs. ", " vs ")
_MACRO_HINTS = ("fed", "cpi", "inflation", "rate cut", "rate hike", "gdp", "unemployment", "fomc")


@dataclass(frozen=True, slots=True)
class PolymarketCredentials:
    """Shape of the credentials the live path would need. Never populated here."""

    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    funder_address: str = ""
    signature_type: int = 0

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)


class PolymarketL2Signer:
    """Placeholder for CLOB L2 HMAC headers. Intentionally not implemented."""

    def __init__(self, credentials: PolymarketCredentials) -> None:
        self.credentials = credentials

    def headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        del method, path, body
        raise NotImplementedError("Polymarket authenticated requests are stubbed (paper-only)")


class SignedOrderBuilder:
    """Placeholder for EIP-712 order signing. Intentionally not implemented."""

    def __init__(self, credentials: PolymarketCredentials) -> None:
        self.credentials = credentials

    def build(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise NotImplementedError("Polymarket signed orders are stubbed (paper-only)")


def infer_category(item: dict[str, Any]) -> str:
    explicit = str(item.get("category") or "").strip().lower()
    if explicit:
        if any(h in explicit for h in ("econom", "financ", "business", "fed", "crypto")):
            return "macro" if "crypto" not in explicit else "crypto"
        if "sport" in explicit:
            return "sports"
        return explicit
    text = " ".join(str(item.get(k) or "") for k in ("question", "slug", "title")).lower()
    if any(h in text for h in _SPORTS_HINTS):
        return "sports"
    if any(h in text for h in _MACRO_HINTS):
        return "macro"
    return ""


def _token_ids(item: dict[str, Any]) -> list[str]:
    raw = item.get("clobTokenIds") or item.get("clob_token_ids") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [part.strip() for part in raw.strip("[]").split(",") if part.strip()]
    return [str(token) for token in raw]


class PolymarketClient(PaperExecutionMixin, VenueClient):
    venue = Venue.POLYMARKET

    def __init__(
        self,
        *,
        paper: bool = True,
        use_fixtures: bool = True,
        http: httpx.AsyncClient | None = None,
        fee_schedule: FeeSchedule | None = None,
        credentials: PolymarketCredentials | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.paper = paper
        self.use_fixtures = use_fixtures
        self.credentials = credentials or PolymarketCredentials()
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._fixture_books: dict[str, OrderBook] = {}
        self._init_paper(fee_schedule or zero_fee)

    async def list_markets(self, *, limit: int = 20) -> list[Market]:
        if self.use_fixtures:
            markets, books = load_fixture(FIXTURE_PATH, Venue.POLYMARKET)
            self._fixture_books = books
            markets = markets[:limit]
        else:
            response = await self._http.get(
                f"{GAMMA_URL}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": min(max(limit, 1), 500),
                    "order": "liquidityNum",
                    "ascending": "false",
                },
            )
            response.raise_for_status()
            markets = []
            for item in response.json():
                if not isinstance(item, dict):
                    continue
                token_ids = _token_ids(item)
                market_id = str(item.get("conditionId") or item.get("id") or "")
                if not market_id or not token_ids:
                    continue
                events = item.get("events") or []
                event_title = events[0].get("title") if events and isinstance(events[0], dict) else None
                markets.append(
                    Market(
                        venue=Venue.POLYMARKET,
                        market_id=market_id,
                        title=str(item.get("question") or item.get("title") or market_id),
                        active=bool(item.get("active", True)) and not bool(item.get("closed", False)),
                        liquidity=decimal_or_zero(item.get("liquidityNum") or item.get("liquidity")),
                        volume=decimal_or_zero(item.get("volumeNum") or item.get("volume")),
                        yes_token_id=token_ids[0] if token_ids else None,
                        no_token_id=token_ids[1] if len(token_ids) > 1 else None,
                        metadata={
                            "source": "network",
                            "category": infer_category(item),
                            "slug": item.get("slug"),
                            "event_title": event_title,
                            "resolution_text": item.get("description", ""),
                            "raw": item,
                        },
                    )
                )
            markets = markets[:limit]
        self._market_cache.update({market.market_id: market for market in markets})
        return markets

    async def get_order_book(self, market: Market) -> OrderBook:
        if self.use_fixtures:
            if not self._fixture_books:
                _, self._fixture_books = load_fixture(FIXTURE_PATH, Venue.POLYMARKET)
            return self._fixture_books.get(market.market_id, OrderBook(market_id=market.market_id))
        if not market.yes_token_id:
            return OrderBook(market_id=market.market_id)
        response = await self._http.get(f"{CLOB_URL}/book", params={"token_id": market.yes_token_id})
        response.raise_for_status()
        payload = response.json()
        levels = lambda key: tuple(  # noqa: E731
            PriceLevel(decimal_or_zero(level.get("price")), decimal_or_zero(level.get("size")))
            for level in payload.get(key) or []
            if isinstance(level, dict)
        )
        return OrderBook(market_id=market.market_id, bids=levels("bids"), asks=levels("asks"))

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()
