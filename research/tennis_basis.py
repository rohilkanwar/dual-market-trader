"""``tennis_basis``: cross-platform tennis basis measured against free public odds.

One paper track, its own :class:`core.ledger.PaperLedger`, and a persistent
**gap register** that carries open gaps across runs:

1. capture the venue tennis universe (Kalshi ``KXATPMATCH`` / ``KXWTAMATCH``
   match-winner markets, Polymarket tag ``864`` ``moneyline`` markets) and the
   outside lines (:mod:`research.tennis_odds`);
2. for every venue market: mandatory settlement-basis filter -> pair with one
   outside event by player names (fail-closed on ambiguity) -> consensus line
   -> gap = venue mid - consensus;
3. |gap| >= 3c and not yet started: open a record and paper-lean toward the
   sharp side through the fair-value engine; otherwise refuse with a reason;
4. an open record gets one observation per run; at the scheduled start it is
   closed on its last pre-start observation, the paper position is unwound at
   that mid (``order_id = event_start_unwind``) and the closure fraction is
   computed;
5. closed records stay ``settlement_pending`` until the venue reports a normal
   winner (confirmed) or a walkover / cancellation / 50-50 / fair-price
   settlement (excluded). An operator results file can also mark retirements.

Network runs without ``ODDS_API_KEY`` are an honest empty (``no_outside_source``).
Fixture runs replay ``research/fixtures/tennis_basis_replay.json`` step by
step through this same code path, which is what proves the arithmetic.
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
from core.types import ONE, ZERO, Market, OrderBook, Venue
from research.scoreboard import (
    DEFAULT_RISK_LIMITS,
    DEFAULT_STARTING_CASH,
    TrackRuntime,
    TrackSummary,
    VenueSnapshot,
    _venue_pnl,
    capture_snapshot,
)
from research.tennis_odds import (
    FREE_TIER,
    ODDS_API_KEY_ENV,
    OutsideBatch,
    OutsideEvent,
    OutsideLineSource,
    StaticOutsideSource,
    parse_events,
    parse_time,
)
from strategies.tennis_basis import (
    BOOKMAKER_BASIS,
    TRACK,
    BasisParameters,
    TennisBasisLean,
    classify_settlement_basis,
    closure_fraction,
    same_player,
    venue_mid,
    verdict,
)
from venues.fixtures import parse_fixture
from venues.kalshi import KalshiClient
from venues.polymarket import PolymarketClient

TENNIS_TRACK = TRACK
TENNIS_TRACKS: tuple[str, ...] = (TENNIS_TRACK,)
TENNIS_TRACK_LABEL = "Tennis cross-platform basis (free odds)"
KALSHI_TENNIS_SERIES: tuple[str, ...] = ("KXATPMATCH", "KXWTAMATCH")
POLYMARKET_TENNIS_TAG = 864
UNWIND_ORDER_ID = "event_start_unwind"
REGISTER_SCHEMA = "1.0.0"
REPLAY_FIXTURE = Path(__file__).with_name("fixtures") / "tennis_basis_replay.json"
Q4 = Decimal("0.0001")
_BINARY_NAMES = {"yes", "no"}


def _now() -> datetime:
    return datetime.now(UTC)


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4) if value is not None else None


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


# --------------------------------------------------------------------------
# Venue tennis markets
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class VenueMatchMarket:
    market: Market
    yes_player: str
    opponent: str | None
    scheduled_start: datetime | None
    rules_text: str
    series: str | None
    match_key: str  # groups the two Kalshi sides of one match
    lookup: str  # ticker (Kalshi) or slug (Polymarket) for the settlement check

    @property
    def venue(self) -> Venue:
        return self.market.venue


def _split_vs(title: str) -> tuple[str, str] | None:
    for sep in (" vs. ", " vs ", " v "):
        if sep in title:
            a, b = title.split(sep, 1)
            return a.strip(), b.strip()
    return None


def kalshi_match_market(market: Market) -> VenueMatchMarket | None:
    meta = market.metadata
    series = str(meta.get("series_ticker") or "")
    event_ticker = str(meta.get("event_ticker") or "")
    if not series:
        series = event_ticker.split("-", 1)[0] if event_ticker else ""
    if series not in KALSHI_TENNIS_SERIES:
        return None
    yes_player = str(meta.get("subtitle") or meta.get("yes_sub_title") or "")
    if not yes_player:
        return None
    event_title = str(meta.get("event_title") or "")
    opponent: str | None = None
    if (pair := _split_vs(event_title)) is not None:
        opponent = pair[1] if same_player(yes_player, pair[0]) else pair[0]
        if not same_player(yes_player, pair[0]) and not same_player(yes_player, pair[1]):
            opponent = None
    return VenueMatchMarket(
        market=market,
        yes_player=yes_player,
        opponent=opponent,
        scheduled_start=parse_time(meta.get("occurrence_datetime") or meta.get("expected_expiration_time")),
        rules_text=str(meta.get("resolution_text") or ""),
        series=series,
        match_key=f"kalshi:{event_ticker or market.market_id}",
        lookup=market.market_id,
    )


def polymarket_match_market(market: Market) -> VenueMatchMarket | None:
    meta = market.metadata
    outcomes = [str(o) for o in (meta.get("outcomes") or [])]
    kind = str(meta.get("sports_market_type") or "")
    if len(outcomes) != 2 or {o.lower() for o in outcomes} & _BINARY_NAMES:
        return None
    if kind and kind != "moneyline":
        return None
    if not kind and _split_vs(market.title) is None:
        return None
    return VenueMatchMarket(
        market=market,
        yes_player=outcomes[0],
        opponent=outcomes[1],
        scheduled_start=parse_time(meta.get("game_start_time")),
        rules_text=str(meta.get("resolution_text") or ""),
        series=str(meta.get("event_slug") or meta.get("slug") or "").split("-", 1)[0].upper() or None,
        match_key=f"polymarket:{market.market_id}",
        lookup=str(meta.get("slug") or market.market_id),
    )


def tennis_markets(snapshot: VenueSnapshot) -> list[VenueMatchMarket]:
    builder = kalshi_match_market if snapshot.venue is Venue.KALSHI else polymarket_match_market
    out = []
    for market in snapshot.markets:
        built = builder(market)
        if built is not None:
            out.append(built)
    return out


# --------------------------------------------------------------------------
# Pairing with the outside feed
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Pairing:
    event: OutsideEvent | None
    reason: str  # paired | no_outside_match | ambiguous_match | opponent_mismatch
    candidates: int = 0


def pair_outside_event(vm: VenueMatchMarket, events: list[OutsideEvent], *, parameters: BasisParameters) -> Pairing:
    matches: list[OutsideEvent] = []
    window = timedelta(hours=float(parameters.match_window_hours))
    for event in events:
        a, b = same_player(vm.yes_player, event.player_a), same_player(vm.yes_player, event.player_b)
        if a == b:  # neither side, or both (same surname): not a usable match
            continue
        other = event.player_b if a else event.player_a
        if vm.opponent is not None and not same_player(vm.opponent, other):
            continue
        if vm.scheduled_start and event.commence_time and abs(event.commence_time - vm.scheduled_start) > window:
            continue
        matches.append(event)
    if not matches:
        return Pairing(None, "no_outside_match")
    if len(matches) > 1:
        return Pairing(None, "ambiguous_match", len(matches))
    return Pairing(matches[0], "paired", 1)


# --------------------------------------------------------------------------
# Gap register
# --------------------------------------------------------------------------
@dataclass(slots=True)
class GapRecord:
    record_id: str
    venue: str
    market_id: str
    title: str
    yes_player: str
    opponent: str | None
    outside_event_id: str
    sport_key: str
    lookup: str
    opened_at: str
    scheduled_start: str | None
    mid_open: str
    outside_open: str
    gap_open: str
    side: str | None
    consensus_method_open: str
    books_open: list[str]
    settlement_basis: dict[str, Any]
    observations: list[dict[str, Any]] = field(default_factory=list)
    status: str = "open"  # open | closed_at_start | no_pre_start_observation
    closed_at: str | None = None
    mid_final: str | None = None
    outside_final: str | None = None
    gap_final: str | None = None
    closure_fraction: str | None = None
    closed_half: bool | None = None
    overshoot: bool | None = None
    settlement_status: str = "not_applicable"  # pending | confirmed | excluded
    settlement_detail: str | None = None
    start_revisions: int = 0
    paper: dict[str, Any] = field(default_factory=dict)

    @property
    def start(self) -> datetime | None:
        return parse_time(self.scheduled_start)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GapRegister:
    records: dict[str, GapRecord] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: _now().isoformat())
    updated_at: str = field(default_factory=lambda: _now().isoformat())

    def open_records(self) -> list[GapRecord]:
        return [r for r in self.records.values() if r.status == "open"]

    def closed_records(self) -> list[GapRecord]:
        return [r for r in self.records.values() if r.status == "closed_at_start"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REGISTER_SCHEMA,
            "paper_only": True,
            "track": TENNIS_TRACK,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "records": [self.records[k].as_dict() for k in sorted(self.records)],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GapRegister:
        if payload.get("paper_only") is not True:
            raise ValueError("refusing to load a gap register that is not marked paper_only")
        register = cls(created_at=payload.get("created_at", _now().isoformat()), updated_at=payload.get("updated_at", _now().isoformat()))
        for item in payload.get("records", []):
            record = GapRecord(**item)
            register.records[record.record_id] = record
        return register

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = _now().isoformat()
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    @classmethod
    def load_or_create(cls, path: Path) -> GapRegister:
        if path.exists():
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls()


def register_path(artifact_dir: Path) -> Path:
    return artifact_dir / "tennis_basis" / "register.json"


# --------------------------------------------------------------------------
# Settlement checks (post-start filter)
# --------------------------------------------------------------------------
SettlementLookup = Callable[[GapRecord], Awaitable[dict[str, Any] | None]]


def classify_settlement(venue: Venue, status: dict[str, Any] | None, operator_result: str | None = None) -> tuple[str, str | None]:
    """``(settlement_status, detail)`` from a venue status payload and an optional operator result."""
    if operator_result:
        result = operator_result.strip().lower()
        if result in {"retired", "retirement", "walkover", "cancelled", "canceled", "withdrawn", "default"}:
            return "excluded", f"operator_result:{result}"
        if result in {"completed", "played"}:
            pass  # fall through to the venue's own verdict
    if not status:
        return "pending", None
    if venue is Venue.KALSHI:
        state = str(status.get("status") or "").lower()
        result = str(status.get("result") or "").lower()
        if state in {"finalized", "settled", "determined"}:
            if result in {"yes", "no"}:
                return "confirmed", f"kalshi_result:{result}"
            return "excluded", f"kalshi_non_binary_settlement:{result or 'fair_price'}"
        return "pending", None
    prices = [_dec(p) for p in (status.get("outcome_prices") or [])]
    if status.get("closed") and len(prices) == 2 and all(p is not None for p in prices):
        if {p.quantize(Decimal("0.01")) for p in prices} <= {ZERO, ONE}:
            return "confirmed", f"polymarket_resolved:{[str(p) for p in prices]}"
        return "excluded", f"polymarket_non_binary_settlement:{[str(p) for p in prices]}"
    return "pending", None


async def network_settlement_lookup(record: GapRecord, *, kalshi_env: str | None) -> dict[str, Any] | None:
    if record.venue == Venue.KALSHI.value:
        client = KalshiClient(paper=True, use_fixtures=False, environment=kalshi_env, series_tickers=KALSHI_TENNIS_SERIES)
        try:
            return await client.get_market_status(record.lookup)
        finally:
            await client.close()
    client = PolymarketClient(paper=True, use_fixtures=False, search_terms=None)
    try:
        return await client.get_market_status(record.lookup)
    finally:
        await client.close()


# --------------------------------------------------------------------------
# One cycle
# --------------------------------------------------------------------------
def _observation(at: datetime, mid: Decimal | None, outside: Decimal | None, method: str, start: datetime | None) -> dict[str, Any]:
    g = (mid - outside) if (mid is not None and outside is not None) else None
    return {
        "at": at.isoformat(),
        "mid": str(_q(mid)) if mid is not None else None,
        "outside": str(_q(outside)) if outside is not None else None,
        "gap": str(_q(g)) if g is not None else None,
        "method": method,
        "pre_start": bool(start is None or at < start),
    }


def _close_record(record: GapRecord, *, as_of: datetime, parameters: BasisParameters) -> None:
    start = record.start
    pre_start = [o for o in record.observations[1:] if o.get("gap") is not None and (start is None or parse_time(o["at"]) < start)]
    record.closed_at = as_of.isoformat()
    if not pre_start:
        record.status = "no_pre_start_observation"
        record.settlement_status = "not_applicable"
        return
    last = pre_start[-1]
    g0, gt = Decimal(record.gap_open), Decimal(last["gap"])
    fraction = closure_fraction(g0, gt)
    record.status = "closed_at_start"
    record.mid_final, record.outside_final, record.gap_final = last["mid"], last["outside"], last["gap"]
    record.closure_fraction = str(_q(fraction)) if fraction is not None else None
    record.closed_half = bool(fraction is not None and fraction >= parameters.closure_target)
    record.overshoot = bool(fraction is not None and fraction > ONE)
    record.settlement_status = "pending"


async def run_tennis_basis_cycle(
    runtime: TrackRuntime,
    register: GapRegister,
    *,
    outside: OutsideBatch,
    parameters: BasisParameters | None = None,
    as_of: datetime | None = None,
    settlement_lookup: SettlementLookup | None = None,
    operator_results: dict[str, str] | None = None,
) -> TrackSummary:
    params = parameters or BasisParameters()
    as_of = as_of or _now()
    summary = runtime.summary
    summary.label = TENNIS_TRACK_LABEL
    lean = TennisBasisLean(parameters=params, portfolio=runtime.ledger.portfolio, risk=runtime.risk)
    rows: list[dict[str, Any]] = []
    seen_matches: set[str] = set()
    opened = observed = closed = 0
    markets_by_venue: dict[str, int] = {}
    present_records: set[str] = set()

    for venue, snapshot in runtime.snapshots.items():
        venue_markets = tennis_markets(snapshot)
        markets_by_venue[venue.value] = len(venue_markets)
        for vm in venue_markets:
            market, book = vm.market, snapshot.book(vm.market)
            summary.candidates += 1
            row: dict[str, Any] = {
                "venue": venue.value,
                "market_id": market.market_id,
                "title": market.title,
                "yes_player": vm.yes_player,
                "opponent": vm.opponent,
                "scheduled_start": vm.scheduled_start.isoformat() if vm.scheduled_start else None,
            }
            rows.append(row)

            # 1. Mandatory settlement-basis filter, before anything is priced.
            basis = classify_settlement_basis(vm.rules_text, series=vm.series)
            row["settlement_basis"] = basis.as_dict()
            if not basis.admitted:
                row["reason"] = basis.reason
                summary.refuse(basis.reason)
                continue

            # 2. Exactly one outside event, by both players' names.
            pairing = pair_outside_event(vm, outside.events, parameters=params)
            if pairing.event is None:
                row["reason"] = pairing.reason
                summary.refuse(pairing.reason)
                continue
            event = pairing.event
            outside_p, line, side_tag = event.probability_for(vm.yes_player, parameters=params, as_of=as_of)
            row["outside_event"] = event.as_dict()
            row["consensus"] = line.as_dict()
            start = event.commence_time or vm.scheduled_start
            row["start"] = start.isoformat() if start else None
            mid = venue_mid(book)
            row["mid"] = _q(mid)
            row["outside"] = _q(outside_p)
            row["gap"] = _q(mid - outside_p) if (mid is not None and outside_p is not None) else None

            record_id = f"{venue.value}:{market.market_id}:{event.event_id}"
            record = register.records.get(record_id)
            if record is not None:
                present_records.add(record_id)
                seen_matches.add(vm.match_key)  # the other Kalshi side must not open a mirror record
                if record.status != "open":
                    row["reason"] = "record_" + record.status
                    summary.refuse("record_already_closed")
                    continue
                # Keep the start current (postponements) and append this run's observation,
                # even when the line is unavailable this run (gap None, still dated).
                if start and record.scheduled_start != start.isoformat():
                    record.scheduled_start = start.isoformat()
                    record.start_revisions += 1
                record.observations.append(_observation(as_of, mid, outside_p, line.method, start))
                observed += 1
                if start is not None and as_of >= start:
                    _close_record(record, as_of=as_of, parameters=params)
                    _unwind(runtime, record, market, mid)
                    closed += 1
                    row["reason"] = "closed_at_start" if record.status == "closed_at_start" else record.status
                else:
                    row["reason"] = "tracking_open_gap"
                summary.refuse(row["reason"])
                continue
            if outside_p is None:
                reason = "consensus_" + (line.method if side_tag in ("a", "b") else side_tag)
                row["reason"] = reason
                summary.refuse(reason)
                continue

            # 3. New gap: only one side of a Kalshi match, only before the start.
            if vm.match_key in seen_matches:
                row["reason"] = "duplicate_side"
                summary.refuse("duplicate_side")
                continue
            seen_matches.add(vm.match_key)
            if start is not None and as_of >= start:
                row["reason"] = "event_started"
                summary.refuse("event_started")
                continue
            evaluation = lean.evaluate(market, book, outside_p, record_id=record_id, consensus=line)
            row["lean"] = evaluation.as_dict()
            if mid is None or evaluation.reason in ("no_two_sided_mid", "gap_below_threshold"):
                row["reason"] = evaluation.reason
                summary.refuse(evaluation.reason)
                continue
            record = GapRecord(
                record_id=record_id,
                venue=venue.value,
                market_id=market.market_id,
                title=market.title,
                yes_player=vm.yes_player,
                opponent=vm.opponent,
                outside_event_id=event.event_id,
                sport_key=event.sport_key,
                lookup=vm.lookup,
                opened_at=as_of.isoformat(),
                scheduled_start=start.isoformat() if start else None,
                mid_open=str(_q(mid)),
                outside_open=str(_q(outside_p)),
                gap_open=str(_q(mid - outside_p)),
                side=evaluation.side,
                consensus_method_open=line.method,
                books_open=list(line.books_used),
                settlement_basis=basis.as_dict(),
                observations=[_observation(as_of, mid, outside_p, line.method, start)],
                paper={"lean_reason": evaluation.reason, "fills": 0, "quantity": "0", "fees": "0"},
            )
            register.records[record_id] = record
            present_records.add(record_id)
            opened += 1
            summary.admitted += 1
            row["reason"] = "gap_opened"
            if not evaluation.traded:
                # The gap is measured either way; the lean just could not be executed at the touch.
                summary.refuse("lean_" + evaluation.reason)
                continue
            summary.proposed_orders += len(evaluation.orders)
            summary.admitted_edges.append(evaluation.cost_adjusted_edge or ZERO)
            fills_before = summary.paper_fills
            for order in evaluation.orders:
                report = await runtime.submit(order, edge=evaluation.cost_adjusted_edge)
                if report is not None:
                    qty = sum((f.quantity for f in report.fills), ZERO)
                    fees = sum((f.fee for f in report.fills), ZERO)
                    record.paper["fills"] = len(report.fills)
                    record.paper["quantity"] = str(qty)
                    record.paper["fees"] = str(fees)
                    record.paper["entry_yes_price"] = str(report.fills[0].yes_equivalent_price) if report.fills else None
            row["paper_fills"] = summary.paper_fills - fills_before
            summary.edges.append(_edge_row(runtime.name, record, evaluation.cost_adjusted_edge, filled=summary.paper_fills > fills_before))

    # 4. Open records not observed above: either the outside event vanished from the
    #    feed (market still quoted -> dated mid, no gap) or the venue delisted the
    #    market. Both are recorded; the record still closes at its start.
    mids_by_market = {
        (venue.value, m.market_id): venue_mid(snap.book(m)) for venue, snap in runtime.snapshots.items() for m in snap.markets
    }
    for record in register.open_records():
        if record.record_id in present_records:
            continue
        key = (record.venue, record.market_id)
        listed = key in mids_by_market
        mid = mids_by_market.get(key)
        record.observations.append(
            {
                "at": as_of.isoformat(),
                "mid": str(_q(mid)) if mid is not None else None,
                "outside": None,
                "gap": None,
                "method": "outside_event_missing" if listed else "market_missing",
                "pre_start": bool(record.start is None or as_of < record.start),
            }
        )
        # A delisted market with no known start can never reach "start": close it on what was observed.
        if (not listed and record.start is None) or (record.start is not None and as_of >= record.start):
            _close_record(record, as_of=as_of, parameters=params)
            market = runtime.market_for(Venue(record.venue), record.market_id)
            _unwind(runtime, record, market, mid)
            closed += 1

    # 5. Post-start settlement filter for closed records.
    settlement_counts = await _settle(register, settlement_lookup, operator_results or {})

    confirmed = [Decimal(r.closure_fraction) for r in register.closed_records() if r.settlement_status == "confirmed" and r.closure_fraction is not None]
    provisional = [Decimal(r.closure_fraction) for r in register.closed_records() if r.settlement_status in ("confirmed", "pending") and r.closure_fraction is not None]
    excluded = sum(1 for r in register.closed_records() if r.settlement_status == "excluded")
    pending = sum(1 for r in register.closed_records() if r.settlement_status == "pending")
    no_obs = sum(1 for r in register.records.values() if r.status == "no_pre_start_observation")

    summary.metrics.update(
        {
            "status": track_status(outside, summary),
            "as_of": as_of.isoformat(),
            "parameters": params.as_dict(),
            "outside_source": outside.as_dict(),
            "free_source": FREE_TIER,
            "bookmaker_basis": BOOKMAKER_BASIS,
            "markets_by_venue": markets_by_venue,
            "gaps_opened": opened,
            "gaps_observed": observed,
            "gaps_closed": closed,
            "register": {
                "records": len(register.records),
                "open": len(register.open_records()),
                "closed_at_start": len(register.closed_records()),
                "no_pre_start_observation": no_obs,
                "settlement_confirmed": len(confirmed),
                "settlement_pending": pending,
                "settlement_excluded": excluded,
                "settlement_checks_this_run": settlement_counts,
            },
            "verdict": verdict(confirmed, parameters=params, excluded_settlement_mismatch=excluded, pending_settlement_check=pending, open_records=len(register.open_records())).as_dict(),
            "verdict_provisional_including_pending": verdict(provisional, parameters=params, excluded_settlement_mismatch=excluded, pending_settlement_check=pending, open_records=len(register.open_records())).as_dict(),
            "by_venue": _by_venue(register, params),
            "measurements": rows,
            "records": [r.as_dict() for r in register.records.values()],
            "network_status": _network_status(outside),
        }
    )
    summary.settlement_risk_flag = False
    summary.notes = (
        "Venue mid vs. a free public consensus line (The Odds API free tier; Pinnacle when quoted, else the "
        "median of >=2 books, multiplicatively de-vigged). |gap| >= 3c opens a record and a paper lean toward "
        "the line; records close on the last observation before the scheduled start and the position is "
        "unwound at that mid. Pre-registered pass: >= 60% of gaps close >= 50% (n >= 30). Mandatory settlement "
        "filter: unreadable / ITF / mismatched retirement-walkover clauses are refused before pricing, and a "
        "match that settles as a walkover, cancellation, 50-50 or fair price (or is reported retired) is "
        "excluded after the fact. The bookmaker line is P(win | completed); venues price P(advances); that "
        "basis difference is documented, not modelled."
    )
    return summary


def _network_status(outside: OutsideBatch) -> str:
    if outside.source == "none":
        return (
            "UNKNOWN: no free outside line configured in this run; set ODDS_API_KEY (The Odds API free tier) "
            "or pass --odds-file. Fixture replays prove the arithmetic only."
        )
    if outside.source == "fixture":
        return "fixture_synthetic"
    if outside.source == "the_odds_api":
        return "measured_against_free_source"
    return f"measured_against_operator_supplied_lines ({outside.source}); provenance is the operator's"


def track_status(outside: OutsideBatch, summary: TrackSummary) -> str:
    if outside.source == "none":
        return "no_outside_source"
    if outside.source == "fixture":
        return "fixture_synthetic"
    if not outside.events and outside.errors:
        return "outside_source_errors"
    if not outside.events:
        return "no_outside_events"
    if summary.candidates == 0:
        return "no_venue_tennis_markets"
    return "measured"


def _unwind(runtime: TrackRuntime, record: GapRecord, market: Market | None, mid: Decimal | None) -> None:
    venue = Venue(record.venue)
    position = runtime.ledger.portfolio.get(venue, record.market_id)
    if position is None or position.quantity == ZERO:
        record.paper["unwind"] = "no_position"
        return
    # Unwind at the last pre-start mid (the price the measurement is defined on),
    # never at an in-play print; fall back to the current mid, then the last mark.
    price = Decimal(record.mid_final) if record.mid_final is not None else mid
    if price is None:
        price = runtime.ledger.marks.get((venue, record.market_id), position.average_price)
        record.paper["unwind_price_source"] = "last_mark"
    client = runtime.clients[venue]
    schedule = client._fee_schedule_for(market) if market is not None else client.fee_schedule
    fee = schedule(abs(position.quantity), price)
    fill = runtime.ledger.close_position(venue, record.market_id, yes_price=price, order_id=UNWIND_ORDER_ID, fee=fee)
    runtime.ledger.mark(venue, record.market_id, price)
    if fill is not None:
        # Unwinds are booked on the ledger and listed, but not counted as paper_fills:
        # fill_rate stays fills-per-proposed-lean.
        runtime.summary.metrics["unwind_fills"] = int(runtime.summary.metrics.get("unwind_fills", 0)) + 1
        runtime.summary.fills.append(
            {
                "track": runtime.name,
                "venue": venue.value,
                "market": record.market_id,
                "title": record.title,
                "side": fill.side.value,
                "outcome": fill.outcome.value,
                "qty": fill.quantity,
                "price": fill.price,
                "yes_equivalent_price": fill.yes_equivalent_price,
                "fee": fill.fee,
                "edge_bps": None,
                "paper_pnl": None,
                "filled_at": fill.timestamp.isoformat(),
                "order_id": UNWIND_ORDER_ID,
                "strategy": TENNIS_TRACK,
            }
        )
        record.paper["unwind"] = {"price": str(_q(price)), "quantity": str(fill.quantity), "fee": str(fee)}
        realized = runtime.ledger.portfolio.get(venue, record.market_id)
        record.paper["realized_pnl"] = str(_q(realized.realized_pnl)) if realized else None


async def _settle(register: GapRegister, lookup: SettlementLookup | None, operator_results: dict[str, str]) -> dict[str, int]:
    counts = {"checked": 0, "confirmed": 0, "excluded": 0, "pending": 0, "lookup_errors": 0}
    for record in register.closed_records():
        if record.settlement_status != "pending":
            continue
        counts["checked"] += 1
        status: dict[str, Any] | None = None
        if lookup is not None:
            try:
                status = await lookup(record)
            except Exception as exc:  # a dead endpoint leaves the record pending
                counts["lookup_errors"] += 1
                record.settlement_detail = f"lookup_error: {type(exc).__name__}: {exc}"
        operator = operator_results.get(record.outside_event_id) or operator_results.get(record.record_id)
        state, detail = classify_settlement(Venue(record.venue), status, operator)
        record.settlement_status = state
        if detail is not None:
            record.settlement_detail = detail
        counts[state] += 1
    return counts


def _by_venue(register: GapRegister, params: BasisParameters) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for venue in (Venue.KALSHI.value, Venue.POLYMARKET.value):
        closed = [r for r in register.closed_records() if r.venue == venue]
        confirmed = [Decimal(r.closure_fraction) for r in closed if r.settlement_status == "confirmed" and r.closure_fraction]
        out[venue] = {
            "open": sum(1 for r in register.open_records() if r.venue == venue),
            "closed_at_start": len(closed),
            "confirmed": verdict(confirmed, parameters=params).as_dict(),
        }
    return out


def _edge_row(track: str, record: GapRecord, edge: Decimal | None, *, filled: bool) -> dict[str, Any]:
    return {
        "track": track,
        "venue": record.venue,
        "market": record.market_id,
        "title": record.title,
        "record_id": record.record_id,
        "edge_bps": int((edge * 10000).to_integral_value()) if edge is not None else None,
        "gap_bps": int((Decimal(record.gap_open) * 10000).to_integral_value()),
        "admitted": True,
        "filled": filled,
        "reason": "gap_opened",
        "fair_value": Decimal(record.outside_open),
        "mid": Decimal(record.mid_open),
        "side": record.side,
    }


# --------------------------------------------------------------------------
# Snapshots (network) and replay (fixtures)
# --------------------------------------------------------------------------
async def capture_tennis_snapshots(*, limit: int, kalshi_env: str | None = None) -> dict[Venue, VenueSnapshot]:
    """Public read-only capture of both venues' tennis match-winner universes."""
    kalshi = KalshiClient(paper=True, use_fixtures=False, environment=kalshi_env, series_tickers=KALSHI_TENNIS_SERIES)
    polymarket = PolymarketClient(paper=True, use_fixtures=False, search_terms=None)

    async def poly() -> VenueSnapshot:
        snapshot = VenueSnapshot(venue=Venue.POLYMARKET, source="network")
        try:
            markets = await polymarket.list_markets_by_tag(POLYMARKET_TENNIS_TAG, limit=max(limit * 4, 50))
        except Exception as exc:
            snapshot.errors.append(f"list_markets_by_tag: {type(exc).__name__}: {exc}")
            return snapshot
        snapshot.markets = [m for m in markets if polymarket_match_market(m) is not None][:limit]
        for market in snapshot.markets:
            try:
                snapshot.books[market.market_id] = await polymarket.get_order_book(market)
            except Exception as exc:
                snapshot.errors.append(f"order_book[{market.market_id}]: {type(exc).__name__}: {exc}")
                snapshot.books[market.market_id] = OrderBook(market_id=market.market_id)
        return snapshot

    try:
        k_snap, p_snap = await asyncio.gather(capture_snapshot(kalshi, limit=limit, source="network"), poly())
    finally:
        await asyncio.gather(kalshi.close(), polymarket.close())
    return {Venue.KALSHI: k_snap, Venue.POLYMARKET: p_snap}


