"""Data sources for the weather calibration track: free Open-Meteo models and Polymarket's weather events.

Open-Meteo (https://open-meteo.com, free for non-commercial use, no key):

* ``/v1/forecast`` with ``models=<list>`` returns every listed model's own
  hourly 2 m temperature; the day-ahead **maximum** is the max over the target
  day's 24 hourly values in the city's time zone (the same construction the
  markets settle on: the highest hourly reading).
* ``previous-runs-api`` exposes ``temperature_2m_previous_day1`` — the value the
  run issued one day earlier predicted for that hour — for up to 92 past days.
  That is the day-ahead forecast archive the calibration store is filled from.

The optional paid endpoint (``customer-*.open-meteo.com`` + ``apikey``) is
only used when the caller passes ``allow_paid_keys=True`` **and**
``OPEN_METEO_API_KEY`` is set. Default: free hosts, no key.

Polymarket (public Gamma ``/events?tag_id=104596`` + CLOB ``/books``): open
and closed "Highest temperature in <city> on <date>?" events, one YES book per
bucket leg, and the settled leg (``outcomePrices == ["1","0"]``) on closed
events, which is the calibration truth.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx

from core.types import Market, OrderBook, PriceLevel, Venue
from strategies.weather_types import (
    POLYMARKET_WEATHER_TAG,
    CityRegistry,
    CityStation,
    WeatherEvent,
    WeatherLeg,
    bucket_from_market,
    parse_bucket_label,
    parse_target_date,
    parse_weather_event_title,
)
from venues.fixtures import decimal_or_zero
from venues.polymarket.client import CLOB_URL, GAMMA_URL, PolymarketClient

OPEN_METEO_API_KEY_ENV = "OPEN_METEO_API_KEY"
FREE_FORECAST_HOST = "https://api.open-meteo.com"
FREE_PREVIOUS_RUNS_HOST = "https://previous-runs-api.open-meteo.com"
PAID_FORECAST_HOST = "https://customer-api.open-meteo.com"
PAID_PREVIOUS_RUNS_HOST = "https://customer-previous-runs-api.open-meteo.com"
PREVIOUS_RUNS_MAX_PAST_DAYS = 92
USER_AGENT = "dual-market-trader-paper/0.2 (+paper-only research; weather calibration)"
FREE_TIER_NOTE = "Open-Meteo free tier: non-commercial use, no key, ~10,000 calls/day; one call per city per run."


def _now() -> datetime:
    return datetime.now(UTC)


def _daily_max(hourly: dict[str, Any], variable: str, models: tuple[str, ...]) -> dict[date, dict[str, float]]:
    """``{local_date: {model: max hourly value}}`` from an Open-Meteo hourly block."""
    times = hourly.get("time") or []
    out: dict[date, dict[str, float]] = defaultdict(dict)
    for model in models:
        key = f"{variable}_{model}" if len(models) > 1 or f"{variable}_{model}" in hourly else variable
        values = hourly.get(key)
        if values is None:
            continue
        by_day: dict[date, list[float]] = defaultdict(list)
        for stamp, value in zip(times, values):
            if value is None:
                continue
            by_day[date.fromisoformat(str(stamp)[:10])].append(float(value))
        for day, vals in by_day.items():
            if len(vals) >= 20:  # a day with fewer hourly points is not a daily maximum
                out[day][model] = max(vals)
    return dict(out)


# --------------------------------------------------------------------------
# Forecast sources
# --------------------------------------------------------------------------
class ForecastSource(Protocol):
    name: str

    async def day_max(self, city: CityStation, target: date) -> dict[str, float]: ...

    async def previous_day1_history(self, city: CityStation, *, past_days: int) -> dict[date, dict[str, float]]: ...


class OpenMeteoSource:
    """Free Open-Meteo per-model forecasts; paid host only when explicitly allowed."""

    name = "open_meteo"

    def __init__(
        self,
        *,
        models: tuple[str, ...],
        http: httpx.AsyncClient | None = None,
        allow_paid_keys: bool = False,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.models = models
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT})
        key = api_key if api_key is not None else os.getenv(OPEN_METEO_API_KEY_ENV)
        self.api_key = key if (allow_paid_keys and key) else None
        self.paid_key_used = self.api_key is not None
        self.calls = 0
        self.errors: list[str] = []

    @property
    def forecast_host(self) -> str:
        return PAID_FORECAST_HOST if self.api_key else FREE_FORECAST_HOST

    @property
    def previous_runs_host(self) -> str:
        return PAID_PREVIOUS_RUNS_HOST if self.api_key else FREE_PREVIOUS_RUNS_HOST

    def _params(self, city: CityStation, **extra: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "latitude": city.latitude,
            "longitude": city.longitude,
            "timezone": city.timezone,
            "temperature_unit": "fahrenheit" if city.unit == "F" else "celsius",
            "models": ",".join(self.models),
            **extra,
        }
        if self.api_key:
            params["apikey"] = self.api_key
        return params

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            self.calls += 1
            response = await self._http.get(url, params=params)
            if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                await asyncio.sleep(1.5)
                continue
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("error"):
                raise ValueError(str(payload.get("reason") or payload))
            return payload
        raise RuntimeError("unreachable")

    async def day_max(self, city: CityStation, target: date) -> dict[str, float]:
        payload = await self._get(
            f"{self.forecast_host}/v1/forecast",
            self._params(city, hourly="temperature_2m", start_date=target.isoformat(), end_date=target.isoformat()),
        )
        return _daily_max(payload.get("hourly") or {}, "temperature_2m", self.models).get(target, {})

    async def previous_day1_history(self, city: CityStation, *, past_days: int) -> dict[date, dict[str, float]]:
        days = max(1, min(past_days, PREVIOUS_RUNS_MAX_PAST_DAYS))
        payload = await self._get(
            f"{self.previous_runs_host}/v1/forecast",
            self._params(city, hourly="temperature_2m_previous_day1", past_days=days, forecast_days=1),
        )
        return _daily_max(payload.get("hourly") or {}, "temperature_2m_previous_day1", self.models)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "models": list(self.models),
            "forecast_host": self.forecast_host,
            "previous_runs_host": self.previous_runs_host,
            "paid_key_used": self.paid_key_used,
            "calls": self.calls,
            "errors": self.errors,
            "note": FREE_TIER_NOTE,
        }

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()


class StaticForecastSource:
    """Fixture / operator forecasts keyed by ``"<city_key>:<YYYY-MM-DD>"``."""

    name = "fixture"

    def __init__(self, forecasts: dict[str, dict[str, float]], history: dict[str, dict[str, float]] | None = None) -> None:
        self.forecasts = forecasts
        self.history = history or {}
        self.calls = 0
        self.paid_key_used = False
        self.errors: list[str] = []

    async def day_max(self, city: CityStation, target: date) -> dict[str, float]:
        self.calls += 1
        return dict(self.forecasts.get(f"{city.key}:{target.isoformat()}", {}))

    async def previous_day1_history(self, city: CityStation, *, past_days: int) -> dict[date, dict[str, float]]:
        self.calls += 1
        out: dict[date, dict[str, float]] = {}
        for key, values in self.history.items():
            c, _, day = key.partition(":")
            if c == city.key:
                out[date.fromisoformat(day)] = dict(values)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "models": [], "paid_key_used": False, "calls": self.calls, "errors": self.errors, "note": "fixture forecasts; synthetic"}

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# Polymarket weather universe
# --------------------------------------------------------------------------
class WeatherUniverse(Protocol):
    name: str

    async def open_events(self, *, limit: int) -> list[WeatherEvent]: ...

    async def closed_events(self, *, start: date, end: date, limit: int) -> list[WeatherEvent]: ...

    async def event_by_slug(self, slug: str) -> WeatherEvent | None: ...

    async def books(self, events: list[WeatherEvent]) -> dict[str, OrderBook]: ...


def _leg_resolution(item: dict[str, Any]) -> bool | None:
    prices = item.get("outcomePrices") or item.get("outcome_prices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            prices = []
    if not item.get("closed") or not isinstance(prices, list) or len(prices) != 2:
        return None
    try:
        yes = float(prices[0])
    except (TypeError, ValueError):
        return None
    if yes >= 0.999:
        return True
    if yes <= 0.001:
        return False
    return None


def event_from_gamma(raw: dict[str, Any], *, registry: CityRegistry, client: PolymarketClient) -> WeatherEvent | None:
    """Parse one Gamma ``/events`` item into a :class:`WeatherEvent` (or ``None`` if it is not one)."""
    title = str(raw.get("title") or "")
    parsed = parse_weather_event_title(title)
    if parsed is None:
        return None
    city_name = parsed[0]
    slug = str(raw.get("slug") or "")
    target = parse_target_date(title, slug=slug, end_date=raw.get("endDate"))
    if target is None:
        return None
    legs: list[WeatherLeg] = []
    for item in raw.get("markets") or []:
        if not isinstance(item, dict):
            continue
        market = client._market_from_item(item, raw, discovered_by=f"tag:{POLYMARKET_WEATHER_TAG}")
        if market is None:
            continue
        market = replace(
            market,
            metadata={**market.metadata, "group_item_title": item.get("groupItemTitle"), "event_slug": slug, "category": "weather"},
        )
        bucket = bucket_from_market(market)
        if bucket is None:
            continue
        legs.append(WeatherLeg(market=market, bucket=bucket, resolved=_leg_resolution(item)))
    if not legs:
        return None
    return WeatherEvent(
        slug=slug,
        event_id=str(raw.get("id") or slug),
        title=title,
        city_name=city_name,
        city=registry.resolve(city_name),
        target_date=target,
        legs=tuple(sorted(legs, key=lambda leg: (leg.bucket.lo is not None, leg.bucket.lo if leg.bucket.lo is not None else 0))),
        closed=bool(raw.get("closed", False)),
        metadata={"volume": raw.get("volume"), "liquidity": raw.get("liquidity"), "start_date": raw.get("startDate"), "end_date": raw.get("endDate")},
    )


class PolymarketWeatherUniverse:
    """Public Gamma + CLOB reads, no keys; ``paper=True`` client, never places orders."""

    name = "polymarket_gamma"

    def __init__(self, *, registry: CityRegistry, http: httpx.AsyncClient | None = None, timeout: float = 30.0) -> None:
        self.registry = registry
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT})
        self.client = PolymarketClient(paper=True, use_fixtures=False, http=self._http, search_terms=None)
        self.calls = 0
        self.errors: list[str] = []

    async def _events(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls += 1
        response = await self._http.get(f"{GAMMA_URL}/events", params=params)
        response.raise_for_status()
        payload = response.json()
        return [e for e in payload if isinstance(e, dict)] if isinstance(payload, list) else []

    async def open_events(self, *, limit: int) -> list[WeatherEvent]:
        out: list[WeatherEvent] = []
        seen: set[str] = set()
        offset = 0
        while len(out) < limit and offset < 1000:
            page = await self._events(
                {"tag_id": POLYMARKET_WEATHER_TAG, "closed": "false", "active": "true", "limit": 100, "offset": offset, "order": "startDate", "ascending": "false"}
            )
            if not page:
                break
            for raw in page:
                event = event_from_gamma(raw, registry=self.registry, client=self.client)
                if event is not None and event.slug not in seen:
                    seen.add(event.slug)
                    out.append(event)
            offset += len(page)
            if len(page) < 100:
                break
        return out[:limit]

    async def closed_events(self, *, start: date, end: date, limit: int) -> list[WeatherEvent]:
        """Closed events whose end date falls in ``[start, end]``, paged by one-week windows."""
        out: list[WeatherEvent] = []
        seen: set[str] = set()
        window_start = start
        while window_start <= end and len(out) < limit:
            window_end = min(end, window_start + timedelta(days=6))
            offset = 0
            while len(out) < limit:
                page = await self._events(
                    {
                        "tag_id": POLYMARKET_WEATHER_TAG,
                        "closed": "true",
                        "limit": 100,
                        "offset": offset,
                        "end_date_min": f"{window_start.isoformat()}T00:00:00Z",
                        "end_date_max": f"{(window_end + timedelta(days=1)).isoformat()}T00:00:00Z",
                    }
                )
                if not page:
                    break
                for raw in page:
                    event = event_from_gamma(raw, registry=self.registry, client=self.client)
                    if event is not None and event.slug not in seen and start <= event.target_date <= end:
                        seen.add(event.slug)
                        out.append(event)
                offset += len(page)
                if len(page) < 100 or offset >= 1500:
                    break
            window_start = window_end + timedelta(days=1)
        return out[:limit]

    async def event_by_slug(self, slug: str) -> WeatherEvent | None:
        page = await self._events({"slug": slug})
        if not page:
            return None
        return event_from_gamma(page[0], registry=self.registry, client=self.client)

    async def books(self, events: list[WeatherEvent]) -> dict[str, OrderBook]:
        """Batched CLOB YES books for every leg (``POST /books``, chunked by the client)."""
        tokens = [leg.market.yes_token_id for event in events for leg in event.legs if leg.market.yes_token_id]
        if not tokens:
            return {}
        self.calls += 1
        by_token = await self.client.get_books(tokens)
        out: dict[str, OrderBook] = {}
        for event in events:
            for leg in event.legs:
                book = by_token.get(leg.market.yes_token_id or "")
                out[leg.market_id] = (
                    OrderBook(market_id=leg.market_id, bids=book.bids, asks=book.asks, timestamp=book.timestamp)
                    if book is not None
                    else OrderBook(market_id=leg.market_id)
                )
        return out

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "gamma": GAMMA_URL, "clob": CLOB_URL, "tag_id": POLYMARKET_WEATHER_TAG, "calls": self.calls, "errors": self.errors}

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()


# --------------------------------------------------------------------------
# Fixture universe (replay steps)
# --------------------------------------------------------------------------
def event_from_fixture(item: dict[str, Any], *, registry: CityRegistry) -> tuple[WeatherEvent, dict[str, OrderBook]]:
    """Build an event and its YES books from the compact replay-fixture shape.

    ``{"slug", "event_id", "title", "closed", "legs": [{"market_id", "label",
    "bids": [[price, size]], "asks": [[price, size]], "resolved": true|false|null,
    "taker_fee_rate": "0.05"}]}``
    """
    title = str(item["title"])
    parsed = parse_weather_event_title(title)
    if parsed is None:
        raise ValueError(f"fixture event title does not parse: {title!r}")
    slug = str(item.get("slug") or "")
    target = parse_target_date(title, slug=slug, end_date=item.get("end_date"))
    if target is None:
        raise ValueError(f"fixture event {slug!r} has no target date")
    legs: list[WeatherLeg] = []
    books: dict[str, OrderBook] = {}
    for leg in item.get("legs", []):
        label = str(leg["label"])
        bucket = parse_bucket_label(label)
        if bucket is None:
            raise ValueError(f"fixture bucket label does not parse: {label!r}")
        market_id = str(leg["market_id"])
        market = Market(
            venue=Venue.POLYMARKET,
            market_id=market_id,
            title=str(leg.get("question") or f"Will the highest temperature in {parsed[0]} be {label} on {title.split(' on ', 1)[1].rstrip('?')}?"),
            active=not bool(item.get("closed", False)),
            yes_token_id=str(leg.get("yes_token_id") or f"yes-{market_id}"),
            no_token_id=str(leg.get("no_token_id") or f"no-{market_id}"),
            metadata={
                "source": "fixture",
                "category": "weather",
                "slug": leg.get("slug") or market_id,
                "event_slug": slug,
                "group_item_title": label,
                "fee_type": "weather_fees",
                "fees_enabled": True,
                "taker_fee_rate": str(leg.get("taker_fee_rate", "0.05")),
                "neg_risk": True,
            },
        )
        legs.append(WeatherLeg(market=market, bucket=bucket, resolved=leg.get("resolved")))
        books[market_id] = OrderBook(
            market_id=market_id,
            bids=tuple(PriceLevel(decimal_or_zero(p), decimal_or_zero(s)) for p, s in leg.get("bids", [])),
            asks=tuple(PriceLevel(decimal_or_zero(p), decimal_or_zero(s)) for p, s in leg.get("asks", [])),
        )
    event = WeatherEvent(
        slug=slug,
        event_id=str(item.get("event_id") or slug),
        title=title,
        city_name=parsed[0],
        city=registry.resolve(parsed[0]),
        target_date=target,
        legs=tuple(legs),
        closed=bool(item.get("closed", False)),
        metadata={"source": "fixture"},
    )
    return event, books


@dataclass(slots=True)
class FixtureUniverse:
    """One replay step's open events, books and settlement lookups."""

    registry: CityRegistry
    open_items: list[dict[str, Any]] = field(default_factory=list)
    settlement_items: dict[str, dict[str, Any]] = field(default_factory=dict)
    closed_items: list[dict[str, Any]] = field(default_factory=list)
    name: str = "fixture"
    calls: int = 0
    errors: list[str] = field(default_factory=list)

    async def open_events(self, *, limit: int) -> list[WeatherEvent]:
        self.calls += 1
        return [event_from_fixture(item, registry=self.registry)[0] for item in self.open_items][:limit]

    async def closed_events(self, *, start: date, end: date, limit: int) -> list[WeatherEvent]:
        self.calls += 1
        events = [event_from_fixture(item, registry=self.registry)[0] for item in self.closed_items]
        return [e for e in events if start <= e.target_date <= end][:limit]

    async def event_by_slug(self, slug: str) -> WeatherEvent | None:
        self.calls += 1
        item = self.settlement_items.get(slug)
        if item is None:
            return None
        return event_from_fixture(item, registry=self.registry)[0]

    async def books(self, events: list[WeatherEvent]) -> dict[str, OrderBook]:
        out: dict[str, OrderBook] = {}
        for item in self.open_items:
            _, books = event_from_fixture(item, registry=self.registry)
            out.update(books)
        return {leg.market_id: out.get(leg.market_id, OrderBook(market_id=leg.market_id)) for e in events for leg in e.legs}

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "calls": self.calls, "errors": self.errors, "open_events": len(self.open_items), "settlements": len(self.settlement_items)}

    async def close(self) -> None:
        return None


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "FREE_TIER_NOTE",
    "OPEN_METEO_API_KEY_ENV",
    "PREVIOUS_RUNS_MAX_PAST_DAYS",
    "FixtureUniverse",
    "ForecastSource",
    "OpenMeteoSource",
    "PolymarketWeatherUniverse",
    "StaticForecastSource",
    "WeatherUniverse",
    "event_from_fixture",
    "event_from_gamma",
    "load_json",
]
