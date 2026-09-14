"""Run every paper track once and write ledger-backed scoreboard artifacts.

Outputs (relative to ``--artifact-dir``, default ``artifacts/``):

* ``scoreboard_<mode>.json``       dashboard artifact (schema 1.3.0, source=measured)
* ``scoreboard_latest.json``       same document; what the dashboard sync prefers
* ``gate_report_<mode>.json``      per-pair admissibility verdicts for gated_cross_venue
* ``gate_report_latest.json``      same document; emitted even with zero candidates
* ``specialist_scoreboard_<mode>.json``, ``specialist_scoreboard_latest.json``
                                   category_specialist board: per-trader-category scores,
                                   promotions, follow log, pre-registered evaluation
* ``paper/specialist_follow_state.json``  the specialist follow log carried across runs
* ``paper/ledger_<track>.json``    full ledger per track (fills, marks, equity curve)
* ``paper/equity_curve_<track>.jsonl``  one appended equity point per run
* ``paper/runs/<run_id>.json``     raw track summaries for this run

Paper-only: refuses to start if the environment requests live trading.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from core.config import require_paper_only
from core.ledger import PaperLedger
from research.harvest_scoreboard import findings_from_harvest
from research.harvests import HarvestBundle
from research.news_signals import build_signal_source
from research.scoreboard import GATED_TRACK, SPECIALIST_TRACK, TRACKS, TrackSummary, measure_all_with_ledgers
from research.scoreboard_artifact import build_gate_report, build_scoreboard_artifact
from research.specialist_scoreboard import SpecialistState, build_specialist_report, load_specialist_state, state_path
from research.specialist_sources import build_trader_source
from strategies.edge import load_priors
from strategies.polymarket_arb import ArbParameters


def add_specialist_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("category specialist track")
    group.add_argument(
        "--specialist-traders", type=int, default=10,
        help="network runs: wallets read from the public Polymarket volume leaderboard (0 disables the lane)",
    )
    group.add_argument("--specialist-window", default="month", help="leaderboard window: day, week, month, all")


def add_arb_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("polymarket arbitrage tracks")
    group.add_argument("--slippage-ticks", type=Decimal, default=None, help="per-leg slippage buffer in ticks (default 1)")
    group.add_argument("--max-sets", type=Decimal, default=None, help="max sets per opportunity (default 100)")
    group.add_argument("--max-capital", type=Decimal, default=None, help="max USDC per opportunity (default 250)")
    group.add_argument("--min-net-edge", type=Decimal, default=None, help="min net edge per set after fees+slippage (default 0.001)")
    group.add_argument("--converter-fee-bps", type=Decimal, default=None, help="NegRiskAdapter conversion fee assumption (default 0)")


def arb_parameters_from_args(args: argparse.Namespace) -> ArbParameters | None:
    overrides = {
        "slippage_ticks": args.slippage_ticks,
        "maximum_sets": args.max_sets,
        "maximum_capital": args.max_capital,
        "minimum_net_edge_per_set": args.min_net_edge,
        "converter_fee_bps": args.converter_fee_bps,
    }
    provided = {k: v for k, v in overrides.items() if v is not None}
    return ArbParameters(**provided) if provided else None


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def to_jsonable(value: Any) -> Any:
    """Round-trip through the JSON encoder so nested Decimals become floats."""
    return json.loads(json.dumps(value, default=json_default))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, default=json_default, indent=2, sort_keys=True) + "\n"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, default=json_default, sort_keys=True) + "\n")


def ledger_path(artifact_dir: Path, track: str) -> Path:
    return artifact_dir / "paper" / f"ledger_{track}.json"


def equity_curve_path(artifact_dir: Path, track: str) -> Path:
    return artifact_dir / "paper" / f"equity_curve_{track}.jsonl"


def load_ledgers(artifact_dir: Path, tracks: tuple[str, ...]) -> dict[str, PaperLedger]:
    ledgers: dict[str, PaperLedger] = {}
    for track in tracks:
        path = ledger_path(artifact_dir, track)
        if path.exists():
            ledgers[track] = PaperLedger.load(path)
    return ledgers


def persist_run(
    summaries: list[TrackSummary],
    ledgers_by_track: dict[str, PaperLedger],
    *,
    artifact_dir: Path,
    mode: str,
    measured_at: str,
    limit: int,
    kalshi_env: str | None,
    cycle: int | None = None,
    harvest_dir: Path | None = None,
    scoreboard_name: str | None = None,
    write_latest: bool = True,
    artifact_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save ledgers, the scoreboard artifact(s) and the raw run manifest.

    ``scoreboard_name`` defaults to ``scoreboard_<mode>.json``; a track family
    with its own CLI passes its own name (``scoreboard_polymarket_arb.json``)
    and ``write_latest=False`` so it does not displace the full board.
    """
    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    findings = None
    if harvest_dir is not None and harvest_dir.exists():
        findings = findings_from_harvest(HarvestBundle.load(harvest_dir)) or None
    for track, ledger in ledgers_by_track.items():
        ledger.save(ledger_path(artifact_dir, track))
        if ledger.equity_curve:
            append_jsonl(
                equity_curve_path(artifact_dir, track),
                {"run_id": run_id, "track": track, "mode": mode, **asdict(ledger.equity_curve[-1])},
            )
    artifact = build_scoreboard_artifact(
        summaries,
        mode=mode,
        measured_at=measured_at,
        limit=limit,
        kalshi_env=kalshi_env,
        cycle=cycle,
        findings=findings,
        **(artifact_kwargs or {}),
    )
    artifact["meta"]["run_id"] = run_id
    if any(summary.track == GATED_TRACK for summary in summaries):
        gate_report = build_gate_report(summaries, mode=mode, measured_at=measured_at, run_id=run_id)
        write_json(artifact_dir / f"gate_report_{mode}.json", gate_report)
        write_json(artifact_dir / "gate_report_latest.json", gate_report)
        artifact["gate_report"] = {"file": f"gate_report_{mode}.json", "totals": gate_report["totals"]}
    specialist = next((s for s in summaries if s.track == SPECIALIST_TRACK), None)
    if specialist is not None:
        report = build_specialist_report(specialist, mode=mode, measured_at=measured_at, run_id=run_id)
        write_json(artifact_dir / f"specialist_scoreboard_{mode}.json", report)
        write_json(artifact_dir / "specialist_scoreboard_latest.json", report)
        artifact["specialist_scoreboard"] = {"file": f"specialist_scoreboard_{mode}.json", "totals": report["totals"]}
        if SPECIALIST_TRACK in ledgers_by_track and specialist.metrics.get("follow_state"):
            # The follow log persists alongside the ledgers, never without them.
            SpecialistState.from_dict(specialist.metrics["follow_state"]).save(state_path(artifact_dir))
    write_json(artifact_dir / (scoreboard_name or f"scoreboard_{mode}.json"), artifact)
    if write_latest:
        write_json(artifact_dir / "scoreboard_latest.json", artifact)
    write_json(
        artifact_dir / "paper" / "runs" / f"{run_id}.json",
        {
            "run_id": run_id,
            "paper_only": True,
            "mode": mode,
            "measured_at": measured_at,
            "label": artifact["meta"]["label"],
            "venues": artifact["meta"]["venues"],
            "venue_focus": artifact["meta"]["venue_focus"],
            "kalshi_env": kalshi_env,
            "primary_track": artifact["meta"]["primary_track"],
            "track_family": artifact["meta"].get("track_family"),
            "tracks": [summary.as_dict() for summary in summaries],
        },
    )
    return artifact


