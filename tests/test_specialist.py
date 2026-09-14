import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apps.measure_all import json_default, persist_run
from core.types import Outcome, Venue
from research.scoreboard import TRACKS, measure_all_with_ledgers
from research.scoreboard_artifact import build_scoreboard_artifact
from research.specialist_scoreboard import (
    SPECIALIST_TRACK,
    SpecialistState,
    load_specialist_state,
    measure_specialist_lane,
    state_path,
)
from research.specialist_sources import (
    FIXTURE_PATH,
    FixtureTraderSource,
    NullTraderSource,
    PolymarketDataApiSource,
    PolymarketResolutionOracle,
    TraderHistoryBatch,
    build_trader_source,
    load_fixture_bets,
    parse_closed_position,
    parse_leaderboard,
    parse_open_position,
    parse_resolution,
    specialist_category,
)
from strategies.specialist import (
    STATUS_FAIL,
    STATUS_NO_FOLLOWS,
    STATUS_PASS,
    STATUS_PENDING,
    STATUS_UNDERPOWERED,
    FollowedBet,
    SpecialistParameters,
    TraderBet,
    bet_brier_skill,
    binomial_tail,
    brier_skill,
    directional_forecast,
    evaluate_follows,
    preregistration,
    promote,
    score_traders,
    specialists,
    wins_required,
)

D = Decimal
AS_OF = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
YES, NO = Outcome.YES, Outcome.NO


def bet(
    trader: str,
    category: str,
    entry: str,
    direction: Outcome = YES,
    *,
    won: bool | None = True,
    size: str = "300",
    mid: str | None = None,
    index: int = 0,
    market_id: str | None = None,
    realized: str | None = None,
) -> TraderBet:
    resolved = won is not None
    outcome = None
    if resolved:
        outcome = direction if won else (NO if direction is YES else YES)
    placed = AS_OF - timedelta(days=60) + timedelta(hours=index)
    return TraderBet(
        bet_id=f"{trader}-{category}-{index}",
        trader=trader,
        venue=Venue.POLYMARKET,
        market_id=market_id or f"m-{trader}-{category}-{index}",
        category=category,
        direction=direction,
        entry_price=D(entry),
        size=D(size),
        placed_at=placed,
        resolved=resolved,
        outcome=outcome,
        resolved_at=placed + timedelta(days=1) if resolved else None,
        market_mid_at_entry=D(mid) if mid is not None else None,
        realized_pnl=D(realized) if realized is not None else None,
    )


def follow(
    category: str,
    direction: Outcome,
    mid: str,
    *,
    won: bool | None,
    index: int = 0,
) -> FollowedBet:
    outcome = None
    if won is not None:
        outcome = direction if won else (NO if direction is YES else YES)
    return FollowedBet(
        follow_id=f"f-{category}-{index}",
        trader="t",
        category=category,
        venue=Venue.POLYMARKET,
        market_id=f"fm-{category}-{index}",
        direction=direction,
        title="",
        followed_at=AS_OF,
        mid_at_follow=D(mid),
        quantity=D("10"),
        resolved=won is not None,
        outcome=outcome,
    )


# --------------------------------------------------------------------------
# Pure arithmetic
# --------------------------------------------------------------------------
def test_directional_brier_skill_hand_calculation() -> None:
    shade = D("0.5")
    assert directional_forecast(D("0.4"), YES, shade) == D("0.7")
    assert directional_forecast(D("0.4"), NO, shade) == D("0.2")
    # YES bet at m=0.4: won -> (0.6^2 - 0.3^2) = 0.27 ; lost -> (0.4^2 - 0.7^2) = -0.33
    assert brier_skill(D("0.4"), YES, YES, shade) == D("0.27")
    assert brier_skill(D("0.4"), YES, NO, shade) == D("-0.33")
    # NO bet at m=0.4 that resolves NO: 0.16 - 0.04 = 0.12
    assert brier_skill(D("0.4"), NO, NO, shade) == D("0.12")


