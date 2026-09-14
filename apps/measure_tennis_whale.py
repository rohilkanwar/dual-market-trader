"""Tennis whale copy with lag: paper measurement CLI.

Replays a public Polymarket tennis taker tape, qualifies whales walk-forward,
paper-copies their prints at 30 s / 2 min / 10 min into one
:class:`core.ledger.PaperLedger` per lag and scores settlement EV and closing-line
value with a market-clustered bootstrap. Outputs (relative to ``--artifact-dir``,
default ``artifacts/``):

* ``tennis_whale_report_latest.json``  verdicts, whales, per-copy rows, pre-registration (headline document)
* ``scoreboard_tennis_whale.json``     scoreboard artifact for the three lag tracks (measured, ledger-backed)
* ``paper/ledger_<track>.json``        one ledger per lag (always rebuilt from the tape; never carried across runs)
* ``paper/equity_curve_<track>.jsonl``
* ``paper/runs/<run_id>.json``         run record picked up by the dashboard experiments index

Paper-only: refuses to start if the environment requests live trading. Reads
only unauthenticated Polymarket Gamma / Data API endpoints (and the public
Kalshi series list to record that Kalshi tennis is listed but not identifiable).
The harvested tape is written to ``--harvest-dir/polymarket_tennis_tape.json``
(git-ignored) and reused by later runs unless ``--harvest`` is given again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from apps.measure_all import append_jsonl, equity_curve_path, ledger_path, to_jsonable, write_json
from core.config import require_paper_only
from core.ledger import PaperLedger
from research.scoreboard import TrackSummary
from research.scoreboard_artifact import build_scoreboard_artifact
from research.tennis_whale import (
    COPIES_FILE,
    FIXTURE_PATH,
    HARVEST_FILE,
    TRACK_FAMILY,
    ReplayResult,
    TennisTape,
    build_report,
    harvest_tennis_tape,
    load_tape,
    replay_copy_tracks,
    tape_from_payload,
)
from strategies.tennis_whale_copy import CopyParameters, track_for_lag

SCOREBOARD_FILE = "scoreboard_tennis_whale.json"
REPORT_FILE = "tennis_whale_report_latest.json"


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def load_or_harvest_tape(
    *,
    use_network: bool,
    harvest: bool,
    harvest_dir: Path,
    tape_path: Path | None,
    harvest_kwargs: dict[str, Any],
) -> TennisTape:
    if tape_path is not None:
        return load_tape(tape_path)
    if not use_network:
        return load_tape(FIXTURE_PATH)
    target = harvest_dir / HARVEST_FILE
    if harvest:
        payload = await harvest_tennis_tape(**harvest_kwargs)
        write_json(target, payload)
    if target.exists():
        return load_tape(target)
    return tape_from_payload(
        {"meta": {"source": "network", "errors": [f"no tennis tape at {target}; run with --harvest"]}, "markets": [], "trades": []}
    )


def persist_tennis_run(
    *,
    artifact_dir: Path,
    report: dict[str, Any],
    result: ReplayResult,
    summaries: list[TrackSummary],
    ledgers: dict[str, PaperLedger],
    mode: str,
    measured_at: str,
    run_id: str,
    markets: int,
    lags: tuple[int, ...],
) -> dict[str, Any]:
    write_json(
        artifact_dir / COPIES_FILE,
        {
            "kind": "tennis_whale_copies",
            "paper_only": True,
            "run_id": run_id,
            "mode": mode,
            "status": report["status"],
            "measured_at": measured_at,
            "copies": [c.as_dict() for c in result.copies],
            "whale_own": result.whale_own,
        },
    )
    for track, ledger in ledgers.items():
        ledger.save(ledger_path(artifact_dir, track))
        if ledger.equity_curve:
            append_jsonl(equity_curve_path(artifact_dir, track), {"run_id": run_id, "track": track, "mode": mode, **asdict(ledger.equity_curve[-1])})
    findings = {
        "tennis_whale_copy": {
            "status": report["status"],
            "headline": report["headline"],
            "overall_verdict": report["overall_verdict"],
            "verdicts": {row["check"]: row["verdict"] for row in report["verdict_table"]},
            "kill_rule": report["kill_rule"],
            "whales": report.get("whales", {}).get("qualified", 0),
            "copies": report.get("copies_total", 0),
            "report": REPORT_FILE,
        },
        "divergence_findings_status": "not_applicable_tennis_whale_run",
        "arbai_summary": report["headline"],
    }
    primary = track_for_lag(lags[0])
    artifact = build_scoreboard_artifact(
        summaries,
        mode=mode,
        measured_at=measured_at,
        limit=markets,
        kalshi_env=None,
        primary_track=primary,
        findings=findings,
        venues=("polymarket",),
        venue_focus="polymarket",
        label_suffix="TENNIS WHALE COPY",
        track_family=TRACK_FAMILY,
    )
    artifact["meta"]["run_id"] = run_id
    artifact["meta"]["kind"] = "tennis_whale_copy"
    artifact["meta"]["refresh"] = "python -m apps.measure_tennis_whale [--network --harvest] && cd dashboard && npm run sync-artifacts"
    write_json(artifact_dir / SCOREBOARD_FILE, artifact)
    write_json(artifact_dir / REPORT_FILE, report)
    write_json(
        artifact_dir / "paper" / "runs" / f"{run_id}.json",
        {
            "run_id": run_id,
            "paper_only": True,
            "kind": "tennis_whale_copy",
            "mode": mode,
            "measured_at": measured_at,
            "venues": ["polymarket"],
            "venue_focus": "polymarket",
            "kalshi_env": None,
            "primary_track": primary,
            "track_family": TRACK_FAMILY,
            "label": artifact["meta"]["label"],
            "tennis_whale_copy": findings["tennis_whale_copy"],
            "tracks": [{**summary.as_dict(), "family": TRACK_FAMILY} for summary in summaries],
        },
    )
    return artifact


def _print_report(report: dict[str, Any]) -> None:
    print(f"\ntennis whale copy paper measurement ({report['mode']}, run {report['run_id']}, status {report['status']})")
    print(report["headline"])
    print(f"\n{'scope':<9}{'check':<42}{'verdict':<20}detail")
    print("-" * 120)
    for row in report["verdict_table"]:
        print(f"{row['scope']:<9}{row['check']:<42}{row['verdict']:<20}{(row['detail'] or '')[:60]}")
    universe = report.get("universe")
    if universe:
        print(
            f"\nuniverse: {universe['markets']} markets ({universe['resolved_markets']} resolved, {universe['open_markets']} open), "
            f"{universe['prints']} prints, {universe['wallets']} wallets, truncated {universe['markets_truncated']}, source {universe['source']}"
        )
    whales = report.get("whales")
    if whales:
        print(f"whales: {whales['qualified']} qualified ({whales['refused_two_sided']} refused two-sided), {whales['signals']} signals; reasons {whales['signal_reasons']}")
        for w in whales["top"][:8]:
            print(f"  {w['wallet'][:14]}  large={w['large_fills']:<4} mkts={w['markets']:<4} two_sided={w['two_sided_share']}  signals={w['signals']:<4} copies={w['copies_all_lags']:<4} own_roi={w['own_settlement_roi_mean']}")
    if report.get("lags"):
        print(f"\n{'lag':<6}{'copies':>7}{'mkts':>6}{'n_set':>6}{'ROI':>9}{'ci_low':>9}{'ci_high':>9}{'n_clv':>6}{'CLV':>9}{'ci_low':>9}{'ci_high':>9}  verdict")
        for r in report["lags"].values():
            s, c = r["settlement_roi"], r["clv_roi"]
            f = lambda v: f"{v:>9.4f}" if v is not None else f"{'-':>9}"  # noqa: E731
            print(f"{r['lag']:<6}{r['copies']:>7}{s['n_clusters']:>6}{s['n']:>6}{f(s['mean'])}{f(s['ci_low'])}{f(s['ci_high'])}{c['n']:>6}{f(c['mean'])}{f(c['ci_low'])}{f(c['ci_high'])}  {r['verdict']}")
    tracks = report.get("tracks", {})
    if tracks:
        print(f"\n{'track':<24}{'cand':>6}{'adm':>6}{'fill':>6}{'real':>10}{'unreal':>10}{'fees':>8}  refused")
        for name, t in tracks.items():
            l = t["ledger"]
            print(f"{name:<24}{t['candidates']:>6}{t['admitted']:>6}{t['paper_fills']:>6}{float(l.get('realized_pnl') or 0):>10.4f}{float(l.get('unrealized_pnl') or 0):>10.4f}{float(l.get('fees_paid') or 0):>8.2f}  {t['refused_by_reason']}")
    own = report.get("whale_own_benchmark", {}).get("settlement_roi")
    if own:
        print(f"\nwhale-own benchmark (lag 0): n={own['n']} markets={own['n_clusters']} ROI={own['mean']} ci=[{own['ci_low']}, {own['ci_high']}] sufficient={own.get('sufficient')}")
    if report.get("lags"):
        print("\nby market type (settlement ROI mean, n settled):")
        for r in report["lags"].values():
            cells = ", ".join(f"{k}={v['settlement_roi_mean']} (n={v['n_settled']})" for k, v in r["by_market_type"].items())
            print(f"  {r['lag']:<5} {cells}")
    print(f"\nkill rule: {report['kill_rule']['status']} — {report['kill_rule']['reason']}")


async def run(
    *,
    use_network: bool,
    harvest: bool,
    harvest_dir: Path,
    tape_path: Path | None,
    artifact_dir: Path,
    params: CopyParameters,
    model_fees: bool,
    persist_ledgers: bool,
    min_copies: int,
    min_markets: int,
    resamples: int,
    seed: int,
    alpha: float,
    harvest_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_paper_only("measure_tennis_whale")
    mode = "network" if use_network else "fixtures"
    measured_at = _now()
    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-tennis-{uuid.uuid4().hex[:8]}"
    tape = await load_or_harvest_tape(
        use_network=use_network, harvest=harvest, harvest_dir=harvest_dir, tape_path=tape_path, harvest_kwargs=harvest_kwargs or {},
    )
    result = await replay_copy_tracks(tape, params=params, model_fees=model_fees, cycle_label=f"tennis_whale:{mode}:{run_id}")
    report = build_report(
        tape, result, mode=mode, run_id=run_id, measured_at=measured_at,
        alpha=alpha, min_copies=min_copies, min_markets=min_markets, resamples=resamples, seed=seed,
    )
    report = to_jsonable(report)
    persist_tennis_run(
        artifact_dir=artifact_dir, report=report, result=result, summaries=result.summaries,
        ledgers=result.ledgers if persist_ledgers else {}, mode=mode, measured_at=measured_at,
        run_id=run_id, markets=len(tape.markets), lags=params.lags,
    )
    _print_report(report)
    print(f"\nartifacts: {artifact_dir / REPORT_FILE}  {artifact_dir / SCOREBOARD_FILE}  {artifact_dir / 'paper'}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", action="store_true", help="use the harvested public tape instead of the synthetic fixture")
    parser.add_argument("--harvest", action="store_true", help="fetch the tennis tape from public Gamma + Data API now (network only)")
    parser.add_argument("--harvest-dir", type=Path, default=Path("data/harvests"), help=f"directory holding {HARVEST_FILE}")
    parser.add_argument("--tape", type=Path, default=None, help="explicit tape JSON to replay (overrides fixture/harvest)")
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts"))
    harvest = parser.add_argument_group("harvest")
    harvest.add_argument("--resolved-markets", type=int, default=150)
    harvest.add_argument("--open-markets", type=int, default=40)
    harvest.add_argument("--min-volume", type=Decimal, default=Decimal("2000"), help="skip resolved markets below this USDC volume")
    harvest.add_argument("--max-trades-per-market", type=int, default=4000)
    harvest.add_argument("--no-kalshi-check", action="store_true")
    rule = parser.add_argument_group("pre-registered copy rule")
    rule.add_argument("--lags", type=int, nargs="+", default=[30, 120, 600], help="copy lags in seconds")
    rule.add_argument("--large-fill-notional", type=Decimal, default=Decimal("500"))
    rule.add_argument("--min-large-fills", type=int, default=3)
    rule.add_argument("--min-whale-markets", type=int, default=2)
    rule.add_argument("--signal-min-notional", type=Decimal, default=Decimal("200"))
    rule.add_argument("--max-two-sided-share", type=Decimal, default=Decimal("0.5"))
    rule.add_argument("--stake", type=Decimal, default=Decimal("10"), help="USDC per copy")
    rule.add_argument("--max-wait-seconds", type=int, default=1800)
    rule.add_argument("--slippage-ticks", type=int, default=1)
    rule.add_argument("--cooldown-seconds", type=int, default=600)
    stats = parser.add_argument_group("inference")
    stats.add_argument("--min-copies", type=int, default=30, help="copies with outcomes required per lag for a verdict")
    stats.add_argument("--min-markets", type=int, default=10, help="distinct markets with outcomes required per lag for a verdict")
    stats.add_argument("--resamples", type=int, default=2000)
    stats.add_argument("--seed", type=int, default=20260914)
    stats.add_argument("--alpha", type=float, default=0.05, help="family-wise alpha; Bonferroni-split across lags")
    parser.add_argument("--no-persist", action="store_true", help="do not write ledger files")
    parser.add_argument("--no-fees", action="store_true", help="disable the Polymarket taker fee model")
    args = parser.parse_args()
    require_paper_only("measure_tennis_whale")
    params = CopyParameters(
        lags=tuple(args.lags),
        large_fill_notional=args.large_fill_notional,
        min_large_fills=args.min_large_fills,
        min_markets=args.min_whale_markets,
        signal_min_notional=args.signal_min_notional,
        max_two_sided_share=args.max_two_sided_share,
        stake_per_copy=args.stake,
        max_wait_seconds=args.max_wait_seconds,
        slippage_ticks=args.slippage_ticks,
        cooldown_seconds=args.cooldown_seconds,
    )
    asyncio.run(
        run(
            use_network=args.network,
            harvest=args.harvest,
            harvest_dir=args.harvest_dir,
            tape_path=args.tape,
            artifact_dir=args.artifact_dir,
            params=params,
            model_fees=not args.no_fees,
            persist_ledgers=not args.no_persist,
            min_copies=args.min_copies,
            min_markets=args.min_markets,
            resamples=max(100, args.resamples),
            seed=args.seed,
            alpha=args.alpha,
            harvest_kwargs={
                "resolved_markets": max(0, args.resolved_markets),
                "open_markets": max(0, args.open_markets),
                "min_volume": args.min_volume,
                "max_trades_per_market": max(1, args.max_trades_per_market),
                "check_kalshi": not args.no_kalshi_check,
            },
        )
    )


if __name__ == "__main__":
    main()
