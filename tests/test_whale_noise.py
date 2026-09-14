import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from apps.measure_all import load_ledgers
from apps.measure_whale_noise import build_parser, parameters_from_args, run
from core.ledger import PaperLedger
from core.risk import RiskManager
from core.types import Market, OrderBook, Outcome, Position, PriceLevel, Venue
from research.flb_expost import load_settled_trades
from research.whale_noise import (
    COMBINED,
    EXPOST_FIXTURE_PATH,
    MAKER_LEG,
    TAKER_LEG,
    WHALE_NOISE_TRACKS,
    MarketTimeline,
    combined_verdict,
    load_fixture_timelines,
    run_whale_noise_tracks,
    timelines_from_archive,
    timelines_from_network,
    whale_flow_expost,
)
from strategies.flb import FLB_RISK_LIMITS
from strategies.whale_noise import (
    ConservativeQueueModel,
    Print,
    PrintClass,
    WhaleNoiseParameters,
    WhaleRule,
    classify_print,
    displayed_at_level,
    load_whale_registry,
    whale_follow_quote,
)

D = Decimal
T0 = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _market(market_id: str = "KXWTAMATCH-26SEP15AAABBB-AAA", **meta: object) -> Market:
    return Market(venue=Venue.KALSHI, market_id=market_id, title=market_id, metadata={"series_ticker": "KXWTAMATCH", **meta})


def _book(bids: list[tuple[str, str]], asks: list[tuple[str, str]], market_id: str = "KXWTAMATCH-26SEP15AAABBB-AAA") -> OrderBook:
    return OrderBook(
        market_id=market_id,
        bids=tuple(PriceLevel(D(p), D(s)) for p, s in bids),
        asks=tuple(PriceLevel(D(p), D(s)) for p, s in asks),
    )


def _print(seconds: float, side: str, yes_price: str, size: str, trade_id: str = "", block: bool = False) -> Print:
    return Print(ts=_t(seconds), yes_price=D(yes_price), size=D(size), taker_side=side, trade_id=trade_id or f"t{seconds}", block=block)


# ------------------------------------------------------------ classification
def test_whale_is_a_size_class_not_an_identity() -> None:
    params = WhaleNoiseParameters()
    history = [D("20"), D("30"), D("25"), D("40"), D("35")]  # median 30 -> 10x = 300
    assert classify_print(_print(60, "yes", "0.62", "1200"), history, params) is PrintClass.WHALE_LIFT
    # absolute floor: 900 contracts is below the 1000 floor even though it is 30x the median
    assert classify_print(_print(60, "yes", "0.62", "900"), history, params) is PrintClass.RETAIL_OTHER
    # relative floor: with a median of 150 a 1200 lot is only 8x
    assert classify_print(_print(60, "yes", "0.62", "1200"), [D("150")] * 5, params) is PrintClass.RETAIL_OTHER
    # notional floor: 1200 contracts at 5c is $60
    assert classify_print(_print(60, "yes", "0.05", "1200"), history, params) is PrintClass.RETAIL_LONGSHOT
    # fewer than min_history prints: only the absolute floors apply
    assert classify_print(_print(60, "yes", "0.62", "1200"), [D("500")], params) is PrintClass.WHALE_LIFT
    # block trades are negotiated off-book: never a whale lift, never noise
    assert classify_print(_print(60, "yes", "0.62", "5000", block=True), history, params) is PrintClass.BLOCK_TRADE
    # retail longshot flow is judged on the price the taker paid, on either side
    assert classify_print(_print(60, "yes", "0.08", "10"), history, params) is PrintClass.RETAIL_LONGSHOT
    assert classify_print(_print(60, "no", "0.85", "10"), history, params) is PrintClass.RETAIL_LONGSHOT  # paid 0.15 for NO
    assert classify_print(_print(60, "no", "0.15", "10"), history, params) is PrintClass.RETAIL_OTHER  # paid 0.85 for NO
    assert _print(60, "no", "0.15", "10").direction == -1 and _print(60, "yes", "0.15", "10").direction == 1


def test_whale_registry_is_operator_input_and_defaults_stay_unverified() -> None:
    assert WhaleRule().ev_status == "unverified"
    default, series = load_whale_registry({"default": {"min_contracts": 2000}, "series": {"KXWTAMATCH": {"ev_status": "operator_asserted", "note": "desk view"}}})
    assert default.min_contracts == D("2000") and default.ev_status == "unverified"
    assert series["KXWTAMATCH"].min_contracts == D("2000") and series["KXWTAMATCH"].ev_status == "operator_asserted"
    params = WhaleNoiseParameters(default_rule=default, series_rules=series)
    assert params.rule_for("KXWTAMATCH").ev_status == "operator_asserted"
    assert params.rule_for("KXATPMATCH").ev_status == "unverified"
    with pytest.raises(ValueError):
        WhaleNoiseParameters(ticks_behind=0)  # the maker never joins or improves the whale's level


