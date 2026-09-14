import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apps.measure_all import json_default, persist_run
from core.types import Market, OrderBook, PriceLevel, Venue
from research.scoreboard import VenueSnapshot
from research.tennis_basis import (
    TENNIS_TRACK,
    UNWIND_ORDER_ID,
    GapRegister,
    classify_settlement,
    kalshi_match_market,
    load_replay,
    measure_tennis_basis,
    pair_outside_event,
    polymarket_match_market,
    replay_fixture,
    tennis_markets,
)
from research.tennis_odds import (
    JsonOutsideSource,
    NullOutsideSource,
    OddsApiSource,
    OutsideEvent,
    StaticOutsideSource,
    build_outside_source,
    parse_event,
)
from strategies.tennis_basis import (
    BasisParameters,
    BookQuote,
    TennisBasisLean,
    classify_settlement_basis,
    closure_fraction,
    consensus_line,
    devig_two_way,
    normalize_name,
    overround,
    same_player,
    sharp_side,
    verdict,
    wilson_interval,
)

D = Decimal
AS_OF = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)

KALSHI_RULES = (
    "If Janice Tjen wins the Stephens vs Tjen professional tennis match in the 2026 WTA Guadalajara Round Of 16 "
    "after a ball has been played, then the market resolves to Yes. The following market refers to the Stephens vs "
    "Tjen professional tennis match in the 2026 WTA Guadalajara Round Of 16 after a ball has been played. If the "
    "match does not occur (signaled by a ball being played) due to a player injury, walkover, forfeiture, or any "
    "other cancellation (all before the match starts), the market will resolve to a fair price in accordance with "
    "the rules. If this match is postponed or delayed, the market will remain open and close after the rescheduled "
    "match has finished (within two weeks). Settlement source: WTA https://www.wtatennis.com/"
)
POLY_RULES = (
    "This market refers to the tennis match between Tatjana Maria and Taylor Townsend in the Guadalajara Open Akron, "
    "originally scheduled for September 13, 2026 at 12:00PM ET.\n\nThis market will resolve to 'Tatjana Maria' if "
    "Tatjana Maria advances against Taylor Townsend.\n\nIf the match is canceled (not played at all), ends in a tie, "
    "or a winner has not been determined by September 27, 2026, 11:59 PM ET (14 days after the scheduled start), "
    "this market will resolve to 50-50.\n\nIf the match begins but is not completed, and one player advances due to "
    "the opponent's retirement, default, or disqualification, this market will resolve to the player who advances."
    "\n\nIf the match ends in a walkover (player withdraws before the start and the other advances automatically), "
    "this market will resolve to 50-50.\n\nThe primary resolution source will be official information from the WTA Tour."
)


def book(bid: str, ask: str, size: str = "200", market_id: str = "M") -> OrderBook:
    return OrderBook(market_id=market_id, bids=(PriceLevel(D(bid), D(size)),), asks=(PriceLevel(D(ask), D(size)),))


def quote(book_key: str, a: str, b: str, age: int = 60) -> BookQuote:
    return BookQuote(book_key, D(a), D(b), AS_OF - timedelta(seconds=age))


def event(event_id: str, home: str, away: str, quotes: list[BookQuote], commence: datetime | None = AS_OF + timedelta(hours=10)) -> OutsideEvent:
    return OutsideEvent(event_id, "tennis_wta_test", commence, home, away, tuple(quotes), "test")


# --------------------------------------------------------------------------
# De-vig and consensus
# --------------------------------------------------------------------------
def test_multiplicative_devig_is_exact_on_vig_free_and_vigged_pairs() -> None:
    assert devig_two_way(D("1.25"), D("5")) == (D("0.8"), D("0.2"))
    assert devig_two_way(D("2"), D("2")) == (D("0.5"), D("0.5"))
    a, b = devig_two_way(D("1.80"), D("2.20"))
    assert a.quantize(D("0.0001")) == D("0.5500") and (a + b).quantize(D("0.000001")) == D("1")
    assert overround(D("1.80"), D("2.20")).quantize(D("0.0001")) == D("0.0101")
    with pytest.raises(ValueError):
        devig_two_way(D("1"), D("3"))
    with pytest.raises(ValueError):
        BookQuote("x", D("0.9"), D("2"))


