import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from core.ledger import PaperLedger
from core.types import Market, Outcome, Venue
from research.flb import KalshiFeeModel
from research.flb_expost import SettledMarket, TradeRecord, harvest_settled_trades, load_settled_trades
from research.scoreboard import VenueSnapshot
from research.tourist_fade import (
    MARKETS_FIXTURE_PATH,
    SETTLED_FIXTURE_PATH,
    TENNIS_SERIES,
    TOURIST_TRACK,
    UNIVERSES,
    ClusterStat,
    create_tourist_runtime,
    expost_report,
    fade_ev_verdict,
    fetch_market_result,
    fetch_recent_tape,
    finalize_tourist_metrics,
    not_measured_report,
    replay_market,
    replay_universe,
    run_fade_the_tourist_track,
    settle_resolved_positions,
    tape_from_rows,
    tape_from_settled,
    tourist_loses_verdict,
)
from strategies.tourist_flow import TOURIST_RISK_LIMITS, TapeTrade, TouristParameters
from venues.fixtures import load_fixture

D = Decimal
OPEN = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
CLOSE = OPEN + timedelta(hours=3)


def _settled(ticker: str, result: str, prints: list[tuple[float, str, str, str]], *, event: str | None = None, series: str = "KXATPMATCH", category: str = "sports", fee_type: str = "quadratic_with_maker_fees") -> SettledMarket:
    return SettledMarket(
        ticker=ticker,
        series_ticker=series,
        result=result,
        close_time=CLOSE,
        open_time=OPEN,
        event_ticker=event or ticker.rsplit("-", 1)[0],
        category=category,
        fee_type=fee_type,
        fee_multiplier=D("1"),
        trades=[TradeRecord(OPEN + timedelta(seconds=s), side, D(p), D(c)) for s, side, p, c in prints],
    )


def _longshot_burst(start: float, yes_price: str = "0.74", count: str = "32", n: int = 7) -> list[tuple[float, str, str, str]]:
    """Small NO buys (longshot at 1 - yes_price) that cross the cluster threshold."""
    return [(start + i * 30, "no", yes_price, count) for i in range(n)]


def _quiet(start: float, yes_price: str = "0.74", n: int = 10) -> list[tuple[float, str, str, str]]:
    return [(start + i * 120, "yes" if i % 2 else "no", yes_price, "300") for i in range(n)]


def test_tape_parsing_accepts_public_rows_and_compact_rows_and_sorts() -> None:
    rows = [
        {"created_time": "2026-09-13T21:54:39.283863Z", "taker_side": "no", "yes_price_dollars": "0.9900", "count_fp": "76.68", "trade_id": "b"},
        {"created_time": "2026-09-13T21:45:23Z", "taker_side": "yes", "yes_price": 96, "count": 5, "trade_id": "a"},
        {"t": "2026-09-13T21:50:00Z", "s": "yes", "p": "0.97", "c": "10"},
        {"t": "bad", "s": "yes", "p": "0.97", "c": "10"},
        {"t": "2026-09-13T21:51:00Z", "s": "maybe", "p": "0.97", "c": "10"},
        "not a row",
    ]
    tape = tape_from_rows(rows)
    assert [t.trade_id for t in tape] == ["a", None, "b"]
    assert tape[0].yes_price == D("0.96") and tape[0].count == D("5")
    assert tape[-1].taker_price == D("0.0100") and tape[-1].taker_notional == D("0.766800")


def test_replay_books_a_fade_per_cluster_and_settles_it_through_the_ledger() -> None:
    params = TouristParameters()
    ledger = PaperLedger(starting_cash=D("1000"), ledger_id="replay")
    # Favourite wins: two longshot bursts in the second half, one quiet stretch between.
    market = _settled("KXATPMATCH-26AUG01AAABBB-AAA", "yes", _quiet(0) + _longshot_burst(6000) + _quiet(6400, n=4) + _longshot_burst(7200))
    replay = replay_market(market, params, KalshiFeeModel(), ledger=ledger)
    assert replay.clusters == 2 and len(replay.fades) == 2 and replay.refusals == {}
    assert all(f.fade_side == "yes" and f.faded_side == "no" and f.regime == "strong" for f in replay.fades)
    assert replay.fades[0].at < replay.fades[1].at
    # Both fades settled through the ledger: two paper fills, one settlement fill, flat afterwards.
    assert ledger.summary()["fills"] == 3 and ledger.summary()["settlement_fills"] == 1 and ledger.open_positions == []
    assert ledger.realized_pnl.quantize(D("0.0001")) == sum(f.settlement_pnl for f in replay.fades)


