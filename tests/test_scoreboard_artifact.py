import json
from decimal import Decimal
from pathlib import Path

import pytest

from apps.measure_all import json_default, load_ledgers, persist_run
from research.scoreboard import PRIMARY_TRACK, TRACKS, measure_all_with_ledgers, settlement_gate
from research.scoreboard_artifact import SampleSourceRefused, build_scoreboard_artifact
from strategies.matching import MarketMatcher
from venues.fixtures import load_fixture
from venues.kalshi.client import FIXTURE_PATH as KALSHI_FIXTURE
from venues.polymarket.client import FIXTURE_PATH as POLY_FIXTURE
from core.types import Venue


async def test_writer_refuses_sample_source() -> None:
    summaries, _ = await measure_all_with_ledgers()
    with pytest.raises(SampleSourceRefused):
        build_scoreboard_artifact(summaries, mode="fixtures", measured_at="t", limit=3, source="sample")


async def test_pnl_fields_come_from_ledgers_and_identity_holds() -> None:
    summaries, ledgers = await measure_all_with_ledgers()
    artifact = build_scoreboard_artifact(summaries, mode="fixtures", measured_at="t", limit=3)

    assert artifact["meta"]["source"] == "measured"
    assert artifact["meta"]["pnl_source"] == "core.ledger.PaperLedger"
    expected_total = sum((l.total_pnl for l in ledgers.values()), Decimal("0"))
    assert artifact["totals"]["paper_pnl"] == expected_total.quantize(Decimal("0.0001"))
    assert artifact["portfolio"]["realized_pnl"] == sum((l.realized_pnl for l in ledgers.values()), Decimal("0")).quantize(Decimal("0.0001"))
    assert artifact["portfolio"]["unrealized_pnl"] == sum((l.unrealized_pnl for l in ledgers.values()), Decimal("0")).quantize(Decimal("0.0001"))
    assert artifact["portfolio"]["equity"] == artifact["portfolio"]["starting_cash"] + artifact["portfolio"]["realized_pnl"] + artifact["portfolio"]["unrealized_pnl"]
    assert artifact["portfolio"]["primary"]["ledger_id"] == PRIMARY_TRACK
    assert "pnl_from_ledger_not_placeholder" in artifact["portfolio"]["risk_flags"]
    # Fixture fills lift the ask and are marked at mid, so PnL is non-zero and not 0.42.
    assert artifact["totals"]["paper_pnl"] != Decimal("0.42")
    assert all(row["paper_pnl"] is not None for row in artifact["top_fills"])
    json.dumps(artifact, default=json_default)  # serialisable end to end


async def test_persist_run_writes_ledgers_and_reloads_them(tmp_path: Path) -> None:
    summaries, ledgers = await measure_all_with_ledgers()
    persist_run(summaries, ledgers, artifact_dir=tmp_path, mode="fixtures", measured_at="t", limit=3, kalshi_env=None)

    assert (tmp_path / "scoreboard_fixtures.json").exists()
    assert (tmp_path / "scoreboard_latest.json").exists()
    for track in TRACKS:
        assert (tmp_path / "paper" / f"ledger_{track}.json").exists()
        assert (tmp_path / "paper" / f"equity_curve_{track}.jsonl").read_text().count("\n") == 1
    reloaded = load_ledgers(tmp_path, TRACKS)
    assert reloaded[PRIMARY_TRACK].equity == ledgers[PRIMARY_TRACK].equity

    # Second cycle with carried ledgers: positions are already at target, nothing re-enters.
    summaries2, ledgers2 = await measure_all_with_ledgers(ledgers=reloaded)
    primary = next(s for s in summaries2 if s.track == PRIMARY_TRACK)
    assert primary.paper_fills == 0
    assert primary.refused_by_reason.get("target_position_reached") == 4
    assert ledgers2[PRIMARY_TRACK].equity == ledgers[PRIMARY_TRACK].equity
    assert len(ledgers2[PRIMARY_TRACK].equity_curve) == 2


async def test_no_fee_mode_zeroes_fees_but_keeps_spread_cost() -> None:
    _, ledgers = await measure_all_with_ledgers(model_fees=False)
    primary = ledgers[PRIMARY_TRACK]
    assert primary.fees_paid == 0
    assert primary.realized_pnl == 0
    assert primary.unrealized_pnl < 0  # bought at ask, marked at mid


def test_settlement_gate_blocks_clause_mismatch_and_admits_fed_pair() -> None:
    kalshi, _ = load_fixture(KALSHI_FIXTURE, Venue.KALSHI)
    poly, _ = load_fixture(POLY_FIXTURE, Venue.POLYMARKET)
    results = {pair.pair_id: settlement_gate(pair) for pair in MarketMatcher().match(kalshi, poly)}

    assert results["fed-rate-cut-september"].admitted
    cpi = results["august-cpi-over-3"]
    assert not cpi.admitted
    assert cpi.reason == "clause_refuse_mismatch"
    assert cpi.details["fingerprint_relation"] == "indeterminate"
    nba = results["nba-new-york-boston"]
    assert not nba.admitted
    assert nba.details["host_conflict"] is True
    assert nba.details["kalshi_host_tier"] == "media" and nba.details["polymarket_host_tier"] == "official"


async def test_kalshi_canary_metrics_present() -> None:
    summaries, _ = await measure_all_with_ledgers()
    primary = next(s for s in summaries if s.track == PRIMARY_TRACK)
    canary = primary.metrics["kalshi_canary"]
    assert canary["venue_breakdown"]["fills"] == 2
    assert canary["pnl"]["total_pnl"] < 0
    # 4 fixture fills scored against paper_settlement_outcome; the Kalshi CPI buy
    # (prior 0.40, settles NO) is the one miss. Network fills are never scored.
    assert primary.metrics["hit_rate"] == Decimal("0.7500")
    assert primary.metrics["settlement_preview"]["scored_fills"] == 4