def test_bet_pnl_roi_and_brier_proxy() -> None:
    b = bet("t", "tennis", "0.40", YES, won=True, size="10", mid="0.42")
    assert b.cost == D("4") and b.pnl == D("6") and b.won is True
    lost_no = bet("t", "tennis", "0.30", NO, won=False, size="10")
    assert lost_no.yes_equivalent_entry == D("0.70") and lost_no.pnl == D("-3")
    venue_reported = bet("t", "tennis", "0.30", YES, won=True, size="10", realized="5.5")
    assert venue_reported.pnl == D("5.5")
    # Benchmark is the recorded mid when present, else the YES-equivalent entry (flagged).
    skill, proxy = bet_brier_skill(b, D("0.5"))
    assert (skill, proxy) == (brier_skill(D("0.42"), YES, YES, D("0.5")), False)
    skill, proxy = bet_brier_skill(lost_no, D("0.5"))
    assert proxy is True and skill == brier_skill(D("0.70"), NO, YES, D("0.5"))
    assert bet_brier_skill(bet("t", "tennis", "0.5", won=None), D("0.5")) is None


def test_bet_validation() -> None:
    with pytest.raises(ValueError):
        TraderBet("x", "t", Venue.POLYMARKET, "m", "tennis", YES, D("0.5"), D("1"), AS_OF, resolved=True)
    with pytest.raises(ValueError):
        TraderBet("x", "t", Venue.POLYMARKET, "m", "tennis", YES, D("0.5"), D("1"), AS_OF, outcome=YES)
    with pytest.raises(ValueError):
        TraderBet("x", "t", Venue.POLYMARKET, "m", "tennis", YES, D("0.5"), D("1"), AS_OF.replace(tzinfo=None))
    with pytest.raises(ValueError):
        TraderBet("x", "t", Venue.POLYMARKET, "m", "", YES, D("0.5"), D("1"), AS_OF)


def test_parameters_are_validated_and_preregistered() -> None:
    with pytest.raises(ValueError):
        SpecialistParameters(min_resolved_bets=30, window_bets=20)
    with pytest.raises(ValueError):
        SpecialistParameters(top_fraction=D("0"))
    with pytest.raises(ValueError):
        SpecialistParameters(alpha=D("1"))
    pre = preregistration(SpecialistParameters())
    assert pre["n"] == 30 and pre["alpha"] == D("0.05") and pre["wins_required_at_n"] == 20
    assert "underpowered" in pre["underpowered_rule"]


def test_rolling_window_is_count_based_and_in_category_only() -> None:
    params = SpecialistParameters()
    bets = [bet("t", "macro", "0.5", won=False, index=i) for i in range(5)]  # oldest: five losses
    bets += [bet("t", "macro", "0.5", won=True, index=10 + i) for i in range(20)]  # newest: 20 wins
    bets += [bet("t", "tennis", "0.5", won=False, index=100 + i) for i in range(15)]  # other category
    scores = {(s.trader, s.category): s for s in score_traders(bets, params)}
    macro = scores[("t", "macro")]
    assert macro.resolved_bets == 20 and macro.total_resolved == 25 and macro.wins == 20
    assert macro.roi == D("1") and macro.hit_rate == D("1")
    assert macro.eligible
    tennis = scores[("t", "tennis")]
    assert tennis.wins == 0 and tennis.reasons == ["negative_roi", "negative_brier"]
    # Specialization uses the window notional of every category the trader touched.
    assert macro.specialization == D("3000") / D("5250")


def test_scoring_reasons() -> None:
    params = SpecialistParameters()
    few = [bet("few", "macro", "0.5", won=True, index=i) for i in range(9)]
    small = [bet("small", "macro", "0.5", won=True, size="1", index=i) for i in range(12)]
    losing = [bet("lose", "macro", "0.5", won=i % 3 == 0, index=i) for i in range(12)]
    longshots = [bet("long", "macro", "0.2", won=i % 4 == 0, size="600", index=i) for i in range(12)]
    scores = {s.trader: s for s in score_traders(few + small + losing + longshots, params)}
    assert scores["few"].reasons == ["insufficient_history"]
    assert scores["small"].reasons == ["below_notional"]
    assert scores["lose"].roi < 0 and "negative_roi" in scores["lose"].reasons
    # 3 of 12 longshots at 0.20 win: ROI +20% but the directional tilt loses on Brier.
    assert scores["long"].roi > 0 and scores["long"].reasons == ["negative_brier"]


