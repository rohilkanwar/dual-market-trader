from decimal import Decimal

import pytest

from core.ledger import PaperLedger
from core.types import Market, Order, OrderBook, Outcome, PriceLevel, Side, Venue
from research.flb import (
    BAND_ORDER,
    FLB_TRACKS,
    FlbSnapshotClient,
    KalshiFeeModel,
    band_for,
    snapshot_band_table,
    snapshot_event_overround,
    snapshot_verdicts,
)
from research.scoreboard import TRACKS, VenueSnapshot, measure_all_with_ledgers, run_flb_tracks
from strategies.flb import (
    FLB_RISK_LIMITS,
    FlbParameters,
    LongshotFadeStrategy,
    MakerQuoteStrategy,
    identify_longshot,
    longshot_buy_order,
    position_cash_at_risk,
)
from apps.measure_flb import FLB_FIXTURE_PATH
from venues.fixtures import load_fixture

D = Decimal


def _market(market_id: str = "KX-TEST", **meta: object) -> Market:
    return Market(venue=Venue.KALSHI, market_id=market_id, title=market_id, metadata=dict(meta))


def _book(bid: tuple[str, str] | None, ask: tuple[str, str] | None, market_id: str = "KX-TEST") -> OrderBook:
    return OrderBook(
        market_id=market_id,
        bids=(PriceLevel(D(bid[0]), D(bid[1])),) if bid else (),
        asks=(PriceLevel(D(ask[0]), D(ask[1])),) if ask else (),
    )


def _flb_snapshot() -> VenueSnapshot:
    markets, books = load_fixture(FLB_FIXTURE_PATH, Venue.KALSHI)
    return VenueSnapshot(venue=Venue.KALSHI, source="fixture", markets=markets, books=books)


# ----------------------------------------------------------------- bands / fees
def test_band_boundaries_are_lower_inclusive_upper_exclusive() -> None:
    assert band_for(D("0")) == "<10c"
    assert band_for(D("0.0999")) == "<10c"
    assert band_for(D("0.10")) == "10-20c"
    assert band_for(D("0.1999")) == "10-20c"
    assert band_for(D("0.20")) == "20-30c"
    assert band_for(D("0.89")) == "80-90c"
    assert band_for(D("0.90")) == ">=90c"
    assert band_for(D("1")) == ">=90c"
    assert len(BAND_ORDER) == 10
    with pytest.raises(ValueError):
        band_for(D("1.01"))


def test_fee_model_matches_kalshi_schedule_and_series_metadata() -> None:
    model = KalshiFeeModel()
    plain = _market(fee_type="quadratic_with_maker_fees", fee_multiplier=1)
    # 0.07 * 10 * 0.05 * 0.95 = 0.03325 -> centicent round-up 0.0333
    assert model.fee(D("10"), D("0.05"), maker=False, market=plain) == D("0.0333")
    # maker 0.0175 * 10 * 0.05 * 0.95 = 0.0083125 -> 0.0084
    assert model.fee(D("10"), D("0.05"), maker=True, market=plain) == D("0.0084")
    half = _market(fee_type="quadratic_with_maker_fees", fee_multiplier=0.5)
    assert model.rate_for(half, maker=False) == D("0.035")
    assert model.rate_for(half, maker=True) == D("0.00875")
    no_maker = _market(fee_type="quadratic", fee_multiplier=1)
    assert model.rate_for(no_maker, maker=True) == D("0")
    assert model.rate_for(no_maker, maker=False) == D("0.07")
    # Unknown metadata: conservative (maker fees assumed), multiplier 1.
    assert model.rate_for(_market(), maker=True) == D("0.0175")
    assert model.rate_for(None, maker=False) == D("0.07")
    assert KalshiFeeModel.zero().fee(D("100"), D("0.5"), maker=False) == D("0")
    # Relative cost of the taker fee is 0.07 * (1 - p) of the price paid: largest for longshots.
    assert model.per_contract(D("0.05"), maker=False) / D("0.05") > model.per_contract(D("0.95"), maker=False) / D("0.95")


