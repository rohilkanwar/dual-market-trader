"""Three Polymarket intra-venue arbitrage paper tracks.

* ``polymarket_rebalancing_arb``   binary YES+NO merge / split (arXiv:2508.03474)
* ``polymarket_negrisk_arb``       buy-all-NO + NegRiskAdapter conversion (arXiv:2608.00666)
* ``polymarket_combinatorial_arb`` buy-all-YES sum-to-one, held to resolution

All three read the same frozen event snapshot (YES *and* NO book per leg), size
depth-aware with fees and a slippage buffer, and paper-fill every leg through
the track's risk-gated :class:`core.execution.ExecutionEngine` into its own
:class:`core.ledger.PaperLedger`. Off-book primitives are booked explicitly:

* merge / split legs net to a flat position, so realized PnL is
  ``sets * (1 - YES_ask - NO_ask)`` (or ``bid_sum - 1``) minus fees;
* a NegRisk conversion closes the ``K`` NO legs with ``order_id =
  "negrisk_convert"`` at YES-equivalent prices summing to ``1`` (plus the
  converter fee), which books exactly ``K - 1`` USDC of collateral;
* buy-all-YES positions stay open and are marked at the book mid, so the
  scoreboard shows the mid-mark, not the resolution payoff, and the locked
  capital is reported separately.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from core.ledger import PaperLedger
from core.risk import RiskLimits
from core.types import ONE, ZERO, Market, MarketGroup, Order, Venue
from research.scoreboard import (
    DEFAULT_RISK_LIMITS,
    DEFAULT_STARTING_CASH,
    POLYMARKET_ARB_TRACKS,
    TrackRuntime,
    TrackSummary,
    VenueSnapshot,
    _venue_pnl,
    capture_polymarket_groups,
)
from strategies.polymarket_arb import (
    ADMITTED,
    ArbEvaluation,
    ArbKind,
    ArbParameters,
    books_are_mirrors,
    evaluate_binary,
    evaluate_long_all_yes,
    evaluate_negrisk_convert,
)

REBALANCING = "polymarket_rebalancing_arb"
NEGRISK = "polymarket_negrisk_arb"
COMBINATORIAL = "polymarket_combinatorial_arb"
CONVERT_ORDER_ID = "negrisk_convert"
BPS = Decimal("10000")
Q5 = Decimal("0.00001")
Q6 = Decimal("0.000001")


def _now() -> datetime:
    return datetime.now(UTC)


def _bps(value: Decimal | None) -> int | None:
    return int((value * BPS).to_integral_value()) if value is not None else None


# --------------------------------------------------------------------------
# Execution helpers
# --------------------------------------------------------------------------
@dataclass(slots=True)
class LegResult:
    market_id: str
    requested: Decimal
    filled: Decimal = ZERO
    fees: Decimal = ZERO
    cost: Decimal = ZERO  # signed cash paid (positive = paid out)


@dataclass(slots=True)
class Execution:
    evaluation: ArbEvaluation
    legs: list[LegResult] = field(default_factory=list)
    risk_capped_sets: Decimal | None = None
    reason: str = ADMITTED

    @property
    def complete_sets(self) -> Decimal:
        return min((leg.filled for leg in self.legs), default=ZERO)

    @property
    def fills(self) -> int:
        return sum(1 for leg in self.legs if leg.filled > ZERO)

    @property
    def residual(self) -> Decimal:
        sets = self.complete_sets
        return sum((leg.filled - sets for leg in self.legs), ZERO)


def _risk_cap(runtime: TrackRuntime, orders: tuple[Order, ...]) -> Decimal:
    caps = []
    for order in orders:
        position = runtime.ledger.portfolio.get(order.venue, order.market_id)
        caps.append(runtime.risk.remaining_order_capacity(order, position))
    return min(caps, default=ZERO).to_integral_value(rounding=ROUND_DOWN)


async def _execute(runtime: TrackRuntime, evaluation: ArbEvaluation, params: ArbParameters) -> Execution:
    """Submit every leg through the risk gate; returns per-leg fills."""
    execution = Execution(evaluation)
    orders = evaluation.orders
    cap = _risk_cap(runtime, orders)
    sets = evaluation.quantity
    if cap < sets:
        execution.risk_capped_sets = cap
        sets = cap
    min_size = max((plan.leg.min_order_size for plan in evaluation.legs), default=ZERO)
    if sets <= ZERO or sets < min_size:
        execution.reason = "risk_capacity_below_min_order_size"
        return execution
    resized = tuple(
        Order(
            venue=o.venue, market_id=o.market_id, side=o.side, outcome=o.outcome,
            quantity=sets, price=o.price, metadata=dict(o.metadata),
        )
        for o in orders
    )
    for order in resized:
        result = LegResult(order.market_id, requested=sets)
        report = await runtime.submit(order, edge=evaluation.net_edge_per_set)
        if report is not None:
            for fill in report.fills:
                result.filled += fill.quantity
                result.fees += fill.fee
                result.cost += fill.quantity * fill.price if order.side.value == "buy" else -fill.quantity * fill.price
        execution.legs.append(result)
    return execution


def _convert(runtime: TrackRuntime, group: MarketGroup, execution: Execution, params: ArbParameters) -> dict[str, Any]:
    """Book the NegRiskAdapter NO-set -> collateral conversion for the complete sets."""
    sets = execution.complete_sets
    k = Decimal(len(execution.legs))
    if sets <= ZERO or k < 2:
        return {"sets": ZERO, "collateral_out": ZERO}
    fee_fraction = params.converter_fee_bps / BPS
    # Closing K short-YES legs at YES prices summing to 1 + (K-1)*fee books exactly
    # (K-1)*(1-fee) USDC of collateral against the NO tokens burned.
    total_close = ONE + (k - ONE) * fee_fraction
    per_leg = (total_close / k).quantize(Q6, rounding=ROUND_DOWN)
    prices = [per_leg] * (int(k) - 1)
    prices.append(total_close - sum(prices, ZERO))
    for leg, price in zip(execution.legs, prices, strict=True):
        runtime.ledger.close_position(
            Venue.POLYMARKET, leg.market_id, yes_price=price, quantity=sets, order_id=CONVERT_ORDER_ID
        )
    collateral = (k - ONE) * sets * (ONE - fee_fraction)
    return {
        "group": group.group_id,
        "title": group.title,
        "legs": int(k),
        "sets": sets,
        "collateral_out": collateral.quantize(Q5),
        "no_cost": sum((leg.cost for leg in execution.legs), ZERO).quantize(Q5),
        "fees": sum((leg.fees for leg in execution.legs), ZERO).quantize(Q5),
        "augmented": group.augmented,
        "hidden_placeholder_yes_valued_at_zero": group.augmented,
        "mechanism": "NegRiskAdapter.convertPositions(indexSet=all visible legs); one-way NO->collateral",
    }


def _edge_row(track: str, evaluation: ArbEvaluation, *, filled: bool, market_id: str | None = None) -> dict[str, Any]:
    return {
        "track": track,
        "venue": Venue.POLYMARKET.value,
        "market": market_id or evaluation.group_id,
        "title": evaluation.title,
        "kind": evaluation.kind.value,
        "edge_bps": evaluation.edge_bps,
        "gross_edge_bps": _bps(evaluation.gross_edge_per_set),
        "admitted": evaluation.traded,
        "filled": filled,
        "reason": evaluation.reason,
        "top_of_book_sum": evaluation.top_of_book_sum,
        "payoff_per_set": evaluation.payoff_per_set,
        "sets": evaluation.quantity,
        "net_profit": evaluation.net_profit.quantize(Q5),
        "capital_required": evaluation.capital_required.quantize(Q5),
        "executable_now": evaluation.executable_now,
        "lockup": evaluation.lockup,
        "lockup_until": evaluation.lockup_until,
        "hidden_outcome_risk": evaluation.hidden_outcome_risk,
        "mid": evaluation.top_of_book_sum,
        "fair_value": evaluation.payoff_per_set if evaluation.kind is not ArbKind.SPLIT else ONE,
    }


def _admit(summary: TrackSummary, evaluation: ArbEvaluation, execution: Execution) -> None:
    summary.admitted += 1
    summary.proposed_orders += len(execution.legs)
    summary.admitted_edges.append(evaluation.net_edge_per_set or ZERO)
    summary.estimated_fees_buffer += evaluation.total_slippage


def _quiet_top_sum(values: list[Decimal]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(values),
        "min": ordered[0],
        "median": ordered[len(ordered) // 2],
        "max": ordered[-1],
    }


# --------------------------------------------------------------------------
# Track: binary rebalancing (merge / split)
# --------------------------------------------------------------------------
async def run_rebalancing_track(runtime: TrackRuntime, params: ArbParameters) -> TrackSummary:
    summary = runtime.summary
    snap = runtime.snapshots[Venue.POLYMARKET]
    ask_sums: list[Decimal] = []
    bid_sums: list[Decimal] = []
    mirror_consistent = 0
    mirror_inconsistent = 0
    mirror_inconsistent_markets: list[dict[str, Any]] = []
    opportunities: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    for market in snap.markets:
        yes_book = snap.book(market)
        no_book = snap.no_book(market)
        if no_book is None:
            summary.refuse("no_no_book")
            continue
        if yes_book.best_ask is None or no_book.best_ask is None or yes_book.best_bid is None or no_book.best_bid is None:
            summary.refuse("empty_book")
            continue
        summary.candidates += 1
        ask_sums.append(yes_book.best_ask.price + no_book.best_ask.price)
        bid_sums.append(yes_book.best_bid.price + no_book.best_bid.price)
        if books_are_mirrors(yes_book, no_book):
            mirror_consistent += 1
        else:
            mirror_inconsistent += 1
            if len(mirror_inconsistent_markets) < 25:
                mirror_inconsistent_markets.append(
                    {
                        "market": market.market_id,
                        "title": market.title,
                        "yes_ask_plus_no_ask": ask_sums[-1],
                        "yes_bid_plus_no_bid": bid_sums[-1],
                        "yes_levels": len(yes_book.bids) + len(yes_book.asks),
                        "no_levels": len(no_book.bids) + len(no_book.asks),
                        # Non-zero skew means the two ladders were captured at
                        # different instants inside one batch response.
                        "book_timestamp_skew_ms": int(
                            abs((yes_book.timestamp - no_book.timestamp).total_seconds()) * 1000
                        ),
                    }
                )
        evaluation = evaluate_binary(market, yes_book, no_book, params)
        if not evaluation.traded:
            summary.refuse(evaluation.reason)
            if evaluation.gross_edge_per_set is not None and evaluation.gross_edge_per_set > ZERO:
                opportunities.append(evaluation.as_dict())
                summary.edges.append(_edge_row(runtime.name, evaluation, filled=False, market_id=market.market_id))
            continue
        execution = await _execute(runtime, evaluation, params)
        if execution.reason != ADMITTED:
            summary.refuse(execution.reason)
            summary.edges.append(_edge_row(runtime.name, evaluation, filled=False, market_id=market.market_id))
            continue
        _admit(summary, evaluation, execution)
        opportunities.append(evaluation.as_dict())
        executions.append(
            {
                "market": market.market_id,
                "kind": evaluation.kind.value,
                "sets_planned": evaluation.quantity,
                "sets_complete": execution.complete_sets,
                "residual_contracts": execution.residual,
                "risk_capped_sets": execution.risk_capped_sets,
                "mechanism": "CTF mergePositions" if evaluation.kind is ArbKind.MERGE else "CTF splitPosition + sell both legs",
            }
        )
        summary.edges.append(_edge_row(runtime.name, evaluation, filled=execution.fills > 0, market_id=market.market_id))
    summary.metrics.update(
        {
            "markets_checked": summary.candidates,
            "mirror_consistent": mirror_consistent,
            "mirror_inconsistent": mirror_inconsistent,
            "mirror_inconsistent_markets": mirror_inconsistent_markets,
            "top_of_book_ask_sum": _quiet_top_sum(ask_sums),
            "top_of_book_bid_sum": _quiet_top_sum(bid_sums),
            "opportunities": opportunities,
            "executions": executions,
            "parameters": _params_dict(params),
            "literature": "arXiv:2508.03474 (rebalancing); live CLOB YES/NO books are mirrors so ask_sum >= 1 + spread",
        }
    )
    summary.settlement_risk_flag = False
    summary.notes = (
        "Binary YES+NO rebalancing: buy both below 1 and merge, or split and sell both above 1. "
        "Depth-aware, fee- and slippage-adjusted. The live CLOB serves mirror books, so this is "
        "expected to be quiet; mirror_consistent counts how often that held."
    )
    return summary


# --------------------------------------------------------------------------
# Track: NegRisk buy-all-NO + convert
# --------------------------------------------------------------------------
def _group_row(group: MarketGroup, evaluation: ArbEvaluation, snap: VenueSnapshot) -> dict[str, Any]:
    yes_bids = [snap.book(m).best_bid for m in group.markets]
    yes_asks = [snap.book(m).best_ask for m in group.markets]
    no_asks = [nb.best_ask if (nb := snap.no_book(m)) is not None else None for m in group.markets]
    return {
        "group": group.group_id,
        "title": group.title,
        "slug": group.metadata.get("slug"),
        "legs": group.size,
        "listed_markets": group.metadata.get("listed_markets"),
        "neg_risk": group.convertible,
        "augmented": group.augmented,
        "exclusive": group.exclusive,
        "yes_bid_sum": sum((lvl.price for lvl in yes_bids if lvl), ZERO) if all(yes_bids) else None,
        "yes_ask_sum": sum((lvl.price for lvl in yes_asks if lvl), ZERO) if all(yes_asks) else None,
        "no_ask_sum": sum((lvl.price for lvl in no_asks if lvl), ZERO) if all(no_asks) else None,
        "kind": evaluation.kind.value,
        "reason": evaluation.reason,
        "gross_edge_per_set": evaluation.gross_edge_per_set,
        "top_fees_per_set": evaluation.top_fees_per_set.quantize(Q5) if evaluation.top_fees_per_set is not None else None,
        "top_slippage_per_set": evaluation.top_slippage_per_set.quantize(Q5) if evaluation.top_slippage_per_set is not None else None,
        "net_edge_per_set": evaluation.net_edge_per_set.quantize(Q5) if evaluation.net_edge_per_set is not None else None,
        "sets": evaluation.quantity,
        "net_profit": evaluation.net_profit.quantize(Q5),
        "capital_required": evaluation.capital_required.quantize(Q5),
        "end_date": group.metadata.get("end_date"),
    }


async def run_negrisk_track(runtime: TrackRuntime, params: ArbParameters) -> TrackSummary:
    summary = runtime.summary
    snap = runtime.snapshots[Venue.POLYMARKET]
    rows: list[dict[str, Any]] = []
    conversions: list[dict[str, Any]] = []
    residual = ZERO
    convertible = 0
    augmented = 0
    single_outcome = 0
    for group in snap.groups:
        if group.size < 2:
            single_outcome += 1  # binary event: nothing to convert, not a candidate
            continue
        summary.candidates += 1
        convertible += int(group.convertible)
        augmented += int(group.augmented)
        evaluation = evaluate_negrisk_convert(group, snap.no_books, params)
        rows.append(_group_row(group, evaluation, snap))
        if not evaluation.traded:
            summary.refuse(evaluation.reason)
            if evaluation.gross_edge_per_set is not None and evaluation.gross_edge_per_set > ZERO:
                summary.edges.append(_edge_row(runtime.name, evaluation, filled=False))
            continue
        execution = await _execute(runtime, evaluation, params)
        if execution.reason != ADMITTED:
            summary.refuse(execution.reason)
            summary.edges.append(_edge_row(runtime.name, evaluation, filled=False))
            continue
        _admit(summary, evaluation, execution)
        conversion = _convert(runtime, group, execution, params)
        conversion["risk_capped_sets"] = execution.risk_capped_sets
        conversions.append(conversion)
        residual += execution.residual
        summary.edges.append(_edge_row(runtime.name, evaluation, filled=execution.fills > 0))
    rows.sort(key=_group_sort_key(admissible="neg_risk"), reverse=True)
    summary.metrics.update(
        {
            "groups_checked": summary.candidates,
            "single_outcome_groups_skipped": single_outcome,
            "convertible_groups": convertible,
            "augmented_groups": augmented,
            "groups": rows,
            "conversions": conversions,
            "legging_residual_contracts": residual,
            "converter_fee_bps": params.converter_fee_bps,
            "parameters": _params_dict(params),
            "literature": "arXiv:2608.00666 (executable NegRisk arbitrage; NO->YES converter asymmetry)",
        }
    )
    summary.settlement_risk_flag = False
    summary.notes = (
        "Buy one NO on every visible leg of a NegRisk event when the NO asks sum below K-1, then convert "
        "the set to K-1 USDC through the NegRiskAdapter (one-way NO->collateral, no resolution wait). "
        "Hidden placeholder outcomes only help this side. Fees and a per-leg slippage buffer are deducted."
    )
    return summary


# --------------------------------------------------------------------------
# Track: buy-all-YES (sum-to-one), held to resolution
# --------------------------------------------------------------------------
async def run_combinatorial_track(runtime: TrackRuntime, params: ArbParameters) -> TrackSummary:
    summary = runtime.summary
    snap = runtime.snapshots[Venue.POLYMARKET]
    rows: list[dict[str, Any]] = []
    holdings: list[dict[str, Any]] = []
    locked_capital = ZERO
    payoff_at_resolution = ZERO
    single_outcome = 0
    for group in snap.groups:
        if group.size < 2:
            single_outcome += 1
            continue
        summary.candidates += 1
        evaluation = evaluate_long_all_yes(group, snap.books, params)
        rows.append(_group_row(group, evaluation, snap))
        if not evaluation.traded:
            summary.refuse(evaluation.reason)
            if evaluation.gross_edge_per_set is not None and evaluation.gross_edge_per_set > ZERO:
                summary.edges.append(_edge_row(runtime.name, evaluation, filled=False))
            continue
        execution = await _execute(runtime, evaluation, params)
        if execution.reason != ADMITTED:
            summary.refuse(execution.reason)
            summary.edges.append(_edge_row(runtime.name, evaluation, filled=False))
            continue
        _admit(summary, evaluation, execution)
        cost = sum((leg.cost for leg in execution.legs), ZERO)
        fees = sum((leg.fees for leg in execution.legs), ZERO)
        sets = execution.complete_sets
        locked_capital += cost
        payoff_at_resolution += sets
        holdings.append(
            {
                "group": group.group_id,
                "title": group.title,
                "legs": group.size,
                "sets_complete": sets,
                "residual_contracts": execution.residual,
                "cost": cost.quantize(Q5),
                "fees": fees.quantize(Q5),
                "payoff_at_resolution": sets,
                "profit_at_resolution": (sets - cost - fees).quantize(Q5),
                "lockup_until": evaluation.lockup_until,
                "risk_capped_sets": execution.risk_capped_sets,
                "mechanism": "hold every YES to resolution; no YES->collateral converter exists",
            }
        )
        summary.edges.append(_edge_row(runtime.name, evaluation, filled=execution.fills > 0))
    rows.sort(key=_group_sort_key(admissible="exclusive"), reverse=True)
    summary.metrics.update(
        {
            "groups_checked": summary.candidates,
            "single_outcome_groups_skipped": single_outcome,
            "groups": rows,
            "holdings": holdings,
            "locked_capital": locked_capital.quantize(Q5),
            "payoff_at_resolution": payoff_at_resolution,
            "lockup_until": max((h["lockup_until"] or "" for h in holdings), default=None) or None,
            "parameters": _params_dict(params),
            "literature": "arXiv:2508.03474 (combinatorial / sum-to-one); one-way converter means capital is locked",
        }
    )
    summary.settlement_risk_flag = False
    summary.notes = (
        "Buy one YES on every leg of an exclusive event when the asks sum below 1. No converter exists for "
        "YES->collateral, so positions are held to resolution: the ledger marks them at mid and "
        "locked_capital / payoff_at_resolution report the lockup. Augmented events (hidden placeholders) "
        "and groups without an exclusivity guarantee are refused."
    )
    return summary


def _group_sort_key(*, admissible: str) -> Any:
    """Admissible groups first, then by top-of-book gross edge; non-admissible
    bundles (e.g. hundreds of independent game props) never outrank them."""

    def key(row: dict[str, Any]) -> tuple[int, Decimal]:
        gross = row["gross_edge_per_set"] if row["gross_edge_per_set"] is not None else Decimal("-9")
        return (int(bool(row.get(admissible))), gross)

    return key


def _params_dict(params: ArbParameters) -> dict[str, Any]:
    return {
        "slippage_ticks": params.slippage_ticks,
        "maximum_sets": params.maximum_sets,
        "maximum_capital": params.maximum_capital,
        "minimum_net_edge_per_set": params.minimum_net_edge_per_set,
        "converter_fee_bps": params.converter_fee_bps,
        "allow_unverified_exclusivity": params.allow_unverified_exclusivity,
        "allow_hidden_outcome_long_yes": params.allow_hidden_outcome_long_yes,
        "model_fees": params.model_fees,
    }


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------
async def run_polymarket_arb_tracks(
    runtimes: dict[str, TrackRuntime], *, parameters: ArbParameters | None = None
) -> list[TrackSummary]:
    params = parameters or ArbParameters()
    return list(
        await asyncio.gather(
            run_rebalancing_track(runtimes[REBALANCING], params),
            run_negrisk_track(runtimes[NEGRISK], params),
            run_combinatorial_track(runtimes[COMBINATORIAL], params),
        )
    )


async def measure_polymarket_arb_with_ledgers(
    *,
    use_fixtures: bool = True,
    limit: int = 25,
    ledgers: dict[str, PaperLedger] | None = None,
    risk_limits: RiskLimits | None = None,
    starting_cash: Decimal = DEFAULT_STARTING_CASH,
    model_fees: bool = True,
    parameters: ArbParameters | None = None,
    group_snapshot: VenueSnapshot | None = None,
    cycle_label: str = "",
) -> tuple[list[TrackSummary], dict[str, PaperLedger], VenueSnapshot]:
    """Run only the three Polymarket arb tracks (dedicated CLI path)."""
    snapshot = group_snapshot or await capture_polymarket_groups(use_fixtures=use_fixtures, limit=limit)
    limits = risk_limits or DEFAULT_RISK_LIMITS
    ledgers = ledgers or {}
    runtimes = {
        name: TrackRuntime.create(
            name,
            {Venue.POLYMARKET: snapshot},
            ledger=ledgers.get(name),
            risk_limits=limits,
            starting_cash=starting_cash,
            model_fees=model_fees,
        )
        for name in POLYMARKET_ARB_TRACKS
    }
    params = parameters or ArbParameters()
    if not model_fees:
        params = replace(params, model_fees=False)
    summaries = await run_polymarket_arb_tracks(runtimes, parameters=params)
    label = cycle_label or f"{'fixtures' if use_fixtures else 'network'}:{_now().isoformat()}"
    for rt in runtimes.values():
        rt.finalize(label=label)
        rt.summary.metrics["venue_pnl"] = _venue_pnl(rt.ledger)
        rt.summary.metrics["snapshot"] = {
            Venue.POLYMARKET.value: {
                "source": snapshot.source,
                "markets": len(snapshot.markets),
                "groups": len(snapshot.groups),
                "errors": snapshot.errors,
            }
        }
    return summaries, {name: rt.ledger for name, rt in runtimes.items()}, snapshot