def test_promotion_is_top_decile_per_category_and_positive() -> None:
    params = SpecialistParameters()
    bets: list[TraderBet] = []
    # 12 tennis traders with distinct positive ROI -> ceil(1.2) = 2 promoted.
    for rank, wins in enumerate(range(19, 7, -1)):
        trader = f"tennis-{rank:02d}"
        bets += [bet(trader, "tennis", "0.5", won=i < wins, index=i) for i in range(20)]
    # The best tennis trader is terrible in soccer.
    bets += [bet("tennis-00", "soccer", "0.5", won=False, index=i) for i in range(12)]
    # A lone crypto trader with negative ROI: decile of one, still not promoted.
    bets += [bet("crypto-x", "crypto", "0.5", won=i < 4, index=i) for i in range(12)]
    scores = promote(score_traders(bets, params), params)
    promoted = {(s.trader, s.category) for s in specialists(scores)}
    assert promoted == {("tennis-00", "tennis"), ("tennis-01", "tennis")}
    by_key = {(s.trader, s.category): s for s in scores}
    assert by_key[("tennis-00", "tennis")].rank == 1 and by_key[("tennis-00", "tennis")].scored_in_category == 12
    assert by_key[("tennis-02", "tennis")].reasons == ["below_top_decile"]
    assert by_key[("tennis-00", "soccer")].rank == 1 and not by_key[("tennis-00", "soccer")].promoted
    assert by_key[("crypto-x", "crypto")].rank == 1 and "negative_roi" in by_key[("crypto-x", "crypto")].reasons


def test_promotion_breaks_roi_ties_on_brier_skill() -> None:
    params = SpecialistParameters()
    # Same ROI (7/12 wins at 0.5) but "b" recorded mids that make its calls look better.
    a = [bet("a", "golf", "0.5", won=i < 7, index=i, mid="0.50") for i in range(12)]
    b = [bet("b", "golf", "0.5", won=i < 7, index=i, mid="0.45" if i < 7 else "0.50") for i in range(12)]
    scores = promote(score_traders(a + b, params), params)
    by_trader = {s.trader: s for s in scores}
    assert by_trader["a"].roi == by_trader["b"].roi
    assert by_trader["b"].rank == 1 and by_trader["a"].rank == 2


def test_binomial_tail_and_wins_required() -> None:
    assert binomial_tail(3, 3) == D("0.125")
    assert binomial_tail(0, 5) == D("1")
    assert binomial_tail(5, 5) == D("0.03125")
    assert wins_required(30, D("0.05")) == 20
    assert binomial_tail(20, 30) < D("0.05") < binomial_tail(19, 30)
    assert wins_required(3, D("0.05")) is None  # three follows cannot reach significance


def test_followed_bet_excess_vs_mid_and_round_trip() -> None:
    yes_win = follow("macro", YES, "0.42", won=True)
    assert yes_win.direction_mid == D("0.42") and yes_win.excess_vs_mid == D("0.58")
    no_win = follow("macro", NO, "0.405", won=True, index=1)
    assert no_win.direction_mid == D("0.595") and no_win.excess_vs_mid == D("0.405")
    yes_loss = follow("macro", YES, "0.42", won=False, index=2)
    assert yes_loss.excess_vs_mid == D("-0.42")
    assert yes_win.brier_skill_vs_mid(D("0.5")) == brier_skill(D("0.42"), YES, YES, D("0.5"))
    rebuilt = FollowedBet.from_dict(json.loads(json.dumps(no_win.as_dict(), default=json_default)))
    assert rebuilt.mid_at_follow == D("0.405") and rebuilt.outcome is NO and rebuilt.won is True
    assert follow("macro", YES, "0.42", won=None).excess_vs_mid is None


