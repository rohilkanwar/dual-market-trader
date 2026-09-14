"""Polymarket adapter: fixture books, public Gamma/CLOB read-only data, paper fills.

Only unauthenticated endpoints are used (Gamma ``/markets`` and CLOB
``/book``). Credentials, L2 signing and signed order construction are
fail-closed stubs; no wallet key is ever read by this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from core.types import Market, MarketGroup, OrderBook, PriceLevel, Venue
from core.venue import VenueClient
from venues.fixtures import decimal_or_zero, load_fixture
from venues.paper import FeeSchedule, PaperExecutionMixin, zero_fee
from venues.polymarket.fees import taker_fee_rate

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "markets.json"
EVENT_FIXTURE_PATH = Path(__file__).with_name("fixtures") / "events.json"
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
BOOKS_BATCH_SIZE = 200
DEFAULT_TICK_SIZE = Decimal("0.001")
DEFAULT_MIN_ORDER_SIZE = Decimal("5")
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
    if item.get("yes_token_id") and item.get("no_token_id"):
        return [str(item["yes_token_id"]), str(item["no_token_id"])]
    raw = item.get("clobTokenIds") or item.get("clob_token_ids") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [part.strip() for part in raw.strip("[]").split(",") if part.strip()]
    return [str(token) for token in raw]


@dataclass(slots=True)
class EventSnapshot:
    """Multi-outcome events with a YES *and* a NO book for every leg."""

    groups: list[MarketGroup] = field(default_factory=list)
    yes_books: dict[str, OrderBook] = field(default_factory=dict)
    no_books: dict[str, OrderBook] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def markets(self) -> list[Market]:
        return [market for group in self.groups for market in group.markets]


def _book_from_levels(market_id: str, payload: dict[str, Any]) -> OrderBook:
    def levels(key: str) -> tuple[PriceLevel, ...]:
        out = []
        for level in payload.get(key) or []:
            if isinstance(level, dict):
                price, size = level.get("price"), level.get("size")
            elif isinstance(level, (list, tuple)) and len(level) == 2:
                price, size = level
            else:
                continue
            out.append(PriceLevel(decimal_or_zero(price), decimal_or_zero(size)))
        return tuple(out)

    return OrderBook(market_id=market_id, bids=levels("bids"), asks=levels("asks"))


def _leg_market(item: dict[str, Any], event: dict[str, Any], *, source: str) -> Market | None:
    token_ids = _token_ids(item)
    market_id = str(item.get("conditionId") or item.get("market_id") or item.get("id") or "")
    if not market_id or len(token_ids) < 2:
        return None
    fees_enabled = item.get("feesEnabled", item.get("fees_enabled", event.get("fees_enabled")))
    fee_type = item.get("feeType", item.get("fee_type", event.get("fee_type")))
    neg_risk = bool(item.get("negRisk", item.get("neg_risk", event.get("negRisk", event.get("neg_risk", False)))))
    return Market(
        venue=Venue.POLYMARKET,
        market_id=market_id,
        title=str(item.get("question") or item.get("title") or market_id),
        active=bool(item.get("active", True)) and not bool(item.get("closed", False)),
        liquidity=decimal_or_zero(item.get("liquidityNum") or item.get("liquidity")),
        volume=decimal_or_zero(item.get("volumeNum") or item.get("volume")),
        yes_token_id=token_ids[0],
        no_token_id=token_ids[1],
        metadata={
            "source": source,
            "category": infer_category(item) or infer_category(event),
            "slug": item.get("slug"),
            "event_id": str(event.get("id") or event.get("event_id") or ""),
            "event_slug": event.get("slug"),
            "event_title": event.get("title"),
            "group_item_title": item.get("groupItemTitle") or item.get("group_item_title"),
            "neg_risk": neg_risk,
            "neg_risk_market_id": item.get("negRiskMarketID") or event.get("negRiskMarketID"),
            "neg_risk_augmented": bool(event.get("negRiskAugmented", event.get("neg_risk_augmented", False))),
            "fees_enabled": bool(fees_enabled) if fees_enabled is not None else False,
            "fee_type": fee_type,
            "taker_fee_rate": str(taker_fee_rate(fee_type, bool(fees_enabled) if fees_enabled is not None else False)),
            "tick_size": str(decimal_or_zero(item.get("orderPriceMinTickSize") or item.get("tick_size")) or DEFAULT_TICK_SIZE),
            "min_order_size": str(decimal_or_zero(item.get("orderMinSize") or item.get("min_order_size")) or DEFAULT_MIN_ORDER_SIZE),
            "end_date": item.get("endDate") or event.get("endDate") or event.get("end_date"),
            "resolution_text": item.get("description", ""),
        },
    )


def group_from_event(event: dict[str, Any], *, source: str) -> MarketGroup | None:
    """Build a :class:`MarketGroup` from a Gamma ``/events`` item (or fixture)."""
    raw_markets = event.get("markets") or []
    legs = []
    for item in raw_markets:
        if not isinstance(item, dict):
            continue
        if source == "network" and (
            not item.get("active", True)
            or item.get("closed", False)
            or item.get("enableOrderBook") is False
            or item.get("acceptingOrders") is False
        ):
            continue
        market = _leg_market(item, event, source=source)
        if market is not None:
            legs.append(market)
    group_id = str(event.get("id") or event.get("event_id") or event.get("slug") or "")
    if not group_id or not legs:
        return None
    neg_risk = bool(event.get("negRisk", event.get("neg_risk", False)))
    return MarketGroup(
        venue=Venue.POLYMARKET,
        group_id=group_id,
        title=str(event.get("title") or event.get("slug") or group_id),
        markets=tuple(legs),
        # NegRisk events guarantee exactly one YES via the adapter; anything
        # else must say so explicitly (fixtures may set "exclusive": true).
        exclusive=neg_risk or bool(event.get("exclusive", False)),
        convertible=neg_risk,
        augmented=bool(event.get("negRiskAugmented", event.get("neg_risk_augmented", False))),
        metadata={
            "source": source,
            "slug": event.get("slug"),
            "neg_risk": neg_risk,
            "neg_risk_market_id": event.get("negRiskMarketID") or event.get("neg_risk_market_id"),
            "end_date": event.get("endDate") or event.get("end_date"),
            "liquidity": str(decimal_or_zero(event.get("liquidity"))),
            "volume": str(decimal_or_zero(event.get("volume"))),
            "listed_markets": len(raw_markets),
            "scenario": event.get("scenario"),
        },
    )


def load_event_fixture(path: Path = EVENT_FIXTURE_PATH) -> EventSnapshot:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    snapshot = EventSnapshot()
    for event in payload.get("events", []):
        group = group_from_event(event, source="fixture")
        if group is not None:
            snapshot.groups.append(group)
    for market_id, books in payload.get("order_books", {}).items():
        snapshot.yes_books[market_id] = _book_from_levels(market_id, books.get("yes", {}))
        snapshot.no_books[market_id] = _book_from_levels(market_id, books.get("no", {}))
    for market in snapshot.markets:
        snapshot.yes_books.setdefault(market.market_id, OrderBook(market_id=market.market_id))
        snapshot.no_books.setdefault(market.market_id, OrderBook(market_id=market.market_id))
    return snapshot


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

    # ------------------------------------------------------------ events
    async def list_events(self, *, limit: int = 20) -> list[MarketGroup]:
        """Top events by liquidity (Gamma ``/events``), each as a MarketGroup."""
        if self.use_fixtures:
            groups = load_event_fixture().groups[:limit]
        else:
            response = await self._http.get(
                f"{GAMMA_URL}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": min(max(limit, 1), 100),
                    "order": "liquidity",
                    "ascending": "false",
                },
            )
            response.raise_for_status()
            groups = []
            for event in response.json():
                if not isinstance(event, dict):
                    continue
                group = group_from_event(event, source="network")
                if group is not None:
                    groups.append(group)
            groups = groups[:limit]
        for group in groups:
            self._market_cache.update({market.market_id: market for market in group.markets})
        return groups

    async def get_books(self, token_ids: list[str]) -> dict[str, OrderBook]:
        """Batch CLOB books keyed by token id (``POST /books``, chunked)."""
        if self.use_fixtures:
            snapshot = load_event_fixture()
            by_token: dict[str, OrderBook] = {}
            for market in snapshot.markets:
                if market.yes_token_id:
                    by_token[market.yes_token_id] = snapshot.yes_books[market.market_id]
                if market.no_token_id:
                    by_token[market.no_token_id] = snapshot.no_books[market.market_id]
            return {token: by_token[token] for token in token_ids if token in by_token}
        books: dict[str, OrderBook] = {}
        for start in range(0, len(token_ids), BOOKS_BATCH_SIZE):
            chunk = token_ids[start : start + BOOKS_BATCH_SIZE]
            response = await self._http.post(
                f"{CLOB_URL}/books", json=[{"token_id": token} for token in chunk]
            )
            response.raise_for_status()
            for payload in response.json():
                if not isinstance(payload, dict):
                    continue
                token = str(payload.get("asset_id") or "")
                if token:
                    books[token] = _book_from_levels(str(payload.get("market") or token), payload)
        return books

    async def capture_events(self, *, limit: int = 20) -> EventSnapshot:
        """Events plus both books per leg; network failures land in ``errors``."""
        snapshot = EventSnapshot()
        try:
            snapshot.groups = await self.list_events(limit=limit)
        except Exception as exc:  # a dead endpoint must not kill the run
            snapshot.errors.append(f"list_events: {type(exc).__name__}: {exc}")
            return snapshot
        tokens = [
            token
            for market in snapshot.markets
            for token in (market.yes_token_id, market.no_token_id)
            if token
        ]
        try:
            by_token = await self.get_books(tokens)
        except Exception as exc:
            snapshot.errors.append(f"books: {type(exc).__name__}: {exc}")
            by_token = {}
        for market in snapshot.markets:
            yes = by_token.get(market.yes_token_id or "")
            no = by_token.get(market.no_token_id or "")
            if yes is None or no is None:
                snapshot.errors.append(f"book_missing[{market.market_id}]")
            snapshot.yes_books[market.market_id] = (
                OrderBook(market_id=market.market_id, bids=yes.bids, asks=yes.asks)
                if yes is not None
                else OrderBook(market_id=market.market_id)
            )
            snapshot.no_books[market.market_id] = (
                OrderBook(market_id=market.market_id, bids=no.bids, asks=no.asks)
                if no is not None
                else OrderBook(market_id=market.market_id)
            )
        return snapshot

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()