# ------------------------------------------------------------- quote builder
def test_yes_whale_rests_a_yes_bid_one_tick_behind_the_pre_impact_touch() -> None:
    params = WhaleNoiseParameters()
    book = _book([("0.60", "300"), ("0.59", "150")], [("0.62", "500")])
    ev = whale_follow_quote(_market(), book, _print(60, "yes", "0.62", "1200", "w"), params, position=None, risk=RiskManager(FLB_RISK_LIMITS), total_cash_at_risk=D("0"))
    assert ev.traded and ev.quote is not None and ev.order is not None
    q = ev.quote
    assert q.outcome is Outcome.YES and q.yes_price == D("0.59") and q.price == D("0.59")
    assert q.quantity == D("42")  # floor($25 / 0.59)
    assert q.queue_ahead == q.queue_ahead_initial == D("150")  # behind everything displayed at 0.59
    assert q.active_from == _t(62) and q.expires_at == _t(362)
    assert ev.order.notional <= FLB_RISK_LIMITS.max_notional_per_order
    assert ev.order.metadata["placement"] == "behind" and ev.order.metadata["ev_status"] == "unverified"
    # two ticks behind, and a level with nothing displayed has an empty queue
    ev2 = whale_follow_quote(_market(), book, _print(60, "yes", "0.62", "1200"), WhaleNoiseParameters(ticks_behind=2), position=None, risk=None, total_cash_at_risk=D("0"))
    assert ev2.quote is not None and ev2.quote.yes_price == D("0.58") and ev2.quote.queue_ahead == D("0")


def test_no_whale_rests_a_no_bid_behind_the_no_touch() -> None:
    params = WhaleNoiseParameters()
    book = _book([("0.33", "200")], [("0.35", "250"), ("0.36", "100")])
    ev = whale_follow_quote(_market(), book, _print(60, "no", "0.33", "1000", "w"), params, position=None, risk=None, total_cash_at_risk=D("0"))
    assert ev.quote is not None
    q = ev.quote
    # NO best bid is 1 - 0.35 = 0.65; one tick behind is a NO bid at 0.64 = a YES ask at 0.36
    assert q.outcome is Outcome.NO and q.yes_price == D("0.36") and q.price == D("0.64")
    assert q.quantity == D("39") and q.queue_ahead == D("100")  # 100 displayed at the 0.36 YES ask
    assert displayed_at_level(book, Outcome.NO, D("0.36")) == D("100")


def test_quote_refusals_are_explicit() -> None:
    params = WhaleNoiseParameters()
    whale = _print(60, "yes", "0.62", "1200")
    assert whale_follow_quote(_market(), _book([], [("0.62", "5")]), whale, params, position=None, risk=None, total_cash_at_risk=D("0")).reason == "one_sided_book"
    assert whale_follow_quote(_market(), _book([("0.01", "5")], [("0.03", "5")]), whale, params, position=None, risk=None, total_cash_at_risk=D("0")).reason == "touch_at_bound"
    book = _book([("0.60", "300")], [("0.62", "500")])
    assert whale_follow_quote(_market(), book, whale, params, position=None, risk=None, total_cash_at_risk=D("0"), active_quotes_in_market=1).reason == "quote_already_resting"
    full = Position(venue=Venue.KALSHI, market_id="KXWTAMATCH-26SEP15AAABBB-AAA", quantity=D("75"), average_price=D("0.995"))  # $74.63 at risk
    assert whale_follow_quote(_market(), book, whale, params, position=full, risk=None, total_cash_at_risk=D("74.625")).reason == "market_cap_reached"
    assert whale_follow_quote(_market(), book, whale, params, position=None, risk=None, total_cash_at_risk=D("999.90")).reason == "capital_cap_reached"
    capped = Position(venue=Venue.KALSHI, market_id="KXWTAMATCH-26SEP15AAABBB-AAA", quantity=D("75"), average_price=D("0.10"))
    assert whale_follow_quote(_market(), book, whale, params, position=capped, risk=RiskManager(FLB_RISK_LIMITS), total_cash_at_risk=D("7.5")).reason == "risk_position_cap_reached"
    # A whale print that already sits on our side of the quote means the book is stale.
    assert whale_follow_quote(_market(), book, _print(60, "yes", "0.59", "1200"), params, position=None, risk=None, total_cash_at_risk=D("0")).reason == "stale_book_crossed"
    assert whale_follow_quote(_market(), _book([("0.33", "200")], [("0.35", "250")]), _print(60, "no", "0.36", "1000"), params, position=None, risk=None, total_cash_at_risk=D("0")).reason == "stale_book_crossed"
    strict = WhaleNoiseParameters(require_ev_verified=True)
    assert whale_follow_quote(_market(), book, whale, strict, position=None, risk=None, total_cash_at_risk=D("0")).reason == "whale_ev_unverified"
    asserted = WhaleNoiseParameters(require_ev_verified=True, series_rules={"KXWTAMATCH": WhaleRule(ev_status="operator_asserted")})
    assert whale_follow_quote(_market(), book, whale, asserted, position=None, risk=None, total_cash_at_risk=D("0")).traded