def test_consensus_prefers_the_sharp_book_then_the_median_and_drops_stale_quotes() -> None:
    quotes = [quote("williamhill", "1.83", "2.10"), quote("pinnacle", "1.80", "2.20"), quote("betsson", "1.70", "2.30")]
    line = consensus_line(quotes, as_of=AS_OF)
    assert line.method == "sharp_book" and line.sharp_book == "pinnacle"
    assert line.probability_a.quantize(D("0.0001")) == D("0.5500")
    assert set(line.per_book) == {"williamhill", "pinnacle", "betsson"}

    no_sharp = consensus_line(quotes[:1] + quotes[2:], as_of=AS_OF)
    assert no_sharp.method == "median" and no_sharp.books_used == ("betsson", "williamhill")
    expected = (devig_two_way(D("1.83"), D("2.10"))[0] + devig_two_way(D("1.70"), D("2.30"))[0]) / 2
    assert no_sharp.probability_a == expected

    one = consensus_line(quotes[:1], as_of=AS_OF)
    assert one.method == "insufficient_books" and one.probability_a is None

    stale = consensus_line([quote("pinnacle", "1.80", "2.20", age=7200)], as_of=AS_OF)
    assert stale.method == "no_quotes" and not stale.available

    relaxed = consensus_line(quotes[:1], as_of=AS_OF, parameters=BasisParameters(min_books=1))
    assert relaxed.method == "median" and relaxed.probability_a is not None


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------
def test_player_name_matching_handles_accents_initials_and_rejects_different_players() -> None:
    assert normalize_name("Jessica Bouzas Maneiro") == "jessica bouzas maneiro"
    assert normalize_name("Renata Zarazúa") == "renata zarazua"
    assert same_player("S. Kenin", "Sofia Kenin")
    assert same_player("Zverev", "Alexander Zverev")
    assert same_player("Tatjana Maria", "T Maria")
    assert not same_player("Taylor Fritz", "Brandon Fritz")
    assert not same_player("Alcaraz", "Sinner")
    assert not same_player("", "Sinner")


# --------------------------------------------------------------------------
# Settlement basis filter (verbatim venue texts, read 2026-09-14)
# --------------------------------------------------------------------------
def test_kalshi_and_polymarket_rule_texts_are_classified_and_admitted() -> None:
    kalshi = classify_settlement_basis(KALSHI_RULES, series="KXWTAMATCH")
    assert kalshi.admitted and kalshi.retirement == "advancing_player" and kalshi.walkover == "fair_price"
    poly = classify_settlement_basis(POLY_RULES)
    assert poly.admitted and poly.retirement == "advancing_player" and poly.walkover == "fifty_fifty"
    assert poly.as_dict()["bookmaker_basis"] == {"walkover": "void", "retirement": "void_or_book_specific"}


@pytest.mark.parametrize(
    ("text", "series", "reason"),
    [
        ("If Taylor Fritz wins the Fritz vs Nakashima professional tennis match, the market resolves to Yes.", "KXATPMATCH", "settlement_basis_unreadable"),
        ("", None, "settlement_basis_unreadable"),
        (POLY_RULES.replace("Guadalajara Open Akron", "ITF W35 Redding"), None, "settlement_basis_itf"),
        (KALSHI_RULES, "KXITFMATCH", "settlement_basis_itf"),
        (POLY_RULES.replace("this market will resolve to 50-50.\n\nThe primary", "the market will be void and refunded.\n\nThe primary"), None, "settlement_basis_mismatch"),
    ],
)
def test_settlement_basis_filter_is_fail_closed(text: str, series: str | None, reason: str) -> None:
    basis = classify_settlement_basis(text, series=series)
    assert not basis.admitted and basis.reason == reason


# --------------------------------------------------------------------------
# Closure and verdict
# --------------------------------------------------------------------------
def test_closure_fraction_and_sharp_side_math() -> None:
    assert closure_fraction(D("0.06"), D("0.02")) == D("0.04") / D("0.06")
    assert closure_fraction(D("-0.05"), D("0.03")) == D("1.6")  # crossed through the line
    assert closure_fraction(D("0.04"), D("0.07")) == D("-0.75")  # widened
    assert closure_fraction(D("0.06"), D("0.03")) == D("0.5")
    assert closure_fraction(D("0"), D("0.01")) is None
    assert sharp_side(D("0.05")) == "sell" and sharp_side(D("-0.05")) == "buy" and sharp_side(D("0")) is None


