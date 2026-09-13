    try:
        grouped = await asyncio.gather(*(client.list_markets() for client in clients))
        books = await asyncio.gather(
            *(
                client.get_order_book(markets[0])
                for client, markets in zip(clients, grouped, strict=True)
            )
        )
        assert {market.venue for markets in grouped for market in markets} == {
            Venue.KALSHI,