# ---------------------------------------------------------- queue fill model
def _resting_yes_bid() -> tuple[ConservativeQueueModel, object]:
    params = WhaleNoiseParameters()
    book = _book([("0.60", "300"), ("0.59", "150")], [("0.62", "500")])
    ev = whale_follow_quote(_market(), book, _print(60, "yes", "0.62", "1200", "w"), params, position=None, risk=None, total_cash_at_risk=D("0"))
    assert ev.quote is not None
    return ConservativeQueueModel(params), ev.quote


def test_queue_model_never_fills_ahead_of_displayed_size() -> None:
    model, q = _resting_yes_bid()
    assert model.on_print(q, _print(61, "no", "0.59", "50")) == 0  # inside reaction latency
    assert model.on_print(q, _print(90, "yes", "0.59", "500")) == 0  # a YES buyer never fills a YES bid
    assert model.on_print(q, _print(90, "no", "0.60", "100")) == 0  # hit the touch, not our level
    assert model.on_print(q, _print(120, "no", "0.59", "100")) == 0 and q.queue_ahead == D("50")  # eats the queue ahead only
    assert model.on_print(q, _print(150, "no", "0.59", "80.9")) == D("30") and q.queue_ahead == D("0")  # 50 ahead, floor(30.9) to us
    assert q.filled == D("30") and q.remaining == D("12") and q.status == "resting"


def test_trade_through_fills_at_most_the_printed_size() -> None:
    model, q = _resting_yes_bid()
    q.queue_ahead = D("0")
    assert model.on_print(q, _print(100, "no", "0.58", "5")) == D("5")  # through our level, but only a 5 lot
    assert q.trade_through is True and q.remaining == D("37")
    assert model.on_print(q, _print(110, "no", "0.57", "500")) == D("37")
    assert q.status == "filled" and q.filled == q.quantity
    assert model.on_print(q, _print(120, "no", "0.57", "500")) == 0  # nothing left


def test_later_books_only_lengthen_the_queue_and_ttl_expires_the_quote() -> None:
    model, q = _resting_yes_bid()
    model.on_book(q, _book([("0.60", "300"), ("0.59", "400")], [("0.62", "500")]))
    assert q.queue_ahead == D("400") and q.queue_raised_by_books == 1
    model.on_book(q, _book([("0.60", "300"), ("0.59", "10")], [("0.62", "500")]))  # cancels ahead are never assumed
    assert q.queue_ahead == D("400")
    assert model.on_print(q, _print(362, "no", "0.50", "1000")) == 0  # expires_at is exclusive
    q.expire(_t(362))
    assert q.status == "expired" and q.reserved_collateral() == D("0")
    lax = WhaleNoiseParameters(later_arrivals_ahead=False)
    _, q2 = _resting_yes_bid()
    ConservativeQueueModel(lax).on_book(q2, _book([("0.59", "400")], [("0.62", "5")]))
    assert q2.queue_ahead == D("150")


def test_no_bid_is_filled_by_yes_buyers_at_or_above_our_yes_price() -> None:
    params = WhaleNoiseParameters()
    book = _book([("0.33", "200")], [("0.35", "250"), ("0.36", "100")])
    ev = whale_follow_quote(_market(), book, _print(60, "no", "0.33", "1000", "w"), params, position=None, risk=None, total_cash_at_risk=D("0"))
    assert ev.quote is not None
    model, q = ConservativeQueueModel(params), ev.quote
    assert model.on_print(q, _print(100, "yes", "0.35", "60")) == 0  # lifted 0.35, our ask is 0.36
    assert model.on_print(q, _print(100, "no", "0.36", "60")) == 0  # a NO buyer cannot fill a NO bid
    assert model.on_print(q, _print(130, "yes", "0.36", "130")) == D("30")  # 100 ahead, 30 to us
    assert model.on_print(q, _print(170, "yes", "0.37", "20")) == D("9") and q.status == "filled"


# ---------------------------------------------------------------- replay
@pytest.fixture(scope="module")
def fixture_run() -> tuple[dict, dict, object]:
    import asyncio

    timelines, _ = load_fixture_timelines()
    summaries, ledgers, replay = asyncio.run(run_whale_noise_tracks(timelines))
    return {s.track: s for s in summaries}, ledgers, replay


