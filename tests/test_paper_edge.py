from decimal import Decimal

from core.portfolio import Portfolio
from core.risk import RiskLimits, RiskManager
from core.types import Fill, Market, OrderBook, Outcome, PriceLevel, Side, Venue
from strategies.cross_venue import DepthAwareCrossVenueStrategy, DepthAwareParameters
from strategies.matching import MatchedMarketPair
from strategies.paper_edge import compute_paper_edge
from venues.paper import kalshi_fee, zero_fee

D = Decimal


def book(market_id: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        bids=tuple(PriceLevel(D(p), D(s)) for p, s in bids),
        asks=tuple(PriceLevel(D(p), D(s)) for p, s in asks),
    )


def flat_fee(quantity: Decimal, price: Decimal) -> Decimal:
    del price
    return quantity * D("0.01")


def test_walks_both_books_and_charges_each_venue_fee_on_consumed_levels() -> None:
    cheap = book("P", bids=[("0.41", "20")], asks=[("0.43", "16"), ("0.44", "440")])
    dear = book("K", bids=[("0.52", "120"), ("0.51", "200")], asks=[("0.54", "110")])

    edge = compute_paper_edge(
        cheap_yes_view=cheap, dear_yes_view=dear,
        cheap_venue=Venue.POLYMARKET, dear_venue=Venue.KALSHI,
        fee_cheap=zero_fee, fee_dear=kalshi_fee,
        max_quantity=D("25"), min_net_edge=D("0.01"),
    )

    assert edge.reason == "ok"
    assert edge.quantity == D("25")
    assert edge.levels_walked == 2
    assert edge.yes_leg is not None and edge.no_leg is not None
    assert [(l.price, l.quantity) for l in edge.yes_leg.levels] == [(D("0.43"), D("16")), (D("0.44"), D("9"))]
    assert edge.yes_leg.limit_price == D("0.44")
    # Hedge buys NO at 1 - 0.52 on the dear side; Kalshi fee rounds up per fill.
    assert [(l.price, l.quantity, l.fee) for l in edge.no_leg.levels] == [
        (D("0.48"), D("16"), D("0.28")),
        (D("0.48"), D("9"), D("0.16")),
    ]
    assert edge.touch_gross_edge == D("0.09")
    assert edge.gross_edge_per_contract == D("0.0864")  # (16*0.09 + 9*0.08) / 25
    assert edge.fees_per_contract == D("0.0176")
    assert edge.net_edge_per_contract == D("0.0688")
    assert edge.expected_paper_pnl == D("1.7200")


def test_stops_at_the_level_where_marginal_edge_no_longer_clears_fees() -> None:
    cheap = book("P", bids=[("0.40", "10")], asks=[("0.45", "5"), ("0.49", "500")])
    dear = book("K", bids=[("0.50", "500")], asks=[("0.52", "10")])

    edge = compute_paper_edge(
        cheap_yes_view=cheap, dear_yes_view=dear,
        cheap_venue=Venue.POLYMARKET, dear_venue=Venue.KALSHI,
        fee_cheap=flat_fee, fee_dear=flat_fee,
        max_quantity=D("100"), min_net_edge=D("0.005"),
    )

    # Second level: marginal 0.01 gross - 0.02 fees < 0.005, so only 5 fill.
    assert edge.reason == "ok"
    assert edge.quantity == D("5")
    assert edge.levels_walked == 1
    assert edge.net_edge_per_contract == D("0.0300")
    assert edge.depth_limited is False


def test_fees_can_erase_the_whole_edge() -> None:
    cheap = book("P", bids=[("0.40", "10")], asks=[("0.49", "50")])
    dear = book("K", bids=[("0.50", "50")], asks=[("0.52", "10")])

    edge = compute_paper_edge(
        cheap_yes_view=cheap, dear_yes_view=dear,
        cheap_venue=Venue.POLYMARKET, dear_venue=Venue.KALSHI,
        fee_cheap=zero_fee, fee_dear=kalshi_fee,
        max_quantity=D("25"), min_net_edge=D("0"),
    )

    assert edge.reason == "marginal_edge_below_fees"
    assert edge.quantity == 0
    assert edge.touch_gross_edge == D("0.01")


def test_depth_limited_and_minimum_quantity() -> None:
    cheap = book("P", bids=[("0.40", "10")], asks=[("0.43", "0.5")])
    dear = book("K", bids=[("0.52", "100")], asks=[("0.54", "10")])

    edge = compute_paper_edge(
        cheap_yes_view=cheap, dear_yes_view=dear,
        cheap_venue=Venue.POLYMARKET, dear_venue=Venue.KALSHI,
        fee_cheap=zero_fee, fee_dear=zero_fee,
        max_quantity=D("25"), min_quantity=D("1"),
    )

    assert edge.reason == "insufficient_depth"
    assert edge.quantity == D("0.5")
    assert edge.depth_limited is True


