"""Ten isolated paper tracks measured against shared market snapshots.

Every track owns its own risk manager, execution engine, paper portfolio and
:class:`core.ledger.PaperLedger`, so numbers never leak between tracks. Market
data is captured once per venue (fixture or public network read) and served to
every track from memory, which keeps paper fills deterministic for a run.

``news_underreaction`` is an optional lane: it only has candidates when a
signal source is configured (fixtures by default on fixture runs, nothing on
network runs) and is otherwise an honest empty row. The three
``polymarket_*_arb`` tracks read a separate event snapshot (YES and NO book per
leg); see ``research/polymarket_arb_tracks.py``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from core.execution import ExecutionEngine
from core.ledger import PaperLedger
from core.observability import InMemoryEventSink
from core.risk import RiskLimits, RiskManager, RiskViolation
from core.types import ONE, ZERO, ExecutionReport, Fill, Market, MarketGroup, Order, OrderBook, Outcome, Venue
from core.venue import VenueClient
from research.news_signals import (
    FixtureSignalSource,
    NullSignalSource,
    SignalBatch,
    SignalSource,
)
from settlement.gate import STAGE_ORDER, STRICT_POLICY, GatePolicy, GateResult, settlement_gate
from strategies.cross_venue import (
    CrossVenueEvaluation,
    CrossVenueMispricingStrategy,
    CrossVenueParameters,
    DepthAwareCrossVenueStrategy,
    DepthAwareParameters,
)
from strategies.edge import CalibratedFairValueStrategy, FairValueEvaluation
from strategies.matching import CuratedPair, MarketMatcher, MatchedMarketPair
from strategies.news_underreaction import (
    LITERATURE_REFERENCE,
    NewsUnderreactionStrategy,
    UnderreactionEvaluation,
    UnderreactionParameters,
)
from venues.kalshi import KalshiClient
from venues.paper import FeeSchedule, PaperExecutionMixin, kalshi_fee, zero_fee
from venues.polymarket import PolymarketClient
from venues.polymarket.fees import polymarket_fee_schedule

LOGGER = logging.getLogger("scoreboard")

GATED_TRACK = "gated_cross_venue"
CONTROL_TRACK = "ungated_cross_venue_macro"
CROSS_VENUE_AND_FAIR_VALUE_TRACKS: tuple[str, ...] = (
    GATED_TRACK,
    "gated_cross_venue_macro",
    CONTROL_TRACK,
    "single_venue_fair_value",
    "sports_cross_venue",
    "small_deliberate_bet",
    "news_underreaction",
)
POLYMARKET_ARB_TRACKS: tuple[str, ...] = (
    "polymarket_rebalancing_arb",
    "polymarket_negrisk_arb",
    "polymarket_combinatorial_arb",
)
TRACKS: tuple[str, ...] = CROSS_VENUE_AND_FAIR_VALUE_TRACKS + POLYMARKET_ARB_TRACKS
TRACK_LABELS = {
    GATED_TRACK: "Gated cross-venue (clause-matched)",
    "gated_cross_venue_macro": "Gated cross-venue macro",
    CONTROL_TRACK: "Ungated cross-venue macro (control)",
    "single_venue_fair_value": "Single-venue fair value",
    "sports_cross_venue": "Sports cross-venue",
    "small_deliberate_bet": "Small deliberate bet",
    "news_underreaction": "News underreaction",
    "polymarket_rebalancing_arb": "Polymarket YES+NO rebalancing",
    "polymarket_negrisk_arb": "Polymarket NegRisk convert",
    "polymarket_combinatorial_arb": "Polymarket sum-to-one (hold)",
}
NEWS_TRACK = "news_underreaction"
CROSS_VENUE_TRACKS = {
    GATED_TRACK,
    "gated_cross_venue_macro",
    CONTROL_TRACK,
    "sports_cross_venue",
    "small_deliberate_bet",
}
PRIMARY_TRACK = "single_venue_fair_value"
DEFAULT_RISK_LIMITS = RiskLimits(
    max_notional_per_order=Decimal("100"),
    max_position_per_market=Decimal("500"),
    max_daily_loss=Decimal("250"),
)
DEFAULT_STARTING_CASH = Decimal("1000")
BPS = Decimal("10000")


def _now() -> datetime:
    return datetime.now(UTC)


def _bps(value: Decimal | None) -> int | None:
    return int((value * BPS).to_integral_value()) if value is not None else None


# --------------------------------------------------------------------------
# Market snapshots
# --------------------------------------------------------------------------
@dataclass(slots=True)
class VenueSnapshot:
    venue: Venue
    source: str
    markets: list[Market] = field(default_factory=list)
    books: dict[str, OrderBook] = field(default_factory=dict)
    fetched_at: str = field(default_factory=lambda: _now().isoformat())
    errors: list[str] = field(default_factory=list)
    # Multi-outcome groups and separately fetched NO books (Polymarket events).
    # ``books`` stays the YES book; ``no_books`` is only present when the venue
    # served a distinct NO ladder, so mirror consistency can be verified.
    groups: list[MarketGroup] = field(default_factory=list)
    no_books: dict[str, OrderBook] = field(default_factory=dict)

    def book(self, market: Market) -> OrderBook:
        return self.books.get(market.market_id, OrderBook(market_id=market.market_id))

    def no_book(self, market: Market) -> OrderBook | None:
        return self.no_books.get(market.market_id)


async def capture_group_snapshot(client: PolymarketClient, *, limit: int, source: str) -> VenueSnapshot:
    """Polymarket events with a YES and a NO book per leg (for the arb tracks)."""
    events = await client.capture_events(limit=limit)
    return VenueSnapshot(
        venue=client.venue,
        source=source,
        markets=list(events.markets),
        books=dict(events.yes_books),
        no_books=dict(events.no_books),
        groups=list(events.groups),
        errors=list(events.errors),
    )


async def capture_snapshot(client: VenueClient, *, limit: int, source: str) -> VenueSnapshot:
    snapshot = VenueSnapshot(venue=client.venue, source=source)
    try:
        snapshot.markets = await client.list_markets(limit=limit)
    except Exception as exc:  # network failures must not kill the whole run
        snapshot.errors.append(f"list_markets: {type(exc).__name__}: {exc}")
        return snapshot
    for market in snapshot.markets:
        try:
            snapshot.books[market.market_id] = await client.get_order_book(market)
        except Exception as exc:
            snapshot.errors.append(f"order_book[{market.market_id}]: {type(exc).__name__}: {exc}")
            snapshot.books[market.market_id] = OrderBook(market_id=market.market_id)
    return snapshot


class SnapshotClient(PaperExecutionMixin, VenueClient):
    """Paper venue client that serves one frozen snapshot. Always ``paper=True``."""

    def __init__(self, snapshot: VenueSnapshot, fee_schedule: FeeSchedule, *, model_fees: bool = True) -> None:
        self.venue = snapshot.venue
        self.paper = True
        self.snapshot = snapshot
        self.model_fees = model_fees
        self._init_paper(fee_schedule)
        self._market_cache.update({m.market_id: m for m in snapshot.markets})

    async def list_markets(self, *, limit: int = 20) -> list[Market]:
        return self.snapshot.markets[:limit]

    async def get_order_book(self, market: Market) -> OrderBook:
        return self.snapshot.book(market)

    def _fee_schedule_for(self, market: Market) -> FeeSchedule:
        """Polymarket markets carry their published taker rate in metadata."""
        if self.model_fees and market.venue is Venue.POLYMARKET:
            raw = market.metadata.get("taker_fee_rate")
            if raw not in (None, ""):
                rate = Decimal(str(raw))
                if rate > ZERO:
                    return polymarket_fee_schedule(rate)
        return self.fee_schedule

    async def place_order(self, order: Order) -> ExecutionReport:
        """NO orders walk the venue's real NO ladder when the snapshot has one."""
        if not self.paper:
            raise PermissionError("SnapshotClient is paper-only")
        market = self._market_cache.get(order.market_id)
        if market is None or not market.active:
            raise ValueError(f"market {order.market_id} is unavailable")
        no_book = self.snapshot.no_book(market)
        if order.outcome is Outcome.NO and no_book is not None:
            # ``for_outcome`` is an involution, so handing the simulator the NO
            # ladder expressed as a YES book makes it walk the real NO levels.
            return await self.place_order_with_book(order, market, no_book.for_outcome(Outcome.NO))
        return await self.place_order_with_book(order, market, self.snapshot.book(market))

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# Settlement gate: implemented in settlement.gate, re-exported for callers.
# --------------------------------------------------------------------------
__all__ = ["GateResult", "settlement_gate"]


