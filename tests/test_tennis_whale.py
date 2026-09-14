import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from core.types import ONE, ZERO, Outcome, Side
from research.tennis_whale import (
    FIXTURE_PATH,
    TRACK_FAMILY,
    TennisMarket,
    TennisTape,
    build_report,
    cluster_bootstrap,
    evaluate_lag,
    harvest_tennis_tape,
    kill_rule,
    load_tape,
    replay_copy_tracks,
    tape_from_payload,
)
from strategies.tennis_whale_copy import (
    COPY_RISK_LIMITS,
    CopyParameters,
    TapePrint,
    WhaleTracker,
    copy_decision,
    lag_label,
    track_for_lag,
)

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _print(market: str, wallet: str, *, side: str = "BUY", outcome: Outcome = Outcome.YES, price: str, size: str, at: int) -> TapePrint:
    return TapePrint(market, wallet, Side(side.lower()), outcome, Decimal(price), Decimal(size), T0 + timedelta(seconds=at), tx=f"tx{at}")


# --------------------------------------------------------------------------
# strategy
# --------------------------------------------------------------------------
def test_print_long_outcome_and_yes_equivalent_price() -> None:
    buy_yes = _print("m", "w", side="BUY", outcome=Outcome.YES, price="0.40", size="10", at=0)
    sell_yes = _print("m", "w", side="SELL", outcome=Outcome.YES, price="0.40", size="10", at=0)
    buy_no = _print("m", "w", side="BUY", outcome=Outcome.NO, price="0.30", size="10", at=0)
    sell_no = _print("m", "w", side="SELL", outcome=Outcome.NO, price="0.30", size="10", at=0)
    assert buy_yes.long_outcome is Outcome.YES and buy_yes.yes_equivalent_price == Decimal("0.40")
    assert sell_yes.long_outcome is Outcome.NO and sell_yes.signed_quantity == Decimal("-10")
    assert buy_no.long_outcome is Outcome.NO and buy_no.yes_equivalent_price == Decimal("0.70")
    assert sell_no.long_outcome is Outcome.YES and sell_no.signed_quantity == Decimal("10")
    assert buy_yes.notional == Decimal("4.0")


def test_lag_labels_and_track_ids() -> None:
    assert [lag_label(l) for l in (30, 120, 600, 3600, 45)] == ["30s", "2m", "10m", "1h", "45s"]
    assert track_for_lag(120) == "tennis_whale_copy_2m"


def test_whale_qualifies_walk_forward_only_from_prior_history() -> None:
    params = CopyParameters(large_fill_notional=Decimal("500"), min_large_fills=3, min_markets=2)
    tracker = WhaleTracker(params)
    fills = [
        _print("m1", "w", price="0.50", size="1200", at=0),      # 600 large #1
        _print("m1", "w", price="0.50", size="1200", at=10),     # 600 large #2 (same market)
        _print("m2", "w", price="0.50", size="1200", at=20),     # 600 large #3, 2nd market -> qualifies after this
        _print("m3", "w", price="0.50", size="1000", at=30),     # first print after qualification
    ]
    decisions = []
    for p in fills:
        decisions.append(tracker.evaluate(p))
        tracker.observe(p)
    assert [d.reason for d in decisions] == ["not_whale", "not_whale", "not_whale", "signal"]
    assert tracker.stats("w").qualified_at == fills[2].timestamp
    assert tracker.whales()[0].wallet == "w"


