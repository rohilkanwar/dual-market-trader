"""``weather_dead_bucket``: paper track for Polymarket daily-temperature dead buckets.

One paper track with its own :class:`core.ledger.PaperLedger`, the FLB-style
paper caps ($25/order, 75 contracts/market, $75 daily loss) and a persistent
**position register** carried across runs:

1. discover Polymarket "Highest temperature in <city> on <date>?" events
   (Gamma tag ``104596``; YES and NO book per bucket leg) or replay the
   committed synthetic fixture;
2. parse each event's settlement spec fail-closed (station ICAO from the
   resolution URL, local date, unit, timezone) - ``station_unparsed``,
   ``date_unparsed``, ``unit_unparsed``, ``station_timezone_unknown``,
   ``market_kind_unsupported``;
3. fetch the station's free public observations for the local day
   (``research.weather_obs``) - ``no_obs``, ``station_mismatch``, ``stale_obs``;
4. fold them into the running high and classify every bucket
   (``strategies.weather_dead_bucket.classify_bucket``): dead buckets are bought
   as NO, certain buckets as YES, when the ask leaves ``>= 2c`` net after the
   venue taker fee; every other leg is refused with its reason
   (``too_early_in_day``, ``not_falling``, ``live``, ``bucket_edge_ambiguous``,
   ``edge_below_threshold``, ``no_ask``, ``insufficient_depth`` ...);
5. later runs settle open records from the venue's resolution (Gamma
   ``outcomePrices``), book 1/0 on the ledger and record the
   observation-implied outcome alongside, so resolution-source basis risk is
   measured rather than assumed.

Network runs read only unauthenticated endpoints; nothing here can place a
venue order. Fixture replays prove the arithmetic and every refusal branch;
their numbers are synthetic and carry no evidence about the hypothesis.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.ledger import PaperLedger
from core.risk import RiskLimits
from core.types import ONE, ZERO, Market, MarketGroup, OrderBook, Outcome, Venue
from research.scoreboard import DEFAULT_STARTING_CASH, TrackRuntime, TrackSummary, VenueSnapshot, _venue_pnl
from research.weather_obs import (
    FREE_SOURCES,
    NullObservationSource,
    ObservationBatch,
    ObservationSource,
    StaticObservationSource,
    parse_fixture_observations,
    parse_time,
)
from strategies.weather_dead_bucket import (
    TRACK,
    TRACK_LABEL,
    WEATHER_RISK_LIMITS,
    BucketVerdict,
    DeadBucketParameters,
    DeadBucketStrategy,
    KillRule,
    RunningHigh,
    SettledRecord,
    TemperatureBucket,
    WeatherMarketSpec,
    classify_bucket,
    local_day_window,
    parse_bucket_label,
    parse_weather_market,
    running_high,
    verdict,
)
from venues.polymarket.client import PolymarketClient, _book_from_levels, group_from_event

# Track-id contract with the dashboard wiring swarm (``research/weather_tracks.py`` on its
# branch): family ``weather``, reserved ids below, ``scoreboard_weather.json`` with
# ``meta.track_family = "weather"`` and ``weather_report_<mode>.json`` (kind
# ``weather_report``). Imported when present so the ids/labels have one source of
# truth; mirrored here so this branch runs on its own.
try:  # pragma: no cover - exercised only once research.weather_tracks has merged
    from research.weather_tracks import (  # type: ignore[import-not-found]
        WEATHER_FAMILY,
        WEATHER_REPORT_KIND,
        WEATHER_SCOREBOARD_NAME,
        WEATHER_TRACK_LABELS,
        WEATHER_TRACKS as RESERVED_WEATHER_TRACKS,
    )
except ImportError:
    WEATHER_FAMILY = "weather"
    WEATHER_REPORT_KIND = "weather_report"
    WEATHER_SCOREBOARD_NAME = "scoreboard_weather.json"
    RESERVED_WEATHER_TRACKS: tuple[str, ...] = ("weather_bucket_edge", "weather_dead_bucket", "weather_calibrated_ensemble")
    WEATHER_TRACK_LABELS: dict[str, str] = {
        "weather_bucket_edge": "Weather bucket edge (ensemble vs. mid)",
        "weather_dead_bucket": TRACK_LABEL,
        "weather_calibrated_ensemble": "Weather calibrated ensemble",
    }

WEATHER_TRACK = TRACK
assert WEATHER_TRACK in RESERVED_WEATHER_TRACKS, "weather_dead_bucket must stay a reserved weather id"
DEAD_BUCKET_TRACKS: tuple[str, ...] = (WEATHER_TRACK,)
WEATHER_TRACK_LABEL = WEATHER_TRACK_LABELS.get(WEATHER_TRACK, TRACK_LABEL)
POLYMARKET_HIGHEST_TEMPERATURE_TAG = 104596
POLYMARKET_DAILY_TEMPERATURE_TAG = 103040
REPLAY_FIXTURE = Path(__file__).with_name("fixtures") / "weather_dead_bucket_replay.json"
REPORT_FILE = "weather_report_<mode>.json"
REGISTER_SCHEMA = "1.0.0"
Q4 = Decimal("0.0001")
# ``metrics.evaluation.status`` vocabulary shared with the specialist lane and the weather contract.
EVALUATION_STATUSES = ("not_run", "no_candidates", "pending_resolutions", "underpowered", "pass", "fail")


def _now() -> datetime:
    return datetime.now(UTC)


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4) if value is not None else None


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


# --------------------------------------------------------------------------
# Weather events in a snapshot
# --------------------------------------------------------------------------
@dataclass(slots=True)
class WeatherEvent:
    group: MarketGroup
    spec: WeatherMarketSpec | None
    reason: str  # parsed | <refusal>
    detail: str
    description: str
    buckets: dict[str, TemperatureBucket | None] = field(default_factory=dict)

    @property
    def parsed(self) -> bool:
        return self.spec is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.group.group_id,
            "title": self.group.title,
            "legs": self.group.size,
            "reason": self.reason,
            "detail": self.detail,
            "spec": self.spec.as_dict() if self.spec else None,
            "buckets": {k: (b.as_dict() if b else None) for k, b in self.buckets.items()},
        }


def event_description(group: MarketGroup) -> str:
    text = str(group.metadata.get("description") or "")
    if not text:
        for market in group.markets:
            text = str(market.metadata.get("resolution_text") or "")
            if text:
                break
    return text


def event_source_url(group: MarketGroup) -> str | None:
    url = group.metadata.get("resolution_source")
    if url:
        return str(url)
    for market in group.markets:
        if market.metadata.get("source_url"):
            return str(market.metadata["source_url"])
    return None


def bucket_label_of(market: Market) -> str:
    return str(market.metadata.get("group_item_title") or market.title or "")


def weather_events(snapshot: VenueSnapshot, *, station_timezones: dict[str, str] | None = None) -> list[WeatherEvent]:
    """Parse every group in the snapshot; unparseable events keep their reason."""
    out: list[WeatherEvent] = []
    for group in snapshot.groups:
        description = event_description(group)
        parse = parse_weather_market(
            event_title=group.title,
            description=description,
            resolution_source_url=event_source_url(group),
            station_timezones=station_timezones,
        )
        event = WeatherEvent(group=group, spec=parse.spec, reason=parse.reason, detail=parse.detail, description=description)
        if parse.spec is not None:
            for market in group.markets:
                event.buckets[market.market_id] = parse_bucket_label(bucket_label_of(market), parse.spec.unit)
        out.append(event)
    return out


# --------------------------------------------------------------------------
# Position register
# --------------------------------------------------------------------------
@dataclass(slots=True)
class DeadBucketRecord:
    record_id: str  # market id
    event_id: str
    event_title: str
    city: str
    station: str
    local_date: str
    unit: str
    timezone: str
    bucket_label: str
    bucket_lo: int | None
    bucket_hi: int | None
    rule: str
    buy_outcome: str  # yes | no
    lookup: str  # Gamma slug (or market id) for the settlement check
    opened_at: str
    local_time_at_entry: str
    running_high_low: int | None
    running_high_high: int | None
    running_high_all_reports: int | None
    latest_temp_at_entry: int | None
    quantity: str
    entry_price: str
    yes_equivalent_price: str
    entry_fee: str
    gross_edge: str
    net_edge: str
    expected_net_pnl: str
    status: str = "open"  # open | settled
    venue_outcome: str | None = None  # yes | no
    venue_detail: str | None = None
    obs_final_high_low: int | None = None
    obs_final_high_high: int | None = None
    obs_implied_outcome: str | None = None  # yes | no | ambiguous
    agreement: str = "unknown"  # agree | disagree | unknown
    realized_pnl: str | None = None
    net_per_contract: str | None = None
    won: bool | None = None
    settled_at: str | None = None
    settlement_checks: int = 0
    settlement_detail: str | None = None

    @property
    def station_day(self) -> str:
        return f"{self.station}:{self.local_date}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def settled(self) -> SettledRecord | None:
        if self.status != "settled" or self.realized_pnl is None or self.won is None:
            return None
        return SettledRecord(
            station_day=self.station_day,
            rule=KillRule(self.rule),
            buy_outcome=Outcome(self.buy_outcome),
            quantity=Decimal(self.quantity),
            net_pnl=Decimal(self.realized_pnl),
            won=self.won,
        )


@dataclass(slots=True)
class DeadBucketRegister:
    records: dict[str, DeadBucketRecord] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: _now().isoformat())
    updated_at: str = field(default_factory=lambda: _now().isoformat())

    def open_records(self) -> list[DeadBucketRecord]:
        return [r for r in self.records.values() if r.status == "open"]

    def settled_records(self) -> list[DeadBucketRecord]:
        return [r for r in self.records.values() if r.status == "settled"]

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
    def from_dict(cls, payload: dict[str, Any]) -> DeadBucketRegister:
        if payload.get("paper_only") is not True:
            raise ValueError("refusing to load a register that is not marked paper_only")
        register = cls(created_at=payload.get("created_at", _now().isoformat()), updated_at=payload.get("updated_at", _now().isoformat()))
        for item in payload.get("records", []):
            record = DeadBucketRecord(**item)
            register.records[record.record_id] = record
        return register

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = _now().isoformat()
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    @classmethod
    def load_or_create(cls, path: Path) -> DeadBucketRegister:
        if path.exists():
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls()


def register_path(artifact_dir: Path) -> Path:
    return artifact_dir / "weather_dead_bucket" / "register.json"


# --------------------------------------------------------------------------
# Venue resolution
# --------------------------------------------------------------------------
SettlementLookup = Callable[[DeadBucketRecord], Awaitable[dict[str, Any] | None]]


def classify_venue_resolution(status: dict[str, Any] | None) -> tuple[Outcome | None, str | None]:
    """Gamma market status -> resolved outcome. Only a closed market with binary prices counts."""
    if not status:
        return None, None
    prices = [_dec(p) for p in (status.get("outcome_prices") or [])]
    if not status.get("closed") or len(prices) != 2 or any(p is None for p in prices):
        return None, None
    rounded = [p.quantize(Decimal("0.01")) for p in prices]  # type: ignore[union-attr]
    if rounded == [ONE, ZERO]:
        return Outcome.YES, "polymarket_resolved_yes"
    if rounded == [ZERO, ONE]:
        return Outcome.NO, "polymarket_resolved_no"
    return None, f"polymarket_non_binary_prices:{[str(p) for p in prices]}"


async def network_settlement_lookup(record: DeadBucketRecord) -> dict[str, Any] | None:
    client = PolymarketClient(paper=True, use_fixtures=False, search_terms=None)
    try:
        return await client.get_market_status(record.lookup)
    finally:
        await client.close()


def obs_implied_outcome(record: DeadBucketRecord, high: RunningHigh) -> str | None:
    if not high.day_complete or high.high_low is None or high.high_high is None:
        return None
    bucket = TemperatureBucket(record.bucket_lo, record.bucket_hi, record.bucket_label)
    low, up = bucket.contains(high.high_low), bucket.contains(high.high_high)
    if low and up:
        return "yes"
    if not low and not up:
        return "no"
    return "ambiguous"


# --------------------------------------------------------------------------
# One cycle
# --------------------------------------------------------------------------
def _fetch_window(events: list[WeatherEvent], as_of: datetime) -> tuple[set[str], datetime]:
    stations: set[str] = set()
    start = as_of
    for event in events:
        if event.spec is None:
            continue
        stations.add(event.spec.station_icao)
        day_start, _ = local_day_window(event.spec.local_date, event.spec.timezone)
        start = min(start, day_start)
    return stations, start


def _event_status(event: WeatherEvent, high: RunningHigh | None, batch: ObservationBatch, params: DeadBucketParameters, as_of: datetime) -> tuple[str, str]:
    """Event-level refusal (reason, detail) or ("ok", "")."""
    spec = event.spec
    assert spec is not None
    rows = batch.for_station(spec.station_icao)
    if not rows:
        errors = [e for e in batch.errors if spec.station_icao in e]
        return "no_obs", errors[0] if errors else f"observation source {batch.source} returned nothing for {spec.station_icao}"
    if not any(o.station_icao == spec.station_icao for o in rows):
        return "station_mismatch", f"observations carry {sorted({o.station_icao for o in rows})}, market settles on {spec.station_icao}"
    assert high is not None
    from zoneinfo import ZoneInfo

    local_now = as_of.astimezone(ZoneInfo(spec.timezone))
    if high.hourly_observations == 0:
        if local_now.date() < spec.local_date:
            return "too_early_in_day", f"local date {local_now.date().isoformat()} precedes market date {spec.local_date.isoformat()}"
        return "no_obs", f"no hourly observations inside {spec.local_date.isoformat()} local"
    if not high.day_complete:
        assert high.latest_observed_at is not None
        age = as_of - high.latest_observed_at
        if age > timedelta(minutes=params.max_obs_age_minutes):
            return "stale_obs", f"latest hourly observation is {int(age.total_seconds() // 60)} min old (max {params.max_obs_age_minutes})"
    return "ok", ""


async def run_weather_dead_bucket_cycle(
    runtime: TrackRuntime,
    register: DeadBucketRegister,
    *,
    observations: ObservationSource,
    parameters: DeadBucketParameters | None = None,
    as_of: datetime | None = None,
    settlement_lookup: SettlementLookup | None = None,
    station_timezones: dict[str, str] | None = None,
) -> TrackSummary:
    params = parameters or DeadBucketParameters()
    as_of = as_of or _now()
    summary = runtime.summary
    summary.label = WEATHER_TRACK_LABEL
    snapshot = runtime.snapshots.get(Venue.POLYMARKET) or VenueSnapshot(venue=Venue.POLYMARKET, source="none")
    strategy = DeadBucketStrategy(params, portfolio=runtime.ledger.portfolio, risk=runtime.risk)
    events = weather_events(snapshot, station_timezones=station_timezones)

    stations, start = _fetch_window(events, as_of)
    try:
        batch = await observations.fetch(stations, start=start, end=as_of + timedelta(minutes=1)) if stations else ObservationBatch(source=getattr(observations, "name", "none"))
    except Exception as exc:  # the observation feed must never take the run down
        batch = ObservationBatch(source=getattr(observations, "name", type(observations).__name__))
        batch.errors.append(f"fetch: {type(exc).__name__}: {exc}")

    event_rows: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    highs: dict[str, tuple[RunningHigh, str]] = {}
    resting_only: list[dict[str, Any]] = []
    opened = 0
    for event in events:
        event_row = event.as_dict()
        event_rows.append(event_row)
        if event.spec is None:
            for market in event.group.markets:
                summary.candidates += 1
                summary.refuse(event.reason)
                rows.append({"event_id": event.group.group_id, "market_id": market.market_id, "bucket": bucket_label_of(market), "reason": event.reason, "detail": event.detail})
            continue
        spec = event.spec
        obs_rows = [o for o in batch.for_station(spec.station_icao) if o.station_icao == spec.station_icao]
        high = running_high(obs_rows, spec) if obs_rows else None
        status, detail = _event_status(event, high, batch, params, as_of)
        event_row["observation_status"] = status
        event_row["observation_detail"] = detail
        if high is not None:
            event_row["running_high"] = high.as_dict(spec.timezone)
            highs[f"{spec.station_icao}:{spec.local_date.isoformat()}"] = (high, spec.timezone)
            for record in register.open_records():
                if record.station == spec.station_icao and record.local_date == spec.local_date.isoformat() and high.day_complete:
                    record.obs_final_high_low, record.obs_final_high_high = high.high_low, high.high_high
                    record.obs_implied_outcome = obs_implied_outcome(record, high)
        for market in event.group.markets:
            summary.candidates += 1
            bucket = event.buckets.get(market.market_id)
            row: dict[str, Any] = {
                "event_id": event.group.group_id,
                "market_id": market.market_id,
                "title": market.title,
                "bucket": bucket_label_of(market),
                "station": spec.station_icao,
                "local_date": spec.local_date.isoformat(),
            }
            rows.append(row)
            if status != "ok":
                row["reason"], row["detail"] = status, detail
                summary.refuse(status)
                continue
            if bucket is None:
                row["reason"] = "bucket_unparsed"
                summary.refuse("bucket_unparsed")
                continue
            if market.market_id in register.records:
                row["reason"] = "already_positioned"
                summary.refuse("already_positioned")
                continue
            assert high is not None
            bucket_verdict: BucketVerdict = classify_bucket(bucket, high, spec, params, as_of=as_of)
            row["verdict"] = bucket_verdict.as_dict()
            if bucket_verdict.outcome is None:
                row["reason"] = bucket_verdict.status
                summary.refuse(bucket_verdict.status)
                continue
            evaluation = strategy.evaluate(market, snapshot.book(market), snapshot.no_book(market), bucket_verdict, spec=spec, high=high)
            row["evaluation"] = evaluation.as_dict()
            if not evaluation.traded:
                row["reason"] = evaluation.reason
                summary.refuse(evaluation.reason)
                summary.edges.append(_edge_row(runtime.name, market, evaluation, spec, filled=False))
                if evaluation.reason == "no_ask" and evaluation.bid is not None:
                    resting_only.append(
                        {
                            "event_id": event.group.group_id,
                            "market_id": market.market_id,
                            "title": market.title,
                            "bucket": bucket.label,
                            "rule": bucket_verdict.rule.value if bucket_verdict.rule else None,
                            "buy_outcome": bucket_verdict.outcome.value,
                            "best_bid": _q(evaluation.bid),
                            "best_bid_size": evaluation.bid_size,
                            "resting_gross_edge_at_best_bid": _q(ONE - evaluation.bid),
                            "note": "no taker ask on the certain side; only a resting order at or above best_bid could enter",
                        }
                    )
                continue
            summary.admitted += 1
            summary.proposed_orders += len(evaluation.orders)
            summary.admitted_edges.append(evaluation.net_edge or ZERO)
            summary.estimated_fees_buffer += (evaluation.fee_per_contract or ZERO) * evaluation.quantity
            fills_before = summary.paper_fills
            filled_qty = ZERO
            fees = ZERO
            price: Decimal | None = None
            for order in evaluation.orders:
                report = await runtime.submit(order, edge=evaluation.net_edge)
                if report is not None and report.fills:
                    filled_qty += sum((f.quantity for f in report.fills), ZERO)
                    fees += sum((f.fee for f in report.fills), ZERO)
                    price = sum((f.quantity * f.price for f in report.fills), ZERO) / filled_qty
            row["paper_fills"] = summary.paper_fills - fills_before
            summary.edges.append(_edge_row(runtime.name, market, evaluation, spec, filled=summary.paper_fills > fills_before))
            if filled_qty <= ZERO or price is None:
                row["reason"] = "trade_unfilled"
                summary.refuse("trade_unfilled")
                continue
            row["reason"] = "trade"
            yes_price = price if bucket_verdict.outcome is Outcome.YES else ONE - price
            record = DeadBucketRecord(
                record_id=market.market_id,
                event_id=event.group.group_id,
                event_title=event.group.title,
                city=spec.city,
                station=spec.station_icao,
                local_date=spec.local_date.isoformat(),
                unit=spec.unit.value,
                timezone=spec.timezone,
                bucket_label=bucket.label,
                bucket_lo=bucket.lo,
                bucket_hi=bucket.hi,
                rule=bucket_verdict.rule.value if bucket_verdict.rule else "",
                buy_outcome=bucket_verdict.outcome.value,
                lookup=str(market.metadata.get("slug") or market.market_id),
                opened_at=as_of.isoformat(),
                local_time_at_entry=high.as_dict(spec.timezone)["latest_observed_at_local"] or "",
                running_high_low=high.high_low,
                running_high_high=high.high_high,
                running_high_all_reports=high.all_high_high,
                latest_temp_at_entry=high.latest_temp_high,
                quantity=str(filled_qty),
                entry_price=str(_q(price)),
                yes_equivalent_price=str(_q(yes_price)),
                entry_fee=str(fees),
                gross_edge=str(_q(ONE - price)),
                net_edge=str(_q(evaluation.net_edge)),
                expected_net_pnl=str(_q((ONE - price) * filled_qty - fees)),
            )
            if high.day_complete:
                record.obs_final_high_low, record.obs_final_high_high = high.high_low, high.high_high
                record.obs_implied_outcome = obs_implied_outcome(record, high)
            register.records[record.record_id] = record
            opened += 1

    settled_now = await _settle(runtime, register, settlement_lookup)

    settled = [r.settled() for r in register.settled_records()]
    settled_rows = [s for s in settled if s is not None]
    agreement = {"agree": 0, "disagree": 0, "unknown": 0}
    for record in register.settled_records():
        agreement[record.agreement] = agreement.get(record.agreement, 0) + 1

    parsed_events = [e for e in events if e.parsed]
    stations_parsed = {
        e.spec.station_icao for e in parsed_events if e.spec is not None
        and any(o.station_icao == e.spec.station_icao for o in batch.for_station(e.spec.station_icao))
    }
    classified = [r for r in rows if r.get("verdict")]
    kills = [r for r in classified if r["verdict"].get("status") == "dead"]
    certain = [r for r in classified if r["verdict"].get("status") == "certain_yes"]
    no_verdict = verdict(settled_rows, parameters=params, side=Outcome.NO)
    summary.metrics.update(
        {
            # research/weather_tracks.py METRIC_KEYS (the dashboard's findings.weather headline).
            "family": WEATHER_FAMILY,
            "track_family": WEATHER_FAMILY,
            "detail_file": REPORT_FILE,
            "status": track_status(snapshot, batch, events, summary),
            "source": {"name": batch.source, "requests": batch.requests, "errors": len(batch.errors), "note": batch.note},
            "markets": len(events),
            "buckets": summary.candidates,
            "cities": sorted({e.spec.city for e in parsed_events if e.spec is not None}),
            "stations": len(stations),
            "stations_parsed": len(stations_parsed),
            "dead_bucket": {
                "candidates": len(classified),
                "kills": len(kills),
                "certain_yes": len(certain),
                "kills_without_taker_ask": len(resting_only),
                "positions_opened": opened,
            },
            "evaluation": {
                "status": evaluation_status(no_verdict, open_records=len(register.open_records()), candidates=len(classified)),
                "preregistered_n": params.min_settled_positions,
                "preregistered_station_days": params.min_station_days,
                "n": no_verdict.n,
                "station_days": no_verdict.station_days,
                "verdict": no_verdict.status,
                "kill_rule_triggered": no_verdict.kill_rule_triggered,
            },
            "as_of": as_of.isoformat(),
            "parameters": params.as_dict(),
            "observation_source": batch.as_dict(),
            "free_sources": FREE_SOURCES,
            "weather_events": event_rows,
            "events_parsed": sum(1 for e in events if e.parsed),
            "events_total": len(events),
            "running_highs": {k: high.as_dict(tz) for k, (high, tz) in highs.items()},
            "positions_opened": opened,
            "positions_settled_this_run": settled_now,
            "dead_buckets_without_taker_ask": len(resting_only),
            "resting_only_best_bid": _best_bid_summary(resting_only),
            "resting_only_opportunities": resting_only,
            "register": {
                "records": len(register.records),
                "open": len(register.open_records()),
                "settled": len(register.settled_records()),
                "station_days": len({r.station_day for r in register.records.values()}),
                "agreement_venue_vs_observations": agreement,
            },
            "verdict": no_verdict.as_dict(),
            "verdict_certain_yes": verdict(settled_rows, parameters=params, side=Outcome.YES).as_dict(),
            "measurements": rows,
            "records": [r.as_dict() for r in register.records.values()],
            "network_status": _network_status(snapshot, batch),
        }
    )
    summary.settlement_risk_flag = False
    summary.notes = (
        "Polymarket daily highest-temperature buckets vs. the settlement station's own public METAR/ASOS "
        "observations (aviationweather.gov, NWS fallback; $0, no key). A bucket entirely below the hourly running "
        "high is dead at any hour; after 17:00 local with the temperature >= 2°F (1°C) below the high and not "
        "rising, buckets more than 1° above the all-report high are dead and the bucket covering the plausible "
        "range is the certain YES; once the first observation of the next local date exists the final high decides "
        "every leg. Dead legs are bought as NO (certain legs as YES) at the touch when the ask leaves >= 2c net after "
        "the 5 % weather taker fee, $25/order, 75 contracts/market. Positions settle on the venue's resolution and "
        "the observation-implied outcome is recorded next to it. Pre-registered: PASS at n >= 30 settled NO "
        "positions over >= 10 station-days with mean net >= 2c/contract and zero dead-bucket losses; one loss trips "
        "the kill rule. Rounding at exactly .5 and SPECI-vs-hourly differences are handled fail-closed."
    )
    return summary


async def _settle(runtime: TrackRuntime, register: DeadBucketRegister, lookup: SettlementLookup | None) -> int:
    settled = 0
    for record in register.open_records():
        record.settlement_checks += 1
        status: dict[str, Any] | None = None
        if lookup is not None:
            try:
                status = await lookup(record)
            except Exception as exc:  # a dead endpoint leaves the record open
                record.settlement_detail = f"lookup_error: {type(exc).__name__}: {exc}"
                continue
        outcome, detail = classify_venue_resolution(status)
        if detail is not None:
            record.settlement_detail = detail
        if outcome is None:
            continue
        venue = Venue.POLYMARKET
        runtime.ledger.settle(venue, record.record_id, outcome)
        position = runtime.ledger.portfolio.get(venue, record.record_id)
        realized = position.realized_pnl if position is not None else ZERO
        record.status = "settled"
        record.venue_outcome = outcome.value
        record.venue_detail = detail
        record.realized_pnl = str(_q(realized))
        quantity = Decimal(record.quantity)
        record.net_per_contract = str(_q(realized / quantity)) if quantity > ZERO else None
        record.won = outcome.value == record.buy_outcome
        record.settled_at = _now().isoformat()
        if record.obs_implied_outcome in ("yes", "no"):
            record.agreement = "agree" if record.obs_implied_outcome == outcome.value else "disagree"
        else:
            record.agreement = "unknown"
        runtime.summary.metrics["settlement_fills"] = int(runtime.summary.metrics.get("settlement_fills", 0)) + 1
        runtime.summary.fills.append(
            {
                "track": runtime.name,
                "venue": venue.value,
                "market": record.record_id,
                "title": f"{record.event_title} [{record.bucket_label}]",
                "side": "sell" if record.buy_outcome == "yes" else "buy",
                "outcome": "yes",
                "qty": quantity,
                "price": ONE if outcome is Outcome.YES else ZERO,
                "yes_equivalent_price": ONE if outcome is Outcome.YES else ZERO,
                "fee": ZERO,
                "edge_bps": None,
                "paper_pnl": _q(realized),
                "filled_at": record.settled_at,
                "order_id": "settlement",
                "strategy": WEATHER_TRACK,
            }
        )
        settled += 1
    return settled


def _best_bid_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Where the resting queue sits on dead legs that have no taker ask (1 - bid = the most a resting NO buyer could earn)."""
    bids = sorted(r["best_bid"] for r in rows if r.get("best_bid") is not None)
    if not bids:
        return {"n": 0, "min": None, "median": None, "max": None, "max_resting_gross_edge": None}
    mid = len(bids) // 2
    median = bids[mid] if len(bids) % 2 else (bids[mid - 1] + bids[mid]) / 2
    return {"n": len(bids), "min": bids[0], "median": _q(median), "max": bids[-1], "max_resting_gross_edge": _q(ONE - bids[0])}