def test_verdict_applies_the_pre_registered_rule() -> None:
    closed = [D("0.7")] * 18 + [D("1.2")] * 2 + [D("0.2")] * 10  # 20 / 30 = 0.667
    v = verdict(closed)
    assert v.n == 30 and v.closed_half == 20 and v.status == "PASS" and v.overshoots == 2
    assert v.rate.quantize(D("0.0001")) == D("0.6667")
    lo, hi = v.wilson_95
    assert lo < D("0.6667") < hi
    assert verdict([D("0.7")] * 17 + [D("0.1")] * 13).status == "FAIL"
    assert verdict([D("1")] * 29).status == "insufficient_sample"
    assert verdict([]).rate is None and verdict([]).wilson_95 is None
    assert verdict([D("0.5")] * 30).status == "PASS"  # exactly half counts as closed
    assert wilson_interval(0, 0) is None
    small = verdict([D("1")] * 5, parameters=BasisParameters(min_sample=5))
    assert small.status == "PASS" and small.pre_registered["min_sample"] == 5


def test_parameters_are_validated() -> None:
    with pytest.raises(ValueError):
        BasisParameters(gap_threshold=D("0"))
    with pytest.raises(ValueError):
        BasisParameters(pass_rate=D("1.5"))
    with pytest.raises(ValueError):
        BasisParameters(min_sample=0)


# --------------------------------------------------------------------------
# Paper lean through the fair-value engine
# --------------------------------------------------------------------------
def test_lean_sells_yes_at_the_bid_when_the_venue_is_above_the_line() -> None:
    market = Market(Venue.KALSHI, "M", "Tjen wins")
    line = consensus_line([quote("pinnacle", "1.80", "2.20")], as_of=AS_OF)
    evaluation = TennisBasisLean().evaluate(market, book("0.60", "0.62"), D("0.55"), record_id="r", consensus=line)
    assert evaluation.traded and evaluation.side == "sell"
    (order,) = evaluation.orders
    assert order.side.value == "sell" and order.price == D("0.60") and order.quantity == D("10")
    assert evaluation.gap == D("0.06")
    assert evaluation.cost_adjusted_edge == D("0.60") - D("0.55") - D("0.02")  # Kalshi fee buffer
    assert order.metadata["strategy"] == TENNIS_TRACK and order.metadata["record_id"] == "r"
    assert order.metadata["price_signal_status"] == "free_public_consensus_line"
    assert order.metadata["consensus_method"] == "sharp_book"


def test_lean_refuses_small_gaps_one_sided_books_and_unexecutable_touches() -> None:
    market = Market(Venue.POLYMARKET, "P", "A vs B")
    line = consensus_line([quote("pinnacle", "2", "2")], as_of=AS_OF)
    lean = TennisBasisLean()
    assert lean.evaluate(market, book("0.51", "0.53"), D("0.50"), record_id="r", consensus=line).reason == "gap_below_threshold"
    one_sided = OrderBook(market_id="P", asks=(PriceLevel(D("0.70"), D("5")),))
    assert lean.evaluate(market, one_sided, D("0.50"), record_id="r", consensus=line).reason == "no_two_sided_mid"
    # Mid 0.535 is 3.5c above 0.50 but the bid (0.50) offers no edge after the fee buffer.
    wide = lean.evaluate(market, book("0.50", "0.57"), D("0.50"), record_id="r", consensus=line)
    assert not wide.traded and wide.reason in ("no_edge", "below_edge_threshold") and wide.gap == D("0.035")


# --------------------------------------------------------------------------
# Outside sources
# --------------------------------------------------------------------------
ODDS_ITEM = {
    "id": "abc123",
    "sport_key": "tennis_wta_guadalajara_open",
    "commence_time": "2026-09-15T20:00:00Z",
    "home_team": "Janice Tjen",
    "away_team": "Sloane Stephens",
    "bookmakers": [
        {"key": "pinnacle", "title": "Pinnacle", "last_update": "2026-09-15T09:50:00Z", "markets": [{"key": "h2h", "outcomes": [{"name": "Sloane Stephens", "price": 2.2}, {"name": "Janice Tjen", "price": 1.8}]}]},
        {"key": "betfair_ex_eu", "title": "Betfair", "last_update": "2026-09-15T09:50:00Z", "markets": [{"key": "h2h_lay", "outcomes": [{"name": "Janice Tjen", "price": 1.9}, {"name": "Sloane Stephens", "price": 2.3}]}]},
        {"key": "broken", "title": "Broken", "markets": [{"key": "h2h", "outcomes": [{"name": "Janice Tjen", "price": 1.0}, {"name": "Sloane Stephens", "price": 2.0}]}]},
    ],
}


