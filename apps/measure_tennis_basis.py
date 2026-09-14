"""Run the ``tennis_basis`` paper track: venue tennis mids vs. a free public consensus line.

Outputs (relative to ``--artifact-dir``, default ``artifacts/``):

* ``scoreboard_tennis_basis.json``   dashboard artifact (schema 1.3.0, source=measured,
                                     track_family=tennis_basis); ``scoreboard_latest.json``
                                     only with ``--publish-latest``
* ``tennis_basis_latest.json``       full report: pre-registered rule, verdict (settlement-
                                     confirmed and provisional), every venue market's reason,
                                     every gap record with its observations, ledger, source
* ``tennis_basis/register.json``     the persistent gap register (open and closed records)
* ``paper/ledger_tennis_basis.json`` the track's PaperLedger; ``paper/runs/<run_id>.json``

Fixture mode (default) replays ``research/fixtures/tennis_basis_replay.json``.
Network mode reads public Kalshi (``KXATPMATCH`` / ``KXWTAMATCH``) and Polymarket
(tag 864, moneyline) books plus, when ``ODDS_API_KEY`` is set, The Odds API free
tier (``regions=eu``, ``markets=h2h``). Without a key or ``--odds-file`` the run is
an honest empty (``status=no_outside_source``). Paper-only: refuses live flags.
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
from research.tennis_basis import (
    TENNIS_TRACK,
    GapRegister,
    load_operator_results,
    measure_tennis_basis,
    register_path,
)
from research.tennis_odds import (
    DEFAULT_REGIONS,
    FREE_TIER,
    ODDS_API_KEY_ENV,
    build_outside_source,
)
from strategies.tennis_basis import BasisParameters

SCOREBOARD_NAME = "scoreboard_tennis_basis.json"
REPORT_NAME = "tennis_basis_latest.json"
TRACK_FAMILY = "tennis_basis"


def build_report(summary: TrackSummary, *, mode: str, measured_at: str, run_id: str, register: GapRegister) -> dict[str, Any]:
    m = summary.metrics
    return to_jsonable(
        {
            "schema_version": "1.0.0",
            "kind": "tennis_basis_report",
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
                    "When a Kalshi/Polymarket tennis match-winner mid drifts >= 3c from the free public "
                    "consensus line, the venue mid moves back toward the line before the match starts."
                ),
                "pre_registered": m.get("verdict", {}).get("pre_registered"),
                "outside_line": {
                    "source": FREE_TIER,
                    "consensus": "sharp book (pinnacle) when quoted, else median of >= min_books de-vigged books",
                    "devig": "multiplicative (normalise 1/odds to sum to 1)",
                },
                "settlement_mismatch_filter": {
                    "at_admission": "venue rules must state retirement -> advancing player and walkover -> fair price / 50-50; ITF refused; silence refused",
                    "after_start": "records settle 'confirmed' only on a binary venue result; walkover / cancellation / 50-50 / fair-price settlements and operator-reported retirements are excluded",
                    "bookmaker_basis": m.get("bookmaker_basis"),
                },
            },
            "parameters": m.get("parameters"),
            "outside_source": m.get("outside_source"),
            "snapshot": m.get("snapshot"),
            "markets_by_venue": m.get("markets_by_venue"),
            "candidates": summary.candidates,
            "gaps_opened_this_run": m.get("gaps_opened"),
            "gaps_observed_this_run": m.get("gaps_observed"),
            "gaps_closed_this_run": m.get("gaps_closed"),
            "proposed_orders": summary.proposed_orders,
            "paper_fills": summary.paper_fills,
            "refused_by_reason": dict(sorted(summary.refused_by_reason.items())),
            "register": m.get("register"),
            "verdict": m.get("verdict"),
            "verdict_provisional_including_pending": m.get("verdict_provisional_including_pending"),
            "by_venue": m.get("by_venue"),
            "measurements": m.get("measurements"),
            "records": [r.as_dict() for r in register.records.values()],
            "fills": summary.fills,
            "ledger": summary.ledger,
            "not_validated": [
                "network sample: no free-tier key was available in the build environment, so the live "
                "Kalshi/Polymarket vs. Odds-API gap distribution is UNKNOWN until an operator runs with ODDS_API_KEY",
                "bookmaker 'match completed' basis vs. venue 'advances' basis (retirement premium) is not modelled",
                "Pinnacle odds on the free tier come from Pinnacle's public site and may be delayed",
                "Kalshi occurrence_datetime is a session time, not the first serve; the outside commence_time is used when paired",
                "retirements are only detected from an operator results file; venue results pay the winner either way",
                "fixture closure rates are hand-written and carry no evidence about the hypothesis",
            ],
        }
    )


def _print(summary: TrackSummary, report: dict[str, Any]) -> None:
    m = summary.metrics
    print(f"\ntennis_basis ({report['mode']}) status={report['status']} outside={m['outside_source']['source']} events={m['outside_source']['events']}")
    for error in m["outside_source"]["errors"][:5]:
        print(f"  outside error: {error}")
    print(f"venue tennis markets: {m['markets_by_venue']}  candidates={summary.candidates}  opened={m['gaps_opened']} observed={m['gaps_observed']} closed={m['gaps_closed']}")
    if summary.refused_by_reason:
        print("  reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.refused_by_reason.items())))
    header = f"{'record':<58}{'side':<5}{'gap0':>8}{'gapT':>8}{'closure':>9}  status / settlement"
    print(header)
    print("-" * len(header))
    for r in report["records"]:
        print(
            f"{r['record_id'][:57]:<58}{str(r['side'] or '-'):<5}{float(r['gap_open']):>8.4f}"
            f"{(float(r['gap_final']) if r['gap_final'] is not None else float('nan')):>8.4f}"
            f"{(float(r['closure_fraction']) if r['closure_fraction'] is not None else float('nan')):>9.3f}"
            f"  {r['status']} / {r['settlement_status']}{(' (' + r['settlement_detail'] + ')') if r['settlement_detail'] else ''}"
        )
    v, p = report["verdict"], report["verdict_provisional_including_pending"]
    print(
        f"\nverdict (settlement-confirmed): n={v['n']} closed>=50%={v['closed_half']} rate={v['rate']} "
        f"wilson95={v['wilson_95']} -> {v['status']}  [pre-registered: n>={v['pre_registered']['min_sample']}, rate>={v['pre_registered']['pass_rate']}]"
    )
    print(f"provisional (incl. {v['pending_settlement_check']} pending): n={p['n']} rate={p['rate']} -> {p['status']}; excluded by settlement filter={v['excluded_settlement_mismatch']}; open={v['open_records']}")
    ledger = report["ledger"] or {}
    print(f"ledger: realized={ledger.get('realized_pnl')} unrealized={ledger.get('unrealized_pnl')} fees={ledger.get('fees_paid')} equity={ledger.get('equity')} fills={ledger.get('fills')}")
    print(f"network: {report['network_status']}")


def parameters_from_args(args: argparse.Namespace) -> BasisParameters | None:
    overrides = {
        "gap_threshold": args.gap_threshold,
        "closure_target": args.closure_target,
        "pass_rate": args.pass_rate,
        "min_sample": args.min_sample,
        "min_books": args.min_books,
        "maximum_order_size": args.max_order_size,
    }
    provided = {k: v for k, v in overrides.items() if v is not None}
    if args.sharp_book:
        provided["sharp_books"] = tuple(args.sharp_book)
    return BasisParameters(**provided) if provided else None


async def run(
    *,
    use_network: bool,
    limit: int,
    artifact_dir: Path,
    persist: bool,
    reset: bool,
    kalshi_env: str | None,
    model_fees: bool,
    publish_latest: bool,
    odds_file: Path | None,
    results_file: Path | None,
    regions: str,
    max_credits: int,
    min_remaining: int,
    sport_keys: tuple[str, ...] | None,
    parameters: BasisParameters | None,
) -> dict[str, Any]:
    require_paper_only("measure_tennis_basis")
    mode = "network" if use_network else "fixtures"
    ledger = None
    register = None
    if persist and not reset:
        path = ledger_path(artifact_dir, TENNIS_TRACK)
        ledger = PaperLedger.load(path) if path.exists() else None
        register = GapRegister.load_or_create(register_path(artifact_dir))
    measured_at = datetime.now(UTC).isoformat()
    source = build_outside_source(
        use_fixtures=not use_network, odds_file=odds_file, regions=regions,
        max_credits_per_run=max_credits, min_remaining=min_remaining, sport_keys=sport_keys,
    )
    summary, ledger, register = await measure_tennis_basis(
        use_fixtures=not use_network,
        limit=limit,
        kalshi_env=kalshi_env,
        outside_source=source,
        parameters=parameters,
        ledger=ledger,
        register=register,
        model_fees=model_fees,
        operator_results=load_operator_results(results_file) if results_file else None,
    )
    artifact = persist_run(
        [summary],
        {TENNIS_TRACK: ledger} if persist else {},
        artifact_dir=artifact_dir,
        mode=mode,
        measured_at=measured_at,
        limit=limit,
        kalshi_env=kalshi_env,
        scoreboard_name=SCOREBOARD_NAME,
        write_latest=publish_latest,
        artifact_kwargs={
            "primary_track": TENNIS_TRACK,
            "venue_focus": "cross",
            "label_suffix": "TENNIS BASIS",
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
    parser.add_argument("--network", action="store_true", help="read public venue books (+ The Odds API when ODDS_API_KEY is set)")
    parser.add_argument("--limit", type=int, default=60, help="tennis markets per venue")
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--no-persist", action="store_true", help="do not carry the ledger / gap register across runs")
    parser.add_argument("--reset", action="store_true", help="start from a fresh ledger and empty gap register")
    parser.add_argument("--kalshi-env", choices=("demo", "prod"), default=None)
    parser.add_argument("--no-fees", action="store_true", help="disable the venue fee models on paper fills")
    parser.add_argument("--publish-latest", action="store_true", help="also overwrite scoreboard_latest.json")
    outside = parser.add_argument_group("outside line (free public odds)")
    outside.add_argument("--odds-file", type=Path, default=None, help="saved The-Odds-API v4 /odds response (or fixture in that shape); overrides the network source")
    outside.add_argument("--results-file", type=Path, default=None, help="operator JSON {event_id: completed|retired|walkover|cancelled} for the post-start filter")
    outside.add_argument("--regions", default=DEFAULT_REGIONS, help="Odds API regions (default eu; pinnacle lives there)")
    outside.add_argument("--max-credits", type=int, default=6, help="Odds API credits to spend per run (default 6)")
    outside.add_argument("--min-remaining", type=int, default=20, help="stop spending when x-requests-remaining would drop below this")
    outside.add_argument("--sport-key", action="append", default=[], help="restrict to these Odds API sport keys (repeatable; default: every in-season tennis key)")
    exp = parser.add_argument_group("pre-registered parameters (override only for sensitivity checks)")
    exp.add_argument("--gap-threshold", type=Decimal, default=None)
    exp.add_argument("--closure-target", type=Decimal, default=None)
    exp.add_argument("--pass-rate", type=Decimal, default=None)
    exp.add_argument("--min-sample", type=int, default=None)
    exp.add_argument("--min-books", type=int, default=None)
    exp.add_argument("--max-order-size", type=Decimal, default=None)
    exp.add_argument("--sharp-book", action="append", default=[], help="sharp bookmaker keys in priority order (default pinnacle)")
    args = parser.parse_args()
    require_paper_only("measure_tennis_basis")
    if args.network and not os.getenv(ODDS_API_KEY_ENV) and args.odds_file is None:
        print(f"note: {ODDS_API_KEY_ENV} is not set and no --odds-file given; the outside line is UNKNOWN and the run will be an honest empty")
    asyncio.run(
        run(
            use_network=args.network,
            limit=max(1, args.limit),
            artifact_dir=args.artifact_dir,
            persist=not args.no_persist,
            reset=args.reset,
            kalshi_env=args.kalshi_env,
            model_fees=not args.no_fees,
            publish_latest=args.publish_latest,
            odds_file=args.odds_file,
            results_file=args.results_file,
            regions=args.regions,
            max_credits=args.max_credits,
            min_remaining=args.min_remaining,
            sport_keys=tuple(args.sport_key) or None,
            parameters=parameters_from_args(args),
        )
    )


if __name__ == "__main__":
    main()