def _edge_row(track: str, market: Market, evaluation: Any, spec: WeatherMarketSpec, *, filled: bool) -> dict[str, Any]:
    return {
        "track": track,
        "venue": market.venue.value,
        "market": market.market_id,
        "title": market.title,
        "edge_bps": int((evaluation.net_edge * 10000).to_integral_value()) if evaluation.net_edge is not None else None,
        "gross_edge_bps": int((evaluation.gross_edge * 10000).to_integral_value()) if evaluation.gross_edge is not None else None,
        "admitted": evaluation.traded,
        "filled": filled,
        "reason": evaluation.reason,
        "rule": evaluation.verdict.rule.value if evaluation.verdict.rule else None,
        "bucket": evaluation.verdict.bucket.label,
        "station": spec.station_icao,
        "local_date": spec.local_date.isoformat(),
        "side": "buy",
        "outcome": evaluation.verdict.outcome.value if evaluation.verdict.outcome else None,
        "fair_value": ONE if evaluation.verdict.outcome is Outcome.YES else ZERO,
        "mid": evaluation.ask,
    }


def evaluation_status(no_verdict: Any, *, open_records: int, candidates: int) -> str:
    """Map the pre-registered verdict onto the shared evaluation vocabulary.

    ``pass`` / ``fail`` only when the verdict itself says so; ``underpowered``
    while settled positions exist below the pre-registered floors;
    ``pending_resolutions`` when positions are open but none has settled;
    ``no_candidates`` when nothing was even classified; ``not_run`` otherwise.
    """
    if no_verdict.status == "PASS":
        return "pass"
    if no_verdict.status == "FAIL":
        return "fail"
    if no_verdict.n > 0:
        return "underpowered"
    if open_records > 0:
        return "pending_resolutions"
    if candidates == 0:
        return "no_candidates"
    return "not_run"


