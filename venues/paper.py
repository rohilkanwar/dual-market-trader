"""Deterministic paper execution shared by every venue adapter.

Paper fills walk the *current* YES book (fixture or public snapshot) and never
touch a venue order endpoint. Given the same book and order the result is
identical, which is what makes the scoreboard reproducible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import ROUND_UP, Decimal
from uuid import uuid4

from core.portfolio import Portfolio
from core.types import (
    ONE,
    ZERO,
    ExecutionReport,
    Fill,
    Market,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Outcome,
    Position,
    Side,
)

FeeSchedule = Callable[[Decimal, Decimal], Decimal]
CENT = Decimal("0.01")


def kalshi_fee(quantity: Decimal, price: Decimal) -> Decimal:
    """Kalshi's published general fee: ``ceil(0.07 * C * P * (1 - P))`` per order.

    Approximation only: the live schedule has market-specific rates and maker
    rebates. Documented as an assumption in ``docs/ASSUMPTIONS.md``.
    """
    raw = Decimal("0.07") * quantity * price * (ONE - price)
    return raw.quantize(CENT, rounding=ROUND_UP) if raw > ZERO else ZERO


def zero_fee(quantity: Decimal, price: Decimal) -> Decimal:
    del quantity, price
    return ZERO


class PaperExecutionMixin:
    """Provides ``place_order`` / ``get_positions`` for ``paper=True`` clients.

    Host classes must set ``venue``, ``paper``, maintain ``_market_cache`` and
    implement ``list_markets`` / ``get_order_book``.
    """

    fee_schedule: FeeSchedule = staticmethod(zero_fee)  # type: ignore[assignment]

    def _init_paper(self, fee_schedule: FeeSchedule | None = None) -> None:
        self._market_cache: dict[str, Market] = {}
        self._paper_portfolio = Portfolio()
        self._paper_fills: list[Fill] = []
        if fee_schedule is not None:
            self.fee_schedule = fee_schedule  # type: ignore[assignment]

    def _fee_schedule_for(self, market: Market) -> FeeSchedule:
        """Hook for per-market fee schedules; defaults to the venue-wide one."""
        del market
        return self.fee_schedule

    async def _lookup_market(self, market_id: str) -> Market | None:
        market = self._market_cache.get(market_id)
        if market is None:
            for candidate in await self.list_markets(limit=200):  # type: ignore[attr-defined]
                if candidate.market_id == market_id:
                    market = candidate
                    break
        return market

    async def place_order(self, order: Order) -> ExecutionReport:
        if not getattr(self, "paper", False):
            raise PermissionError(
                f"{type(self).__name__} live order routing is intentionally not "
                "implemented; this build is paper-only"
            )
        market = await self._lookup_market(order.market_id)
        if market is None or not market.active:
            raise ValueError(f"market {order.market_id} is unavailable")
        book = await self.get_order_book(market)  # type: ignore[attr-defined]
        return await self.place_order_with_book(order, market, book)

    async def place_order_with_book(
        self,
        order: Order,
        market: Market,
        book: OrderBook,
    ) -> ExecutionReport:
        """Simulate an order against a supplied fixture or public book snapshot."""
        if market.market_id != order.market_id or not market.active:
            raise ValueError(f"market {order.market_id} is unavailable")
        view = book if order.outcome is Outcome.YES else book.for_outcome(Outcome.NO)

        # Buying lifts asks; selling hits bids, always in the traded outcome's view.
        resting = view.asks if order.side is Side.BUY else view.bids
        remaining = order.quantity
        fills: list[Fill] = []
        order_id = f"paper-{uuid4().hex[:12]}"
        fee_schedule = self._fee_schedule_for(market)
        for level in resting:
            if remaining <= ZERO:
                break
            if order.order_type is OrderType.LIMIT and order.price is not None:
                crosses = (
                    level.price <= order.price
                    if order.side is Side.BUY
                    else level.price >= order.price
                )
                if not crosses:
                    break
            take = min(remaining, level.size)
            if take <= ZERO:
                continue
            fills.append(
                Fill(
                    venue=order.venue,
                    market_id=order.market_id,
                    order_id=order_id,
                    side=order.side,
                    outcome=order.outcome,
                    quantity=take,
                    price=level.price,
                    fee=fee_schedule(take, level.price),
                )
            )
            remaining -= take

        filled = order.quantity - remaining
        if filled == ZERO:
            status = OrderStatus.ACCEPTED  # rests unfilled; paper books never move
        elif remaining > ZERO:
            status = OrderStatus.PARTIALLY_FILLED
        else:
            status = OrderStatus.FILLED
        accepted = replace(order, order_id=order_id, status=status)
        for fill in fills:
            self._paper_portfolio.apply_fill(fill)
            self._paper_fills.append(fill)
        return ExecutionReport(accepted, tuple(fills))

    async def cancel_order(self, order_id: str) -> None:
        del order_id  # paper orders never rest on a venue

    async def get_positions(self) -> list[Position]:
        return self._paper_portfolio.positions()