# -------------------------------------------------------------- strategies
def test_identify_longshot_on_both_sides() -> None:
    assert identify_longshot(_book(("0.01", "5"), ("0.03", "5")), D("0.20")) == (Outcome.YES, D("0.03"))
    assert identify_longshot(_book(("0.91", "5"), ("0.93", "5")), D("0.20")) == (Outcome.NO, D("0.09"))
    assert identify_longshot(_book(("0.45", "5"), ("0.48", "5")), D("0.20")) is None
    assert identify_longshot(_book(None, ("0.20", "5")), D("0.20")) is None  # strict: 20c is not a longshot
    assert identify_longshot(_book(None, ("0.19", "5")), D("0.20")) == (Outcome.YES, D("0.19"))


def test_taker_fade_buys_the_favourite_at_the_touch_within_caps() -> None:
    strategy = LongshotFadeStrategy()
    ev = strategy.evaluate(_market(), _book(("0.01", "500"), ("0.03", "800")))
    assert ev.traded and ev.reason == "trade"
    (order,) = ev.orders
    assert order.side is Side.BUY and order.outcome is Outcome.NO
    assert order.price == D("0.99")  # NO ask = 1 - YES bid
    assert order.quantity == D("25")  # floor($25 / 0.99), whole contracts
    assert order.notional <= FLB_RISK_LIMITS.max_notional_per_order
    assert ev.placement == "take" and ev.fill_probability == D("1")
    assert ev.expected_edge_vs_mid == D("-0.01")  # pays the half-spread

    mirror = strategy.evaluate(_market(), _book(("0.91", "2000"), ("0.93", "1800")))
    (order,) = mirror.orders
    assert order.outcome is Outcome.YES and order.price == D("0.93") and order.quantity == D("26")
    assert mirror.longshot_outcome is Outcome.NO and mirror.longshot_price == D("0.09")

    assert strategy.evaluate(_market(), _book(None, ("0.02", "50"))).reason == "one_sided_book"
    assert strategy.evaluate(_market(), _book(("0.45", "5"), ("0.48", "5"))).reason == "not_longshot"
    assert strategy.evaluate(_market(), _book(("0.01", "0.5"), ("0.03", "5"))).reason == "insufficient_touch_depth"


def test_market_cash_at_risk_cap_stops_re_entry() -> None:
    ledger = PaperLedger(starting_cash=D("1000"))
    strategy = LongshotFadeStrategy(portfolio=ledger.portfolio)
    book = _book(("0.01", "500"), ("0.03", "800"))
    market = _market()
    total = D("0")
    for _ in range(5):
        ev = strategy.evaluate(market, book)
        if not ev.traded:
            assert ev.reason == "market_cap_reached"
            break
        (order,) = ev.orders
        total += order.notional
        from core.types import Fill

        ledger.record_fill(Fill(venue=Venue.KALSHI, market_id="KX-TEST", order_id="x", side=Side.BUY, outcome=Outcome.NO, quantity=order.quantity, price=order.price))
    else:
        pytest.fail("market cap never reached")
    assert total <= FLB_RISK_LIMITS.max_position_per_market
    assert position_cash_at_risk(ledger.portfolio.get(Venue.KALSHI, "KX-TEST")) <= D("75")
    assert abs(ledger.portfolio.get(Venue.KALSHI, "KX-TEST").quantity) <= FLB_RISK_LIMITS.max_position_per_market