def track_status(snapshot: VenueSnapshot, batch: ObservationBatch, events: list[WeatherEvent], summary: TrackSummary) -> str:
    if batch.source == "fixture":
        return "fixture_synthetic"
    if not events:
        return "no_weather_events"
    if not any(e.parsed for e in events):
        return "no_parseable_events"
    if batch.source == "none":
        return "no_observation_source"
    if not batch.observations and batch.errors:
        return "observation_source_errors"
    if not batch.observations:
        return "no_obs"
    return "measured"


def _network_status(snapshot: VenueSnapshot, batch: ObservationBatch) -> str:
    if batch.source == "fixture":
        return "fixture_synthetic"
    if batch.source == "none":
        return "UNKNOWN: no observation source configured"
    return f"measured_against_free_source ({batch.source})"


# --------------------------------------------------------------------------
# Snapshots (network) and replay (fixtures)
# --------------------------------------------------------------------------
async def capture_weather_snapshot(*, limit: int = 60, tag_id: int = POLYMARKET_HIGHEST_TEMPERATURE_TAG, client: PolymarketClient | None = None) -> VenueSnapshot:
    """Public read-only capture of Polymarket daily-temperature events with both books per leg."""
    owned = client is None
    client = client or PolymarketClient(paper=True, use_fixtures=False, search_terms=None)
    snapshot = VenueSnapshot(venue=Venue.POLYMARKET, source="network")
    try:
        try:
            groups = await client.list_events_by_tag(tag_id, limit=limit)
        except Exception as exc:
            snapshot.errors.append(f"list_events_by_tag[{tag_id}]: {type(exc).__name__}: {exc}")
            return snapshot
        events = await client.capture_events(groups=groups)
        snapshot.groups = list(events.groups)
        snapshot.markets = list(events.markets)
        snapshot.books = dict(events.yes_books)
        snapshot.no_books = dict(events.no_books)
        snapshot.errors.extend(events.errors)
    finally:
        if owned:
            await client.close()
    return snapshot


