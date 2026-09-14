import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apps.measure_all import json_default, persist_run
from core.portfolio import Portfolio
from core.types import Market, OrderBook, Outcome, PriceLevel, Side, Venue
from research.news_signals import (
    FIXTURE_PATH,
    CompositeSignalSource,
    FixtureSignalSource,
    JsonSignalSource,
    NewsSignal,
    NullSignalSource,
    RssHeadlineSource,
    SignalBatch,
    build_signal_source,
    load_signals,
)
from research.news_underreaction import measure_news_lane
from research.scoreboard import NEWS_TRACK, TRACKS, measure_all_with_ledgers
from research.scoreboard_artifact import build_scoreboard_artifact
from strategies.news_underreaction import (
    NewsUnderreactionStrategy,
    UnderreactionParameters,
    measure_underreaction,
)
from venues.fixtures import load_fixture
from venues.kalshi.client import FIXTURE_PATH as KALSHI_FIXTURE
from venues.polymarket.client import FIXTURE_PATH as POLY_FIXTURE

AS_OF = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
MARKET = Market(Venue.KALSHI, "M", "Will the Fed cut rates at the September meeting?")
D = Decimal


def book(bid: str, bid_size: str, ask: str, ask_size: str, market_id: str = "M") -> OrderBook:
    return OrderBook(
        market_id=market_id,
        bids=(PriceLevel(D(bid), D(bid_size)),),
        asks=(PriceLevel(D(ask), D(ask_size)),),
    )


def signal(
    *,
    implied: str | None = "0.70",
    pre: str | None = "0.45",
    age: int = 180,
    confidence: str = "0.9",
    market_id: str = "M",
    venue: Venue = Venue.KALSHI,
) -> NewsSignal:
    return NewsSignal(
        signal_id="s1",
        venue=venue,
        market_id=market_id,
        headline="Fed officials signal a September cut",
        observed_at=AS_OF - timedelta(seconds=age),
        source="test",
        implied_probability=D(implied) if implied is not None else None,
        pre_signal_mid=D(pre) if pre is not None else None,
        confidence=D(confidence),
        mapping="operator_assigned" if implied is not None else "unmapped",
    )


# --------------------------------------------------------------------------
# Pure measurement math
# --------------------------------------------------------------------------
def test_underreaction_math_matches_hand_calculation() -> None:
    m = measure_underreaction(signal(), MARKET, book("0.52", "120", "0.54", "110"), as_of=AS_OF)

    assert m.reason == "measured"
    assert m.pre_signal_mid == D("0.45") and m.current_mid == D("0.53") and m.implied_probability == D("0.70")
    assert m.full_move == D("0.25")
    assert m.observed_move == D("0.08")
    assert m.reaction_ratio == D("0.32")
    assert m.residual_to_fair == D("0.17")
    # literature: (1 - 0.64) * full_move
    assert m.literature_residual == D("0.36") * D("0.25")
    assert m.target_price == D("0.70")
    assert m.direction == "yes"
    assert m.age_seconds == D("180")


def test_partial_convergence_moves_the_target_toward_the_current_mid() -> None:
    params = UnderreactionParameters(convergence_fraction=D("0.5"))
    m = measure_underreaction(signal(), MARKET, book("0.52", "120", "0.54", "110"), as_of=AS_OF, parameters=params)
    assert m.target_price == D("0.53") + D("0.5") * D("0.17")


def test_negative_residual_points_to_no_side() -> None:
    m = measure_underreaction(signal(implied="0.05", pre="0.36"), MARKET, book("0.31", "150", "0.34", "175"), as_of=AS_OF)
    assert m.residual_to_fair == D("0.05") - D("0.325")
    assert m.direction == "no"
    assert m.reaction_ratio == (D("0.325") - D("0.36")) / (D("0.05") - D("0.36"))


def test_zero_full_move_has_no_ratio_but_still_a_residual() -> None:
    m = measure_underreaction(signal(implied="0.45", pre="0.45"), MARKET, book("0.52", "1", "0.54", "1"), as_of=AS_OF)
    assert m.full_move == D("0") and m.reaction_ratio is None
    assert m.residual_to_fair == D("0.45") - D("0.53")


def test_missing_pre_signal_mid_disables_ratio_only() -> None:
    m = measure_underreaction(signal(pre=None), MARKET, book("0.52", "1", "0.54", "1"), as_of=AS_OF)
    assert m.reason == "measured"
    assert m.reaction_ratio is None and m.literature_residual is None
    assert m.residual_to_fair == D("0.17")


