"""``weather_bucket_edge``: Polymarket temperature buckets vs. a free multi-model ensemble.

One paper track, its own :class:`core.ledger.PaperLedger`, and a persistent
**city-day register** carried across runs:

1. discover the Polymarket daily-temperature events (Gamma tag ``103040``; one
   NegRisk event per city, per day, per highest / lowest) with a YES and a NO
   book per bucket;
2. for every event: parse the settlement station, unit, high/low and date out
   of the rules text (fail-closed on anything missing or ambiguous), parse every
   bucket label and require the buckets to tile the line exactly once;
3. fetch the free ensemble for the station coordinates and the observation
   date, turn the members into bucket probabilities, price every bucket against
   the YES ask / NO ask net of the venue fee, and paper-fill the buckets whose
   net edge clears the pre-registered threshold (caps per order / market /
   city-day and the ``RiskManager`` rails);
4. after the station's local day ends, settle each city-day on the venue's own
   resolution (positions close at 1 / 0 on the ledger). Until the venue
   resolves, the public METAR history gives a *provisional* outcome that is
   reported and compared but never booked;
5. the pre-registered verdict runs over venue-settled city-days only.

Network runs without a feed are an honest empty (``no_weather_feed``); fixture
runs replay ``research/fixtures/weather_buckets_replay.json`` through the same
code path, which is what proves the arithmetic. ``WEATHER_TRACKS`` is the hook
for sister tracks (late-day METAR dead-bucket, per-city calibration) that share
this register's (station, city, date, bucket bounds) interface.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.ledger import PaperLedger
from core.risk import RiskLimits
from core.types import ONE, ZERO, Market, MarketGroup, OrderBook, Outcome, Venue
from research.scoreboard import DEFAULT_STARTING_CASH, TrackRuntime, TrackSummary, VenueSnapshot, _venue_pnl
from research.weather_sources import (
    FREE_SOURCES,
    EnsembleForecast,
    FixtureWeatherFeed,
    NullWeatherFeed,
    ObservedExtreme,
    StationRegistry,
    WeatherFeed,
    observed_extreme,
    paid_source_policy,
    parse_time,
    zone_for,
)
from strategies.weather_buckets import (
    TRACK,
    WEATHER_RISK_LIMITS,
    BucketProbabilities,
    CityDayResult,
    SettlementRules,
    TemperatureBucket,
    WeatherBucketEdgeStrategy,
    WeatherEdgeParameters,
    WeatherEvaluation,
    brier,
    bucket_for_value,
    buckets_partition_reason,
    ensemble_bucket_probabilities,
    parse_bucket_label,
    parse_settlement_rules,
    portfolio_cash_at_risk,
    verdict,
)
from venues.polymarket import PolymarketClient
from venues.polymarket.client import _book_from_levels, group_from_event

WEATHER_TRACK = TRACK
WEATHER_TRACKS: tuple[str, ...] = (WEATHER_TRACK,)
WEATHER_TRACK_LABEL = "Weather temperature-bucket edge (free ensemble)"
TRACK_FAMILY = "weather"
DAILY_TEMPERATURE_TAG_ID = 103040
REGISTER_SCHEMA = "1.0.0"
REPLAY_FIXTURE = Path(__file__).with_name("fixtures") / "weather_buckets_replay.json"
SETTLEMENT_GRACE = timedelta(hours=1)
UNRESOLVED_EXPIRY = timedelta(days=5)
Q4 = Decimal("0.0001")
_RE_SLUG = re.compile(r"^(highest|lowest)-temperature-in-(.+?)-on-([a-z]+)-(\d{1,2})(?:-(\d{4}))?$")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"),
        start=1,
    )
}


def _now() -> datetime:
    return datetime.now(UTC)


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4) if value is not None else None


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


# --------------------------------------------------------------------------
# City-day parsing (fail-closed)
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BucketMarket:
    market: Market
    bucket: TemperatureBucket


@dataclass(slots=True)
class CityDay:
    group: MarketGroup
    slug: str
    title: str
    city: str | None
    kind: str | None
    observation_date: date | None
    rules: SettlementRules
    buckets: list[BucketMarket] = field(default_factory=list)
    reason: str = "admitted"

    @property
    def admitted(self) -> bool:
        return self.reason == "admitted"

    @property
    def station(self) -> str | None:
        return self.rules.station

    @property
    def unit(self) -> str | None:
        return self.rules.unit

    @property
    def record_id(self) -> str:
        return f"{self.station}:{self.observation_date.isoformat() if self.observation_date else '?'}:{self.kind}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.group.group_id,
            "slug": self.slug,
            "title": self.title,
            "city": self.city,
            "kind": self.kind,
            "observation_date": self.observation_date.isoformat() if self.observation_date else None,
            "station": self.station,
            "unit": self.unit,
            "rules": self.rules.as_dict(),
            "buckets": [bm.bucket.as_dict() | {"market_id": bm.market.market_id} for bm in self.buckets],
            "reason": self.reason,
        }


def parse_slug(slug: str) -> tuple[str | None, str | None, date | None]:
    """``highest-temperature-in-nyc-on-september-15-2026`` -> (kind, city, date)."""
    match = _RE_SLUG.match(slug or "")
    if match is None:
        return None, None, None
    kind = "high" if match.group(1) == "highest" else "low"
    city = match.group(2).replace("-", " ")
    month = _MONTHS.get(match.group(3))
    parsed: date | None = None
    if month is not None and match.group(5):
        try:
            parsed = date(int(match.group(5)), month, int(match.group(4)))
        except ValueError:
            parsed = None
    return kind, city, parsed


def parse_city_day(group: MarketGroup) -> CityDay:
    slug = str(group.metadata.get("slug") or "")
    slug_kind, city, slug_date = parse_slug(slug)
    rules_texts = [str(m.metadata.get("resolution_text") or "") for m in group.markets]
    parsed_rules = [parse_settlement_rules(text) for text in rules_texts]
    rules = parsed_rules[0] if parsed_rules else parse_settlement_rules("")
    stations = {r.station for r in parsed_rules if r.station}
    kind = rules.kind or slug_kind
    observation_date = rules.observation_date or slug_date
    city_day = CityDay(group=group, slug=slug, title=group.title, city=city, kind=kind, observation_date=observation_date, rules=rules)
    if slug_kind is None and rules.kind is None:
        city_day.reason = "not_a_temperature_event"
        return city_day
    if not group.markets:
        city_day.reason = "no_markets"
        return city_day
    if len(stations) > 1:
        city_day.reason = "station_ambiguous_across_legs"
        return city_day
    if not rules.admitted:
        city_day.reason = rules.reason
        return city_day
    if observation_date is None:
        city_day.reason = "no_date"
        return city_day
    if slug_kind is not None and rules.kind is not None and slug_kind != rules.kind:
        city_day.reason = "kind_mismatch_slug_vs_rules"
        return city_day
    if not group.exclusive:
        city_day.reason = "not_exclusive_group"
        return city_day
    if group.augmented:
        city_day.reason = "augmented_group"
        return city_day
    buckets: list[BucketMarket] = []
    for market in group.markets:
        label = str(market.metadata.get("group_item_title") or "") or market.title
        bucket = parse_bucket_label(label)
        if bucket is None:
            city_day.reason = "bucket_parse_failed"
            return city_day
        buckets.append(BucketMarket(market, bucket))
    if any(bm.bucket.unit != rules.unit for bm in buckets):
        city_day.reason = "unit_mismatch"
        return city_day
    partition_reason = buckets_partition_reason([bm.bucket for bm in buckets])
    if partition_reason is not None:
        city_day.reason = f"buckets_not_partition_{partition_reason}"
        return city_day
    city_day.buckets = sorted(buckets, key=lambda bm: bm.bucket.sort_key)
    return city_day


# --------------------------------------------------------------------------
# Register
# --------------------------------------------------------------------------
@dataclass(slots=True)
class CityDayRecord:
    record_id: str
    event_id: str
    slug: str
    title: str
    city: str | None
    station: str
    station_name: str | None
    observation_date: str
    kind: str
    unit: str
    rules_source: str | None
    timezone: str | None
    utc_offset_seconds: int | None
    entered_at: str
    forecast: dict[str, Any]
    model: dict[str, Any]
    buckets: list[dict[str, Any]]
    sum_of_mids: str | None
    status: str = "open"  # open | settled | excluded
    observations: list[dict[str, Any]] = field(default_factory=list)
    settlement: dict[str, Any] = field(default_factory=dict)
    metar: dict[str, Any] = field(default_factory=dict)
    paper: dict[str, Any] = field(default_factory=dict)
    brier: dict[str, Any] = field(default_factory=dict)
    exclusion_reason: str | None = None

    @property
    def date(self) -> date:
        return date.fromisoformat(self.observation_date)

    @property
    def contracts(self) -> Decimal:
        return sum((Decimal(str(b.get("filled_quantity") or "0")) for b in self.buckets), ZERO)

    def bucket_objects(self) -> list[TemperatureBucket]:
        return [TemperatureBucket(b["label"], b.get("lower"), b.get("upper"), self.unit) for b in self.buckets]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CityDayRegister:
    records: dict[str, CityDayRecord] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: _now().isoformat())
    updated_at: str = field(default_factory=lambda: _now().isoformat())

    def open_records(self) -> list[CityDayRecord]:
        return [r for r in self.records.values() if r.status == "open"]

    def settled_records(self) -> list[CityDayRecord]:
        return [r for r in self.records.values() if r.status == "settled"]

    def excluded_records(self) -> list[CityDayRecord]:
        return [r for r in self.records.values() if r.status == "excluded"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REGISTER_SCHEMA,
            "paper_only": True,
            "track": WEATHER_TRACK,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "records": [self.records[k].as_dict() for k in sorted(self.records)],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CityDayRegister:
        if payload.get("paper_only") is not True:
            raise ValueError("refusing to load a city-day register that is not marked paper_only")
        register = cls(created_at=payload.get("created_at", _now().isoformat()), updated_at=payload.get("updated_at", _now().isoformat()))
        for item in payload.get("records", []):
            record = CityDayRecord(**item)
            register.records[record.record_id] = record
        return register

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = _now().isoformat()
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    @classmethod
    def load_or_create(cls, path: Path) -> CityDayRegister:
        if path.exists():
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls()


def register_path(artifact_dir: Path) -> Path:
    return artifact_dir / "weather_buckets" / "register.json"


def load_station_registry() -> StationRegistry | None:
    try:
        return StationRegistry.load()
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# Venue resolution
# --------------------------------------------------------------------------
ResolutionLookup = Callable[[CityDayRecord], Awaitable[dict[str, dict[str, Any]] | None]]


def classify_venue_resolution(status_by_market: dict[str, dict[str, Any]] | None, market_ids: list[str]) -> tuple[str, str | None, str]:
    """``(state, winning_market_id, detail)`` from per-leg Gamma status; state is resolved | excluded | pending."""
    if not status_by_market:
        return "pending", None, "no_status"
    winners: list[str] = []
    closed = 0
    non_binary = 0
    for market_id in market_ids:
        status = status_by_market.get(market_id)
        if not status or not status.get("closed"):
            continue
        closed += 1
        prices = [_dec(p) for p in (status.get("outcome_prices") or [])]
        if len(prices) != 2 or any(p is None for p in prices):
            non_binary += 1
            continue
        rounded = [p.quantize(Decimal("0.01")) for p in prices]  # type: ignore[union-attr]
        if rounded == [ONE, ZERO]:
            winners.append(market_id)
        elif rounded != [ZERO, ONE]:
            non_binary += 1
    if closed < len(market_ids):
        return "pending", None, f"closed_{closed}_of_{len(market_ids)}"
    if non_binary:
        return "excluded", None, f"non_binary_resolution_on_{non_binary}_legs"
    if len(winners) != 1:
        return "excluded", None, f"{len(winners)}_winning_buckets"
    return "resolved", winners[0], "venue_resolved"


async def network_resolution_lookup(record: CityDayRecord) -> dict[str, dict[str, Any]] | None:
    client = PolymarketClient(paper=True, use_fixtures=False, search_terms=None)
    try:
        return await client.get_event_status(record.slug)
    finally:
        await client.close()


# --------------------------------------------------------------------------
# Snapshot capture (network)
# --------------------------------------------------------------------------
async def capture_weather_snapshot(client: PolymarketClient, *, limit: int, source: str = "network") -> VenueSnapshot:
    """Daily-temperature events with a YES and a NO book per bucket; failures land in ``errors``."""
    snapshot = VenueSnapshot(venue=Venue.POLYMARKET, source=source)
    try:
        groups = await client.list_events_by_tag(DAILY_TEMPERATURE_TAG_ID, limit=limit, order="startDate", ascending=False)
    except Exception as exc:  # a dead endpoint must not kill the run
        snapshot.errors.append(f"list_events_by_tag: {type(exc).__name__}: {exc}")
        return snapshot
    snapshot.groups = list(groups)
    snapshot.markets = [m for g in groups for m in g.markets]
    tokens = [t for m in snapshot.markets for t in (m.yes_token_id, m.no_token_id) if t]
    try:
        by_token = await client.get_books(tokens)
    except Exception as exc:
        snapshot.errors.append(f"books: {type(exc).__name__}: {exc}")
        by_token = {}
    for market in snapshot.markets:
        yes = by_token.get(market.yes_token_id or "")
        no = by_token.get(market.no_token_id or "")
        if yes is None or no is None:
            snapshot.errors.append(f"book_missing[{market.market_id}]")
        snapshot.books[market.market_id] = (
            OrderBook(market_id=market.market_id, bids=yes.bids, asks=yes.asks, timestamp=yes.timestamp) if yes is not None else OrderBook(market_id=market.market_id)
        )
        snapshot.no_books[market.market_id] = (
            OrderBook(market_id=market.market_id, bids=no.bids, asks=no.asks, timestamp=no.timestamp) if no is not None else OrderBook(market_id=market.market_id)
        )
    return snapshot


async def capture_weather_snapshots(*, limit: int) -> VenueSnapshot:
    client = PolymarketClient(paper=True, use_fixtures=False, search_terms=None)
    try:
        return await capture_weather_snapshot(client, limit=limit)
    finally:
        await client.close()


# --------------------------------------------------------------------------
# One cycle
# --------------------------------------------------------------------------
def _fee_rate(market: Market, *, model_fees: bool) -> Decimal:
    if not model_fees:
        return ZERO
    raw = market.metadata.get("taker_fee_rate")
    rate = _dec(raw)
    return rate if rate is not None and rate > ZERO else ZERO


def _edge_row(track: str, market: Market, record_id: str, ev: WeatherEvaluation, *, filled: bool) -> dict[str, Any]:
    return {
        "track": track,
        "venue": market.venue.value,
        "market": market.market_id,
        "title": market.title,
        "record_id": record_id,
        "bucket": ev.bucket,
        "edge_bps": int((ev.net_edge * 10000).to_integral_value()) if ev.net_edge is not None else None,
        "gross_edge_bps": int((ev.gross_edge * 10000).to_integral_value()) if ev.gross_edge is not None else None,
        "admitted": ev.traded,
        "filled": filled,
        "reason": ev.reason,
        "fair_value": _q(ev.p_model),
        "mid": _q(ev.mid),
        "side": ev.side,
        "price": _q(ev.price),
        "fee_per_contract": _q(ev.fee_per_contract),
    }


def _mids(snapshot: VenueSnapshot, city_day: CityDay) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for bm in city_day.buckets:
        book = snapshot.book(bm.market)
        out[bm.bucket.label] = str(_q(book.mid_price)) if book.mid_price is not None else None
    return out


def _local_day_end(record_date: date, zone: Any) -> datetime:
    return datetime.combine(record_date + timedelta(days=1), datetime.min.time(), tzinfo=zone)


def _provisional_pnl(record: CityDayRecord, winner_label: str | None) -> Decimal | None:
    """Hypothetical net PnL of the record's fills if ``winner_label`` resolves YES."""
    if winner_label is None:
        return None
    total = ZERO
    for row in record.buckets:
        qty = _dec(row.get("filled_quantity")) or ZERO
        if qty <= ZERO:
            continue
        yes_price = _dec(row.get("average_yes_price"))
        fee = _dec(row.get("fees")) or ZERO
        if yes_price is None:
            continue
        signed = qty if row.get("side") == "buy_yes" else -qty
        settle = ONE if row["label"] == winner_label else ZERO
        total += signed * (settle - yes_price) - fee
    return total


