"""Polymarket intra-venue arbitrage tracks: detectors, ledger booking, artifacts."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal as D
from pathlib import Path

import httpx

from apps.measure_all import load_ledgers, persist_run
from core.ledger import PaperLedger
from core.types import Fill, MarketGroup, OrderBook, Outcome, PriceLevel, Side, Venue
from research.polymarket_arb_tracks import (
    COMBINATORIAL,
    CONVERT_ORDER_ID,
    NEGRISK,
    REBALANCING,
    measure_polymarket_arb_with_ledgers,
)
from research.scoreboard import POLYMARKET_ARB_TRACKS, TRACKS, measure_all_with_ledgers
from strategies.polymarket_arb import (
    ArbParameters,
    books_are_mirrors,
    evaluate_binary,
    evaluate_long_all_yes,
    evaluate_merge,
    evaluate_negrisk_convert,
    evaluate_split,
)
from venues.polymarket.client import PolymarketClient, load_event_fixture
from venues.polymarket.fees import polymarket_taker_fee, taker_fee_rate

FIXTURE = load_event_fixture()
GROUPS = {group.group_id: group for group in FIXTURE.groups}
MARKETS = {market.market_id: market for market in FIXTURE.markets}
PARAMS = ArbParameters()


def _identity(ledger: PaperLedger) -> None:
    assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl


# --------------------------------------------------------------------------
# Fee model
# --------------------------------------------------------------------------
def test_fee_formula_matches_published_tables() -> None:
    # docs.polymarket.com fee tables: 100 shares.
    assert polymarket_taker_fee(D("100"), D("0.50"), D("0.04")) == D("1.00000")  # politics @ 50c
    assert polymarket_taker_fee(D("100"), D("0.10"), D("0.07")) == D("0.63000")  # crypto @ 10c
    assert polymarket_taker_fee(D("100"), D("0.85"), D("0.05")) == D("0.63750")  # sports @ 85c (table shows 0.64)
    assert polymarket_taker_fee(D("100"), D("0.30"), D("0.04")) == polymarket_taker_fee(D("100"), D("0.70"), D("0.04"))
    assert polymarket_taker_fee(D("10"), D("0.5"), D("0")) == D("0")


def test_fee_rate_mapping_is_fail_closed() -> None:
    assert taker_fee_rate("politics_fees", True) == D("0.04")
    assert taker_fee_rate("sports_fees_v3", True) == D("0.05")
    assert taker_fee_rate("crypto_fees_v2", True) == D("0.07")
    assert taker_fee_rate("finance_prices_fees", True) == D("0.04")
    assert taker_fee_rate(None, False) == D("0")
    assert taker_fee_rate("politics_fees", False) == D("0")
    assert taker_fee_rate("brand_new_category_fees", True) == D("0.07")  # unknown -> highest rate


# --------------------------------------------------------------------------
# Binary rebalancing detectors
# --------------------------------------------------------------------------
def test_mirror_books_never_show_a_binary_edge() -> None:
    market = MARKETS["0xfixture-binary-mirror"]
    yes, no = FIXTURE.yes_books[market.market_id], FIXTURE.no_books[market.market_id]
    assert books_are_mirrors(yes, no)
    evaluation = evaluate_binary(market, yes, no, PARAMS)
    assert not evaluation.traded
    assert evaluation.reason == "no_edge"
    assert evaluation.mirror_consistent is True
    # ask_sum = 1 + spread, bid_sum = 1 - spread: structurally excluded.
    assert evaluate_merge(market, yes, no, PARAMS).top_of_book_sum == D("1.020")
    assert evaluate_split(market, yes, no, PARAMS).top_of_book_sum == D("0.980")


def test_merge_is_depth_aware_and_fee_aware() -> None:
    market = MARKETS["0xfixture-binary-merge-feefree"]
    evaluation = evaluate_binary(market, FIXTURE.yes_books[market.market_id], FIXTURE.no_books[market.market_id], PARAMS)
    assert evaluation.traded and evaluation.kind == "merge"
    assert evaluation.mirror_consistent is False
    # NO depth at 0.53 is 80; the next NO level (0.56) pushes the pair above 1.
    assert evaluation.quantity == D("80")
    assert evaluation.gross_edge_per_set == D("0.020")
    assert evaluation.total_fees == D("0")
    assert evaluation.total_slippage == D("80") * D("2") * D("0.001")
    assert evaluation.net_profit == D("80") * D("0.020") - evaluation.total_slippage
    assert evaluation.capital_required == D("80") * (D("0.45") + D("0.53"))
    assert [(o.side, o.outcome, o.price) for o in evaluation.orders] == [
        (Side.BUY, Outcome.YES, D("0.450")),
        (Side.BUY, Outcome.NO, D("0.530")),
    ]

    with_fees = MARKETS["0xfixture-binary-merge-fees"]
    refused = evaluate_binary(with_fees, FIXTURE.yes_books[with_fees.market_id], FIXTURE.no_books[with_fees.market_id], PARAMS)
    assert not refused.traded
    assert refused.reason == "fees_exceed_edge"
    assert refused.gross_edge_per_set == D("0.020")  # gross survives, net does not


def test_split_sells_both_legs_into_bids() -> None:
    market = MARKETS["0xfixture-binary-split"]
    evaluation = evaluate_binary(market, FIXTURE.yes_books[market.market_id], FIXTURE.no_books[market.market_id], PARAMS)
    assert evaluation.traded and evaluation.kind == "split"
    assert evaluation.quantity == D("60")  # NO bid depth at 0.50
    assert evaluation.top_of_book_sum == D("1.020")
    assert evaluation.capital_required == D("60")  # 1 USDC per set to split
    assert all(o.side is Side.SELL for o in evaluation.orders)


# --------------------------------------------------------------------------
# Multi-outcome detectors
# --------------------------------------------------------------------------
def test_negrisk_convert_sizing_matches_hand_calculation() -> None:
    group = GROUPS["fixture-negrisk-convert"]
    evaluation = evaluate_negrisk_convert(group, FIXTURE.no_books, PARAMS)
    assert evaluation.traded and evaluation.executable_now and evaluation.lockup == "none"
    assert evaluation.payoff_per_set == D("3")
    assert evaluation.top_of_book_sum == D("2.950")
    assert evaluation.gross_edge_per_set == D("0.050")
    # 30 sets at 2.95, then leg C steps to 0.81 (gross 0.04, still positive),
    # then leg B steps to 0.66 (gross 0.03 < fees + slippage) -> stop at 40.
    assert evaluation.quantity == D("40")
    fee_at_top = D("0.04") * (D("0.6") * D("0.4") + D("0.65") * D("0.35") + D("0.8") * D("0.2") + D("0.9") * D("0.1"))
    fee_after_step = fee_at_top - D("0.04") * D("0.8") * D("0.2") + D("0.04") * D("0.81") * D("0.19")
    assert evaluation.total_fees == D("30") * fee_at_top + D("10") * fee_after_step
    assert evaluation.total_slippage == D("40") * D("4") * D("0.001")
    gross = D("30") * D("0.05") + D("10") * D("0.04")
    assert evaluation.net_profit == gross - evaluation.total_fees - evaluation.total_slippage
    assert evaluation.capital_required == D("30") * D("2.95") + D("10") * D("2.96")
    assert all(o.outcome is Outcome.NO and o.side is Side.BUY for o in evaluation.orders)


def test_negrisk_convert_refuses_without_converter_or_edge() -> None:
    plain = evaluate_negrisk_convert(GROUPS["fixture-plain-multi"], FIXTURE.no_books, PARAMS)
    assert plain.reason == "converter_unavailable" and not plain.executable_now
    quiet = evaluate_negrisk_convert(GROUPS["fixture-negrisk-long-yes"], FIXTURE.no_books, PARAMS)
    assert quiet.reason == "no_edge"
    assert quiet.gross_edge_per_set == D("2") - D("2.110")


def test_long_all_yes_locks_capital_and_refuses_hidden_outcomes() -> None:
    admitted = evaluate_long_all_yes(GROUPS["fixture-negrisk-long-yes"], FIXTURE.yes_books, PARAMS)
    assert admitted.traded
    assert not admitted.executable_now and admitted.lockup == "until_resolution"
    assert admitted.lockup_until == "2027-01-15T00:00:00Z"
    assert admitted.quantity == D("60")  # next ask level (0.98 sum) no longer clears fees
    assert admitted.top_of_book_sum == D("0.950")

    augmented = evaluate_long_all_yes(GROUPS["fixture-negrisk-augmented-long-yes"], FIXTURE.yes_books, PARAMS)
    assert augmented.reason == "hidden_outcome_risk" and augmented.gross_edge_per_set == D("0.060")
    unverified = evaluate_long_all_yes(GROUPS["fixture-plain-multi"], FIXTURE.yes_books, PARAMS)
    assert unverified.reason == "exclusivity_unverified"
    # Opting in is explicit and still sized normally.
    relaxed = ArbParameters(allow_hidden_outcome_long_yes=True)
    assert evaluate_long_all_yes(GROUPS["fixture-negrisk-augmented-long-yes"], FIXTURE.yes_books, relaxed).traded


def test_capital_and_set_caps_bound_the_plan() -> None:
    group = GROUPS["fixture-negrisk-convert"]
    capped = evaluate_negrisk_convert(group, FIXTURE.no_books, ArbParameters(maximum_capital=D("30")))
    assert capped.traded and capped.quantity == D("10") and capped.capital_required == D("29.5")
    tiny = evaluate_negrisk_convert(group, FIXTURE.no_books, ArbParameters(maximum_capital=D("10")))
    assert tiny.reason == "below_min_order_size"  # 3 sets < min order size 5
    few = evaluate_negrisk_convert(group, FIXTURE.no_books, ArbParameters(maximum_sets=D("7")))
    assert few.quantity == D("7")


# --------------------------------------------------------------------------
# Ledger booking through the tracks
# --------------------------------------------------------------------------
async def test_tracks_book_merge_split_and_conversion_in_the_ledger() -> None:
    summaries, ledgers, snapshot = await measure_polymarket_arb_with_ledgers()
    by_track = {s.track: s for s in summaries}
    assert set(by_track) == set(POLYMARKET_ARB_TRACKS)
    assert snapshot.errors == []

    reb = by_track[REBALANCING]
    assert reb.candidates == 16 and reb.admitted == 2 and reb.paper_fills == 4
    assert reb.refused_by_reason == {"fees_exceed_edge": 1, "no_edge": 13}
    assert reb.metrics["mirror_consistent"] == 13 and reb.metrics["mirror_inconsistent"] == 3
    # merge: 80 * (1 - 0.45 - 0.53); split: 60 * (0.52 + 0.50 - 1); both legs net flat.
    assert ledgers[REBALANCING].realized_pnl == D("80") * D("0.02") + D("60") * D("0.02")
    assert ledgers[REBALANCING].open_positions == []
    _identity(ledgers[REBALANCING])

    neg = by_track[NEGRISK]
    assert neg.candidates == 4 and neg.admitted == 1 and neg.proposed_orders == 4
    assert neg.refused_by_reason == {"converter_unavailable": 1, "no_edge": 2}
    conversion = neg.metrics["conversions"][0]
    assert conversion["sets"] == D("40") and conversion["collateral_out"] == D("120")
    ledger = ledgers[NEGRISK]
    converts = [f for f in ledger.fills if f.order_id == CONVERT_ORDER_ID]
    assert len(converts) == 4 and sum((f.price for f in converts), D("0")) == D("1")
    assert ledger.open_positions == []
    # 120 USDC of collateral minus 118.10 paid for the NO legs minus fees.
    assert ledger.realized_pnl == D("120") - D("118.10") - ledger.fees_paid
    assert ledger.fees_paid == D("1.14556")
    _identity(ledger)

    comb = by_track[COMBINATORIAL]
    assert comb.admitted == 1 and comb.paper_fills == 3
    assert comb.refused_by_reason == {"exclusivity_unverified": 1, "hidden_outcome_risk": 2}
    holding = comb.metrics["holdings"][0]
    assert holding["sets_complete"] == D("60") and holding["lockup_until"] == "2027-01-15T00:00:00Z"
    assert comb.metrics["locked_capital"] == D("57")
    assert holding["profit_at_resolution"] == D("60") - D("57") - ledgers[COMBINATORIAL].fees_paid
    # Held positions are marked at mid (one tick inside the ask), never at the payoff.
    assert len(ledgers[COMBINATORIAL].open_positions) == 3
    assert ledgers[COMBINATORIAL].unrealized_pnl == -D("60") * D("3") * D("0.01")
    _identity(ledgers[COMBINATORIAL])


async def test_no_fee_mode_zeroes_polymarket_fees_and_no_orders_walk_the_real_no_book() -> None:
    _, ledgers, _ = await measure_polymarket_arb_with_ledgers(model_fees=False)
    assert ledgers[NEGRISK].fees_paid == D("0")
    # Fixture NO ladders are the real legs walked: A 0.60, B 0.65, C 0.80/0.81, D 0.90.
    prices = sorted({(f.market_id, f.price) for f in ledgers[NEGRISK].fills if f.outcome is Outcome.NO})
    assert ("0xfixture-nom-c", D("0.800")) in prices and ("0xfixture-nom-c", D("0.810")) in prices


def test_close_position_books_partial_close_at_arbitrary_price() -> None:
    ledger = PaperLedger(starting_cash=D("100"))
    ledger.record_fill(Fill(venue=Venue.POLYMARKET, market_id="m", order_id="o", side=Side.BUY, outcome=Outcome.NO, quantity=D("10"), price=D("0.60")))
    fill = ledger.close_position(Venue.POLYMARKET, "m", yes_price=D("0.25"), quantity=D("4"), order_id="negrisk_convert")
    assert fill is not None and fill.side is Side.BUY and fill.quantity == D("4")
    position = ledger.portfolio.get(Venue.POLYMARKET, "m")
    assert position is not None and position.quantity == D("-6")
    assert position.realized_pnl == D("4") * (D("0.40") - D("0.25"))
    assert ledger.close_position(Venue.POLYMARKET, "none", yes_price=D("0.5"), order_id="x") is None
    _identity(ledger)


# --------------------------------------------------------------------------
# Snapshot parsing (network payload shape, no network)
# --------------------------------------------------------------------------
async def test_network_events_and_batch_books_parse_into_groups() -> None:
    posted: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("clob"):
            body = json.loads(request.content)
            posted.append([row["token_id"] for row in body])
            books = []
            for row in body:
                token = row["token_id"]
                # Live shape: bids ascending, asks descending, mirror across YES/NO.
                if token.endswith("1"):
                    books.append({"market": "0xcond", "asset_id": token, "tick_size": "0.001", "min_order_size": "5", "neg_risk": True,
                                  "bids": [{"price": "0.10", "size": "500"}, {"price": "0.12", "size": "100"}],
                                  "asks": [{"price": "0.15", "size": "300"}, {"price": "0.13", "size": "50"}]})
                else:
                    books.append({"market": "0xcond", "asset_id": token, "tick_size": "0.001", "min_order_size": "5", "neg_risk": True,
                                  "bids": [{"price": "0.85", "size": "300"}, {"price": "0.87", "size": "50"}],
                                  "asks": [{"price": "0.90", "size": "500"}, {"price": "0.88", "size": "100"}]})
            return httpx.Response(200, json=books)
        assert request.url.params["order"] == "liquidity"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "30829", "slug": "dem-nominee-2028", "title": "Democratic nominee 2028?",
                    "negRisk": True, "negRiskAugmented": True, "negRiskMarketID": "0xnr", "endDate": "2028-08-01T00:00:00Z",
                    "liquidity": 77557648.3,
                    "markets": [
                        {"conditionId": "0xc1", "question": "Newsom?", "groupItemTitle": "Gavin Newsom", "clobTokenIds": '["111", "112"]',
                         "active": True, "closed": False, "feesEnabled": True, "feeType": "politics_fees", "takerBaseFee": 1000,
                         "orderPriceMinTickSize": 0.001, "orderMinSize": 5},
                        {"conditionId": "0xc2", "question": "AOC?", "groupItemTitle": "AOC", "clobTokenIds": '["221", "222"]',
                         "active": True, "closed": False, "feesEnabled": True, "feeType": "politics_fees"},
                        {"conditionId": "0xdead", "question": "closed leg", "clobTokenIds": '["331", "332"]', "active": True, "closed": True},
                    ],
                },
                {"id": "1", "slug": "binary", "title": "Binary geopolitics", "negRisk": False,
                 "markets": [{"conditionId": "0xb1", "question": "Ceasefire?", "clobTokenIds": '["441", "442"]', "feesEnabled": False}]},
            ],
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = PolymarketClient(paper=True, use_fixtures=False, http=http)
    try:
        snapshot = await client.capture_events(limit=5)
    finally:
        await client.close()
        await http.aclose()

    assert snapshot.errors == []
    assert [g.group_id for g in snapshot.groups] == ["30829", "1"]
    nominee = snapshot.groups[0]
    assert nominee.size == 2  # closed leg dropped
    assert nominee.exclusive and nominee.convertible and nominee.augmented
    assert nominee.metadata["listed_markets"] == 3
    leg = nominee.markets[0]
    assert leg.metadata["taker_fee_rate"] == "0.04"  # feeType, not takerBaseFee=1000
    assert leg.metadata["tick_size"] == "0.001" and leg.metadata["min_order_size"] == "5"
    assert leg.metadata["group_item_title"] == "Gavin Newsom"
    binary = snapshot.groups[1]
    assert not binary.convertible and binary.markets[0].metadata["taker_fee_rate"] == "0"
    assert posted == [["111", "112", "221", "222", "441", "442"]]  # one batch, both tokens per leg
    yes, no = snapshot.yes_books["0xc1"], snapshot.no_books["0xc1"]
    assert yes.best_bid is not None and yes.best_bid.price == D("0.12")  # sorted regardless of wire order
    assert yes.best_ask is not None and yes.best_ask.price == D("0.13")
    assert books_are_mirrors(yes, no)


# --------------------------------------------------------------------------
# Artifacts and CLIs
# --------------------------------------------------------------------------
async def test_measure_all_includes_arb_tracks_and_persists_their_ledgers(tmp_path: Path) -> None:
    summaries, ledgers = await measure_all_with_ledgers()
    assert [s.track for s in summaries] == list(TRACKS)
    artifact = persist_run(summaries, ledgers, artifact_dir=tmp_path, mode="fixtures", measured_at="t", limit=3, kalshi_env=None)
    assert artifact["meta"]["source"] == "measured"
    assert {row["track"] for row in artifact["charts"]["pnl_by_track"]} >= set(POLYMARKET_ARB_TRACKS)
    assert "combinatorial_positions_marked_at_mid_not_resolution" in artifact["portfolio"]["risk_flags"]
    reloaded = load_ledgers(tmp_path, POLYMARKET_ARB_TRACKS)
    assert reloaded[NEGRISK].realized_pnl == ledgers[NEGRISK].realized_pnl
    manifest = json.loads(next((tmp_path / "paper" / "runs").glob("*.json")).read_text())
    assert manifest["primary_track"] == "single_venue_fair_value"
    assert {t["track"] for t in manifest["tracks"]} == set(TRACKS)

    # A second cycle on carried ledgers must not re-enter the held sum-to-one set.
    summaries2, ledgers2 = await measure_all_with_ledgers(ledgers=reloaded)
    comb2 = next(s for s in summaries2 if s.track == COMBINATORIAL)
    assert comb2.admitted == 1  # detector still sees the edge on the frozen book...
    # ...but the risk rail caps re-entry to the per-market position headroom, and
    # equity carries over (positions doubled at the same book, identity holds).
    _identity(ledgers2[COMBINATORIAL])
    assert len(ledgers2[COMBINATORIAL].equity_curve) == 2


def test_dedicated_cli_writes_family_scoreboard_without_touching_latest(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apps.measure_polymarket_arb", "--artifact-dir", str(tmp_path), "--no-persist"],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "scoreboard_polymarket_arb.json").exists()
    assert not (tmp_path / "scoreboard_latest.json").exists()
    board = json.loads((tmp_path / "scoreboard_polymarket_arb.json").read_text())
    assert board["meta"]["source"] == "measured"
    assert board["meta"]["track_family"] == "polymarket_arb"
    assert board["meta"]["primary_track"] == NEGRISK
    assert board["meta"]["venues"] == ["polymarket"]
    assert board["meta"]["pnl_source"] == "core.ledger.PaperLedger"
    assert [t["track"] for t in board["tracks"]] == list(POLYMARKET_ARB_TRACKS)
    report = json.loads((tmp_path / "polymarket_arb_latest.json").read_text())
    assert report["paper_only"] is True and report["source"] == "measured"
    assert report["rebalancing"]["mirror_consistent"] == 13
    assert report["negrisk"]["conversions"][0]["collateral_out"] == 120.0
    assert report["combinatorial"]["locked_capital"] == 57.0
    manifest = json.loads(next((tmp_path / "paper" / "runs").glob("*.json")).read_text())
    assert manifest["primary_track"] == NEGRISK and manifest["venues"] == ["polymarket"]
    assert "mirror check: 13 consistent / 3 inconsistent" in result.stdout


def test_cli_refuses_live_flags(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apps.measure_polymarket_arb", "--artifact-dir", str(tmp_path)],
        check=False, capture_output=True, text=True,
        env={"PATH": "", "TRADING_MODE": "live", "ENABLE_LIVE_TRADING": "true", "PYTHONPATH": str(Path.cwd())},
    )
    assert result.returncode != 0
    assert not (tmp_path / "scoreboard_polymarket_arb.json").exists()


def test_group_dataclass_defaults() -> None:
    group = MarketGroup(venue=Venue.POLYMARKET, group_id="g", title="t")
    assert group.size == 0 and not group.exclusive and not group.convertible
    book = OrderBook(market_id="m", bids=(PriceLevel(D("0.4"), D("1")),), asks=(PriceLevel(D("0.6"), D("1")),))
    assert books_are_mirrors(book, book.for_outcome(Outcome.NO))