@pytest.mark.parametrize(
    ("kwargs", "market", "bk", "reason"),
    [
        ({"implied": None, "age": 5000}, MARKET, book("0.52", "1", "0.54", "1"), "no_implied_probability"),
        ({"age": 901}, MARKET, book("0.52", "1", "0.54", "1"), "signal_stale"),
        ({"age": -5}, MARKET, book("0.52", "1", "0.54", "1"), "signal_in_future"),
        ({"confidence": "0.2"}, MARKET, book("0.52", "1", "0.54", "1"), "low_confidence"),
        ({}, None, OrderBook(market_id="M"), "market_not_in_snapshot"),
        ({"market_id": "OTHER"}, MARKET, book("0.52", "1", "0.54", "1"), "market_not_in_snapshot"),
        ({}, Market(Venue.KALSHI, "M", "t", active=False), book("0.52", "1", "0.54", "1"), "market_inactive"),
        ({}, MARKET, OrderBook(market_id="M"), "empty_book"),
    ],
)
def test_rails_report_the_first_blocking_reason(kwargs, market, bk, reason) -> None:
    m = measure_underreaction(signal(**kwargs), market, bk, as_of=AS_OF)
    assert m.reason == reason
    assert not m.measurable


def test_stale_signal_still_reports_its_ratio_for_the_record() -> None:
    m = measure_underreaction(signal(age=3600), MARKET, book("0.52", "1", "0.54", "1"), as_of=AS_OF)
    assert m.reason == "signal_stale"
    assert m.reaction_ratio == D("0.32")


def test_one_sided_book_uses_the_available_touch_as_current_mid() -> None:
    one_sided = OrderBook(market_id="M", asks=(PriceLevel(D("0.60"), D("5")),))
    m = measure_underreaction(signal(), MARKET, one_sided, as_of=AS_OF)
    assert m.current_mid == D("0.60")


# --------------------------------------------------------------------------
# Hypothetical paper trade
# --------------------------------------------------------------------------
def test_positive_residual_buys_yes_at_the_ask_with_news_metadata() -> None:
    strategy = NewsUnderreactionStrategy()
    evaluation = strategy.evaluate(signal(), MARKET, book("0.52", "120", "0.54", "110"), as_of=AS_OF)

    assert evaluation.traded and evaluation.reason == "trade"
    (order,) = evaluation.orders
    assert order.side is Side.BUY and order.outcome is Outcome.YES
    assert order.price == D("0.54") and order.quantity == D("10")
    # Kalshi fee buffer 0.02: 0.70 - 0.54 - 0.02
    assert evaluation.cost_adjusted_edge == D("0.14")
    assert order.metadata["strategy"] == "news_underreaction"
    assert order.metadata["price_signal_status"] == "news_signal_implied_probability"
    assert order.metadata["signal_id"] == "s1"
    assert order.metadata["reaction_ratio"] == "0.3200"
    assert order.metadata["residual_to_fair"] == "0.1700"
    assert order.metadata["literature_reference"] == "arXiv:2606.07811"


def test_negative_residual_sells_yes_at_the_bid() -> None:
    strategy = NewsUnderreactionStrategy()
    evaluation = strategy.evaluate(
        signal(implied="0.05", pre="0.36"), MARKET, book("0.31", "150", "0.34", "175"), as_of=AS_OF
    )
    (order,) = evaluation.orders
    assert order.side is Side.SELL and order.price == D("0.31")
    assert order.signed_quantity < 0


def test_small_residual_is_refused_before_touching_the_book() -> None:
    strategy = NewsUnderreactionStrategy()
    evaluation = strategy.evaluate(signal(implied="0.54", pre="0.50"), MARKET, book("0.52", "1", "0.54", "1"), as_of=AS_OF)
    assert evaluation.reason == "below_residual_threshold"
    assert evaluation.fair_value is None and not evaluation.traded


def test_refusals_propagate_from_measurement() -> None:
    strategy = NewsUnderreactionStrategy()
    evaluation = strategy.evaluate(signal(implied=None), MARKET, book("0.52", "1", "0.54", "1"), as_of=AS_OF)
    assert evaluation.reason == "no_implied_probability" and not evaluation.traded


def test_shared_portfolio_stops_re_entry_like_the_primary_track() -> None:
    portfolio = Portfolio()
    strategy = NewsUnderreactionStrategy(portfolio=portfolio)
    first = strategy.evaluate(signal(), MARKET, book("0.52", "120", "0.54", "110"), as_of=AS_OF)
    from core.types import Fill

    portfolio.apply_fill(Fill(Venue.KALSHI, "M", "o", Side.BUY, D("10"), D("0.54")))
    second = strategy.evaluate(signal(), MARKET, book("0.52", "120", "0.54", "110"), as_of=AS_OF)
    assert first.traded
    assert second.reason == "target_position_reached" and not second.traded