def test_evaluation_statuses_follow_the_preregistration() -> None:
    params = SpecialistParameters()
    assert evaluate_follows([], params).status == STATUS_NO_FOLLOWS
    pending = [follow("macro", YES, "0.5", won=None, index=i) for i in range(3)]
    assert evaluate_follows(pending, params).status == STATUS_PENDING
    three = [follow("macro", YES, "0.5", won=True, index=i) for i in range(3)]
    ev = evaluate_follows(three, params)
    assert ev.status == STATUS_UNDERPOWERED and ev.wins == 3 and ev.sign_test_p == D("0.125")
    assert "27 more needed" in ev.note and ev.powered is False
    # N reached, 25/30 wins at mid 0.5: significant and positive -> pass.
    powered = [follow("macro", YES, "0.5", won=i < 25, index=i) for i in range(30)]
    ev = evaluate_follows(powered, params)
    assert ev.status == STATUS_PASS and ev.powered and ev.mean_excess_vs_mid > 0 and ev.sign_test_p < D("0.05")
    # N reached, coin-flip hit rate -> fail.
    coin = [follow("macro", YES, "0.5", won=i < 15, index=i) for i in range(30)]
    assert evaluate_follows(coin, params).status == STATUS_FAIL
    # N reached, 25 wins but they were near-certain favourites: hit rate significant, mean excess negative -> fail.
    favourites = [follow("macro", YES, "0.99", won=True, index=i) for i in range(25)]
    favourites += [follow("macro", YES, "0.99", won=False, index=25 + i) for i in range(5)]
    ev = evaluate_follows(favourites, params)
    assert ev.sign_test_p < D("0.05") and ev.mean_excess_vs_mid < 0 and ev.status == STATUS_FAIL
    # Per-category readouts are never powered, whatever their size.
    per_cat = evaluate_follows(powered, params, category="macro")
    assert per_cat.status == STATUS_UNDERPOWERED and per_cat.n_resolved == 30 and not per_cat.powered
    assert evaluate_follows(powered, params, category="tennis").status == STATUS_NO_FOLLOWS


# --------------------------------------------------------------------------
# Sources: taxonomy, Data API parsing, fixture
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("texts", "slugs", "tags", "expected"),
    [
        (("LoL: G2 Esports vs T1 - Game 4 Winner",), ("lol-g2-t1-2026-07-08",), (), "esports"),
        (("Broncos vs. Chiefs",), ("nfl-den-kc-2026-09-14",), (), "american_football"),
        (("France vs. Morocco: O/U 2.5",), ("fifa-wc-fra-mar",), (), "soccer"),
        (("Will Bitcoin be above $100k on Friday?",), (), (), "crypto"),
        (("Will the Fed cut rates in September?",), (), (), "macro"),
        (("Will the highest temperature in Munich be 23°C or below on September 16?",), (), (), "weather"),
        (("Who will win the match?",), (), ("Tennis",), "tennis"),
        (("Will Kai and Speed beat the Minecraft challenge by August 17?",), (), (), "other"),
        (("Will New York beat Boston?",), (), ("NBA",), "basketball"),
    ],
)
def test_specialist_category_taxonomy(texts, slugs, tags, expected) -> None:
    assert specialist_category(*texts, slugs=slugs, tags=tags) == expected


CLOSED_WIN = {
    "proxyWallet": "0xabc", "asset": "111", "conditionId": "0xcond1", "avgPrice": 0.4339, "totalBought": 2087046,
    "realizedPnl": 1181385, "curPrice": 1, "title": "France vs. Spain: Team to Advance", "slug": "fifa-fra-esp",
    "eventSlug": "fifa-fra-esp-2026-07-10", "outcome": "Spain", "outcomeIndex": 1, "oppositeOutcome": "France",
    "oppositeAsset": "222", "endDate": "2026-07-10", "timestamp": 1784064256,
}
CLOSED_LOSS = {**CLOSED_WIN, "conditionId": "0xcond2", "asset": "333", "oppositeAsset": "444", "outcomeIndex": 0,
               "outcome": "Yes", "curPrice": 0, "realizedPnl": -905000, "avgPrice": 0.3989, "totalBought": 4011530,
               "title": "Will Japan win on 2026-06-25?", "eventSlug": "fifa-jpn-2026-06-25"}
CLOSED_EXITED = {**CLOSED_WIN, "conditionId": "0xcond3", "curPrice": 0.35}
OPEN = {
    "proxyWallet": "0xabc", "asset": "555", "conditionId": "0xcond4", "size": 4853928.4209, "avgPrice": 0.5551,
    "curPrice": 0.545, "redeemable": False, "title": "Broncos vs. Chiefs", "slug": "nfl-den-kc-2026-09-14",
    "eventSlug": "nfl-den-kc-2026-09-14", "outcome": "Chiefs", "outcomeIndex": 1, "oppositeAsset": "666",
    "endDate": "2026-09-15", "negativeRisk": False,
}
OPEN_REDEEMABLE = {**OPEN, "conditionId": "0xcond5", "curPrice": 1, "redeemable": True}


