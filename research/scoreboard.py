"""Five isolated paper tracks measured against one shared market snapshot.

Every track owns its own risk manager, execution engine, paper portfolio and
:class:`core.ledger.PaperLedger`, so numbers never leak between tracks. Market
data is captured once per venue (fixture or public network read) and served to
every track from memory, which keeps paper fills deterministic for a run.
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
from core.types import ONE, ZERO, ExecutionReport, Fill, Market, Order, OrderBook, Outcome, Venue
from core.venue import VenueClient
from settlement.clauses import ClauseVerdict, detect_clauses, pair_clause_verdict
from settlement.fingerprint import Relation, compare, from_mapping
from settlement.hosts import HostTier, classify
from strategies.cross_venue import (
    CrossVenueEvaluation,
    CrossVenueMispricingStrategy,
    CrossVenueParameters,
)
from strategies.edge import CalibratedFairValueStrategy, FairValueEvaluation
from strategies.matching import MarketMatcher, MatchedMarketPair
from venues.kalshi import KalshiClient
from venues.paper import FeeSchedule, PaperExecutionMixin, kalshi_fee, zero_fee
from venues.polymarket import PolymarketClient

LOGGER = logging.getLogger("scoreboard")

TRACKS: tuple[str, ...] = (
    "gated_cross_venue_macro",
    "ungated_cross_venue_macro",
    "single_venue_fair_value",
    "sports_cross_venue",
    "small_deliberate_bet",
)
TRACK_LABELS = {
    "gated_cross_venue_macro": "Gated cross-venue macro",
    "ungated_cross_venue_macro": "Ungated cross-venue macro",
    "single_venue_fair_value": "Single-venue fair value",
    "sports_cross_venue": "Sports cross-venue",
    "small_deliberate_bet": "Small deliberate bet",
}
CROSS_VENUE_TRACKS = {
    "gated_cross_venue_macro",
    "ungated_cross_venue_macro",
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

    def book(self, market: Market) -> OrderBook:
        return self.books.get(market.market_id, OrderBook(market_id=market.market_id))


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

    def __init__(self, snapshot: VenueSnapshot, fee_schedule: FeeSchedule) -> None:
        self.venue = snapshot.venue
        self.paper = True
        self.snapshot = snapshot
        self._init_paper(fee_schedule)
        self._market_cache.update({m.market_id: m for m in snapshot.markets})

    async def list_markets(self, *, limit: int = 20) -> list[Market]:
        return self.snapshot.markets[:limit]

    async def get_order_book(self, market: Market) -> OrderBook:
        return self.snapshot.book(market)

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# Settlement gate
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GateResult:
    admitted: bool
    reason: str
    details: dict[str, Any]


def settlement_gate(pair: MatchedMarketPair) -> GateResult:
    """Fail-closed admissibility check: clauses, then fingerprint, then hosts."""
    k_meta, p_meta = pair.kalshi.metadata, pair.polymarket.metadata
    k_clauses = detect_clauses(str(k_meta.get("resolution_text") or ""))
    p_clauses = detect_clauses(str(p_meta.get("resolution_text") or ""))
    clause_verdict = pair_clause_verdict(k_clauses, p_clauses)

    k_fp, p_fp = k_meta.get("fingerprint"), p_meta.get("fingerprint")
    if isinstance(k_fp, dict) and isinstance(p_fp, dict):
        try:
            verdict = compare(from_mapping(k_fp), from_mapping(p_fp))
            relation, fp_reason = verdict.relation, verdict.reason
        except ValueError as exc:
            relation, fp_reason = Relation.INDETERMINATE, f"unparseable fingerprint: {exc}"
    else:
        relation, fp_reason = Relation.INDETERMINATE, "fingerprint missing on at least one side"

    k_tier = classify(str(k_meta.get("source_url") or (k_clauses.source_urls or [""])[0]))
    p_tier = classify(str(p_meta.get("source_url") or (p_clauses.source_urls or [""])[0]))
    host_conflict = k_tier != p_tier or HostTier.UNCLASSIFIED in (k_tier, p_tier)

    polarity_ok = (relation is Relation.EQUIVALENT and pair.same_polarity) or (
        relation is Relation.COMPLEMENT and not pair.same_polarity
    )
    if clause_verdict is not ClauseVerdict.ADMIT:
        reason = f"clause_{clause_verdict.value}"
    elif relation in (Relation.INDETERMINATE, Relation.NOT_EQUIVALENT):
        reason = f"fingerprint_{relation.value}"
    elif not polarity_ok:
        reason = "fingerprint_polarity_conflict"
    elif host_conflict:
        reason = "host_conflict"
    else:
        reason = "admitted"
    details = {
        "clause_verdict": clause_verdict.value,
        "fingerprint_relation": relation.value,
        "fingerprint_reason": fp_reason,
        "kalshi_host_tier": k_tier.value,
        "polymarket_host_tier": p_tier.value,
        "host_conflict": host_conflict,
        "kalshi_clauses": k_clauses.as_dict(),
        "polymarket_clauses": p_clauses.as_dict(),
        "match_method": pair.method,
        "match_confidence": pair.confidence,
    }
    return GateResult(reason == "admitted", reason, details)


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
        clients = {venue: SnapshotClient(snap, fee_for[venue]) for venue, snap in snapshots.items()}
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


async def run_cross_venue_track(
    runtime: TrackRuntime,
    *,
    pair_filter: Any,
    gate: bool,
    parameters: CrossVenueParameters,
    flag_when_gate_would_refuse: bool,
    matcher: MarketMatcher | None = None,
) -> TrackSummary:
    summary = runtime.summary
    kalshi_snap, poly_snap = runtime.snapshots[Venue.KALSHI], runtime.snapshots[Venue.POLYMARKET]
    all_pairs = (matcher or MarketMatcher()).match(kalshi_snap.markets, poly_snap.markets)
    pairs = [pair for pair in all_pairs if pair_filter(pair)]
    summary.candidates = len(pairs)
    summary.metrics["matched_pairs_all_categories"] = len(all_pairs)
    summary.metrics["gate_enabled"] = gate
    summary.metrics["parameters"] = {
        "minimum_mid_edge": parameters.minimum_mid_edge,
        "maximum_order_size": parameters.maximum_order_size,
        "fee_buffer_per_contract": parameters.fee_buffer_per_contract,
    }
    refused_pairs: dict[str, Any] = {}
    gate_results: dict[str, Any] = {}
    host_conflicts = 0
    gate_would_refuse = 0
    strategy = CrossVenueMispricingStrategy(
        risk=runtime.risk, portfolio=runtime.ledger.portfolio, parameters=parameters
    )
    for pair in pairs:
        result = settlement_gate(pair)
        gate_results[pair.pair_id] = {"admitted": result.admitted, "reason": result.reason, **result.details}
        host_conflicts += int(result.details["host_conflict"])
        if gate and not result.admitted:
            summary.refuse(result.reason)
            refused_pairs[pair.pair_id] = {"reason": result.reason, **result.details}
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
    summary.metrics["host_conflicts"] = host_conflicts
    summary.metrics["gate_would_have_refused_traded_pairs"] = gate_would_refuse
    summary.settlement_risk_flag = (
        gate_would_refuse > 0 if flag_when_gate_would_refuse else host_conflicts > 0
    )
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


async def measure_all(**kwargs: Any) -> list[TrackSummary]:
    """Run all five tracks once and return their summaries (ledgers discarded)."""
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
    cycle_label: str = "",
) -> tuple[list[TrackSummary], dict[str, PaperLedger]]:
    """Run all five tracks against one shared snapshot.

    Returns the summaries plus the per-track ledgers so callers can persist
    them. ``ledgers`` lets a caller (the paper loop) carry a track's ledger
    across cycles; any track not present gets a fresh ledger.
    """
    snapshots = snapshots or await capture_snapshots(
        use_fixtures=use_fixtures, limit=limit, kalshi_env=kalshi_env
    )
    limits = risk_limits or DEFAULT_RISK_LIMITS
    ledgers = ledgers or {}

    def runtime(name: str) -> TrackRuntime:
        return TrackRuntime.create(
            name,
            snapshots,
            ledger=ledgers.get(name),
            risk_limits=limits,
            starting_cash=starting_cash,
            model_fees=model_fees,
        )

    gated_rt, ungated_rt, fair_rt, sports_rt, small_rt = (runtime(name) for name in TRACKS)
    default_params = CrossVenueParameters()
    summaries = await asyncio.gather(
        run_cross_venue_track(
            gated_rt, pair_filter=_is_macro, gate=True, parameters=default_params,
            flag_when_gate_would_refuse=True,
        ),
        run_cross_venue_track(
            ungated_rt, pair_filter=_is_macro, gate=False, parameters=default_params,
            flag_when_gate_would_refuse=True,
        ),
        run_fair_value_track(fair_rt, priors=priors),
        run_cross_venue_track(
            sports_rt, pair_filter=_is_sports, gate=False, parameters=default_params,
            flag_when_gate_would_refuse=False,
        ),
        run_cross_venue_track(
            small_rt, pair_filter=_is_macro, gate=True,
            parameters=CrossVenueParameters(maximum_order_size=Decimal("2")),
            flag_when_gate_would_refuse=True,
        ),
    )
    runtimes = (gated_rt, ungated_rt, fair_rt, sports_rt, small_rt)
    label = cycle_label or f"{'fixtures' if use_fixtures else 'network'}:{_now().isoformat()}"
    for rt in runtimes:
        rt.finalize(label=label)
        rt.summary.metrics["venue_pnl"] = _venue_pnl(rt.ledger)
        rt.summary.metrics["snapshot"] = {
            venue.value: {"source": snap.source, "markets": len(snap.markets), "errors": snap.errors}
            for venue, snap in snapshots.items()
        }

    gated_rt.summary.notes = (
        "Macro pairs must pass clause, fingerprint and host gates before any paper order. "
        "Refusals are the expected state when venues' settlement rules diverge."
    )
    ungated_rt.summary.notes = (
        "Same macro pairs traded without the settlement gate. Exists only to measure what the "
        "gate refuses; flagged settlement-risk whenever a traded pair would have been refused."
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
    fair_rt.summary.metrics["kalshi_canary"] = {
        "venue_breakdown": fair_rt.summary.metrics["venue_breakdown"].get(Venue.KALSHI.value, {}),
        "pnl": fair_rt.summary.metrics["venue_pnl"].get(Venue.KALSHI.value, {}),
    }
    return list(summaries), {rt.name: rt.ledger for rt in runtimes}
