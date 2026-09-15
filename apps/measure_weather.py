"""Run the ``weather_dead_bucket`` paper track: Polymarket daily-temperature dead buckets vs. station METARs.

Outputs (relative to ``--artifact-dir``, default ``artifacts/``):

* ``scoreboard_weather_dead_bucket.json``  dashboard artifact (schema 1.3.0, source=measured,
                                           track_family=weather_dead_bucket); ``scoreboard_latest.json``
                                           only with ``--publish-latest``
* ``weather_dead_bucket_latest.json``      full report: pre-registered rule, verdicts (dead-bucket NO and
                                           certain-YES samples), every event's parsed spec and running high,
                                           every bucket's reason, every register record, ledger, sources
* ``weather_dead_bucket/register.json``    the persistent position register (open and settled records)
* ``paper/ledger_weather_dead_bucket.json`` the track's PaperLedger; ``paper/runs/<run_id>.json``

Fixture mode (default) replays ``research/fixtures/weather_dead_bucket_replay.json``
(synthetic METAR sequences + Polymarket-shaped bucket events). Network mode reads
public Gamma events (tag 104596 "Highest temperature") with CLOB YES/NO books and
the settlement station's free observations (aviationweather.gov, NWS fallback).
Paper-only: refuses live flags. A measurement needs repeated runs against the
same ``--artifact-dir``: late-day runs open positions, later runs settle them.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from apps.measure_all import ledger_path, persist_run, to_jsonable, write_json
from core.config import require_paper_only
from core.ledger import PaperLedger
from research.scoreboard import TrackSummary
from research.weather_dead_bucket import (
    POLYMARKET_HIGHEST_TEMPERATURE_TAG,
    REPORT_FILE,
    WEATHER_FAMILY,
    WEATHER_TRACK,
    DeadBucketRegister,
    measure_weather_dead_bucket,
    register_path,
)
from research.weather_obs import FREE_SOURCES, build_observation_source
from strategies.weather_dead_bucket import DeadBucketParameters

SCOREBOARD_NAME = "scoreboard_weather_dead_bucket.json"
REPORT_NAME = REPORT_FILE
TRACK_FAMILY = WEATHER_FAMILY


def build_report(summary: TrackSummary, *, mode: str, measured_at: str, run_id: str, register: DeadBucketRegister) -> dict[str, Any]:
    m = summary.metrics
    return to_jsonable(
        {
            "schema_version": "1.0.0",
            "kind": "weather_dead_bucket_report",
            "paper_only": True,
            "source": "measured",
            "run_id": run_id,
            "mode": mode,
            "measured_at": measured_at,
            "track": summary.track,
            "status": m.get("status"),
            "network_status": m.get("network_status"),
            "experiment": {
                "hypothesis": (
                    "Once the settlement station's running daily high is in (and, late in the day, the temperature is "
                    "falling), Polymarket temperature buckets that can no longer contain the daily high still quote a few "
                    "cents of probability; buying NO on those dead buckets (and YES on the one certain bucket) earns >= 2c "
                    "net per contract after the taker fee on settled days."
                ),
                "pre_registered": m.get("verdict", {}).get("pre_registered"),
                "kill_rules": {
                    "dead_below_running_high": "bucket.hi < hourly running high (low rounding candidate); any hour",
                    "dead_above_late_day": ">= late_day_local_hour local, latest hourly temp <= high - falling_margin, last N hourly obs not rising, bucket.lo > all-report high + headroom",
                    "certain_yes_late_day": "same late-day gate and the bucket covers [high, all-report high + headroom]",
                    "dead_day_complete / certain_yes_day_complete": "an hourly observation from the following local date exists; the bucket contains neither / both rounding candidates of the final high",
                    "refusals": "bucket_edge_ambiguous when the final or running high rounds across a bucket edge; too_early_in_day / not_falling / live otherwise",
                },
                "observation_sources": FREE_SOURCES,
            },
            "parameters": m.get("parameters"),
            "observation_source": m.get("observation_source"),
            "snapshot": m.get("snapshot"),
            "events_total": m.get("events_total"),
            "events_parsed": m.get("events_parsed"),
            "candidates": summary.candidates,
            "admitted": summary.admitted,
            "proposed_orders": summary.proposed_orders,
            "paper_fills": summary.paper_fills,
            "positions_opened": m.get("positions_opened"),
            "positions_settled_this_run": m.get("positions_settled_this_run"),
            "dead_buckets_without_taker_ask": m.get("dead_buckets_without_taker_ask"),
            "resting_only_opportunities": m.get("resting_only_opportunities"),
            "refused_by_reason": dict(sorted(summary.refused_by_reason.items())),
            "register": m.get("register"),
            "verdict": m.get("verdict"),
            "verdict_certain_yes": m.get("verdict_certain_yes"),
            "running_highs": m.get("running_highs"),
            "events": m.get("weather_events"),
            "measurements": m.get("measurements"),
            "records": [r.as_dict() for r in register.records.values()],
            "fills": summary.fills,
            "ledger": summary.ledger,
            "not_validated": [
                "the NOAA WRH time-series table rounds tenths-of-a-degree observations to whole degrees with an undocumented "
                "tie rule; exact .5 values are refused (bucket_edge_ambiguous), not resolved",
                "the table is built from the same ASOS/METAR observations aviationweather.gov serves, but late corrections "
                "(revisions until the next day's first data point) and hourly-only filtering can make it differ; the "
                "register records venue-vs-observation agreement per settled position so that basis risk is measured",
                "late-day 'no further rise' is a heuristic (17:00 local, 2°F/1°C below the high, two non-rising hourly obs, "
                "1° headroom); it can be wrong on days with evening fronts or foehn events - that is exactly the kill rule",
                "Weather Underground fallback resolutions and non-airport sources (Hong Kong Observatory) are refused, not modelled",
                "SPECI observations warmer than the hourly table are used only to widen the upper kill, never to kill lower buckets",
                "fixture sequences and settlements are synthetic (one staged disagreement) and carry no evidence about the hypothesis",
                "network sample: the committed snapshot is one cycle; the pre-registered verdict needs >= 30 settled positions over >= 10 station-days",
            ],
        }
    )


def _print(summary: TrackSummary, report: dict[str, Any]) -> None:
    m = summary.metrics
    src = m.get("observation_source", {})
    print(f"\nweather_dead_bucket ({report['mode']}) status={report['status']} obs_source={src.get('source')} stations={src.get('stations')} errors={len(src.get('errors', []))}")
    for error in src.get("errors", [])[:5]:
        print(f"  obs error: {error}")
    print(f"events: {report['events_parsed']}/{report['events_total']} parsed  candidates={summary.candidates} admitted={summary.admitted} fills={summary.paper_fills} opened={report['positions_opened']} settled_now={report['positions_settled_this_run']}")
    if summary.refused_by_reason:
        print("  reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.refused_by_reason.items())))
    resting = report.get("resting_only_opportunities") or []
    if resting:
        bids = sorted(float(r["best_bid"]) for r in resting if r.get("best_bid") is not None)
        print(f"  dead/certain legs with no taker ask (resting order only): {len(resting)}; best bids {bids[0]:.3f}..{bids[-1]:.3f}" if bids else f"  dead/certain legs with no taker ask: {len(resting)}")
    for key, high in (report.get("running_highs") or {}).items():
        print(f"  {key}: high={high.get('running_high_low')}..{high.get('running_high_high')} all={high.get('running_high_all_reports_high')} latest={high.get('latest_temp_high')} trend={high.get('trend')} complete={high.get('day_complete')} at={high.get('latest_observed_at_local')}")
    header = f"{'city':<14}{'bucket':<16}{'rule':<28}{'buy':<4}{'qty':>5}{'px':>8}{'net':>8}  status / venue / obs"
    print(header)
    print("-" * len(header))
    for r in report["records"]:
        print(f"{r['city'][:13]:<14}{r['bucket_label'][:15]:<16}{r['rule'][:27]:<28}{r['buy_outcome']:<4}{float(r['quantity']):>5.0f}{float(r['entry_price']):>8.3f}{float(r['net_edge']):>8.4f}  {r['status']} / {r['venue_outcome']} / {r['obs_implied_outcome']} ({r['agreement']}) pnl={r['realized_pnl']}")
    v = report["verdict"]
    print(f"\nverdict (dead-bucket NO): n={v['n']} station_days={v['station_days']} mean_net/ct={v['mean_net_per_contract']} losses={v['losses']} kill={v['kill_rule_triggered']} -> {v['status']}  [pre-registered: n>={v['pre_registered']['min_settled_positions']}, station_days>={v['pre_registered']['min_station_days']}, mean>={v['pre_registered']['pass_mean_net_per_contract']}, zero losses]")
    y = report["verdict_certain_yes"]
    print(f"verdict (certain YES):    n={y['n']} mean_net/ct={y['mean_net_per_contract']} losses={y['losses']} -> {y['status']}")
    ledger = report["ledger"] or {}
    print(f"ledger: realized={ledger.get('realized_pnl')} unrealized={ledger.get('unrealized_pnl')} fees={ledger.get('fees_paid')} equity={ledger.get('equity')} fills={ledger.get('fills')} open={ledger.get('open_positions')}")
    print(f"network: {report['network_status']}")


def parameters_from_args(args: argparse.Namespace) -> DeadBucketParameters | None:
    overrides = {
        "min_net_edge": args.min_net_edge,
        "late_day_local_hour": args.late_day_hour,
        "falling_margin_f": args.falling_margin_f,
        "falling_margin_c": args.falling_margin_c,
        "upper_headroom_degrees": args.upper_headroom,
        "max_obs_age_minutes": args.max_obs_age,
        "min_settled_positions": args.min_settled,
        "min_station_days": args.min_station_days,
    }
    provided = {k: v for k, v in overrides.items() if v is not None}
    return DeadBucketParameters(**provided) if provided else None


async def run(
    *,
    use_network: bool,
    limit: int,
    artifact_dir: Path,
    persist: bool,
    reset: bool,
    model_fees: bool,
    publish_latest: bool,
    obs_file: Path | None,
    tag_id: int,
    parameters: DeadBucketParameters | None,
) -> dict[str, Any]:
    require_paper_only("measure_weather")
    mode = "network" if use_network else "fixtures"
    ledger = None
    register = None
    if persist and not reset:
        path = ledger_path(artifact_dir, WEATHER_TRACK)
        ledger = PaperLedger.load(path) if path.exists() else None
        register = DeadBucketRegister.load_or_create(register_path(artifact_dir))
    measured_at = datetime.now(UTC).isoformat()
    source = build_observation_source(use_fixtures=not use_network, obs_file=obs_file)
    summary, ledger, register = await measure_weather_dead_bucket(
        use_fixtures=not use_network,
        limit=limit,
        observation_source=source,
        parameters=parameters,
        ledger=ledger,
        register=register,
        model_fees=model_fees,
        tag_id=tag_id,
    )
    artifact = persist_run(
        [summary],
        {WEATHER_TRACK: ledger} if persist else {},
        artifact_dir=artifact_dir,
        mode=mode,
        measured_at=measured_at,
        limit=limit,
        kalshi_env=None,
        scoreboard_name=SCOREBOARD_NAME,
        write_latest=publish_latest,
        artifact_kwargs={
            "primary_track": WEATHER_TRACK,
            "venues": ("polymarket",),
            "venue_focus": "polymarket",
            "label_suffix": "WEATHER DEAD BUCKET",
            "track_family": TRACK_FAMILY,
        },
    )
    if persist:
        register.save(register_path(artifact_dir))
    report = build_report(summary, mode=mode, measured_at=measured_at, run_id=artifact["meta"]["run_id"], register=register)
    write_json(artifact_dir / REPORT_NAME, report)
    _print(summary, report)
    print(f"artifacts: {artifact_dir / SCOREBOARD_NAME}  {artifact_dir / REPORT_NAME}  {register_path(artifact_dir)}  {artifact_dir / 'paper'}")
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", action="store_true", help="read public Polymarket weather events + free station observations")
    parser.add_argument("--limit", type=int, default=60, help="Polymarket daily-temperature events to read (soonest end first)")
    parser.add_argument("--tag-id", type=int, default=POLYMARKET_HIGHEST_TEMPERATURE_TAG, help="Gamma tag id for event discovery (default 104596 = Highest temperature)")
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--no-persist", action="store_true", help="do not carry the ledger / position register across runs")
    parser.add_argument("--reset", action="store_true", help="start from a fresh ledger and empty register")
    parser.add_argument("--no-fees", action="store_true", help="disable the Polymarket fee model on paper fills")
    parser.add_argument("--publish-latest", action="store_true", help="also overwrite scoreboard_latest.json")
    parser.add_argument("--obs-file", type=Path, default=None, help="saved aviationweather.gov /api/data/metar?format=json response to use instead of the network (network mode)")
    exp = parser.add_argument_group("pre-registered parameters (override only for sensitivity checks)")
    exp.add_argument("--min-net-edge", type=Decimal, default=None)
    exp.add_argument("--late-day-hour", type=int, default=None)
    exp.add_argument("--falling-margin-f", type=int, default=None)
    exp.add_argument("--falling-margin-c", type=int, default=None)
    exp.add_argument("--upper-headroom", type=int, default=None)
    exp.add_argument("--max-obs-age", type=int, default=None, help="minutes")
    exp.add_argument("--min-settled", type=int, default=None)
    exp.add_argument("--min-station-days", type=int, default=None)
    args = parser.parse_args()
    require_paper_only("measure_weather")
    asyncio.run(
        run(
            use_network=args.network,
            limit=max(1, args.limit),
            artifact_dir=args.artifact_dir,
            persist=not args.no_persist,
            reset=args.reset,
            model_fees=not args.no_fees,
            publish_latest=args.publish_latest,
            obs_file=args.obs_file,
            tag_id=args.tag_id,
            parameters=parameters_from_args(args),
        )
    )


if __name__ == "__main__":
    main()