def test_replay_fade_arithmetic_is_exact() -> None:
    params = TouristParameters()
    ledger = PaperLedger(starting_cash=D("1000"), ledger_id="replay")
    market = _settled("KXATPMATCH-26AUG01AAABBB-AAA", "yes", _longshot_burst(6000))
    replay = replay_market(market, params, KalshiFeeModel(), ledger=ledger)
    (fade,) = replay.fades
    # Tourists bought NO at 0.26; the fade buys YES at 1 - 0.26 + 2 ticks = 0.76 -> $20 strong fade / 0.76 = 26 contracts.
    assert fade.price == D("0.76") and fade.quantity == D("26") and fade.regime == "strong"
    assert fade.fee == KalshiFeeModel().fee(D("26"), D("0.76"), maker=False, market=market.as_market())
    assert fade.won is True
    assert fade.settlement_pnl == (D("26") * (D("1") - D("0.76")) - fade.fee).quantize(D("0.0001"))
    # The ledger realized exactly that, and the position is closed by a settlement fill.
    assert ledger.realized_pnl.quantize(D("0.0001")) == fade.settlement_pnl
    assert ledger.open_positions == [] and ledger.summary()["settlement_fills"] == 1
    assert ledger.equity == ledger.starting_cash + ledger.realized_pnl
    # Losing side: same tape but the longshot wins.
    ledger2 = PaperLedger(starting_cash=D("1000"), ledger_id="replay2")
    lost = replay_market(_settled("KXATPMATCH-26AUG01CCCDDD-CCC", "no", _longshot_burst(6000)), params, KalshiFeeModel(), ledger=ledger2)
    (fade2,) = lost.fades
    assert fade2.won is False and fade2.settlement_pnl == (-D("26") * D("0.76") - fade2.fee).quantize(D("0.0001"))
    assert ledger2.realized_pnl.quantize(D("0.0001")) == fade2.settlement_pnl


def test_replay_markouts_record_the_takers_bought_side() -> None:
    params = TouristParameters()
    ledger = PaperLedger(starting_cash=D("1000"), ledger_id="replay")
    # 7 small longshot NO buys at 0.26 in a market that settles YES: tourist settlement markout = -0.26 each.
    market = _settled("KXATPMATCH-26AUG01AAABBB-AAA", "yes", _longshot_burst(6000) + _quiet(7000, n=25))
    replay = replay_market(market, params, KalshiFeeModel(), ledger=ledger)
    contracts, value, n = replay.markouts["tourist|settlement"]
    assert n == 7 and contracts == 7 * 32 and abs(value / contracts + 0.26) < 1e-9
    # +5 prints later the tape is still at 0.74 -> NO side unchanged -> zero markout.
    c5, v5, n5 = replay.markouts["tourist|+5"]
    assert n5 == 7 and abs(v5) < 1e-9
    assert "combo:small+longshot|settlement" in replay.markouts
    assert replay.tourist_trades == 7 and replay.tape_trades == 32


def test_replay_caps_apply_per_market_and_are_reported() -> None:
    params = TouristParameters(cooldown_seconds=0)
    ledger = PaperLedger(starting_cash=D("1000"), ledger_id="replay")
    # Six bursts in a row: $20 per fade -> the $75 market cap is hit after the 3rd fade (3 x 19.76 = 59.28; 4th would be 79.04).
    prints: list[tuple[float, str, str, str]] = []
    for k in range(6):
        prints += _longshot_burst(6000 + k * 400)
    replay = replay_market(_settled("KXATPMATCH-26AUG01AAABBB-AAA", "yes", prints), params, KalshiFeeModel(), ledger=ledger)
    assert len(replay.fades) == 3
    assert set(replay.refusals) <= {"market_cap_reached", "no_position_headroom", "risk_position_cap_reached"} and sum(replay.refusals.values()) == replay.clusters - 3
    assert all(f.quantity * f.price <= TOURIST_RISK_LIMITS.max_notional_per_order for f in replay.fades)


def test_cluster_stat_clusters_by_event_and_reports_equal_weight_view() -> None:
    stat = ClusterStat()
    stat.add("mega", 1_000_000, -0.01 * 1_000_000)
    for i in range(9):
        stat.add(f"s{i}", 10, 0.05 * 10)
    s = stat.summary()
    assert s["n_events"] == 10 and s["effective_n_events"] < 1.01
    assert s["mean"] < 0 < s["mean_equal_weight"]
    assert ClusterStat().summary()["n_events"] == 0
    one = ClusterStat()
    one.add("only", 100, 5.0)
    assert one.summary()["clustered_se"] is None and one.summary()["t_stat"] is None