@dataclass(slots=True)
class ReplayStep:
    as_of: datetime
    snapshots: dict[Venue, VenueSnapshot]
    outside: list[OutsideEvent]
    settlements: dict[str, dict[str, Any]]
    operator_results: dict[str, str]
    label: str = ""


def load_replay(path: Path = REPLAY_FIXTURE) -> list[ReplayStep]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    steps: list[ReplayStep] = []
    for item in payload["steps"]:
        snapshots: dict[Venue, VenueSnapshot] = {}
        for venue in (Venue.KALSHI, Venue.POLYMARKET):
            doc = item.get(venue.value) or {"markets": [], "order_books": {}}
            markets, books = parse_fixture(doc, venue)
            snapshots[venue] = VenueSnapshot(venue=venue, source="fixture", markets=markets, books=books)
        as_of = parse_time(item["as_of"])
        if as_of is None:
            raise ValueError(f"replay step {item.get('label')!r} has no valid as_of")
        steps.append(
            ReplayStep(
                as_of=as_of,
                snapshots=snapshots,
                outside=parse_events(item.get("outside") or [], source="fixture"),
                settlements={str(k): v for k, v in (item.get("settlements") or {}).items()},
                operator_results={str(k): str(v) for k, v in (item.get("operator_results") or {}).items()},
                label=str(item.get("label") or ""),
            )
        )
    return steps


