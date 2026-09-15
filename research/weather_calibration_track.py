"""``weather_naive_ensemble`` vs ``weather_calibrated_ensemble``: a paper A/B on Polymarket daily-temperature buckets.

Two isolated paper tracks (own :class:`core.ledger.PaperLedger`, risk manager
and execution engine each) price the same events from the same free forecasts:

* **naive** (control): equal-weight mean of the raw per-model day-ahead maxima,
  pooled sigma, bucket probabilities from a normal;
* **calibrated**: per-city bias removal and inverse-variance x hit-rate
  weights from the :class:`~strategies.weather_calibration.CalibrationStore`,
  per-city residual sigma; refuses cities with too little settled history.

Per cycle:

1. settle: every pending city-day is looked up on Gamma; a closed event with
   exactly one YES leg settles both lanes' positions on its legs at 1 / 0,
   records the net EV per admission and adds the city-day (the forecasts that
   were recorded when the event was first priced + the settled bucket) to the
   calibration store;
2. discover: open events with an admissible lead (target day = city-local
   tomorrow), one YES book per leg, one forecast call per city;
3. price: ladder probabilities per lane; a leg is admitted when
   |p - mid| >= edge_threshold and the fair-value engine finds a positive
   cost-adjusted touch edge; fills land in the lane's ledger and register.

Fixture replays (``research/fixtures/weather_calibration_replay.json``) run the
same code path with synthetic forecasts, books and settlements; they prove
arithmetic and carry no evidence about the hypothesis.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.ledger import PaperLedger
from core.risk import RiskLimits
from core.types import ONE, ZERO, Market, OrderBook, Outcome, Venue
from research.scoreboard import (
    DEFAULT_RISK_LIMITS,
    DEFAULT_STARTING_CASH,
    TrackRuntime,
    TrackSummary,
    VenueSnapshot,
    _venue_pnl,
)
from research.weather_calibration_sources import (
    FixtureUniverse,
    ForecastSource,
    StaticForecastSource,
    WeatherUniverse,
    load_json,
)
from strategies.weather_calibration import (
    CALIBRATED_TRACK,
    LANES,
    NAIVE_TRACK,
    WEATHER_TRACKS,
    ABVerdict,
    CalibrationSample,
    CalibrationStore,
    EnsembleEstimate,
    SettledRecord,
    WeatherLaneStrategy,
    WeatherParameters,
    ab_verdict,
    calibrated_ensemble,
    ladder_probabilities,
    naive_ensemble,
    walk_forward_skill,
)
from strategies.weather_types import CityRegistry, WeatherEvent

TRACK_FAMILY = "weather_calibration"
WEATHER_LABELS = {
    NAIVE_TRACK: "Weather naive ensemble (control)",
    CALIBRATED_TRACK: "Weather calibrated ensemble (per city)",
}
REGISTER_SCHEMA = "1.0.0"
REPLAY_FIXTURE = Path(__file__).with_name("fixtures") / "weather_calibration_replay.json"
DETAIL_FILE = "weather_calibration_latest.json"
Q4 = Decimal("0.0001")


def _now() -> datetime:
    return datetime.now(UTC)


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4) if value is not None else None


def _bps(value: Decimal | None) -> int | None:
    return int((value * 10000).to_integral_value()) if value is not None else None


def register_path(artifact_dir: Path) -> Path:
    return artifact_dir / TRACK_FAMILY / "register.json"


def store_path(artifact_dir: Path) -> Path:
    return artifact_dir / TRACK_FAMILY / "calibration_store.json"


# --------------------------------------------------------------------------
# Register: city-days priced and paper admissions per lane
# --------------------------------------------------------------------------
@dataclass(slots=True)
class CityDayRecord:
    key: str  # city_key:YYYY-MM-DD
    event_slug: str
    event_id: str
    title: str
    city: str
    city_name: str
    date: str
    unit: str
    precision: str
    forecasts: dict[str, float]  # first admissible-lead snapshot: the calibration sample
    forecast_source: str
    forecast_fetched_at: str
    lead_days: int
    legs: list[str]
    status: str = "pending"  # pending | resolved | ambiguous | missing
    latest_forecasts: dict[str, float] = field(default_factory=dict)
    estimates: dict[str, Any] = field(default_factory=dict)
    ladder: list[dict[str, Any]] = field(default_factory=list)
    resolved_bucket: dict[str, Any] | None = None
    truth_value: float | None = None
    resolved_at: str | None = None
    observations: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AdmissionRecord:
    record_id: str  # lane:market_id
    lane: str
    track: str
    market_id: str
    event_slug: str
    city: str
    date: str
    title: str
    bucket: dict[str, Any]
    p: str
    mid: str
    gap: str
    side: str
    opened_at: str
    quantity: str = "0"
    entry_yes_price: str | None = None
    fees: str = "0"
    fills: int = 0
    status: str = "open"  # open | settled | void
    outcome: str | None = None
    settle_price: str | None = None
    net_ev: str | None = None
    settled_at: str | None = None

    @property
    def signed_quantity(self) -> Decimal:
        qty = Decimal(self.quantity)
        return qty if self.side == "buy" else -qty

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WeatherRegister:
    city_days: dict[str, CityDayRecord] = field(default_factory=dict)
    admissions: dict[str, AdmissionRecord] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: _now().isoformat())
    updated_at: str = field(default_factory=lambda: _now().isoformat())

    def pending_city_days(self) -> list[CityDayRecord]:
        return [r for r in self.city_days.values() if r.status == "pending"]

    def open_admissions(self, lane: str | None = None) -> list[AdmissionRecord]:
        return [a for a in self.admissions.values() if a.status == "open" and (lane is None or a.lane == lane)]

    def settled_admissions(self, lane: str | None = None) -> list[AdmissionRecord]:
        return [a for a in self.admissions.values() if a.status == "settled" and (lane is None or a.lane == lane)]

    def settled_records(self) -> list[SettledRecord]:
        return [
            SettledRecord(lane=a.lane, cluster=f"{a.city}:{a.date}", contracts=Decimal(a.quantity), net_ev=Decimal(a.net_ev or "0"))
            for a in self.settled_admissions()
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REGISTER_SCHEMA,
            "paper_only": True,
            "kind": "weather_calibration_register",
            "tracks": list(WEATHER_TRACKS),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "city_days": [self.city_days[k].as_dict() for k in sorted(self.city_days)],
            "admissions": [self.admissions[k].as_dict() for k in sorted(self.admissions)],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WeatherRegister:
        if payload.get("paper_only") is not True:
            raise ValueError("refusing to load a register that is not marked paper_only")
        register = cls(created_at=payload.get("created_at", _now().isoformat()), updated_at=payload.get("updated_at", _now().isoformat()))
        for item in payload.get("city_days", []):
            record = CityDayRecord(**item)
            register.city_days[record.key] = record
        for item in payload.get("admissions", []):
            record = AdmissionRecord(**item)
            register.admissions[record.record_id] = record
        return register

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = _now().isoformat()
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    @classmethod
    def load_or_create(cls, path: Path) -> WeatherRegister:
        if path.exists():
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls()


# --------------------------------------------------------------------------
# Settlement
# --------------------------------------------------------------------------
def _settle_admission(record: AdmissionRecord, runtime: TrackRuntime, outcome: Outcome, as_of: datetime) -> Decimal:
    settle_price = ONE if outcome is Outcome.YES else ZERO
    runtime.ledger.settle(Venue.POLYMARKET, record.market_id, outcome)
    entry = Decimal(record.entry_yes_price) if record.entry_yes_price is not None else settle_price
    net = record.signed_quantity * (settle_price - entry) - Decimal(record.fees)
    record.status = "settled"
    record.outcome = outcome.value
    record.settle_price = str(settle_price)
    record.net_ev = str(_q(net))
    record.settled_at = as_of.isoformat()
    return net


async def settle_pending(
    register: WeatherRegister,
    store: CalibrationStore,
    runtimes: dict[str, TrackRuntime],
    *,
    universe: WeatherUniverse,
    as_of: datetime,
    max_lookups: int = 400,
) -> dict[str, Any]:
    counts = {"checked": 0, "resolved": 0, "still_pending": 0, "ambiguous": 0, "missing": 0, "lookup_errors": 0, "admissions_settled": 0, "samples_added": 0}
    settlements: list[dict[str, Any]] = []
    for record in sorted(register.pending_city_days(), key=lambda r: r.date):
        if counts["checked"] >= max_lookups:
            break
        if date.fromisoformat(record.date) >= as_of.date():
            counts["still_pending"] += 1
            continue  # the observation day has not ended (UTC) yet; no lookup spent
        counts["checked"] += 1
        try:
            event = await universe.event_by_slug(record.event_slug)
        except Exception as exc:  # a dead endpoint leaves the day pending
            counts["lookup_errors"] += 1
            settlements.append({"city_day": record.key, "status": "lookup_error", "detail": f"{type(exc).__name__}: {exc}"})
            continue
        if event is None:
            record.status = "missing"
            counts["missing"] += 1
            settlements.append({"city_day": record.key, "status": "missing"})
            continue
        if not event.closed:
            counts["still_pending"] += 1
            continue
        winner = event.resolved_leg
        if winner is None:
            record.status = "ambiguous"
            counts["ambiguous"] += 1
            settlements.append({"city_day": record.key, "status": "ambiguous", "detail": "closed without exactly one YES leg"})
            continue
        truth = event.truth_value()
        record.status = "resolved"
        record.resolved_bucket = winner.bucket.as_dict()
        record.truth_value = truth
        record.resolved_at = as_of.isoformat()
        counts["resolved"] += 1
        lane_results: dict[str, Any] = {}
        for leg in event.legs:
            outcome = Outcome.YES if leg.resolved else Outcome.NO
            for lane, runtime in runtimes.items():
                admission = register.admissions.get(f"{lane}:{leg.market_id}")
                if admission is None or admission.status != "open":
                    continue
                net = _settle_admission(admission, runtime, outcome, as_of)
                counts["admissions_settled"] += 1
                lane_results.setdefault(lane, []).append({"market_id": leg.market_id, "bucket": leg.bucket.label, "outcome": outcome.value, "net_ev": str(_q(net))})
        if truth is not None and record.forecasts:
            added = store.add_sample(
                CalibrationSample(
                    city=record.city,
                    date=record.date,
                    unit=record.unit,  # type: ignore[arg-type]
                    precision=record.precision,  # type: ignore[arg-type]
                    truth_lo=winner.bucket.lo,
                    truth_hi=winner.bucket.hi,
                    truth_value=truth,
                    forecasts=dict(record.forecasts),
                    forecast_source="open_meteo_forecast_live" if record.forecast_source != "fixture" else "fixture_live",
                    truth_source="polymarket_resolution" if record.forecast_source != "fixture" else "fixture",
                    event_slug=record.event_slug,
                )
            )
            counts["samples_added"] += int(added)
        settlements.append({"city_day": record.key, "status": "resolved", "bucket": winner.bucket.label, "truth_value": truth, "lanes": lane_results})
    return {"counts": counts, "settlements": settlements}


# --------------------------------------------------------------------------
# One pricing cycle
# --------------------------------------------------------------------------
def _edge_row(track: str, event: WeatherEvent, market: Market, evaluation: Any, *, filled: bool) -> dict[str, Any]:
    return {
        "track": track,
        "venue": Venue.POLYMARKET.value,
        "market": market.market_id,
        "title": market.title,
        "city": event.city.key if event.city else event.city_name,
        "date": event.target_date.isoformat(),
        "edge_bps": _bps(evaluation.cost_adjusted_edge),
        "gap_bps": _bps(evaluation.gap),
        "admitted": True,
        "filled": filled,
        "reason": "admitted",
        "fair_value": evaluation.probability,
        "mid": evaluation.mid,
        "side": evaluation.side,
    }


async def run_weather_cycle(
    runtimes: dict[str, TrackRuntime],
    register: WeatherRegister,
    store: CalibrationStore,
    *,
    events: list[WeatherEvent],
    universe: WeatherUniverse,
    forecasts: ForecastSource,
    parameters: WeatherParameters | None = None,
    as_of: datetime | None = None,
    mode: str = "network",
    settle: bool = True,
) -> dict[str, TrackSummary]:
    params = parameters or WeatherParameters()
    as_of = as_of or _now()
    summaries = {lane: rt.summary for lane, rt in runtimes.items()}
    for lane, summary in summaries.items():
        summary.label = WEATHER_LABELS[LANES[lane]]
    strategies = {
        lane: WeatherLaneStrategy(lane, parameters=params, portfolio=rt.ledger.portfolio, risk=rt.risk) for lane, rt in runtimes.items()
    }

    # 1. Settlement of city-days priced on earlier runs (also feeds the store).
    settlement = await settle_pending(register, store, runtimes, universe=universe, as_of=as_of) if settle else {"counts": {}, "settlements": []}

    # 2./3. Price this run's events.
    rows: list[dict[str, Any]] = []
    event_reasons: dict[str, int] = {}
    forecast_cache: dict[str, dict[str, float]] = {}
    naive_sigma_cache: dict[str, tuple[float, str]] = {}
    admitted_by_lane = {lane: 0 for lane in runtimes}
    cities_priced: set[str] = set()

    def refuse_event(reason: str, legs: int) -> None:
        event_reasons[reason] = event_reasons.get(reason, 0) + 1
        for summary in summaries.values():
            summary.refuse(reason)
            summary.candidates += legs

    for event in events:
        n_legs = len(event.legs)
        row: dict[str, Any] = {**event.as_dict(), "as_of": as_of.isoformat()}
        rows.append(row)
        city = event.city
        if city is None:
            row["reason"] = "city_unknown"
            refuse_event("city_unknown", n_legs)
            continue
        if not event.contiguous:
            row["reason"] = "ladder_not_contiguous"
            refuse_event("ladder_not_contiguous", n_legs)
            continue
        lead = event.lead_days(as_of)
        row["lead_days"] = lead
        if lead not in params.admissible_lead_days:
            row["reason"] = "lead_not_admissible"
            refuse_event("lead_not_admissible", n_legs)
            continue
        cache_key = f"{city.key}:{event.target_date.isoformat()}"
        if cache_key not in forecast_cache:
            try:
                forecast_cache[cache_key] = await forecasts.day_max(city, event.target_date)
            except Exception as exc:  # one dead forecast call must not kill the run
                forecasts.errors.append(f"{cache_key}: {type(exc).__name__}: {exc}")
                forecast_cache[cache_key] = {}
        per_model = {m: v for m, v in forecast_cache[cache_key].items() if m in params.models}
        row["forecasts"] = per_model
        if len(per_model) < params.min_models_per_sample:
            row["reason"] = "forecast_unavailable"
            refuse_event("forecast_unavailable", n_legs)
            continue
        cities_priced.add(city.key)

        # Register the city-day (first sight keeps its forecasts as the calibration sample).
        record = register.city_days.get(cache_key)
        if record is not None and record.status != "pending":
            row["reason"] = "city_day_already_" + record.status
            refuse_event(row["reason"], n_legs)
            continue
        if record is None:
            record = CityDayRecord(
                key=cache_key,
                event_slug=event.slug,
                event_id=event.event_id,
                title=event.title,
                city=city.key,
                city_name=event.city_name,
                date=event.target_date.isoformat(),
                unit=event.unit,
                precision=event.precision,
                forecasts=dict(per_model),
                forecast_source=forecasts.name,
                forecast_fetched_at=as_of.isoformat(),
                lead_days=lead,
                legs=[leg.market_id for leg in event.legs],
            )
            register.city_days[cache_key] = record
        else:
            record.observations += 1
        record.latest_forecasts = dict(per_model)

        # Ensembles (statistics strictly before the target day; the store never sees today's truth).
        if event.unit not in naive_sigma_cache:
            naive_sigma_cache[event.unit] = store.pooled_naive_sigma(unit=event.unit, parameters=params, before=event.target_date)
        sigma_naive, sigma_source = naive_sigma_cache[event.unit]
        naive = naive_ensemble(per_model, sigma=sigma_naive, sigma_source=sigma_source)
        calibration = store.city_calibration(city.key, unit=event.unit, precision=event.precision, parameters=params, before=event.target_date)
        calibrated: EnsembleEstimate | None = calibrated_ensemble(calibration, per_model) if calibration.models else None
        p_naive = ladder_probabilities(naive.mean, naive.sigma, event.buckets, event.precision)
        p_cal = ladder_probabilities(calibrated.mean, calibrated.sigma, event.buckets, event.precision) if calibrated else {}
        row["naive"] = naive.as_dict()
        row["calibrated"] = calibrated.as_dict() if calibrated else {"adequate": False, "n_calibration_days": calibration.n_days}
        row["calibration_n_days"] = calibration.n_days
        record.estimates = {"naive": row["naive"], "calibrated": row["calibrated"]}

        snapshot = next(iter(runtimes.values())).snapshots[Venue.POLYMARKET]
        ladder_rows: list[dict[str, Any]] = []
        for leg in event.legs:
            book = snapshot.book(leg.market)
            leg_row: dict[str, Any] = {
                "market_id": leg.market_id,
                "bucket": leg.bucket.label,
                "mid": _q(book.mid_price),
                "p_naive": round(p_naive[leg.bucket.label], 4),
                "p_calibrated": round(p_cal[leg.bucket.label], 4) if p_cal else None,
                "lanes": {},
            }
            ladder_rows.append(leg_row)
            for lane, runtime in runtimes.items():
                summary = summaries[lane]
                summary.candidates += 1
                if lane == "calibrated" and (calibrated is None or not calibration.adequate):
                    summary.refuse("calibration_underpowered")
                    leg_row["lanes"][lane] = {"reason": "calibration_underpowered", "n_calibration_days": calibration.n_days}
                    continue
                record_id = f"{lane}:{leg.market_id}"
                admission = register.admissions.get(record_id)
                if admission is not None and admission.status != "open":
                    summary.refuse("admission_already_" + admission.status)
                    leg_row["lanes"][lane] = {"reason": "admission_already_" + admission.status}
                    continue
                probability = p_cal[leg.bucket.label] if lane == "calibrated" else p_naive[leg.bucket.label]
                estimate = calibrated if lane == "calibrated" else naive
                assert estimate is not None
                evaluation = strategies[lane].evaluate(
                    leg, book, probability, estimate=estimate,
                    context={"city": city.key, "target_date": event.target_date.isoformat(), "bucket": leg.bucket.label, "event_slug": event.slug},
                )
                leg_row["lanes"][lane] = evaluation.as_dict()
                if not evaluation.traded:
                    summary.refuse(evaluation.reason)
                    continue
                summary.admitted += 1
                admitted_by_lane[lane] += 1
                summary.proposed_orders += len(evaluation.orders)
                summary.admitted_edges.append(evaluation.cost_adjusted_edge or ZERO)
                fills_before = summary.paper_fills
                if admission is None:
                    admission = AdmissionRecord(
                        record_id=record_id,
                        lane=lane,
                        track=LANES[lane],
                        market_id=leg.market_id,
                        event_slug=event.slug,
                        city=city.key,
                        date=event.target_date.isoformat(),
                        title=leg.market.title,
                        bucket=leg.bucket.as_dict(),
                        p=str(_q(evaluation.probability)),
                        mid=str(_q(evaluation.mid)),
                        gap=str(_q(evaluation.gap)),
                        side=evaluation.side or "",
                        opened_at=as_of.isoformat(),
                    )
                for order in evaluation.orders:
                    report = await runtime.submit(order, edge=evaluation.cost_adjusted_edge)
                    if report is None or not report.fills:
                        continue
                    qty = sum((f.quantity for f in report.fills), ZERO)
                    fees = sum((f.fee for f in report.fills), ZERO)
                    notional = sum((f.quantity * f.yes_equivalent_price for f in report.fills), ZERO)
                    prev_qty = Decimal(admission.quantity)
                    prev_notional = prev_qty * Decimal(admission.entry_yes_price) if admission.entry_yes_price else ZERO
                    total_qty = prev_qty + qty
                    admission.quantity = str(total_qty)
                    admission.entry_yes_price = str(_q((prev_notional + notional) / total_qty)) if total_qty > ZERO else None
                    admission.fees = str(Decimal(admission.fees) + fees)
                    admission.fills += len(report.fills)
                filled = summary.paper_fills > fills_before
                if admission.fills > 0:
                    register.admissions[record_id] = admission
                leg_row["lanes"][lane]["paper_fills"] = summary.paper_fills - fills_before
                summary.edges.append(_edge_row(LANES[lane], event, leg.market, evaluation, filled=filled))
        row["ladder"] = ladder_rows
        record.ladder = [{k: v for k, v in r.items() if k != "lanes"} for r in ladder_rows]
        row["reason"] = "priced"
        event_reasons["priced"] = event_reasons.get("priced", 0) + 1

    # Metrics shared by both lanes.
    verdict = ab_verdict(register.settled_records(), parameters=params)
    calibration_summary = store.summary(parameters=params)
    per_city = calibration_summary.pop("per_city")
    calibration_summary["walk_forward_skill"] = walk_forward_skill(store, parameters=params)
    register_counts = {
        "city_days": len(register.city_days),
        "city_days_pending": len(register.pending_city_days()),
        "city_days_resolved": sum(1 for r in register.city_days.values() if r.status == "resolved"),
        "city_days_ambiguous": sum(1 for r in register.city_days.values() if r.status == "ambiguous"),
        "city_days_missing": sum(1 for r in register.city_days.values() if r.status == "missing"),
        "admissions_open": {lane: len(register.open_admissions(lane)) for lane in runtimes},
        "admissions_settled": {lane: len(register.settled_admissions(lane)) for lane in runtimes},
    }
    status = track_status(mode, events, verdict, calibration_summary)
    for lane, summary in summaries.items():
        summary.metrics.update(
            {
                "status": status,
                "lane": lane,
                "as_of": as_of.isoformat(),
                "mode": mode,
                "detail_file": DETAIL_FILE,
                "parameters": params.as_dict(),
                "forecast_source": forecasts.as_dict(),
                "universe": universe.as_dict(),
                "event_universe": {"listed": len(events), "by_reason": dict(sorted(event_reasons.items())), "cities_priced": len(cities_priced)},
                "admitted_this_run": admitted_by_lane[lane],
                "settlement": settlement["counts"],
                "settlements_detail": settlement["settlements"],
                "register": register_counts,
                "calibration": calibration_summary,
                "calibration_detail": per_city,
                "verdict": verdict.as_dict(),
                "measurements": rows,
                "network_status": network_status(mode, verdict, calibration_summary),
            }
        )
        summary.settlement_risk_flag = False
    summaries["naive"].notes = (
        "CONTROL. Equal-weight mean of the raw free per-model day-ahead maxima (Open-Meteo: GFS, ECMWF IFS, ICON, "
        "GEM, Meteo-France, UKMO, JMA), pooled sigma, normal bucket probabilities. Trades a bucket only when |p - mid| "
        ">= edge_threshold and the touch clears fees. Same books, sizes and rails as the calibrated lane."
    )
    summaries["calibrated"].notes = (
        "Per-city x model calibration from settled Polymarket city-days (free Open-Meteo previous-runs archive + live "
        "forecasts recorded at first sight): bias removal, inverse-variance x hit-rate weights shrunk toward equal, "
        "per-city residual sigma. Refuses cities below min_calibration_samples settled days. Pre-registered A/B on "
        "settled net EV per contract vs the naive control: PASS only with >= min_settled_per_lane settled admissions in "
        "both lanes, margin >= 0.02 and absolute EV >= 0.025; otherwise FAIL / UNDERPOWERED."
    )
    return summaries


def track_status(mode: str, events: list[WeatherEvent], verdict: ABVerdict, calibration: dict[str, Any]) -> str:
    if mode == "fixtures":
        return "fixture_synthetic"
    if not events and calibration.get("samples", 0) == 0:
        return "no_weather_events"
    return f"measured_{verdict.status.lower()}"


def network_status(mode: str, verdict: ABVerdict, calibration: dict[str, Any]) -> str:
    if mode == "fixtures":
        return "fixture_synthetic: arithmetic only, no evidence about the hypothesis"
    if verdict.status == "PASS":
        return f"PASS on settled paper admissions ({verdict.sample_callout}); calibration {calibration.get('samples')} city-days / {calibration.get('cities_adequate')} adequate cities"
    if verdict.status == "FAIL":
        return f"FAIL on settled paper admissions ({verdict.sample_callout})"
    return (
        f"UNDERPOWERED: {verdict.sample_callout}. Calibration store holds {calibration.get('samples')} settled city-days "
        f"({calibration.get('cities_adequate')} of {calibration.get('cities')} cities adequate); the paper A/B needs the loop to "
        "run daily until both lanes clear the floor."
    )


# --------------------------------------------------------------------------
# History backfill: Polymarket settled buckets x Open-Meteo previous-runs day-1 forecasts
# --------------------------------------------------------------------------
async def backfill_calibration(
    store: CalibrationStore,
    *,
    universe: WeatherUniverse,
    forecasts: ForecastSource,
    parameters: WeatherParameters,
    start: date,
    end: date,
    event_limit: int = 5000,
    forecast_source_name: str = "open_meteo_previous_runs_day1",
) -> dict[str, Any]:
    counts = {"closed_events": 0, "resolved_city_days": 0, "cities": 0, "samples_added": 0, "already_present": 0, "no_forecast": 0, "city_unknown": 0, "errors": 0}
    errors: list[str] = []
    try:
        events = await universe.closed_events(start=start, end=end, limit=event_limit)
    except Exception as exc:
        errors.append(f"closed_events: {type(exc).__name__}: {exc}")
        return {"counts": counts, "errors": errors, "window": [start.isoformat(), end.isoformat()]}
    counts["closed_events"] = len(events)
    truths: dict[str, dict[date, tuple[WeatherEvent, float]]] = {}
    for event in events:
        if event.city is None:
            counts["city_unknown"] += 1
            continue
        truth = event.truth_value()
        if truth is None:
            continue
        truths.setdefault(event.city.key, {})[event.target_date] = (event, truth)
    counts["resolved_city_days"] = sum(len(v) for v in truths.values())
    counts["cities"] = len(truths)
    today = _now().date()
    for city_key, by_day in sorted(truths.items()):
        missing = [d for d in by_day if not store.has(city_key, d)]
        if not missing:
            counts["already_present"] += len(by_day)
            continue
        city = next(iter(by_day.values()))[0].city
        assert city is not None
        past_days = max(1, (today - min(missing)).days + 1)
        try:
            history = await forecasts.previous_day1_history(city, past_days=past_days)
        except Exception as exc:
            counts["errors"] += 1
            errors.append(f"{city_key}: {type(exc).__name__}: {exc}")
            continue
        for day, (event, truth) in by_day.items():
            if store.has(city_key, day):
                counts["already_present"] += 1
                continue
            per_model = {m: v for m, v in history.get(day, {}).items() if m in parameters.models}
            if len(per_model) < parameters.min_models_per_sample:
                counts["no_forecast"] += 1
                continue
            winner = event.resolved_bucket
            assert winner is not None
            added = store.add_sample(
                CalibrationSample(
                    city=city_key,
                    date=day.isoformat(),
                    unit=event.unit,
                    precision=event.precision,
                    truth_lo=winner.lo,
                    truth_hi=winner.hi,
                    truth_value=truth,
                    forecasts=per_model,
                    forecast_source=forecast_source_name,
                    truth_source="polymarket_resolution" if forecast_source_name != "fixture" else "fixture",
                    event_slug=event.slug,
                )
            )
            counts["samples_added"] += int(added)
    return {"counts": counts, "errors": errors, "window": [start.isoformat(), end.isoformat()]}


# --------------------------------------------------------------------------
# Runtimes, snapshots, fixture replay, entry point
# --------------------------------------------------------------------------
def snapshot_from_events(events: list[WeatherEvent], books: dict[str, OrderBook], *, source: str) -> VenueSnapshot:
    markets = [leg.market for event in events for leg in event.legs]
    return VenueSnapshot(
        venue=Venue.POLYMARKET,
        source=source,
        markets=markets,
        books={m.market_id: books.get(m.market_id, OrderBook(market_id=m.market_id)) for m in markets},
    )


def create_runtimes(
    snapshot: VenueSnapshot,
    *,
    ledgers: dict[str, PaperLedger] | None,
    risk_limits: RiskLimits,
    starting_cash: Decimal,
    model_fees: bool,
) -> dict[str, TrackRuntime]:
    ledgers = ledgers or {}
    return {
        lane: TrackRuntime.create(
            track, {Venue.POLYMARKET: snapshot}, ledger=ledgers.get(track), risk_limits=risk_limits, starting_cash=starting_cash, model_fees=model_fees
        )
        for lane, track in LANES.items()
    }


def finish(runtimes: dict[str, TrackRuntime], *, label: str) -> dict[str, TrackSummary]:
    for rt in runtimes.values():
        rt.finalize(label=label)
        rt.summary.metrics["venue_pnl"] = _venue_pnl(rt.ledger)
        rt.summary.metrics["snapshot"] = {
            venue.value: {"source": snap.source, "markets": len(snap.markets), "groups": 0, "errors": snap.errors}
            for venue, snap in rt.snapshots.items()
        }
    return {lane: rt.summary for lane, rt in runtimes.items()}


async def capture_weather_snapshot(universe: WeatherUniverse, *, limit: int, source: str) -> tuple[list[WeatherEvent], VenueSnapshot]:
    snapshot = VenueSnapshot(venue=Venue.POLYMARKET, source=source)
    try:
        events = await universe.open_events(limit=limit)
    except Exception as exc:
        snapshot.errors.append(f"open_events: {type(exc).__name__}: {exc}")
        return [], snapshot
    try:
        books = await universe.books(events)
    except Exception as exc:
        snapshot.errors.append(f"books: {type(exc).__name__}: {exc}")
        books = {}
    full = snapshot_from_events(events, books, source=source)
    full.errors.extend(snapshot.errors)
    return events, full


@dataclass(slots=True)
class ReplayStep:
    label: str
    as_of: datetime
    open_items: list[dict[str, Any]]
    forecasts: dict[str, dict[str, float]]
    settlements: dict[str, dict[str, Any]]


@dataclass(slots=True)
class Replay:
    history: list[CalibrationSample]
    steps: list[ReplayStep]
    note: str = ""


def load_replay(path: Path = REPLAY_FIXTURE) -> Replay:
    payload = load_json(path)
    history = [
        CalibrationSample(
            city=str(item["city"]),
            date=str(item["date"]),
            unit=item.get("unit", "F"),
            precision=item.get("precision", "whole"),
            truth_lo=item.get("truth_lo"),
            truth_hi=item.get("truth_hi"),
            truth_value=float(item["truth_value"]),
            forecasts={str(k): float(v) for k, v in item["forecasts"].items()},
            forecast_source="fixture",
            truth_source="fixture",
            event_slug=item.get("event_slug"),
        )
        for item in payload.get("history", [])
    ]
    steps = []
    for item in payload["steps"]:
        as_of = datetime.fromisoformat(str(item["as_of"]).replace("Z", "+00:00")).astimezone(UTC)
        steps.append(
            ReplayStep(
                label=str(item.get("label") or as_of.isoformat()),
                as_of=as_of,
                open_items=list(item.get("open_events") or []),
                forecasts={str(k): {str(m): float(v) for m, v in vals.items()} for k, vals in (item.get("forecasts") or {}).items()},
                settlements={str(k): v for k, v in (item.get("settlements") or {}).items()},
            )
        )
    return Replay(history=history, steps=steps, note=str(payload.get("_note") or ""))


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
    replay: Replay | None = None,
    *,
    registry: CityRegistry | None = None,
    ledgers: dict[str, PaperLedger] | None = None,
    register: WeatherRegister | None = None,
    store: CalibrationStore | None = None,
    parameters: WeatherParameters | None = None,
    risk_limits: RiskLimits | None = None,
    starting_cash: Decimal = DEFAULT_STARTING_CASH,
    model_fees: bool = True,
) -> tuple[dict[str, TrackSummary], dict[str, PaperLedger], WeatherRegister, CalibrationStore, list[dict[str, TrackSummary]]]:
    """Every fixture step through the live code path; returns aggregates plus per-step summaries."""
    replay = replay or load_replay()
    registry = registry or CityRegistry.load()
    register = register or WeatherRegister()
    store = store or CalibrationStore()
    for sample in replay.history:
        store.add_sample(sample)
    limits = risk_limits or DEFAULT_RISK_LIMITS
    ledgers = dict(ledgers or {})
    step_summaries: list[dict[str, TrackSummary]] = []
    for step in replay.steps:
        universe = FixtureUniverse(registry=registry, open_items=step.open_items, settlement_items=step.settlements)
        forecasts = StaticForecastSource(step.forecasts)
        events, snapshot = await capture_weather_snapshot(universe, limit=1000, source="fixture")
        runtimes = create_runtimes(snapshot, ledgers=ledgers, risk_limits=limits, starting_cash=starting_cash, model_fees=model_fees)
        summaries = await run_weather_cycle(
            runtimes, register, store, events=events, universe=universe, forecasts=forecasts, parameters=parameters, as_of=step.as_of, mode="fixtures"
        )
        for summary in summaries.values():
            summary.metrics["step"] = step.label
        step_summaries.append(finish(runtimes, label=f"fixtures:{step.label}"))
        ledgers = {rt.name: rt.ledger for rt in runtimes.values()}
    aggregates: dict[str, TrackSummary] = {}
    for lane, track in LANES.items():
        aggregate = TrackSummary(track, label=WEATHER_LABELS[track])
        for summaries in step_summaries:
            _merge(aggregate, summaries[lane])
        last = step_summaries[-1][lane]
        per_step = [s[lane].metrics for s in step_summaries]
        by_reason: dict[str, int] = {}
        for m in per_step:
            for reason, count in m.get("event_universe", {}).get("by_reason", {}).items():
                by_reason[reason] = by_reason.get(reason, 0) + count
        settlement_counts: dict[str, int] = {}
        for m in per_step:
            for key, count in m.get("settlement", {}).items():
                settlement_counts[key] = settlement_counts.get(key, 0) + int(count)
        aggregate.metrics = {
            **last.metrics,
            "steps": [m.get("step") for m in per_step],
            "replayed_steps": len(step_summaries),
            "event_universe": {
                "listed": sum(int(m.get("event_universe", {}).get("listed", 0)) for m in per_step),
                "by_reason": dict(sorted(by_reason.items())),
                "cities_priced": sum(int(m.get("event_universe", {}).get("cities_priced", 0)) for m in per_step),
            },
            "admitted_this_run": sum(int(m.get("admitted_this_run", 0)) for m in per_step),
            "settlement": settlement_counts,
            "settlements_detail": [row for m in per_step for row in m.get("settlements_detail", [])],
            "measurements": [row for m in per_step for row in m.get("measurements", [])],
        }
        aggregate.notes = last.notes
        aggregate.ledger = last.ledger
        aggregates[lane] = aggregate
    return aggregates, ledgers, register, store, step_summaries


async def measure_weather_calibration(
    *,
    use_fixtures: bool = True,
    limit: int = 400,
    backfill_days: int = 0,
    allow_paid_keys: bool = False,
    registry: CityRegistry | None = None,
    parameters: WeatherParameters | None = None,
    ledgers: dict[str, PaperLedger] | None = None,
    register: WeatherRegister | None = None,
    store: CalibrationStore | None = None,
    risk_limits: RiskLimits | None = None,
    starting_cash: Decimal = DEFAULT_STARTING_CASH,
    model_fees: bool = True,
    as_of: datetime | None = None,
    universe: WeatherUniverse | None = None,
    forecasts: ForecastSource | None = None,
) -> tuple[dict[str, TrackSummary], dict[str, PaperLedger], WeatherRegister, CalibrationStore, dict[str, Any]]:
    """Fixtures: replay the committed steps. Network: optional backfill, then one cycle on public data."""
    params = parameters or WeatherParameters()
    registry = registry or CityRegistry.load()
    if use_fixtures:
        summaries, ledgers_out, register, store, _ = await replay_fixture(
            registry=registry, ledgers=ledgers, register=register, store=store, parameters=params,
            risk_limits=risk_limits, starting_cash=starting_cash, model_fees=model_fees,
        )
        return summaries, ledgers_out, register, store, {"backfill": None}

    from research.weather_calibration_sources import OpenMeteoSource, PolymarketWeatherUniverse

    as_of = as_of or _now()
    register = register or WeatherRegister()
    store = store or CalibrationStore()
    owns_universe = universe is None
    owns_forecasts = forecasts is None
    universe = universe or PolymarketWeatherUniverse(registry=registry)
    forecasts = forecasts or OpenMeteoSource(models=params.models, allow_paid_keys=allow_paid_keys)
    extras: dict[str, Any] = {"backfill": None}
    try:
        if backfill_days > 0:
            end = as_of.date() - timedelta(days=1)
            start = end - timedelta(days=backfill_days - 1)
            extras["backfill"] = await backfill_calibration(store, universe=universe, forecasts=forecasts, parameters=params, start=start, end=end)
        events, snapshot = await capture_weather_snapshot(universe, limit=limit, source="network")
        runtimes = create_runtimes(snapshot, ledgers=ledgers, risk_limits=risk_limits or DEFAULT_RISK_LIMITS, starting_cash=starting_cash, model_fees=model_fees)
        summaries = await run_weather_cycle(
            runtimes, register, store, events=events, universe=universe, forecasts=forecasts, parameters=params, as_of=as_of, mode="network"
        )
        finish(runtimes, label=f"network:{as_of.isoformat()}")
    finally:
        if owns_universe:
            await universe.close()  # type: ignore[union-attr]
        if owns_forecasts:
            await forecasts.close()  # type: ignore[union-attr]
    return summaries, {rt.name: rt.ledger for rt in runtimes.values()}, register, store, extras


__all__ = [
    "DETAIL_FILE",
    "REPLAY_FIXTURE",
    "TRACK_FAMILY",
    "WEATHER_LABELS",
    "AdmissionRecord",
    "CityDayRecord",
    "Replay",
    "ReplayStep",
    "WeatherRegister",
    "backfill_calibration",
    "capture_weather_snapshot",
    "create_runtimes",
    "finish",
    "load_replay",
    "measure_weather_calibration",
    "network_status",
    "register_path",
    "replay_fixture",
    "run_weather_cycle",
    "settle_pending",
    "snapshot_from_events",
    "store_path",
    "track_status",
]