def test_fixture_replay_counts_are_deterministic(fixture_run) -> None:
    by_track, ledgers, replay = fixture_run
    assert set(by_track) == set(WHALE_NOISE_TRACKS)
    assert replay.prints_seen == 48
    assert replay.print_classes == {"block_trade": 1, "retail_longshot": 7, "retail_other": 35, "whale_lift": 5}
    combined, maker, taker = by_track[COMBINED], by_track[MAKER_LEG], by_track[TAKER_LEG]

    assert (combined.candidates, combined.admitted, combined.proposed_orders, combined.paper_fills) == (12, 7, 7, 8)
    assert combined.refused_by_reason == {"taker_cooldown": 1, "fade_risk_position_cap_reached": 1, "risk_position_cap_reached": 1, "whale_flow_in_lookback": 2}
    assert (maker.candidates, maker.admitted, maker.paper_fills) == (5, 5, 7) and maker.refused_by_reason == {}
    assert (taker.candidates, taker.admitted, taker.paper_fills) == (7, 3, 3)
    assert taker.refused_by_reason == {"taker_cooldown": 1, "fade_risk_position_cap_reached": 1, "whale_flow_in_lookback": 2}

    m = maker.metrics["maker"]
    assert (m["quotes_placed"], m["quotes_filled"], m["quotes_expired_unfilled"]) == (5, 4, 1)
    assert (m["contracts_quoted"], m["contracts_filled"], m["fill_rate_contracts"]) == (D("189"), D("153"), D("0.8095"))
    assert m["queue_raised_by_later_books"] == 1 and m["trade_through_fills"] == 5
    quotes = {q["market"][-3:]: q for q in maker.metrics["quotes"]}
    assert quotes["AAA"]["status"] == "filled" and [(f["quantity"], f["trade_id"]) for f in quotes["AAA"]["fills"]] == [(D("30"), "A-partial"), (D("12"), "A-through")]
    assert quotes["AAA"]["queue_ahead_initial"] == D("150") and quotes["AAA"]["consumed_ahead"] == D("150")
    assert quotes["III"]["status"] == "expired" and quotes["III"]["filled"] == 0 and quotes["III"]["queue_raised_by_books"] == 1 and quotes["III"]["queue_ahead_final"] == D("30")
    assert quotes["EEE"]["outcome"] == "no" and [f["quantity"] for f in quotes["EEE"]["fills"]] == [D("7"), D("19")]  # trade-through capped at the 7 lot

    # The combined book refused C's whale quote because the taker leg already held the 75-contract cap there.
    cm = combined.metrics["maker"]
    assert cm["quotes_placed"] == 4 and cm["contracts_filled"] == D("127")
    assert {q["market"][-3:] for q in combined.metrics["quotes"]} == {"AAA", "CCC", "GGG", "III"}
    t = taker.metrics["taker"]
    assert (t["retail_longshot_triggers"], t["fades_proposed"], t["fill_events"], t["contracts_filled"]) == (7, 3, 3, D("75"))


def test_fixture_replay_verdict_fill_rate_and_toxicity(fixture_run) -> None:
    by_track, ledgers, replay = fixture_run
    combined, maker, taker = by_track[COMBINED], by_track[MAKER_LEG], by_track[TAKER_LEG]
    verdict = combined.metrics["combined_vs_legs"]
    assert verdict["verdict"] == "PASS"
    assert verdict["combined_pnl"] == D(str(combined.ledger["total_pnl"])) == D("2.95920")
    assert verdict["maker_leg_pnl"] == D("2.47490") and verdict["taker_leg_pnl"] == D("0.74430")
    assert verdict["interaction_pnl"] == D("-0.26000")  # the whale quote the shared inventory cap refused
    assert verdict["combined_pnl"] > max(verdict["maker_leg_pnl"], verdict["taker_leg_pnl"])
    assert maker.metrics["combined_vs_legs"] == verdict

    tox = maker.metrics["maker"]["toxicity"]
    assert tox["60"]["fills_with_markout"] == 7 and tox["60"]["toxic_fills"] == 1
    assert tox["300"]["fills_with_markout"] == 7 and tox["300"]["toxic_fills"] == 1 and tox["300"]["toxicity_rate"] == D("0.1429")
    assert tox["300"]["contract_weighted_markout_cents"] == D("0.8627")
    ctox = combined.metrics["maker"]["toxicity"]["300"]
    assert ctox["fills_with_markout"] == 5 and ctox["toxic_fills"] == 1 and ctox["toxicity_rate"] == D("0.2000")
    drift = maker.metrics["whale_follow_through"]
    assert drift["events"] == 5 and drift["60"]["n"] == 5 and drift["60"]["reverted"] == 2 and drift["60"]["reversal_rate"] == D("0.4000")
    assert maker.metrics["maker_toxicity_verdict"]["verdict"] == "PASS"
    toxic_rows = [row for row in combined.fills if row["toxic_at_longest_horizon"] is True]
    assert len(toxic_rows) == 1 and toxic_rows[0]["market"].endswith("GGG") and toxic_rows[0]["markout_cents"]["300"] == D("-7.0000")
    assert taker.metrics["maker_toxicity_verdict"] is None
    assert taker.metrics["taker"]["toxicity"]["300"]["toxic_fills"] == 0
    # Fill rows carry role + markouts for the scoreboard
    assert all(row["role"] in ("maker", "taker") for row in combined.fills)
    assert any(row["toxic_at_longest_horizon"] is True for row in combined.fills)
    assert combined.metrics["settlement_preview"]["scored_fills"] == 8


