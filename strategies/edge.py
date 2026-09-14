"""Single-venue fair-value strategy (the primary paper track).

Fair values come from explicit *priors* keyed by market id. There is no
implicit model: a market without a prior is never traded (``no_fair_value``).
The committed defaults only cover the fixture markets; for a network canary
supply ``--priors`` with operator-reviewed probabilities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from core.portfolio import Portfolio
from core.risk import RiskManager
from core.types import ONE, ZERO, Market, Order, OrderBook, Outcome, PriceLevel, Side, Venue
from strategies.base import Strategy


@dataclass(frozen=True, slots=True)
class FairValueParameters:
    minimum_edge: Decimal = Decimal("0.02")
    fee_buffer_per_contract: Decimal = Decimal("0.01")
    maximum_order_size: Decimal = Decimal("10")
    minimum_touch_size: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.minimum_edge < ZERO or self.fee_buffer_per_contract < ZERO:
            raise ValueError("edge thresholds must not be negative")
        if self.maximum_order_size <= ZERO or self.minimum_touch_size <= ZERO:
            raise ValueError("sizes must be positive")


DEFAULT_PRIORS: dict[str, Decimal] = {
    # Fixture-only priors. They exist so the fixture scoreboard is deterministic;
    # they are not forecasts.
    "KX-FED-SEP-CUT": Decimal("0.60"),
    "0xfixture-fed-september": Decimal("0.60"),
    "KX-CPI-AUG-OVER3": Decimal("0.40"),
    "0xfixture-cpi-august": Decimal("0.40"),
    "KX-NBA-NY-BOS-NY": Decimal("0.57"),
    "0xfixture-nba-ny-boston": Decimal("0.57"),
}

DEFAULT_VENUE_PARAMETERS: dict[Venue, FairValueParameters] = {
    Venue.KALSHI: FairValueParameters(fee_buffer_per_contract=Decimal("0.02")),
    Venue.POLYMARKET: FairValueParameters(fee_buffer_per_contract=Decimal("0.01")),
}

_QUANTUM = Decimal("0.0001")


def load_priors(path: Path) -> dict[str, Decimal]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("priors file must be a JSON object of market_id -> probability")
    priors = {str(k): Decimal(str(v)) for k, v in payload.items()}
    if any(not ZERO <= p <= ONE for p in priors.values()):
        raise ValueError("all priors must be probabilities between 0 and 1")
    return priors


@dataclass(frozen=True, slots=True)
class FairValueEvaluation:
    venue: Venue
    market_id: str
    reason: str
    fair_value: Decimal | None = None
    mid: Decimal | None = None
    side: Side | None = None
    touch: PriceLevel | None = None
    raw_edge: Decimal | None = None
    cost_adjusted_edge: Decimal | None = None
    quantity: Decimal = ZERO
    orders: tuple[Order, ...] = ()

    @property
    def traded(self) -> bool:
        return bool(self.orders)

    def as_dict(self) -> dict[str, object]:
        return {
            "venue": self.venue.value,
            "market_id": self.market_id,
            "reason": self.reason,
            "fair_value": str(self.fair_value) if self.fair_value is not None else None,
            "mid": str(self.mid) if self.mid is not None else None,
            "side": self.side.value if self.side else None,
            "touch_price": str(self.touch.price) if self.touch else None,
            "touch_size": str(self.touch.size) if self.touch else None,
            "raw_edge": str(self.raw_edge) if self.raw_edge is not None else None,
            "cost_adjusted_edge": (
                str(self.cost_adjusted_edge) if self.cost_adjusted_edge is not None else None
            ),
            "quantity": str(self.quantity),
            "orders": len(self.orders),
        }


class CalibratedFairValueStrategy(Strategy):
    name = "calibrated_fair_value"

    def __init__(
        self,
        priors: dict[str, Decimal] | None = None,
        *,
        venue_parameters: dict[Venue, FairValueParameters] | None = None,
        portfolio: Portfolio | None = None,
        risk: RiskManager | None = None,
    ) -> None:
        self.priors = DEFAULT_PRIORS if priors is None else priors
        self.venue_parameters = (
            DEFAULT_VENUE_PARAMETERS if venue_parameters is None else venue_parameters
        )
        if any(not Decimal("0") <= prior <= ONE for prior in self.priors.values()):
            raise ValueError("all priors must be probabilities between 0 and 1")
        self.portfolio = portfolio
        self.risk = risk

    def parameters_for(self, venue: Venue) -> FairValueParameters:
        return self.venue_parameters.get(venue, FairValueParameters())

    def fair_value_for(self, market: Market) -> Decimal | None:
        return self.priors.get(market.market_id)

    def evaluate(self, market: Market, book: OrderBook) -> FairValueEvaluation:
        params = self.parameters_for(market.venue)
        base = {"venue": market.venue, "market_id": market.market_id, "mid": book.mid_price}
        fair = self.fair_value_for(market)
        if fair is None:
            return FairValueEvaluation(reason="no_fair_value", **base)
        if not market.active:
            return FairValueEvaluation(reason="market_inactive", fair_value=fair, **base)
        ask, bid = book.best_ask, book.best_bid
        if ask is None and bid is None:
            return FairValueEvaluation(reason="empty_book", fair_value=fair, **base)

        buy_edge = fair - ask.price if ask is not None else None
        sell_edge = bid.price - fair if bid is not None else None
        side: Side
        touch: PriceLevel
        raw: Decimal
        if buy_edge is not None and (sell_edge is None or buy_edge >= sell_edge):
            side, touch, raw = Side.BUY, ask, buy_edge  # type: ignore[assignment]
        else:
            side, touch, raw = Side.SELL, bid, sell_edge  # type: ignore[assignment]
        common = {**base, "fair_value": fair, "side": side, "touch": touch, "raw_edge": raw}
        if raw <= ZERO:
            return FairValueEvaluation(reason="no_edge", **common)
        cost_adjusted = raw - params.fee_buffer_per_contract
        common["cost_adjusted_edge"] = cost_adjusted
        if cost_adjusted <= params.minimum_edge:
            return FairValueEvaluation(reason="below_edge_threshold", **common)
        if not ZERO < touch.price < ONE:
            return FairValueEvaluation(reason="touch_at_bound", **common)
        if touch.size < params.minimum_touch_size:
            return FairValueEvaluation(reason="insufficient_touch_depth", **common)

        direction = ONE if side is Side.BUY else -ONE
        position = self.portfolio.get(market.venue, market.market_id) if self.portfolio else None
        if position is not None and position.quantity * direction >= params.maximum_order_size:
            return FairValueEvaluation(reason="target_position_reached", **common)

        quantity = min(touch.size, params.maximum_order_size)
        if position is not None and position.quantity * direction > ZERO:
            quantity = min(quantity, params.maximum_order_size - position.quantity * direction)
        probe = Order(
            venue=market.venue,
            market_id=market.market_id,
            side=side,
            quantity=max(quantity, _QUANTUM),
            outcome=Outcome.YES,
            price=touch.price,
        )
        if self.risk is not None:
            quantity = min(quantity, self.risk.remaining_order_capacity(probe, position))
        quantity = quantity.quantize(_QUANTUM)
        if quantity <= ZERO:
            return FairValueEvaluation(reason="no_position_headroom", **common)

        order = Order(
            venue=market.venue,
            market_id=market.market_id,
            side=side,
            quantity=quantity,
            outcome=Outcome.YES,
            price=touch.price,
            metadata={
                "strategy": self.name,
                "fair_value": str(fair),
                "mid": str(book.mid_price) if book.mid_price is not None else "",
                "raw_edge": str(raw),
                "cost_adjusted_edge": str(cost_adjusted),
                "fee_buffer_per_contract": str(params.fee_buffer_per_contract),
                "price_signal_status": "explicit_prior",
            },
        )
        return FairValueEvaluation(reason="trade", quantity=quantity, orders=(order,), **common)

    async def propose(self, market: Market, book: OrderBook) -> list[Order]:
        return list(self.evaluate(market, book).orders)