def _brier_scores(record: CityDayRecord, winner_label: str) -> dict[str, Any]:
    model_scores: list[Decimal] = []
    market_scores: list[Decimal] = []
    for row in record.buckets:
        won = row["label"] == winner_label
        p_model = _dec(row.get("p_model"))
        mid = _dec(row.get("mid"))
        if p_model is not None:
            model_scores.append(brier(p_model, won))
        if mid is not None:
            market_scores.append(brier(mid, won))
    return {
        "model": _q(sum(model_scores, ZERO)) if model_scores else None,
        "market": _q(sum(market_scores, ZERO)) if market_scores else None,
        "n_buckets": len(record.buckets),
        "note": "multi-class Brier: sum over buckets of (p - outcome)^2; lower is better",
    }


async def run_weather_cycle(
    runtime: TrackRuntime,
    register: CityDayRegister,
    *,
    feed: WeatherFeed,
    parameters: WeatherEdgeParameters | None = None,
    as_of: datetime | None = None,
    resolution_lookup: ResolutionLookup | None = None,
    model_fees: bool = True,
) -> TrackSummary:
    params = parameters or WeatherEdgeParameters()
    as_of = as_of or _now()
    summary = runtime.summary
    summary.label = WEATHER_TRACK_LABEL
    snapshot = runtime.snapshots[Venue.POLYMARKET]
    strategy = WeatherBucketEdgeStrategy(params, portfolio=runtime.ledger.portfolio, risk=runtime.risk)
    rows: list[dict[str, Any]] = []
    station_parse: dict[str, int] = {}
    bucket_refusals: dict[str, int] = {}
    feed_stats = {"forecasts_requested": 0, "forecasts_available": 0, "forecast_errors": [], "forecast_age_hours_max": None, "metar_requested": 0, "metar_latest_age_hours": None}
    entered_this_run: set[str] = set()
    bucket_markets = 0
    priced_city_days = 0

    # 1. Discovery, parsing, pricing, paper entry.
    for group in snapshot.groups:
        city_day = parse_city_day(group)
        summary.candidates += 1
        bucket_markets += len(group.markets)
        station_parse[city_day.rules.reason] = station_parse.get(city_day.rules.reason, 0) + 1
        row: dict[str, Any] = {**city_day.as_dict(), "record_id": city_day.record_id if city_day.admitted else None}
        rows.append(row)
        if not city_day.admitted:
            row["reason"] = city_day.reason
            summary.refuse(city_day.reason)
            continue
        assert city_day.observation_date is not None and city_day.station is not None and city_day.kind is not None and city_day.unit is not None
        record = register.records.get(city_day.record_id)
        if record is not None:
            record.observations.append({"at": as_of.isoformat(), "mids": _mids(snapshot, city_day)})
            row["reason"] = "already_entered" if record.status == "open" else f"record_{record.status}"
            summary.refuse(row["reason"])
            continue
        # Coarse UTC-date gate before spending a forecast call; the precise local check follows.
        utc_lead = (city_day.observation_date - as_of.date()).days
        if utc_lead < -1:
            row["reason"] = "observation_day_over"
            summary.refuse("observation_day_over")
            continue
        if utc_lead > params.max_lead_days + 1:
            row["reason"] = "too_far_ahead"
            summary.refuse("too_far_ahead")
            continue
        station = await feed.station(city_day.station)
        if station is None:
            row["reason"] = "station_unknown"
            summary.refuse("station_unknown")
            continue
        row["station_coordinates"] = station.as_dict()
        feed_stats["forecasts_requested"] += 1
        try:
            forecast = await feed.forecast(station, target_date=city_day.observation_date, kind=city_day.kind, unit=city_day.unit, as_of=as_of)
        except Exception as exc:  # the feed must never take the run down
            forecast = EnsembleForecast(station=station.icao, target_date=city_day.observation_date, kind=city_day.kind, unit=city_day.unit, source=getattr(feed, "name", "feed"))
            forecast.errors.append(f"{type(exc).__name__}: {exc}")
        row["forecast"] = forecast.as_dict()
        if forecast.errors or forecast.n_members == 0:
            feed_stats["forecast_errors"].extend(f"{city_day.record_id}: {e}" for e in forecast.errors or ["empty"])
            row["reason"] = "forecast_unavailable"
            summary.refuse("forecast_unavailable")
            continue
        feed_stats["forecasts_available"] += 1
        age = forecast.age_hours(as_of)
        if age is not None:
            age = max(age, ZERO)
            current = feed_stats["forecast_age_hours_max"]
            feed_stats["forecast_age_hours_max"] = age if current is None else max(current, age)
        zone = zone_for(forecast.timezone, forecast.utc_offset_seconds)
        local_today = as_of.astimezone(zone).date()
        lead_days = (city_day.observation_date - local_today).days
        row["lead_days"] = lead_days
        if lead_days < 0:
            row["reason"] = "observation_day_over"
            summary.refuse("observation_day_over")
            continue
        if lead_days > params.max_lead_days:
            row["reason"] = "too_far_ahead"
            summary.refuse("too_far_ahead")
            continue
        if forecast.n_members < params.min_members:
            row["reason"] = "insufficient_members"
            summary.refuse("insufficient_members")
            continue
        if forecast.n_models < params.min_models:
            row["reason"] = "insufficient_models"
            summary.refuse("insufficient_models")
            continue
        if age is not None and age > params.max_forecast_age_hours:
            row["reason"] = "forecast_stale"
            summary.refuse("forecast_stale")
            continue
        buckets = [bm.bucket for bm in city_day.buckets]
        probabilities: BucketProbabilities = ensemble_bucket_probabilities(forecast.members, buckets, parameters=params)
        row["model"] = probabilities.as_dict()
        priced_city_days += 1

        bucket_rows: list[dict[str, Any]] = []
        mids: list[Decimal] = []
        city_notional = ZERO
        traded_any = False
        for bm in city_day.buckets:
            market, bucket = bm.market, bm.bucket
            yes_book, no_book = snapshot.book(market), snapshot.no_book(market)
            if yes_book.mid_price is not None:
                mids.append(yes_book.mid_price)
            p_model = probabilities.probabilities[bucket.label]
            ev = strategy.evaluate(
                market,
                bucket,
                p_model,
                yes_book,
                no_book,
                fee_rate=_fee_rate(market, model_fees=model_fees),
                city_day_notional_used=city_notional,
                context={"station": city_day.station, "city": city_day.city or "", "date": city_day.observation_date.isoformat(), "kind": city_day.kind, "record_id": city_day.record_id},
            )
            bucket_row: dict[str, Any] = {
                "market_id": market.market_id,
                "label": bucket.label,
                "lower": bucket.lower,
                "upper": bucket.upper,
                "members": probabilities.counts[bucket.label],
                **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in ev.as_dict().items() if k != "bucket"},
                "fills": 0,
                "filled_quantity": "0",
                "average_yes_price": None,
                "fees": "0",
                "cost": "0",
            }
            # Only buckets with a positive gross edge are worth a row on the board;
            # the full per-bucket table lives in the register record.
            if ev.gross_edge is not None and (ev.gross_edge > ZERO or ev.traded):
                summary.edges.append(_edge_row(runtime.name, market, city_day.record_id, ev, filled=False))
            if not ev.traded:
                bucket_refusals[ev.reason] = bucket_refusals.get(ev.reason, 0) + 1
                bucket_rows.append(bucket_row)
                continue
            summary.proposed_orders += len(ev.orders)
            summary.admitted_edges.append(ev.net_edge or ZERO)
            fills_before = summary.paper_fills
            for order in ev.orders:
                report = await runtime.submit(order, edge=ev.net_edge)
                if report is None or not report.fills:
                    continue
                qty = sum((f.quantity for f in report.fills), ZERO)
                fees = sum((f.fee for f in report.fills), ZERO)
                yes_cost = sum((f.quantity * f.yes_equivalent_price for f in report.fills), ZERO)
                cost = sum((f.quantity * f.price for f in report.fills), ZERO)
                bucket_row["fills"] = len(report.fills)
                bucket_row["filled_quantity"] = str(qty)
                bucket_row["average_yes_price"] = str(_q(yes_cost / qty))
                bucket_row["fees"] = str(fees)
                bucket_row["cost"] = str(_q(cost))
                city_notional += cost + fees
                traded_any = True
            if summary.paper_fills > fills_before:
                summary.edges[-1]["filled"] = True
                summary.edges[-1]["admitted"] = True
            bucket_rows.append(bucket_row)
        if traded_any:
            summary.admitted += 1
        record = CityDayRecord(
            record_id=city_day.record_id,
            event_id=group.group_id,
            slug=city_day.slug,
            title=city_day.title,
            city=city_day.city,
            station=city_day.station,
            station_name=city_day.rules.station_name,
            observation_date=city_day.observation_date.isoformat(),
            kind=city_day.kind,
            unit=city_day.unit,
            rules_source=city_day.rules.source,
            timezone=forecast.timezone,
            utc_offset_seconds=forecast.utc_offset_seconds,
            entered_at=as_of.isoformat(),
            forecast=forecast.as_dict(),
            model=probabilities.as_dict(),
            buckets=bucket_rows,
            sum_of_mids=str(_q(sum(mids, ZERO))) if mids else None,
            observations=[{"at": as_of.isoformat(), "mids": _mids(snapshot, city_day)}],
            paper={"contracts": str(sum((Decimal(b["filled_quantity"]) for b in bucket_rows), ZERO)), "cost": str(_q(city_notional)), "lead_days": lead_days, "cash_at_risk_after": str(_q(portfolio_cash_at_risk(runtime.ledger.portfolio)))},
        )
        register.records[record.record_id] = record
        entered_this_run.add(record.record_id)
        row["reason"] = "entered" if traded_any else "priced_no_edge"
        row["paper_fills"] = sum(int(b["fills"]) for b in bucket_rows)
        if not traded_any:
            summary.refuse("priced_no_edge")

    # 2. Settlement of open records whose local day has ended.
    settlement_counts = {"checked": 0, "venue_resolved": 0, "excluded": 0, "pending": 0, "metar_provisional": 0, "lookup_errors": 0}
    for record in register.open_records():
        zone = zone_for(record.timezone, record.utc_offset_seconds)
        day_end = _local_day_end(record.date, zone)
        if as_of < day_end + SETTLEMENT_GRACE:
            continue
        settlement_counts["checked"] += 1
        market_ids = [b["market_id"] for b in record.buckets]
        status: dict[str, dict[str, Any]] | None = None
        lookup_error: str | None = None
        if resolution_lookup is not None:
            try:
                status = await resolution_lookup(record)
            except Exception as exc:  # a dead endpoint leaves the record open
                settlement_counts["lookup_errors"] += 1
                lookup_error = f"{type(exc).__name__}: {exc}"
        state, winner_id, detail = classify_venue_resolution(status, market_ids)
        if lookup_error is not None:
            detail = f"lookup_error: {lookup_error}"
        winner_label = next((b["label"] for b in record.buckets if b["market_id"] == winner_id), None)

        # METAR-derived outcome: provisional cross-check, reported and compared, never booked.
        station = await feed.station(record.station)
        extreme: ObservedExtreme | None = None
        metar_error: str | None = None
        if station is not None:
            feed_stats["metar_requested"] += 1
            try:
                observations = await feed.observations(station, as_of=as_of, hours=72)
            except Exception as exc:
                observations = []
                metar_error = f"{type(exc).__name__}: {exc}"
            if observations:
                latest_age = Decimal(str(round((as_of - observations[-1].time).total_seconds() / 3600, 4)))
                current = feed_stats["metar_latest_age_hours"]
                feed_stats["metar_latest_age_hours"] = latest_age if current is None else max(current, latest_age)
            extreme = observed_extreme(
                observations, local_day=record.date, zone=zone, unit=record.unit, kind=record.kind, as_of=as_of,
                min_observations=params.min_complete_day_observations,
            )
            metar_bucket = bucket_for_value(record.bucket_objects(), extreme.value) if extreme.value is not None else None
            record.metar = {
                **extreme.as_dict(),
                "checked_at": as_of.isoformat(),
                "bucket": metar_bucket.label if metar_bucket else None,
                "provisional_pnl": str(_q(_provisional_pnl(record, metar_bucket.label))) if (metar_bucket and extreme.complete) else None,
                "agrees_with_venue": None,
                **({"error": metar_error} if metar_error else {}),
            }
        else:
            record.metar = {"reason": "station_unknown", "checked_at": as_of.isoformat(), "bucket": None, "provisional_pnl": None, "agrees_with_venue": None}

        if state == "resolved" and winner_label is not None:
            for bucket_row in record.buckets:
                outcome = Outcome.YES if bucket_row["market_id"] == winner_id else Outcome.NO
                position = runtime.ledger.portfolio.get(Venue.POLYMARKET, bucket_row["market_id"])
                if position is not None and position.quantity != ZERO:
                    runtime.ledger.settle(Venue.POLYMARKET, bucket_row["market_id"], outcome)
                    settled = runtime.ledger.portfolio.get(Venue.POLYMARKET, bucket_row["market_id"])
                    bucket_row["realized_pnl"] = str(_q(settled.realized_pnl)) if settled else None
                bucket_row["won"] = outcome is Outcome.YES
            realized = sum((_dec(b.get("realized_pnl")) or ZERO for b in record.buckets), ZERO)
            record.status = "settled"
            record.settlement = {"method": "venue", "winning_market_id": winner_id, "winning_label": winner_label, "settled_at": as_of.isoformat(), "detail": detail}
            record.paper["realized_pnl"] = str(_q(realized))
            record.paper["fees"] = str(sum((_dec(b.get("fees")) or ZERO for b in record.buckets), ZERO))
            record.brier = _brier_scores(record, winner_label)
            if record.metar.get("bucket") is not None:
                record.metar["agrees_with_venue"] = record.metar["bucket"] == winner_label
            settlement_counts["venue_resolved"] += 1
            summary.metrics["settlement_fills"] = int(summary.metrics.get("settlement_fills", 0)) + 1
        elif state == "excluded":
            record.status = "excluded"
            record.exclusion_reason = detail
            record.settlement = {"method": "venue", "winning_market_id": None, "winning_label": None, "settled_at": as_of.isoformat(), "detail": detail}
            settlement_counts["excluded"] += 1
        elif as_of > day_end + UNRESOLVED_EXPIRY:
            record.status = "excluded"
            record.exclusion_reason = "unresolved_after_expiry"
            record.settlement = {"method": None, "winning_market_id": None, "winning_label": None, "settled_at": as_of.isoformat(), "detail": detail}
            settlement_counts["excluded"] += 1
        else:
            record.settlement = {
                "method": "metar_provisional" if record.metar.get("provisional_pnl") is not None else None,
                "winning_market_id": None,
                "winning_label": record.metar.get("bucket"),
                "settled_at": None,
                "detail": detail,
                **({"lookup_error": lookup_error} if lookup_error else {}),
            }
            settlement_counts["pending"] += 1
            if record.metar.get("provisional_pnl") is not None:
                settlement_counts["metar_provisional"] += 1

    # 3. Verdict and metrics.
    settled = register.settled_records()
    settled_results = [
        CityDayResult(r.record_id, r.contracts, _dec(r.paper.get("realized_pnl")) or ZERO, "venue") for r in settled
    ]
    provisional_results = settled_results + [
        CityDayResult(r.record_id, r.contracts, _dec(r.metar.get("provisional_pnl")) or ZERO, "metar_provisional")
        for r in register.open_records()
        if r.metar.get("provisional_pnl") is not None
    ]
    agreement = [r.metar.get("agrees_with_venue") for r in settled if r.metar.get("agrees_with_venue") is not None]
    model_briers = [_dec(r.brier.get("model")) for r in settled if r.brier.get("model") is not None]
    market_briers = [_dec(r.brier.get("market")) for r in settled if r.brier.get("market") is not None]
    admitted_city_days = summary.admitted
    total = summary.candidates
    parsed = sum(v for k, v in station_parse.items() if k == "parsed")
    feed_name = getattr(feed, "name", type(feed).__name__)
    summary.metrics.update(
        {
            "status": track_status(feed_name, summary, snapshot),
            "as_of": as_of.isoformat(),
            "parameters": params.as_dict(),
            "feed": {"name": feed_name, **(feed.as_dict() if hasattr(feed, "as_dict") else {}), "stats": feed_stats},
            "free_sources": FREE_SOURCES,
            "paid_sources": paid_source_policy(),
            "station_parse": {
                "counts": dict(sorted(station_parse.items())),
                "parsed": parsed,
                "total": total,
                "success_rate": _q(Decimal(parsed) / Decimal(total)) if total else None,
            },
            "city_days": {
                "candidates": total,
                "bucket_markets": bucket_markets,
                "priced": priced_city_days,
                "entered_this_run": len(entered_this_run),
                "traded_this_run": admitted_city_days,
            },
            "bucket_refusals": dict(sorted(bucket_refusals.items())),
            "register": {
                "records": len(register.records),
                "open": len(register.open_records()),
                "settled": len(settled),
                "excluded": len(register.excluded_records()),
                "with_contracts": sum(1 for r in register.records.values() if r.contracts > ZERO),
                "settlement_checks_this_run": settlement_counts,
            },
            "verdict": verdict(settled_results, parameters=params).as_dict(),
            "verdict_provisional_including_metar": verdict(provisional_results, parameters=params).as_dict(),
            "metar_venue_agreement": {
                "checked": len(agreement),
                "agree": sum(1 for a in agreement if a),
                "disagree": sum(1 for a in agreement if not a),
                "note": "METAR-derived whole-degree extreme vs the venue's winning bucket; a disagreement is a station / rounding / source mismatch signal",
            },
            "brier": {
                "n_settled": len(model_briers),
                "model_mean": _q(sum(model_briers, ZERO) / len(model_briers)) if model_briers else None,
                "market_mean": _q(sum(market_briers, ZERO) / len(market_briers)) if market_briers else None,
            },
            "measurements": rows,
            "records": [r.as_dict() for r in register.records.values()],
            "network_status": _network_status(feed_name),
        }
    )
    summary.settlement_risk_flag = False
    summary.notes = (
        "Polymarket daily highest/lowest temperature buckets vs a free multi-model ensemble (Open-Meteo: GEFS, "
        "ECMWF IFS, ICON-EPS members) at the exact settlement station parsed from the rules text (fail-closed on "
        "missing or ambiguous stations). Member daily max/min rounded to whole degrees gives bucket probabilities "
        "(alpha 0.5 smoothing); a bucket is bought (YES or NO, taker at the touch) only when the model edge net of "
        "the Polymarket weather taker fee clears 3c, with caps $10/order, $20/bucket, $40/city-day, $1,000 total "
        "cost basis (= paper cash, never borrows), two-sided books with spread <= 10c, and RiskManager rails. City-days settle on the venue resolution only; METAR-derived outcomes are a provisional cross-check. "
        "Pre-registered pass: mean net PnL per contract >= 2c across >= 30 venue-settled city-days with 95% lower "
        "bound > 0. Full register: weather_buckets_latest.json."
    )
    return summary


