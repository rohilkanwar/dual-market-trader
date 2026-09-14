"""Trader-history sources for the ``category_specialist`` paper track.

Two real sources and a null:

* :class:`FixtureTraderSource` -- committed **synthetic** histories
  (``research/fixtures/specialist_trades.json``) whose open bets sit on the
  Polymarket fixture markets so the follow leg can paper-fill deterministically.
* :class:`PolymarketDataApiSource` -- the public, unauthenticated Polymarket
  Data API (``data-api.polymarket.com``): the volume leaderboard picks the
  "large traders", ``/closed-positions`` gives each wallet's resolved bets and
  ``/positions`` its open ones. $0, read-only, no wallet key anywhere.
* :class:`NullTraderSource` -- nothing fetched, nothing invented.

Kalshi publishes no per-trader data (trades are anonymous), so the track is
Polymarket-only by construction and says so in its status.

Every source returns a :class:`TraderHistoryBatch`; network failures are
recorded on ``errors`` and never raise into the scoreboard.

Category taxonomy
-----------------
The Data API rows carry no category. :func:`specialist_category` maps the
market title, slug and event slug (plus Gamma tag labels when available) to a
small fixed taxonomy through keyword and slug-prefix tables. It is heuristic;
anything unrecognised is ``other`` and the per-category counts are reported so
the coverage of the taxonomy is visible in every artifact.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from core.types import ONE, ZERO, Outcome, Venue
from strategies.specialist import TraderBet

LOGGER = logging.getLogger("specialist_sources")

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "specialist_trades.json"
DATA_API_URL = "https://data-api.polymarket.com"
USER_AGENT = "dual-market-trader-paper/0.2 (paper-only measurement; public read-only)"
CLOSED_PAGE_SIZE = 50
POSITIONS_PAGE_SIZE = 100
RESOLVED_YES_PRICE = "1000000000000000000"
RESOLVED_NO_PRICE = "0"

# --------------------------------------------------------------------------
# Category taxonomy
# --------------------------------------------------------------------------
CATEGORIES: tuple[str, ...] = (
    "tennis",
    "soccer",
    "basketball",
    "american_football",
    "baseball",
    "hockey",
    "mma",
    "esports",
    "motorsport",
    "golf",
    "crypto",
    "macro",
    "politics",
    "weather",
    "entertainment",
    "other",
)

# Polymarket event slugs are usually "<league>-<teams>-<date>"; the first token
# is the most reliable category signal available without Gamma tags.
_SLUG_PREFIXES: dict[str, str] = {
    "atp": "tennis", "wta": "tennis", "tennis": "tennis",
    "epl": "soccer", "ucl": "soccer", "uel": "soccer", "mls": "soccer", "laliga": "soccer", "seriea": "soccer",
    "bundesliga": "soccer", "ligue1": "soccer", "fifa": "soccer", "uefa": "soccer", "copa": "soccer",
    "eredivisie": "soccer", "liga": "soccer", "sco": "soccer", "por": "soccer", "tur": "soccer",
    "nba": "basketball", "wnba": "basketball", "ncaab": "basketball", "cbb": "basketball", "euroleague": "basketball",
    "nfl": "american_football", "ncaaf": "american_football", "cfb": "american_football",
    "mlb": "baseball", "kbo": "baseball", "npb": "baseball",
    "nhl": "hockey", "khl": "hockey",
    "ufc": "mma", "mma": "mma", "pfl": "mma", "bellator": "mma", "boxing": "mma",
    "lol": "esports", "cs2": "esports", "csgo": "esports", "dota2": "esports", "dota": "esports",
    "valorant": "esports", "val": "esports", "esports": "esports", "rl": "esports", "cod": "esports",
    "f1": "motorsport", "nascar": "motorsport", "motogp": "motorsport", "indycar": "motorsport",
    "pga": "golf", "lpga": "golf", "golf": "golf",
    "btc": "crypto", "eth": "crypto", "sol": "crypto", "crypto": "crypto", "bitcoin": "crypto", "ethereum": "crypto",
    "fed": "macro", "cpi": "macro", "fomc": "macro", "gdp": "macro",
    "highest": "weather", "lowest": "weather", "temperature": "weather",
}

_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tennis", ("tennis", "atp ", "wta ", "wimbledon", "roland garros", "french open", "australian open", "grand slam", " set ", "davis cup")),
    ("soccer", ("soccer", "premier league", "la liga", "serie a", "bundesliga", "ligue 1", "champions league", "europa league",
                "uefa", "fifa", "world cup", "copa ", " fc ", " fc", "cf ", "mls ", "eredivisie", "primeira", "o/u 2.5", "o/u 3.5", "o/u 1.5", "both teams to score")),
    ("basketball", ("nba", "wnba", "basketball", "ncaa tournament", "march madness", "euroleague")),
    ("american_football", ("nfl", "super bowl", "ncaaf", "college football", "touchdown", "quarterback")),
    ("baseball", ("mlb", "baseball", "world series", "home run")),
    ("hockey", ("nhl", "hockey", "stanley cup")),
    ("mma", ("ufc", " mma", "bellator", "pfl ", "boxing", "heavyweight", "fight night", "noche ufc")),
    ("esports", ("league of legends", "lol:", "cs2", "counter-strike", "csgo", "dota", "valorant", "esports", "rocket league", "call of duty", "worlds 20")),
    ("motorsport", ("formula 1", "f1 ", "grand prix", "nascar", "motogp", "indycar")),
    ("golf", ("pga", "golf", "masters tournament", "ryder cup", "the open championship")),
    ("crypto", ("bitcoin", "btc", "ethereum", " eth ", "solana", "crypto", "dogecoin", "xrp", "binance", "coinbase", "up or down")),
    ("macro", ("fed ", "fomc", "federal reserve", "rate cut", "rate hike", "interest rate", "cpi", "inflation", "gdp",
               "unemployment", "nonfarm", "payrolls", "treasury yield", "recession", "tariff")),
    ("politics", ("election", "president", "presidential", "senate", "congress", "governor", "prime minister", "parliament",
                  "trump", "biden", "harris", "vance", "democrat", "republican", "supreme court", "impeach", "ceasefire", "nato",
                  "cabinet", "mayor", "referendum", "chancellor", "vote")),
    ("weather", ("temperature", "°c", "°f", "degrees", "rainfall", "snowfall", "hurricane", "heat index")),
    ("entertainment", ("oscars", "academy award", "grammy", "emmy", "box office", "spotify", "billboard", "netflix",
                       "album", "movie", "rotten tomatoes", "eurovision", "time person of the year")),
)

_TAG_ALIASES: dict[str, str] = {
    "tennis": "tennis", "soccer": "soccer", "football": "soccer", "epl": "soccer", "champions league": "soccer",
    "nba": "basketball", "basketball": "basketball", "nfl": "american_football", "college football": "american_football",
    "mlb": "baseball", "baseball": "baseball", "nhl": "hockey", "hockey": "hockey", "ufc": "mma", "mma": "mma", "boxing": "mma",
    "esports": "esports", "league of legends": "esports", "counter-strike": "esports", "f1": "motorsport", "formula 1": "motorsport",
    "golf": "golf", "crypto": "crypto", "bitcoin": "crypto", "ethereum": "crypto", "economy": "macro", "fed": "macro",
    "macro": "macro", "business": "macro", "finance": "macro", "politics": "politics", "elections": "politics",
    "us politics": "politics", "world": "politics", "geopolitics": "politics", "weather": "weather", "climate": "weather",
    "pop culture": "entertainment", "entertainment": "entertainment", "movies": "entertainment", "music": "entertainment",
}

_SLUG_TOKEN = re.compile(r"[a-z0-9]+")


def specialist_category(
    *texts: str | None,
    slugs: tuple[str | None, ...] = (),
    tags: tuple[str, ...] | list[str] = (),
) -> str:
    """Map free text / slugs / Gamma tag labels to one of :data:`CATEGORIES`.

    Precedence: explicit tag alias, then slug prefix, then keyword table in the
    listed order, then ``other``.
    """
    for tag in tags:
        alias = _TAG_ALIASES.get(str(tag).strip().lower())
        if alias:
            return alias
    for slug in slugs:
        tokens = _SLUG_TOKEN.findall(str(slug or "").lower())
        if tokens and tokens[0] in _SLUG_PREFIXES:
            return _SLUG_PREFIXES[tokens[0]]
    haystack = " " + " ".join(str(t or "").lower() for t in texts) + " "
    for category, needles in _KEYWORDS:
        if any(needle in haystack for needle in needles):
            return category
    return "other"


# --------------------------------------------------------------------------
# Batch + protocol
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TraderRef:
    trader: str
    name: str = ""
    volume: Decimal | None = None
    pnl: Decimal | None = None
    rank: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"trader": self.trader, "name": self.name, "volume": self.volume, "pnl": self.pnl, "rank": self.rank}


@dataclass(slots=True)
class TraderHistoryBatch:
    source: str
    traders: list[TraderRef] = field(default_factory=list)
    bets: list[TraderBet] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fetched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    note: str = ""
    requests: int = 0


class TraderHistorySource(Protocol):
    name: str

    async def fetch(self, *, as_of: datetime) -> TraderHistoryBatch: ...


class NullTraderSource:
    name = "none"

    async def fetch(self, *, as_of: datetime) -> TraderHistoryBatch:
        del as_of
        return TraderHistoryBatch(
            source=self.name,
            note="no trader-history source configured; pass --specialist-traders N (network) to read the public Data API",
        )


# --------------------------------------------------------------------------
# Fixture source
# --------------------------------------------------------------------------
def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{value!r} is not a decimal") from exc


def _timestamp(item: dict[str, Any], key: str, *, as_of: datetime, age_key: str | None = None) -> datetime | None:
    raw = item.get(key)
    if raw not in (None, ""):
        if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
            seconds = int(raw)
            if seconds > 10**11:  # epoch milliseconds
                seconds //= 1000
            return datetime.fromtimestamp(seconds, tz=UTC)
        parsed = datetime.fromisoformat(str(raw))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    if age_key and item.get(age_key) not in (None, ""):
        return as_of - timedelta(seconds=float(item[age_key]))
    return None


def parse_bet(item: dict[str, Any], *, as_of: datetime, venue: Venue = Venue.POLYMARKET) -> TraderBet:
    placed = _timestamp(item, "placed_at", as_of=as_of, age_key="age_seconds")
    if placed is None:
        raise ValueError(f"bet {item.get('bet_id')!r} needs placed_at or age_seconds")
    outcome_raw = item.get("outcome")
    return TraderBet(
        bet_id=str(item["bet_id"]),
        trader=str(item["trader"]),
        venue=Venue(str(item.get("venue") or venue.value)),
        market_id=str(item["market_id"]),
        category=str(item.get("category") or specialist_category(item.get("title"), slugs=(item.get("slug"),))),
        direction=Outcome(str(item.get("direction") or "yes")),
        entry_price=_decimal(item["entry_price"]) or ZERO,
        size=_decimal(item["size"]) or ZERO,
        placed_at=placed,
        title=str(item.get("title") or ""),
        resolved=bool(item.get("resolved", False)),
        outcome=Outcome(str(outcome_raw)) if outcome_raw else None,
        resolved_at=_timestamp(item, "resolved_at", as_of=as_of),
        realized_pnl=_decimal(item.get("realized_pnl")),
        market_mid_at_entry=_decimal(item.get("market_mid_at_entry")),
        yes_token_id=item.get("yes_token_id"),
        no_token_id=item.get("no_token_id"),
        metadata=dict(item.get("metadata") or {}),
    )


def load_fixture_bets(path: Path, *, as_of: datetime) -> tuple[list[TraderRef], list[TraderBet], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    traders = [
        TraderRef(trader=str(t["trader"]), name=str(t.get("name") or t["trader"]), volume=_decimal(t.get("volume")))
        for t in payload.get("traders", [])
    ]
    bets = [parse_bet(item, as_of=as_of) for item in payload.get("bets", [])]
    return traders, bets, str(payload.get("_note") or "")


class FixtureTraderSource:
    """Committed synthetic trader histories; exists to prove the arithmetic and every rail."""

    name = "fixture"

    def __init__(self, path: Path = FIXTURE_PATH) -> None:
        self.path = path

    async def fetch(self, *, as_of: datetime) -> TraderHistoryBatch:
        batch = TraderHistoryBatch(source=self.name)
        try:
            batch.traders, batch.bets, batch.note = load_fixture_bets(self.path, as_of=as_of)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            batch.errors.append(f"{type(exc).__name__}: {exc}")
        return batch


# --------------------------------------------------------------------------
# Polymarket Data API source (public, read-only, $0)
# --------------------------------------------------------------------------
def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("data", [])
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def parse_leaderboard(payload: Any) -> list[TraderRef]:
    refs: list[TraderRef] = []
    for row in _rows(payload):
        wallet = str(row.get("user_id") or row.get("proxyWallet") or row.get("wallet") or "").lower()
        if not wallet:
            continue
        rank_raw = row.get("rank")
        try:
            rank = int(rank_raw) if rank_raw is not None else None
        except (TypeError, ValueError):
            rank = None
        refs.append(
            TraderRef(
                trader=wallet,
                name=str(row.get("user_name") or row.get("userName") or ""),
                volume=_decimal(row.get("volume", row.get("vol"))),
                pnl=_decimal(row.get("pnl")),
                rank=rank,
            )
        )
    return refs


def _direction(row: dict[str, Any]) -> Outcome:
    index = row.get("outcomeIndex")
    try:
        return Outcome.YES if int(index) == 0 else Outcome.NO
    except (TypeError, ValueError):
        return Outcome.YES if str(row.get("outcome", "")).strip().lower() == "yes" else Outcome.NO


def _tokens(row: dict[str, Any], direction: Outcome) -> tuple[str | None, str | None]:
    asset = str(row.get("asset") or "") or None
    opposite = str(row.get("oppositeAsset") or "") or None
    return (asset, opposite) if direction is Outcome.YES else (opposite, asset)


def parse_closed_position(row: dict[str, Any], *, trader: str, as_of: datetime) -> TraderBet | None:
    """One resolved bet from a ``/closed-positions`` row; ``None`` if it was exited before resolution."""
    cur = _decimal(row.get("curPrice"))
    if cur not in (ZERO, ONE):
        return None
    market_id = str(row.get("conditionId") or "")
    price = _decimal(row.get("avgPrice"))
    size = _decimal(row.get("totalBought"))
    if not market_id or price is None or size is None or size <= ZERO:
        return None
    direction = _direction(row)
    won = cur == ONE
    outcome = direction if won else (Outcome.NO if direction is Outcome.YES else Outcome.YES)
    closed_at = _timestamp(row, "timestamp", as_of=as_of) or as_of
    yes_token, no_token = _tokens(row, direction)
    title = str(row.get("title") or "")
    return TraderBet(
        bet_id=f"pm-closed-{trader[:10]}-{market_id[:12]}-{str(row.get('asset') or '')[:8]}",
        trader=trader,
        venue=Venue.POLYMARKET,
        market_id=market_id,
        category=specialist_category(title, slugs=(row.get("eventSlug"), row.get("slug"))),
        direction=direction,
        entry_price=min(max(price, ZERO), ONE),
        size=size,
        # The Data API reports when the position closed, not when it was opened.
        placed_at=closed_at,
        title=title,
        resolved=True,
        outcome=outcome,
        resolved_at=closed_at,
        realized_pnl=_decimal(row.get("realizedPnl")),
        market_mid_at_entry=None,
        yes_token_id=yes_token,
        no_token_id=no_token,
        metadata={"slug": row.get("slug"), "event_slug": row.get("eventSlug"), "end_date": row.get("endDate"),
                  "outcome_label": row.get("outcome"), "placed_at_is_close_time": True},
    )


def parse_open_position(row: dict[str, Any], *, trader: str, as_of: datetime) -> TraderBet | None:
    market_id = str(row.get("conditionId") or "")
    price = _decimal(row.get("avgPrice"))
    size = _decimal(row.get("size"))
    cur = _decimal(row.get("curPrice"))
    if not market_id or price is None or size is None or size <= ZERO:
        return None
    if cur in (ZERO, ONE) or bool(row.get("redeemable")):
        return None  # already resolved, awaiting redemption: not a "next" bet
    direction = _direction(row)
    yes_token, no_token = _tokens(row, direction)
    title = str(row.get("title") or "")
    return TraderBet(
        bet_id=f"pm-open-{trader[:10]}-{market_id[:12]}-{str(row.get('asset') or '')[:8]}",
        trader=trader,
        venue=Venue.POLYMARKET,
        market_id=market_id,
        category=specialist_category(title, slugs=(row.get("eventSlug"), row.get("slug"))),
        direction=direction,
        entry_price=min(max(price, ZERO), ONE),
        size=size,
        placed_at=as_of,
        title=title,
        resolved=False,
        yes_token_id=yes_token,
        no_token_id=no_token,
        metadata={"slug": row.get("slug"), "event_slug": row.get("eventSlug"), "end_date": row.get("endDate"),
                  "outcome_label": row.get("outcome"), "cur_price": cur, "neg_risk": row.get("negativeRisk"),
                  "placed_at_unknown": True},
    )


class PolymarketDataApiSource:
    """Volume leaderboard -> per-wallet closed + open positions. Unauthenticated GETs only."""

    name = "polymarket_data_api"

    def __init__(
        self,
        *,
        traders: int = 10,
        window: str = "month",
        closed_pages: int = 2,
        timeout: float = 20.0,
        http_get: Any | None = None,
        base_url: str = DATA_API_URL,
    ) -> None:
        self.traders = max(0, traders)
        self.window = window
        self.closed_pages = max(1, closed_pages)
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._http_get = http_get
        self._client: Any | None = None

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if self._http_get is not None:
            return await self._http_get(path, params)
        import httpx  # local import keeps fixture runs free of network deps

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": USER_AGENT})
        response = await self._client.get(f"{self.base_url}{path}", params=params)
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, *, as_of: datetime) -> TraderHistoryBatch:
        batch = TraderHistoryBatch(
            source=self.name,
            note=(
                f"top {self.traders} wallets by {self.window} volume from GET /v2/leaderboard; resolved bets from "
                "GET /closed-positions (curPrice in {0,1}); open bets from GET /positions. Public, no credentials."
            ),
        )
        if self.traders == 0:
            batch.note = "specialist trader limit is 0; Data API not queried"
            return batch
        try:
            batch.requests += 1
            payload = await self._get(
                "/v2/leaderboard",
                {"time_period": self.window, "sort_by": "VOLUME", "limit": min(self.traders, 50)},
            )
            batch.traders = parse_leaderboard(payload)[: self.traders]
        except Exception as exc:  # the scoreboard must survive a dead endpoint
            batch.errors.append(f"leaderboard: {type(exc).__name__}: {exc}")
            await self.close()
            return batch
        for ref in batch.traders:
            wallet_ok = True
            for page in range(self.closed_pages):
                try:
                    batch.requests += 1
                    rows = _rows(
                        await self._get(
                            "/closed-positions",
                            {"user": ref.trader, "limit": CLOSED_PAGE_SIZE, "offset": page * CLOSED_PAGE_SIZE,
                             "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
                        )
                    )
                except Exception as exc:
                    batch.errors.append(f"closed-positions[{ref.trader[:10]}]: {type(exc).__name__}: {exc}")
                    wallet_ok = False
                    break
                for row in rows:
                    bet = parse_closed_position(row, trader=ref.trader, as_of=as_of)
                    if bet is not None:
                        batch.bets.append(bet)
                if len(rows) < CLOSED_PAGE_SIZE:
                    break
            if not wallet_ok:
                continue  # a wallet without history must not be followed on open bets alone
            try:
                batch.requests += 1
                rows = _rows(await self._get("/positions", {"user": ref.trader, "limit": POSITIONS_PAGE_SIZE}))
            except Exception as exc:
                batch.errors.append(f"positions[{ref.trader[:10]}]: {type(exc).__name__}: {exc}")
                continue
            for row in rows:
                bet = parse_open_position(row, trader=ref.trader, as_of=as_of)
                if bet is not None:
                    batch.bets.append(bet)
        await self.close()
        return batch


# --------------------------------------------------------------------------
# Resolution oracles (how a followed market is settled on the paper ledger)
# --------------------------------------------------------------------------
class ResolutionOracle(Protocol):
    name: str

    async def resolve(self, market_ids: list[str]) -> dict[str, Outcome | None]: ...


class FixtureResolutionOracle:
    """Fixture markets settle by their ``paper_settlement_outcome`` metadata."""

    name = "fixture_paper_settlement_outcome"

    def __init__(self, outcomes: dict[str, Outcome]) -> None:
        self.outcomes = outcomes

    async def resolve(self, market_ids: list[str]) -> dict[str, Outcome | None]:
        return {market_id: self.outcomes.get(market_id) for market_id in market_ids}


class NullResolutionOracle:
    name = "none"

    async def resolve(self, market_ids: list[str]) -> dict[str, Outcome | None]:
        return {market_id: None for market_id in market_ids}


def parse_resolution(payload: Any) -> Outcome | None:
    """``/v2/resolutions`` -> YES/NO when finally resolved to a full payout, else ``None``."""
    for row in _rows(payload):
        if str(row.get("status", "")).lower() != "resolved":
            continue
        price = str(row.get("price") or "")
        if price == RESOLVED_YES_PRICE:
            return Outcome.YES
        if price == RESOLVED_NO_PRICE:
            return Outcome.NO
    return None


class PolymarketResolutionOracle:
    """UMA resolution status from the public Data API; split payouts stay unresolved here."""

    name = "polymarket_data_api_resolutions"

    def __init__(self, *, timeout: float = 20.0, http_get: Any | None = None, base_url: str = DATA_API_URL) -> None:
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._http_get = http_get
        self.errors: list[str] = []

    async def resolve(self, market_ids: list[str]) -> dict[str, Outcome | None]:
        out: dict[str, Outcome | None] = {}
        if not market_ids:
            return out
        if self._http_get is None:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": USER_AGENT}) as client:
                for market_id in market_ids:
                    try:
                        response = await client.get(f"{self.base_url}/v2/resolutions", params={"condition": market_id})
                        response.raise_for_status()
                        out[market_id] = parse_resolution(response.json())
                    except Exception as exc:
                        self.errors.append(f"resolutions[{market_id[:12]}]: {type(exc).__name__}: {exc}")
                        out[market_id] = None
            return out
        for market_id in market_ids:
            try:
                out[market_id] = parse_resolution(await self._http_get("/v2/resolutions", {"condition": market_id}))
            except Exception as exc:
                self.errors.append(f"resolutions[{market_id[:12]}]: {type(exc).__name__}: {exc}")
                out[market_id] = None
        return out


def build_trader_source(*, use_fixtures: bool, traders: int | None = None, window: str = "month") -> TraderHistorySource:
    """Fixtures on fixture runs; the public Data API on network runs (``traders=0`` disables)."""
    if use_fixtures:
        return FixtureTraderSource()
    if traders is None:
        traders = 10
    if traders <= 0:
        return NullTraderSource()
    return PolymarketDataApiSource(traders=traders, window=window)