def _runtime(snapshots: dict[Venue, VenueSnapshot], ledger: PaperLedger | None, limits: RiskLimits, starting_cash: Decimal, model_fees: bool) -> TrackRuntime:
    return TrackRuntime.create(TENNIS_TRACK, snapshots, ledger=ledger, risk_limits=limits, starting_cash=starting_cash, model_fees=model_fees)


def _finish(runtime: TrackRuntime, *, label: str) -> TrackSummary:
    runtime.finalize(label=label)
    runtime.summary.metrics["venue_pnl"] = _venue_pnl(runtime.ledger)
    runtime.summary.metrics["snapshot"] = {
        venue.value: {"source": snap.source, "markets": len(snap.markets), "groups": 0, "errors": snap.errors}
        for venue, snap in runtime.snapshots.items()
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
    register: GapRegister | None = None,
    parameters: BasisParameters | None = None,
    risk_limits: RiskLimits | None = None,
    starting_cash: Decimal = DEFAULT_STARTING_CASH,
    model_fees: bool = True,
) -> tuple[TrackSummary, PaperLedger, GapRegister, list[TrackSummary]]:
    """Replay every fixture step through the live code path; returns the aggregate."""
    steps = steps or load_replay()
    register = register or GapRegister()
    limits = risk_limits or DEFAULT_RISK_LIMITS
    step_summaries: list[TrackSummary] = []
    runtime: TrackRuntime | None = None
    for step in steps:
        runtime = _runtime(step.snapshots, ledger, limits, starting_cash, model_fees)
        ledger = runtime.ledger
        outside = await StaticOutsideSource(step.outside, note=f"fixture step {step.label}").fetch(as_of=step.as_of)

        async def lookup(record: GapRecord, _settlements: dict[str, dict[str, Any]] = step.settlements) -> dict[str, Any] | None:
            return _settlements.get(record.record_id) or _settlements.get(f"{record.venue}:{record.market_id}")

        summary = await run_tennis_basis_cycle(
            runtime, register, outside=outside, parameters=parameters, as_of=step.as_of,
            settlement_lookup=lookup, operator_results=step.operator_results,
        )
        summary.metrics["step"] = step.label
        step_summaries.append(_finish(runtime, label=f"fixtures:{step.label or step.as_of.isoformat()}"))
    assert runtime is not None and ledger is not None
    aggregate = TrackSummary(TENNIS_TRACK, label=TENNIS_TRACK_LABEL)
    for step_summary in step_summaries:
        _merge(aggregate, step_summary)
    last = step_summaries[-1]
    aggregate.metrics = {
        **last.metrics,
        "steps": [s.metrics.get("step") for s in step_summaries],
        "replayed_steps": len(step_summaries),
        **{
            key: sum(int(s.metrics.get(key, 0)) for s in step_summaries)
            for key in ("gaps_opened", "gaps_observed", "gaps_closed", "unwind_fills")
        },
    }
    aggregate.notes = last.notes
    aggregate.ledger = last.ledger
    aggregate.settlement_risk_flag = False
    return aggregate, ledger, register, step_summaries