def test_fixture_replay_respects_caps_and_ledger_invariants(fixture_run) -> None:
    by_track, ledgers, _ = fixture_run
    for track, ledger in ledgers.items():
        assert ledger.mark_method == "conservative"
        assert ledger.equity == ledger.starting_cash + ledger.total_pnl
        assert ledger.unmarked_positions == 0
        for fill in ledger.fills:
            assert fill.quantity * fill.price <= FLB_RISK_LIMITS.max_notional_per_order
            assert fill.quantity == fill.quantity.to_integral_value()
        for position in ledger.open_positions:
            assert abs(position.quantity) <= FLB_RISK_LIMITS.max_position_per_market
            unit = position.average_price if position.quantity > 0 else 1 - position.average_price
            assert abs(position.quantity) * unit <= D("75")
        summary = by_track[track]
        assert summary.metrics["parameters"]["risk_limits"]["max_notional_per_order"] == D("25")
        assert summary.metrics["alignment"] == "time_aligned"
    # maker fees are the maker rate; the taker leg pays the taker rate; the challenger series charges makers nothing
    maker_ledger = ledgers[MAKER_LEG]
    challenger = [f for f in maker_ledger.fills if f.market_id.startswith("KXATPCHALLENGERMATCH")]
    assert challenger and all(f.fee == 0 for f in challenger)
    wta = [f for f in maker_ledger.fills if f.market_id.startswith("KXWTAMATCH")]
    assert all(f.fee > 0 for f in wta)


def test_combined_verdict_needs_enough_fills_and_a_forward_tape() -> None:
    import asyncio

    timelines, _ = load_fixture_timelines()
    summaries, _, _ = asyncio.run(run_whale_noise_tracks(timelines, params=WhaleNoiseParameters(min_fills_for_verdict=50)))
    by_track = {s.track: s for s in summaries}
    assert by_track[COMBINED].metrics["combined_vs_legs"]["verdict"] == "INSUFFICIENT_DATA"
    assert combined_verdict(by_track, WhaleNoiseParameters(), alignment="single_snapshot_after_tape")["verdict"] == "NOT_SIMULATED"


# ---------------------------------------------------------------- archive
def _write_archive(root: Path, *, duplicate_trade: bool = False) -> None:
    day = root / "kalshi" / "2026-09-15"
    day.mkdir(parents=True)
    payload = json.loads(Path("research/fixtures/whale_noise_tape.json").read_text())
    market_a = "KXWTAMATCH-26SEP15AAABBB-AAA"
    other = "KXFEDDECISION-26SEP-C25"
    tl = payload["timeline"][market_a]
    with (day / "markets.jsonl").open("w") as fh:
        fh.write(json.dumps({"kind": "market", "v": 1, "venue": "kalshi", "market_id": market_a, "ts": tl["books"][0]["ts"], "series_ticker": "KXWTAMATCH", "title": "A beats B", "volume": "12000"}) + "\n")
    with (day / "books.jsonl").open("w") as fh:
        for i, book in enumerate(tl["books"]):
            fh.write(json.dumps({"kind": "book", "v": 1, "venue": "kalshi", "market_id": market_a, "ts": book["ts"], "cycle": i, "bids": book["bids"], "asks": book["asks"]}) + "\n")
        fh.write(json.dumps({"kind": "book", "v": 1, "venue": "kalshi", "market_id": other, "ts": tl["books"][0]["ts"], "cycle": 0, "bids": [["0.50", "10"]], "asks": [["0.52", "10"]]}) + "\n")
    with (day / "trades.jsonl").open("w") as fh:
        for trade in tl["trades"]:
            record = {"kind": "trade", "v": 1, "venue": "kalshi", "market_id": market_a, "trade_id": trade["trade_id"], "ts": trade["ts"], "ts_venue": trade["ts"], "price": trade["yes_price"], "size": trade["size"], "taker_side": trade["taker_side"]}
            fh.write(json.dumps(record) + "\n")
            if duplicate_trade and trade["trade_id"] == "A-whale":
                fh.write(json.dumps(record) + "\n")


