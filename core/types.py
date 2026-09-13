"""Venue-neutral domain types.

Prices are probabilities in decimal dollars (``0`` through ``1``). Quantities
are contract counts. Positions are net YES exposure, so buying NO decreases the
position. Venue adapters translate outcome semantics to their APIs.
"""

from __future__ import annotations
    SELL = "sell"


class Outcome(StrEnum):
    YES = "yes"
    NO = "no"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"
    market_id: str
    side: Side
    quantity: Decimal
    outcome: Outcome = Outcome.YES
    price: Decimal | None = None
    order_type: OrderType = OrderType.LIMIT
    client_order_id: str = field(default_factory=lambda: str(uuid4()))

    @property
    def signed_quantity(self) -> Decimal:
        increases_yes = (self.side is Side.BUY) == (self.outcome is Outcome.YES)
        return self.quantity if increases_yes else -self.quantity


@dataclass(frozen=True, slots=True)
    side: Side
    quantity: Decimal
    price: Decimal
    outcome: Outcome = Outcome.YES
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


