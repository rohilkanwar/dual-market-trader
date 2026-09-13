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
