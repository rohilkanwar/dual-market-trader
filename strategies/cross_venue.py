"""Cross-venue mispricing: buy the cheap YES, hedge with the dear venue's NO.

The strategy itself knows nothing about settlement rules. Whether a pair may
be traded at all is decided upstream by the settlement gates in
``research/scoreboard.py``; the ungated track exists to measure what the gate
would have refused.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

from core.execution import ExecutionEngine
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.types import ONE, ZERO, ExecutionReport, Order, OrderBook, Outcome, Side, Venue
from core.venue import VenueClient
from strategies.matching import MarketMatcher, MatchedMarketPair
from strategies.paper_edge import PaperEdge, compute_paper_edge
from venues.paper import FeeSchedule, zero_fee

_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class CrossVenueParameters:
    minimum_mid_edge: Decimal = Decimal("0.04")
    minimum_touch_size: Decimal = Decimal("1")
    maximum_order_size: Decimal = Decimal("25")
    fee_buffer_per_contract: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if self.minimum_mid_edge < ZERO or self.fee_buffer_per_contract < ZERO:
            raise ValueError("edge thresholds must not be negative")
        if self.maximum_order_size <= ZERO or self.minimum_touch_size <= ZERO:
            raise ValueError("sizes must be positive")


@dataclass(frozen=True, slots=True)
class Touch:
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


def outcome_touch(book: OrderBook, outcome: Outcome) -> Touch | None:
    """Best bid/ask for ``outcome`` normalised so YES and NO views are comparable."""
    view = book.for_outcome(outcome)
    if view.best_bid is None or view.best_ask is None:
        return None
    return Touch(
        bid=view.best_bid.price,
        ask=view.best_ask.price,
        bid_size=view.best_bid.size,
        ask_size=view.best_ask.size,
    )


@dataclass(frozen=True, slots=True)
class CrossVenueEvaluation:
    pair_id: str
    reason: str
    kalshi_mid: Decimal | None = None
    polymarket_mid: Decimal | None = None
    raw_edge: Decimal | None = None
    executable_edge: Decimal | None = None
    cheap_venue: Venue | None = None
    quantity: Decimal = ZERO
    orders: tuple[Order, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    # Depth-aware extras (None for the touch-only strategy).
    net_edge: Decimal | None = None
    fees_per_contract: Decimal | None = None
    paper_edge: dict[str, object] | None = None

    @property
    def traded(self) -> bool:
        return bool(self.orders)

    def as_dict(self) -> dict[str, object]:
        def s(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "pair_id": self.pair_id,
            "reason": self.reason,
            "kalshi_mid": s(self.kalshi_mid),
            "polymarket_mid": s(self.polymarket_mid),
            "raw_edge": s(self.raw_edge),
            "executable_edge": s(self.executable_edge),
            "net_edge": s(self.net_edge),
            "fees_per_contract": s(self.fees_per_contract),
            "cheap_venue": self.cheap_venue.value if self.cheap_venue else None,
            "quantity": str(self.quantity),
            "orders": len(self.orders),
            "paper_edge": self.paper_edge,
        }


def actual_outcome(pair: MatchedMarketPair, venue: Venue, normalised: Outcome) -> Outcome:
    """Map a Kalshi-polarity outcome to the outcome actually traded on ``venue``."""
    if venue is Venue.POLYMARKET and not pair.same_polarity:
        return Outcome.NO if normalised is Outcome.YES else Outcome.YES
    return normalised


class CrossVenueMispricingStrategy:
    name = "cross_venue_mispricing"

    def __init__(
        self,
        *,
        risk: RiskManager,
        portfolio: Portfolio,
        parameters: CrossVenueParameters | None = None,
    ) -> None:
        self.risk = risk
        self.portfolio = portfolio
        self.parameters = parameters or CrossVenueParameters()

    def evaluate(
        self,
        pair: MatchedMarketPair,
        kalshi_book: OrderBook,
        polymarket_book: OrderBook,
    ) -> CrossVenueEvaluation:
        params = self.parameters
        kalshi_touch = outcome_touch(kalshi_book, Outcome.YES)
        poly_outcome = Outcome.YES if pair.same_polarity else Outcome.NO
        poly_touch = outcome_touch(polymarket_book, poly_outcome)
        if kalshi_touch is None or poly_touch is None:
            return CrossVenueEvaluation(pair.pair_id, "missing_touch")
        base = {"kalshi_mid": kalshi_touch.mid, "polymarket_mid": poly_touch.mid}

        raw_edge = abs(kalshi_touch.mid - poly_touch.mid)
        if raw_edge <= params.minimum_mid_edge:
            return CrossVenueEvaluation(pair.pair_id, "below_mid_edge_threshold", raw_edge=raw_edge, **base)

        if kalshi_touch.mid <= poly_touch.mid:
            cheap_venue, cheap, dear_venue, dear = Venue.KALSHI, kalshi_touch, Venue.POLYMARKET, poly_touch
            cheap_market, dear_market = pair.kalshi, pair.polymarket
        else:
            cheap_venue, cheap, dear_venue, dear = Venue.POLYMARKET, poly_touch, Venue.KALSHI, kalshi_touch
            cheap_market, dear_market = pair.polymarket, pair.kalshi

        executable_edge = dear.bid - cheap.ask
        common = {**base, "raw_edge": raw_edge, "executable_edge": executable_edge, "cheap_venue": cheap_venue}
        if executable_edge - 2 * params.fee_buffer_per_contract <= ZERO:
            return CrossVenueEvaluation(pair.pair_id, "executable_edge_below_fees", **common)
        if min(cheap.ask_size, dear.bid_size) < params.minimum_touch_size:
            return CrossVenueEvaluation(pair.pair_id, "insufficient_touch_depth", **common)

        hedge_price = ONE - dear.bid
        if not (ZERO < cheap.ask < ONE and ZERO < hedge_price < ONE):
            return CrossVenueEvaluation(pair.pair_id, "touch_at_bound", **common)

        # Outcomes in normalised (Kalshi-polarity) terms, mapped back per venue.
        def actual_outcome(venue: Venue, normalised: Outcome) -> Outcome:
            if venue is Venue.POLYMARKET and not pair.same_polarity:
                return Outcome.NO if normalised is Outcome.YES else Outcome.YES
            return normalised

        metadata = {
            "strategy": self.name,
            "pair_id": pair.pair_id,
            "match_method": pair.method,
            "raw_edge": str(raw_edge),
            "executable_edge": str(executable_edge),
            "fee_buffer_per_contract": str(params.fee_buffer_per_contract),
            "hedge_assumption": "buy_complementary_outcome",
            "price_signal_status": "unvalidated",
        }
        quantity = min(cheap.ask_size, dear.bid_size, params.maximum_order_size)
        cheap_probe = Order(
            venue=cheap_venue,
            market_id=cheap_market.market_id,
            side=Side.BUY,
            quantity=max(quantity, _QUANTUM),
            outcome=actual_outcome(cheap_venue, Outcome.YES),
            price=cheap.ask if actual_outcome(cheap_venue, Outcome.YES) is Outcome.YES else ONE - cheap.ask,
        )
        dear_probe = Order(
            venue=dear_venue,
            market_id=dear_market.market_id,
            side=Side.BUY,
            quantity=max(quantity, _QUANTUM),
            outcome=actual_outcome(dear_venue, Outcome.NO),
            price=hedge_price if actual_outcome(dear_venue, Outcome.NO) is Outcome.NO else dear.bid,
        )
        for probe in (cheap_probe, dear_probe):
            position = self.portfolio.get(probe.venue, probe.market_id)
            direction = ONE if probe.signed_quantity > ZERO else -ONE
            if position is not None and position.quantity * direction >= params.maximum_order_size:
                return CrossVenueEvaluation(
                    pair.pair_id, "target_position_reached", metadata=metadata, **common
                )
            quantity = min(quantity, self.risk.remaining_order_capacity(probe, position))
        quantity = quantity.quantize(_QUANTUM)
        if quantity <= ZERO:
            return CrossVenueEvaluation(pair.pair_id, "no_position_headroom", metadata=metadata, **common)

        orders = (
            Order(
                venue=cheap_probe.venue,
                market_id=cheap_probe.market_id,
                side=Side.BUY,
                quantity=quantity,
                outcome=cheap_probe.outcome,
                price=cheap_probe.price,
                metadata={**metadata, "leg": "cheap_yes"},
            ),
            Order(
                venue=dear_probe.venue,
                market_id=dear_probe.market_id,
                side=Side.BUY,
                quantity=quantity,
                outcome=dear_probe.outcome,
                price=dear_probe.price,
                metadata={**metadata, "leg": "dear_no_hedge"},
            ),
        )
        return CrossVenueEvaluation(
            pair.pair_id, "trade", quantity=quantity, orders=orders, metadata=metadata, **common
        )


@dataclass(frozen=True, slots=True)
class DepthAwareParameters:
    """Sizing knobs for :class:`DepthAwareCrossVenueStrategy`.

    ``minimum_net_edge`` is per contract *after* both venues' fees; the walk
    stops at the first level whose marginal contract no longer clears it.
    """

    minimum_net_edge: Decimal = Decimal("0.01")
    minimum_quantity: Decimal = Decimal("1")
    maximum_order_size: Decimal = Decimal("25")

    def __post_init__(self) -> None:
        if self.minimum_net_edge < ZERO:
            raise ValueError("minimum_net_edge must not be negative")
        if self.maximum_order_size <= ZERO or self.minimum_quantity <= ZERO:
            raise ValueError("sizes must be positive")

    def as_dict(self) -> dict[str, str]:
        return {
            "minimum_net_edge": str(self.minimum_net_edge),
            "minimum_quantity": str(self.minimum_quantity),
            "maximum_order_size": str(self.maximum_order_size),
        }


class DepthAwareCrossVenueStrategy:
    """Cross-venue YES/NO lock priced through book depth and venue fees.

    Used by the ``gated_cross_venue`` track. It assumes the pair has already
    been admitted by ``settlement.gate``; it never inspects settlement rules.
    """

    name = "gated_cross_venue_depth_aware"

    def __init__(
        self,
        *,
        risk: RiskManager,
        portfolio: Portfolio,
        fee_schedules: dict[Venue, FeeSchedule],
        parameters: DepthAwareParameters | None = None,
    ) -> None:
        self.risk = risk
        self.portfolio = portfolio
        self.fee_schedules = fee_schedules
        self.parameters = parameters or DepthAwareParameters()

    def _size(
        self,
        pair: MatchedMarketPair,
        cheap_venue: Venue,
        cheap_view: OrderBook,
        dear_venue: Venue,
        dear_view: OrderBook,
        max_quantity: Decimal,
    ) -> PaperEdge:
        return compute_paper_edge(
            cheap_yes_view=cheap_view,
            dear_yes_view=dear_view,
            cheap_venue=cheap_venue,
            dear_venue=dear_venue,
            fee_cheap=self.fee_schedules.get(cheap_venue, zero_fee),
            fee_dear=self.fee_schedules.get(dear_venue, zero_fee),
            max_quantity=max_quantity,
            min_quantity=self.parameters.minimum_quantity,
            min_net_edge=self.parameters.minimum_net_edge,
        )

    def evaluate(
        self,
        pair: MatchedMarketPair,
        kalshi_book: OrderBook,
        polymarket_book: OrderBook,
    ) -> CrossVenueEvaluation:
        params = self.parameters
        kalshi_view = kalshi_book
        poly_view = polymarket_book.for_outcome(Outcome.YES if pair.same_polarity else Outcome.NO)
        kalshi_touch = outcome_touch(kalshi_view, Outcome.YES)
        poly_touch = outcome_touch(poly_view, Outcome.YES)
        if kalshi_touch is None or poly_touch is None:
            return CrossVenueEvaluation(pair.pair_id, "missing_touch")
        base = {"kalshi_mid": kalshi_touch.mid, "polymarket_mid": poly_touch.mid}
        raw_edge = abs(kalshi_touch.mid - poly_touch.mid)

        # Either direction may lock a payout; take the one with the better touch.
        k_cheap = poly_touch.bid - kalshi_touch.ask
        p_cheap = kalshi_touch.bid - poly_touch.ask
        if k_cheap >= p_cheap:
            cheap_venue, cheap_view, dear_venue, dear_view = Venue.KALSHI, kalshi_view, Venue.POLYMARKET, poly_view
            cheap_market, dear_market = pair.kalshi, pair.polymarket
        else:
            cheap_venue, cheap_view, dear_venue, dear_view = Venue.POLYMARKET, poly_view, Venue.KALSHI, kalshi_view
            cheap_market, dear_market = pair.polymarket, pair.kalshi

        edge = self._size(pair, cheap_venue, cheap_view, dear_venue, dear_view, params.maximum_order_size)
        common = {
            **base,
            "raw_edge": raw_edge,
            "executable_edge": edge.touch_gross_edge,
            "cheap_venue": cheap_venue,
            "net_edge": edge.net_edge_per_contract,
            "fees_per_contract": edge.fees_per_contract,
            "paper_edge": edge.as_dict(),
        }
        if not edge.tradeable:
            return CrossVenueEvaluation(pair.pair_id, edge.reason, **common)
        assert edge.yes_leg is not None and edge.no_leg is not None

        yes_outcome = actual_outcome(pair, cheap_venue, Outcome.YES)
        no_outcome = actual_outcome(pair, dear_venue, Outcome.NO)

        def probe(venue: Venue, market_id: str, outcome: Outcome, price: Decimal, quantity: Decimal) -> Order:
            return Order(venue=venue, market_id=market_id, side=Side.BUY, quantity=quantity, outcome=outcome, price=price)

        yes_price = edge.yes_leg.limit_price or ZERO
        no_price = edge.no_leg.limit_price or ZERO
        if not (ZERO < yes_price < ONE and ZERO < no_price < ONE):
            return CrossVenueEvaluation(pair.pair_id, "touch_at_bound", **common)
        # Prices are in the traded outcome's terms; flipping the outcome flips the price.
        yes_actual_price = yes_price if yes_outcome is Outcome.YES else ONE - yes_price
        no_actual_price = no_price if no_outcome is Outcome.NO else ONE - no_price

        quantity = edge.quantity
        for venue, market, outcome, price in (
            (cheap_venue, cheap_market, yes_outcome, yes_actual_price),
            (dear_venue, dear_market, no_outcome, no_actual_price),
        ):
            order = probe(venue, market.market_id, outcome, price, max(quantity, _QUANTUM))
            position = self.portfolio.get(venue, market.market_id)
            direction = ONE if order.signed_quantity > ZERO else -ONE
            if position is not None and position.quantity * direction >= params.maximum_order_size:
                return CrossVenueEvaluation(pair.pair_id, "target_position_reached", **common)
            quantity = min(quantity, self.risk.remaining_order_capacity(order, position))
        quantity = quantity.quantize(_QUANTUM)
        if quantity <= ZERO:
            return CrossVenueEvaluation(pair.pair_id, "no_position_headroom", **common)
        if quantity < edge.quantity:
            # Re-walk at the capped size so VWAP, fees and limit prices are exact.
            edge = self._size(pair, cheap_venue, cheap_view, dear_venue, dear_view, quantity)
            common.update(
                {
                    "net_edge": edge.net_edge_per_contract,
                    "fees_per_contract": edge.fees_per_contract,
                    "paper_edge": edge.as_dict(),
                }
            )
            if not edge.tradeable or edge.yes_leg is None or edge.no_leg is None:
                return CrossVenueEvaluation(pair.pair_id, edge.reason, **common)
            yes_price = edge.yes_leg.limit_price or yes_price
            no_price = edge.no_leg.limit_price or no_price
            yes_actual_price = yes_price if yes_outcome is Outcome.YES else ONE - yes_price
            no_actual_price = no_price if no_outcome is Outcome.NO else ONE - no_price
            quantity = edge.quantity

        metadata = {
            "strategy": self.name,
            "pair_id": pair.pair_id,
            "match_method": pair.method,
            "raw_edge": str(raw_edge),
            "touch_gross_edge": str(edge.touch_gross_edge),
            "gross_edge_per_contract": str(edge.gross_edge_per_contract),
            "fees_per_contract": str(edge.fees_per_contract),
            "net_edge_per_contract": str(edge.net_edge_per_contract),
            "levels_walked": str(edge.levels_walked),
            "hedge_assumption": "buy_complementary_outcome_settlement_equivalent",
            "price_signal_status": "unvalidated",
        }
        orders = (
            Order(
                venue=cheap_venue,
                market_id=cheap_market.market_id,
                side=Side.BUY,
                quantity=quantity,
                outcome=yes_outcome,
                price=yes_actual_price,
                metadata={**metadata, "leg": "cheap_yes"},
            ),
            Order(
                venue=dear_venue,
                market_id=dear_market.market_id,
                side=Side.BUY,
                quantity=quantity,
                outcome=no_outcome,
                price=no_actual_price,
                metadata={**metadata, "leg": "dear_no_hedge"},
            ),
        )
        return CrossVenueEvaluation(
            pair.pair_id, "trade", quantity=quantity, orders=orders, metadata=metadata, **common
        )


class CrossVenueStrategyRunner:
    def __init__(
        self,
        strategy: CrossVenueMispricingStrategy,
        execution: ExecutionEngine,
        *,
        matcher: MarketMatcher | None = None,
    ) -> None:
        self.strategy = strategy
        self.execution = execution
        self.matcher = matcher or MarketMatcher()

    async def run(
        self,
        kalshi_client: VenueClient,
        polymarket_client: VenueClient,
        *,
        limit: int = 20,
    ) -> list[ExecutionReport]:
        kalshi_markets, poly_markets = await asyncio.gather(
            kalshi_client.list_markets(limit=limit),
            polymarket_client.list_markets(limit=limit),
        )
        clients = {Venue.KALSHI: kalshi_client, Venue.POLYMARKET: polymarket_client}
        reports: list[ExecutionReport] = []
        for pair in self.matcher.match(kalshi_markets, poly_markets):
            kalshi_book, poly_book = await asyncio.gather(
                kalshi_client.get_order_book(pair.kalshi),
                polymarket_client.get_order_book(pair.polymarket),
            )
            evaluation = self.strategy.evaluate(pair, kalshi_book, poly_book)
            self.execution.events.emit(
                "cross_venue_evaluation",
                strategy=self.strategy.name,
                pair_id=pair.pair_id,
                reason=evaluation.reason,
                raw_edge=evaluation.raw_edge,
                executable_edge=evaluation.executable_edge,
                quantity=evaluation.quantity,
            )
            for order in evaluation.orders:
                reports.append(await self.execution.submit(clients[order.venue], order))
        return reports
