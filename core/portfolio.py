
from decimal import Decimal

from core.types import ONE, ZERO, Fill, Outcome, Position, Side, Venue


class Portfolio:
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

        position = Position(
            venue=fill.venue,