def snapshot_from_fixture(payload: dict[str, Any]) -> VenueSnapshot:
    """``{"events": [...], "order_books": {market_id: {"yes": {...}, "no": {...}}}}`` in the events-fixture shape."""
    snapshot = VenueSnapshot(venue=Venue.POLYMARKET, source="fixture")
    for event in payload.get("events", []):
        group = group_from_event(event, source="fixture")
        if group is not None:
            snapshot.groups.append(group)
    snapshot.markets = [m for g in snapshot.groups for m in g.markets]
    for market_id, books in (payload.get("order_books") or {}).items():
        snapshot.books[market_id] = _book_from_levels(market_id, books.get("yes", {}))
        snapshot.no_books[market_id] = _book_from_levels(market_id, books.get("no", {}))
    for market in snapshot.markets:
        snapshot.books.setdefault(market.market_id, OrderBook(market_id=market.market_id))
        snapshot.no_books.setdefault(market.market_id, OrderBook(market_id=market.market_id))
    return snapshot


@dataclass(slots=True)
class ReplayStep:
    as_of: datetime
    snapshot: VenueSnapshot
    observations: dict[str, list[Any]]
    settlements: dict[str, dict[str, Any]]
    label: str = ""


def load_replay(path: Path = REPLAY_FIXTURE) -> list[ReplayStep]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    steps: list[ReplayStep] = []
    for item in payload["steps"]:
        as_of = parse_time(item["as_of"])
        if as_of is None:
            raise ValueError(f"replay step {item.get('label')!r} has no valid as_of")
        observations = parse_fixture_observations(item.get("observations") or {}, month_anchor=as_of)
        steps.append(
            ReplayStep(
                as_of=as_of,
                snapshot=snapshot_from_fixture(item.get("polymarket") or {}),
                observations=observations,
                settlements={str(k): v for k, v in (item.get("settlements") or {}).items()},
                label=str(item.get("label") or ""),
            )
        )
    return steps


