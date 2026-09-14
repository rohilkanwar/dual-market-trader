import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from apps.measure_all import load_ledgers
from apps.measure_tourist_fade import run, settle_carried
from core.ledger import PaperLedger
from core.types import Fill, Outcome, Side, Venue
from research.scoreboard import VenueSnapshot
from research.tourist_fade import TOURIST_TRACK
from strategies.tourist_flow import TouristParameters

D = Decimal


def _run(tmp_path: Path, **overrides: object) -> dict:
    kwargs = dict(
        use_network=False,
        universe="tennis",
        series=None,
        limit=60,
        tape_limit=1000,
        artifact_dir=tmp_path,
        harvest_dir=tmp_path / "harvests",
        harvest_trades=False,
        settled_per_series=5,
        max_trades_per_market=100,
        kalshi_env=None,
        persist_ledgers=True,
        reset_ledgers=False,
        model_fees=True,
        params=TouristParameters(),
        min_events=10,
        min_fades=20,
    )
    kwargs.update(overrides)
    return run(**kwargs)  # type: ignore[arg-type]


async def test_fixture_run_writes_report_scoreboard_ledger_and_run_record(tmp_path: Path) -> None:
    report = await _run(tmp_path)
    assert report["paper_only"] is True and report["kind"] == "fade_the_tourist_report" and report["universe"] == "tennis"
    verdicts = {row["check"]: row["verdict"] for row in report["verdict_table"]}
    assert verdicts == {
        "fade_ev_positive_after_fees": "FAIL",
        "strong_regime_fade_ev_positive": "PASS",
        "strong_regime_beats_weak": "PASS",
        "tourist_flow_loses": "PASS",
        "tourist_worse_than_other_takers": "PASS",
    }
    assert "PASS (favourite >= 70c)" in report["headline"] and "adverse selection not detected" in report["headline"]
    assert report["ex_post"]["note"].startswith("Synthetic fixture")
    assert report["adverse_selection_fail_risk"].startswith("Flow that looks recreational can be informed")
    for key in ("classifier", "cluster", "fade", "replay_fill", "fees", "settlement", "inference", "tape_limits", "daily_loss_in_replay"):
        assert report["assumptions"][key]
    live = report["live_track"]
    assert live["candidates"] == 7 and live["admitted"] == 2 and live["paper_fills"] == 2
    assert live["refused_by_reason"] == {"cluster_stale": 1, "market_inactive": 1, "no_tape": 1, "no_tourist_cluster": 1, "one_sided_book": 1}
    assert set(live["paper_pnl_by_regime"]) == {"strong", "weak"} and live["ledger"]["open_positions"] == 2

    scoreboard = json.loads((tmp_path / "scoreboard_tourist_fade.json").read_text())
    assert scoreboard["meta"]["source"] == "measured" and scoreboard["meta"]["pnl_source"] == "core.ledger.PaperLedger"
    assert scoreboard["meta"]["label"] == "MEASURED / FIXTURES / FADE THE TOURIST"
    assert scoreboard["meta"]["track_family"] == "tourist_fade" and scoreboard["meta"]["primary_track"] == TOURIST_TRACK
    assert scoreboard["meta"]["run_id"] == report["run_id"] and scoreboard["meta"]["venues"] == ["kalshi"]
    assert [t["track"] for t in scoreboard["tracks"]] == [TOURIST_TRACK]
    assert scoreboard["tracks"][0]["metrics"]["family"] == "tourist_fade"
    assert scoreboard["findings"]["fade_the_tourist"]["verdicts"]["strong_regime_fade_ev_positive"] == "PASS"
    assert scoreboard["findings"]["fade_the_tourist"]["adverse_selection_detected"] is False
    assert scoreboard["totals"]["paper_fills"] == 2 and scoreboard["portfolio"]["source"] == "ledger_aggregate"
    assert D(str(scoreboard["totals"]["paper_pnl"])) == D(str(live["ledger"]["total_pnl"]))

    record = json.loads((tmp_path / "paper" / "runs" / f"{report['run_id']}.json").read_text())
    assert record["paper_only"] is True and record["kind"] == "fade_the_tourist" and record["track_family"] == "tourist_fade"
    assert record["tracks"][0]["family"] == "tourist_fade" and record["primary_track"] == TOURIST_TRACK
    assert (tmp_path / "paper" / f"ledger_{TOURIST_TRACK}.json").exists()
    assert (tmp_path / "paper" / f"equity_curve_{TOURIST_TRACK}.jsonl").read_text().count("\n") == 1