def test_archive_replay_matches_the_fixture_market(tmp_path: Path) -> None:
    import asyncio

    _write_archive(tmp_path, duplicate_trade=True)
    timelines, meta = timelines_from_archive(tmp_path, series=("KXWTAMATCH",))
    assert meta["markets"] == 1 and meta["duplicate_trades_dropped"] == 1 and meta["books"] == 6 and meta["trades"] == 11
    (timeline,) = timelines
    assert timeline.market.metadata["series_ticker"] == "KXWTAMATCH" and timeline.market.title == "A beats B"
    summaries, _, replay = asyncio.run(run_whale_noise_tracks(timelines, source="archive"))
    maker = next(s for s in summaries if s.track == MAKER_LEG)
    (quote,) = maker.metrics["quotes"]
    assert quote["status"] == "filled" and quote["filled"] == D("42") and [f["trade_id"] for f in quote["fills"]] == ["A-partial", "A-through"]
    assert replay.print_classes["whale_lift"] == 1
    # explicit tickers override the series filter; windows clip both books and prints
    everything, meta_all = timelines_from_archive(tmp_path)
    assert meta_all["markets"] == 2
    windowed, meta_w = timelines_from_archive(tmp_path, tickers=("KXWTAMATCH-26SEP15AAABBB-AAA",), since=_t(100), until=_t(250))
    assert meta_w["trades"] == 3 and meta_w["books"] == 1


# ---------------------------------------------------------------- network
def _kalshi_mock(calls: list[str], now: datetime) -> httpx.MockTransport:
    ticker = "KXWTAMATCH-26SEP15STETJE-TJE"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert "demo-api" not in request.url.host
        assert "/portfolio" not in request.url.path and "/orders" not in request.url.path
        path = request.url.path
        if path.endswith("/markets/trades"):
            assert request.url.params["ticker"] == ticker
            return httpx.Response(200, json={"trades": [
                {"trade_id": "recent-retail", "ticker": ticker, "count_fp": "9.00", "yes_price_dollars": "0.1200", "taker_side": "yes", "created_time": (now - timedelta(seconds=30)).isoformat(), "is_block_trade": False},
                {"trade_id": "recent-whale", "ticker": ticker, "count_fp": "1500.00", "yes_price_dollars": "0.5700", "taker_side": "yes", "created_time": (now - timedelta(seconds=90)).isoformat(), "is_block_trade": False},
                {"trade_id": "old-whale", "ticker": ticker, "count_fp": "3000.00", "yes_price_dollars": "0.5500", "taker_side": "yes", "created_time": (now - timedelta(hours=3)).isoformat(), "is_block_trade": False},
            ], "cursor": None})
        if path.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook_fp": {"yes_dollars": [["0.5500", "200"], ["0.5400", "300"]], "no_dollars": [["0.4300", "150"]]}})
        if "/events/" in path:
            return httpx.Response(200, json={"event": {"category": "Sports", "title": "Stearns vs Tjen", "mutually_exclusive": True}})
        if "/series/" in path:
            return httpx.Response(200, json={"series": {"ticker": "KXWTAMATCH", "category": "Sports", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}})
        assert path.endswith("/markets")
        if request.url.params.get("series_ticker") != "KXWTAMATCH":
            return httpx.Response(200, json={"markets": []})
        return httpx.Response(200, json={"markets": [{"ticker": ticker, "event_ticker": "KXWTAMATCH-26SEP15STETJE", "title": "Tjen beats Stearns?", "status": "open", "yes_bid_dollars": "0.5500", "yes_ask_dollars": "0.5700", "volume_fp": "902.12", "open_interest_fp": "902.12"}]})

    return httpx.MockTransport(handler)


async def test_network_mode_records_resting_quotes_but_never_simulates_fills() -> None:
    calls: list[str] = []
    now = datetime.now(UTC)
    async with httpx.AsyncClient(transport=_kalshi_mock(calls, now)) as http:
        timelines, meta = await timelines_from_network(kalshi_env="prod", series=("KXWTAMATCH", "KXATPMATCH"), limit=10, tape_window_seconds=1800, http=http)
    assert meta["alignment"] == "single_snapshot_after_tape" and meta["markets"] == 1
    assert meta["trades"] == 2 and meta["trades_outside_window_dropped"] == 1  # the 3-hour-old whale is not a reason to quote now
    (timeline,) = timelines
    assert timeline.alignment == "single_snapshot_after_tape" and timeline.market.metadata["fee_type"] == "quadratic_with_maker_fees"
    assert all(p.startswith("/trade-api/v2/") for p in calls)
    summaries, _, replay = await run_whale_noise_tracks(timelines, source="network")
    by_track = {s.track: s for s in summaries}
    maker = by_track[MAKER_LEG]
    assert maker.proposed_orders == 1 and maker.paper_fills == 0
    (quote,) = maker.metrics["quotes"]
    assert quote["yes_price"] == D("0.54") and quote["status"] in ("resting", "expired") and quote["filled"] == 0
    assert maker.metrics["maker"]["fills_not_simulated_single_snapshot"] >= 1
    assert by_track[COMBINED].metrics["combined_vs_legs"]["verdict"] == "NOT_SIMULATED"
    # The retail longshot print 30s after the whale is inside the lookback: no fade either.
    assert by_track[TAKER_LEG].refused_by_reason == {"whale_flow_in_lookback": 1}