# --------------------------------------------------------------------------
# Track summary
# --------------------------------------------------------------------------
@dataclass(slots=True)
class TrackSummary:
    track: str
    label: str = ""
    candidates: int = 0
    admitted: int = 0
    refused_by_reason: dict[str, int] = field(default_factory=dict)
    proposed_orders: int = 0
    paper_fills: int = 0
    settlement_risk_flag: bool = False
    estimated_fees_buffer: Decimal = ZERO
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    ledger: dict[str, Any] = field(default_factory=dict)
    fills: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    admitted_edges: list[Decimal] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.label:
            self.label = TRACK_LABELS.get(self.track, self.track.replace("_", " "))

    @property
    def rejects(self) -> int:
        return max(0, self.candidates - self.admitted)

    @property
    def fill_rate(self) -> Decimal | None:
        if self.proposed_orders == 0:
            return None
        return (Decimal(self.paper_fills) / Decimal(self.proposed_orders)).quantize(Decimal("0.0001"))

    @property
    def edge_bps(self) -> int | None:
        if not self.admitted_edges:
            return None
        return _bps(sum(self.admitted_edges, ZERO) / len(self.admitted_edges))

    def refuse(self, reason: str) -> None:
        self.refused_by_reason[reason] = self.refused_by_reason.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "track": self.track,
            "label": self.label,
            "candidates": self.candidates,
            "admitted": self.admitted,
            "rejects": self.rejects,
            "refused_by_reason": dict(sorted(self.refused_by_reason.items())),
            "reject_reasons": dict(sorted(self.refused_by_reason.items())),
            "proposed_orders": self.proposed_orders,
            "paper_fills": self.paper_fills,
            "fill_rate": self.fill_rate,
            "edge_bps": self.edge_bps,
            "settlement_risk_flag": self.settlement_risk_flag,
            "settlement_risk": self.settlement_risk_flag,
            "estimated_fees_buffer": self.estimated_fees_buffer,
            "metrics": self.metrics,
            "notes": self.notes,
            "ledger": self.ledger,
            "fills": self.fills,
            "edges": self.edges,
        }


