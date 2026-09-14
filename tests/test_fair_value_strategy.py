from decimal import Decimal

import pytest

from core.portfolio import Portfolio
from core.risk import RiskLimits, RiskManager
from core.types import Fill, Market, OrderBook, Outcome, PriceLevel, Side, Venue
from strategies.edge import CalibratedFairValueStrategy, FairValueParameters


def book(bid: str, bid_size: str, ask: str, ask_size: str) -> OrderBook:
    return OrderBook(
        market_id="M",
        bids=(PriceLevel(Decimal(bid), Decimal(bid_size)),),
        asks=(PriceLevel(Decimal(ask), Decimal(ask_size)),),
    )


MARKET = Market(Venue.KALSHI, "M", "Test market")
PARAMS = {Venue.KALSHI: FairValueParameters(minimum_edge=Decimal("0.02"), fee_buffer_per_contract=Decimal("0.01"))}


async def test_buys_at_ask_when_fair_value_exceeds_it() -> None:
    strategy = CalibratedFairValueStrategy({"M": Decimal("0.65")}, venue_parameters=PARAMS)

    orders = await strategy.propose(MARKET, book("0.49", "10", "0.51", "30"))

    assert len(orders) == 1
    assert orders[0].side is Side.BUY
    assert orders[0].outcome is Outcome.YES
    assert orders[0].price == Decimal("0.51")
    assert orders[0].quantity == Decimal("10")  # capped at maximum_order_size
    assert Decimal(orders[0].metadata["fair_value"]) == Decimal("0.65")


async def test_cost_adjusted_edge_at_threshold_does_not_trade() -> None:
    # raw edge 0.03 - fee buffer 0.01 == minimum edge 0.02 -> not strictly greater.
    strategy = CalibratedFairValueStrategy({"M": Decimal("0.54")}, venue_parameters=PARAMS)
    evaluation = strategy.evaluate(MARKET, book("0.49", "10", "0.51", "30"))

    assert evaluation.reason == "below_edge_threshold"
    assert not evaluation.orders


async def test_sells_at_bid_when_fair_value_below_it() -> None:
    strategy = CalibratedFairValueStrategy({"M": Decimal("0.40")}, venue_parameters=PARAMS)
    evaluation = strategy.evaluate(MARKET, book("0.49", "4", "0.51", "30"))

    assert evaluation.reason == "trade"
    assert evaluation.side is Side.SELL
    assert evaluation.orders[0].price == Decimal("0.49")
    assert evaluation.quantity == Decimal("4")  # limited by touch depth


async def test_market_without_prior_is_never_traded() -> None:
    strategy = CalibratedFairValueStrategy({}, venue_parameters=PARAMS)
    evaluation = strategy.evaluate(MARKET, book("0.10", "10", "0.12", "10"))
    assert evaluation.reason == "no_fair_value"


async def test_target_position_stops_re_entry_and_risk_caps_size() -> None:
    portfolio = Portfolio()
    portfolio.apply_fill(
        Fill(venue=Venue.KALSHI, market_id="M", order_id="x", side=Side.BUY, quantity=Decimal("10"), price=Decimal("0.5"))
    )
    strategy = CalibratedFairValueStrategy(
        {"M": Decimal("0.65")},
        venue_parameters=PARAMS,
        portfolio=portfolio,
        risk=RiskManager(RiskLimits(Decimal("100"), Decimal("12"), Decimal("50"))),
    )
    assert strategy.evaluate(MARKET, book("0.49", "10", "0.51", "30")).reason == "target_position_reached"

    portfolio = Portfolio()
    portfolio.apply_fill(
        Fill(venue=Venue.KALSHI, market_id="M", order_id="x", side=Side.BUY, quantity=Decimal("9"), price=Decimal("0.5"))
    )
    strategy = CalibratedFairValueStrategy(
        {"M": Decimal("0.65")},
        venue_parameters=PARAMS,
        portfolio=portfolio,
        risk=RiskManager(RiskLimits(Decimal("100"), Decimal("12"), Decimal("50"))),
    )
    evaluation = strategy.evaluate(MARKET, book("0.49", "10", "0.51", "30"))
    assert evaluation.reason == "trade"
    assert evaluation.quantity == Decimal("1")  # 10 target - 9 held, within the 12 cap


def test_invalid_prior_rejected() -> None:
    with pytest.raises(ValueError, match="probabilities"):
        CalibratedFairValueStrategy({"M": Decimal("1.2")})
