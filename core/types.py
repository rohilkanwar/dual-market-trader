"""Venue-neutral domain types.

Prices are probabilities in decimal dollars (``0`` through ``1``). Quantities
are contract counts. Positions are net YES exposure, so buying NO decreases the
position. Venue adapters translate outcome semantics to their APIs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

ZERO = Decimal("0")
ONE = Decimal("1")


def _now() -> datetime:
    return datetime.now(UTC)


class Venue(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Outcome(StrEnum):
    YES = "yes"
    NO = "no"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Market:
    venue: Venue
    market_id: str
    title: str
    active: bool = True
    liquidity: Decimal = ZERO
    volume: Decimal = ZERO
    yes_token_id: str | None = None
    no_token_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def category(self) -> str:
        return str(self.metadata.get("category") or "").strip().lower()


@dataclass(frozen=True, slots=True)
class MarketGroup:
    """Several binary markets that together describe one multi-outcome event.

    ``exclusive`` means exactly one member resolves YES (Polymarket NegRisk
    events guarantee this through the adapter; other groups must declare it).
    ``convertible`` means a NO->YES converter exists (Polymarket's
    NegRiskAdapter). ``augmented`` means hidden placeholder outcomes exist, so
    the *visible* YES tokens are not exhaustive.
    """

    venue: Venue
    group_id: str
    title: str
    markets: tuple[Market, ...] = ()
    exclusive: bool = False
    convertible: bool = False
    augmented: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.markets)


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        if not ZERO <= self.price <= ONE:
            raise ValueError(f"price {self.price} must be between 0 and 1")
        if self.size < ZERO:
            raise ValueError(f"size {self.size} must not be negative")


@dataclass(frozen=True, slots=True)
class OrderBook:
    """YES-denominated book. ``bids`` sorted descending, ``asks`` ascending."""

    market_id: str
    bids: tuple[PriceLevel, ...] = ()
    asks: tuple[PriceLevel, ...] = ()
    timestamp: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "bids", tuple(sorted(self.bids, key=lambda l: l.price, reverse=True))
        )
        object.__setattr__(self, "asks", tuple(sorted(self.asks, key=lambda l: l.price)))

    @property
    def best_bid(self) -> PriceLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> PriceLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid_price(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid.price + self.best_ask.price) / 2

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask.price - self.best_bid.price

    def for_outcome(self, outcome: Outcome) -> OrderBook:
        """Return the book from the perspective of ``outcome``.

        A NO bid at ``p`` is a YES ask at ``1 - p`` and vice versa, so the NO view
        swaps and complements both sides.
        """
        if outcome is Outcome.YES:
            return self
        return OrderBook(
            market_id=self.market_id,
            bids=tuple(PriceLevel(ONE - level.price, level.size) for level in self.asks),
            asks=tuple(PriceLevel(ONE - level.price, level.size) for level in self.bids),
            timestamp=self.timestamp,
        )


@dataclass(frozen=True, slots=True)
class Order:
    venue: Venue
    market_id: str
    side: Side
    quantity: Decimal
    outcome: Outcome = Outcome.YES
    price: Decimal | None = None
    order_type: OrderType = OrderType.LIMIT
    client_order_id: str = field(default_factory=lambda: str(uuid4()))
    order_id: str | None = None
    status: OrderStatus = OrderStatus.NEW
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.quantity <= ZERO:
            raise ValueError(f"quantity {self.quantity} must be positive")
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("limit orders require a price")
        if self.price is not None and not ZERO < self.price < ONE:
            raise ValueError(f"price {self.price} must be strictly between 0 and 1")

    @property
    def signed_quantity(self) -> Decimal:
        increases_yes = (self.side is Side.BUY) == (self.outcome is Outcome.YES)
        return self.quantity if increases_yes else -self.quantity

    @property
    def yes_equivalent_price(self) -> Decimal | None:
        if self.price is None:
            return None
        return self.price if self.outcome is Outcome.YES else ONE - self.price

    @property
    def notional(self) -> Decimal:
        """Cash at risk: contracts times the price paid for the traded outcome."""
        return self.quantity * (self.price if self.price is not None else ONE)


@dataclass(frozen=True, slots=True)
class Fill:
    venue: Venue
    market_id: str
    order_id: str
    side: Side
    quantity: Decimal
    price: Decimal
    outcome: Outcome = Outcome.YES
    timestamp: datetime = field(default_factory=_now)
    fee: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.quantity <= ZERO:
            raise ValueError(f"fill quantity {self.quantity} must be positive")
        if not ZERO <= self.price <= ONE:
            raise ValueError(f"fill price {self.price} must be between 0 and 1")
        if self.fee < ZERO:
            raise ValueError("fee must not be negative")

    @property
    def signed_quantity(self) -> Decimal:
        increases_yes = (self.side is Side.BUY) == (self.outcome is Outcome.YES)
        return self.quantity if increases_yes else -self.quantity

    @property
    def yes_equivalent_price(self) -> Decimal:
        return self.price if self.outcome is Outcome.YES else ONE - self.price


@dataclass(frozen=True, slots=True)
class Position:
    venue: Venue
    market_id: str
    quantity: Decimal = ZERO
    average_price: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO

    @property
    def is_flat(self) -> bool:
        return self.quantity == ZERO


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    order: Order
    fills: tuple[Fill, ...] = ()

    @property
    def filled_quantity(self) -> Decimal:
        return sum((fill.quantity for fill in self.fills), ZERO)
