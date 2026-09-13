from dataclasses import replace
from decimal import Decimal

import pytest

from core.execution import ExecutionEngine, LiveTradingDisabled
from core.observability import InMemoryEventSink
from core.portfolio import Portfolio
from core.risk import RiskLimits, RiskManager, RiskViolation
from core.types import (
    ExecutionReport,
    Fill,
    Market,
    Order,
    OrderBook,
    OrderStatus,
    Position,
    Side,
    Venue,
)
from core.venue import VenueClient


class MockVenueClient(VenueClient):
    venue = Venue.KALSHI

    def __init__(self, *, paper: bool = True) -> None:
        self.paper = paper
        self.place_calls = 0

    async def list_markets(self, *, limit: int = 20) -> list[Market]:
        return []

    async def get_order_book(self, market: Market) -> OrderBook:
        return OrderBook(market_id=market.market_id)

    async def place_order(self, order: Order) -> ExecutionReport:
        self.place_calls += 1
        accepted = replace(order, order_id="mock-order", status=OrderStatus.FILLED)
        fill = Fill(
            venue=order.venue,
            market_id=order.market_id,
            order_id="mock-order",
            side=order.side,
            quantity=order.quantity,
            price=order.price or Decimal("1"),
        )
        return ExecutionReport(accepted, (fill,))

    async def cancel_order(self, order_id: str) -> None:
        del order_id

    async def get_positions(self) -> list[Position]:
        return []

    async def close(self) -> None:
        return None


def engine(*, live_enabled: bool = False) -> tuple[ExecutionEngine, InMemoryEventSink]:
    events = InMemoryEventSink()
    return (
        ExecutionEngine(
            risk=RiskManager(
                RiskLimits(
                    max_notional_per_order=Decimal("10"),
                    max_position_per_market=Decimal("20"),
                    max_daily_loss=Decimal("5"),
                )
            ),
            portfolio=Portfolio(),
            events=events,
            live_enabled=live_enabled,
        ),
        events,
    )


def make_order(quantity: str = "2") -> Order:
    return Order(
        venue=Venue.KALSHI,
        market_id="TEST",
        side=Side.BUY,
        quantity=Decimal(quantity),
        price=Decimal("0.50"),
    )


async def test_mock_fill_updates_portfolio_and_emits_events() -> None:
    execution, events = engine()
    report = await execution.submit(MockVenueClient(), make_order())

    assert report.order.status is OrderStatus.FILLED
    assert execution.portfolio.get(Venue.KALSHI, "TEST").quantity == Decimal("2")
    assert [event["event"] for event in events.events] == [
        "order_submitted",
        "fill",
        "order_acknowledged",
    ]


async def test_risk_rejection_never_reaches_venue() -> None:
    execution, events = engine()
    client = MockVenueClient()

    with pytest.raises(RiskViolation):
        await execution.submit(client, make_order("30"))

    assert client.place_calls == 0
    assert events.events[0]["event"] == "order_rejected"


async def test_live_client_requires_separate_execution_gate() -> None:
    execution, _ = engine(live_enabled=False)
    client = MockVenueClient(paper=False)

    with pytest.raises(LiveTradingDisabled):
        await execution.submit(client, make_order())

    assert client.place_calls == 0