def test_parse_event_reads_the_v4_shape_and_keeps_only_valid_h2h_quotes() -> None:
    ev = parse_event(ODDS_ITEM, source="test")
    assert ev.event_id == "abc123" and ev.commence_time == datetime(2026, 9, 15, 20, tzinfo=UTC)
    assert [q.book for q in ev.quotes] == ["pinnacle"]  # lay market and odds <= 1 dropped
    assert ev.quotes[0].odds_a == D("1.8") and ev.quotes[0].odds_b == D("2.2")
    p, line, side = ev.probability_for("Stephens", as_of=datetime(2026, 9, 15, 10, tzinfo=UTC))
    assert side == "b" and p.quantize(D("0.0001")) == D("0.4500") and line.method == "sharp_book"
    assert ev.probability_for("Alcaraz")[2] == "unmatched"
    assert parse_event({"id": "x", "home_team": "A"}, source="t") is None


async def test_odds_api_source_lists_tennis_keys_spends_within_budget_and_reads_headers() -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_get(path: str, params: dict) -> tuple[object, dict[str, str]]:
        calls.append((path, params))
        if path == "/sports":
            return (
                [
                    {"key": "tennis_wta_guadalajara_open", "group": "Tennis", "active": True, "has_outrights": False},
                    {"key": "tennis_atp_chengdu_open", "group": "Tennis", "active": True, "has_outrights": False},
                    {"key": "tennis_atp_finals_winner", "group": "Tennis", "active": True, "has_outrights": True},
                    {"key": "soccer_epl", "group": "Soccer", "active": True, "has_outrights": False},
                ],
                {"x-requests-remaining": "120"},
            )
        if path.endswith("/odds"):
            remaining = 119 if "chengdu" in path else 118
            return [ODDS_ITEM] if "guadalajara" in path else [], {"x-requests-remaining": str(remaining)}
        raise AssertionError(path)

    source = OddsApiSource("key", http_get=fake_get, max_credits_per_run=6)
    batch = await source.fetch(as_of=AS_OF)
    assert batch.sport_keys == ["tennis_atp_chengdu_open", "tennis_wta_guadalajara_open"]  # no outrights, no soccer
    assert batch.credits_used == 2 and batch.requests_remaining == 118 and batch.errors == []
    assert len(batch.events) == 1 and batch.events[0].sport_key == "tennis_wta_guadalajara_open"
    odds_calls = [p for path, p in calls if path.endswith("/odds")]
    assert all(p["markets"] == "h2h" and p["regions"] == "eu" and p["oddsFormat"] == "decimal" for p in odds_calls)

    tight = await OddsApiSource("key", http_get=fake_get, max_credits_per_run=1).fetch(as_of=AS_OF)
    assert tight.credits_used == 1 and any("budget" in e for e in tight.errors)

    floor = await OddsApiSource("key", http_get=fake_get, min_remaining=200).fetch(as_of=AS_OF)
    assert floor.credits_used == 0 and any("min_remaining" in e for e in floor.errors)

    with pytest.raises(ValueError):
        OddsApiSource("")


async def test_outside_source_failures_are_reported_not_raised(tmp_path: Path) -> None:
    async def failing(path: str, params: dict) -> tuple[object, dict[str, str]]:
        raise ConnectionError("offline")

    batch = await OddsApiSource("key", http_get=failing).fetch(as_of=AS_OF)
    assert batch.events == [] and "ConnectionError" in batch.errors[0]

    saved = tmp_path / "odds.json"
    saved.write_text(json.dumps([ODDS_ITEM]))
    file_batch = await JsonOutsideSource(saved).fetch(as_of=AS_OF)
    assert len(file_batch.events) == 1 and file_batch.errors == []
    broken = tmp_path / "broken.json"
    broken.write_text("{nope")
    assert (await JsonOutsideSource(broken).fetch(as_of=AS_OF)).errors


def test_build_outside_source_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    assert build_outside_source(use_fixtures=True) is None
    assert isinstance(build_outside_source(use_fixtures=False), NullOutsideSource)
    assert isinstance(build_outside_source(use_fixtures=False, odds_file=tmp_path / "x.json"), JsonOutsideSource)
    monkeypatch.setenv("ODDS_API_KEY", "k")
    src = build_outside_source(use_fixtures=False)
    assert isinstance(src, OddsApiSource) and src.regions == "eu"