# --------------------------------------------------------------------------
# Track runtime
# --------------------------------------------------------------------------
@dataclass(slots=True)
class TrackRuntime:
    name: str
    ledger: PaperLedger
    risk: RiskManager
    events: InMemoryEventSink
    execution: ExecutionEngine
    clients: dict[Venue, SnapshotClient]
    snapshots: dict[Venue, VenueSnapshot]
    summary: TrackSummary

    @classmethod
    def create(
        cls,
        name: str,
        snapshots: dict[Venue, VenueSnapshot],
        *,
        ledger: PaperLedger | None,
        risk_limits: RiskLimits,
        starting_cash: Decimal,
        model_fees: bool,
    ) -> TrackRuntime:
        ledger = ledger or PaperLedger(starting_cash=starting_cash, ledger_id=name)
        risk = RiskManager(risk_limits)
        risk.record_realized_pnl(ledger.realized_pnl)  # persisted losses still count
        events = InMemoryEventSink()
        execution = ExecutionEngine(risk=risk, events=events, ledger=ledger)
        fee_for = {
            Venue.KALSHI: kalshi_fee if model_fees else zero_fee,
            Venue.POLYMARKET: zero_fee,
        }
        clients = {
            venue: SnapshotClient(snap, fee_for[venue], model_fees=model_fees)
            for venue, snap in snapshots.items()
        }
        return cls(name, ledger, risk, events, execution, clients, snapshots, TrackSummary(name))

    def market_for(self, venue: Venue, market_id: str) -> Market | None:
        return self.clients[venue]._market_cache.get(market_id)

    async def submit(self, order: Order, *, edge: Decimal | None) -> ExecutionReport | None:
        try:
            report = await self.execution.submit(self.clients[order.venue], order)
        except RiskViolation as exc:
            self.summary.refuse("risk_" + str(exc).split(" ")[0].lower())
            self.summary.metrics.setdefault("risk_rejections", []).append(
                {"venue": order.venue.value, "market_id": order.market_id, "reason": str(exc)}
            )
            return None
        self.summary.paper_fills += len(report.fills)
        for fill in report.fills:
            self.summary.fills.append(self._fill_row(fill, order, edge))
        return report

    def _fill_row(self, fill: Fill, order: Order, edge: Decimal | None) -> dict[str, Any]:
        market = self.market_for(fill.venue, fill.market_id)
        return {
            "track": self.name,
            "venue": fill.venue.value,
            "market": fill.market_id,
            "title": market.title if market else "",
            "side": fill.side.value,
            "outcome": fill.outcome.value,
            "qty": fill.quantity,
            "price": fill.price,
            "yes_equivalent_price": fill.yes_equivalent_price,
            "fee": fill.fee,
            "edge_bps": _bps(edge),
            "paper_pnl": None,  # filled in after marks
            "filled_at": fill.timestamp.isoformat(),
            "order_id": fill.order_id,
            "strategy": order.metadata.get("strategy", ""),
        }

    def finalize(self, *, label: str) -> TrackSummary:
        """Mark open positions from the snapshot books, then snapshot equity."""
        for position in self.ledger.open_positions:
            snapshot = self.snapshots.get(position.venue)
            market = self.market_for(position.venue, position.market_id)
            if snapshot is not None and market is not None:
                self.ledger.mark_from_book(position.venue, position.market_id, snapshot.book(market))
        point = self.ledger.snapshot(label=label)
        for row in self.summary.fills:
            mark = self.ledger.marks.get((Venue(row["venue"]), row["market"]))
            if mark is None:
                continue
            signed = row["qty"] if (row["side"] == "buy") == (row["outcome"] == "yes") else -row["qty"]
            row["mark"] = mark
            row["paper_pnl"] = (signed * (mark - row["yes_equivalent_price"]) - row["fee"]).quantize(
                Decimal("0.0001")
            )
        self.summary.ledger = self.ledger.summary()
        self.summary.metrics["equity_point"] = {
            "equity": point.equity,
            "drawdown": point.drawdown,
            "total_pnl": point.total_pnl,
        }
        self.summary.metrics["events"] = {
            name: sum(1 for e in self.events.events if e["event"] == name)
            for name in ("order_submitted", "order_rejected", "fill", "order_acknowledged")
        }
        return self.summary


# --------------------------------------------------------------------------
# Track implementations
# --------------------------------------------------------------------------
def _is_macro(pair: MatchedMarketPair) -> bool:
    return pair.category == "macro" or (
        isinstance(pair.kalshi.metadata.get("fingerprint"), dict)
        and isinstance(pair.polymarket.metadata.get("fingerprint"), dict)
    )


def _is_sports(pair: MatchedMarketPair) -> bool:
    return pair.category == "sports"


def _edge_row(track: str, pair: MatchedMarketPair, evaluation: CrossVenueEvaluation, *, admitted: bool, filled: bool) -> dict[str, Any]:
    return {
        "track": track,
        "venue": "cross",
        "market": pair.pair_id,
        "title": pair.kalshi.title,
        "edge_bps": _bps(evaluation.executable_edge if evaluation.executable_edge is not None else evaluation.raw_edge),
        "raw_edge_bps": _bps(evaluation.raw_edge),
        "admitted": admitted,
        "filled": filled,
        "reason": evaluation.reason,
        "kalshi_mid": evaluation.kalshi_mid,
        "polymarket_mid": evaluation.polymarket_mid,
        "mid": evaluation.kalshi_mid,
        "fair_value": evaluation.polymarket_mid,
    }


def _count_all_reasons(results: dict[str, GateResult]) -> dict[str, int]:
    """Histogram of *every* failing stage reason across pairs (not just the primary)."""
    out: dict[str, int] = {}
    for result in results.values():
        for reason in result.reasons:
            out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items()))


