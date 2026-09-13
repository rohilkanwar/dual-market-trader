from decimal import Decimal

from core.portfolio import Portfolio
from core.risk import RiskLimits, RiskManager
from core.types import Fill, Market, OrderBook, Outcome, PriceLevel, Side, Venue
from strategies.cross_venue import (
    CrossVenueMispricingStrategy,
    CrossVenueParameters,
    outcome_touch,
)
from strategies.matching import MatchedMarketPair


def book(market_id: str, bid: str, bid_size: str, ask: str, ask_size: str) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        bids=(PriceLevel(Decimal(bid), Decimal(bid_size)),),
        asks=(PriceLevel(Decimal(ask), Decimal(ask_size)),),
    )


def pair(*, same_polarity: bool = True) -> MatchedMarketPair:
    return MatchedMarketPair(
        pair_id="test-pair",
        kalshi=Market(Venue.KALSHI, "K", "Test event"),
        polymarket=Market(Venue.POLYMARKET, "P", "Test event"),
        same_polarity=same_polarity,
        confidence=1.0,
        method="curated",
    )


def strategy(
    portfolio: Portfolio | None = None,
    *,
    max_position: str = "100",
) -> CrossVenueMispricingStrategy:
    return CrossVenueMispricingStrategy(
        risk=RiskManager(
            RiskLimits(
                max_notional_per_order=Decimal("100"),
                max_position_per_market=Decimal(max_position),
                max_daily_loss=Decimal("100"),
            )
        ),
        portfolio=portfolio or Portfolio(),
        parameters=CrossVenueParameters(
            minimum_mid_edge=Decimal("0.04"),
            minimum_touch_size=Decimal("1"),
            maximum_order_size=Decimal("25"),
            fee_buffer_per_contract=Decimal("0.01"),
        ),
    )


def test_outcome_touch_normalizes_inverse_polarity() -> None:
    no_touch = outcome_touch(book("P", "0.69", "8", "0.71", "9"), Outcome.NO)

    assert no_touch is not None
    assert no_touch.bid == Decimal("0.29")
    assert no_touch.ask == Decimal("0.31")
    assert no_touch.mid == Decimal("0.30")


def test_edge_detection_fires_and_sizes_to_minimum_touch_depth() -> None:
    evaluation = strategy().evaluate(
        pair(),
        book("K", "0.55", "6", "0.57", "20"),
        book("P", "0.47", "30", "0.49", "7"),
    )

    assert evaluation.reason == "trade"
    assert evaluation.raw_edge == Decimal("0.08")
    assert evaluation.executable_edge == Decimal("0.06")
    assert evaluation.quantity == Decimal("6.0000")
    assert len(evaluation.orders) == 2
    assert evaluation.orders[0].venue is Venue.POLYMARKET
    assert evaluation.orders[0].outcome is Outcome.YES
    assert evaluation.orders[1].venue is Venue.KALSHI
    assert evaluation.orders[1].outcome is Outcome.NO


def test_edge_equal_to_threshold_does_not_fire() -> None:
    evaluation = strategy().evaluate(
        pair(),
        book("K", "0.53", "10", "0.55", "10"),
        book("P", "0.49", "10", "0.51", "10"),
    )

    assert evaluation.raw_edge == Decimal("0.04")
    assert evaluation.reason == "below_mid_edge_threshold"
    assert not evaluation.orders


def test_sizing_respects_remaining_position_headroom() -> None:
    portfolio = Portfolio()
    portfolio.apply_fill(
        Fill(
            venue=Venue.POLYMARKET,
            market_id="P",
            order_id="existing",
            side=Side.BUY,
            outcome=Outcome.YES,
            quantity=Decimal("4"),
            price=Decimal("0.40"),
        )
    )

    evaluation = strategy(portfolio, max_position="5").evaluate(
        pair(),
        book("K", "0.55", "20", "0.57", "20"),
        book("P", "0.47", "20", "0.49", "20"),
    )

    assert evaluation.reason == "trade"
    assert evaluation.quantity == Decimal("1.0000")