# --------------------------------------------------------------------------
# Venue market normalisation and pairing
# --------------------------------------------------------------------------
def _kalshi(ticker: str, yes: str, event_title: str, series: str = "KXWTAMATCH") -> Market:
    return Market(
        Venue.KALSHI, ticker, f"{yes} wins",
        metadata={"series_ticker": series, "event_ticker": ticker.rsplit("-", 1)[0], "event_title": event_title, "subtitle": yes, "occurrence_datetime": "2026-09-15T21:00:00Z", "resolution_text": KALSHI_RULES},
    )


def _poly(market_id: str, a: str, b: str, kind: str = "moneyline") -> Market:
    return Market(
        Venue.POLYMARKET, market_id, f"Open: {a} vs {b}", yes_token_id="y", no_token_id="n",
        metadata={"slug": f"slug-{market_id}", "outcomes": [a, b], "game_start_time": "2026-09-15 20:00:00+00", "sports_market_type": kind, "resolution_text": POLY_RULES},
    )


def test_venue_markets_are_normalised_to_one_yes_player_each() -> None:
    k = kalshi_match_market(_kalshi("KXWTAMATCH-26SEP15STETJE-TJE", "Janice Tjen", "Stephens vs Tjen"))
    assert k.yes_player == "Janice Tjen" and k.opponent == "Stephens" and k.match_key == "kalshi:KXWTAMATCH-26SEP15STETJE"
    assert k.scheduled_start == datetime(2026, 9, 15, 21, tzinfo=UTC) and k.lookup == "KXWTAMATCH-26SEP15STETJE-TJE"
    assert kalshi_match_market(_kalshi("KXFEDDECISION-26SEP-C25", "Cut", "Fed", series="KXFEDDECISION")) is None
    p = polymarket_match_market(_poly("0xabc", "Tatjana Maria", "Taylor Townsend"))
    assert p.yes_player == "Tatjana Maria" and p.opponent == "Taylor Townsend" and p.lookup == "slug-0xabc"
    assert p.scheduled_start == datetime(2026, 9, 15, 20, tzinfo=UTC)
    assert polymarket_match_market(_poly("0xset", "Tatjana Maria", "Taylor Townsend", kind="tennis_set_winner")) is None
    assert polymarket_match_market(_poly("0xyn", "Yes", "No")) is None
    snap = VenueSnapshot(Venue.KALSHI, "test", markets=[_kalshi("KXWTAMATCH-26SEP15STETJE-TJE", "Janice Tjen", "Stephens vs Tjen"), _kalshi("KXFEDDECISION-26SEP-C25", "Cut", "Fed", series="KXFEDDECISION")])
    assert len(tennis_markets(snap)) == 1


def test_pairing_is_fail_closed_on_ambiguity_opponent_and_time_window() -> None:
    vm = kalshi_match_market(_kalshi("KXWTAMATCH-26SEP15STETJE-TJE", "Janice Tjen", "Stephens vs Tjen"))
    params = BasisParameters()
    pin = [quote("pinnacle", "1.8", "2.2")]
    good = event("e1", "Janice Tjen", "Sloane Stephens", pin, commence=datetime(2026, 9, 15, 20, tzinfo=UTC))
    assert pair_outside_event(vm, [good], parameters=params).reason == "paired"
    assert pair_outside_event(vm, [], parameters=params).reason == "no_outside_match"
    dup = event("e2", "J. Tjen", "S. Stephens", pin, commence=datetime(2026, 9, 15, 20, tzinfo=UTC))
    assert pair_outside_event(vm, [good, dup], parameters=params).reason == "ambiguous_match"
    wrong_opponent = event("e3", "Janice Tjen", "Iga Swiatek", pin, commence=datetime(2026, 9, 15, 20, tzinfo=UTC))
    assert pair_outside_event(vm, [wrong_opponent], parameters=params).reason == "no_outside_match"
    far = event("e4", "Janice Tjen", "Sloane Stephens", pin, commence=datetime(2026, 9, 20, 20, tzinfo=UTC))
    assert pair_outside_event(vm, [far], parameters=params).reason == "no_outside_match"
    # Initials disambiguate sisters; a bare surname against two same-surname players cannot.
    sisters = event("e5", "Venus Williams", "Serena Williams", pin)
    w = kalshi_match_market(_kalshi("KXWTAMATCH-26SEP15WILWIL-VEN", "Venus Williams", "Williams vs Williams"))
    assert pair_outside_event(w, [sisters], parameters=params).reason == "paired"
    bare = kalshi_match_market(_kalshi("KXWTAMATCH-26SEP15WILWIL-WIL", "Williams", "Williams vs Williams"))
    assert pair_outside_event(bare, [sisters], parameters=params).reason == "no_outside_match"