def _network_status(feed_name: str) -> str:
    if feed_name == "none":
        return "UNKNOWN: no weather feed configured in this run (the default network feed is Open-Meteo + aviationweather.gov, both free and keyless)"
    if feed_name == "fixture":
        return "fixture_synthetic"
    return "measured_against_free_public_feeds"


def track_status(feed_name: str, summary: TrackSummary, snapshot: VenueSnapshot) -> str:
    if feed_name == "none":
        return "no_weather_feed"
    if feed_name == "fixture":
        return "fixture_synthetic"
    if not snapshot.groups and snapshot.errors:
        return "venue_snapshot_errors"
    if not snapshot.groups:
        return "no_weather_markets"
    if summary.candidates and summary.refused_by_reason.get("forecast_unavailable", 0) == summary.candidates:
        return "feed_errors"
    return "measured"


# --------------------------------------------------------------------------
# Replay (fixtures)
# --------------------------------------------------------------------------
@dataclass(slots=True)
class ReplayStep:
    as_of: datetime
    snapshot: VenueSnapshot
    feed: FixtureWeatherFeed
    resolutions: dict[str, dict[str, dict[str, Any]]]
    label: str = ""


def snapshot_from_events(events: list[dict[str, Any]], order_books: dict[str, Any], *, source: str = "fixture") -> VenueSnapshot:
    """Gamma-shaped events plus ``{market_id: {yes: {...}, no: {...}}}`` books -> snapshot."""
    snapshot = VenueSnapshot(venue=Venue.POLYMARKET, source=source)
    for event in events:
        group = group_from_event(event, source=source)
        if group is not None:
            snapshot.groups.append(group)
    snapshot.markets = [m for g in snapshot.groups for m in g.markets]
    for market_id, books in (order_books or {}).items():
        snapshot.books[market_id] = _book_from_levels(market_id, books.get("yes", {}))
        snapshot.no_books[market_id] = _book_from_levels(market_id, books.get("no", {}))
    for market in snapshot.markets:
        snapshot.books.setdefault(market.market_id, OrderBook(market_id=market.market_id))
        snapshot.no_books.setdefault(market.market_id, OrderBook(market_id=market.market_id))
    return snapshot


