"""Run the weather calibration paper A/B: naive vs per-city calibrated NWP ensemble on Polymarket temperature buckets.

Outputs (relative to ``--artifact-dir``, default ``artifacts/``):

* ``scoreboard_weather_calibration.json``     dashboard artifact (schema 1.3.0, source=measured,
                                              track_family=weather_calibration, both lanes as tracks);
                                              ``scoreboard_latest.json`` only with ``--publish-latest``
* ``weather_calibration_latest.json``         full A/B report: pre-registered rule and verdict, per-lane
                                              candidates / admitted / fills / settled EV, calibration store
                                              summary per city x model, every event's ladder with both
                                              lanes' probabilities and reasons, settlements, ledgers
* ``weather_calibration/register.json``       persistent register: priced city-days (with the forecasts
                                              recorded at first sight) and paper admissions per lane
* ``weather_calibration/calibration_store.json``  settled city-day samples the calibration is computed from
* ``paper/ledger_weather_naive_ensemble.json``, ``paper/ledger_weather_calibrated_ensemble.json``

Fixture mode (default) replays ``research/fixtures/weather_calibration_replay.json``.
Network mode reads public Gamma/CLOB (tag 104596) and the free Open-Meteo forecast and
previous-runs endpoints; ``--backfill-days N`` fills the store from the last N days of
resolved Polymarket city-days x archived day-ahead forecasts before pricing. Paid
Open-Meteo keys are OFF unless ``--allow-paid-keys`` *and* ``OPEN_METEO_API_KEY`` are
both present. Paper-only: refuses live flags. Run it once a day (same ``--artifact-dir``)
so admissions settle and the calibration store grows.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from apps.measure_all import ledger_path, persist_run, to_jsonable, write_json
from core.config import require_paper_only
from core.ledger import PaperLedger
from research.scoreboard import TrackSummary
from research.weather_calibration_track import (
    DETAIL_FILE,
    TRACK_FAMILY,
    CalibrationStore,
    WeatherRegister,
    measure_weather_calibration,
    register_path,
    store_path,
)
from research.weather_calibration_sources import FREE_TIER_NOTE, OPEN_METEO_API_KEY_ENV
from strategies.weather_calibration import CALIBRATED_TRACK, WEATHER_TRACKS, WeatherParameters

SCOREBOARD_NAME = "scoreboard_weather_calibration.json"
REPORT_NAME = DETAIL_FILE


def build_report(
    summaries: dict[str, TrackSummary],
    *,
    mode: str,
    measured_at: str,
    run_id: str,
    register: WeatherRegister,
    store: CalibrationStore,
    backfill: dict[str, Any] | None,
) -> dict[str, Any]:
    cal = summaries["calibrated"].metrics
    lanes = {
        lane: {
            "track": s.track,
            "candidates": s.candidates,
            "admitted": s.admitted,
            "proposed_orders": s.proposed_orders,
            "paper_fills": s.paper_fills,
            "refused_by_reason": dict(sorted(s.refused_by_reason.items())),
            "admitted_this_run": s.metrics.get("admitted_this_run"),
            "open_admissions": len(register.open_admissions(lane)),
            "settled_admissions": len(register.settled_admissions(lane)),
            "settled": cal.get("verdict", {}).get(lane),
            "ledger": s.ledger,
        }
        for lane, s in summaries.items()
    }
    return to_jsonable(
        {
            "schema_version": "1.0.0",
            "kind": "weather_calibration_report",
            "paper_only": True,
            "source": "measured",
            "run_id": run_id,
            "mode": mode,
            "measured_at": measured_at,
            "tracks": list(WEATHER_TRACKS),
            "status": cal.get("status"),
            "network_status": cal.get("network_status"),
            "experiment": {
                "hypothesis": (
                    "Some free NWP models are systematically better per city; a per-city bias- and hit-rate-weighted "
                    "ensemble beats the unweighted average; trading only when the calibrated bucket probability differs "
                    "from the market mid by >= the threshold yields better settled net EV after fees than the naive ensemble."
                ),
                "pre_registered": cal.get("verdict", {}).get("pre_registered"),
                "lanes": {
                    "naive": "equal-weight mean of raw per-model day-ahead maxima, pooled sigma (control)",
                    "calibrated": "per-city x model bias removal, (hit_rate + 0.1) / de-biased variance weights shrunk toward equal, per-city residual sigma",
                },
                "truth": "the bucket Polymarket settled on (midpoint as the continuous stand-in; open ends one width past the edge)",
                "forecasts": {
                    "live": "Open-Meteo /v1/forecast hourly temperature_2m per model, max over the target day's 24 local hours",
                    "history": "Open-Meteo previous-runs temperature_2m_previous_day1 (the run issued one day earlier), same daily max",
                    "free_tier": FREE_TIER_NOTE,
                    "paid_key_used": cal.get("forecast_source", {}).get("paid_key_used", False),
                },
            },
            "parameters": cal.get("parameters"),
            "forecast_source": cal.get("forecast_source"),
            "universe": cal.get("universe"),
            "snapshot": cal.get("snapshot"),
            "event_universe": cal.get("event_universe"),
            "backfill": backfill,
            "settlement": cal.get("settlement"),
            "settlements_detail": cal.get("settlements_detail"),
            "register": cal.get("register"),
            "lanes": lanes,
            "verdict": cal.get("verdict"),
            "calibration": {**cal.get("calibration", {}), "per_city": cal.get("calibration_detail", [])},
            "measurements": cal.get("measurements"),
            "city_days": [r.as_dict() for r in register.city_days.values()],
            "admissions": [a.as_dict() for a in register.admissions.values()],
            "fills": {lane: s.fills for lane, s in summaries.items()},
            "not_validated": [
                "the paper A/B is forward-only: admissions settle the next day, so the verdict stays UNDERPOWERED until the loop has run daily long enough for both lanes to clear the pre-registered floor",
                "calibration samples use the previous-runs archive (run issued ~24 h earlier); live admissions use the freshest run at pricing time, so calibration lead is somewhat longer than trading lead",
                "the settled bucket midpoint stands in for the station reading (+-1F / +-0.5C quantisation is inside the residual sigma, not modelled separately)",
                "per-model hit rates and the residual sigma are in-sample over the city's window; only the settled paper EV is out of sample by construction",
                "station coordinates are approximate airport positions; Jinan and Taipei name no station in the rule text",
                "Open-Meteo 'seamless' models splice regional runs where available, so a model's skill can differ by region for reasons unrelated to the global model",
                "fixture history, forecasts and books are synthetic and carry no evidence",
            ],
        }
    )


def _print(summaries: dict[str, TrackSummary], report: dict[str, Any]) -> None:
    cal = summaries["calibrated"].metrics
    print(f"\nweather_calibration ({report['mode']}) status={report['status']}")
    fs = report.get("forecast_source") or {}
    print(f"forecasts: {fs.get('name')} models={len(fs.get('models', []))} calls={fs.get('calls')} paid_key_used={fs.get('paid_key_used')} errors={len(fs.get('errors') or [])}")
    if report.get("backfill"):
        b = report["backfill"]
        print(f"backfill {b.get('window')}: {b.get('counts')} errors={len(b.get('errors') or [])}")
    eu = report.get("event_universe") or {}
    print(f"events: listed={eu.get('listed')} by_reason={eu.get('by_reason')} cities_priced={eu.get('cities_priced')}")
    print(f"settlement this run: {report.get('settlement')}")
    c = report["calibration"]
    print(
        f"calibration store: samples={c.get('samples')} cities={c.get('cities')} adequate={c.get('cities_adequate')} "
        f"(floor {c.get('min_calibration_samples')}) range={c.get('date_range')} by_source={c.get('samples_by_forecast_source')} "
        f"best_model_by_city={c.get('best_model_by_city_count')}"
    )
    header = f"{'lane':<12}{'cand':>6}{'adm':>6}{'fills':>7}{'open':>6}{'settled':>9}{'contracts':>11}{'net_ev':>10}{'ev/ct':>9}  ci95 ev/ct"
    print(header)
    print("-" * len(header))
    for lane in ("naive", "calibrated"):
        row = report["lanes"][lane]
        v = row.get("settled") or {}
        ev = v.get("ev_per_contract")
        ci = v.get("ci95_ev_per_contract")
        print(
            f"{lane:<12}{row['candidates']:>6}{row['admitted']:>6}{row['paper_fills']:>7}{row['open_admissions']:>6}"
            f"{row['settled_admissions']:>9}{float(v.get('contracts') or 0):>11.1f}{float(v.get('net_ev') or 0):>10.4f}"
            f"{(float(ev) if ev is not None else float('nan')):>9.4f}  {ci}"
        )
        if row["refused_by_reason"]:
            print(f"{'':<12}refused: " + ", ".join(f"{k}={v}" for k, v in row["refused_by_reason"].items()))
    verdict = report["verdict"]
    pr = verdict["pre_registered"]
    print(
        f"\nverdict: {verdict['status']}  difference(cal - naive) per contract={verdict['difference_per_contract']}  "
        f"[pre-registered: n>={pr['min_settled_per_lane']} per lane, margin>={pr['margin_vs_naive']}, absolute>={pr['absolute_min_ev']}]"
    )
    print(f"sample: {verdict['sample_callout']}")
    for lane in ("naive", "calibrated"):
        ledger = report["lanes"][lane]["ledger"] or {}
        print(f"ledger[{lane}]: realized={ledger.get('realized_pnl')} unrealized={ledger.get('unrealized_pnl')} fees={ledger.get('fees_paid')} equity={ledger.get('equity')} fills={ledger.get('fills')} open={ledger.get('open_positions')}")
    print(f"network: {cal.get('network_status')}")


def parameters_from_args(args: argparse.Namespace) -> WeatherParameters | None:
    overrides = {
        "edge_threshold": args.edge_threshold,
        "minimum_edge": args.minimum_edge,
        "maximum_order_size": args.max_order_size,
        "min_calibration_samples": args.min_calibration_samples,
        "min_settled_per_lane": args.min_settled,
        "margin_vs_naive": args.margin,
        "absolute_min_ev": args.absolute_min_ev,
    }
    provided = {k: v for k, v in overrides.items() if v is not None}
    if args.model:
        provided["models"] = tuple(args.model)
    if args.lead_days:
        provided["admissible_lead_days"] = tuple(args.lead_days)
    return WeatherParameters(**provided) if provided else None


async def run(
    *,
    use_network: bool,
    limit: int,
    artifact_dir: Path,
    persist: bool,
    reset: bool,
    model_fees: bool,
    publish_latest: bool,
    backfill_days: int,
    allow_paid_keys: bool,
    parameters: WeatherParameters | None,
) -> dict[str, Any]:
    require_paper_only("measure_weather_calibration")
    mode = "network" if use_network else "fixtures"
    ledgers: dict[str, PaperLedger] = {}
    register = None
    store = None
    if persist and not reset:
        for track in WEATHER_TRACKS:
            path = ledger_path(artifact_dir, track)
            if path.exists():
                ledgers[track] = PaperLedger.load(path)
        register = WeatherRegister.load_or_create(register_path(artifact_dir))
        store = CalibrationStore.load_or_create(store_path(artifact_dir))
    measured_at = datetime.now(UTC).isoformat()
    summaries, ledgers, register, store, extras = await measure_weather_calibration(
        use_fixtures=not use_network,
        limit=limit,
        backfill_days=backfill_days,
        allow_paid_keys=allow_paid_keys,
        parameters=parameters,
        ledgers=ledgers,
        register=register,
        store=store,
        model_fees=model_fees,
    )
    ordered = [summaries["naive"], summaries["calibrated"]]
    artifact = persist_run(
        ordered,
        {track: ledgers[track] for track in WEATHER_TRACKS if track in ledgers} if persist else {},
        artifact_dir=artifact_dir,
        mode=mode,
        measured_at=measured_at,
        limit=limit,
        kalshi_env=None,
        scoreboard_name=SCOREBOARD_NAME,
        write_latest=publish_latest,
        artifact_kwargs={
            "primary_track": CALIBRATED_TRACK,
            "venue_focus": "polymarket",
            "venues": ("polymarket",),
            "label_suffix": "WEATHER CALIBRATION A/B",
            "track_family": TRACK_FAMILY,
        },
    )
    if persist:
        register.save(register_path(artifact_dir))
        store.save(store_path(artifact_dir))
    report = build_report(
        summaries, mode=mode, measured_at=measured_at, run_id=artifact["meta"]["run_id"], register=register, store=store, backfill=extras.get("backfill")
    )
    write_json(artifact_dir / REPORT_NAME, report)
    _print(summaries, report)
    print(f"artifacts: {artifact_dir / SCOREBOARD_NAME}  {artifact_dir / REPORT_NAME}  {register_path(artifact_dir)}  {store_path(artifact_dir)}  {artifact_dir / 'paper'}")
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", action="store_true", help="read public Gamma/CLOB weather events and free Open-Meteo forecasts")
    parser.add_argument("--limit", type=int, default=400, help="open weather events to consider (default 400 ~ every city for the next few days)")
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--no-persist", action="store_true", help="do not carry ledgers / register / calibration store across runs")
    parser.add_argument("--reset", action="store_true", help="start from fresh ledgers, an empty register and an empty calibration store")
    parser.add_argument("--no-fees", action="store_true", help="disable the Polymarket taker-fee model on paper fills")
    parser.add_argument("--publish-latest", action="store_true", help="also overwrite scoreboard_latest.json")
    data = parser.add_argument_group("data sources (free by default)")
    data.add_argument("--backfill-days", type=int, default=0, help="network: fill the calibration store from the last N days of resolved city-days x previous-runs day-1 forecasts (<= 92)")
    data.add_argument("--allow-paid-keys", action="store_true", help=f"use the Open-Meteo customer endpoint when {OPEN_METEO_API_KEY_ENV} is set (OFF by default)")
    data.add_argument("--model", action="append", default=[], help="Open-Meteo model id (repeatable; default: the seven free global models)")
    exp = parser.add_argument_group("pre-registered parameters (override only for sensitivity checks)")
    exp.add_argument("--edge-threshold", type=Decimal, default=None, help="|p - mid| needed to admit a leg (default 0.05)")
    exp.add_argument("--minimum-edge", type=Decimal, default=None, help="cost-adjusted touch edge floor (default 0.02)")
    exp.add_argument("--max-order-size", type=Decimal, default=None)
    exp.add_argument("--min-calibration-samples", type=int, default=None, help="settled city-days before the calibrated lane trades a city (default 20)")
    exp.add_argument("--min-settled", type=int, default=None, help="settled admissions per lane before PASS/FAIL (default 50)")
    exp.add_argument("--margin", type=Decimal, default=None, help="required calibrated - naive EV per contract (default 0.02)")
    exp.add_argument("--absolute-min-ev", type=Decimal, default=None, help="required calibrated EV per contract (default 0.025)")
    exp.add_argument("--lead-days", type=int, action="append", default=[], help="admissible lead in city-local days (repeatable; default 1 = tomorrow)")
    args = parser.parse_args()
    require_paper_only("measure_weather_calibration")
    if args.allow_paid_keys and not os.getenv(OPEN_METEO_API_KEY_ENV):
        print(f"note: --allow-paid-keys given but {OPEN_METEO_API_KEY_ENV} is not set; using the free endpoints")
    asyncio.run(
        run(
            use_network=args.network,
            limit=max(1, args.limit),
            artifact_dir=args.artifact_dir,
            persist=not args.no_persist,
            reset=args.reset,
            model_fees=not args.no_fees,
            publish_latest=args.publish_latest,
            backfill_days=max(0, args.backfill_days),
            allow_paid_keys=args.allow_paid_keys,
            parameters=parameters_from_args(args),
        )
    )


if __name__ == "__main__":
    main()