# ---------------------------------------------------------------- ex post
def test_whale_flow_expost_fixture_passes_and_fair_prices_do_not() -> None:
    markets, _ = load_settled_trades(EXPOST_FIXTURE_PATH)
    report = whale_flow_expost(markets, WhaleNoiseParameters())
    verdicts = report["verdicts"]
    assert verdicts["whale_flow_ev_positive"]["verdict"] == "PASS" and verdicts["whale_flow_ev_positive"]["ev_status"] == "verified_expost"
    assert verdicts["retail_longshot_fade_ev_positive"]["verdict"] == "PASS"
    assert report["classes"]["whale"]["n_markets"] == 16 and report["classes"]["block"]["n_markets"] == 0
    early = report["excluding_final_minutes"]
    assert early["minutes"] == 60 and set(early["verdicts"]) == {"whale_flow_ev_positive", "retail_longshot_fade_ev_positive"}
    # The fixture's prints all sit more than an hour before close; a wider window drops end-game prints
    wide = whale_flow_expost(markets, WhaleNoiseParameters(), exclude_final_minutes=300)["excluding_final_minutes"]
    assert wide["classes"]["retail_longshot"]["n_trades"] < report["classes"]["retail_longshot"]["n_trades"]
    assert whale_flow_expost(markets, WhaleNoiseParameters(), exclude_final_minutes=10_000)["excluding_final_minutes"]["verdicts"]["whale_flow_ev_positive"]["verdict"] == "INSUFFICIENT_DATA"
    # Too few markets -> INSUFFICIENT_DATA, never a PASS on thin data
    thin = whale_flow_expost(markets[:3], WhaleNoiseParameters())
    assert thin["verdicts"]["whale_flow_ev_positive"]["verdict"] == "INSUFFICIENT_DATA"
    assert thin["verdicts"]["whale_flow_ev_positive"]["ev_status"] == "unverified"
    # Whales that are wrong as often as right: not verified
    for market in markets:
        for i, trade in enumerate(market.trades):
            if trade.count >= 1000:
                object.__setattr__(trade, "taker_side", "yes" if i % 2 == 0 else "no")
    fair = whale_flow_expost(markets, WhaleNoiseParameters())
    assert fair["verdicts"]["whale_flow_ev_positive"]["verdict"] != "PASS"


# ------------------------------------------------------------------- CLI
def _run(tmp_path: Path, **overrides: object) -> dict:
    args = build_parser().parse_args([])
    kwargs = dict(
        use_network=False,
        archive=None,
        limit=200,
        artifact_dir=tmp_path,
        harvest_dir=tmp_path / "harvests",
        harvest_trades=False,
        settled_per_series=5,
        max_trades_per_market=100,
        kalshi_env=None,
        persist_ledgers=True,
        reset_ledgers=False,
        model_fees=True,
        params=parameters_from_args(args),
    )
    kwargs.update(overrides)
    return run(**kwargs)  # type: ignore[arg-type]


async def test_cli_fixture_run_writes_report_scoreboard_ledgers_and_run_record(tmp_path: Path) -> None:
    report = await _run(tmp_path)
    assert report["paper_only"] is True and report["kind"] == "kalshi_whale_noise_report" and report["mode"] == "fixtures"
    verdicts = {row["check"]: row["verdict"] for row in report["verdict_table"]}
    assert verdicts["combined_beats_each_leg"] == "PASS"
    assert verdicts["maker_fills_not_adversely_selected"] == "PASS"
    assert verdicts["whale_flow_ev_positive"] == "PASS" and report["ex_post"]["note"].startswith("Synthetic fixture")
    assert "PASS" in report["headline"] and "fill rate" in report["headline"] and "toxicity" in report["headline"]
    for key in ("whale_definition", "one_tick_behind", "fill_model", "taker_leg", "toxicity", "marks", "sizing", "comparison", "ex_post"):
        assert report["assumptions"][key]
    assert report["experiment"]["fail_risks"] and report["event_stream"]["whale_events"] == 5
    assert report["legs"][COMBINED]["maker"]["fill_rate_contracts"] == pytest.approx(127 / 163, abs=1e-4)
    assert report["quotes"][MAKER_LEG] and len(report["quotes"][COMBINED]) == 4

    scoreboard = json.loads((tmp_path / "scoreboard_whale_noise.json").read_text())
    assert scoreboard["meta"]["source"] == "measured" and scoreboard["meta"]["pnl_source"] == "core.ledger.PaperLedger"
    assert scoreboard["meta"]["label"] == "MEASURED / FIXTURES / KALSHI WHALE/NOISE" and scoreboard["meta"]["kind"] == "kalshi_whale_noise"
    assert scoreboard["meta"]["primary_track"] == COMBINED and scoreboard["meta"]["track_family"] == "kalshi_flb" and scoreboard["meta"]["venues"] == ["kalshi"]
    assert [t["track"] for t in scoreboard["tracks"]] == list(WHALE_NOISE_TRACKS)
    assert scoreboard["findings"]["kalshi_whale_noise"]["verdicts"]["combined_beats_each_leg"] == "PASS"
    assert scoreboard["findings"]["kalshi_whale_noise"]["combined_vs_legs"]["interaction_pnl"] == pytest.approx(-0.26)
    assert scoreboard["portfolio"]["primary"]["mark_method"] == "conservative"
    assert scoreboard["totals"]["paper_fills"] == 8 + 7 + 3
    # per-quote / per-print rows live in the report, the scoreboard keeps counts + a pointer
    combined_row = next(t for t in scoreboard["tracks"] if t["track"] == COMBINED)
    assert combined_row["metrics"]["quotes"] == {"count": 4, "detail": "whale_noise_report_latest.json"}
    assert combined_row["metrics"]["fade_evaluations"]["count"] == 4
    assert report["evaluations"]["quote_evaluations"][COMBINED]["count"] == 5 and len(report["evaluations"]["fade_evaluations"][TAKER_LEG]["rows"]) == 4
    record = json.loads((tmp_path / "paper" / "runs" / f"{report['run_id']}.json").read_text())
    assert record["kind"] == "kalshi_whale_noise" and record["primary_track"] == COMBINED and len(record["tracks"]) == 3
    for track in WHALE_NOISE_TRACKS:
        assert (tmp_path / "paper" / f"ledger_{track}.json").exists()
        assert (tmp_path / "paper" / f"equity_curve_{track}.jsonl").read_text().count("\n") == 1