def load_replay(path: Path = REPLAY_FIXTURE, *, registry: StationRegistry | None = None) -> list[ReplayStep]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    steps: list[ReplayStep] = []
    for item in payload["steps"]:
        as_of = parse_time(item["as_of"])
        if as_of is None:
            raise ValueError(f"replay step {item.get('label')!r} has no valid as_of")
        steps.append(
            ReplayStep(
                as_of=as_of,
                snapshot=snapshot_from_events(item.get("events") or [], item.get("order_books") or {}),
                feed=FixtureWeatherFeed.from_step(item, registry=registry),
                resolutions={str(k): v for k, v in (item.get("resolutions") or {}).items()},
                label=str(item.get("label") or ""),
            )
        )
    return steps


def _runtime(snapshot: VenueSnapshot, ledger: PaperLedger | None, limits: RiskLimits, starting_cash: Decimal, model_fees: bool) -> TrackRuntime:
    return TrackRuntime.create(WEATHER_TRACK, {Venue.POLYMARKET: snapshot}, ledger=ledger, risk_limits=limits, starting_cash=starting_cash, model_fees=model_fees)


def _finish(runtime: TrackRuntime, *, label: str) -> TrackSummary:
    runtime.finalize(label=label)
    runtime.summary.metrics["venue_pnl"] = _venue_pnl(runtime.ledger)
    snap = runtime.snapshots[Venue.POLYMARKET]
    runtime.summary.metrics["snapshot"] = {
        Venue.POLYMARKET.value: {"source": snap.source, "markets": len(snap.markets), "groups": len(snap.groups), "errors": snap.errors}
    }
    return runtime.summary