def _runtime(snapshot: VenueSnapshot, ledger: PaperLedger | None, limits: RiskLimits, starting_cash: Decimal, model_fees: bool) -> TrackRuntime:
    return TrackRuntime.create(WEATHER_TRACK, {Venue.POLYMARKET: snapshot}, ledger=ledger, risk_limits=limits, starting_cash=starting_cash, model_fees=model_fees)


def _finish(runtime: TrackRuntime, *, label: str) -> TrackSummary:
    runtime.finalize(label=label)
    runtime.summary.metrics["venue_pnl"] = _venue_pnl(runtime.ledger)
    runtime.summary.metrics["snapshot"] = {
        venue.value: {"source": snap.source, "markets": len(snap.markets), "groups": len(snap.groups), "errors": snap.errors}
        for venue, snap in runtime.snapshots.items()
    }
    return runtime.summary


def _merge(into: TrackSummary, step: TrackSummary) -> None:
    into.candidates += step.candidates
    into.admitted += step.admitted
    into.proposed_orders += step.proposed_orders
    into.paper_fills += step.paper_fills
    into.estimated_fees_buffer += step.estimated_fees_buffer
    for reason, count in step.refused_by_reason.items():
        into.refused_by_reason[reason] = into.refused_by_reason.get(reason, 0) + count
    into.fills.extend(step.fills)
    into.edges.extend(step.edges)
    into.admitted_edges.extend(step.admitted_edges)