def test_two_sided_wallets_are_refused_as_farmers_and_small_prints_are_not_signals() -> None:
    params = CopyParameters(large_fill_notional=Decimal("500"), min_large_fills=2, min_markets=2, signal_min_notional=Decimal("200"))
    tracker = WhaleTracker(params)
    for p in (
        _print("m1", "f", price="0.50", size="1200", at=0),
        _print("m1", "f", outcome=Outcome.NO, price="0.50", size="1200", at=1),
        _print("m2", "f", price="0.50", size="1200", at=2),
        _print("m2", "f", outcome=Outcome.NO, price="0.50", size="1200", at=3),
    ):
        tracker.observe(p)
    assert tracker.is_whale("f")
    assert tracker.stats("f").two_sided_share == Decimal("1")
    decision = tracker.evaluate(_print("m3", "f", price="0.50", size="1000", at=10))
    assert decision.reason == "two_sided_flow" and not decision.signal and decision.whale

    tracker2 = WhaleTracker(params)
    for p in (_print("m1", "w", price="0.50", size="1200", at=0), _print("m2", "w", price="0.50", size="1200", at=1)):
        tracker2.observe(p)
    small = tracker2.evaluate(_print("m3", "w", price="0.50", size="100", at=5))  # 50 USDC
    assert small.reason == "below_signal_notional"


def test_copy_decision_reference_slippage_size_cap_and_refusals() -> None:
    params = CopyParameters(stake_per_copy=Decimal("10"), slippage_ticks=1, max_wait_seconds=600, cooldown_seconds=300)
    signal = _print("m", "whale", side="SELL", outcome=Outcome.YES, price="0.40", size="1000", at=0)  # whale long NO
    reference = _print("m", "r", price="0.42", size="9", at=35)  # YES print at 0.42 -> NO at 0.58
    plan = copy_decision(signal, lag_seconds=30, reference=reference, params=params, tick_size=Decimal("0.01"), market_closed_at=None, last_copy_at=None)
    assert plan.copy and plan.order is not None
    assert plan.order.outcome is Outcome.NO and plan.order.side is Side.BUY
    assert plan.copy_price == Decimal("0.59")  # 0.58 + one tick against us
    assert plan.quantity == Decimal("9")  # floor(10/0.59)=16 capped by the print size 9
    assert plan.yes_equivalent_price == Decimal("0.41")
    assert plan.order.metadata["strategy"] == "tennis_whale_copy_30s"

    assert copy_decision(signal, lag_seconds=30, reference=None, params=params, tick_size=Decimal("0.01"), market_closed_at=None, last_copy_at=None).reason == "no_print_within_window"
    late = _print("m", "r", price="0.42", size="9", at=30 + 601)
    assert copy_decision(signal, lag_seconds=30, reference=late, params=params, tick_size=Decimal("0.01"), market_closed_at=None, last_copy_at=None).reason == "no_print_within_window"
    closed = copy_decision(signal, lag_seconds=30, reference=reference, params=params, tick_size=Decimal("0.01"), market_closed_at=T0 + timedelta(seconds=34), last_copy_at=None)
    assert closed.reason == "market_closed_before_copy"
    cooled = copy_decision(signal, lag_seconds=30, reference=reference, params=params, tick_size=Decimal("0.01"), market_closed_at=None, last_copy_at=T0 - timedelta(seconds=100))
    assert cooled.reason == "cooldown"
    extreme = _print("m", "r", price="0.01", size="9", at=35)  # NO at 0.99 + tick -> out of bounds
    assert copy_decision(signal, lag_seconds=30, reference=extreme, params=params, tick_size=Decimal("0.01"), market_closed_at=None, last_copy_at=None).reason == "price_out_of_bounds"
    tiny = _print("m", "r", price="0.42", size="0.5", at=35)
    assert copy_decision(signal, lag_seconds=30, reference=tiny, params=params, tick_size=Decimal("0.01"), market_closed_at=None, last_copy_at=None).reason == "size_below_one_contract"
    with pytest.raises(ValueError):
        copy_decision(signal, lag_seconds=30, reference=_print("m", "r", price="0.42", size="9", at=5), params=params, tick_size=Decimal("0.01"), market_closed_at=None, last_copy_at=None)


def test_parameters_validate() -> None:
    with pytest.raises(ValueError):
        CopyParameters(lags=())
    with pytest.raises(ValueError):
        CopyParameters(min_copy_price=Decimal("0.5"), max_copy_price=Decimal("0.4"))
    assert CopyParameters().as_dict()["lags_seconds"] == [30, 120, 600]