def _merge(into: TrackSummary, step: TrackSummary) -> None:
    into.candidates += step.candidates
    into.admitted += step.admitted
    into.proposed_orders += step.proposed_orders
    into.paper_fills += step.paper_fills
    for reason, count in step.refused_by_reason.items():
        into.refused_by_reason[reason] = into.refused_by_reason.get(reason, 0) + count
    into.fills.extend(step.fills)
    into.edges.extend(step.edges)
    into.admitted_edges.extend(step.admitted_edges)


async def replay_fixture(
    steps: list[ReplayStep] | None = None,
    *,
    ledger: PaperLedger | None = None,
    register: CityDayRegister | None = None,
    parameters: WeatherEdgeParameters | None = None,
    risk_limits: RiskLimits | None = None,
    starting_cash: Decimal = DEFAULT_STARTING_CASH,
    model_fees: bool = True,
) -> tuple[TrackSummary, PaperLedger, CityDayRegister, list[TrackSummary]]:
    """Replay every fixture step through the live code path; returns the aggregate."""
    steps = steps or load_replay()
    register = register or CityDayRegister()
    limits = risk_limits or WEATHER_RISK_LIMITS
    step_summaries: list[TrackSummary] = []
    runtime: TrackRuntime | None = None
    for step in steps:
        runtime = _runtime(step.snapshot, ledger, limits, starting_cash, model_fees)
        ledger = runtime.ledger

        async def lookup(record: CityDayRecord, _resolutions: dict[str, dict[str, dict[str, Any]]] = step.resolutions) -> dict[str, dict[str, Any]] | None:
            return _resolutions.get(record.record_id) or _resolutions.get(record.slug)

        summary = await run_weather_cycle(
            runtime, register, feed=step.feed, parameters=parameters, as_of=step.as_of, resolution_lookup=lookup, model_fees=model_fees,
        )
        summary.metrics["step"] = step.label
        step_summaries.append(_finish(runtime, label=f"fixtures:{step.label or step.as_of.isoformat()}"))
    assert runtime is not None and ledger is not None
    aggregate = TrackSummary(WEATHER_TRACK, label=WEATHER_TRACK_LABEL)
    for step_summary in step_summaries:
        _merge(aggregate, step_summary)
    last = step_summaries[-1]
    station_counts: dict[str, int] = {}
    bucket_refusals: dict[str, int] = {}
    city_days = {"candidates": 0, "bucket_markets": 0, "priced": 0, "entered_this_run": 0, "traded_this_run": 0}
    for s in step_summaries:
        for key, value in s.metrics.get("station_parse", {}).get("counts", {}).items():
            station_counts[key] = station_counts.get(key, 0) + int(value)
        for key, value in s.metrics.get("bucket_refusals", {}).items():
            bucket_refusals[key] = bucket_refusals.get(key, 0) + int(value)
        for key in city_days:
            city_days[key] += int(s.metrics.get("city_days", {}).get(key, 0))
    parsed, total = station_counts.get("parsed", 0), sum(station_counts.values())
    aggregate.metrics = {
        **last.metrics,
        "station_parse": {
            "counts": dict(sorted(station_counts.items())),
            "parsed": parsed,
            "total": total,
            "success_rate": _q(Decimal(parsed) / Decimal(total)) if total else None,
        },
        "bucket_refusals": dict(sorted(bucket_refusals.items())),
        "city_days": city_days,
        "steps": [s.metrics.get("step") for s in step_summaries],
        "replayed_steps": len(step_summaries),
        "settlement_fills": sum(int(s.metrics.get("settlement_fills", 0)) for s in step_summaries),
    }
    aggregate.notes = last.notes
    aggregate.ledger = last.ledger
    aggregate.settlement_risk_flag = False
    return aggregate, ledger, register, step_summaries