def test_post_start_settlement_classification() -> None:
    assert classify_settlement(Venue.KALSHI, {"status": "finalized", "result": "yes"}) == ("confirmed", "kalshi_result:yes")
    assert classify_settlement(Venue.KALSHI, {"status": "finalized", "result": ""})[0] == "excluded"
    assert classify_settlement(Venue.KALSHI, {"status": "active", "result": ""}) == ("pending", None)
    assert classify_settlement(Venue.POLYMARKET, {"closed": True, "outcome_prices": ["0", "1"]})[0] == "confirmed"
    assert classify_settlement(Venue.POLYMARKET, {"closed": True, "outcome_prices": ["0.5", "0.5"]})[0] == "excluded"
    assert classify_settlement(Venue.POLYMARKET, {"closed": False, "outcome_prices": ["0.99", "0.01"]}) == ("pending", None)
    assert classify_settlement(Venue.POLYMARKET, None) == ("pending", None)
    assert classify_settlement(Venue.POLYMARKET, {"closed": True, "outcome_prices": ["1", "0"]}, "retired") == ("excluded", "operator_result:retired")
    assert classify_settlement(Venue.POLYMARKET, {"closed": True, "outcome_prices": ["1", "0"]}, "completed")[0] == "confirmed"


# --------------------------------------------------------------------------
# Fixture replay through the live code path
# --------------------------------------------------------------------------
async def test_replay_measures_every_scripted_branch() -> None:
    summary, ledger, register, steps = await replay_fixture()
    assert [s.metrics["step"] for s in steps] == ["t0_open", "t1_track", "t2_last_pre_start", "t3_in_play", "t4_settlement"]
    assert summary.candidates == 63 and summary.admitted == 5 and summary.proposed_orders == 5 and summary.paper_fills == 5
    assert summary.metrics["unwind_fills"] == 5 and summary.metrics["gaps_opened"] == 5 and summary.metrics["gaps_closed"] == 5
    reasons = summary.refused_by_reason
    for reason in ("settlement_basis_itf", "settlement_basis_unreadable", "ambiguous_match", "no_outside_match", "consensus_insufficient_books", "no_two_sided_mid", "event_started", "gap_below_threshold", "duplicate_side"):
        assert reasons[reason] >= 1, reason

    by_market = {r.market_id: r for r in register.records.values()}
    k1 = by_market["KXWTAMATCH-26SEP15STETJE-TJE"]
    assert k1.side == "sell" and k1.gap_open == "0.0600" and k1.gap_final == "0.0200"
    assert D(k1.closure_fraction) == D("0.6667") and k1.closed_half and not k1.overshoot
    assert k1.status == "closed_at_start" and k1.settlement_status == "confirmed"
    assert k1.scheduled_start == "2026-09-15T20:00:00+00:00"  # outside commence_time, not Kalshi's 21:00 session
    assert [o["pre_start"] for o in k1.observations] == [True, True, True, False]
    assert k1.paper["unwind"]["price"] == "0.5700"  # last pre-start mid, not the 0.75 in-play print
    assert "KXWTAMATCH-26SEP15STETJE-STE" not in by_market  # the mirror side never opens

    k2 = by_market["KXWTAMATCH-26SEP15PARBEJ-PAR"]
    assert D(k2.closure_fraction) == D("-0.7500") and not k2.closed_half
    assert k2.settlement_status == "excluded" and k2.settlement_detail.startswith("kalshi_non_binary_settlement")

    p1 = by_market["0xfixture-tennis-maria-townsend"]
    assert p1.side == "buy" and p1.gap_open == "-0.0500" and D(p1.closure_fraction) == D("1.6000") and p1.overshoot
    assert p1.settlement_status == "confirmed"

    p2 = by_market["0xfixture-tennis-bouzas-salkova"]
    assert p2.consensus_method_open == "median" and p2.books_open == ["betsson", "unibet_fr", "williamhill"]
    assert p2.closed_half and p2.settlement_status == "excluded" and p2.settlement_detail == "operator_result:retired"

    p3 = by_market["0xfixture-tennis-gray-maloney"]
    assert D(p3.closure_fraction) == D("0.5000") and p3.closed_half  # exactly half counts
    assert p3.observations[-1]["method"] == "market_missing" and p3.settlement_status == "pending"
    assert p3.paper["unwind"]["price"] == "0.5300"

    v = summary.metrics["verdict"]
    assert v["n"] == 2 and v["closed_half"] == 2 and v["status"] == "insufficient_sample"
    assert v["excluded_settlement_mismatch"] == 2 and v["pending_settlement_check"] == 1 and v["overshoots"] == 1
    assert summary.metrics["verdict_provisional_including_pending"]["n"] == 3
    assert summary.metrics["by_venue"]["kalshi"]["confirmed"]["n"] == 1

    # Ledger: 5 leans + 5 unwinds, everything flat, identity holds, fees on both venues.
    assert ledger.ledger_id == TENNIS_TRACK and len(ledger.fills) == 10 and ledger.open_positions == []
    assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl
    assert sum(1 for f in ledger.fills if f.order_id == UNWIND_ORDER_ID) == 5
    k1_fills = [f for f in ledger.fills if f.market_id == "KXWTAMATCH-26SEP15STETJE-TJE"]
    assert [str(f.price) for f in k1_fills] == ["0.60", "0.5700"] and [str(f.fee) for f in k1_fills] == ["0.17", "0.18"]
    assert k1.paper["realized_pnl"] == "-0.0500"  # (0.60 - 0.57) * 10 - 0.35 fees
    poly_fee = [f for f in ledger.fills if f.market_id == "0xfixture-tennis-maria-townsend"][0].fee
    assert poly_fee == D("10") * D("0.05") * D("0.36") * D("0.64")  # 5% sports taker fee
    assert all(row["strategy"] == TENNIS_TRACK for row in summary.fills)
    assert summary.metrics["status"] == "fixture_synthetic" and summary.metrics["network_status"] == "fixture_synthetic"