def test_fixture_replay_exercises_pass_fail_and_adverse_selection_paths() -> None:
    markets, meta = load_settled_trades(SETTLED_FIXTURE_PATH)
    assert meta["source"] == "fixture" and len(markets) == 30
    assert all(m.open_time is not None and m.event_ticker for m in markets)
    report = expost_report(markets, TouristParameters())
    assert report["status"] == "measured_from_settled_trades" and report["events"] == 30
    verdicts = report["verdicts"]
    assert verdicts["strong_regime_fade_ev_positive"]["verdict"] == "PASS"
    assert verdicts["strong_regime_beats_weak"]["verdict"] == "PASS"
    assert verdicts["fade_ev_positive_after_fees"]["verdict"] == "FAIL"  # weak-regime chase fades drag the pooled result below significance
    assert verdicts["tourist_flow_loses"]["verdict"] == "PASS" and report["adverse_selection"]["detected"] is False
    assert verdicts["tourist_worse_than_other_takers"]["verdict"] == "PASS"
    assert report["by_regime"]["strong"]["mean"] > 0 > report["by_regime"]["weak"]["mean"]
    # Synthetic crypto chasers are right: positive tourist markout in that category (the failure mode, isolated).
    crypto = report["adverse_selection"]["by_category"]["crypto"]["tourist"]["settlement"]
    sports = report["adverse_selection"]["by_category"]["sports"]["tourist"]["settlement"]
    assert crypto["mean"] > 0 > sports["mean"]
    # Ledger identity and fade bookkeeping agree.
    ledger = report["ledger"]
    assert D(str(ledger["equity"])) == D(str(ledger["starting_cash"])) + D(str(ledger["realized_pnl"])) + D(str(ledger["unrealized_pnl"]))
    assert ledger["open_positions"] == 0 and ledger["settlement_fills"] == report["fades"]["markets"]
    assert sum(D(str(r["settlement_pnl"])) for r in report["fade_rows"]) == D(str(report["fades"]["net_pnl"]))
    assert report["tape"]["tourist_share_of_trades"] < 0.5
    json.dumps(report, default=str)


def test_verdicts_are_insufficient_on_small_samples_and_fail_on_fair_prices() -> None:
    params = TouristParameters()
    few = expost_report([_settled(f"KXATPMATCH-26AUG0{i}AAABBB-AAA", "yes", _longshot_burst(6000)) for i in range(3)], params)
    assert few["verdicts"]["fade_ev_positive_after_fees"]["verdict"] == "INSUFFICIENT_DATA"
    assert few["verdicts"]["tourist_flow_loses"]["verdict"] == "INSUFFICIENT_DATA"
    assert few["verdicts"]["tourist_flow_loses"]["adverse_selection_detected"] is None
    # Fairly priced longshot: 26% of markets go to the longshot -> the fade has no edge and pays fees + spread.
    fair = [_settled(f"KXATPMATCH-26AUG{i:02d}AAABBB-AAA", "no" if i % 4 == 0 else "yes", _longshot_burst(6000)) for i in range(24)]
    report = expost_report(fair, params, min_events=10, min_fades=20)
    ev = report["verdicts"]["fade_ev_positive_after_fees"]
    assert ev["verdict"] == "FAIL" and ev["stats"]["fades"] == 24
    assert not (report["fades"]["mean"] > 0 and report["fades"]["t_stat"] >= 2)


def test_adverse_selection_is_detected_when_tourists_are_right() -> None:
    params = TouristParameters()
    # Every "tourist" longshot burst wins: informed flow dressed as retail.
    informed = [_settled(f"KXATPMATCH-26AUG{i:02d}AAABBB-AAA", "no", _longshot_burst(6000) + _quiet(7000, n=3)) for i in range(12)]
    # Add dispersion so a clustered SE exists.
    informed.append(_settled("KXATPMATCH-26AUG30ZZZYYY-ZZZ", "yes", _longshot_burst(6000)))
    report = expost_report(informed, params, min_events=10, min_fades=10)
    loses = report["verdicts"]["tourist_flow_loses"]
    assert loses["verdict"] == "FAIL" and loses["adverse_selection_detected"] is True
    assert "ADVERSE SELECTION" in loses["reason"]
    assert report["adverse_selection"]["detected"] is True
    assert report["verdicts"]["fade_ev_positive_after_fees"]["verdict"] == "FAIL"
    assert report["fades"]["mean"] < 0


