"""Net-YES position book with average-cost realized PnL."""

from __future__ import annotations

from decimal import Decimal

from core.types import ONE, ZERO, Fill, Outcome, Position, Side, Venue


class Portfolio:
    def __init__(self) -> None:
        self._positions: dict[tuple[Venue, str], Position] = {}

    def get(self, venue: Venue, market_id: str) -> Position | None:
        return self._positions.get((venue, market_id))

    def positions(self, *, include_flat: bool = False) -> list[Position]:
        return [
            position
            for position in self._positions.values()
            if include_flat or position.quantity != ZERO
        ]

    @property
    def realized_pnl(self) -> Decimal:
        return sum((position.realized_pnl for position in self._positions.values()), ZERO)

    @property
    def fees_paid(self) -> Decimal:
        return sum((position.fees_paid for position in self._positions.values()), ZERO)

    def apply_fill(self, fill: Fill) -> Position:
        """Fold a fill into the net YES position and return the new position.

        Fees are deducted from realized PnL as they occur so the daily-loss rail
        sees them immediately.
        """
        key = (fill.venue, fill.market_id)
        previous = self._positions.get(
            key,
            Position(venue=fill.venue, market_id=fill.market_id, quantity=ZERO),
        )
        increases_yes = (fill.side is Side.BUY) == (fill.outcome is Outcome.YES)
        delta = fill.quantity if increases_yes else -fill.quantity
        yes_equivalent_price = (
            fill.price if fill.outcome is Outcome.YES else ONE - fill.price
        )
        new_quantity = previous.quantity + delta
        realized = previous.realized_pnl

        if previous.quantity == ZERO or previous.quantity * delta > ZERO:
            gross_cost = (
                abs(previous.quantity) * previous.average_price
                + abs(delta) * yes_equivalent_price
            )
            average_price = gross_cost / abs(new_quantity) if new_quantity else ZERO
        else:
            closing_quantity = min(abs(previous.quantity), abs(delta))
            direction = Decimal("1") if previous.quantity > ZERO else Decimal("-1")
            realized += (
                closing_quantity
                * (yes_equivalent_price - previous.average_price)
                * direction
            )
            if new_quantity == ZERO:
                average_price = ZERO
            elif previous.quantity * new_quantity > ZERO:
                average_price = previous.average_price
            else:
                average_price = yes_equivalent_price

        realized -= fill.fee
        position = Position(
            venue=fill.venue,
            market_id=fill.market_id,
            quantity=new_quantity,
            average_price=average_price,
            realized_pnl=realized,
            fees_paid=previous.fees_paid + fill.fee,
        )
        self._positions[key] = position
        return position