async def replay_fixture(
    steps: list[ReplayStep] | None = None,
    *,
    ledger: PaperLedger | None = None,
    register: DeadBucketRegister | None = None,
    parameters: DeadBucketParameters | None = None,
    risk_limits: RiskLimits | None = None,
    starting_cash: Decimal = DEFAULT_STARTING_CASH,
    model_fees: bool = True,
) -> tuple[TrackSummary, PaperLedger, DeadBucketRegister, list[TrackSummary]]:
    """Replay every fixture step through the live code path; returns the aggregate."""
    steps = steps or load_replay()
    register = register or DeadBucketRegister()
    limits = risk_limits or WEATHER_RISK_LIMITS
    step_summaries: list[TrackSummary] = []
    runtime: TrackRuntime | None = None
    for step in steps:
        runtime = _runtime(step.snapshot, ledger, limits, starting_cash, model_fees)
        ledger = runtime.ledger
        source = StaticObservationSource(step.observations, source="fixture", note=f"fixture step {step.label}")

        async def lookup(record: DeadBucketRecord, _settlements: dict[str, dict[str, Any]] = step.settlements) -> dict[str, Any] | None:
            return _settlements.get(record.record_id) or _settlements.get(record.lookup)

        summary = await run_weather_dead_bucket_cycle(
            runtime, register, observations=source, parameters=parameters, as_of=step.as_of, settlement_lookup=lookup,
        )
        summary.metrics["step"] = step.label
        step_summaries.append(_finish(runtime, label=f"fixtures:{step.label or step.as_of.isoformat()}"))
    assert runtime is not None and ledger is not None
    aggregate = TrackSummary(WEATHER_TRACK, label=WEATHER_TRACK_LABEL)
    for step_summary in step_summaries:
        _merge(aggregate, step_summary)
    last = step_summaries[-1]
    running_highs: dict[str, Any] = {}
    stations: dict[str, int] = {}
    for step_summary in step_summaries:
        running_highs.update(step_summary.metrics.get("running_highs", {}))
        for station, count in step_summary.metrics.get("observation_source", {}).get("stations", {}).items():
            stations[station] = max(stations.get(station, 0), int(count))
    aggregate.metrics = {
        **last.metrics,
        "steps": [s.metrics.get("step") for s in step_summaries],
        "replayed_steps": len(step_summaries),
        "running_highs": running_highs,
        "observation_source": {**last.metrics.get("observation_source", {}), "stations": dict(sorted(stations.items()))},
        "weather_events": [{"step": s.metrics.get("step"), **row} for s in step_summaries for row in s.metrics.get("weather_events", [])],
        "measurements": [{"step": s.metrics.get("step"), **row} for s in step_summaries for row in s.metrics.get("measurements", [])],
        "resting_only_opportunities": (resting_rows := [{"step": s.metrics.get("step"), **row} for s in step_summaries for row in s.metrics.get("resting_only_opportunities", [])]),
        "resting_only_best_bid": _best_bid_summary(resting_rows),
        "cities": sorted({c for s in step_summaries for c in s.metrics.get("cities", [])}),
        "stations": len({k.split(":")[0] for s in step_summaries for k in s.metrics.get("running_highs", {})} | set(stations)),
        "stations_parsed": len({k.split(":")[0] for s in step_summaries for k in s.metrics.get("running_highs", {})}),
        "dead_bucket": {
            key: sum(int(s.metrics.get("dead_bucket", {}).get(key, 0)) for s in step_summaries)
            for key in ("candidates", "kills", "certain_yes", "kills_without_taker_ask", "positions_opened")
        },
        **{key: sum(int(s.metrics.get(key, 0)) for s in step_summaries) for key in ("positions_opened", "positions_settled_this_run", "settlement_fills", "events_total", "events_parsed", "dead_buckets_without_taker_ask", "markets", "buckets")},
    }
    aggregate.notes = last.notes
    aggregate.ledger = last.ledger
    aggregate.settlement_risk_flag = False
    return aggregate, ledger, register, step_summaries


