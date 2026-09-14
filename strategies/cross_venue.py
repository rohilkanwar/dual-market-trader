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

    @property
    def traded(self) -> bool:
        return bool(self.orders)

    def as_dict(self) -> dict[str, object]:
        return {
            "pair_id": self.pair_id,
            "reason": self.reason,
            "kalshi_mid": str(self.kalshi_mid) if self.kalshi_mid is not None else None,
            "polymarket_mid": str(self.polymarket_mid) if self.polymarket_mid is not None else None,
            "raw_edge": str(self.raw_edge) if self.raw_edge is not None else None,
            "executable_edge": (
                str(self.executable_edge) if self.executable_edge is not None else None
            ),
            "cheap_venue": self.cheap_venue.value if self.cheap_venue else None,
            "quantity": str(self.quantity),
            "orders": len(self.orders),
        }


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