# --------------------------------------------------------------------------
# tape + replay
# --------------------------------------------------------------------------
def test_tape_parses_gamma_and_data_api_shapes() -> None:
    payload = {
        "meta": {"source": "network"},
        "markets": [
            {
                "conditionId": "0xabc", "question": "A vs B", "clobTokenIds": '["11", "22"]', "outcomePrices": '["1", "0"]',
                "umaResolutionStatus": "resolved", "closedTime": "2026-09-14 22:45:00+00", "feeType": "sports_fees_v3",
                "feesEnabled": True, "orderPriceMinTickSize": 0.001, "sportsMarketType": "moneyline", "volumeNum": 12345.6,
                "events": [{"slug": "atp-a-b"}], "closed": True,
            },
            {"conditionId": "0xopen", "question": "C vs D", "clobTokenIds": '["33", "44"]', "closed": False, "feeType": "sports_fees_v3", "feesEnabled": True},
        ],
        "trades": [
            {"conditionId": "0xabc", "proxyWallet": "0xWALLET", "side": "BUY", "outcomeIndex": 1, "price": 0.35, "size": 200, "timestamp": 1789425838, "transactionHash": "0x1"},
            {"conditionId": "0xabc", "proxyWallet": "0xWALLET", "side": "SELL", "outcomeIndex": 0, "price": 0.66, "size": 50, "timestamp": 1789425800},
            {"conditionId": "0xunknown", "proxyWallet": "0xz", "side": "BUY", "outcomeIndex": 0, "price": 0.5, "size": 1, "timestamp": 1789425800},
            {"conditionId": "0xabc", "proxyWallet": "0xz", "side": "HOLD", "outcomeIndex": 0, "price": 0.5, "size": 1, "timestamp": 1789425800},
        ],
    }
    tape = tape_from_payload(payload)
    assert tape.source == "network"
    m = tape.markets["0xabc"]
    assert m.resolved_outcome is Outcome.YES and m.closed and m.closed_at == datetime(2026, 9, 14, 22, 45, tzinfo=UTC)
    assert m.yes_token_id == "11" and m.no_token_id == "22" and m.tick_size == Decimal("0.001")
    assert m.taker_fee_rate == Decimal("0.05") and m.event_slug == "atp-a-b" and m.market_type == "moneyline"
    assert tape.markets["0xopen"].resolved_outcome is None and not tape.markets["0xopen"].closed
    assert len(tape.prints) == 2  # unknown market and bad side dropped
    assert tape.prints[0].timestamp < tape.prints[1].timestamp  # sorted ascending
    assert tape.prints[0].wallet == "0xwallet"  # lower-cased
    assert tape.prints[1].outcome is Outcome.NO and tape.prints[1].long_outcome is Outcome.NO
    market = m.as_market()
    assert market.active and market.metadata["taker_fee_rate"] == "0.05" and market.metadata["resolved_outcome"] == "yes"
    assert tape.first_print_at_or_after("0xabc", tape.prints[0].timestamp + timedelta(seconds=1)) is tape.prints[1]
    assert tape.last_print_before("0xabc", m.closed_at) is tape.prints[1]
    assert tape.last_print_before("0xabc", tape.prints[0].timestamp) is None


def test_fixture_is_labelled_synthetic_and_loads() -> None:
    raw = json.loads(FIXTURE_PATH.read_text())
    assert raw["_comment"].startswith("SYNTHETIC FIXTURE")
    tape = load_tape()
    assert tape.source == "fixture" and len(tape.markets) == 14 and len(tape.prints) > 500
    assert sum(1 for m in tape.markets.values() if m.resolved_outcome is not None) == 12