async def measure_weather_dead_bucket(
    *,
    use_fixtures: bool = True,
    limit: int = 60,
    observation_source: ObservationSource | None = None,
    parameters: DeadBucketParameters | None = None,
    ledger: PaperLedger | None = None,
    register: DeadBucketRegister | None = None,
    risk_limits: RiskLimits | None = None,
    starting_cash: Decimal = DEFAULT_STARTING_CASH,
    model_fees: bool = True,
    as_of: datetime | None = None,
    snapshot: VenueSnapshot | None = None,
    settlement_lookup: SettlementLookup | None = None,
    tag_id: int = POLYMARKET_HIGHEST_TEMPERATURE_TAG,
) -> tuple[TrackSummary, PaperLedger, DeadBucketRegister]:
    """Fixtures: replay the committed steps. Network: one cycle against public data."""
    if use_fixtures:
        summary, ledger, register, _ = await replay_fixture(
            ledger=ledger, register=register, parameters=parameters, risk_limits=risk_limits, starting_cash=starting_cash, model_fees=model_fees,
        )
        return summary, ledger, register
    as_of = as_of or _now()
    register = register or DeadBucketRegister()
    source = observation_source or NullObservationSource()
    snapshot = snapshot or await capture_weather_snapshot(limit=limit, tag_id=tag_id)
    runtime = _runtime(snapshot, ledger, risk_limits or WEATHER_RISK_LIMITS, starting_cash, model_fees)
    lookup = settlement_lookup if settlement_lookup is not None else network_settlement_lookup
    summary = await run_weather_dead_bucket_cycle(runtime, register, observations=source, parameters=parameters, as_of=as_of, settlement_lookup=lookup)
    _finish(runtime, label=f"network:{as_of.isoformat()}")
    return summary, runtime.ledger, register


# --------------------------------------------------------------------------
# Contract hooks (research/weather_tracks.py): runner + report builder
# --------------------------------------------------------------------------
async def run_weather_tracks(
    snapshots: dict[Venue, VenueSnapshot],
    *,
    ledgers: dict[str, PaperLedger] | None,
    starting_cash: Decimal = DEFAULT_STARTING_CASH,
    model_fees: bool = True,
    use_fixtures: bool = True,
    cycle_label: str = "",
    register: DeadBucketRegister | None = None,
    observation_source: ObservationSource | None = None,
    parameters: DeadBucketParameters | None = None,
    limit: int = 60,
) -> tuple[list[TrackSummary], dict[str, PaperLedger]]:
    """``WeatherRunner``-shaped entry point for this track (``research/weather_tracks.py``).

    The shared ``snapshots`` never contain the weather events (they come from
    Gamma tag 104596, not the market listing), so fixture runs replay the
    committed steps and network runs capture their own weather snapshot. The
    register is returned inside ``summary.metrics["register_state"]`` so a
    caller that persists ledgers can persist it too; without one, positions
    opened here are not carried across runs.
    """
    del snapshots  # the weather universe is captured separately (see docstring)
    ledger = (ledgers or {}).get(WEATHER_TRACK)
    if use_fixtures:
        summary, ledger, register, _ = await replay_fixture(ledger=ledger, register=register, parameters=parameters, starting_cash=starting_cash, model_fees=model_fees)
    else:
        from research.weather_obs import build_observation_source

        summary, ledger, register = await measure_weather_dead_bucket(
            use_fixtures=False, limit=limit, observation_source=observation_source or build_observation_source(use_fixtures=False),
            parameters=parameters, ledger=ledger, register=register, starting_cash=starting_cash, model_fees=model_fees,
        )
    summary.metrics["register_state"] = register.to_dict()
    summary.metrics["cycle_label"] = cycle_label
    return [summary], {WEATHER_TRACK: ledger}


def build_weather_report(summaries: list[TrackSummary], *, mode: str, measured_at: str, run_id: str | None = None) -> dict[str, Any]:
    """``weather_report_<mode>.json`` (kind ``weather_report``): one block per weather track.

    Sister tracks add their own block under ``tracks[<id>]``; the dashboard sync
    only needs ``kind``, ``paper_only`` and ``meta.source``. This track's block
    carries the pre-registration, both verdicts, running highs, per-leg reasons,
    resting-only legs and every register record.
    """
    blocks: dict[str, Any] = {}
    status = "not_run"
    for summary in summaries:
        if summary.track == WEATHER_TRACK:
            blocks[WEATHER_TRACK] = dead_bucket_report_block(summary)
            status = str(summary.metrics.get("status") or status)
        elif summary.track in RESERVED_WEATHER_TRACKS or summary.track.startswith("weather_"):
            blocks[summary.track] = {"status": summary.metrics.get("status"), "metrics_keys": sorted(summary.metrics), "note": "no report builder for this track in this module"}
    weather = [s for s in summaries if s.track in blocks]
    return {
        "schema_version": "1.0.0",
        "kind": WEATHER_REPORT_KIND,
        "paper_only": True,
        "status": status,
        "meta": {
            "source": "measured",
            "paper_only": True,
            "mode": mode,
            "measured_at": measured_at,
            "generated_at": _now().isoformat(),
            "run_id": run_id,
            "status": status,
            "track_family": WEATHER_FAMILY,
            "tracks": list(blocks),
            "pnl_source": "core.ledger.PaperLedger",
        },
        "totals": {
            "tracks": len(blocks),
            "candidates": sum(s.candidates for s in weather),
            "admitted": sum(s.admitted for s in weather),
            "paper_fills": sum(s.paper_fills for s in weather),
            "positions_opened": sum(int(s.metrics.get("positions_opened", 0)) for s in weather),
            "positions_settled_this_run": sum(int(s.metrics.get("positions_settled_this_run", 0)) for s in weather),
            "dead_bucket_kills": sum(int(s.metrics.get("dead_bucket", {}).get("kills", 0)) for s in weather),
            "kills_without_taker_ask": sum(int(s.metrics.get("dead_bucket", {}).get("kills_without_taker_ask", 0)) for s in weather),
            "evaluation_status": next((s.metrics.get("evaluation", {}).get("status") for s in weather if s.metrics.get("evaluation")), "not_run"),
        },
        "tracks": blocks,
    }


