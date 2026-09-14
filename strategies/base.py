"""Strategy contract and the single-venue runner."""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.execution import ExecutionEngine
from core.types import ExecutionReport, Market, Order, OrderBook
from core.venue import VenueClient


class Strategy(ABC):
    name: str = "strategy"

    @abstractmethod
    async def propose(self, market: Market, book: OrderBook) -> list[Order]:
        raise NotImplementedError


class StrategyRunner:
    def __init__(self, strategy: Strategy, execution: ExecutionEngine) -> None:
        self.strategy = strategy
        self.execution = execution

    async def run(self, client: VenueClient, *, limit: int = 20) -> list[ExecutionReport]:
        reports: list[ExecutionReport] = []
        markets = await client.list_markets(limit=limit)
        for market in markets:
            book = await client.get_order_book(market)
            self.execution.events.emit(
                "market_snapshot",
                venue=market.venue,
                market_id=market.market_id,
                mid_price=book.mid_price,
                spread=book.spread,
            )
            orders = await self.strategy.propose(market, book)
            self.execution.events.emit(
                "strategy_evaluation",
                strategy=self.strategy.name,
                venue=market.venue,
                market_id=market.market_id,
                proposed_orders=orders,
            )
            for order in orders:
                reports.append(await self.execution.submit(client, order))
        return reports
