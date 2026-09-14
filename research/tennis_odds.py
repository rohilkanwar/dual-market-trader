"""Free public outside lines for the ``tennis_basis`` paper track.

The documented free source is **The Odds API** (https://the-odds-api.com), an
official, keyed, free-tier odds API:

* free "Starter" plan: 500 credits / month, no card, key by e-mail sign-up;
* ``GET /v4/sports`` is free (no quota) and lists the in-season tennis keys
  (``tennis_atp_*`` / ``tennis_wta_*``; Grand Slams, ATP/WTA 1000 and 500);
* ``GET /v4/sports/{key}/odds?regions=eu&markets=h2h`` costs
  ``markets x regions`` = 1 credit per tournament per call and returns every
  ``eu`` bookmaker's match-winner odds, Pinnacle included (``key = pinnacle``,
  taken from Pinnacle's public site "which may incur a delay");
* the response headers ``x-requests-remaining`` / ``x-requests-used`` are the
  budget; this module refuses to spend below ``min_remaining``.

No other site is scraped. Without ``ODDS_API_KEY`` the network default is
:class:`NullOutsideSource` and the track reports ``no_outside_source`` with
zero candidates; that honest empty is the intended output. An operator can also
supply a saved v4 response (``--odds-file``) which is parsed by the same code.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from strategies.tennis_basis import BasisParameters, BookQuote, ConsensusLine, consensus_line, same_player

LOGGER = logging.getLogger("tennis_odds")

ODDS_API_URL = "https://api.the-odds-api.com/v4"
ODDS_API_KEY_ENV = "ODDS_API_KEY"
DEFAULT_REGIONS = "eu"
TENNIS_GROUP = "Tennis"
FREE_TIER = {
    "provider": "The Odds API (the-odds-api.com)",
    "plan": "Starter (free): 500 credits/month, API key by e-mail sign-up, no payment details",
    "cost_model": "1 credit per market per region per call; GET /v4/sports is free",
    "sharp_book": "pinnacle (eu region; odds from Pinnacle's public website, may be delayed)",
    "terms": "https://the-odds-api.com/liveapi/guides/v4/ ; personal / non-commercial use on the free plan",
}


def _now() -> datetime:
    return datetime.now(UTC)


def parse_time(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=UTC)
    text = str(raw).strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if text.endswith("+00"):
        text += ":00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class OutsideEvent:
    event_id: str
    sport_key: str
    commence_time: datetime | None
    player_a: str
    player_b: str
    quotes: tuple[BookQuote, ...]
    source: str

    def consensus(self, *, parameters: BasisParameters | None = None, as_of: datetime | None = None) -> ConsensusLine:
        return consensus_line(self.quotes, parameters=parameters, as_of=as_of)

    def probability_for(self, player: str, *, parameters: BasisParameters | None = None, as_of: datetime | None = None) -> tuple[Decimal | None, ConsensusLine, str]:
        """Consensus probability that ``player`` wins, plus which side they were matched to."""
        line = self.consensus(parameters=parameters, as_of=as_of)
        is_a, is_b = same_player(player, self.player_a), same_player(player, self.player_b)
        if is_a == is_b:
            return None, line, "ambiguous" if is_a else "unmatched"
        if line.probability_a is None:
            return None, line, "a" if is_a else "b"
        return (line.probability_a if is_a else Decimal(1) - line.probability_a), line, ("a" if is_a else "b")

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "sport_key": self.sport_key,
            "commence_time": self.commence_time.isoformat() if self.commence_time else None,
            "player_a": self.player_a,
            "player_b": self.player_b,
            "books": len(self.quotes),
            "source": self.source,
        }


@dataclass(slots=True)
class OutsideBatch:
    source: str
    events: list[OutsideEvent] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    sport_keys: list[str] = field(default_factory=list)
    credits_used: int = 0
    requests_remaining: int | None = None
    fetched_at: str = field(default_factory=lambda: _now().isoformat())
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "events": len(self.events),
            "errors": list(self.errors),
            "sport_keys": list(self.sport_keys),
            "credits_used": self.credits_used,
            "requests_remaining": self.requests_remaining,
            "fetched_at": self.fetched_at,
            "note": self.note,
        }


class OutsideLineSource(Protocol):
    name: str

    async def fetch(self, *, as_of: datetime) -> OutsideBatch: ...


# --------------------------------------------------------------------------
# Parsing (The Odds API v4 shape, shared by network, operator file and fixtures)
# --------------------------------------------------------------------------
def parse_event(item: dict[str, Any], *, source: str, default_sport_key: str = "") -> OutsideEvent | None:
    """One ``/v4/sports/{key}/odds`` item -> :class:`OutsideEvent` (h2h only)."""
    home, away = str(item.get("home_team") or ""), str(item.get("away_team") or "")
    event_id = str(item.get("id") or "")
    if not (home and away and event_id):
        return None
    quotes: list[BookQuote] = []
    for book in item.get("bookmakers") or []:
        if not isinstance(book, dict):
            continue
        for market in book.get("markets") or []:
            if not isinstance(market, dict) or market.get("key", "h2h") != "h2h":
                continue
            prices: dict[str, Decimal] = {}
            for outcome in market.get("outcomes") or []:
                if not isinstance(outcome, dict):
                    continue
                price = _decimal(outcome.get("price"))
                if price is not None:
                    prices[str(outcome.get("name") or "")] = price
            odds_a, odds_b = prices.get(home), prices.get(away)
            if odds_a is None or odds_b is None or odds_a <= 1 or odds_b <= 1:
                continue
            try:
                quotes.append(
                    BookQuote(
                        book=str(book.get("key") or book.get("title") or "unknown"),
                        odds_a=odds_a,
                        odds_b=odds_b,
                        last_update=parse_time(book.get("last_update") or market.get("last_update")),
                    )
                )
            except ValueError:
                continue
    return OutsideEvent(
        event_id=event_id,
        sport_key=str(item.get("sport_key") or default_sport_key),
        commence_time=parse_time(item.get("commence_time")),
        player_a=home,
        player_b=away,
        quotes=tuple(quotes),
        source=source,
    )


def parse_events(payload: Any, *, source: str, default_sport_key: str = "") -> list[OutsideEvent]:
    items = payload.get("events", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("odds payload must be a list of events or an object with an 'events' list")
    events = []
    for item in items:
        if isinstance(item, dict):
            event = parse_event(item, source=source, default_sport_key=default_sport_key)
            if event is not None:
                events.append(event)
    return events


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
class NullOutsideSource:
    name = "none"

    async def fetch(self, *, as_of: datetime) -> OutsideBatch:
        del as_of
        return OutsideBatch(
            source=self.name,
            note=f"no outside line source configured; set {ODDS_API_KEY_ENV} or pass --odds-file",
        )


class JsonOutsideSource:
    """Operator-saved v4 response (or a hand-written file in the same shape)."""

    name = "operator_file"

    def __init__(self, path: Path, *, source: str | None = None) -> None:
        self.path = path
        self.source = source or self.name

    async def fetch(self, *, as_of: datetime) -> OutsideBatch:
        del as_of
        batch = OutsideBatch(source=self.source, note=f"outside lines loaded from {self.path}")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            batch.events = parse_events(payload, source=self.source)
            if isinstance(payload, dict):
                batch.sport_keys = [str(k) for k in payload.get("sport_keys", [])]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            batch.errors.append(f"{type(exc).__name__}: {exc}")
        return batch


class StaticOutsideSource:
    """In-memory events (used by the fixture replay, one instance per step)."""

    name = "fixture"

    def __init__(self, events: list[OutsideEvent], *, source: str = "fixture", note: str = "") -> None:
        self.events = events
        self.source = source
        self.note = note

    async def fetch(self, *, as_of: datetime) -> OutsideBatch:
        del as_of
        return OutsideBatch(source=self.source, events=list(self.events), note=self.note)


class OddsApiSource:
    """The Odds API free tier, read-only, budget-guarded.

    One call to the free ``/sports`` listing, then one ``/odds`` call per active
    tennis key (1 credit each with a single region) until ``max_credits_per_run``
    is spent or ``x-requests-remaining`` would drop below ``min_remaining``.
    """

    name = "the_odds_api"

    def __init__(
        self,
        api_key: str,
        *,
        regions: str = DEFAULT_REGIONS,
        bookmakers: tuple[str, ...] | None = None,
        max_credits_per_run: int = 6,
        min_remaining: int = 20,
        sport_keys: tuple[str, ...] | None = None,
        timeout: float = 15.0,
        http_get: Any | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OddsApiSource requires an API key")
        self.api_key = api_key
        self.regions = regions
        self.bookmakers = bookmakers
        self.max_credits_per_run = max_credits_per_run
        self.min_remaining = min_remaining
        self.sport_keys = sport_keys
        self.timeout = timeout
        self._http_get = http_get

    async def _get(self, path: str, params: dict[str, Any]) -> tuple[Any, dict[str, str]]:
        if self._http_get is not None:
            return await self._http_get(path, params)
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{ODDS_API_URL}{path}",
                params={**params, "apiKey": self.api_key},
                headers={"User-Agent": "dual-market-trader-paper/0.2"},
            )
            response.raise_for_status()
            return response.json(), {k.lower(): v for k, v in response.headers.items()}

    @staticmethod
    def _remaining(headers: dict[str, str]) -> int | None:
        raw = headers.get("x-requests-remaining")
        try:
            return int(float(raw)) if raw not in (None, "") else None
        except ValueError:
            return None

    async def _tennis_keys(self, batch: OutsideBatch) -> list[str]:
        if self.sport_keys is not None:
            return list(self.sport_keys)
        payload, headers = await self._get("/sports", {})
        batch.requests_remaining = self._remaining(headers)
        keys = [
            str(item["key"])
            for item in payload
            if isinstance(item, dict)
            and item.get("group") == TENNIS_GROUP
            and item.get("active", True)
            and not item.get("has_outrights", False)
        ]
        return sorted(keys)

    async def fetch(self, *, as_of: datetime) -> OutsideBatch:
        del as_of
        batch = OutsideBatch(source=self.name, note=f"{FREE_TIER['provider']}; regions={self.regions}; markets=h2h")
        try:
            keys = await self._tennis_keys(batch)
        except Exception as exc:  # network failure must not kill the run
            batch.errors.append(f"/sports: {type(exc).__name__}: {exc}")
            return batch
        batch.sport_keys = keys
        if not keys:
            batch.note += "; no in-season tennis keys"
            return batch
        cost_per_call = max(1, len(self.regions.split(",")))
        for key in keys:
            if batch.credits_used + cost_per_call > self.max_credits_per_run:
                batch.errors.append(f"budget: stopped before {key} (max_credits_per_run={self.max_credits_per_run})")
                break
            if batch.requests_remaining is not None and batch.requests_remaining - cost_per_call < self.min_remaining:
                batch.errors.append(
                    f"budget: {batch.requests_remaining} credits remaining < min_remaining={self.min_remaining}; stopped"
                )
                break
            params: dict[str, Any] = {"markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"}
            if self.bookmakers:
                params["bookmakers"] = ",".join(self.bookmakers)
            else:
                params["regions"] = self.regions
            try:
                payload, headers = await self._get(f"/sports/{key}/odds", params)
            except Exception as exc:
                batch.errors.append(f"/sports/{key}/odds: {type(exc).__name__}: {exc}")
                continue
            batch.credits_used += cost_per_call
            remaining = self._remaining(headers)
            if remaining is not None:
                batch.requests_remaining = remaining
            try:
                batch.events.extend(parse_events(payload, source=self.name, default_sport_key=key))
            except ValueError as exc:
                batch.errors.append(f"/sports/{key}/odds: {exc}")
        return batch


def build_outside_source(
    *,
    use_fixtures: bool,
    odds_file: Path | None = None,
    api_key: str | None = None,
    regions: str = DEFAULT_REGIONS,
    max_credits_per_run: int = 6,
    min_remaining: int = 20,
    sport_keys: tuple[str, ...] | None = None,
) -> OutsideLineSource | None:
    """Operator file first; else the free API when a key is present; else nothing.

    Returns ``None`` on fixture runs: the replay fixture carries its own lines.
    """
    if odds_file is not None:
        return JsonOutsideSource(odds_file)
    if use_fixtures:
        return None
    key = api_key if api_key is not None else os.getenv(ODDS_API_KEY_ENV, "")
    if key:
        return OddsApiSource(
            key, regions=regions, max_credits_per_run=max_credits_per_run, min_remaining=min_remaining, sport_keys=sport_keys
        )
    return NullOutsideSource()