def _compact_measurement(row: dict[str, Any]) -> dict[str, Any]:
    """One line per bucket leg: the reason plus the numbers that justify it."""
    bucket_verdict = row.get("verdict") or {}
    evaluation = row.get("evaluation") or {}
    out = {k: row.get(k) for k in ("step", "event_id", "market_id", "bucket", "station", "local_date", "reason", "detail", "paper_fills") if k in row}
    if bucket_verdict:
        out["verdict"] = bucket_verdict.get("status")
        out["rule"] = bucket_verdict.get("rule")
        out["buy_outcome"] = bucket_verdict.get("buy_outcome")
    for key in ("ask", "ask_size", "bid", "net_edge", "quantity"):
        if evaluation.get(key) is not None:
            out[key] = evaluation[key]
    return out


def _compact_event(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in ("step", "event_id", "title", "legs", "reason", "detail", "observation_status", "observation_detail") if k in row}
    spec = row.get("spec") or {}
    out["spec"] = {k: spec.get(k) for k in ("station_icao", "city", "local_date", "unit", "timezone", "resolution_source")} if spec else None
    high = row.get("running_high") or {}
    out["running_high"] = {k: high.get(k) for k in ("running_high_low", "running_high_high", "running_high_all_reports_high", "latest_temp_high", "latest_observed_at_local", "trend", "day_complete", "hourly_observations")} if high else None
    return out


def dead_bucket_report_block(summary: TrackSummary) -> dict[str, Any]:
    """This track's section of the weather report (also usable standalone)."""
    m = summary.metrics
    return {
        "track": summary.track,
        "label": summary.label,
        "status": m.get("status"),
        "network_status": m.get("network_status"),
        "experiment": {
            "hypothesis": (
                "Once the settlement station's running daily high is in (and, late in the day, the temperature is "
                "falling), Polymarket temperature buckets that can no longer contain the daily high still quote a few "
                "cents of probability; buying NO on those dead buckets (and YES on the one certain bucket) earns >= 2c "
                "net per contract after the taker fee on settled days."
            ),
            "pre_registered": m.get("verdict", {}).get("pre_registered"),
            "kill_rules": {
                "dead_below_running_high": "bucket.hi < hourly running high (low rounding candidate); any hour",
                "dead_above_late_day": ">= late_day_local_hour local, latest hourly temp <= high - falling_margin, last N hourly obs not rising, bucket.lo > all-report high + headroom",
                "certain_yes_late_day": "same late-day gate and the bucket covers [high, all-report high + headroom]",
                "dead_day_complete / certain_yes_day_complete": "an hourly observation from the following local date exists; the bucket contains neither / both rounding candidates of the final high",
                "refusals": "bucket_edge_ambiguous when the final or running high rounds across a bucket edge; too_early_in_day / not_falling / live otherwise",
            },
            "observation_sources": FREE_SOURCES,
        },
        "parameters": m.get("parameters"),
        "observation_source": m.get("observation_source"),
        "snapshot": m.get("snapshot"),
        "events_total": m.get("events_total"),
        "events_parsed": m.get("events_parsed"),
        "cities": m.get("cities"),
        "stations": m.get("stations"),
        "stations_parsed": m.get("stations_parsed"),
        "candidates": summary.candidates,
        "admitted": summary.admitted,
        "proposed_orders": summary.proposed_orders,
        "paper_fills": summary.paper_fills,
        "positions_opened": m.get("positions_opened"),
        "positions_settled_this_run": m.get("positions_settled_this_run"),
        "dead_bucket": m.get("dead_bucket"),
        "dead_buckets_without_taker_ask": m.get("dead_buckets_without_taker_ask"),
        "resting_only_best_bid": m.get("resting_only_best_bid"),
        "resting_only_opportunities": m.get("resting_only_opportunities"),
        "refused_by_reason": dict(sorted(summary.refused_by_reason.items())),
        "register": m.get("register"),
        "evaluation": m.get("evaluation"),
        "verdict": m.get("verdict"),
        "verdict_certain_yes": m.get("verdict_certain_yes"),
        "running_highs": m.get("running_highs"),
        "events": [_compact_event(row) for row in (m.get("weather_events") or [])],
        "measurements": [_compact_measurement(row) for row in (m.get("measurements") or [])],
        "records": m.get("records"),
        "fills": summary.fills,
        "ledger": summary.ledger,
        "not_validated": [
            "the NOAA WRH time-series table rounds tenths-of-a-degree observations to whole degrees with an undocumented "
            "tie rule; exact .5 values are refused (bucket_edge_ambiguous), not resolved",
            "the table is built from the same ASOS/METAR observations aviationweather.gov serves, but late corrections "
            "(revisions until the next day's first data point) and hourly-only filtering can make it differ; the "
            "register records venue-vs-observation agreement per settled position so that basis risk is measured",
            "late-day 'no further rise' is a heuristic (17:00 local, 2°F/1°C below the high, two non-rising hourly obs, "
            "1° headroom); it can be wrong on days with evening fronts or foehn events - that is exactly the kill rule",
            "Weather Underground fallback resolutions and non-airport sources (Hong Kong Observatory) are refused, not modelled",
            "SPECI observations warmer than the hourly table are used only to widen the upper kill, never to kill lower buckets",
            "fixture sequences and settlements are synthetic (one staged disagreement) and carry no evidence about the hypothesis",
            "network sample: the committed snapshot is one cycle; the pre-registered verdict needs >= 30 settled positions over >= 10 station-days",
        ],
    }


__all__ = [
    "DEAD_BUCKET_TRACKS",
    "DeadBucketRecord",
    "DeadBucketRegister",
    "POLYMARKET_DAILY_TEMPERATURE_TAG",
    "POLYMARKET_HIGHEST_TEMPERATURE_TAG",
    "REPLAY_FIXTURE",
    "REPORT_FILE",
    "ReplayStep",
    "EVALUATION_STATUSES",
    "RESERVED_WEATHER_TRACKS",
    "WEATHER_FAMILY",
    "WEATHER_REPORT_KIND",
    "WEATHER_SCOREBOARD_NAME",
    "WEATHER_TRACK",
    "WEATHER_TRACK_LABEL",
    "WEATHER_TRACK_LABELS",
    "WeatherEvent",
    "build_weather_report",
    "capture_weather_snapshot",
    "classify_venue_resolution",
    "dead_bucket_report_block",
    "evaluation_status",
    "load_replay",
    "measure_weather_dead_bucket",
    "network_settlement_lookup",
    "obs_implied_outcome",
    "register_path",
    "replay_fixture",
    "run_weather_dead_bucket_cycle",
    "run_weather_tracks",
    "snapshot_from_fixture",
    "track_status",
    "weather_events",
]