async def run_cross_venue_track(
    runtime: TrackRuntime,
    *,
    pair_filter: Any,
    gate: bool,
    parameters: CrossVenueParameters,
    flag_when_gate_would_refuse: bool,
    matcher: MarketMatcher | None = None,
    policy: GatePolicy = STRICT_POLICY,
) -> TrackSummary:
    summary = runtime.summary
    kalshi_snap, poly_snap = runtime.snapshots[Venue.KALSHI], runtime.snapshots[Venue.POLYMARKET]
    matcher = matcher or MarketMatcher()
    all_pairs = matcher.match(kalshi_snap.markets, poly_snap.markets)
    pairs = [pair for pair in all_pairs if pair_filter(pair)]
    summary.candidates = len(pairs)
    summary.metrics["matched_pairs_all_categories"] = len(all_pairs)
    summary.metrics["vetoed_candidates"] = [veto.as_dict() for veto in matcher.vetoed]
    summary.metrics["gate_enabled"] = gate
    summary.metrics["gate_policy"] = policy.as_dict()
    summary.metrics["parameters"] = {
        "minimum_mid_edge": parameters.minimum_mid_edge,
        "maximum_order_size": parameters.maximum_order_size,
        "fee_buffer_per_contract": parameters.fee_buffer_per_contract,
    }
    refused_pairs: dict[str, Any] = {}
    gate_results: dict[str, Any] = {}
    raw_results: dict[str, GateResult] = {}
    host_conflicts = 0
    gate_would_refuse = 0
    strategy = CrossVenueMispricingStrategy(
        risk=runtime.risk, portfolio=runtime.ledger.portfolio, parameters=parameters
    )
    for pair in pairs:
        result = settlement_gate(pair, policy=policy)
        raw_results[pair.pair_id] = result
        gate_results[pair.pair_id] = result.as_dict()
        host_conflicts += int(result.details["host_conflict"])
        if gate and not result.admitted:
            summary.refuse(result.reason)
            refused_pairs[pair.pair_id] = {"reason": result.reason, "reasons": list(result.reasons), **result.details}
            continue
        evaluation = strategy.evaluate(pair, kalshi_snap.book(pair.kalshi), poly_snap.book(pair.polymarket))
        if not evaluation.traded:
            summary.refuse(evaluation.reason)
            summary.edges.append(_edge_row(runtime.name, pair, evaluation, admitted=False, filled=False))
            continue
        if not result.admitted:
            gate_would_refuse += 1
        summary.admitted += 1
        summary.proposed_orders += len(evaluation.orders)
        summary.admitted_edges.append(evaluation.executable_edge or ZERO)
        summary.estimated_fees_buffer += sum(
            (order.quantity * parameters.fee_buffer_per_contract for order in evaluation.orders), ZERO
        )
        fills_before = summary.paper_fills
        for order in evaluation.orders:
            await runtime.submit(order, edge=evaluation.executable_edge)
        summary.edges.append(
            _edge_row(runtime.name, pair, evaluation, admitted=True, filled=summary.paper_fills > fills_before)
        )
    summary.metrics["refused_pairs"] = refused_pairs
    summary.metrics["gate_results"] = gate_results
    summary.metrics["gate_reject_reasons_all"] = _count_all_reasons(raw_results)
    summary.metrics["host_conflicts"] = host_conflicts
    summary.metrics["gate_would_have_refused_traded_pairs"] = gate_would_refuse
    summary.settlement_risk_flag = (
        gate_would_refuse > 0 if flag_when_gate_would_refuse else host_conflicts > 0
    )
    return summary


def _gated_edge_row(
    track: str, pair: MatchedMarketPair, evaluation: CrossVenueEvaluation, *, admitted: bool, filled: bool
) -> dict[str, Any]:
    row = _edge_row(track, pair, evaluation, admitted=admitted, filled=filled)
    row["edge_bps"] = _bps(evaluation.net_edge if evaluation.net_edge is not None else evaluation.executable_edge)
    row["gross_edge_bps"] = _bps(evaluation.executable_edge)
    row["net_edge_bps"] = _bps(evaluation.net_edge)
    row["fees_per_contract"] = evaluation.fees_per_contract
    row["quantity"] = evaluation.quantity
    return row


async def run_gated_cross_venue_track(
    runtime: TrackRuntime,
    *,
    parameters: DepthAwareParameters | None = None,
    policy: GatePolicy = STRICT_POLICY,
    matcher: MarketMatcher | None = None,
) -> TrackSummary:
    """The settlement-safe track: every matched pair, strict gate, depth-aware edge.

    Order of operations per pair is fixed and fail-closed: the gate runs first
    and a refused pair never reaches the pricing step, so ``proposed_orders``
    can only come from pairs whose every gate stage passed.
    """
    summary = runtime.summary
    parameters = parameters or DepthAwareParameters()
    kalshi_snap, poly_snap = runtime.snapshots[Venue.KALSHI], runtime.snapshots[Venue.POLYMARKET]
    matcher = matcher or MarketMatcher()
    pairs = matcher.match(kalshi_snap.markets, poly_snap.markets)
    summary.candidates = len(pairs)
    summary.metrics["matched_pairs_all_categories"] = len(pairs)
    summary.metrics["vetoed_candidates"] = [veto.as_dict() for veto in matcher.vetoed]
    summary.metrics["gate_enabled"] = True
    summary.metrics["gate_policy"] = policy.as_dict()
    summary.metrics["parameters"] = parameters.as_dict()
    summary.metrics["fee_model"] = {
        venue.value: getattr(client.fee_schedule, "__name__", "custom")
        for venue, client in runtime.clients.items()
    }
    strategy = DepthAwareCrossVenueStrategy(
        risk=runtime.risk,
        portfolio=runtime.ledger.portfolio,
        fee_schedules={venue: client.fee_schedule for venue, client in runtime.clients.items()},
        parameters=parameters,
    )
    gate_results: dict[str, Any] = {}
    raw_results: dict[str, GateResult] = {}
    refused_pairs: dict[str, Any] = {}
    admitted_pairs: dict[str, Any] = {}
    priced_but_no_edge: dict[str, Any] = {}
    expected_locked_pnl = ZERO
    for pair in pairs:
        result = settlement_gate(pair, policy=policy)
        raw_results[pair.pair_id] = result
        gate_results[pair.pair_id] = {
            "kalshi": pair.kalshi.market_id,
            "polymarket": pair.polymarket.market_id,
            "kalshi_title": pair.kalshi.title,
            "polymarket_title": pair.polymarket.title,
            "category": pair.category,
            **result.as_dict(),
        }
        if not result.admitted:
            summary.refuse(result.reason)
            refused_pairs[pair.pair_id] = {"reason": result.reason, "reasons": list(result.reasons)}
            continue
        evaluation = strategy.evaluate(pair, kalshi_snap.book(pair.kalshi), poly_snap.book(pair.polymarket))
        gate_results[pair.pair_id]["edge"] = evaluation.as_dict()
        if not evaluation.traded:
            summary.refuse(f"edge_{evaluation.reason}")
            priced_but_no_edge[pair.pair_id] = evaluation.reason
            summary.edges.append(_gated_edge_row(runtime.name, pair, evaluation, admitted=False, filled=False))
            continue
        summary.admitted += 1
        admitted_pairs[pair.pair_id] = {
            "quantity": evaluation.quantity,
            "net_edge_per_contract": evaluation.net_edge,
            "fees_per_contract": evaluation.fees_per_contract,
        }
        summary.proposed_orders += len(evaluation.orders)
        summary.admitted_edges.append(evaluation.net_edge or ZERO)
        summary.estimated_fees_buffer += (evaluation.fees_per_contract or ZERO) * evaluation.quantity
        expected_locked_pnl += (evaluation.net_edge or ZERO) * evaluation.quantity
        fills_before = summary.paper_fills
        for order in evaluation.orders:
            await runtime.submit(order, edge=evaluation.net_edge)
        summary.edges.append(
            _gated_edge_row(runtime.name, pair, evaluation, admitted=True, filled=summary.paper_fills > fills_before)
        )
    summary.metrics["gate_results"] = gate_results
    summary.metrics["refused_pairs"] = refused_pairs
    summary.metrics["admitted_pairs"] = admitted_pairs
    summary.metrics["priced_but_no_edge"] = priced_but_no_edge
    summary.metrics["gate_reject_reasons_all"] = _count_all_reasons(raw_results)
    summary.metrics["gate_refused"] = len(refused_pairs)
    summary.metrics["gate_admitted"] = len(pairs) - len(refused_pairs)
    summary.metrics["host_conflicts"] = sum(int(r.details["host_conflict"]) for r in raw_results.values())
    summary.metrics["gate_would_have_refused_traded_pairs"] = 0
    summary.metrics["expected_locked_pnl_if_settlement_equivalent"] = expected_locked_pnl.quantize(Decimal("0.0001"))
    summary.metrics["stage_order"] = [stage.value for stage in STAGE_ORDER]
    summary.settlement_risk_flag = False
    return summary


