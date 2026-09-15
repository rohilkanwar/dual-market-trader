"""Run the ``weather_bucket_edge`` paper track: Polymarket temperature buckets vs a free ensemble.

Outputs (relative to ``--artifact-dir``, default ``artifacts/``):

* ``scoreboard_weather_buckets.json``   dashboard artifact (schema 1.3.0, source=measured,
                                        track_family=weather); ``scoreboard_latest.json``
                                        only with ``--publish-latest``
* ``weather_buckets_latest.json``       full report: pre-registered rule, verdict (venue-settled
                                        and provisional incl. METAR), station-parse rate, feed
                                        freshness, every city-day's reason, every register record
* ``weather_buckets/register.json``     the persistent city-day register (open / settled / excluded)
* ``paper/ledger_weather_bucket_edge.json``  the track's PaperLedger; ``paper/runs/<run_id>.json``

Fixture mode (default) replays ``research/fixtures/weather_buckets_replay.json``.
Network mode reads public Gamma/CLOB weather events (tag 103040), the free
Open-Meteo ensemble API and public aviationweather.gov METARs. No key of any
kind is read; paid vendors are not implemented (``paid_sources`` in the report
records whether such a key was even present). Paper-only: refuses live flags.
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
from research.weather_buckets import (
    TRACK_FAMILY,
    WEATHER_TRACK,
    CityDayRegister,
    measure_weather_buckets,
    register_path,
)
from research.weather_sources import DEFAULT_MODELS, StationRegistry, build_weather_feed
from strategies.weather_buckets import WeatherEdgeParameters

SCOREBOARD_NAME = "scoreboard_weather_buckets.json"
REPORT_NAME = "weather_buckets_latest.json"
NOT_VALIDATED = [
    "network sample: the committed network snapshot is a single measurement cycle; the pre-registered verdict needs "
    ">= 30 venue-settled city-days with contracts, so the edge is UNKNOWN until an operator runs the CLI several times a day for weeks",
    "ensemble calibration: raw member frequencies (alpha 0.5 smoothing, dispersion 1.0, bias 0) are used as probabilities; "
    "no per-station bias or spread correction has been fitted (sister calibration track)",
    "station representativeness: Open-Meteo interpolates gridded models to the station coordinates; a coastal or airport "
    "micro-climate can differ from the grid cell by more than a bucket width",
    "METAR-derived outcomes reconstruct NOAA's hourly 'Temp' column from the T-group tenths with round-half-up; NOAA's "
    "own rounding, SPECI handling and late corrections are not verified, so METAR settlement is provisional only",
    "fee model: Polymarket 'weather_fees' taker rate 5% * p * (1 - p) from the published table; maker rebates and any later "
    "schedule change are not modelled",
    "fills are taker fills at the displayed touch of a frozen snapshot; queue, latency and adverse selection are not modelled",
    "fixture replay numbers are hand-written and carry no evidence about the hypothesis",
]


def build_report(summary: TrackSummary, *, mode: str, measured_at: str, run_id: str, register: CityDayRegister) -> dict[str, Any]:
    m = summary.metrics
    return to_jsonable(
        {
            "schema_version": "1.0.0",
            "kind": "weather_buckets_report",
            "paper_only": True,
            "source": "measured",
            "run_id": run_id,
            "mode": mode,
            "measured_at": measured_at,
            "track": summary.track,
            "track_family": TRACK_FAMILY,
            "status": m.get("status"),
            "network_status": m.get("network_status"),
            "experiment": {
                "hypothesis": (
                    "A free multi-model ensemble (GEFS + ECMWF IFS + ICON-EPS members) at the exact settlement station, "
                    "rounded to the settlement precision, prices Polymarket daily temperature buckets better than the CLOB "
                    "does: buying the bucket side whose model edge clears the taker fee plus 3c earns >= 2c per contract net "
                    "of fees on venue-settled city-days."
                ),
                "pre_registered": m.get("verdict", {}).get("pre_registered"),
                "data_sources": m.get("free_sources"),
                "paid_sources": m.get("paid_sources"),
            },
            "parameters": m.get("parameters"),
            "feed": m.get("feed"),
            "snapshot": m.get("snapshot"),
            "station_parse": m.get("station_parse"),
            "city_days": m.get("city_days"),
            "candidates": summary.candidates,
            "admitted": summary.admitted,
            "proposed_orders": summary.proposed_orders,
            "paper_fills": summary.paper_fills,
            "refused_by_reason": dict(sorted(summary.refused_by_reason.items())),
            "bucket_refusals": m.get("bucket_refusals"),
            "register": m.get("register"),
            "verdict": m.get("verdict"),
            "verdict_provisional_including_metar": m.get("verdict_provisional_including_metar"),
            "metar_venue_agreement": m.get("metar_venue_agreement"),
            "brier": m.get("brier"),
            "measurements": m.get("measurements"),
            "records": [r.as_dict() for r in register.records.values()],
            "fills": summary.fills,
            "ledger": summary.ledger,
            "not_validated": NOT_VALIDATED,
        }
    )


def _print(summary: TrackSummary, report: dict[str, Any]) -> None:
    m = summary.metrics
    feed = m.get("feed", {})
    print(f"\nweather_bucket_edge ({report['mode']}) status={report['status']} feed={feed.get('name')} requests={feed.get('requests_made', 'n/a')}")
    for error in (feed.get("errors") or [])[:5]:
        print(f"  feed error: {error}")
    sp = report["station_parse"]
    print(f"city-days: candidates={summary.candidates} station_parsed={sp['parsed']}/{sp['total']} priced={report['city_days']['priced']} traded={summary.admitted} orders={summary.proposed_orders} fills={summary.paper_fills}")
    if summary.refused_by_reason:
        print("  reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.refused_by_reason.items())))
    if report.get("bucket_refusals"):
        print("  bucket reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(report["bucket_refusals"].items())))
    header = f"{'record':<30}{'status':<10}{'ct':>5}{'cost':>9}{'realized':>10}{'metar':>7}{'venue':>16}  agree"
    print(header)
    print("-" * len(header))
    for r in report["records"]:
        paper = r.get("paper") or {}
        print(
            f"{r['record_id']:<30}{r['status']:<10}{float(paper.get('contracts') or 0):>5.0f}{float(paper.get('cost') or 0):>9.2f}"
            f"{(float(paper['realized_pnl']) if paper.get('realized_pnl') is not None else float('nan')):>10.3f}"
            f"{str(r.get('metar', {}).get('value') if r.get('metar') else '-'):>7}{str(r.get('settlement', {}).get('winning_label') or '-'):>16}  {r.get('metar', {}).get('agrees_with_venue')}"
        )
    v, p = report["verdict"], report["verdict_provisional_including_metar"]
    print(
        f"\nverdict (venue-settled): n={v['n']} contracts={v['contracts']} net={v['net_pnl']} mean/ct={v['mean_per_contract']} "
        f"ci95=[{v['lower_95']}, {v['upper_95']}] -> {v['status']}  [pre-registered: n>={v['pre_registered']['min_settled_city_days']}, mean>={v['pre_registered']['pass_net_ev_per_contract']}]"
    )
    print(f"provisional (incl. METAR): n={p['n']} mean/ct={p['mean_per_contract']} -> {p['status']}; metar/venue agreement={report['metar_venue_agreement']['agree']}/{report['metar_venue_agreement']['checked']}; brier model={report['brier']['model_mean']} market={report['brier']['market_mean']}")
    ledger = report["ledger"] or {}
    print(f"ledger: realized={ledger.get('realized_pnl')} unrealized={ledger.get('unrealized_pnl')} fees={ledger.get('fees_paid')} equity={ledger.get('equity')} fills={ledger.get('fills')}")
    print(f"network: {report['network_status']}")


def parameters_from_args(args: argparse.Namespace) -> WeatherEdgeParameters | None:
    overrides = {
        "min_net_edge": args.min_net_edge,
        "pass_net_ev_per_contract": args.pass_net_ev,
        "min_settled_city_days": args.min_city_days,
        "max_order_notional": args.max_order_notional,
        "max_market_notional": args.max_market_notional,
        "max_city_day_notional": args.max_city_day_notional,
        "max_lead_days": args.max_lead_days,
        "min_members": args.min_members,
        "smoothing_alpha": args.smoothing_alpha,
        "dispersion_multiplier": args.dispersion,
        "bias_degrees": args.bias_degrees,
    }
    provided = {k: v for k, v in overrides.items() if v is not None}
    return WeatherEdgeParameters(**provided) if provided else None


async def run(
    *,
    use_network: bool,
    limit: int,
    artifact_dir: Path,
    persist: bool,
    reset: bool,
    model_fees: bool,
    publish_latest: bool,
    models: tuple[str, ...],
    max_requests: int,
    parameters: WeatherEdgeParameters | None,
) -> dict[str, Any]:
    require_paper_only("measure_weather_buckets")
    mode = "network" if use_network else "fixtures"
    ledger = None
    register = None
    if persist and not reset:
        path = ledger_path(artifact_dir, WEATHER_TRACK)
        ledger = PaperLedger.load(path) if path.exists() else None
        register = CityDayRegister.load_or_create(register_path(artifact_dir))
    measured_at = datetime.now(UTC).isoformat()
    registry = None
    try:
        registry = StationRegistry.load()
    except (OSError, ValueError):
        registry = None
    feed = build_weather_feed(use_fixtures=not use_network, registry=registry, models=models, max_requests=max_requests)
    summary, ledger, register = await measure_weather_buckets(
        use_fixtures=not use_network,
        limit=limit,
        feed=feed,
        parameters=parameters,
        ledger=ledger,
        register=register,
        model_fees=model_fees,
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
            "label_suffix": "WEATHER BUCKETS",
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
    parser.add_argument("--network", action="store_true", help="read public Gamma/CLOB weather events + Open-Meteo + aviationweather.gov (paper fills stay local)")
    parser.add_argument("--limit", type=int, default=120, help="daily-temperature events to read from Gamma (newest first)")
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--no-persist", action="store_true", help="do not carry the ledger / city-day register across runs")
    parser.add_argument("--reset", action="store_true", help="start from a fresh ledger and empty register")
    parser.add_argument("--no-fees", action="store_true", help="disable the Polymarket fee model on paper fills and in the edge")
    parser.add_argument("--publish-latest", action="store_true", help="also overwrite scoreboard_latest.json")
    feed = parser.add_argument_group("free feeds")
    feed.add_argument("--models", default=",".join(DEFAULT_MODELS), help="Open-Meteo ensemble models, comma separated (default gfs_seamless,ecmwf_ifs025,icon_seamless)")
    feed.add_argument("--max-requests", type=int, default=400, help="cap on free-feed HTTP requests per run (Open-Meteo allows 10,000/day)")
    exp = parser.add_argument_group("pre-registered parameters (override only for sensitivity checks)")
    exp.add_argument("--min-net-edge", type=Decimal, default=None)
    exp.add_argument("--pass-net-ev", type=Decimal, default=None)
    exp.add_argument("--min-city-days", type=int, default=None)
    exp.add_argument("--max-order-notional", type=Decimal, default=None)
    exp.add_argument("--max-market-notional", type=Decimal, default=None)
    exp.add_argument("--max-city-day-notional", type=Decimal, default=None)
    exp.add_argument("--max-lead-days", type=int, default=None)
    exp.add_argument("--min-members", type=int, default=None)
    exp.add_argument("--smoothing-alpha", type=Decimal, default=None)
    exp.add_argument("--dispersion", type=Decimal, default=None, help="multiplier on member deviations from the ensemble mean (default 1)")
    exp.add_argument("--bias-degrees", type=Decimal, default=None, help="additive station bias in market degrees (default 0)")
    args = parser.parse_args()
    require_paper_only("measure_weather_buckets")
    asyncio.run(
        run(
            use_network=args.network,
            limit=max(1, args.limit),
            artifact_dir=args.artifact_dir,
            persist=not args.no_persist,
            reset=args.reset,
            model_fees=not args.no_fees,
            publish_latest=args.publish_latest,
            models=tuple(m.strip() for m in args.models.split(",") if m.strip()),
            max_requests=max(1, args.max_requests),
            parameters=parameters_from_args(args),
        )
    )


if __name__ == "__main__":
    main()
