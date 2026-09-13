from decimal import Decimal

import pytest

from core.risk import RiskLimits, RiskManager, RiskViolation
from core.types import Order, Position, Side, Venue


LIMITS = RiskLimits(
    max_notional_per_order=Decimal("100"),
    max_position_per_market=Decimal("50"),
    max_daily_loss=Decimal("25"),
)


def order(*, quantity: str = "10", price: str = "0.40", side: Side = Side.BUY) -> Order:
    return Order(
        venue=Venue.KALSHI,
        market_id="TEST",
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
    )


def test_rejects_oversized_notional() -> None:
    with pytest.raises(RiskViolation, match="order notional"):
        RiskManager(LIMITS).validate_order(order(quantity="250", price="0.50"))


def test_rejects_projected_position() -> None:
    position = Position(
        venue=Venue.KALSHI,
        market_id="TEST",
        quantity=Decimal("45"),
    )
    with pytest.raises(RiskViolation, match="projected position"):
        RiskManager(LIMITS).validate_order(order(quantity="6"), position)


def test_reducing_position_is_allowed() -> None:
    position = Position(
        venue=Venue.KALSHI,
        market_id="TEST",
        quantity=Decimal("45"),
    )
    RiskManager(LIMITS).validate_order(order(quantity="10", side=Side.SELL), position)


def test_daily_loss_and_kill_switch_are_hard_stops() -> None:
    manager = RiskManager(LIMITS)
    manager.record_realized_pnl(Decimal("-25"))
    with pytest.raises(RiskViolation, match="daily loss"):
        manager.validate_order(order())

    manager = RiskManager(LIMITS, kill_switch=True)
    with pytest.raises(RiskViolation, match="kill switch"):
        manager.validate_order(order())


@pytest.mark.parametrize(
    ("quantity", "price", "message"),
    [
        ("0", "0.5", "quantity"),
        ("1", "0", "price"),
        ("1", "1", "price"),
    ],
)
def test_order_validation(quantity: str, price: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        order(quantity=quantity, price=price)
