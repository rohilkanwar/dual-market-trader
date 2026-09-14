import json
from decimal import Decimal
from pathlib import Path

import pytest

from apps.measure_all import load_ledgers
from apps.measure_flb import run
from research.flb import FLB_TRACKS
from strategies.flb import FlbParameters


def _run(tmp_path: Path, **overrides: object) -> dict:
    kwargs = dict(
        use_network=False,
        limit=200,
        artifact_dir=tmp_path,
        harvest_dir=tmp_path / "harvests",
        harvest_trades=False,
        settled_per_series=5,
        max_trades_per_market=100,
        series=(),
        expost_series=(),
        kalshi_env=None,
        persist_ledgers=True,
        reset_ledgers=False,
        model_fees=True,
        params=FlbParameters(),
        min_markets=10,
        min_contracts=1000.0,
        exclude_final_minutes=60,
    )
    kwargs.update(overrides)
    return run(**kwargs)  # type: ignore[arg-type]


async def test_fixture_run_writes_report_scoreboard_and_run_record(tmp_path: Path) -> None:
    report = await _run(tmp_path)
    assert report["paper_only"] is True and report["kind"] == "kalshi_flb_report"
    verdicts = {row["check"]: row["verdict"] for row in report["verdict_table"]}
    assert verdicts["flb_identifiable_from_snapshot"] == "NOT_IDENTIFIABLE"
    assert verdicts["event_overround_positive"] == "PASS"
    assert verdicts["ex_post_flb"] == "PASS"
    assert verdicts["maker_fade_edge_after_fees"] == "PASS"
    assert "NOT_IDENTIFIABLE" in report["headline"] and "PASS" in report["headline"]
    assert report["ex_post"]["note"].startswith("Synthetic fixture")
    for key in ("fee_model", "fill_probability", "queue", "adverse_selection", "marks", "sizing", "longshot_definition"):
        assert report["assumptions"][key]

    scoreboard = json.loads((tmp_path / "scoreboard_flb.json").read_text())
    assert scoreboard["meta"]["source"] == "measured"
    assert scoreboard["meta"]["pnl_source"] == "core.ledger.PaperLedger"
    assert scoreboard["meta"]["label"] == "MEASURED / FIXTURES / KALSHI FLB"
    assert scoreboard["meta"]["venues"] == ["kalshi"]
    assert scoreboard["meta"]["primary_track"] == "kalshi_maker_quote"
    assert scoreboard["meta"]["run_id"] == report["run_id"]
    assert [t["track"] for t in scoreboard["tracks"]] == list(FLB_TRACKS)
    assert scoreboard["findings"]["kalshi_flb"]["verdicts"]["ex_post_flb"] == "PASS"
    assert scoreboard["totals"]["paper_fills"] == sum(t["paper_fills"] for t in scoreboard["tracks"])
    assert scoreboard["portfolio"]["primary"]["mark_method"] == "conservative"
    assert all(f["venue"] == "kalshi" for f in scoreboard["top_fills"])

    record = json.loads((tmp_path / "paper" / "runs" / f"{report['run_id']}.json").read_text())
    assert record["paper_only"] is True and record["kind"] == "kalshi_flb"
    assert record["primary_track"] == "kalshi_maker_quote" and len(record["tracks"]) == 2
    assert (tmp_path / "flb_report_latest.json").exists()
    for track in FLB_TRACKS:
        assert (tmp_path / "paper" / f"ledger_{track}.json").exists()
        assert (tmp_path / "paper" / f"equity_curve_{track}.jsonl").read_text().count("\n") == 1

    fade = report["tracks"]["kalshi_longshot_fade"]
    assert fade["longshot_candidates"] == 10 and fade["paper_fills"] == 9
    assert set(fade["paper_pnl_by_longshot_band"]) == {"<10c", "10-20c"}
    assert fade["shadow_longshot_buyer"]["total_pnl"] < 0
    maker = report["tracks"]["kalshi_maker_quote"]
    assert maker["ledger"]["mark_method"] == "conservative"
    assert Decimal(str(maker["ledger"]["fees_paid"])) < Decimal(str(fade["ledger"]["fees_paid"]))


async def test_ledgers_carry_across_runs_and_reset_flag_starts_fresh(tmp_path: Path) -> None:
    first = await _run(tmp_path)
    second = await _run(tmp_path)
    ledgers = load_ledgers(tmp_path, FLB_TRACKS)
    assert len(ledgers["kalshi_longshot_fade"].equity_curve) == 2
    assert second["tracks"]["kalshi_longshot_fade"]["ledger"]["fills"] > first["tracks"]["kalshi_longshot_fade"]["ledger"]["fills"]
    reset = await _run(tmp_path, reset_ledgers=True)
    assert reset["tracks"]["kalshi_longshot_fade"]["ledger"]["fills"] == first["tracks"]["kalshi_longshot_fade"]["ledger"]["fills"]
    assert len(load_ledgers(tmp_path, FLB_TRACKS)["kalshi_longshot_fade"].equity_curve) == 1


async def test_no_fees_zeroes_paper_fees(tmp_path: Path) -> None:
    report = await _run(tmp_path, model_fees=False, persist_ledgers=False)
    for track in report["tracks"].values():
        assert Decimal(str(track["ledger"]["fees_paid"])) == 0
    assert report["ex_post"]["verdicts"]["ex_post_flb"]["longshot"]["taker_fee_per_contract"] == 0
    assert not (tmp_path / "paper" / "ledger_kalshi_maker_quote.json").exists()


async def test_refuses_live_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    with pytest.raises(ValueError, match="paper-only"):
        await _run(tmp_path)
    assert not (tmp_path / "scoreboard_flb.json").exists()