async def test_fixture_replay_books_copies_per_lag_through_the_ledger() -> None:
    tape = load_tape()
    result = await replay_copy_tracks(tape)
    whales = result.tracker.whales()
    assert [w.wallet[:8] for w in whales] == ["0xwhale0", "0xfarmer"]
    assert whales[1].two_sided_share == Decimal("1") and result.signal_reasons["two_sided_flow"] > 0
    assert len(result.signals) == 40
    for lag, rt in result.runtimes.items():
        s = rt.summary
        assert s.track == track_for_lag(lag) and s.candidates == 40
        assert s.paper_fills == s.admitted and s.paper_fills > 0
        assert s.refused_by_reason["cooldown"] == 25
        assert s.metrics["family"] == TRACK_FAMILY and s.metrics["lag_seconds"] == lag
        ledger = rt.ledger
        assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl
        assert ledger.unmarked_positions == 0
        assert s.ledger["settlement_fills"] > 0 and s.ledger["open_positions"] == 2  # two open fixture markets marked at the last print
        for fill in ledger.fills:
            if fill.order_id != "settlement":
                assert fill.quantity * fill.price <= COPY_RISK_LIMITS.max_notional_per_order
                assert fill.fee > ZERO  # sports taker fee applied
        assert all(row["paper_pnl"] is not None for row in s.fills)
    assert result.runtimes[600].summary.refused_by_reason["market_closed_before_copy"] == 1
    assert result.runtimes[600].summary.refused_by_reason["no_print_within_window"] == 1
    # every copy happened at or after signal + lag and buys the whale's long outcome
    for c in result.copies:
        assert c.copy_at >= c.signal_at + timedelta(seconds=c.lag_seconds)
        assert c.settlement_roi is not None or tape.markets[c.market_id].resolved_outcome is None
        assert c.stake == c.quantity * c.copy_price


async def test_honest_empty_when_no_wallet_qualifies_or_tape_is_empty() -> None:
    tape = load_tape()
    strict = CopyParameters(min_large_fills=1000)
    result = await replay_copy_tracks(tape, params=strict)
    assert result.tracker.whales() == [] and result.copies == [] and result.signals == []
    report = build_report(tape, result, mode="fixtures", run_id="r", measured_at="t")
    assert report["status"] == "no_tennis_whales_found" and report["overall_verdict"] == "INSUFFICIENT_DATA"
    assert report["kill_rule"]["status"] == "not_evaluable"
    assert all(r["verdict"] == "INSUFFICIENT_DATA" for r in report["lags"].values())
    assert all(t["paper_fills"] == 0 and t["ledger"]["total_pnl"] == ZERO for t in report["tracks"].values())

    empty = TennisTape(markets={}, prints=[], meta={"source": "network"})
    result = await replay_copy_tracks(empty)
    report = build_report(empty, result, mode="network", run_id="r", measured_at="t")
    assert report["status"] == "no_tennis_markets" and "INSUFFICIENT_DATA" in report["headline"]
    assert report["kalshi"]["verdict"] == "NOT_IDENTIFIABLE"


def test_cluster_bootstrap_treats_one_market_as_one_cluster() -> None:
    one_market = {"m1": [Decimal("0.2")] * 200}
    stat = cluster_bootstrap(one_market)
    assert stat["n"] == 200 and stat["n_clusters"] == 1 and stat["ci_low"] is None  # no CI from one cluster
    many = {f"m{i}": [Decimal("0.1") + Decimal(i % 3) / 100] for i in range(30)}
    stat = cluster_bootstrap(many, alpha=0.05, seed=1)
    assert stat["n"] == 30 and stat["n_clusters"] == 30 and stat["ci_low"] > 0 and stat["ci_low"] <= stat["mean"] <= stat["ci_high"]
    again = cluster_bootstrap(many, alpha=0.05, seed=1)
    assert again == stat  # deterministic
    mixed = {"big": [Decimal("0.5")] * 50, **{f"s{i}": [Decimal("-0.5")] for i in range(10)}}
    assert cluster_bootstrap(mixed)["effective_n_clusters"] < 2  # one dominant cluster