def test_missing_touch_and_no_positive_touch_edge() -> None:
    empty = OrderBook(market_id="P")
    dear = book("K", bids=[("0.52", "100")], asks=[("0.54", "10")])
    assert compute_paper_edge(
        cheap_yes_view=empty, dear_yes_view=dear, cheap_venue=Venue.POLYMARKET, dear_venue=Venue.KALSHI,
        fee_cheap=zero_fee, fee_dear=zero_fee, max_quantity=D("1"),
    ).reason == "missing_touch"

    cheap = book("P", bids=[("0.50", "10")], asks=[("0.55", "10")])
    edge = compute_paper_edge(
        cheap_yes_view=cheap, dear_yes_view=dear, cheap_venue=Venue.POLYMARKET, dear_venue=Venue.KALSHI,
        fee_cheap=zero_fee, fee_dear=zero_fee, max_quantity=D("1"),
    )
    assert edge.reason == "no_positive_touch_edge"
    assert edge.touch_gross_edge == D("-0.03")


# --------------------------------------------------------------------------
# Strategy wiring
# --------------------------------------------------------------------------
def pair(*, same_polarity: bool = True) -> MatchedMarketPair:
    return MatchedMarketPair(
        pair_id="test-pair",
        kalshi=Market(Venue.KALSHI, "K", "Test event"),
        polymarket=Market(Venue.POLYMARKET, "P", "Test event"),
        same_polarity=same_polarity,
        confidence=1.0,
        method="curated",
    )


def strategy(portfolio: Portfolio | None = None, *, max_position: str = "500") -> DepthAwareCrossVenueStrategy:
    return DepthAwareCrossVenueStrategy(
        risk=RiskManager(RiskLimits(D("100"), D(max_position), D("250"))),
        portfolio=portfolio or Portfolio(),
        fee_schedules={Venue.KALSHI: kalshi_fee, Venue.POLYMARKET: zero_fee},
        parameters=DepthAwareParameters(minimum_net_edge=D("0.01"), maximum_order_size=D("25")),
    )


def test_strategy_limits_at_worst_consumed_level_and_reports_net_edge() -> None:
    kalshi = book("K", bids=[("0.52", "120"), ("0.51", "200")], asks=[("0.54", "110")])
    poly = book("P", bids=[("0.41", "20")], asks=[("0.43", "16"), ("0.44", "440")])

    evaluation = strategy().evaluate(pair(), kalshi, poly)

    assert evaluation.reason == "trade"
    assert evaluation.cheap_venue is Venue.POLYMARKET
    assert evaluation.quantity == D("25.0000")
    assert evaluation.net_edge == D("0.0688")
    assert evaluation.fees_per_contract == D("0.0176")
    yes_leg, hedge = evaluation.orders
    assert (yes_leg.venue, yes_leg.outcome, yes_leg.price) == (Venue.POLYMARKET, Outcome.YES, D("0.44"))
    assert (hedge.venue, hedge.outcome, hedge.price) == (Venue.KALSHI, Outcome.NO, D("0.48"))
    assert evaluation.paper_edge is not None and evaluation.paper_edge["levels_walked"] == 2


def test_inverse_polarity_maps_legs_back_to_venue_outcomes() -> None:
    # Polymarket asks "Will the Fed NOT cut?"; its NO is Kalshi's YES.
    kalshi = book("K", bids=[("0.52", "100")], asks=[("0.54", "100")])
    poly = book("P", bids=[("0.55", "100")], asks=[("0.57", "100")])  # YES(not cut) 0.55/0.57 => cut 0.43/0.45

    evaluation = strategy().evaluate(pair(same_polarity=False), kalshi, poly)

    assert evaluation.reason == "trade"
    assert evaluation.cheap_venue is Venue.POLYMARKET
    yes_leg, hedge = evaluation.orders
    # Buying Kalshi-polarity YES on Polymarket means buying its NO at 1 - 0.55 = 0.45.
    assert (yes_leg.venue, yes_leg.outcome, yes_leg.price) == (Venue.POLYMARKET, Outcome.NO, D("0.45"))
    assert (hedge.venue, hedge.outcome, hedge.price) == (Venue.KALSHI, Outcome.NO, D("0.48"))


def test_position_headroom_caps_size_and_rewalks_the_books() -> None:
    portfolio = Portfolio()
    portfolio.apply_fill(
        Fill(venue=Venue.POLYMARKET, market_id="P", order_id="x", side=Side.BUY, outcome=Outcome.YES, quantity=D("20"), price=D("0.40"))
    )
    kalshi = book("K", bids=[("0.52", "120")], asks=[("0.54", "110")])
    poly = book("P", bids=[("0.41", "20")], asks=[("0.43", "16"), ("0.44", "440")])

    evaluation = strategy(portfolio, max_position="22").evaluate(pair(), kalshi, poly)

    assert evaluation.reason == "trade"
    assert evaluation.quantity == D("2.0000")
    assert evaluation.orders[0].price == D("0.43")  # only the first level is needed now
    assert evaluation.paper_edge is not None and evaluation.paper_edge["levels_walked"] == 1


def test_target_position_reached_stops_re_entry() -> None:
    portfolio = Portfolio()
    portfolio.apply_fill(
        Fill(venue=Venue.POLYMARKET, market_id="P", order_id="x", side=Side.BUY, outcome=Outcome.YES, quantity=D("25"), price=D("0.40"))
    )
    kalshi = book("K", bids=[("0.52", "120")], asks=[("0.54", "110")])
    poly = book("P", bids=[("0.41", "20")], asks=[("0.43", "16")])

    evaluation = strategy(portfolio).evaluate(pair(), kalshi, poly)

    assert evaluation.reason == "target_position_reached"
    assert not evaluation.orders