def test_parse_closed_positions_from_data_api_rows() -> None:
    win = parse_closed_position(CLOSED_WIN, trader="0xabc", as_of=AS_OF)
    assert win is not None and win.resolved and win.won is True
    assert win.direction is NO and win.outcome is NO  # outcomeIndex 1 == the market's second outcome
    assert win.category == "soccer" and win.entry_price == D("0.4339") and win.pnl == D("1181385")
    assert (win.yes_token_id, win.no_token_id) == ("222", "111")
    assert win.resolved_at == datetime.fromtimestamp(1784064256, tz=UTC)
    assert win.metadata["placed_at_is_close_time"] is True
    loss = parse_closed_position(CLOSED_LOSS, trader="0xabc", as_of=AS_OF)
    assert loss is not None and loss.direction is YES and loss.outcome is NO and loss.won is False
    assert (loss.yes_token_id, loss.no_token_id) == ("333", "444")
    assert parse_closed_position(CLOSED_EXITED, trader="0xabc", as_of=AS_OF) is None


def test_parse_open_positions_from_data_api_rows() -> None:
    open_bet = parse_open_position(OPEN, trader="0xabc", as_of=AS_OF)
    assert open_bet is not None and not open_bet.resolved and open_bet.outcome is None
    assert open_bet.category == "american_football" and open_bet.direction is NO
    assert (open_bet.yes_token_id, open_bet.no_token_id) == ("666", "555")
    assert open_bet.metadata["placed_at_unknown"] is True and open_bet.placed_at == AS_OF
    assert parse_open_position(OPEN_REDEEMABLE, trader="0xabc", as_of=AS_OF) is None


def test_parse_leaderboard_accepts_v2_and_v1_shapes() -> None:
    v2 = {"data": [{"rank": 1, "user_id": "0xAAA", "user_name": "vito", "pnl": 4751123.5, "volume": 17115020.8}]}
    v1 = [{"rank": "2", "proxyWallet": "0xBBB", "userName": "gringo", "vol": 7850042.3, "pnl": 462615.2}]
    (a,) = parse_leaderboard(v2)
    (b,) = parse_leaderboard(v1)
    assert (a.trader, a.name, a.rank, a.volume) == ("0xaaa", "vito", 1, D("17115020.8"))
    assert (b.trader, b.name, b.rank, b.volume) == ("0xbbb", "gringo", 2, D("7850042.3"))
    assert parse_leaderboard({"data": "nope"}) == [] and parse_leaderboard(None) == []


def test_parse_resolution_reads_uma_price() -> None:
    resolved_yes = {"data": [{"status": "resolved", "price": "1000000000000000000"}]}
    resolved_no = {"data": [{"status": "resolved", "price": "0"}]}
    posed = {"data": [{"status": "posed", "price": "69"}]}
    split = {"data": [{"status": "resolved", "price": "500000000000000000"}]}
    assert parse_resolution(resolved_yes) is YES and parse_resolution(resolved_no) is NO
    assert parse_resolution(posed) is None and parse_resolution(split) is None and parse_resolution({"data": []}) is None


async def test_data_api_source_walks_leaderboard_and_wallets_with_fake_http() -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_get(path: str, params: dict) -> object:
        calls.append((path, params))
        if path == "/v2/leaderboard":
            return {"data": [{"rank": 1, "user_id": "0xabc", "volume": 5}, {"rank": 2, "user_id": "0xdead", "volume": 4}]}
        if params["user"] == "0xdead":
            raise ConnectionError("wallet endpoint down")
        if path == "/closed-positions":
            return [CLOSED_WIN, CLOSED_LOSS, CLOSED_EXITED] if params["offset"] == 0 else []
        if path == "/positions":
            return [OPEN, OPEN_REDEEMABLE]
        raise AssertionError(path)

    source = PolymarketDataApiSource(traders=2, http_get=fake_get)
    batch = await source.fetch(as_of=AS_OF)
    assert [t.trader for t in batch.traders] == ["0xabc", "0xdead"]
    assert len(batch.bets) == 3 and sum(b.resolved for b in batch.bets) == 2
    assert batch.requests == len(calls) == 1 + 2 + 1  # leaderboard, one closed page (short), positions, dead wallet
    assert len(batch.errors) == 1 and "0xdead" in batch.errors[0] and "ConnectionError" in batch.errors[0]
    assert calls[0][1] == {"time_period": "month", "sort_by": "VOLUME", "limit": 2}
    assert calls[1][1]["sortBy"] == "TIMESTAMP"


