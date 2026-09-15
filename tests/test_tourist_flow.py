from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.ledger import PaperLedger
from core.risk import RiskManager
from core.types import Market, OrderBook, Outcome, PriceLevel, Venue
from strategies.tourist_flow import (
    TOURIST_RISK_LIMITS,
    ClusterDetector,
    FadeTouristStrategy,
    TapeTrade,
    TouristParameters,
    classify_trade,
    detect_clusters,
    fade_notional,
    life_fraction,
    regime_for,
    replay_fade_price,
    size_fade,
)

D = Decimal
OPEN = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
CLOSE = OPEN + timedelta(hours=6)


def _t(seconds: float) -> datetime:
    return OPEN + timedelta(seconds=seconds)


def _trade(seconds: float, side: str, yes_price: str, count: str) -> TapeTrade:
    return TapeTrade(_t(seconds), Outcome(side), D(yes_price), D(count))


def _market(market_id: str = "KXATPMATCH-26SEP20AAABBB-AAA", *, active: bool = True) -> Market:
    return Market(venue=Venue.KALSHI, market_id=market_id, title=market_id, active=active, metadata={"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1})


def _book(bid: str, ask: str, size: str = "100") -> OrderBook:
    return OrderBook(market_id="m", bids=(PriceLevel(D(bid), D(size)),), asks=(PriceLevel(D(ask), D(size)),))


def test_parameters_reject_nonsense() -> None:
    with pytest.raises(ValueError):
        TouristParameters(longshot_threshold=D("1.2"))
    with pytest.raises(ValueError):
        TouristParameters(min_flags=4)
    with pytest.raises(ValueError):
        TouristParameters(strong_multiplier=D("0.5"))
    assert TouristParameters().as_dict()["min_flags"] == 2


def test_tape_trade_prices_and_notional_follow_the_side_bought() -> None:
    yes_buy = _trade(0, "yes", "0.25", "20")
    no_buy = _trade(0, "no", "0.25", "20")
    assert yes_buy.taker_price == D("0.25") and yes_buy.taker_notional == D("5.00")
    assert no_buy.taker_price == D("0.75") and no_buy.taker_notional == D("15.00")
    assert no_buy.side_price(Outcome.YES) == D("0.25") and no_buy.side_price(Outcome.NO) == D("0.75")
    with pytest.raises(ValueError):
        TapeTrade(_t(0), Outcome.YES, D("1.5"), D("1"))


def test_classifier_flags_small_longshot_and_late_chase() -> None:
    params = TouristParameters()
    # Small ticket, longshot: 20 contracts of YES at 0.25 = $5.
    flags = classify_trade(_trade(0, "yes", "0.25", "20"), [], params, open_time=OPEN, close_time=CLOSE)
    assert flags.small_ticket and flags.longshot_buy and not flags.late_chase
    assert flags.score == 2 and flags.combination == "small+longshot" and flags.is_tourist(params)
    # Large ticket at a fair-ish price: nothing.
    flags = classify_trade(_trade(0, "yes", "0.55", "300"), [], params, open_time=OPEN, close_time=CLOSE)
    assert flags.score == 0 and flags.combination == "none" and not flags.is_tourist(params)
    # Chase: YES ran from 0.40 to 0.48 over the last 20 prints, then a small YES buy late in the match.
    history = [_trade(i * 60, "yes" if i % 2 else "no", str(D("0.40") + D("0.004") * i), "150") for i in range(20)]
    late = classify_trade(_trade(5 * 3600, "yes", "0.48", "15"), history, params, open_time=OPEN, close_time=CLOSE)
    assert late.small_ticket and late.late_chase and not late.longshot_buy
    assert late.chase_move == D("0.0800") and late.life_fraction >= D("0.5")
    # The same chase early in the market's life is not "late".
    early = classify_trade(_trade(30 * 60, "yes", "0.48", "15"), history, params, open_time=OPEN, close_time=CLOSE)
    assert early.chase_move == D("0.0800") and not early.late_chase and early.score == 1
    # Without open/close times the late condition cannot be established.
    unknown = classify_trade(_trade(5 * 3600, "yes", "0.48", "15"), history, params)
    assert unknown.life_fraction is None and not unknown.late_chase
    # Buying the side that fell is not a chase.
    fell = classify_trade(_trade(5 * 3600, "no", "0.48", "15"), history, params, open_time=OPEN, close_time=CLOSE)
    assert fell.chase_move < 0 and not fell.late_chase


def test_life_fraction_is_clamped_and_none_when_unknown() -> None:
    assert life_fraction(OPEN, open_time=OPEN, close_time=CLOSE) == D("0")
    assert life_fraction(CLOSE + timedelta(hours=1), open_time=OPEN, close_time=CLOSE) == D("1")
    assert life_fraction(OPEN + timedelta(hours=3), open_time=OPEN, close_time=CLOSE) == D("0.5")
    assert life_fraction(OPEN, open_time=None, close_time=CLOSE) is None
    assert life_fraction(OPEN, open_time=CLOSE, close_time=OPEN) is None


def test_cluster_detector_needs_count_and_notional_within_window_and_respects_cooldown() -> None:
    params = TouristParameters(cluster_window_seconds=600, cluster_min_trades=5, cluster_min_notional=D("50"), cooldown_seconds=300)
    detector = ClusterDetector("m", params)
    # Five $8 longshot prints within four minutes: count met at the 5th, notional 40 < 50 -> no signal yet.
    prints = [_trade(i * 60, "yes", "0.25", "32") for i in range(5)]
    signals = [detector.observe(t, classify_trade(t, prints[:i], params)) for i, t in enumerate(prints)]
    assert signals == [None] * 5
    # Two more prints push notional to 56 -> signal on the 7th print, side YES, fade side NO.
    sixth, seventh = _trade(5 * 60, "yes", "0.25", "32"), _trade(6 * 60, "yes", "0.25", "32")
    assert detector.observe(sixth, classify_trade(sixth, prints, params)) is None
    signal = detector.observe(seventh, classify_trade(seventh, prints + [sixth], params))
    assert signal is not None and signal.side is Outcome.YES and signal.fade_side is Outcome.NO
    assert signal.trades == 7 and signal.notional == D("56.0000") and signal.favorite_price_estimate == D("0.75")
    assert signal.combinations == {"small+longshot": 7}
    # Another print inside the cooldown is swallowed; after the cooldown a new signal fires.
    eighth = _trade(7 * 60, "yes", "0.25", "32")
    assert detector.observe(eighth, classify_trade(eighth, prints, params)) is None
    ninth = _trade(12 * 60, "yes", "0.25", "32")
    assert detector.observe(ninth, classify_trade(ninth, prints, params)) is not None
    # Non-tourist prints never enter the window.
    big = _trade(13 * 60, "yes", "0.25", "5000")
    assert detector.observe(big, classify_trade(big, prints, params)) is None
    assert detector.tourist_trades == 9
    # Prints older than the window fall out: a lone print an hour later restarts the count.
    later = _trade(2 * 3600, "yes", "0.25", "32")
    assert detector.observe(later, classify_trade(later, prints, params)) is None
    assert len(detector._sides[Outcome.YES].trades) == 1


def test_detect_clusters_sorts_the_tape_and_flags_every_print() -> None:
    params = TouristParameters()
    tape = [_trade(i * 30, "no", "0.74", "32") for i in range(7)]
    detector, flagged = detect_clusters("m", list(reversed(tape)), params)
    assert [t.created_time for t, _ in flagged] == sorted(t.created_time for t in tape)
    assert len(detector.signals) == 1 and detector.signals[0].side is Outcome.NO
    assert detector.signals[0].fade_side is Outcome.YES


def test_regime_and_fade_notional_double_above_70c_and_stay_under_the_order_cap() -> None:
    params = TouristParameters()
    assert regime_for(D("0.70"), params) == "strong" and regime_for(D("0.69"), params) == "weak"
    assert fade_notional(D("0.80"), params) == D("20") and fade_notional(D("0.55"), params) == D("10")
    capped = TouristParameters(base_notional=D("20"), strong_multiplier=D("3"))
    assert fade_notional(D("0.80"), capped) == TOURIST_RISK_LIMITS.max_notional_per_order


def test_size_fade_respects_market_total_touch_and_risk_caps() -> None:
    params = TouristParameters()
    qty, reason = size_fade(favorite_price=D("0.80"), params=params, position=None, total_cash_at_risk=D("0"))
    assert (qty, reason) == (D("25"), "")  # $20 strong fade / 0.80 = 25 contracts
    qty, reason = size_fade(favorite_price=D("0.55"), params=params, position=None, total_cash_at_risk=D("0"))
    assert (qty, reason) == (D("18"), "")  # $10 / 0.55 = 18.18 -> 18
    # Touch depth binds.
    assert size_fade(favorite_price=D("0.80"), params=params, position=None, total_cash_at_risk=D("0"), touch_size=D("7")) == (D("7"), "")
    assert size_fade(favorite_price=D("0.80"), params=params, position=None, total_cash_at_risk=D("0"), touch_size=D("0.5")) == (D("0"), "insufficient_touch_depth")
    # Total collateral cap.
    assert size_fade(favorite_price=D("0.80"), params=params, position=None, total_cash_at_risk=D("999.5")) == (D("0"), "capital_cap_reached")
    # Bound prices are refused.
    assert size_fade(favorite_price=D("1"), params=params, position=None, total_cash_at_risk=D("0"))[1] == "price_at_bound"
    # Risk manager halted -> risk_halted.
    risk = RiskManager(TOURIST_RISK_LIMITS)
    risk.record_realized_pnl(D("-80"))
    from core.types import Order, Side

    probe = Order(venue=Venue.KALSHI, market_id="m", side=Side.BUY, quantity=D("1"), outcome=Outcome.YES, price=D("0.80"))
    assert size_fade(favorite_price=D("0.80"), params=params, position=None, total_cash_at_risk=D("0"), risk=risk, probe=probe) == (D("0"), "risk_halted")


def test_replay_fade_price_adds_the_assumed_spread_and_stays_below_one() -> None:
    params = TouristParameters(assumed_spread_ticks=2)
    _, flagged = detect_clusters("m", [_trade(i * 30, "yes", "0.25", "32") for i in range(7)], params)
    detector, _ = detect_clusters("m", [_trade(i * 30, "yes", "0.25", "32") for i in range(7)], params)
    assert replay_fade_price(detector.signals[0], params) == D("0.77")
    detector_hi, _ = detect_clusters("m", [_trade(i * 30, "yes", "0.01", "900") for i in range(7)], params)
    assert replay_fade_price(detector_hi.signals[0], params) == D("0.99")


def _tape_with_no_cluster(seconds: float = 0) -> list[TapeTrade]:
    return [_trade(seconds + i * 30, "no", "0.74", "32") for i in range(7)]


def test_strategy_fades_a_fresh_cluster_at_the_touch_in_the_strong_regime() -> None:
    ledger = PaperLedger(starting_cash=D("1000"), ledger_id="t")
    strategy = FadeTouristStrategy(portfolio=ledger.portfolio, risk=RiskManager(TOURIST_RISK_LIMITS))
    market = _market()
    ev = strategy.evaluate(market, _book("0.74", "0.76", "150"), _tape_with_no_cluster(), open_time=OPEN, close_time=CLOSE)
    assert ev.traded and ev.reason == "trade" and ev.fade_side is Outcome.YES and ev.regime == "strong"
    assert ev.favorite_price == D("0.76") and ev.quantity == D("26")  # $20 / 0.76
    (order,) = ev.orders
    assert order.outcome is Outcome.YES and order.price == D("0.76") and order.quantity == D("26")
    assert order.metadata["faded_side"] == "no" and order.metadata["regime"] == "strong" and order.metadata["execution"] == "taker"
    assert order.notional <= TOURIST_RISK_LIMITS.max_notional_per_order


def test_strategy_refusals_are_explicit() -> None:
    params = TouristParameters()
    ledger = PaperLedger(starting_cash=D("1000"), ledger_id="t")
    strategy = FadeTouristStrategy(params, portfolio=ledger.portfolio, risk=RiskManager(TOURIST_RISK_LIMITS))
    market = _market()
    book = _book("0.74", "0.76")
    assert strategy.evaluate(_market(active=False), book, _tape_with_no_cluster()).reason == "market_inactive"
    assert strategy.evaluate(market, book, []).reason == "no_tape"
    assert strategy.evaluate(market, OrderBook(market_id="m"), _tape_with_no_cluster()).reason == "empty_book"
    quiet = [_trade(i * 30, "yes" if i % 2 else "no", "0.74", "300") for i in range(10)]
    assert strategy.evaluate(market, book, quiet).reason == "no_tourist_cluster"
    stale = _tape_with_no_cluster() + [_trade(4000 + i * 60, "yes", "0.74", "300") for i in range(3)]
    ev = strategy.evaluate(market, book, stale)
    assert ev.reason == "cluster_stale" and ev.signal is not None
    # as_of later than the newest print also makes the cluster stale.
    assert strategy.evaluate(market, book, _tape_with_no_cluster(), as_of=_t(5000)).reason == "cluster_stale"
    # Fade side is YES; a book without a YES ask cannot be taken.
    one_sided = OrderBook(market_id="m", bids=(PriceLevel(D("0.74"), D("100")),))
    assert strategy.evaluate(market, one_sided, _tape_with_no_cluster()).reason == "one_sided_book"
    assert strategy.evaluate(market, _book("0.74", "0.76", "0.5"), _tape_with_no_cluster()).reason == "insufficient_touch_depth"
    # Once positioned, the same cluster is not re-faded.
    ev = strategy.evaluate(market, book, _tape_with_no_cluster())
    assert ev.traded
    from core.types import Fill, Side

    ledger.record_fill(Fill(venue=Venue.KALSHI, market_id=market.market_id, order_id="x", side=Side.BUY, quantity=D("26"), price=D("0.76"), outcome=Outcome.YES))
    assert strategy.evaluate(market, book, _tape_with_no_cluster()).reason == "already_positioned"


def test_strategy_fades_a_chase_cluster_in_the_weak_regime_by_buying_no() -> None:
    ledger = PaperLedger(starting_cash=D("1000"), ledger_id="t")
    strategy = FadeTouristStrategy(portfolio=ledger.portfolio, risk=RiskManager(TOURIST_RISK_LIMITS))
    run_up = [_trade(i * 40, "yes" if i % 2 else "no", str(D("0.38") + D("0.004") * i), "120") for i in range(22)]
    chase = [_trade(5 * 3600 + i * 45, "yes", "0.47", "18") for i in range(7)]
    ev = strategy.evaluate(_market(), _book("0.45", "0.48", "120"), run_up + chase, open_time=OPEN, close_time=CLOSE)
    assert ev.traded and ev.fade_side is Outcome.NO and ev.regime == "weak"
    assert ev.favorite_price == D("0.55") and ev.quantity == D("18")  # NO ask = 1 - 0.45; $10 / 0.55
    # 6 x $8.46 = $50.76 crosses the $50 threshold on the sixth chase print; the 7th is inside the cooldown.
    assert ev.signal is not None and ev.signal.combinations == {"small+chase": 6}


async def test_propose_never_trades_without_a_tape() -> None:
    assert await FadeTouristStrategy().propose(_market(), _book("0.74", "0.76")) == []
