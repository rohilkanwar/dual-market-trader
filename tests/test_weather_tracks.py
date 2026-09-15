"""The weather lane is wired into the scoreboard before any weather strategy exists.

These tests never import a weather strategy. They prove that (a) a board without
weather tracks is unchanged, (b) a board that carries weather summaries gets an
honest ``findings.weather`` headline, slim metrics and a family stamp, and (c)
``measure_all`` tolerates a missing, stubbed or failing weather runner.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from apps.measure_all import json_default, load_ledgers, persist_run
from core.ledger import PaperLedger
from research.scoreboard import TRACK_LABELS, TRACKS, TrackSummary, measure_all_with_ledgers
from research.scoreboard_artifact import build_scoreboard_artifact
from research.weather_tracks import (
    WEATHER_FAMILY,
    WEATHER_TRACKS,
    is_weather_track,
    load_weather_report_builder,
    load_weather_runner,
    slim_weather_metrics,
    weather_finding,
)


def _weather_summary(track: str, **metrics: Any) -> TrackSummary:
    summary = TrackSummary(track)
    summary.candidates = int(metrics.pop("candidates", 0))
    summary.admitted = int(metrics.pop("admitted", 0))
    summary.paper_fills = int(metrics.pop("paper_fills", 0))
    summary.metrics.update(metrics)
    return summary


def test_reserved_ids_are_registered_and_labelled() -> None:
    assert WEATHER_TRACKS == ("weather_bucket_edge", "weather_dead_bucket", "weather_calibrated_ensemble")
    for track in WEATHER_TRACKS:
        assert is_weather_track(track)
        assert TRACK_LABELS[track], track
        assert track not in TRACKS, "weather ids are opt-in, never part of the thirteen core tracks"
    assert is_weather_track("polymarket_metar_dead_bucket")
    assert not is_weather_track("single_venue_fair_value")
    assert not is_weather_track(None)


def test_runner_and_report_builder_are_absent_on_main() -> None:
    # research.weather_scoreboard is the sibling branches' module. Until it merges
    # both hooks resolve to None and nothing else in this file needs it.
    assert load_weather_runner() is None
    assert load_weather_report_builder() is None


def test_finding_is_none_without_weather_tracks() -> None:
    assert weather_finding([TrackSummary("single_venue_fair_value"), TrackSummary("category_specialist")]) is None


def test_finding_reduces_metrics_and_never_validates_by_default() -> None:
    edge = _weather_summary(
        "weather_bucket_edge", candidates=6, admitted=2, paper_fills=1,
        status="measured", source={"name": "open-meteo"}, markets=6, buckets=30,
        cities=["Chicago", "Dallas"], stations=4, stations_parsed=3,
        ensemble_edge={"n": 12, "mean_bps": 85},
        evaluation={"status": "underpowered", "preregistered_n": 30},
    )
    dead = _weather_summary(
        "weather_dead_bucket", candidates=5, cities=["Dallas", "Denver"],
        dead_bucket={"candidates": 5, "kills": 2}, stations=2, stations_parsed=2,
        kills=[{"market": "a", "bucket": "80-81"}, {"market": "b", "bucket": "60-61"}],
    )
    calib = _weather_summary("weather_calibrated_ensemble", calibration={"n": 40, "status": "collecting"})

    finding = weather_finding([TrackSummary("single_venue_fair_value"), edge, dead, calib])
    assert finding is not None
    assert finding["tracks"] == list(WEATHER_TRACKS)
    assert finding["status"] == "measured" and finding["source"] == "open-meteo"
    assert finding["markets"] == 6 and finding["buckets"] == 30
    assert finding["cities"] == ["Chicago", "Dallas", "Denver"]
    assert (finding["stations"], finding["stations_parsed"]) == (6, 5)
    assert finding["station_parse_rate"] == Decimal("0.8333")
    assert finding["ensemble_edge_n"] == 12 and finding["ensemble_edge_mean_bps"] == 85
    assert (finding["dead_bucket_candidates"], finding["dead_bucket_kills"]) == (5, 2)
    assert finding["calibration_n"] == 40
    assert finding["preregistered_n"] == 30
    assert finding["evaluation_status"] == "underpowered"
    assert finding["hypothesis_validated"] is False
    assert (finding["candidates"], finding["admitted"], finding["paper_fills"]) == (11, 2, 1)

    empty = weather_finding([_weather_summary("weather_bucket_edge")])
    assert empty is not None
    assert empty["evaluation_status"] == "not_run" and empty["hypothesis_validated"] is False
    assert empty["station_parse_rate"] is None and empty["cities"] == []

    passed = weather_finding([_weather_summary("weather_bucket_edge", evaluation={"status": "pass"})])
    assert passed is not None and passed["hypothesis_validated"] is True


def test_slim_metrics_replaces_row_lists_and_keeps_scalars() -> None:
    slim = slim_weather_metrics({
        "cities": ["Chicago"],
        "observations": [{"station": "KORD", "temp_f": 71}, {"station": "KMDW", "temp_f": 72}],
        "buckets": [],
        "stations": 2,
    })
    assert slim["cities"] == ["Chicago"] and slim["stations"] == 2 and slim["buckets"] == []
    assert slim["observations"] == {"count": 2, "detail": "weather_report_<mode>.json"}


async def test_core_board_is_unchanged_without_weather_tracks() -> None:
    summaries, _ = await measure_all_with_ledgers()
    assert [s.track for s in summaries] == list(TRACKS)
    artifact = build_scoreboard_artifact(summaries, mode="fixtures", measured_at="t", limit=3)
    assert "weather" not in artifact["findings"]
    assert "weather_report" not in artifact
    assert "weather_hypothesis_not_validated" not in artifact["portfolio"]["risk_flags"]
    assert all("family" not in row for row in artifact["tracks"])


async def test_artifact_with_weather_tracks_carries_headline_and_flag(tmp_path: Path) -> None:
    summaries, ledgers = await measure_all_with_ledgers()
    weather = [
        _weather_summary(
            "weather_bucket_edge", candidates=3, admitted=1, paper_fills=1, stations=2, stations_parsed=2,
            cities=["Chicago"], observations=[{"station": "KORD"}], evaluation={"status": "pending_resolutions"},
        ),
        _weather_summary("weather_dead_bucket", dead_bucket={"candidates": 4, "kills": 1}),
    ]
    artifact = build_scoreboard_artifact(summaries + weather, mode="fixtures", measured_at="t", limit=3)
    assert artifact["schema_version"] == "1.4.0"
    finding = artifact["findings"]["weather"]
    assert finding["tracks"] == ["weather_bucket_edge", "weather_dead_bucket"]
    assert finding["dead_bucket_kills"] == 1 and finding["station_parse_rate"] == Decimal("1")
    assert "weather_hypothesis_not_validated" in artifact["portfolio"]["risk_flags"]
    rows = {row["track"]: row for row in artifact["tracks"]}
    assert rows["weather_bucket_edge"]["family"] == WEATHER_FAMILY
    assert rows["weather_bucket_edge"]["metrics"]["observations"] == {"count": 1, "detail": "weather_report_<mode>.json"}
    assert "family" not in rows["single_venue_fair_value"]
    # No weather ledger was supplied, so the board's PnL is still the thirteen core ledgers'.
    assert artifact["totals"]["paper_pnl"] == sum((l.total_pnl for l in ledgers.values()), Decimal("0")).quantize(Decimal("0.0001"))

    # Persisting without a report builder writes the board + manifest headline, no weather_report.
    persisted = persist_run(summaries + weather, ledgers, artifact_dir=tmp_path, mode="fixtures", measured_at="t", limit=3, kalshi_env=None)
    assert "weather_report" not in persisted
    assert not (tmp_path / "weather_report_fixtures.json").exists()
    manifest = json.loads(next((tmp_path / "paper" / "runs").glob("*.json")).read_text())
    assert manifest["weather"]["dead_bucket_kills"] == 1
    board = json.loads((tmp_path / "scoreboard_fixtures.json").read_text())
    assert board["findings"]["weather"]["tracks"] == ["weather_bucket_edge", "weather_dead_bucket"]
    json.dumps(artifact, default=json_default)


async def test_measure_all_appends_summaries_from_an_injected_weather_runner() -> None:
    async def runner(snapshots: Any, *, ledgers: Any, starting_cash: Decimal, model_fees: bool, use_fixtures: bool, cycle_label: str):
        assert use_fixtures is True and ledgers == {}
        ledger = PaperLedger(starting_cash=starting_cash, ledger_id="weather_bucket_edge")
        ledger.snapshot(label=cycle_label)
        summary = _weather_summary("weather_bucket_edge", candidates=2, stations=1, stations_parsed=1)
        summary.ledger = ledger.summary()
        return (
            [summary, TrackSummary("tennis_extra")],
            {"weather_bucket_edge": ledger, "tennis_extra": PaperLedger(starting_cash=starting_cash, ledger_id="tennis_extra")},
        )

    summaries, ledgers = await measure_all_with_ledgers(weather_runner=runner)
    assert [s.track for s in summaries] == [*TRACKS, "weather_bucket_edge"], "non-weather ids from the runner are dropped"
    assert set(ledgers) == {*TRACKS, "weather_bucket_edge"}
    artifact = build_scoreboard_artifact(summaries, mode="fixtures", measured_at="t", limit=3)
    assert artifact["findings"]["weather"]["stations_parsed"] == 1
    assert artifact["portfolio"]["by_track"]["weather_bucket_edge"]["starting_cash"] == Decimal("1000")
    assert "weather_hypothesis_not_validated" not in artifact["portfolio"]["risk_flags"], "no fills, no flag"

    opted_out, _ = await measure_all_with_ledgers(weather_runner=runner, include_weather=False)
    assert [s.track for s in opted_out] == list(TRACKS)


async def test_failing_weather_runner_does_not_take_the_board_down(caplog: pytest.LogCaptureFixture) -> None:
    async def broken(snapshots: Any, **kwargs: Any):
        raise RuntimeError("METAR feed unreachable")

    summaries, ledgers = await measure_all_with_ledgers(weather_runner=broken)
    assert [s.track for s in summaries] == list(TRACKS)
    assert set(ledgers) == set(TRACKS)
    assert "weather tracks failed" in caplog.text


def test_load_ledgers_carries_a_weather_ledger_when_present(tmp_path: Path) -> None:
    (tmp_path / "paper").mkdir()
    PaperLedger(starting_cash=Decimal("1000"), ledger_id="weather_dead_bucket").save(tmp_path / "paper" / "ledger_weather_dead_bucket.json")
    PaperLedger(starting_cash=Decimal("1000"), ledger_id="single_venue_fair_value").save(tmp_path / "paper" / "ledger_single_venue_fair_value.json")
    loaded = load_ledgers(tmp_path, TRACKS)
    assert set(loaded) == {"single_venue_fair_value", "weather_dead_bucket"}
    assert set(load_ledgers(tmp_path, TRACKS, optional_tracks=())) == {"single_venue_fair_value"}
