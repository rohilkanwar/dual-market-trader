"""Contract implemented by all venue adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.types import ExecutionReport, Market, Order, OrderBook, Position, Venue


class VenueClient(ABC):
    venue: Venue
    paper: bool

    @abstractmethod
    async def list_markets(self, *, limit: int = 20) -> list[Market]:
        raise NotImplementedError

    @abstractmethod
    async def get_order_book(self, market: Market) -> OrderBook:
        raise NotImplementedError

    @abstractmethod
    async def place_order(self, order: Order) -> ExecutionReport:
        raise NotImplementedError

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

    async def __aenter__(self) -> VenueClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