def test_parameters_are_validated() -> None:
    with pytest.raises(ValueError):
        UnderreactionParameters(literature_beta=D("0"))
    with pytest.raises(ValueError):
        UnderreactionParameters(convergence_fraction=D("1.5"))
    with pytest.raises(ValueError):
        UnderreactionParameters(max_signal_age_seconds=D("0"))


# --------------------------------------------------------------------------
# Signal interface
# --------------------------------------------------------------------------
def test_signal_requires_a_declared_mapping_iff_probability_present() -> None:
    with pytest.raises(ValueError):
        NewsSignal("x", Venue.KALSHI, "M", "h", AS_OF, "t", implied_probability=D("0.5"), mapping="unmapped")
    with pytest.raises(ValueError):
        NewsSignal("x", Venue.KALSHI, "M", "h", AS_OF, "t", implied_probability=None, mapping="operator_assigned")
    with pytest.raises(ValueError):
        NewsSignal("x", Venue.KALSHI, "M", "h", AS_OF.replace(tzinfo=None), "t")


def test_fixture_signals_are_synthetic_and_declared_as_such() -> None:
    signals = load_signals(FIXTURE_PATH, as_of=AS_OF, source="fixture", default_mapping="fixture_assigned")
    assert len(signals) == 8
    assert {s.mapping for s in signals} == {"fixture_assigned", "unmapped"}
    assert all(s.source == "fixture" for s in signals)
    unmapped = [s for s in signals if s.mapping == "unmapped"]
    assert len(unmapped) == 1 and unmapped[0].implied_probability is None
    note = json.loads(FIXTURE_PATH.read_text())["_note"]
    assert "SYNTHETIC" in note


async def test_operator_file_source_and_error_capture(tmp_path: Path) -> None:
    path = tmp_path / "signals.json"
    path.write_text(
        json.dumps(
            [
                {
                    "signal_id": "op-1",
                    "venue": "kalshi",
                    "market_id": "KX-FED-SEP-CUT",
                    "headline": "h",
                    "observed_at": "2026-09-14T11:58:00+00:00",
                    "implied_probability": "0.66",
                }
            ]
        )
    )
    batch = await JsonSignalSource(path).fetch(markets={}, as_of=AS_OF)
    assert batch.errors == []
    assert batch.signals[0].mapping == "operator_assigned"
    assert batch.signals[0].age_seconds(AS_OF) == D("120")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    batch = await JsonSignalSource(broken).fetch(markets={}, as_of=AS_OF)
    assert batch.signals == [] and len(batch.errors) == 1

    batch = await JsonSignalSource(tmp_path / "missing.json").fetch(markets={}, as_of=AS_OF)
    assert batch.signals == [] and "FileNotFoundError" in batch.errors[0]