def _print_open(summaries: list[TrackSummary], label: str) -> None:
    print(f"\n{label}")
    header = f"{'track':<28}{'cand':>6}{'adm':>6}{'ord':>6}{'fill':>6}{'real':>10}{'unreal':>10}{'fees':>8}  flag"
    print(header)
    print("-" * len(header))
    for summary in summaries:
        ledger = summary.ledger
        print(
            f"{summary.track:<28}{summary.candidates:>6}{summary.admitted:>6}"
            f"{summary.proposed_orders:>6}{summary.paper_fills:>6}"
            f"{float(ledger.get('realized_pnl', 0)):>10.4f}"
            f"{float(ledger.get('unrealized_pnl', 0)):>10.4f}"
            f"{float(ledger.get('fees_paid', 0)):>8.2f}"
            f"  {'SETTLEMENT-RISK' if summary.settlement_risk_flag else '-'}"
        )
        if summary.refused_by_reason:
            reasons = ", ".join(f"{k}={v}" for k, v in sorted(summary.refused_by_reason.items()))
            print(f"{'':<28}refused: {reasons}")


async def run(
    *,
    use_network: bool,
    limit: int,
    artifact_dir: Path,
    priors_path: Path | None,
    persist_ledgers: bool,
    reset_ledgers: bool,
    kalshi_env: str | None,
    model_fees: bool,
    harvest_dir: Path | None = None,
    news_signals_path: Path | None = None,
    news_rss: tuple[str, ...] = (),
    event_limit: int | None = None,
    arb_parameters: ArbParameters | None = None,
    specialist_traders: int | None = None,
    specialist_window: str = "month",
) -> dict[str, Any]:
    require_paper_only("measure_all")
    mode = "network" if use_network else "fixtures"
    priors = load_priors(priors_path) if priors_path else None
    carry = persist_ledgers and not reset_ledgers
    ledgers = load_ledgers(artifact_dir, TRACKS) if carry else {}
    specialist_state = load_specialist_state(artifact_dir) if carry else None
    measured_at = datetime.now(UTC).isoformat()
    summaries, ledgers_by_track = await measure_all_with_ledgers(
        use_fixtures=not use_network,
        limit=limit,
        priors=priors,
        ledgers=ledgers,
        kalshi_env=kalshi_env,
        model_fees=model_fees,
        news_signals=build_signal_source(
            use_fixtures=not use_network, signals_path=news_signals_path, rss_urls=news_rss
        ),
        event_limit=event_limit,
        arb_parameters=arb_parameters,
        specialist_source=build_trader_source(
            use_fixtures=not use_network, traders=specialist_traders, window=specialist_window
        ),
        specialist_state=specialist_state,
    )
    artifact = persist_run(
        summaries,
        ledgers_by_track if persist_ledgers else {},
        artifact_dir=artifact_dir,
        mode=mode,
        measured_at=measured_at,
        limit=limit,
        kalshi_env=kalshi_env,
        harvest_dir=harvest_dir,
    )
    _print_open(summaries, f"paper scoreboard ({mode}, {limit} markets/venue)")
    gate = artifact.get("gate_report", {}).get("totals")
    if gate:
        print(
            f"\ngated_cross_venue: candidates={gate['candidates']} gate_admitted={gate['gate_admitted']} "
            f"gate_refused={gate['gate_refused']} priced_but_no_edge={gate['priced_but_no_edge']} "
            f"traded={gate['traded']} status={gate['status']}"
        )
        if gate["all_stage_reject_reasons"]:
            reasons = ", ".join(f"{k}={v}" for k, v in gate["all_stage_reject_reasons"].items())
            print(f"  stage rejects: {reasons}")
    spec = artifact.get("specialist_scoreboard", {}).get("totals")
    if spec:
        print(
            f"\ncategory_specialist: traders={spec['traders']} resolved_bets={spec['resolved_bets']} "
            f"specialists={spec['specialists']} follows_this_run={spec['follows_this_run']} "
            f"follow_log={spec['follow_log_resolved']} resolved/{spec['follow_log_pending']} pending "
            f"evaluation={spec['evaluation_status']}"
        )
    print(
        f"\nledger totals: realized={artifact['totals']['realized_pnl']} "
        f"unrealized={artifact['totals']['unrealized_pnl']} fees={artifact['totals']['fees_paid']} "
        f"paper_pnl={artifact['totals']['paper_pnl']}"
    )
    print(
        f"artifacts: {artifact_dir / f'scoreboard_{mode}.json'}  "
        f"{artifact_dir / f'gate_report_{mode}.json'}  {artifact_dir / 'paper'}"
    )
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", action="store_true", help="read public venue books (paper fills stay local)")
    parser.add_argument("--limit", type=int, default=25, help="markets per venue")
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--harvest-dir", type=Path, default=None, help="directory of harvested resolved markets; adds divergence findings when present")
    parser.add_argument("--priors", type=Path, default=None, help="JSON {market_id: probability} for the fair-value track")
    parser.add_argument("--no-persist", action="store_true", help="do not carry ledgers across runs")
    parser.add_argument("--reset-ledgers", action="store_true", help="start every track from a fresh ledger")
    parser.add_argument("--kalshi-env", choices=("demo", "prod"), default=None, help="Kalshi public API host (default: KALSHI_ENV or demo)")
    parser.add_argument("--no-fees", action="store_true", help="disable the Kalshi and Polymarket fee models on paper fills")
    parser.add_argument("--news-signals", type=Path, default=None, help="JSON signals file for the news_underreaction lane (operator owns the implied probabilities)")
    parser.add_argument("--news-rss", action="append", default=[], metavar="URL", help="public RSS/Atom feed to match headlines against snapshot markets (never mapped to a probability; repeatable)")
    parser.add_argument("--event-limit", type=int, default=None, help="Polymarket events for the arb tracks (default: --limit)")
    add_arb_arguments(parser)
    add_specialist_arguments(parser)
    args = parser.parse_args()
    require_paper_only("measure_all")
    asyncio.run(
        run(
            use_network=args.network,
            limit=max(1, args.limit),
            artifact_dir=args.artifact_dir,
            priors_path=args.priors,
            persist_ledgers=not args.no_persist,
            reset_ledgers=args.reset_ledgers,
            kalshi_env=args.kalshi_env,
            model_fees=not args.no_fees,
            harvest_dir=args.harvest_dir,
            news_signals_path=args.news_signals,
            news_rss=tuple(args.news_rss),
            event_limit=args.event_limit,
            arb_parameters=arb_parameters_from_args(args),
            specialist_traders=args.specialist_traders,
            specialist_window=args.specialist_window,
        )
    )


if __name__ == "__main__":
    main()
