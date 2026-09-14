import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from apps.measure_tennis_whale import REPORT_FILE, SCOREBOARD_FILE, run
from core.ledger import PaperLedger
from research.tennis_whale import TRACK_FAMILY
from strategies.tennis_whale_copy import CopyParameters, track_for_lag

TRACKS = tuple(track_for_lag(lag) for lag in (30, 120, 600))


def _run(tmp_path: Path, **overrides: object):
    kwargs = dict(
        use_network=False,
        harvest=False,
        harvest_dir=tmp_path / "harvests",
        tape_path=None,
        artifact_dir=tmp_path,
        params=CopyParameters(),
        model_fees=True,
        persist_ledgers=True,
        min_copies=10,
        min_markets=5,
        resamples=300,
        seed=1,
        alpha=0.05,
    )
    kwargs.update(overrides)
    return run(**kwargs)  # type: ignore[arg-type]


async def test_fixture_run_writes_report_scoreboard_ledgers_and_run_record(tmp_path: Path) -> None:
    report = await _run(tmp_path)
    assert report["paper_only"] is True and report["kind"] == "tennis_whale_copy_report"
    assert report["status"] == "fixture_synthetic" and report["mode"] == "fixtures"
    assert report["overall_verdict"] == "PASS"  # synthetic informed whale; the headline says so
    assert report["headline"].startswith("Synthetic fixture (not evidence)")
    assert report["pre_registration"]["pass_rule"] and report["pre_registration"]["kill_rule"]
    assert report["kalshi"]["verdict"] == "NOT_IDENTIFIABLE"

    scoreboard = json.loads((tmp_path / SCOREBOARD_FILE).read_text())
    assert scoreboard["meta"]["source"] == "measured" and scoreboard["meta"]["pnl_source"] == "core.ledger.PaperLedger"
    assert scoreboard["meta"]["label"] == "MEASURED / FIXTURES / TENNIS WHALE COPY"
    assert scoreboard["meta"]["venues"] == ["polymarket"] and scoreboard["meta"]["track_family"] == TRACK_FAMILY
    assert scoreboard["meta"]["primary_track"] == "tennis_whale_copy_30s" and scoreboard["meta"]["kind"] == "tennis_whale_copy"
    assert scoreboard["meta"]["run_id"] == report["run_id"]
    assert [t["track"] for t in scoreboard["tracks"]] == list(TRACKS)
    assert all(t["metrics"]["family"] == TRACK_FAMILY for t in scoreboard["tracks"])
    assert scoreboard["findings"]["tennis_whale_copy"]["status"] == "fixture_synthetic"
    assert scoreboard["findings"]["tennis_whale_copy"]["verdicts"]["kalshi_whales_identifiable"] == "NOT_IDENTIFIABLE"
    assert scoreboard["totals"]["paper_fills"] == sum(t["paper_fills"] for t in scoreboard["tracks"]) > 0
    assert scoreboard["portfolio"]["source"] == "ledger_aggregate"
    assert all(f["venue"] == "polymarket" for f in scoreboard["top_fills"])
    ledger_sum = sum(Decimal(str(t["ledger"]["total_pnl"])) for t in scoreboard["tracks"])
    assert Decimal(str(scoreboard["totals"]["paper_pnl"])) == ledger_sum.quantize(Decimal("0.0001"))

    record = json.loads((tmp_path / "paper" / "runs" / f"{report['run_id']}.json").read_text())
    assert record["paper_only"] is True and record["kind"] == "tennis_whale_copy" and record["track_family"] == TRACK_FAMILY
    assert [t["track"] for t in record["tracks"]] == list(TRACKS) and all(t["family"] == TRACK_FAMILY for t in record["tracks"])
    assert (tmp_path / REPORT_FILE).exists()
    copies = json.loads((tmp_path / report["copies_file"]).read_text())
    assert copies["paper_only"] is True and copies["run_id"] == report["run_id"]
    assert len(copies["copies"]) == report["copies_total"] == scoreboard["totals"]["paper_fills"]
    assert copies["copies"][0]["market_type"] == "moneyline"
    for track in TRACKS:
        ledger = PaperLedger.load(tmp_path / "paper" / f"ledger_{track}.json")
        assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl
        assert (tmp_path / "paper" / f"equity_curve_{track}.jsonl").read_text().count("\n") == 1


async def test_ledgers_are_rebuilt_from_the_tape_never_carried(tmp_path: Path) -> None:
    first = await _run(tmp_path)
    second = await _run(tmp_path)
    assert first["tracks"] == {k: {**v, "ledger": v["ledger"]} for k, v in second["tracks"].items()}
    assert (tmp_path / "paper" / "equity_curve_tennis_whale_copy_30s.jsonl").read_text().count("\n") == 2
    ledger = PaperLedger.load(tmp_path / "paper" / "ledger_tennis_whale_copy_30s.json")
    assert len(ledger.fills) == first["tracks"]["tennis_whale_copy_30s"]["ledger"]["fills"]


async def test_no_persist_skips_ledger_files_but_keeps_report_and_run_record(tmp_path: Path) -> None:
    report = await _run(tmp_path, persist_ledgers=False)
    assert not (tmp_path / "paper" / "ledger_tennis_whale_copy_30s.json").exists()
    assert (tmp_path / SCOREBOARD_FILE).exists() and (tmp_path / REPORT_FILE).exists()
    assert (tmp_path / "paper" / "runs" / f"{report['run_id']}.json").exists()


async def test_network_without_harvest_is_an_honest_empty(tmp_path: Path) -> None:
    report = await _run(tmp_path, use_network=True, harvest=False)
    assert report["mode"] == "network" and report["status"] == "no_tennis_markets"
    assert report["overall_verdict"] == "INSUFFICIENT_DATA"
    assert any("run with --harvest" in e for e in report["universe"]["errors"])
    scoreboard = json.loads((tmp_path / SCOREBOARD_FILE).read_text())
    assert scoreboard["totals"]["paper_fills"] == 0 and scoreboard["totals"]["paper_pnl"] == 0
    assert "zero_paper_fills_this_run" in scoreboard["portfolio"]["risk_flags"]


async def test_explicit_tape_path_and_no_fees(tmp_path: Path) -> None:
    from research.tennis_whale import FIXTURE_PATH

    report = await _run(tmp_path, tape_path=FIXTURE_PATH, model_fees=False, use_network=True)
    assert report["mode"] == "network" and report["status"] == "fixture_synthetic"
    assert all(t["ledger"]["fees_paid"] == 0 for t in report["tracks"].values())


def test_cli_refuses_live_flags(tmp_path: Path) -> None:
    env = {**os.environ, "TRADING_MODE": "live", "ENABLE_LIVE_TRADING": "true", "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    proc = subprocess.run(
        [sys.executable, "-m", "apps.measure_tennis_whale", "--artifact-dir", str(tmp_path / "never")],
        env=env, capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
    )
    assert proc.returncode != 0 and "paper-only" in proc.stderr
    assert not (tmp_path / "never").exists()


@pytest.mark.parametrize("flag", ["TRADING_MODE", "ENABLE_LIVE_TRADING"])
async def test_run_refuses_each_live_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    monkeypatch.setenv(flag, "live" if flag == "TRADING_MODE" else "true")
    with pytest.raises(ValueError, match="paper-only"):
        await _run(tmp_path)
    assert not (tmp_path / SCOREBOARD_FILE).exists()