def _fair_value_edge_row(track: str, market: Market, evaluation: FairValueEvaluation, *, filled: bool) -> dict[str, Any]:
    return {
        "track": track,
        "venue": market.venue.value,
        "market": market.market_id,
        "title": market.title,
        "edge_bps": _bps(evaluation.cost_adjusted_edge if evaluation.cost_adjusted_edge is not None else evaluation.raw_edge),
        "admitted": evaluation.traded,
        "filled": filled,
        "reason": evaluation.reason,
        "fair_value": evaluation.fair_value,
        "mid": evaluation.mid,
        "side": evaluation.side.value if evaluation.side else None,
    }


def _settlement_outcome(market: Market) -> Outcome | None:
    raw = market.metadata.get("paper_settlement_outcome")
    if raw in ("yes", "no"):
        return Outcome(raw)
    return None


async def run_fair_value_track(
    runtime: TrackRuntime,
    *,
    priors: dict[str, Decimal] | None,
    venues: tuple[Venue, ...] = (Venue.KALSHI, Venue.POLYMARKET),
) -> TrackSummary:
    summary = runtime.summary
    strategy = CalibratedFairValueStrategy(
        priors, portfolio=runtime.ledger.portfolio, risk=runtime.risk
    )
    breakdown: dict[str, Any] = {}
    hits = 0
    scored = 0
    settlement_preview = ZERO
    for venue in venues:
        snapshot = runtime.snapshots.get(venue)
        if snapshot is None:
            continue
        venue_stats = {
            "markets": len(snapshot.markets),
            "candidates": 0,
            "admitted": 0,
            "fills": 0,
            "reasons": {},
            "snapshot_errors": len(snapshot.errors),
        }
        for market in snapshot.markets:
            book = snapshot.book(market)
            if book.best_bid is None and book.best_ask is None:
                venue_stats["reasons"]["empty_book"] = venue_stats["reasons"].get("empty_book", 0) + 1
                continue
            venue_stats["candidates"] += 1
            summary.candidates += 1
            evaluation = strategy.evaluate(market, book)
            if not evaluation.traded:
                summary.refuse(evaluation.reason)
                venue_stats["reasons"][evaluation.reason] = venue_stats["reasons"].get(evaluation.reason, 0) + 1
                if evaluation.reason != "no_fair_value":
                    summary.edges.append(_fair_value_edge_row(runtime.name, market, evaluation, filled=False))
                continue
            summary.admitted += 1
            venue_stats["admitted"] += 1
            summary.proposed_orders += len(evaluation.orders)
            summary.admitted_edges.append(evaluation.cost_adjusted_edge or ZERO)
            params = strategy.parameters_for(venue)
            summary.estimated_fees_buffer += sum(
                (order.quantity * params.fee_buffer_per_contract for order in evaluation.orders), ZERO
            )
            fills_before = summary.paper_fills
            for order in evaluation.orders:
                report = await runtime.submit(order, edge=evaluation.cost_adjusted_edge)
                outcome = _settlement_outcome(market)
                if report is not None and outcome is not None:
                    settle_price = ONE if outcome is Outcome.YES else ZERO
                    for fill in report.fills:
                        scored += 1
                        hits += int(fill.signed_quantity * (settle_price - fill.yes_equivalent_price) > ZERO)
                        settlement_preview += (
                            fill.signed_quantity * (settle_price - fill.yes_equivalent_price) - fill.fee
                        )
            venue_stats["fills"] += summary.paper_fills - fills_before
            summary.edges.append(
                _fair_value_edge_row(runtime.name, market, evaluation, filled=summary.paper_fills > fills_before)
            )
        breakdown[venue.value] = venue_stats
    summary.metrics["venue_breakdown"] = breakdown
    summary.metrics["hit_rate"] = (
        (Decimal(hits) / Decimal(scored)).quantize(Decimal("0.0001")) if scored else None
    )
    summary.metrics["settlement_preview"] = {
        "scored_fills": scored,
        "hypothetical_pnl_at_fixture_settlement": settlement_preview.quantize(Decimal("0.0001")),
        "note": "Only fixture markets carry paper_settlement_outcome; network fills are not scored.",
    }
    summary.metrics["priors_supplied"] = len(strategy.priors)
    summary.settlement_risk_flag = False
    return summary