def test_total_cash_at_risk_cap_prevents_borrowing() -> None:
    from core.types import Fill

    ledger = PaperLedger(starting_cash=D("100"))
    params = FlbParameters(max_total_cash_at_risk=D("100"))
    strategy = LongshotFadeStrategy(params, portfolio=ledger.portfolio)
    book = _book(("0.01", "500"), ("0.03", "800"))
    filled = 0
    for i in range(10):
        market = _market(f"KX-{i}")
        ev = strategy.evaluate(market, book)
        if not ev.traded:
            assert ev.reason == "capital_cap_reached"
            break
        (order,) = ev.orders
        ledger.record_fill(Fill(venue=Venue.KALSHI, market_id=market.market_id, order_id="x", side=Side.BUY, outcome=Outcome.NO, quantity=order.quantity, price=order.price))
        filled += 1
    else:
        pytest.fail("capital cap never reached")
    assert filled == 5  # 4 x 24.75 = 99, then a 1-contract order, then nothing fits
    assert ledger.cash >= D("0")
    from strategies.flb import portfolio_cash_at_risk

    assert portfolio_cash_at_risk(ledger.portfolio) <= D("100")
    assert longshot_buy_order(_market("KX-Z"), book, params, total_cash_at_risk=D("100")) is None


def test_maker_quote_improves_when_spread_allows_else_joins_with_queue_scaling() -> None:
    params = FlbParameters()
    strategy = MakerQuoteStrategy(params)
    improve = strategy.evaluate(_market(), _book(("0.01", "500"), ("0.03", "800")))
    (order,) = improve.orders
    assert improve.placement == "improve"
    assert order.outcome is Outcome.NO and order.price == D("0.98")  # YES ask 0.03 -> rest at 0.02 -> NO bid 0.98
    assert improve.quote_yes_price == D("0.02")
    assert improve.fill_probability == params.improve_fill_probability
    assert improve.expected_edge_vs_mid == D("0.00")  # sells YES at 0.02 == mid
    assert order.metadata["execution"] == "maker"

    join = strategy.evaluate(_market(), _book(("0.04", "900"), ("0.05", "1500")))
    (order,) = join.orders
    assert join.placement == "join" and order.price == D("0.95")
    # 26 contracts join behind 1500 displayed: 0.25 * 26 / 1526
    assert join.fill_probability == (D("0.25") * D("26") / D("1526")).quantize(D("0.0001"))
    assert join.expected_edge_vs_mid == D("0.005")  # sells YES at the ask, half-spread above mid
    assert join.expected_edge_after_adverse_selection == D("0.005") - params.adverse_selection_haircut * D("0.005")

    assert strategy.evaluate(_market(), _book(None, ("0.02", "50"))).reason == "one_sided_book"
    no_longshot = strategy.evaluate(_market(), _book(("0.91", "20"), ("0.93", "20")))
    (order,) = no_longshot.orders
    assert order.outcome is Outcome.YES and order.price == D("0.92")  # improve the YES bid


async def test_snapshot_client_expected_fill_and_maker_fee() -> None:
    snapshot = _flb_snapshot()
    client = FlbSnapshotClient(snapshot, KalshiFeeModel())
    market = next(m for m in snapshot.markets if m.market_id == "KXFEDDECISION-26DEC-H26")
    book = snapshot.book(market)
    order = Order(venue=Venue.KALSHI, market_id=market.market_id, side=Side.BUY, outcome=Outcome.NO, quantity=D("25"), price=D("0.98"), metadata={"execution": "maker", "fill_probability": "0.5"})
    report = await client.place_order_with_book(order, market, book)
    (fill,) = report.fills
    assert fill.quantity == D("12")  # floor(25 * 0.5)
    assert fill.price == D("0.98")
    assert fill.fee == KalshiFeeModel().fee(D("12"), D("0.98"), maker=True, market=market)
    assert fill.fee == D("0.0042")  # 0.0175 * 12 * 0.98 * 0.02 = 0.004116 -> centicent up
    assert report.order.status.value == "partially_filled"

    unfilled = await client.place_order_with_book(Order(venue=Venue.KALSHI, market_id=market.market_id, side=Side.BUY, outcome=Outcome.NO, quantity=D("3"), price=D("0.98"), metadata={"execution": "maker", "fill_probability": "0.25"}), market, book)
    assert unfilled.fills == () and unfilled.order.status.value == "accepted"

    # A "maker" order priced through the touch is really a taker: it walks the book and pays taker fees.
    crossing = Order(venue=Venue.KALSHI, market_id=market.market_id, side=Side.BUY, outcome=Outcome.NO, quantity=D("10"), price=D("0.99"), metadata={"execution": "maker", "fill_probability": "0.5"})
    report = await client.place_order_with_book(crossing, market, book)
    (fill,) = report.fills
    assert fill.quantity == D("10") and fill.price == D("0.99")
    assert fill.fee == KalshiFeeModel().fee(D("10"), D("0.99"), maker=False, market=market)