async def test_data_api_leaderboard_failure_is_reported_not_raised() -> None:
    async def failing(path: str, params: dict) -> object:
        raise TimeoutError("offline")

    batch = await PolymarketDataApiSource(traders=3, http_get=failing).fetch(as_of=AS_OF)
    assert batch.bets == [] and batch.traders == []
    assert batch.errors and "leaderboard" in batch.errors[0] and "TimeoutError" in batch.errors[0]
    zero = await PolymarketDataApiSource(traders=0, http_get=failing).fetch(as_of=AS_OF)
    assert zero.bets == [] and zero.errors == [] and zero.requests == 0


async def test_resolution_oracle_collects_errors_per_market() -> None:
    async def fake_get(path: str, params: dict) -> object:
        if params["condition"] == "0xbad":
            raise ValueError("boom")
        return {"data": [{"status": "resolved", "price": "0"}]}

    oracle = PolymarketResolutionOracle(http_get=fake_get)
    out = await oracle.resolve(["0xgood", "0xbad"])
    assert out == {"0xgood": NO, "0xbad": None}
    assert len(oracle.errors) == 1 and "ValueError" in oracle.errors[0]


def test_fixture_is_synthetic_and_declared_as_such() -> None:
    traders, bets, note = load_fixture_bets(FIXTURE_PATH, as_of=AS_OF)
    assert "SYNTHETIC" in note and len(traders) == 8
    assert sum(b.resolved for b in bets) == 100 and sum(not b.resolved for b in bets) == 6
    assert {b.category for b in bets} == {"macro", "basketball", "tennis", "crypto"}
    assert all(b.venue is Venue.POLYMARKET for b in bets)


def test_build_trader_source_defaults() -> None:
    assert isinstance(build_trader_source(use_fixtures=True), FixtureTraderSource)
    assert isinstance(build_trader_source(use_fixtures=False), PolymarketDataApiSource)
    assert isinstance(build_trader_source(use_fixtures=False, traders=0), NullTraderSource)
    src = build_trader_source(use_fixtures=False, traders=7, window="week")
    assert isinstance(src, PolymarketDataApiSource) and src.traders == 7 and src.window == "week"


# --------------------------------------------------------------------------
# Track + ledger + artifact integration
# --------------------------------------------------------------------------
async def test_fixture_track_scores_promotes_and_follows() -> None:
    summaries, ledgers = await measure_all_with_ledgers()
    spec = next(s for s in summaries if s.track == SPECIALIST_TRACK)
    ledger = ledgers[SPECIALIST_TRACK]

    assert spec.metrics["status"] == "fixture_synthetic"
    assert spec.candidates == 6 and spec.admitted == 3 and spec.proposed_orders == 3 and spec.paper_fills == 3
    assert spec.refused_by_reason == {"market_not_in_snapshot": 1, "out_of_category": 1, "trader_not_promoted": 1}
    promoted = {(s["trader"], s["category"]) for s in spec.metrics["specialists"]}
    assert promoted == {("fx-macro-alpha", "macro"), ("fx-hoops-delta", "basketball"), ("fx-tennis-zeta", "tennis")}
    board = {(r["trader"], r["category"]): r for r in spec.metrics["scoreboard"]}
    assert board[("fx-macro-alpha", "macro")]["rank"] == 1 and board[("fx-macro-alpha", "macro")]["mid_proxy_bets"] == 2
    assert board[("fx-macro-beta", "macro")]["reasons"] == ["below_top_decile"]
    assert board[("fx-hoops-epsilon", "basketball")]["reasons"] == ["negative_brier", "below_top_decile"]
    assert board[("fx-macro-theta", "macro")]["reasons"] == ["below_notional"]
    assert board[("fx-crypto-eta", "crypto")]["reasons"] == ["insufficient_history"]
    assert board[("fx-hoops-delta", "tennis")]["reasons"] == ["insufficient_history", "below_notional"]

    follows = {(f["trader"], f["market_id"]): f for f in spec.metrics["follows_this_run"]}
    fed = follows[("fx-macro-alpha", "0xfixture-fed-september")]
    cpi = follows[("fx-macro-alpha", "0xfixture-cpi-august")]
    nba = follows[("fx-hoops-delta", "0xfixture-nba-ny-boston")]
    assert (fed["direction"], fed["fill_price"], fed["mid_at_follow"]) == ("yes", D("0.43"), D("0.42"))
    assert (cpi["direction"], cpi["fill_price"], cpi["mid_at_follow"]) == ("no", D("0.61"), D("0.405"))
    assert (nba["direction"], nba["fill_price"], nba["mid_at_follow"]) == ("yes", D("0.53"), D("0.52"))
    assert all(row["strategy"] == SPECIALIST_TRACK for row in spec.fills)

    assert ledger.ledger_id == SPECIALIST_TRACK and len(ledger.fills) == 3
    assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl
    assert ledger.unrealized_pnl == D("-0.35")  # paid the touch, marked at mid
    assert ledger.fees_paid == 0  # fixture Polymarket markets carry no taker fee
    assert spec.metrics["evaluation"]["pooled"]["status"] == STATUS_PENDING
    assert spec.metrics["preregistration"]["n"] == 30
    assert spec.metrics["venue_scope"].startswith("polymarket_only")
    assert spec.metrics["not_validated"][0].startswith("fixture traders and outcomes are SYNTHETIC")