async def test_cli_ledgers_carry_across_runs_and_flags_behave(tmp_path: Path) -> None:
    first = await _run(tmp_path)
    second = await _run(tmp_path)
    assert len(load_ledgers(tmp_path, WHALE_NOISE_TRACKS)[COMBINED].equity_curve) == 2
    assert second["legs"][COMBINED]["ledger"]["fills"] > first["legs"][COMBINED]["ledger"]["fills"]
    reset = await _run(tmp_path, reset_ledgers=True)
    assert reset["legs"][COMBINED]["ledger"]["fills"] == first["legs"][COMBINED]["ledger"]["fills"]
    no_fees = await _run(tmp_path, model_fees=False, persist_ledgers=False, reset_ledgers=True)
    assert all(D(str(leg["ledger"]["fees_paid"])) == 0 for leg in no_fees["legs"].values())
    assert no_fees["ex_post"]["classes"]["whale"]["taker_fee_per_contract"] == 0


async def test_cli_archive_mode_and_missing_harvest_are_honest(tmp_path: Path) -> None:
    root = tmp_path / "books"
    _write_archive(root)
    report = await _run(tmp_path / "out", archive=root, persist_ledgers=False)
    assert report["mode"] == "archive" and report["data"]["source"] == "archive" and report["data"]["markets"] == 1
    assert report["ex_post"]["status"] == "not_measured" and report["ex_post"]["verdicts"]["whale_flow_ev_positive"]["ev_status"] == "unverified"
    assert report["combined_vs_legs"]["verdict"] == "INSUFFICIENT_DATA"  # one market, two fills
    assert report["legs"][MAKER_LEG]["maker"]["quotes_filled"] == 1
    scoreboard = json.loads((tmp_path / "out" / "scoreboard_whale_noise.json").read_text())
    assert scoreboard["meta"]["mode"] == "network" and scoreboard["meta"]["data_mode"] == "archive"
    assert "ARCHIVE REPLAY" in scoreboard["meta"]["label"]


async def test_cli_refuses_live_environment_and_conflicting_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="choose one"):
        await _run(tmp_path, use_network=True, archive=tmp_path)
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    with pytest.raises(ValueError, match="paper-only"):
        await _run(tmp_path)
    assert not (tmp_path / "scoreboard_whale_noise.json").exists()


def test_cli_parameters_from_args_and_registry(tmp_path: Path) -> None:
    registry = tmp_path / "whales.json"
    registry.write_text(json.dumps({"default": {"min_contracts": 2000}, "series": {"KXWTAMATCH": {"ev_status": "operator_asserted"}}}))
    args = build_parser().parse_args(["--whale-registry", str(registry), "--ticks-behind", "2", "--markout-horizons", "30", "300", "30", "--assume-later-arrivals-behind"])
    params = parameters_from_args(args)
    assert params.default_rule.min_contracts == D("2000") and params.rule_for("KXWTAMATCH").ev_status == "operator_asserted"
    assert params.ticks_behind == 2 and params.markout_horizons_seconds == (30, 300) and params.later_arrivals_ahead is False
    default = parameters_from_args(build_parser().parse_args([]))
    assert default.default_rule == WhaleRule() and default.later_arrivals_ahead is True
    assert default.max_order_notional == D("25") and default.max_market_notional == D("75")
    asserted = parameters_from_args(build_parser().parse_args(["--assume-whale-ev"]))
    assert asserted.default_rule.ev_status == "operator_asserted"