def test_build_signal_source_defaults() -> None:
    assert isinstance(build_signal_source(use_fixtures=True), FixtureSignalSource)
    assert isinstance(build_signal_source(use_fixtures=False), NullSignalSource)
    assert isinstance(build_signal_source(use_fixtures=False, rss_urls=["https://x/feed"]), RssHeadlineSource)
    both = build_signal_source(use_fixtures=False, signals_path=Path("x.json"), rss_urls=["https://x/feed"])
    assert isinstance(both, CompositeSignalSource)


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>feed</title>
<item><title>Fed officials signal September rate cut is likely</title><link>https://x/1</link>
<pubDate>Mon, 14 Sep 2026 11:57:00 GMT</pubDate></item>
<item><title>Local bakery wins award</title><link>https://x/2</link></item>
</channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>a</title>
<entry><title>August CPI comes in below 3.0%</title><link href="https://x/3"/><updated>2026-09-14T11:50:00Z</updated></entry>
</feed>"""


def _fixture_markets() -> dict[Venue, list[Market]]:
    kalshi, _ = load_fixture(KALSHI_FIXTURE, Venue.KALSHI)
    poly, _ = load_fixture(POLY_FIXTURE, Venue.POLYMARKET)
    return {Venue.KALSHI: kalshi, Venue.POLYMARKET: poly}


async def test_rss_hook_matches_headlines_but_never_maps_a_probability() -> None:
    feeds = {"https://x/rss": RSS, "https://x/atom": ATOM}

    async def fake_get(url: str) -> str:
        return feeds[url]

    source = RssHeadlineSource(list(feeds), http_get=fake_get)
    batch = await source.fetch(markets=_fixture_markets(), as_of=AS_OF)

    assert batch.errors == []
    assert batch.signals, "the Fed and CPI headlines should match fixture markets on both venues"
    assert all(s.implied_probability is None and s.mapping == "unmapped" for s in batch.signals)
    matched_markets = {s.market_id for s in batch.signals}
    assert {"KX-FED-SEP-CUT", "0xfixture-fed-september", "KX-CPI-AUG-OVER3", "0xfixture-cpi-august"} <= matched_markets
    assert "KX-NBA-NY-BOS-NY" not in matched_markets
    fed = next(s for s in batch.signals if s.market_id == "KX-FED-SEP-CUT")
    assert fed.observed_at == datetime(2026, 9, 14, 11, 57, tzinfo=UTC) and fed.url == "https://x/1"
    # Deterministic ids across processes (no hash()).
    assert fed.signal_id == RssHeadlineSource.name + "-kalshi-KX-FED-SEP-CUT-" + fed.signal_id.rsplit("-", 1)[-1]
    assert len(fed.signal_id.rsplit("-", 1)[-1]) == 8


async def test_rss_network_failure_is_reported_not_raised() -> None:
    async def failing_get(url: str) -> str:
        raise ConnectionError("offline")

    batch = await RssHeadlineSource(["https://x/rss"], http_get=failing_get).fetch(markets=_fixture_markets(), as_of=AS_OF)
    assert batch.signals == []
    assert batch.errors and "ConnectionError" in batch.errors[0]


# --------------------------------------------------------------------------
# Track + ledger + artifact integration
# --------------------------------------------------------------------------
async def test_fixture_track_measures_residuals_and_books_paper_fills() -> None:
    summaries, ledgers = await measure_all_with_ledgers()
    news = next(s for s in summaries if s.track == NEWS_TRACK)
    ledger = ledgers[NEWS_TRACK]

    assert news.candidates == 8
    assert news.admitted == 3 and news.proposed_orders == 3 and news.paper_fills == 3
    assert news.refused_by_reason == {
        "below_edge_threshold": 1,
        "low_confidence": 1,
        "market_not_in_snapshot": 1,
        "no_implied_probability": 1,
        "signal_stale": 1,
    }
    assert news.metrics["status"] == "fixture_synthetic"
    assert news.metrics["mapping"]["counts"] == {"fixture_assigned": 7, "unmapped": 1}
    ratio = news.metrics["reaction_ratio"]
    assert ratio["n"] == 6 and ratio["literature"] == D("0.64")
    assert ratio["observed_mean"] == D("0.1750")
    assert news.metrics["literature"]["validated_here"] is False

    by_id = {row["signal_id"]: row for row in news.metrics["measurements"]}
    assert by_id["fx-fed-sep-cut-kalshi"]["reaction_ratio"] == D("0.3200")
    assert by_id["fx-fed-sep-cut-kalshi"]["residual_to_fair"] == D("0.1700")
    assert by_id["fx-fed-sep-cut-kalshi"]["literature_residual"] == D("0.0900")
    assert by_id["fx-cpi-aug-cool-print"]["side"] == "sell"
    assert by_id["fx-nba-injury-unmapped"]["reason"] == "no_implied_probability"

    # Ledger: 3 fills, marked at mid, identity holds, fees only on Kalshi.
    assert ledger.ledger_id == NEWS_TRACK and len(ledger.fills) == 3
    assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl
    assert ledger.fees_paid == D("0.18") + D("0.15")
    assert ledger.unrealized_pnl < 0  # bought the ask / sold the bid, marked at mid
    assert all(row["strategy"] == "news_underreaction" for row in news.fills)
    assert all("signal_id" in row for row in news.edges)
    assert news.metrics["hit_rate"] == D("1.0000")
    assert news.metrics["settlement_preview"]["scored_fills"] == 3


async def test_carried_ledger_does_not_re_enter() -> None:
    _, ledgers = await measure_all_with_ledgers()
    summaries2, ledgers2 = await measure_all_with_ledgers(ledgers=ledgers)
    news = next(s for s in summaries2 if s.track == NEWS_TRACK)
    assert news.paper_fills == 0
    assert news.refused_by_reason["target_position_reached"] == 3
    assert ledgers2[NEWS_TRACK].equity == ledgers[NEWS_TRACK].equity
    assert len(ledgers2[NEWS_TRACK].equity_curve) == 2


async def test_artifact_carries_news_findings_and_honesty_flag(tmp_path: Path) -> None:
    summaries, ledgers = await measure_all_with_ledgers()
    artifact = build_scoreboard_artifact(summaries, mode="fixtures", measured_at="t", limit=3)

    finding = artifact["findings"]["news_underreaction"]
    assert finding["status"] == "fixture_synthetic"
    assert finding["signals"] == 8 and finding["mapped"] == 7 and finding["unmapped"] == 1
    assert finding["paper_fills"] == 3 and finding["mapping_validated"] is False
    assert finding["reaction_ratio_observed_mean"] == D("0.1750")
    assert "news_signal_mapping_unvalidated" in artifact["portfolio"]["risk_flags"]
    assert NEWS_TRACK in artifact["portfolio"]["by_track"]
    assert NEWS_TRACK in {row["track"] for row in artifact["tracks"]}
    assert {row["track"] for row in artifact["charts"]["pnl_by_track"]} >= {NEWS_TRACK}
    assert artifact["totals"]["paper_pnl"] == sum((l.total_pnl for l in ledgers.values()), D("0")).quantize(D("0.0001"))
    json.dumps(artifact, default=json_default)

    persist_run(summaries, ledgers, artifact_dir=tmp_path, mode="fixtures", measured_at="t", limit=3, kalshi_env=None)
    assert (tmp_path / "paper" / f"ledger_{NEWS_TRACK}.json").exists()
    assert NEWS_TRACK in TRACKS


async def test_no_signal_source_is_an_honest_empty_lane() -> None:
    summaries, ledgers = await measure_all_with_ledgers(news_signals=NullSignalSource())
    news = next(s for s in summaries if s.track == NEWS_TRACK)
    assert news.candidates == 0 and news.paper_fills == 0 and news.refused_by_reason == {}
    assert news.metrics["status"] == "no_signal_source"
    assert ledgers[NEWS_TRACK].equity == ledgers[NEWS_TRACK].starting_cash
    artifact = build_scoreboard_artifact(summaries, mode="fixtures", measured_at="t", limit=3)
    assert artifact["findings"]["news_underreaction"]["status"] == "no_signal_source"
    assert "news_signal_mapping_unvalidated" not in artifact["portfolio"]["risk_flags"]


async def test_raising_source_does_not_take_the_scoreboard_down() -> None:
    class Exploding:
        name = "exploding"

        async def fetch(self, *, markets, as_of) -> SignalBatch:
            raise RuntimeError("boom")

    summaries, _ = await measure_all_with_ledgers(news_signals=Exploding())
    news = next(s for s in summaries if s.track == NEWS_TRACK)
    assert news.candidates == 0
    assert news.metrics["status"] == "signal_source_errors"
    assert "RuntimeError" in news.metrics["signal_source"]["errors"][0]
    assert len(summaries) == 6  # the other tracks still ran


async def test_rss_signals_in_the_track_are_counted_and_refused() -> None:
    async def fake_get(url: str) -> str:
        return RSS

    source = RssHeadlineSource(["https://x/rss"], http_get=fake_get)
    summaries, ledgers = await measure_all_with_ledgers(news_signals=source)
    news = next(s for s in summaries if s.track == NEWS_TRACK)
    assert news.candidates > 0 and news.paper_fills == 0
    assert set(news.refused_by_reason) == {"no_implied_probability"}
    assert news.metrics["status"] == "signals_unmapped"
    assert ledgers[NEWS_TRACK].equity == ledgers[NEWS_TRACK].starting_cash


async def test_measure_news_lane_report_is_complete() -> None:
    report = await measure_news_lane()
    assert report["paper_only"] is True and report["mode"] == "fixtures"
    assert report["candidates"] == 8 and report["paper_fills"] == 3
    assert report["status"] == "fixture_synthetic"
    assert len(report["not_validated"]) >= 4
    assert report["ledger"]["ledger_id"] == NEWS_TRACK


def test_cli_fixture_measurement_runs_and_writes_a_report(tmp_path: Path) -> None:
    out = tmp_path / "news.json"
    result = subprocess.run(
        [sys.executable, "-m", "research.news_underreaction", "--json", str(out)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "TRADING_MODE": "paper", "ENABLE_LIVE_TRADING": "false"},
    )
    assert result.returncode == 0, result.stderr
    assert "status=fixture_synthetic" in result.stdout
    assert "NOT validated" in result.stdout
    report = json.loads(out.read_text())
    assert report["candidates"] == 8 and report["admitted"] == 3
    assert report["reaction_ratio"]["observed_mean"] == pytest.approx(0.175)


def test_cli_refuses_live_flags(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "research.news_underreaction", "--json", str(tmp_path / "x.json")],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "TRADING_MODE": "live", "ENABLE_LIVE_TRADING": "true"},
    )
    assert result.returncode != 0
    assert not (tmp_path / "x.json").exists()