def test_longshot_buy_order_mirrors_the_fade_with_the_same_caps() -> None:
    order = longshot_buy_order(_market(), _book(("0.01", "500"), ("0.03", "800")), FlbParameters())
    assert order is not None
    assert order.outcome is Outcome.YES and order.price == D("0.03")
    assert order.quantity == D("800") and order.notional == D("24.00")  # touch-limited, under $25
    assert longshot_buy_order(_market(), _book(("0.45", "5"), ("0.48", "5")), FlbParameters()) is None


# ------------------------------------------------------------------ tracks
async def test_flb_tracks_on_fixture_universe() -> None:
    snapshot = _flb_snapshot()
    summaries, ledgers = await run_flb_tracks({Venue.KALSHI: snapshot})
    by_track = {s.track: s for s in summaries}
    assert set(by_track) == set(FLB_TRACKS)
    fade, maker = by_track["kalshi_longshot_fade"], by_track["kalshi_maker_quote"]

    assert fade.candidates == 12 and fade.metrics["longshot_candidates"] == 10
    assert fade.refused_by_reason == {"not_longshot": 2, "one_sided_book": 1}
    assert fade.admitted == 9 and fade.paper_fills == 9
    assert all(row["role"] == "taker" for row in fade.fills)
    assert fade.metrics["parameters"]["risk_limits"]["max_notional_per_order"] == D("25")
    assert fade.metrics["parameters"]["risk_limits"]["max_position_per_market"] == D("75")
    assert fade.metrics["parameters"]["risk_limits"]["max_daily_loss"] == D("75")
    assert set(fade.metrics["longshot_bands"]) == {"<10c", "10-20c"}
    assert fade.metrics["longshot_bands"]["<10c"]["fills"] == 6
    assert fade.metrics["longshot_bands"]["10-20c"]["fills"] == 3
    assert set(fade.metrics["paper_pnl_by_longshot_band"]) == {"<10c", "10-20c"}
    shadow = fade.metrics["shadow_longshot_buyer"]
    assert shadow["fills"] == 9 and shadow["total_pnl"] < 0  # buys longshots at the ask, marked at mid
    assert shadow["fees_paid"] > fade.ledger["fees_paid"]  # many more contracts for the same dollars

    assert maker.admitted == 9 and 0 < maker.paper_fills < maker.proposed_orders  # some expected fills floor to zero
    assert all(row["role"] == "maker" for row in maker.fills)
    assert maker.metrics["mark_method"] == "conservative"
    assert ledgers["kalshi_maker_quote"].mark_method == "conservative"
    assert ledgers["kalshi_longshot_fade"].mark_method == "mid"
    assert maker.ledger["fees_paid"] < fade.ledger["fees_paid"]  # maker rate is a quarter of the taker rate
    cash_at_risk = maker.metrics["cash_at_risk"]
    assert cash_at_risk["positions"] + cash_at_risk["resting_orders_reserved"] <= cash_at_risk["cap"] == D("1000")
    assert cash_at_risk["resting_orders_reserved"] > 0  # unfilled remainders lock collateral
    for ledger in ledgers.values():
        assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl
        for position in ledger.open_positions:
            assert abs(position.quantity) <= FLB_RISK_LIMITS.max_position_per_market
            assert position_cash_at_risk(position) <= FLB_RISK_LIMITS.max_position_per_market
    for fill in ledgers["kalshi_longshot_fade"].fills:
        assert fill.quantity * fill.price <= FLB_RISK_LIMITS.max_notional_per_order