def _news_edge_row(track: str, market: Market | None, evaluation: UnderreactionEvaluation, *, filled: bool) -> dict[str, Any]:
    m = evaluation.measurement
    return {
        "track": track,
        "venue": m.venue.value,
        "market": m.market_id,
        "title": market.title if market else "",
        "signal_id": m.signal_id,
        "edge_bps": _bps(evaluation.cost_adjusted_edge if evaluation.cost_adjusted_edge is not None else m.residual_to_fair),
        "residual_bps": _bps(m.residual_to_fair),
        "literature_residual_bps": _bps(m.literature_residual),
        "reaction_ratio": m.reaction_ratio.quantize(Decimal("0.0001")) if m.reaction_ratio is not None else None,
        "admitted": evaluation.traded,
        "filled": filled,
        "reason": evaluation.reason,
        "fair_value": m.implied_probability,
        "mid": m.current_mid,
        "side": evaluation.fair_value.side.value if evaluation.fair_value and evaluation.fair_value.side else None,
    }


def news_track_status(batch: SignalBatch, mapping_counts: dict[str, int]) -> str:
    """One word the artifact can show for *why* the lane looks the way it does."""
    if batch.source == "none":
        return "no_signal_source"
    if batch.source == "fixture":
        return "fixture_synthetic"
    if not batch.signals and batch.errors:
        return "signal_source_errors"
    if batch.signals and mapping_counts.get("unmapped", 0) == len(batch.signals):
        return "signals_unmapped"
    if not batch.signals:
        return "no_signals_matched"
    return "operator_mapped_signals"


async def run_news_underreaction_track(
    runtime: TrackRuntime,
    *,
    source: SignalSource,
    parameters: UnderreactionParameters | None = None,
    as_of: datetime | None = None,
) -> TrackSummary:
    """Optional lane: public signal -> implied probability -> residual -> paper order.

    The signal->probability mapping is never computed here; see
    ``docs/NEWS_UNDERREACTION.md``. Without a configured source the track is
    an honest empty row (``candidates == 0``, ``status == no_signal_source``).
    """
    summary = runtime.summary
    params = parameters or UnderreactionParameters()
    as_of = as_of or _now()
    markets = {venue: list(snap.markets) for venue, snap in runtime.snapshots.items()}
    try:
        batch = await source.fetch(markets=markets, as_of=as_of)
    except Exception as exc:  # a signal source must never take the scoreboard down
        batch = SignalBatch(source=getattr(source, "name", type(source).__name__))
        batch.errors.append(f"fetch: {type(exc).__name__}: {exc}")

    strategy = NewsUnderreactionStrategy(
        parameters=params, portfolio=runtime.ledger.portfolio, risk=runtime.risk
    )
    summary.candidates = len(batch.signals)
    rows: list[dict[str, Any]] = []
    ratios: list[Decimal] = []
    mapping_counts: dict[str, int] = {}
    hits = scored = 0
    settlement_preview = ZERO
    for signal in batch.signals:
        mapping_counts[signal.mapping] = mapping_counts.get(signal.mapping, 0) + 1
        market = runtime.market_for(signal.venue, signal.market_id)
        snapshot = runtime.snapshots.get(signal.venue)
        book = snapshot.book(market) if (snapshot is not None and market is not None) else OrderBook(market_id=signal.market_id)
        evaluation = strategy.evaluate(signal, market, book, as_of=as_of)
        m = evaluation.measurement
        if m.reaction_ratio is not None:
            ratios.append(m.reaction_ratio)
        row = {**evaluation.as_dict(), "headline": signal.headline, "signal_source": signal.source}
        if not evaluation.traded:
            summary.refuse(evaluation.reason)
            if m.residual_to_fair is not None:
                summary.edges.append(_news_edge_row(runtime.name, market, evaluation, filled=False))
            rows.append(row)
            continue
        summary.admitted += 1
        summary.proposed_orders += len(evaluation.orders)
        summary.admitted_edges.append(evaluation.cost_adjusted_edge or ZERO)
        venue_params = strategy.parameters_for(signal.venue)
        summary.estimated_fees_buffer += sum(
            (order.quantity * venue_params.fee_buffer_per_contract for order in evaluation.orders), ZERO
        )
        fills_before = summary.paper_fills
        for order in evaluation.orders:
            report = await runtime.submit(order, edge=evaluation.cost_adjusted_edge)
            outcome = _settlement_outcome(market) if market is not None else None
            if report is not None and outcome is not None:
                settle_price = ONE if outcome is Outcome.YES else ZERO
                for fill in report.fills:
                    scored += 1
                    hits += int(fill.signed_quantity * (settle_price - fill.yes_equivalent_price) > ZERO)
                    settlement_preview += fill.signed_quantity * (settle_price - fill.yes_equivalent_price) - fill.fee
        row["paper_fills"] = summary.paper_fills - fills_before
        rows.append(row)
        summary.edges.append(_news_edge_row(runtime.name, market, evaluation, filled=summary.paper_fills > fills_before))

    summary.metrics["status"] = news_track_status(batch, mapping_counts)
    summary.metrics["signal_source"] = {
        "name": batch.source,
        "signals": len(batch.signals),
        "errors": batch.errors,
        "note": batch.note,
        "fetched_at": batch.fetched_at,
        "as_of": as_of.isoformat(),
    }
    summary.metrics["mapping"] = {
        "counts": dict(sorted(mapping_counts.items())),
        "status": "UNKNOWN: no component in this repository maps a signal to a probability",
    }
    summary.metrics["parameters"] = params.as_dict()
    summary.metrics["literature"] = {
        "reference": LITERATURE_REFERENCE,
        "contemporaneous_pass_through": params.literature_beta,
        "drift_horizon": "several minutes",
        "validated_here": False,
    }
    summary.metrics["reaction_ratio"] = {
        "observed_mean": (sum(ratios, ZERO) / len(ratios)).quantize(Decimal("0.0001")) if ratios else None,
        "n": len(ratios),
        "literature": params.literature_beta,
        "note": (
            "Synthetic fixture values; not evidence for or against the paper."
            if batch.source == "fixture"
            else "Requires operator-supplied pre_signal_mid and implied_probability."
        ),
    }
    summary.metrics["measurements"] = rows
    summary.metrics["hit_rate"] = (Decimal(hits) / Decimal(scored)).quantize(Decimal("0.0001")) if scored else None
    summary.metrics["settlement_preview"] = {
        "scored_fills": scored,
        "hypothetical_pnl_at_fixture_settlement": settlement_preview.quantize(Decimal("0.0001")),
        "note": "Only fixture markets carry paper_settlement_outcome; network fills are not scored.",
    }
    summary.settlement_risk_flag = False
    return summary