async def measure_tennis_basis(
    *,
    use_fixtures: bool = True,
    limit: int = 60,
    kalshi_env: str | None = None,
    outside_source: OutsideLineSource | None = None,
    parameters: BasisParameters | None = None,
    ledger: PaperLedger | None = None,
    register: GapRegister | None = None,
    risk_limits: RiskLimits | None = None,
    starting_cash: Decimal = DEFAULT_STARTING_CASH,
    model_fees: bool = True,
    as_of: datetime | None = None,
    snapshots: dict[Venue, VenueSnapshot] | None = None,
    settlement_lookup: SettlementLookup | None = None,
    operator_results: dict[str, str] | None = None,
) -> tuple[TrackSummary, PaperLedger, GapRegister]:
    """Fixtures: replay the committed steps. Network: one cycle against public data."""
    if use_fixtures:
        summary, ledger, register, _ = await replay_fixture(
            ledger=ledger, register=register, parameters=parameters, risk_limits=risk_limits,
            starting_cash=starting_cash, model_fees=model_fees,
        )
        return summary, ledger, register
    from research.tennis_odds import NullOutsideSource

    as_of = as_of or _now()
    register = register or GapRegister()
    source = outside_source or NullOutsideSource()
    snapshots = snapshots or await capture_tennis_snapshots(limit=limit, kalshi_env=kalshi_env)
    try:
        outside = await source.fetch(as_of=as_of)
    except Exception as exc:  # the outside feed must never take the run down
        outside = OutsideBatch(source=getattr(source, "name", type(source).__name__))
        outside.errors.append(f"fetch: {type(exc).__name__}: {exc}")
    runtime = _runtime(snapshots, ledger, risk_limits or DEFAULT_RISK_LIMITS, starting_cash, model_fees)
    lookup = settlement_lookup
    if lookup is None:
        async def lookup(record: GapRecord) -> dict[str, Any] | None:
            return await network_settlement_lookup(record, kalshi_env=kalshi_env)
    summary = await run_tennis_basis_cycle(
        runtime, register, outside=outside, parameters=parameters, as_of=as_of,
        settlement_lookup=lookup, operator_results=operator_results,
    )
    _finish(runtime, label=f"network:{as_of.isoformat()}")
    return summary, runtime.ledger, register


def load_operator_results(path: Path) -> dict[str, str]:
    """``{outside_event_id_or_record_id: completed|retired|walkover|cancelled}``."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("results", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, dict):
        raise ValueError("results file must be an object of event_id -> result")
    return {str(k): str(v.get("result") if isinstance(v, dict) else v) for k, v in items.items()}


__all__ = [
    "GapRecord",
    "GapRegister",
    "KALSHI_TENNIS_SERIES",
    "ODDS_API_KEY_ENV",
    "POLYMARKET_TENNIS_TAG",
    "REPLAY_FIXTURE",
    "ReplayStep",
    "TENNIS_TRACK",
    "TENNIS_TRACKS",
    "TENNIS_TRACK_LABEL",
    "UNWIND_ORDER_ID",
    "VenueMatchMarket",
    "capture_tennis_snapshots",
    "classify_settlement",
    "kalshi_match_market",
    "load_operator_results",
    "load_replay",
    "measure_tennis_basis",
    "network_settlement_lookup",
    "pair_outside_event",
    "polymarket_match_market",
    "register_path",
    "replay_fixture",
    "run_tennis_basis_cycle",
    "tennis_markets",
    "track_status",
]
