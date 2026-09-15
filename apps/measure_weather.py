"""Run the ``weather_dead_bucket`` paper track: Polymarket daily-temperature dead buckets vs. station METARs.

Outputs (relative to ``--artifact-dir``, default ``artifacts/``), named per the weather
track contract (``research/weather_tracks.py`` on the dashboard-wiring branch):

* ``scoreboard_weather.json``            dashboard artifact (source=measured, ``meta.track_family=weather``);
                                         ``scoreboard_latest.json`` only with ``--publish-latest``
* ``weather_report_<mode>.json``,        kind ``weather_report``: one block per weather track; this track's block
  ``weather_report_latest.json``         carries the pre-registration, both verdicts, every event's running high,
                                         every bucket leg's reason, resting-only legs and every register record
* ``weather_dead_bucket/register.json``  the persistent position register (open and settled records)
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
    WEATHER_FAMILY,
    WEATHER_SCOREBOARD_NAME,
    WEATHER_TRACK,
    DeadBucketRegister,
    build_weather_report,
    measure_weather_dead_bucket,
    register_path,
)
from research.weather_obs import build_observation_source
from strategies.weather_dead_bucket import DeadBucketParameters

SCOREBOARD_NAME = WEATHER_SCOREBOARD_NAME
TRACK_FAMILY = WEATHER_FAMILY


def report_name(mode: str) -> str:
    return f"weather_report_{mode}.json"


def _print(summary: TrackSummary, block: dict[str, Any], *, mode: str) -> None:
    src = block.get("observation_source") or {}
    print(f"\nweather_dead_bucket ({mode}) status={block['status']} obs_source={src.get('source')} stations={src.get('stations')} errors={len(src.get('errors', []))}")
    for error in src.get("errors", [])[:5]:
        print(f"  obs error: {error}")
    print(f"events: {block['events_parsed']}/{block['events_total']} parsed  candidates={summary.candidates} admitted={summary.admitted} fills={summary.paper_fills} opened={block['positions_opened']} settled_now={block['positions_settled_this_run']}")
    if summary.refused_by_reason:
        print("  reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.refused_by_reason.items())))
    resting = block.get("resting_only_opportunities") or []
    if resting:
        bids = sorted(float(r["best_bid"]) for r in resting if r.get("best_bid") is not None)
        print(f"  dead/certain legs with no taker ask (resting order only): {len(resting)}; best bids {bids[0]:.3f}..{bids[-1]:.3f}" if bids else f"  dead/certain legs with no taker ask: {len(resting)}")
    for key, high in (block.get("running_highs") or {}).items():
        print(f"  {key}: high={high.get('running_high_low')}..{high.get('running_high_high')} all={high.get('running_high_all_reports_high')} latest={high.get('latest_temp_high')} trend={high.get('trend')} complete={high.get('day_complete')} at={high.get('latest_observed_at_local')}")
    header = f"{'city':<14}{'bucket':<16}{'rule':<28}{'buy':<4}{'qty':>5}{'px':>8}{'net':>8}  status / venue / obs"
    print(header)
    print("-" * len(header))
    for r in block["records"]:
        print(f"{r['city'][:13]:<14}{r['bucket_label'][:15]:<16}{r['rule'][:27]:<28}{r['buy_outcome']:<4}{float(r['quantity']):>5.0f}{float(r['entry_price']):>8.3f}{float(r['net_edge']):>8.4f}  {r['status']} / {r['venue_outcome']} / {r['obs_implied_outcome']} ({r['agreement']}) pnl={r['realized_pnl']}")
    v = block["verdict"]
    print(f"\nverdict (dead-bucket NO): n={v['n']} station_days={v['station_days']} mean_net/ct={v['mean_net_per_contract']} losses={v['losses']} kill={v['kill_rule_triggered']} -> {v['status']}  [pre-registered: n>={v['pre_registered']['min_settled_positions']}, station_days>={v['pre_registered']['min_station_days']}, mean>={v['pre_registered']['pass_mean_net_per_contract']}, zero losses]")
    y = block["verdict_certain_yes"]
    print(f"verdict (certain YES):    n={y['n']} mean_net/ct={y['mean_net_per_contract']} losses={y['losses']} -> {y['status']}")
    print(f"evaluation: {block['evaluation']}")
    ledger = block["ledger"] or {}
    print(f"ledger: realized={ledger.get('realized_pnl')} unrealized={ledger.get('unrealized_pnl')} fees={ledger.get('fees_paid')} equity={ledger.get('equity')} fills={ledger.get('fills')} open={ledger.get('open_positions')}")
    print(f"network: {block['network_status']}")


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
    report = to_jsonable(build_weather_report([summary], mode=mode, measured_at=measured_at, run_id=artifact["meta"]["run_id"]))
    # ``persist_run`` writes these itself once research.weather_tracks has merged (same
    # builder, same names); writing them here keeps this CLI complete on its own.
    write_json(artifact_dir / report_name(mode), report)
    write_json(artifact_dir / "weather_report_latest.json", report)
    _print(summary, report["tracks"][WEATHER_TRACK], mode=mode)
    print(f"artifacts: {artifact_dir / SCOREBOARD_NAME}  {artifact_dir / report_name(mode)}  {register_path(artifact_dir)}  {artifact_dir / 'paper'}")
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