def _venue_pnl(ledger: PaperLedger) -> dict[str, Any]:
    out: dict[str, dict[str, Decimal]] = {}
    for position in ledger.portfolio.positions(include_flat=True):
        bucket = out.setdefault(
            position.venue.value,
            {"realized_pnl": ZERO, "unrealized_pnl": ZERO, "fees_paid": ZERO, "open_positions": ZERO},
        )
        bucket["realized_pnl"] += position.realized_pnl
        bucket["unrealized_pnl"] += ledger.unrealized_pnl_of(position)
        bucket["fees_paid"] += position.fees_paid
        bucket["open_positions"] += ONE if position.quantity != ZERO else ZERO
    for bucket in out.values():
        bucket["total_pnl"] = bucket["realized_pnl"] + bucket["unrealized_pnl"]
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
async def capture_snapshots(
    *,
    use_fixtures: bool,
    limit: int,
    kalshi_env: str | None = None,
) -> dict[Venue, VenueSnapshot]:
    source = "fixture" if use_fixtures else "network"
    kalshi = KalshiClient(paper=True, use_fixtures=use_fixtures, environment=kalshi_env)
    polymarket = PolymarketClient(paper=True, use_fixtures=use_fixtures)
    try:
        k_snap, p_snap = await asyncio.gather(
            capture_snapshot(kalshi, limit=limit, source=source),
            capture_snapshot(polymarket, limit=limit, source=source),
        )
    finally:
        await asyncio.gather(kalshi.close(), polymarket.close())
    return {Venue.KALSHI: k_snap, Venue.POLYMARKET: p_snap}


async def capture_polymarket_groups(*, use_fixtures: bool, limit: int) -> VenueSnapshot:
    """Polymarket events (both books per leg) for the intra-venue arb tracks."""
    source = "fixture" if use_fixtures else "network"
    polymarket = PolymarketClient(paper=True, use_fixtures=use_fixtures)
    try:
        return await capture_group_snapshot(polymarket, limit=limit, source=source)
    finally:
        await polymarket.close()


async def measure_all(**kwargs: Any) -> list[TrackSummary]:
    """Run every track once and return the summaries (ledgers discarded)."""
    summaries, _ = await measure_all_with_ledgers(**kwargs)
    return summaries


