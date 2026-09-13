        )
        if market is None or not market.active:
            raise ValueError(f"market {order.market_id} is unavailable")
        book = await self.get_order_book(market)
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
        if order.outcome is Outcome.YES:
            best_bid, best_ask = book.best_bid, book.best_ask
        else:
