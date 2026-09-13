    assert pair.method == "heuristic"
    assert pair.same_polarity
    assert same_title_polarity(kalshi.title, polymarket.title)


def test_heuristic_can_use_slug_and_event_context() -> None:
    kalshi = Market(
        venue=Venue.KALSHI,
        market_id="K-RATE",
        title="Contract A",
        metadata={"event_title": "Federal Reserve September rate decision"},
    )
    polymarket = Market(
        venue=Venue.POLYMARKET,
        market_id="P-RATE",
        title="Market B",
        metadata={"slug": "fed-september-rate-decision"},
    )

    pair = MarketMatcher(curated_pairs=()).match([kalshi], [polymarket])[0]

    assert pair.method == "heuristic"