async def measure_all_with_ledgers(
    *,
    use_fixtures: bool = True,
    limit: int = 25,
    priors: dict[str, Decimal] | None = None,
    ledgers: dict[str, PaperLedger] | None = None,
    risk_limits: RiskLimits | None = None,
    starting_cash: Decimal = DEFAULT_STARTING_CASH,
    model_fees: bool = True,
    kalshi_env: str | None = None,
    snapshots: dict[Venue, VenueSnapshot] | None = None,
    group_snapshot: VenueSnapshot | None = None,
    cycle_label: str = "",
    news_signals: SignalSource | None = None,
    news_parameters: UnderreactionParameters | None = None,
    arb_parameters: Any = None,
    event_limit: int | None = None,
    curated_pairs: tuple[CuratedPair, ...] | None = None,
) -> tuple[list[TrackSummary], dict[str, PaperLedger]]:
    """Run every track against one shared snapshot.

    The cross-venue / fair-value / news tracks share ``snapshots`` (one per
    venue); the three Polymarket arbitrage tracks share ``group_snapshot``
    (events with a YES and a NO book per leg). Returns the summaries plus the
    per-track ledgers so callers can persist them. ``ledgers`` lets a caller
    (the paper loop) carry a track's ledger across cycles; any track not
    present gets a fresh ledger. ``curated_pairs``
    replaces the module default pair list (each track gets its own matcher).

    ``news_signals`` feeds the optional ``news_underreaction`` lane. When
    omitted, fixture runs use the committed synthetic fixture signals and
    network runs use no source at all (the lane stays empty by design).
    """
    from research.polymarket_arb_tracks import run_polymarket_arb_tracks

    if snapshots is None or group_snapshot is None:
        captured_snapshots, captured_groups = await asyncio.gather(
            capture_snapshots(use_fixtures=use_fixtures, limit=limit, kalshi_env=kalshi_env)
            if snapshots is None
            else _ready(snapshots),
            capture_polymarket_groups(use_fixtures=use_fixtures, limit=event_limit or limit)
            if group_snapshot is None
            else _ready(group_snapshot),
        )
        snapshots, group_snapshot = captured_snapshots, captured_groups
    limits = risk_limits or DEFAULT_RISK_LIMITS
    ledgers = ledgers or {}
    if news_signals is None:
        news_signals = FixtureSignalSource() if use_fixtures else NullSignalSource()

    def matcher() -> MarketMatcher:
        return MarketMatcher(curated_pairs) if curated_pairs is not None else MarketMatcher()

    def runtime(name: str, with_snapshots: dict[Venue, VenueSnapshot]) -> TrackRuntime:
        return TrackRuntime.create(
            name,
            with_snapshots,
            ledger=ledgers.get(name),
            risk_limits=limits,
            starting_cash=starting_cash,
            model_fees=model_fees,
        )

    strict_rt, gated_rt, ungated_rt, fair_rt, sports_rt, small_rt, news_rt = (
        runtime(name, snapshots) for name in CROSS_VENUE_AND_FAIR_VALUE_TRACKS
    )
    arb_runtimes = {
        name: runtime(name, {Venue.POLYMARKET: group_snapshot}) for name in POLYMARKET_ARB_TRACKS
    }
    default_params = CrossVenueParameters()
    summaries = await asyncio.gather(
        run_gated_cross_venue_track(strict_rt, policy=STRICT_POLICY, matcher=matcher()),
        run_cross_venue_track(
            gated_rt, pair_filter=_is_macro, gate=True, parameters=default_params,
            flag_when_gate_would_refuse=True, matcher=matcher(),
        ),
        run_cross_venue_track(
            ungated_rt, pair_filter=_is_macro, gate=False, parameters=default_params,
            flag_when_gate_would_refuse=True, matcher=matcher(),
        ),
        run_fair_value_track(fair_rt, priors=priors),
        run_cross_venue_track(
            sports_rt, pair_filter=_is_sports, gate=False, parameters=default_params,
            flag_when_gate_would_refuse=False, matcher=matcher(),
        ),
        run_cross_venue_track(
            small_rt, pair_filter=_is_macro, gate=True,
            parameters=CrossVenueParameters(maximum_order_size=Decimal("2")),
            flag_when_gate_would_refuse=True, matcher=matcher(),
        ),
        run_news_underreaction_track(news_rt, source=news_signals, parameters=news_parameters),
        run_polymarket_arb_tracks(arb_runtimes, parameters=_arb_params(arb_parameters, model_fees)),
    )
    arb_summaries = summaries[-1]
    summaries = list(summaries[:-1]) + list(arb_summaries)
    runtimes = (strict_rt, gated_rt, ungated_rt, fair_rt, sports_rt, small_rt, news_rt, *arb_runtimes.values())
    label = cycle_label or f"{'fixtures' if use_fixtures else 'network'}:{_now().isoformat()}"
    for rt in runtimes:
        rt.finalize(label=label)
        rt.summary.metrics["venue_pnl"] = _venue_pnl(rt.ledger)
        rt.summary.metrics["snapshot"] = {
            venue.value: {
                "source": snap.source,
                "markets": len(snap.markets),
                "groups": len(snap.groups),
                "errors": snap.errors,
            }
            for venue, snap in rt.snapshots.items()
        }

    strict_rt.summary.notes = (
        "Settlement-safe track. Every matched pair (any category) must pass all eight gate "
        "stages - match confidence, clauses, fingerprint, polarity, Fed bucket, interval, "
        "hosts, expiry - before it is priced; then both legs must clear venue fees through "
        "book depth. Zero admits is the expected state: the gate exists to prove that most "
        "cross-venue pairs are not settlement-equivalent. Control: ungated_cross_venue_macro."
    )
    gated_rt.summary.notes = (
        "Macro pairs must pass the same strict gate before any paper order, priced at the "
        "touch with a flat fee buffer. Refusals are the expected state when venues' "
        "settlement rules diverge."
    )
    ungated_rt.summary.notes = (
        "CONTROL. Same macro pairs traded without the settlement gate. Exists only to measure "
        "what the gate refuses; flagged settlement-risk whenever a traded pair would have been "
        "refused. Its PnL is not realisable: the legs may settle on different events."
    )
    fair_rt.summary.notes = (
        "Primary track. Single-venue fair value from explicit priors; no cross-venue "
        "settlement exposure by construction. Markets without a prior are never traded."
    )
    sports_rt.summary.notes = (
        "Sports pairs trade ungated but every host-tier conflict is counted and flags the track."
    )
    small_rt.summary.notes = (
        "Gated macro pairs at a 2-contract cap: a process probe, not an edge source."
    )
    news_rt.summary.notes = (
        "Optional lane. Public signal -> implied probability -> underreaction residual -> "
        "paper order via the same fair-value engine as the primary track. The signal-to-"
        "probability mapping is UNKNOWN and never computed here: fixture signals are synthetic, "
        "network runs stay empty unless an operator supplies signals; RSS headlines are matched "
        "but never mapped, so they never trade. Literature anchor arXiv:2606.07811 (0.64 "
        "pass-through) is reported for comparison, not validated."
    )
    fair_rt.summary.metrics["kalshi_canary"] = {
        "venue_breakdown": fair_rt.summary.metrics["venue_breakdown"].get(Venue.KALSHI.value, {}),
        "pnl": fair_rt.summary.metrics["venue_pnl"].get(Venue.KALSHI.value, {}),
    }
    return summaries, {rt.name: rt.ledger for rt in runtimes}


async def _ready(value: Any) -> Any:
    return value


def _arb_params(parameters: Any, model_fees: bool) -> Any:
    from dataclasses import replace

    from strategies.polymarket_arb import ArbParameters

    params = parameters or ArbParameters()
    return params if model_fees else replace(params, model_fees=False)