async def test_register_round_trips_and_a_resumed_replay_matches_a_straight_one(tmp_path: Path) -> None:
    steps = load_replay()
    straight, _, straight_register, _ = await replay_fixture(steps)
    first, ledger, register, _ = await replay_fixture(steps[:2])
    assert len(register.open_records()) == 5
    path = tmp_path / "register.json"
    register.save(path)
    ledger.save(tmp_path / "ledger.json")
    from core.ledger import PaperLedger

    reloaded = GapRegister.load_or_create(path)
    resumed, ledger2, register2, _ = await replay_fixture(steps[2:], ledger=PaperLedger.load(tmp_path / "ledger.json"), register=reloaded)
    assert {k: (r.status, r.closure_fraction, r.settlement_status) for k, r in register2.records.items()} == {
        k: (r.status, r.closure_fraction, r.settlement_status) for k, r in straight_register.records.items()
    }
    assert ledger2.equity == ledger2.starting_cash + ledger2.realized_pnl + ledger2.unrealized_pnl
    with pytest.raises(ValueError):
        GapRegister.from_dict({"paper_only": False, "records": []})


async def test_replaying_a_closed_register_again_opens_nothing_new() -> None:
    _, ledger, register, _ = await replay_fixture()
    again, ledger2, register2, _ = await replay_fixture(ledger=ledger, register=register)
    assert again.admitted == 0 and again.paper_fills == 0 and len(register2.records) == 5
    assert again.refused_by_reason["record_already_closed"] >= 5
    assert ledger2.equity == ledger.equity


async def test_network_path_without_a_key_is_an_honest_empty() -> None:
    step = load_replay()[0]

    async def no_lookup(record):
        return None

    summary, ledger, register = await measure_tennis_basis(
        use_fixtures=False, snapshots=step.snapshots, outside_source=NullOutsideSource(), as_of=step.as_of, settlement_lookup=no_lookup
    )
    assert summary.metrics["status"] == "no_outside_source"
    assert summary.metrics["network_status"].startswith("UNKNOWN")
    assert summary.admitted == 0 and summary.paper_fills == 0 and register.records == {}
    assert ledger.equity == ledger.starting_cash
    assert set(summary.refused_by_reason) <= {"no_outside_match", "settlement_basis_itf", "settlement_basis_unreadable", "duplicate_side"}