async def test_ledger_carries_across_runs_settles_fixture_results_and_resets(tmp_path: Path) -> None:
    first = await _run(tmp_path)
    # Plant a carried position in the fixture's settled (inactive) market: it must be settled at NO on the next run.
    ledger = load_ledgers(tmp_path, (TOURIST_TRACK,))[TOURIST_TRACK]
    ledger.record_fill(Fill(venue=Venue.KALSHI, market_id="KXBTC15M-26SEP201445-45", order_id="carried", side=Side.BUY, quantity=D("10"), price=D("0.60"), outcome=Outcome.NO, fee=D("0.10")))
    ledger.save(tmp_path / "paper" / f"ledger_{TOURIST_TRACK}.json")
    second = await _run(tmp_path)
    live = second["live_track"]
    assert live["settled_this_run"] == [{"market": "KXBTC15M-26SEP201445-45", "outcome": "no", "quantity": -10.0, "average_price": 0.4, "realized_pnl": 4.0}]
    assert live["ledger"]["settlement_fills"] == 1
    # The two open fades from run one are not re-faded.
    assert live["paper_fills"] == 0 and live["refused_by_reason"]["already_positioned"] == 2
    assert live["ledger"]["fills"] == first["live_track"]["ledger"]["fills"] + 2  # planted fill + its settlement
    assert len(load_ledgers(tmp_path, (TOURIST_TRACK,))[TOURIST_TRACK].equity_curve) == 2
    reset = await _run(tmp_path, reset_ledgers=True)
    assert reset["live_track"]["paper_fills"] == 2 and len(load_ledgers(tmp_path, (TOURIST_TRACK,))[TOURIST_TRACK].equity_curve) == 1


async def test_no_fees_and_no_persist(tmp_path: Path) -> None:
    report = await _run(tmp_path, model_fees=False, persist_ledgers=False)
    assert D(str(report["live_track"]["ledger"]["fees_paid"])) == 0
    assert D(str(report["ex_post"]["fades"]["fees"])) == 0
    assert not (tmp_path / "paper" / f"ledger_{TOURIST_TRACK}.json").exists()
    assert (tmp_path / "tourist_fade_report_latest.json").exists()


async def test_refuses_live_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    with pytest.raises(ValueError, match="paper-only"):
        await _run(tmp_path)
    assert not (tmp_path / "scoreboard_tourist_fade.json").exists()


async def test_settle_carried_on_network_reads_public_results_only() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/markets/DONE"):
            return httpx.Response(200, json={"market": {"ticker": "DONE", "result": "yes"}})
        if request.url.path.endswith("/markets/OPEN"):
            return httpx.Response(200, json={"market": {"ticker": "OPEN", "result": ""}})
        return httpx.Response(500, json={})

    ledger = PaperLedger(starting_cash=D("1000"), ledger_id="t")
    for market_id in ("DONE", "OPEN", "ERR"):
        ledger.record_fill(Fill(venue=Venue.KALSHI, market_id=market_id, order_id=market_id, side=Side.BUY, quantity=D("10"), price=D("0.70"), outcome=Outcome.YES))
    snapshot = VenueSnapshot(venue=Venue.KALSHI, source="network")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        rows = await settle_carried(ledger, snapshot, base_url="https://api.elections.kalshi.com/trade-api/v2", http=http)
    assert [r["market"] for r in rows] == ["DONE"] and rows[0]["realized_pnl"] == D("3.0000")
    assert sorted(p.market_id for p in ledger.open_positions) == ["ERR", "OPEN"]
    assert any("result[ERR]" in e for e in snapshot.errors)
    assert all(path.startswith("/trade-api/v2/markets/") for path in calls)