async def test_flb_tracks_carry_ledgers_and_stop_at_the_market_cap() -> None:
    snapshot = _flb_snapshot()
    ledgers: dict[str, PaperLedger] = {}
    for _ in range(4):
        _, ledgers = await run_flb_tracks({Venue.KALSHI: snapshot}, ledgers=ledgers)
    summaries, ledgers = await run_flb_tracks({Venue.KALSHI: snapshot}, ledgers=ledgers)
    fade = next(s for s in summaries if s.track == "kalshi_longshot_fade")
    assert fade.paper_fills == 0
    # Every longshot market is capped: by $75 cash at risk (99c favourites) or by the
    # RiskManager's 75-contract rail (cheaper favourites hit 75 contracts before $75).
    reasons = fade.refused_by_reason
    assert reasons.get("market_cap_reached", 0) + reasons.get("risk_position_cap_reached", 0) == 9
    assert reasons.get("market_cap_reached", 0) >= 1 and reasons.get("risk_position_cap_reached", 0) >= 1
    for position in ledgers["kalshi_longshot_fade"].open_positions:
        assert position_cash_at_risk(position) <= D("75")
        assert abs(position.quantity) <= D("75")
    assert len(ledgers["kalshi_longshot_fade"].equity_curve) == 5


async def test_measure_all_includes_flb_tracks_with_their_own_limits() -> None:
    summaries, ledgers = await measure_all_with_ledgers()
    assert [s.track for s in summaries] == list(TRACKS)
    fade = next(s for s in summaries if s.track == "kalshi_longshot_fade")
    # Standard fixtures have no longshot side, so the tracks stay empty but measured.
    assert fade.candidates == 3 and fade.refused_by_reason == {"not_longshot": 3}
    assert fade.paper_fills == 0 and fade.ledger["total_pnl"] == D("0")
    assert fade.metrics["parameters"]["risk_limits"]["max_daily_loss"] == D("75")
    assert ledgers["kalshi_maker_quote"].mark_method == "conservative"


# --------------------------------------------------------- snapshot verdicts
def test_snapshot_band_table_and_overround() -> None:
    snapshot = _flb_snapshot()
    table = snapshot_band_table(snapshot)
    assert table["<10c"]["markets"] == 6 and table["<10c"]["two_sided"] == 5
    assert table["10-20c"]["markets"] == 2
    # Taking a 3c longshot: half-spread 1c + fee 0.07*0.03*0.97 over 3c is far dearer than a 93c favourite.
    assert table["<10c"]["mean_take_cost_pct_of_price"] > table[">=90c"]["mean_take_cost_pct_of_price"]
    over = snapshot_event_overround(snapshot)
    assert over["n_events"] == 3
    fed = next(e for e in over["events"] if e["event_ticker"] == "KXFEDDECISION-26DEC")
    assert fed["legs"] == 5 and fed["sum_ask"] == D("1.0500") and fed["overround_ask"] == D("0.0500")
    assert fed["longshot_legs"] == 4
    assert over["events_with_positive_overround"] == 3


def test_snapshot_verdicts_never_claim_flb_from_prices() -> None:
    verdicts = snapshot_verdicts(_flb_snapshot())
    assert verdicts["flb_identifiable_from_snapshot"]["verdict"] == "NOT_IDENTIFIABLE"
    assert verdicts["event_overround_positive"]["verdict"] == "PASS"
    # Only two fixture markets sit in the favourite bands: below the 3-market floor.
    assert verdicts["longshot_take_cost_exceeds_favorite"]["verdict"] == "INSUFFICIENT_DATA"
    relaxed = snapshot_verdicts(_flb_snapshot(), min_markets_per_side=1)
    assert relaxed["longshot_take_cost_exceeds_favorite"]["verdict"] == "PASS"
    empty = snapshot_verdicts(VenueSnapshot(venue=Venue.KALSHI, source="fixture"))
    assert empty["flb_identifiable_from_snapshot"]["verdict"] == "INSUFFICIENT_DATA"
    assert empty["event_overround_positive"]["verdict"] == "INSUFFICIENT_DATA"