async def test_carried_state_settles_follows_and_stays_underpowered() -> None:
    summaries, ledgers = await measure_all_with_ledgers()
    first = next(s for s in summaries if s.track == SPECIALIST_TRACK)
    state = SpecialistState.from_dict(first.metrics["follow_state"])
    assert state.runs == 1 and len(state.followed) == 3

    summaries2, ledgers2 = await measure_all_with_ledgers(ledgers=ledgers, specialist_state=state)
    spec = next(s for s in summaries2 if s.track == SPECIALIST_TRACK)
    ledger = ledgers2[SPECIALIST_TRACK]

    assert spec.admitted == 0 and spec.paper_fills == 0
    assert spec.refused_by_reason["already_followed"] == 3
    assert len(spec.metrics["settled_this_run"]) == 3
    pooled = spec.metrics["evaluation"]["pooled"]
    assert pooled["status"] == STATUS_UNDERPOWERED and pooled["n_resolved"] == 3 and pooled["wins"] == 3
    assert pooled["hit_rate"] == D("1") and pooled["sign_test_p"] == D("0.125")
    # (1-0.42) + (1-0.595) + (1-0.52) = 1.465 over three follows
    assert pooled["mean_excess_vs_mid"] == (D("1.465") / 3).quantize(D("0.0001"))
    assert spec.metrics["evaluation"]["by_category"]["macro"]["n_resolved"] == 2
    # Settlement booked on the ledger at 1/0: 10*(1-0.43) + 10*(1-0.61) + 10*(1-0.53)
    assert ledger.realized_pnl == D("14.3") and ledger.open_positions == []
    assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl
    assert len(ledger.equity_curve) == 2
    assert SpecialistState.from_dict(spec.metrics["follow_state"]).runs == 2

    artifact = build_scoreboard_artifact(summaries2, mode="fixtures", measured_at="t", limit=3)
    finding = artifact["findings"]["category_specialist"]
    assert finding["evaluation_status"] == STATUS_UNDERPOWERED and finding["hypothesis_validated"] is False
    assert finding["follow_log_resolved"] == 3 and finding["preregistered_n"] == 30
    assert "specialist_hypothesis_not_validated" in artifact["portfolio"]["risk_flags"]
    row = next(t for t in artifact["tracks"] if t["track"] == SPECIALIST_TRACK)
    assert row["metrics"]["follow_state"] == {"followed": 3, "detail": "specialist_scoreboard_<mode>.json"}
    assert row["metrics"]["follow_attempts"]["by_reason"]["already_followed"] == 3
    assert "traders" not in row["metrics"]
    json.dumps(artifact, default=json_default)


async def test_null_source_is_an_honest_empty_lane() -> None:
    summaries, ledgers = await measure_all_with_ledgers(specialist_source=NullTraderSource())
    spec = next(s for s in summaries if s.track == SPECIALIST_TRACK)
    assert spec.candidates == 0 and spec.paper_fills == 0 and spec.refused_by_reason == {}
    assert spec.metrics["status"] == "no_trader_source"
    assert spec.metrics["evaluation"]["pooled"]["status"] == STATUS_NO_FOLLOWS
    assert ledgers[SPECIALIST_TRACK].equity == ledgers[SPECIALIST_TRACK].starting_cash
    artifact = build_scoreboard_artifact(summaries, mode="fixtures", measured_at="t", limit=3)
    assert artifact["findings"]["category_specialist"]["status"] == "no_trader_source"
    assert "specialist_hypothesis_not_validated" not in artifact["portfolio"]["risk_flags"]