def test_verdict_helpers_cover_edge_cases() -> None:
    assert fade_ev_verdict({"n_events": 0, "fades": 0}, scope="x", min_events=1, min_fades=1)["verdict"] == "INSUFFICIENT_DATA"
    positive_not_sig = {"n_events": 12, "fades": 30, "mean": 0.02, "t_stat": 1.1}
    assert fade_ev_verdict(positive_not_sig, scope="x", min_events=10, min_fades=20)["verdict"] == "FAIL"
    assert tourist_loses_verdict({}, min_events=10)["verdict"] == "INSUFFICIENT_DATA"
    flat = {"tourist": {"settlement": {"n_events": 20, "mean": -0.001, "t_stat": -0.3}, "+5": {}, "+20": {"mean": 0.02, "t_stat": 2.5}}}
    verdict = tourist_loses_verdict(flat, min_events=10)
    assert verdict["verdict"] == "FAIL" and verdict["adverse_selection_detected"] is True  # short-horizon informed
    missing = not_measured_report("no harvest")
    assert missing["status"] == "not_measured" and set(missing["verdicts"]) == {"fade_ev_positive_after_fees", "strong_regime_fade_ev_positive", "strong_regime_beats_weak", "tourist_flow_loses", "tourist_worse_than_other_takers"}
    assert "--harvest-trades" in missing["how_to_measure"]


def test_replay_universe_orders_by_close_time_and_shares_one_ledger() -> None:
    params = TouristParameters()
    a = _settled("KXATPMATCH-26AUG02AAABBB-AAA", "yes", _longshot_burst(6000))
    b = _settled("KXATPMATCH-26AUG01CCCDDD-CCC", "yes", _longshot_burst(6000))
    b.close_time = CLOSE - timedelta(days=1)
    replays, ledger = replay_universe([a, b], params)
    assert [r.ticker for r in replays] == [b.ticker, a.ticker]
    assert ledger.summary()["fills"] == 4 and len(ledger.equity_curve) == 1


def _snapshot_from_fixture() -> VenueSnapshot:
    markets, books = load_fixture(MARKETS_FIXTURE_PATH, Venue.KALSHI)
    return VenueSnapshot(venue=Venue.KALSHI, source="fixture", markets=markets, books=books)


async def test_live_track_fades_fresh_clusters_through_the_risk_gated_engine() -> None:
    snapshot = _snapshot_from_fixture()
    runtime = create_tourist_runtime({Venue.KALSHI: snapshot}, ledger=None, starting_cash=D("1000"), model_fees=True)
    summary = await run_fade_the_tourist_track(runtime, params=TouristParameters())
    runtime.finalize(label="test")
    finalize_tourist_metrics(runtime)
    assert summary.track == TOURIST_TRACK and summary.candidates == 7
    assert summary.admitted == 2 and summary.paper_fills == 2
    assert summary.refused_by_reason == {"cluster_stale": 1, "market_inactive": 1, "no_tape": 1, "no_tourist_cluster": 1, "one_sided_book": 1}
    by_market = {row["market"]: row for row in summary.fills}
    strong = by_market["KXATPMATCH-26SEP20ALCSIN-ALC"]
    assert strong["outcome"] == "yes" and strong["price"] == D("0.76") and strong["regime"] == "strong" and strong["faded_side"] == "no"
    weak = by_market["KXWTAMATCH-26SEP20SABGAU-SAB"]
    assert weak["outcome"] == "no" and weak["price"] == D("0.55") and weak["regime"] == "weak"
    assert all(row["fee"] > 0 for row in summary.fills)
    assert set(summary.metrics["paper_pnl_by_regime"]) == {"strong", "weak"}
    assert summary.metrics["tape"]["clusters"] == 4 and summary.metrics["family"] == "tourist_fade"
    assert runtime.risk.limits == TOURIST_RISK_LIMITS
    ledger = runtime.ledger.summary()
    assert D(str(ledger["equity"])) == D(str(ledger["starting_cash"])) + D(str(ledger["realized_pnl"])) + D(str(ledger["unrealized_pnl"]))
    assert ledger["open_positions"] == 2 and ledger["mark_method"] == "mid"
    # A second pass over the same snapshot with the carried ledger does not re-fade.
    runtime2 = create_tourist_runtime({Venue.KALSHI: snapshot}, ledger=runtime.ledger, starting_cash=D("1000"), model_fees=True)
    summary2 = await run_fade_the_tourist_track(runtime2, params=TouristParameters())
    assert summary2.paper_fills == 0 and summary2.refused_by_reason["already_positioned"] == 2


