"""News / underreaction measurement and hypothetical paper trade.

Literature anchor: arXiv:2606.07811 ("When Do Markets Fully Process Public
Information? Evidence from Real-Time Prediction Markets") reports that a
one-minute change in a benchmark probability is matched by only ~0.64-for-one
contemporaneous price change, and that the missing adjustment predicts drift
over the following several minutes (more so in illiquid markets).

Measurement (all prices are YES probabilities)::

    p0     pre-signal mid          (fixture/operator supplied; UNKNOWN on network)
    p1     current mid             (from the frozen snapshot book)
    p_fair signal-implied prob     (fixture/operator supplied; the unvalidated mapping)

    full_move           = p_fair - p0
    observed_move       = p1 - p0
    reaction_ratio      = observed_move / full_move          (paper: ~0.64)
    residual_to_fair    = p_fair - p1                        (what full convergence would capture)
    literature_residual = (1 - literature_beta) * full_move  (what the paper's average implies)

The hypothetical trade targets ``p1 + convergence_fraction * residual_to_fair``
and is built by the same fair-value engine as the primary track, so it inherits
its touch/fee/size/risk logic and adds nothing of its own. Nothing here claims
the mapping ``signal -> p_fair`` is right; that is the lane's open question.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from core.portfolio import Portfolio
from core.risk import RiskManager
from core.types import ONE, ZERO, Market, Order, OrderBook, Venue
from research.news_signals import NewsSignal
from strategies.edge import (
    DEFAULT_VENUE_PARAMETERS,
    CalibratedFairValueStrategy,
    FairValueEvaluation,
    FairValueParameters,
)

LITERATURE_REFERENCE = "arXiv:2606.07811"
_Q = Decimal("0.0001")


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(_Q) if value is not None else None


@dataclass(frozen=True, slots=True)
class UnderreactionParameters:
    literature_beta: Decimal = Decimal("0.64")
    max_signal_age_seconds: Decimal = Decimal("900")
    minimum_confidence: Decimal = Decimal("0.5")
    minimum_residual: Decimal = Decimal("0.03")
    convergence_fraction: Decimal = ONE
    maximum_order_size: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        if not ZERO < self.literature_beta <= ONE:
            raise ValueError("literature_beta must be within (0, 1]")
        if self.max_signal_age_seconds <= ZERO:
            raise ValueError("max_signal_age_seconds must be positive")
        if not ZERO <= self.minimum_confidence <= ONE:
            raise ValueError("minimum_confidence must be within [0, 1]")
        if self.minimum_residual < ZERO:
            raise ValueError("minimum_residual must not be negative")
        if not ZERO < self.convergence_fraction <= ONE:
            raise ValueError("convergence_fraction must be within (0, 1]")
        if self.maximum_order_size <= ZERO:
            raise ValueError("maximum_order_size must be positive")

    def as_dict(self) -> dict[str, Any]:
        return {
            "literature_beta": self.literature_beta,
            "max_signal_age_seconds": self.max_signal_age_seconds,
            "minimum_confidence": self.minimum_confidence,
            "minimum_residual": self.minimum_residual,
            "convergence_fraction": self.convergence_fraction,
            "maximum_order_size": self.maximum_order_size,
        }


@dataclass(frozen=True, slots=True)
class UnderreactionMeasurement:
    signal_id: str
    venue: Venue
    market_id: str
    reason: str
    age_seconds: Decimal
    confidence: Decimal
    mapping: str
    implied_probability: Decimal | None = None
    pre_signal_mid: Decimal | None = None
    current_mid: Decimal | None = None
    full_move: Decimal | None = None
    observed_move: Decimal | None = None
    reaction_ratio: Decimal | None = None
    residual_to_fair: Decimal | None = None
    literature_residual: Decimal | None = None
    target_price: Decimal | None = None

    @property
    def measurable(self) -> bool:
        return self.reason == "measured"

    @property
    def direction(self) -> str | None:
        if self.residual_to_fair is None or self.residual_to_fair == ZERO:
            return None
        return "yes" if self.residual_to_fair > ZERO else "no"

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "venue": self.venue.value,
            "market_id": self.market_id,
            "reason": self.reason,
            "age_seconds": self.age_seconds,
            "confidence": self.confidence,
            "mapping": self.mapping,
            "implied_probability": self.implied_probability,
            "pre_signal_mid": self.pre_signal_mid,
            "current_mid": _q(self.current_mid),
            "full_move": _q(self.full_move),
            "observed_move": _q(self.observed_move),
            "reaction_ratio": _q(self.reaction_ratio),
            "residual_to_fair": _q(self.residual_to_fair),
            "literature_residual": _q(self.literature_residual),
            "target_price": _q(self.target_price),
            "direction": self.direction,
        }


def _current_mid(book: OrderBook) -> Decimal | None:
    if book.mid_price is not None:
        return book.mid_price
    if book.best_bid is not None:
        return book.best_bid.price
    if book.best_ask is not None:
        return book.best_ask.price
    return None


def measure_underreaction(
    signal: NewsSignal,
    market: Market | None,
    book: OrderBook,
    *,
    as_of: datetime,
    parameters: UnderreactionParameters | None = None,
) -> UnderreactionMeasurement:
    """Pure measurement. Computes every ratio the inputs allow, then reports the
    first rail that blocks a trade (``reason``), or ``measured``."""
    params = parameters or UnderreactionParameters()
    age = signal.age_seconds(as_of)
    base: dict[str, Any] = {
        "signal_id": signal.signal_id,
        "venue": signal.venue,
        "market_id": signal.market_id,
        "age_seconds": age,
        "confidence": signal.confidence,
        "mapping": signal.mapping,
        "implied_probability": signal.implied_probability,
        "pre_signal_mid": signal.pre_signal_mid,
    }
    if market is None or market.market_id != signal.market_id or market.venue is not signal.venue:
        return UnderreactionMeasurement(reason="market_not_in_snapshot", **base)
    if not market.active:
        return UnderreactionMeasurement(reason="market_inactive", **base)

    p1 = _current_mid(book)
    base["current_mid"] = p1
    p_fair, p0 = signal.implied_probability, signal.pre_signal_mid
    if p_fair is not None and p0 is not None:
        base["full_move"] = p_fair - p0
        base["literature_residual"] = (ONE - params.literature_beta) * (p_fair - p0)
        if p1 is not None:
            base["observed_move"] = p1 - p0
            if base["full_move"] != ZERO:
                base["reaction_ratio"] = base["observed_move"] / base["full_move"]
    if p_fair is not None and p1 is not None:
        base["residual_to_fair"] = p_fair - p1
        base["target_price"] = p1 + params.convergence_fraction * (p_fair - p1)

    # The mapping is the fundamental blocker: without p_fair nothing else matters.
    if p_fair is None:
        return UnderreactionMeasurement(reason="no_implied_probability", **base)
    if age < ZERO:
        return UnderreactionMeasurement(reason="signal_in_future", **base)
    if age > params.max_signal_age_seconds:
        return UnderreactionMeasurement(reason="signal_stale", **base)
    if signal.confidence < params.minimum_confidence:
        return UnderreactionMeasurement(reason="low_confidence", **base)
    if p1 is None:
        return UnderreactionMeasurement(reason="empty_book", **base)
    return UnderreactionMeasurement(reason="measured", **base)


@dataclass(frozen=True, slots=True)
class UnderreactionEvaluation:
    measurement: UnderreactionMeasurement
    reason: str
    fair_value: FairValueEvaluation | None = None
    orders: tuple[Order, ...] = ()

    @property
    def traded(self) -> bool:
        return bool(self.orders)

    @property
    def cost_adjusted_edge(self) -> Decimal | None:
        return self.fair_value.cost_adjusted_edge if self.fair_value else None

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.measurement.as_dict(),
            "reason": self.reason,
            "side": self.fair_value.side.value if self.fair_value and self.fair_value.side else None,
            "touch_price": _q(self.fair_value.touch.price) if self.fair_value and self.fair_value.touch else None,
            "raw_edge": _q(self.fair_value.raw_edge) if self.fair_value else None,
            "cost_adjusted_edge": _q(self.cost_adjusted_edge),
            "quantity": self.fair_value.quantity if self.fair_value else ZERO,
            "orders": len(self.orders),
        }


class NewsUnderreactionStrategy:
    """Signal -> measurement -> (optional) hypothetical paper order.

    Order construction is delegated to :class:`CalibratedFairValueStrategy`
    with a one-market prior equal to the measurement's ``target_price``. That
    keeps the touch, fee-buffer, target-position and risk-capacity behaviour
    identical to the primary track.
    """

    name = "news_underreaction"

    def __init__(
        self,
        *,
        parameters: UnderreactionParameters | None = None,
        venue_parameters: dict[Venue, FairValueParameters] | None = None,
        portfolio: Portfolio | None = None,
        risk: RiskManager | None = None,
    ) -> None:
        self.parameters = parameters or UnderreactionParameters()
        base = venue_parameters or DEFAULT_VENUE_PARAMETERS
        self.venue_parameters = {
            venue: replace(
                params,
                minimum_edge=self.parameters.minimum_residual,
                maximum_order_size=self.parameters.maximum_order_size,
            )
            for venue, params in base.items()
        }
        self.portfolio = portfolio
        self.risk = risk

    def parameters_for(self, venue: Venue) -> FairValueParameters:
        return self.venue_parameters.get(venue, FairValueParameters())

    def measure(
        self, signal: NewsSignal, market: Market | None, book: OrderBook, *, as_of: datetime
    ) -> UnderreactionMeasurement:
        return measure_underreaction(signal, market, book, as_of=as_of, parameters=self.parameters)

    def evaluate(
        self, signal: NewsSignal, market: Market | None, book: OrderBook, *, as_of: datetime
    ) -> UnderreactionEvaluation:
        measurement = self.measure(signal, market, book, as_of=as_of)
        if not measurement.measurable or market is None or measurement.target_price is None:
            return UnderreactionEvaluation(measurement, measurement.reason)
        if abs(measurement.residual_to_fair or ZERO) < self.parameters.minimum_residual:
            return UnderreactionEvaluation(measurement, "below_residual_threshold")

        engine = CalibratedFairValueStrategy(
            {market.market_id: measurement.target_price},
            venue_parameters=self.venue_parameters,
            portfolio=self.portfolio,
            risk=self.risk,
        )
        fair = engine.evaluate(market, book)
        orders = tuple(
            replace(
                order,
                metadata={
                    **order.metadata,
                    "strategy": self.name,
                    "price_signal_status": "news_signal_implied_probability",
                    "signal_id": signal.signal_id,
                    "signal_source": signal.source,
                    "signal_mapping": signal.mapping,
                    "implied_probability": str(measurement.implied_probability),
                    "pre_signal_mid": str(measurement.pre_signal_mid),
                    "reaction_ratio": str(_q(measurement.reaction_ratio)),
                    "residual_to_fair": str(_q(measurement.residual_to_fair)),
                    "literature_residual": str(_q(measurement.literature_residual)),
                    "literature_reference": LITERATURE_REFERENCE,
                },
            )
            for order in fair.orders
        )
        return UnderreactionEvaluation(measurement, fair.reason, fair, orders)
