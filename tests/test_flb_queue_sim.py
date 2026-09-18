"""Queue-aware FLB maker fill simulation — paper only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from core.types import Market, OrderBook, Outcome, PriceLevel, Venue
from research.flb_expost import SettledMarket, TradeRecord
from research.flb_queue_sim import (
    FIXTURE_PATH,
    PRE_REGISTRATION,
    TRACK,
    load_fixture_timelines,
    net_ev_verdict,
    run_queue_sim,
    timelines_from_settled,
)
from strategies.flb import FlbParameters
from strategies.flb_queue import (
    DEFAULT_PASS_NET_EV,
    DEFAULT_STRETCH_NET_EV,
    FlbQueueFillModel,
    FlbQueueParameters,
    FlbRestingQuote,
    place_flb_maker_quote,
    place_flb_maker_quote_from_longshot_print,
)
from strategies.whale_noise import Print

D = Decimal


def _market(mid: str = "KX-TEST") -> Market:
    return Market(
        venue=Venue.KALSHI,
        market_id=mid,
        title=mid,
        metadata={"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1, "series_ticker": "KXFEDDECISION"},
    )


def _book(bid: tuple[str, str], ask: tuple[str, str], mid: str = "KX-TEST") -> OrderBook:
    return OrderBook(
        market_id=mid,
        bids=(PriceLevel(D(bid[0]), D(bid[1])),),
        asks=(PriceLevel(D(ask[0]), D(ask[1])),),
    )


def test_pre_registration_bars_are_two_and_three_cents() -> None:
    params = FlbQueueParameters()
    assert params.pass_net_ev == DEFAULT_PASS_NET_EV == D("0.02")
    assert params.stretch_net_ev == DEFAULT_STRETCH_NET_EV == D("0.03")
    assert "2¢" in PRE_REGISTRATION["hypothesis"] or "0.02" in str(PRE_REGISTRATION)
    assert TRACK == "kalshi_maker_queue_sim"


def test_place_improve_sits_first_join_sits_behind_displayed() -> None:
    params = FlbQueueParameters()
    ts = datetime(2026, 9, 15, 16, 0, tzinfo=UTC)
    improve = place_flb_maker_quote(
        _market(), _book(("0.04", "800"), ("0.06", "500")), params, placed_at=ts, position=None, risk=None, total_cash_at_risk=D("0")
    )
    assert improve.traded and improve.placement == "improve"
    assert improve.quote is not None
    assert improve.quote.queue_ahead_initial == D("0")
    assert improve.quote.outcome is Outcome.NO and improve.quote.yes_price == D("0.05")

    join = place_flb_maker_quote(
        _market(), _book(("0.05", "900"), ("0.06", "1500")), params, placed_at=ts, position=None, risk=None, total_cash_at_risk=D("0")
    )
    assert join.placement == "join"
    assert join.quote is not None
    assert join.quote.queue_ahead_initial == D("1500")  # behind full touch


def test_queue_model_never_fills_ahead_of_displayed_size() -> None:
    params = FlbQueueParameters(reaction_latency_seconds=D("0"), quote_ttl_seconds=D("600"))
    ts = datetime(2026, 9, 15, 16, 0, tzinfo=UTC)
    quote = FlbRestingQuote(
        market_id="KX-TEST",
        outcome=Outcome.NO,
        price=D("0.95"),
        yes_price=D("0.05"),
        quantity=D("25"),
        placement="join",
        placed_at=ts,
        active_from=ts,
        expires_at=ts + timedelta(seconds=600),
        queue_ahead=D("100"),
        queue_ahead_initial=D("100"),
        longshot_outcome=Outcome.YES,
        longshot_price=D("0.05"),
        reference_touch_yes=D("0.06"),
        direction=-1,
    )
    model = FlbQueueFillModel(params)
    # 40 at our level: consumes 40 of queue, fills 0
    filled = model.on_print(quote, Print(ts=ts + timedelta(seconds=1), yes_price=D("0.05"), size=D("40"), taker_side="yes", trade_id="1"))
    assert filled == D("0") and quote.queue_ahead == D("60")
    # 70 more: consumes 60, fills 10
    filled = model.on_print(quote, Print(ts=ts + timedelta(seconds=2), yes_price=D("0.05"), size=D("70"), taker_side="yes", trade_id="2"))
    assert filled == D("10") and quote.filled == D("10")


def test_trade_through_fills_at_most_the_printed_size() -> None:
    params = FlbQueueParameters(reaction_latency_seconds=D("0"), quote_ttl_seconds=D("600"))
    ts = datetime(2026, 9, 15, 16, 0, tzinfo=UTC)
    quote = FlbRestingQuote(
        market_id="KX-TEST",
        outcome=Outcome.NO,
        price=D("0.95"),
        yes_price=D("0.05"),
        quantity=D("25"),
        placement="improve",
        placed_at=ts,
        active_from=ts,
        expires_at=ts + timedelta(seconds=600),
        queue_ahead=D("0"),
        queue_ahead_initial=D("0"),
        longshot_outcome=Outcome.YES,
        longshot_price=D("0.05"),
        reference_touch_yes=D("0.06"),
        direction=-1,
    )
    model = FlbQueueFillModel(params)
    filled = model.on_print(quote, Print(ts=ts + timedelta(seconds=1), yes_price=D("0.07"), size=D("12"), taker_side="yes", trade_id="t"))
    assert filled == D("12") and quote.trade_through is True


def test_bookless_quote_fills_only_on_trade_through() -> None:
    params = FlbQueueParameters(reaction_latency_seconds=D("0"), quote_ttl_seconds=D("600"), settled_trade_through_only=True)
    ts = datetime(2026, 9, 15, 16, 0, tzinfo=UTC)
    print_ = Print(ts=ts, yes_price=D("0.12"), size=D("200"), taker_side="yes", trade_id="d1")
    ev = place_flb_maker_quote_from_longshot_print(_market(), print_, params, position=None, risk=None, total_cash_at_risk=D("0"))
    assert ev.traded and ev.quote is not None and ev.quote.bookless
    model = FlbQueueFillModel(params)
    # At-level print must not fill bookless quotes
    assert model.on_print(ev.quote, Print(ts=ts + timedelta(seconds=5), yes_price=ev.quote.yes_price, size=D("50"), taker_side="yes", trade_id="x")) == D("0")
    # Through does
    filled = model.on_print(ev.quote, Print(ts=ts + timedelta(seconds=6), yes_price=ev.quote.yes_price + D("0.02"), size=D("20"), taker_side="yes", trade_id="y"))
    assert filled == D("20")


def test_later_books_only_lengthen_the_queue() -> None:
    params = FlbQueueParameters()
    ts = datetime(2026, 9, 15, 16, 0, tzinfo=UTC)
    quote = FlbRestingQuote(
        market_id="KX-TEST",
        outcome=Outcome.NO,
        price=D("0.95"),
        yes_price=D("0.05"),
        quantity=D("10"),
        placement="join",
        placed_at=ts,
        active_from=ts,
        expires_at=ts + timedelta(seconds=600),
        queue_ahead=D("100"),
        queue_ahead_initial=D("100"),
        longshot_outcome=Outcome.YES,
        longshot_price=D("0.05"),
        reference_touch_yes=D("0.06"),
        direction=-1,
    )
    model = FlbQueueFillModel(params)
    model.on_book(quote, _book(("0.04", "50"), ("0.05", "80")))  # smaller → no change
    assert quote.queue_ahead == D("100") and quote.queue_raised_by_books == 0
    model.on_book(quote, _book(("0.04", "50"), ("0.05", "250")))
    assert quote.queue_ahead == D("250") and quote.queue_raised_by_books == 1


def test_net_ev_verdict_underpowered_pass_fail() -> None:
    params = FlbQueueParameters(min_fills_for_verdict=5, min_markets_for_verdict=2, pass_net_ev=D("0.02"), stretch_net_ev=D("0.03"), t_threshold=D("2"))
    under = net_ev_verdict({"fills": 2, "net_ev": {"n_markets": 1, "mean": 0.05, "t_stat": 3.0}, "primary_scoring": "settlement"}, params)
    assert under["verdict"] == "INSUFFICIENT_DATA" and under["underpowered"] is True

    fail = net_ev_verdict(
        {"fills": 10, "net_ev": {"n_markets": 5, "mean": 0.005, "t_stat": 0.5}, "primary_scoring": "settlement"},
        params,
    )
    assert fail["verdict"] == "FAIL"

    passed = net_ev_verdict(
        {"fills": 10, "net_ev": {"n_markets": 5, "mean": 0.025, "t_stat": 2.5}, "primary_scoring": "settlement"},
        params,
    )
    assert passed["verdict"] == "PASS"
    stretch = net_ev_verdict(
        {"fills": 10, "net_ev": {"n_markets": 5, "mean": 0.04, "t_stat": 3.0}, "primary_scoring": "settlement"},
        params,
    )
    assert stretch["verdict"] == "PASS" and stretch["stretch_pass"] is True


@pytest.mark.asyncio
async def test_fixture_replay_produces_fills_settlement_and_honesty() -> None:
    timelines, meta = load_fixture_timelines(FIXTURE_PATH)
    assert meta["source"] == "fixture" and len(timelines) == 4
    assert any(t.alignment == "settled_trades_only" for t in timelines)
    assert any(t.settlement is not None for t in timelines)
    params = FlbQueueParameters(
        flb=FlbParameters(),
        reaction_latency_seconds=D("2"),
        min_fills_for_verdict=1,
        min_markets_for_verdict=1,
    )
    report = await run_queue_sim(timelines, params=params, model_fees=True, source_meta=meta)
    assert report["paper_only"] is True
    assert report["track"] == TRACK
    assert report["stats"]["fills"] >= 1
    assert report["settlement"]["markets_with_outcome"] >= 1
    assert report["stats"]["primary_scoring"] == "settlement"
    assert "honesty_limits" in report and len(report["honesty_limits"]) >= 5
    assert report["pre_registration"]["name"] == TRACK
    assert "L3" in " ".join(report["honesty_limits"]) or "FIFO" in " ".join(report["honesty_limits"])
    # Existing FLB expected-value params still present on nested flb block
    assert report["parameters"]["flb"]["join_fill_probability"] == D("0.25")


@pytest.mark.asyncio
async def test_settled_history_path_trade_through_only() -> None:
    ts0 = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    settled = [
        SettledMarket(
            ticker="KX-SETTLED-1",
            series_ticker="KXFEDDECISION",
            result="no",
            close_time=ts0 + timedelta(hours=2),
            category="economics",
            fee_type="quadratic_with_maker_fees",
            fee_multiplier=D("1"),
            trades=[
                TradeRecord(created_time=ts0, taker_side="yes", yes_price=D("0.08"), count=D("100")),
                TradeRecord(created_time=ts0 + timedelta(seconds=10), taker_side="yes", yes_price=D("0.08"), count=D("50")),
                TradeRecord(created_time=ts0 + timedelta(seconds=30), taker_side="yes", yes_price=D("0.12"), count=D("40")),
            ],
            title="settled synthetic",
        )
    ]
    timelines, meta = timelines_from_settled(settled)
    assert meta["alignment"] == "settled_trades_only"
    params = FlbQueueParameters(reaction_latency_seconds=D("0"), min_fills_for_verdict=1, min_markets_for_verdict=1)
    report = await run_queue_sim(timelines, params=params, source_meta=meta)
    assert report["alignment"] == "settled_trades_only"
    # Bookless: only the through print at 0.12 should fill (quote yes ~0.09)
    assert report["stats"]["fills"] >= 1
    assert all(f["trade_through"] for f in report["fills"])
    assert report["stats"]["primary_scoring"] == "settlement"


@pytest.mark.asyncio
async def test_measure_flb_queue_sim_flag_writes_artifact(tmp_path: Path) -> None:
    from apps.measure_flb import run

    report = await run(
        use_network=False,
        limit=50,
        artifact_dir=tmp_path,
        harvest_dir=tmp_path,
        harvest_trades=False,
        settled_per_series=5,
        max_trades_per_market=100,
        series=(),
        expost_series=(),
        kalshi_env=None,
        persist_ledgers=False,
        reset_ledgers=True,
        model_fees=True,
        params=FlbParameters(),
        min_markets=1,
        min_contracts=1,
        exclude_final_minutes=60,
        queue_sim=True,
        queue_params=FlbQueueParameters(min_fills_for_verdict=1, min_markets_for_verdict=1),
    )
    # Existing FLB paths still present
    assert "snapshot" in report and "ex_post" in report and "tracks" in report
    assert "kalshi_longshot_fade" in report["tracks"] and "kalshi_maker_quote" in report["tracks"]
    assert "queue_sim" in report
    assert (tmp_path / "flb_queue_sim_latest.json").exists()
    assert (tmp_path / "flb_report_latest.json").exists()
    assert report["queue_sim"]["track"] == TRACK