async def test_live_track_without_kalshi_snapshot_is_an_honest_empty_row() -> None:
    poly = VenueSnapshot(venue=Venue.POLYMARKET, source="fixture")
    runtime = create_tourist_runtime({Venue.POLYMARKET: poly}, ledger=None, starting_cash=D("1000"), model_fees=True)
    summary = await run_fade_the_tourist_track(runtime)
    assert summary.candidates == 0 and "No Kalshi snapshot" in summary.notes


def test_settle_resolved_positions_closes_only_markets_with_a_result() -> None:
    from core.types import Fill, Side

    ledger = PaperLedger(starting_cash=D("1000"), ledger_id="t")
    ledger.record_fill(Fill(venue=Venue.KALSHI, market_id="A", order_id="1", side=Side.BUY, quantity=D("10"), price=D("0.76"), outcome=Outcome.YES, fee=D("0.10")))
    ledger.record_fill(Fill(venue=Venue.KALSHI, market_id="B", order_id="2", side=Side.BUY, quantity=D("10"), price=D("0.55"), outcome=Outcome.NO, fee=D("0.10")))
    rows = settle_resolved_positions(ledger, {"A": Outcome.YES}.get)
    assert [r["market"] for r in rows] == ["A"] and rows[0]["realized_pnl"] == D("2.4000")  # 10 * (1 - 0.76) ; fee was already realized
    assert [p.market_id for p in ledger.open_positions] == ["B"]
    assert settle_resolved_positions(ledger, lambda _: None) == []


async def test_public_reads_use_only_market_endpoints() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/markets/trades"):
            if request.url.params.get("cursor"):
                return httpx.Response(200, json={"cursor": "", "trades": [{"created_time": "2026-09-13T21:40:00Z", "taker_side": "yes", "yes_price_dollars": "0.9500", "count_fp": "3.00", "trade_id": "old"}]})
            return httpx.Response(200, json={"cursor": "p2", "trades": [{"created_time": "2026-09-13T21:54:39Z", "taker_side": "no", "yes_price_dollars": "0.9900", "count_fp": "76.68", "trade_id": "new"}]})
        if request.url.path.endswith("/markets/KXATPMATCH-26SEP13ZVESHE-ZVE"):
            return httpx.Response(200, json={"market": {"ticker": "KXATPMATCH-26SEP13ZVESHE-ZVE", "result": "yes"}})
        if request.url.path.endswith("/markets/OPEN"):
            return httpx.Response(200, json={"market": {"ticker": "OPEN", "result": "", "status": "open"}})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        tape = await fetch_recent_tape(http, "https://api.elections.kalshi.com/trade-api/v2", "KXATPMATCH-26SEP13ZVESHE-ZVE", limit=2000, max_pages=3)
        assert [t.trade_id for t in tape] == ["old", "new"]  # chronological
        assert await fetch_market_result(http, "https://api.elections.kalshi.com/trade-api/v2", "KXATPMATCH-26SEP13ZVESHE-ZVE") is Outcome.YES
        assert await fetch_market_result(http, "https://api.elections.kalshi.com/trade-api/v2", "OPEN") is None
        payload = await harvest_settled_trades(kalshi_env="prod", series=("KXATPMATCH",), settled_per_series=2, max_trades_per_market=5, http=http)
        assert payload["series_requested"] == ["KXATPMATCH"]
    assert calls and all("demo" not in url for url in calls)
    assert not any("/orders" in url or "/portfolio" in url for url in calls)


def test_universes_are_tennis_first_with_crypto_optional() -> None:
    assert UNIVERSES["tennis"] == TENNIS_SERIES and "KXATPMATCH" in TENNIS_SERIES
    assert set(UNIVERSES["crypto"]) == {"KXBTC15M", "KXETH15M"}
    assert set(UNIVERSES["both"]) == set(TENNIS_SERIES) | set(UNIVERSES["crypto"])


def test_tape_from_settled_drops_malformed_and_sorts() -> None:
    market = _settled("X", "yes", [(100, "yes", "0.5", "1"), (10, "no", "0.5", "2")])
    market.trades.append(TradeRecord(OPEN, "maybe", D("0.5"), D("1")))
    tape = tape_from_settled(market)
    assert [t.count for t in tape] == [D("2"), D("1")] and all(isinstance(t, TapeTrade) for t in tape)


def test_settled_market_proxy_carries_event_ticker_for_clustering() -> None:
    market = _settled("KXATPMATCH-26AUG01AAABBB-AAA", "yes", [])
    proxy = market.as_market()
    assert isinstance(proxy, Market) and proxy.metadata["event_ticker"] == "KXATPMATCH-26AUG01AAABBB"
    assert proxy.metadata["fee_type"] == "quadratic_with_maker_fees"