async def test_lag_verdicts_pass_fail_insufficient_and_kill_rule() -> None:
    tape = load_tape()
    result = await replay_copy_tracks(tape)
    pos = evaluate_lag(result.copies, lag=30, alpha=0.05, min_copies=5, min_markets=5, resamples=300, seed=3)
    assert pos["verdict"] == "PASS" and pos["settlement_roi"]["ci_low"] > 0
    insufficient = evaluate_lag(result.copies, lag=30, alpha=0.05, min_copies=1000, min_markets=5, resamples=300, seed=3)
    assert insufficient["verdict"] == "INSUFFICIENT_DATA"
    # flip every copy to the losing side: settlement and CLV both significantly negative -> FAIL at every lag -> kill
    for c in result.copies:
        if c.settlement_roi is not None:
            c.settlement_roi = -c.settlement_roi - Decimal("0.05")
        if c.clv_roi is not None:
            c.clv_roi = -c.clv_roi - Decimal("0.05")
    neg = [evaluate_lag(result.copies, lag=lag, alpha=0.05, min_copies=5, min_markets=5, resamples=300, seed=3) for lag in (30, 120, 600)]
    assert all(r["verdict"] == "FAIL" and r["significantly_negative_both"] for r in neg)
    assert kill_rule(neg)["triggered"] is True and kill_rule(neg)["components"]["copy_negative_all_lags"] is True
    assert kill_rule([insufficient])["status"] == "not_evaluable"
    assert kill_rule([neg[0], pos])["triggered"] is False
    # the whale-own benchmark is an independent trigger, but only with sufficient data
    own_negative = {"n": 50, "n_clusters": 12, "ci_high": -0.01, "sufficient": True}
    assert kill_rule([pos], own_negative)["triggered"] is True and "whale-own" in kill_rule([pos], own_negative)["reason"]
    assert kill_rule([pos], {**own_negative, "sufficient": False})["triggered"] is False
    assert kill_rule([insufficient], own_negative)["triggered"] is True


async def test_report_is_pre_registered_and_bonferroni_corrected() -> None:
    tape = load_tape()
    result = await replay_copy_tracks(tape)
    report = build_report(tape, result, mode="fixtures", run_id="r", measured_at="t", min_copies=10, min_markets=5, resamples=300)
    pre = report["pre_registration"]
    assert pre["inference"]["alpha_per_lag_bonferroni"] == pytest.approx(0.05 / 3)
    assert "kill" in pre["kill_rule"].lower() and "ci lower bound" in pre["pass_rule"].lower()
    assert report["status"] == "fixture_synthetic" and report["headline"].startswith("Synthetic fixture (not evidence)")
    assert report["overall_verdict"] == "PASS"
    assert {row["check"] for row in report["verdict_table"]} >= {"copy_ev_or_clv_positive_30s", "copy_portfolio_positive_at_any_lag", "kill_rule", "kalshi_whales_identifiable"}
    assert report["whales"]["qualified"] == 2 and report["whales"]["refused_two_sided"] == 1
    assert report["whales"]["top"][1]["refused_two_sided"] is True
    assert report["whale_own_benchmark"]["settlement_roi"]["n"] == 34
    assert report["copies_total"] == sum(r["copies"] for r in report["lags"].values()) == sum(sum(m["copies_by_lag"].values()) for m in report["copies_by_market"])
    assert report["copies_file"] == "tennis_whale_copies_latest.json"
    assert report["lags"]["30s"]["by_market_type"]["moneyline"]["copies"] == 14
    assert report["kill_rule"]["components"]["whale_own_evaluable"] is True and report["kill_rule"]["triggered"] is False
    assert report["fail_risks"] and any("exit_liquidity" in r for r in report["fail_risks"]) and any("farmers" in r for r in report["fail_risks"])
    assert report["assumptions"]["fixture"].startswith("research/fixtures/tennis_whale_tape.json is synthetic")