async def test_exploding_outside_source_does_not_take_the_run_down() -> None:
    step = load_replay()[0]

    class Exploding:
        name = "exploding"

        async def fetch(self, *, as_of):
            raise RuntimeError("boom")

    async def no_lookup(record):
        return None

    summary, _, _ = await measure_tennis_basis(use_fixtures=False, snapshots=step.snapshots, outside_source=Exploding(), as_of=step.as_of, settlement_lookup=no_lookup)
    assert summary.metrics["status"] == "outside_source_errors"
    assert "RuntimeError" in summary.metrics["outside_source"]["errors"][0]


async def test_network_style_cycle_with_static_lines_opens_and_tracks_gaps() -> None:
    steps = load_replay()
    ledger = register = None
    for step in steps[:3]:
        async def lookup(record, _s=step):
            return _s.settlements.get(f"{record.venue}:{record.market_id}")

        summary, ledger, register = await measure_tennis_basis(
            use_fixtures=False, snapshots=step.snapshots, outside_source=StaticOutsideSource(step.outside, source="the_odds_api"),
            as_of=step.as_of, ledger=ledger, register=register, settlement_lookup=lookup,
        )
    assert summary.metrics["status"] == "measured" and summary.metrics["network_status"] == "measured_against_free_source"
    assert len(register.open_records()) == 5 and all(len(r.observations) >= 2 for r in register.open_records())


# --------------------------------------------------------------------------
# Artifacts and CLI
# --------------------------------------------------------------------------
async def test_scoreboard_artifact_is_measured_and_ledger_backed(tmp_path: Path) -> None:
    summary, ledger, _ = await measure_tennis_basis()
    artifact = persist_run(
        [summary], {TENNIS_TRACK: ledger}, artifact_dir=tmp_path, mode="fixtures", measured_at="t", limit=60, kalshi_env=None,
        scoreboard_name="scoreboard_tennis_basis.json", write_latest=False,
        artifact_kwargs={"primary_track": TENNIS_TRACK, "venue_focus": "cross", "label_suffix": "TENNIS BASIS", "track_family": "tennis_basis"},
    )
    assert artifact["meta"]["source"] == "measured" and artifact["meta"]["track_family"] == "tennis_basis"
    assert artifact["meta"]["pnl_source"] == "core.ledger.PaperLedger"
    assert artifact["totals"]["paper_pnl"] == ledger.total_pnl.quantize(D("0.0001"))
    assert artifact["totals"]["paper_fills"] == 5 and artifact["totals"]["fill_rate"] == D("1.0000")
    row = artifact["tracks"][0]
    assert row["track"] == TENNIS_TRACK and row["metrics"]["records"] == {"count": 5, "detail": "tennis_basis_latest.json"}
    assert row["metrics"]["verdict"]["status"] == "insufficient_sample"
    assert (tmp_path / "paper" / f"ledger_{TENNIS_TRACK}.json").exists()
    assert not (tmp_path / "scoreboard_latest.json").exists()
    json.dumps(artifact, default=json_default)


def test_cli_fixture_run_writes_report_register_and_ledger(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apps.measure_tennis_basis", "--artifact-dir", str(tmp_path)],
        check=False, capture_output=True, text=True,
        env={**os.environ, "TRADING_MODE": "paper", "ENABLE_LIVE_TRADING": "false"},
    )
    assert result.returncode == 0, result.stderr
    assert "status=fixture_synthetic" in result.stdout and "insufficient_sample" in result.stdout
    report = json.loads((tmp_path / "tennis_basis_latest.json").read_text())
    assert report["paper_only"] is True and report["kind"] == "tennis_basis_report"
    assert report["verdict"]["n"] == 2 and report["register"]["settlement_excluded"] == 2
    assert report["experiment"]["pre_registered"]["min_sample"] == 30
    assert report["not_validated"]
    assert (tmp_path / "tennis_basis" / "register.json").exists()
    assert (tmp_path / "paper" / "ledger_tennis_basis.json").exists()
    assert (tmp_path / "scoreboard_tennis_basis.json").exists()
    assert not (tmp_path / "scoreboard_latest.json").exists()


def test_cli_refuses_live_flags(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apps.measure_tennis_basis", "--artifact-dir", str(tmp_path)],
        check=False, capture_output=True, text=True,
        env={**os.environ, "TRADING_MODE": "live", "ENABLE_LIVE_TRADING": "true"},
    )
    assert result.returncode != 0
    assert not (tmp_path / "tennis_basis_latest.json").exists()