async def test_exploding_source_does_not_take_the_scoreboard_down() -> None:
    class Exploding:
        name = "exploding"

        async def fetch(self, *, as_of) -> TraderHistoryBatch:
            raise RuntimeError("boom")

    summaries, _ = await measure_all_with_ledgers(specialist_source=Exploding())
    spec = next(s for s in summaries if s.track == SPECIALIST_TRACK)
    assert spec.candidates == 0 and spec.metrics["status"] == "source_errors"
    assert "RuntimeError" in spec.metrics["source"]["errors"][0]
    assert len(summaries) == len(TRACKS) == 11


async def test_persist_run_writes_specialist_report_and_follow_state(tmp_path: Path) -> None:
    summaries, ledgers = await measure_all_with_ledgers()
    artifact = persist_run(summaries, ledgers, artifact_dir=tmp_path, mode="fixtures", measured_at="t", limit=3, kalshi_env=None)
    report = json.loads((tmp_path / "specialist_scoreboard_fixtures.json").read_text())
    assert report["kind"] == "specialist_scoreboard" and report["meta"]["source"] == "measured"
    assert report["meta"]["pnl_source"] == "core.ledger.PaperLedger"
    assert report["totals"]["specialists"] == 3 and report["totals"]["follows_this_run"] == 3
    assert report["totals"]["evaluation_status"] == STATUS_PENDING and report["totals"]["powered"] is False
    assert len(report["scoreboard"]) == 10 and len(report["follow_log"]) == 3
    assert report["preregistration"]["n"] == 30
    assert (tmp_path / "specialist_scoreboard_latest.json").exists()
    assert artifact["specialist_scoreboard"]["file"] == "specialist_scoreboard_fixtures.json"
    state = load_specialist_state(tmp_path)
    assert state is not None and len(state.followed) == 3 and state_path(tmp_path).exists()
    assert (tmp_path / "paper" / f"ledger_{SPECIALIST_TRACK}.json").exists()


async def test_persist_run_without_ledgers_does_not_write_state(tmp_path: Path) -> None:
    summaries, _ = await measure_all_with_ledgers()
    persist_run(summaries, {}, artifact_dir=tmp_path, mode="fixtures", measured_at="t", limit=3, kalshi_env=None)
    assert (tmp_path / "specialist_scoreboard_fixtures.json").exists()
    assert load_specialist_state(tmp_path) is None


async def test_measure_specialist_lane_standalone_matches_the_track() -> None:
    report, state, ledger = await measure_specialist_lane()
    assert report["totals"]["follows_this_run"] == 3 and len(state.followed) == 3
    assert ledger.ledger_id == SPECIALIST_TRACK and len(ledger.fills) == 3
    report2, state2, ledger2 = await measure_specialist_lane(state=state, ledger=ledger)
    assert report2["totals"]["follow_log_resolved"] == 3 and report2["totals"]["evaluation_status"] == STATUS_UNDERPOWERED
    assert ledger2.realized_pnl == D("14.3") and state2.runs == 2


def test_cli_fixture_runs_carry_state_and_refuse_live(tmp_path: Path) -> None:
    env = {**os.environ, "TRADING_MODE": "paper", "ENABLE_LIVE_TRADING": "false"}
    cmd = [sys.executable, "-m", "research.specialist_scoreboard", "--artifact-dir", str(tmp_path)]
    first = subprocess.run(cmd, check=False, capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stderr
    assert "status=fixture_synthetic" in first.stdout and "PROMOTED" in first.stdout
    assert "status=pending_resolutions" in first.stdout and "NOT validated" in first.stdout
    second = subprocess.run(cmd, check=False, capture_output=True, text=True, env=env)
    assert second.returncode == 0, second.stderr
    assert "status=underpowered" in second.stdout and "27 more needed" in second.stdout
    report = json.loads((tmp_path / "specialist_scoreboard_fixtures.json").read_text())
    assert report["totals"]["follow_log_resolved"] == 3
    assert report["ledger"]["realized_pnl"] == pytest.approx(14.3)

    live = subprocess.run(
        cmd + ["--json", str(tmp_path / "live.json")],
        check=False, capture_output=True, text=True,
        env={**os.environ, "TRADING_MODE": "live", "ENABLE_LIVE_TRADING": "true"},
    )
    assert live.returncode != 0 and not (tmp_path / "live.json").exists()