async def measure_weather_buckets(
    *,
    use_fixtures: bool = True,
    limit: int = 120,
    feed: WeatherFeed | None = None,
    parameters: WeatherEdgeParameters | None = None,
    ledger: PaperLedger | None = None,
    register: CityDayRegister | None = None,
    risk_limits: RiskLimits | None = None,
    starting_cash: Decimal = DEFAULT_STARTING_CASH,
    model_fees: bool = True,
    as_of: datetime | None = None,
    snapshot: VenueSnapshot | None = None,
    resolution_lookup: ResolutionLookup | None = None,
) -> tuple[TrackSummary, PaperLedger, CityDayRegister]:
    """Fixtures: replay the committed steps. Network: one cycle against public data."""
    if use_fixtures:
        summary, ledger, register, _ = await replay_fixture(
            ledger=ledger, register=register, parameters=parameters, risk_limits=risk_limits, starting_cash=starting_cash, model_fees=model_fees,
        )
        return summary, ledger, register
    as_of = as_of or _now()
    register = register or CityDayRegister()
    feed = feed or NullWeatherFeed(load_station_registry())
    snapshot = snapshot or await capture_weather_snapshots(limit=limit)
    runtime = _runtime(snapshot, ledger, risk_limits or WEATHER_RISK_LIMITS, starting_cash, model_fees)
    lookup = resolution_lookup if resolution_lookup is not None else network_resolution_lookup
    summary = await run_weather_cycle(runtime, register, feed=feed, parameters=parameters, as_of=as_of, resolution_lookup=lookup, model_fees=model_fees)
    _finish(runtime, label=f"network:{as_of.isoformat()}")
    return summary, runtime.ledger, register


__all__ = [
    "BucketMarket",
    "CityDay",
    "CityDayRecord",
    "CityDayRegister",
    "DAILY_TEMPERATURE_TAG_ID",
    "REPLAY_FIXTURE",
    "ReplayStep",
    "TRACK_FAMILY",
    "WEATHER_TRACK",
    "WEATHER_TRACKS",
    "WEATHER_TRACK_LABEL",
    "capture_weather_snapshot",
    "capture_weather_snapshots",
    "classify_venue_resolution",
    "load_replay",
    "measure_weather_buckets",
    "network_resolution_lookup",
    "parse_city_day",
    "parse_slug",
    "register_path",
    "replay_fixture",
    "run_weather_cycle",
    "snapshot_from_events",
    "track_status",
]