# --------------------------------------------------------------------------
# harvest (public endpoints only, mocked transport)
# --------------------------------------------------------------------------
async def test_harvest_reads_public_endpoints_paginates_and_records_kalshi_not_identifiable() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert "Authorization" not in request.headers and "POLY_API_KEY" not in request.headers
        path = request.url.path
        params = dict(request.url.params)
        if path == "/markets" and params.get("closed") == "true":
            if params.get("offset") == "0":
                return httpx.Response(200, json=[
                    {"conditionId": "0xr1", "question": "R1", "clobTokenIds": '["1","2"]', "outcomePrices": '["0","1"]', "umaResolutionStatus": "resolved", "closedTime": "2026-09-14 20:00:00+00", "volumeNum": 5000, "feeType": "sports_fees_v3", "feesEnabled": True, "closed": True},
                    {"conditionId": "0xtiny", "question": "tiny", "clobTokenIds": '["3","4"]', "outcomePrices": '["1","0"]', "umaResolutionStatus": "resolved", "volumeNum": 10, "closed": True},
                    {"conditionId": "0xpending", "question": "pending", "clobTokenIds": '["5","6"]', "umaResolutionStatus": "proposed", "volumeNum": 9000, "closed": True},
                ])
            return httpx.Response(200, json=[])
        if path == "/markets" and params.get("closed") == "false":
            return httpx.Response(200, json=[{"conditionId": "0xo1", "question": "O1", "clobTokenIds": '["7","8"]', "closed": False, "feeType": "sports_fees_v3", "feesEnabled": True, "volume24hr": 100}])
        if path == "/trades":
            assert params["takerOnly"] == "true"
            offset = int(params["offset"])
            market = params["market"]
            if market == "0xr1" and offset == 0:
                return httpx.Response(200, json=[{"proxyWallet": "0xW", "side": "BUY", "outcomeIndex": 0, "price": 0.5, "size": 10, "timestamp": 1789000000 + i} for i in range(2)])
            if market == "0xr1" and offset == 2:
                return httpx.Response(200, json=[{"proxyWallet": "0xW", "side": "SELL", "outcomeIndex": 1, "price": 0.4, "size": 5, "timestamp": 1789000100}])
            return httpx.Response(200, json=[])
        if path == "/trade-api/v2/series":
            return httpx.Response(200, json={"series": [{"ticker": "KXWTAGAME", "title": "WTA Tennis Winner"}, {"ticker": "KXNFLGAME", "title": "NFL Game"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        payload = await harvest_tennis_tape(
            resolved_markets=5, open_markets=5, min_volume=Decimal("1000"), max_trades_per_market=10, page_size=2,
            request_pause=0, http=http, gamma_url="https://gamma.test", data_api_url="https://data.test", kalshi_url="https://kalshi.test/trade-api/v2",
        )
    assert payload["meta"]["authenticated"] is False and payload["meta"]["source"] == "network"
    assert {m["market_id"] for m in payload["markets"]} == {"0xr1", "0xo1"}  # tiny volume and unresolved dropped
    assert len(payload["trades"]) == 3 and payload["meta"]["trades_per_market"] == {"0xr1": 3, "0xo1": 0}
    assert payload["meta"]["kalshi"]["tennis_series_count"] == 1 and payload["meta"]["kalshi"]["whale_identifiable"] is False
    assert not any("/order" in c or "/portfolio" in c for c in calls)
    tape = tape_from_payload(payload)
    assert tape.markets["0xr1"].resolved_outcome is Outcome.NO and len(tape.prints) == 3
    assert tape.prints[-1].long_outcome is Outcome.YES  # SELL NO == long YES


async def test_harvest_errors_are_recorded_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        payload = await harvest_tennis_tape(resolved_markets=1, open_markets=1, request_pause=0, http=http, gamma_url="https://g.test", data_api_url="https://d.test", kalshi_url="https://k.test")
    assert payload["markets"] == [] and payload["trades"] == []
    assert any(e.startswith("resolved_listing") for e in payload["meta"]["errors"])
    assert payload["meta"]["kalshi"]["checked"] is False and payload["meta"]["kalshi"]["whale_identifiable"] is False


def test_tennis_market_fee_rate_and_defaults() -> None:
    m = TennisMarket(market_id="x", question="q", fee_type="sports_fees_v3", fees_enabled=True)
    assert m.taker_fee_rate == Decimal("0.05")
    assert TennisMarket(market_id="y", question="q").taker_fee_rate == ZERO
    assert m.as_market().metadata["tick_size"] == "0.01" and m.as_market().venue.value == "polymarket"
    assert ONE - m.tick_size > Decimal("0.9")
