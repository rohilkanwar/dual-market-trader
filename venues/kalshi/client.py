"""Kalshi adapter: fixture books, public read-only market data, paper fills.

Only unauthenticated endpoints are called:

* ``GET /markets?series_ticker=...&status=open``
* ``GET /markets/{ticker}/orderbook``
* ``GET /events/{event_ticker}`` (category, settlement sources, mutual exclusivity)
* ``GET /series/{series_ticker}`` (fee type + multiplier, used by the FLB fee model)

No API key is required for those. The authenticated signer and live order
routing are deliberately stubs that raise; see ``docs/ASSUMPTIONS.md``.

Network payload notes (verified against the public API on 2026-09-14):

* Prices arrive as dollar strings (``yes_bid_dollars``, ``orderbook_fp``);
  legacy integer-cent fields are still parsed when present.
* ``liquidity_dollars`` is unpopulated (``0.0000``) on the listing, so markets
  are ranked by two-sided quote presence, then ``volume_fp`` / open interest.
* The unfiltered ``/markets`` listing is dominated by zero-liquidity
  multi-leg "MVE" combos; the canary therefore lists explicit macro series.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from core.types import ONE, ZERO, Market, OrderBook, PriceLevel, Venue
from core.venue import VenueClient
from venues.fixtures import decimal_or_zero, load_fixture
from venues.paper import FeeSchedule, PaperExecutionMixin, kalshi_fee

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "markets.json"
HOSTS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
}
CENTS = Decimal("100")
# Macro series used for the single-venue Kalshi canary. Verified to exist and
# quote two-sided on the public API; adjust with ``series_tickers=``.
DEFAULT_MACRO_SERIES: tuple[str, ...] = (
    "KXFEDDECISION",
    "KXFED",
    "KXCPIYOY",
    "KXCPI",
    "KXCPICORE",
    "KXPAYROLLS",
    "KXGDP",
    "KXU3",
)
_CATEGORY_MAP = {
    "economics": "macro",
    "financials": "macro",
    "politics": "politics",
    "sports": "sports",
    "exotics": "exotics",
}
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


def infer_category(item: dict[str, Any], event_category: str | None = None) -> str:
    explicit = str(event_category or item.get("category") or "").strip().lower()
    if explicit:
        if explicit in _CATEGORY_MAP:
            return _CATEGORY_MAP[explicit]
        if any(h in explicit for h in ("econom", "financ", "fed")):
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


def _price(item: dict[str, Any], dollars_key: str, cents_key: str) -> Decimal | None:
    """Read a probability from the dollar-string field, else the legacy cents field."""
    dollars = item.get(dollars_key)
    if dollars not in (None, ""):
        value = decimal_or_zero(dollars)
        return value if ZERO <= value <= ONE else None
    cents = item.get(cents_key)
    if cents in (None, ""):
        return None
    value = decimal_or_zero(cents) / CENTS
    return value if ZERO <= value <= ONE else None


def _level_price(raw: Any, *, dollars: bool) -> Decimal | None:
    value = decimal_or_zero(raw)
    if not dollars:
        value = value / CENTS
    return value if ZERO <= value <= ONE else None


def _rank_key(market: Market) -> tuple[int, Decimal, Decimal]:
    bid = market.metadata.get("yes_bid")
    ask = market.metadata.get("yes_ask")
    two_sided = int(bid is not None and ask is not None and ZERO < bid < ask < ONE)
    return (two_sided, market.volume, decimal_or_zero(market.metadata.get("open_interest")))


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
        series_tickers: tuple[str, ...] | None = DEFAULT_MACRO_SERIES,
        timeout: float = 15.0,
        fixture_path: Path = FIXTURE_PATH,
    ) -> None:
        self.paper = paper
        self.use_fixtures = use_fixtures
        self.fixture_path = fixture_path
        self.environment = (environment or os.getenv("KALSHI_ENV") or "demo").lower()
        if self.environment not in HOSTS:
            raise ValueError(f"KALSHI_ENV must be one of {sorted(HOSTS)}")
        self.base_url = HOSTS[self.environment]
        self.signer = signer
        self.series_tickers = series_tickers
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._fixture_books: dict[str, OrderBook] = {}
        self._event_cache: dict[str, dict[str, Any]] = {}
        self._series_cache: dict[str, dict[str, Any]] = {}
        self._init_paper(fee_schedule or kalshi_fee)

    # ------------------------------------------------------------ market data
    async def list_markets(self, *, limit: int = 20) -> list[Market]:
        if self.use_fixtures:
            markets, books = load_fixture(self.fixture_path, Venue.KALSHI)
            self._fixture_books = books
            markets = markets[:limit]
        else:
            raw_items = await self._fetch_market_items(limit)
            markets = [await self._market_from_api(item) for item in raw_items]
            markets.sort(key=_rank_key, reverse=True)
            markets = markets[:limit]
        self._market_cache.update({market.market_id: market for market in markets})
        return markets

    async def _fetch_market_items(self, limit: int) -> list[dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}
        if self.series_tickers:
            for series in self.series_tickers:
                response = await self._http.get(
                    f"{self.base_url}/markets",
                    params={"series_ticker": series, "status": "open", "limit": 200},
                )
                response.raise_for_status()
                for item in response.json().get("markets", []):
                    if isinstance(item, dict) and item.get("ticker"):
                        items[str(item["ticker"])] = {**item, "series_ticker": series}
        else:
            response = await self._http.get(
                f"{self.base_url}/markets",
                params={"limit": min(max(limit * 10, 100), 1000), "status": "open"},
            )
            response.raise_for_status()
            for item in response.json().get("markets", []):
                if isinstance(item, dict) and item.get("ticker"):
                    items[str(item["ticker"])] = item
        return list(items.values())

    async def _event(self, event_ticker: str | None) -> dict[str, Any]:
        if not event_ticker:
            return {}
        if event_ticker not in self._event_cache:
            try:
                response = await self._http.get(f"{self.base_url}/events/{event_ticker}")
                response.raise_for_status()
                self._event_cache[event_ticker] = response.json().get("event") or {}
            except httpx.HTTPError as exc:
                self._event_cache[event_ticker] = {"error": f"{type(exc).__name__}: {exc}"}
        return self._event_cache[event_ticker]

    async def _series(self, series_ticker: str | None) -> dict[str, Any]:
        if not series_ticker:
            return {}
        if series_ticker not in self._series_cache:
            try:
                response = await self._http.get(f"{self.base_url}/series/{series_ticker}")
                response.raise_for_status()
                self._series_cache[series_ticker] = response.json().get("series") or {}
            except httpx.HTTPError as exc:
                self._series_cache[series_ticker] = {"error": f"{type(exc).__name__}: {exc}"}
        return self._series_cache[series_ticker]

    async def _market_from_api(self, item: dict[str, Any]) -> Market:
        event = await self._event(item.get("event_ticker"))
        series_ticker = item.get("series_ticker") or event.get("series_ticker")
        series = await self._series(series_ticker)
        sources = [s for s in (event.get("settlement_sources") or []) if isinstance(s, dict)]
        source_url = next((str(s.get("url")) for s in sources if s.get("url")), None)
        rules = " ".join(
            str(item.get(field, "")) for field in ("rules_primary", "rules_secondary") if item.get(field)
        )
        if sources:
            rules = f"{rules} Settlement source: " + ", ".join(
                f"{s.get('name', '')} {s.get('url', '')}".strip() for s in sources
            )
        yes_bid = _price(item, "yes_bid_dollars", "yes_bid")
        yes_ask = _price(item, "yes_ask_dollars", "yes_ask")
        return Market(
            venue=Venue.KALSHI,
            market_id=str(item["ticker"]),
            title=str(item.get("title") or item.get("ticker")),
            active=str(item.get("status", "active")).lower() in {"open", "active"},
            liquidity=(
                decimal_or_zero(item.get("liquidity_dollars"))
                if item.get("liquidity_dollars") not in (None, "")
                else decimal_or_zero(item.get("liquidity")) / CENTS
            ),
            volume=(
                decimal_or_zero(item.get("volume_fp"))
                if item.get("volume_fp") not in (None, "")
                else decimal_or_zero(item.get("volume"))
            ),
            metadata={
                "source": "network",
                "environment": self.environment,
                "category": infer_category(item, event.get("category") or series.get("category")),
                "event_category": event.get("category") or series.get("category"),
                "event_ticker": item.get("event_ticker"),
                "event_title": event.get("title") or item.get("event_title"),
                "mutually_exclusive": event.get("mutually_exclusive"),
                "series_ticker": series_ticker,
                "fee_type": series.get("fee_type"),
                "fee_multiplier": series.get("fee_multiplier"),
                "subtitle": item.get("yes_sub_title") or item.get("subtitle"),
                "event_sub_title": event.get("sub_title"),
                "status": item.get("status"),
                "result": item.get("result"),
                # Sports series: ``occurrence_datetime`` is the scheduled session
                # (verified 2026-09-14: several same-day matches share one value),
                # not the exact first serve; the outside feed's commence_time wins.
                "occurrence_datetime": item.get("occurrence_datetime"),
                "expected_expiration_time": item.get("expected_expiration_time"),
                "resolution_text": rules.strip(),
                "source_url": source_url,
                "settlement_sources": sources,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "open_interest": item.get("open_interest_fp") or item.get("open_interest"),
                "close_time": item.get("close_time"),
                "raw": item,
            },
        )

    async def get_market_status(self, ticker: str) -> dict[str, Any]:
        """Public ``GET /markets/{ticker}``: ``status`` / ``result`` for settlement checks."""
        response = await self._http.get(f"{self.base_url}/markets/{ticker}")
        response.raise_for_status()
        item = response.json().get("market") or {}
        return {
            "status": item.get("status"),
            "result": item.get("result"),
            "settlement_value": item.get("settlement_value_dollars") or item.get("settlement_value"),
            "close_time": item.get("close_time"),
        }

    async def get_order_book(self, market: Market) -> OrderBook:
        if self.use_fixtures:
            if not self._fixture_books:
                _, self._fixture_books = load_fixture(self.fixture_path, Venue.KALSHI)
            return self._fixture_books.get(market.market_id, OrderBook(market_id=market.market_id))
        response = await self._http.get(
            f"{self.base_url}/markets/{market.market_id}/orderbook",
            params={"depth": 10},
        )
        response.raise_for_status()
        payload = response.json()
        # Kalshi returns two *bid* ladders: YES bids and NO bids. A NO bid at
        # price p is a YES ask at 1 - p. ``orderbook_fp`` carries dollar
        # strings; the legacy ``orderbook`` carries integer cents.
        if isinstance(payload.get("orderbook_fp"), dict):
            book, yes_key, no_key, dollars = payload["orderbook_fp"], "yes_dollars", "no_dollars", True
        else:
            book, yes_key, no_key, dollars = payload.get("orderbook") or {}, "yes", "no", False
        bids: list[PriceLevel] = []
        asks: list[PriceLevel] = []
        for raw_price, size in book.get(yes_key) or []:
            price = _level_price(raw_price, dollars=dollars)
            if price is not None:
                bids.append(PriceLevel(price, decimal_or_zero(size)))
        for raw_price, size in book.get(no_key) or []:
            price = _level_price(raw_price, dollars=dollars)
            if price is not None:
                asks.append(PriceLevel(ONE - price, decimal_or_zero(size)))
        return OrderBook(market_id=market.market_id, bids=tuple(bids), asks=tuple(asks))

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()
