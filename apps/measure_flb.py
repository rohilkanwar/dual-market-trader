"""Kalshi favorite–longshot bias (FLB) paper measurement.

Runs the two FLB paper tracks (``kalshi_longshot_fade``, ``kalshi_maker_quote``)
against one Kalshi snapshot, measures the snapshot's band structure, and — when
settled-trade data is available — the ex-post maker-vs-taker returns by price
band. Outputs (relative to ``--artifact-dir``, default ``artifacts/``):

* ``flb_report_latest.json``        verdicts, band tables, assumptions (the headline document)
* ``scoreboard_flb.json``           scoreboard artifact for the two tracks (schema 1.2.0, measured)
* ``paper/ledger_<track>.json``     ledgers (carried across runs unless ``--no-persist``)
* ``paper/equity_curve_<track>.jsonl``
* ``paper/runs/<run_id>.json``      run record picked up by the dashboard experiments index

Paper-only: refuses to start if the environment requests live trading. Reads
only unauthenticated Kalshi endpoints; demo keys are not needed and not used.
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

from apps.measure_all import (
    append_jsonl,
    equity_curve_path,
    json_default,
    ledger_path,
    load_ledgers,
    to_jsonable,
    write_json,
)
from core.config import require_paper_only
from core.ledger import PaperLedger
from core.types import Venue
from research.flb import FLB_PRIMARY_TRACK, FLB_TRACKS, VERDICT_INSUFFICIENT, KalshiFeeModel, snapshot_verdicts
from research.flb_expost import (
    DEFAULT_EXPOST_SERIES,
    FIXTURE_PATH as EXPOST_FIXTURE_PATH,
    HARVEST_FILE,
    expost_report,
    harvest_settled_trades,
    load_settled_trades,
    not_measured_report,
)
from research.scoreboard import TrackSummary, VenueSnapshot, capture_snapshot, run_flb_tracks
from research.scoreboard_artifact import build_scoreboard_artifact
from strategies.flb import FlbParameters
from venues.kalshi.client import DEFAULT_MACRO_SERIES, KalshiClient

FLB_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "venues" / "kalshi" / "fixtures" / "flb_markets.json"
REPORT_SCHEMA = "1.0.0"
LITERATURE = [
    "Bürgi, Deng, Whelan — Kalshi makers vs takers and the favorite-longshot bias (UCD WP / SSRN 5502658)",
    "Favorite-longshot bias on Polymarket (arXiv 2609.12878): longshots lose; effect weaker in sports",
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def capture_kalshi_snapshot(*, use_network: bool, limit: int, kalshi_env: str | None, series: tuple[str, ...] | None) -> VenueSnapshot:
    client = KalshiClient(
        paper=True,
        use_fixtures=not use_network,
        environment=kalshi_env,
        series_tickers=series if use_network else None,
        fixture_path=FLB_FIXTURE_PATH,
    )
    try:
        return await capture_snapshot(client, limit=limit, source="network" if use_network else "fixture")
    finally:
        await client.close()


def _verdict_table(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, section in report["snapshot"]["verdicts"].items():
        rows.append({"scope": "snapshot", "check": key, "verdict": section["verdict"], "detail": section.get("reason") or section.get("question")})
    for key, section in report["ex_post"]["verdicts"].items():
        rows.append({"scope": "ex_post", "check": key, "verdict": section["verdict"], "detail": section.get("reason")})
    return rows


def _headline(report: dict[str, Any]) -> str:
    snap = report["snapshot"]["verdicts"]["flb_identifiable_from_snapshot"]["verdict"]
    verdicts = report["ex_post"]["verdicts"]
    ex_post = verdicts["ex_post_flb"]["verdict"]
    equal = verdicts.get("ex_post_flb_equal_weighted_markets", {}).get("verdict", VERDICT_INSUFFICIENT)
    slope = verdicts.get("ex_post_favorite_longshot_slope", {}).get("verdict", VERDICT_INSUFFICIENT)
    maker = verdicts["maker_fade_edge_after_fees"]["verdict"]
    return (
        f"Snapshot books: FLB {snap} (prices without outcomes). "
        f"Ex-post from settled trades: longshot tail (<20c) {ex_post} contract-weighted / {equal} equal-weighted by market; "
        f"favorite-longshot slope (below vs above 50c) {slope}; maker fade edge after fees {maker}."
    )


def build_report(
    *,
    snapshot: VenueSnapshot,
    summaries: list[TrackSummary],
    ex_post: dict[str, Any],
    params: FlbParameters,
    mode: str,
    kalshi_env: str | None,
    measured_at: str,
    run_id: str,
    model_fees: bool,
) -> dict[str, Any]:
    fee_model = KalshiFeeModel() if model_fees else KalshiFeeModel.zero()
    snapshot_section = {
        "source": snapshot.source,
        "fetched_at": snapshot.fetched_at,
        "markets": len(snapshot.markets),
        "errors": snapshot.errors,
        "series": sorted({str(m.metadata.get("series_ticker")) for m in snapshot.markets if m.metadata.get("series_ticker")}),
        **snapshot_verdicts(snapshot, fee_model=fee_model, longshot_threshold=params.longshot_threshold),
    }
    verdicts = {k: snapshot_section.pop(k) for k in ("flb_identifiable_from_snapshot", "longshot_take_cost_exceeds_favorite", "event_overround_positive")}
    snapshot_section["verdicts"] = verdicts
    tracks = {}
    for summary in summaries:
        metrics = summary.metrics
        tracks[summary.track] = {
            "label": summary.label,
            "candidates": summary.candidates,
            "longshot_candidates": metrics.get("longshot_candidates", 0),
            "admitted": summary.admitted,
            "proposed_orders": summary.proposed_orders,
            "paper_fills": summary.paper_fills,
            "refused_by_reason": dict(sorted(summary.refused_by_reason.items())),
            "edge_bps": summary.edge_bps,
            "ledger": {k: summary.ledger.get(k) for k in ("starting_cash", "cash", "equity", "realized_pnl", "unrealized_pnl", "total_pnl", "fees_paid", "gross_notional", "max_drawdown", "open_positions", "unmarked_positions", "fills", "mark_method")},
            "longshot_bands": metrics.get("longshot_bands", {}),
            "paper_pnl_by_longshot_band": metrics.get("paper_pnl_by_longshot_band", {}),
            "paper_pnl_by_price_paid_band": metrics.get("paper_pnl_by_price_paid_band", {}),
            "shadow_longshot_buyer": metrics.get("shadow_longshot_buyer"),
            "notes": summary.notes,
        }
    report = {
        "schema_version": REPORT_SCHEMA,
        "kind": "kalshi_flb_report",
        "paper_only": True,
        "run_id": run_id,
        "mode": mode,
        "kalshi_env": kalshi_env,
        "measured_at": measured_at,
        "generated_at": _now(),
        "pnl_source": "core.ledger.PaperLedger",
        "literature": LITERATURE,
        "assumptions": {
            "fee_model": "Kalshi July-2026 schedule: taker round_up(M*0.07*C*P*(1-P)); maker round_up(M*0.0175*C*P*(1-P)) on quadratic_with_maker_fees series; M = series fee_multiplier (1 default, 0.5 on some game series). Rounded to a centicent on paper fills, unrounded per contract in the ex-post table.",
            "fill_probability": f"Maker fills are expected-value fills floor(qty*p): p={params.improve_fill_probability} when improving the touch by one tick, p={params.join_fill_probability}*own/(own+displayed) when joining the touch. Not measured; a live resting order fills fully or not at all.",
            "queue": "Joining the touch assumes pro-rata position behind the displayed size; improving assumes first in queue at the new price. Book depth beyond the touch is ignored for resting orders.",
            "adverse_selection": f"{params.adverse_selection_haircut} of the half-spread is deducted from the reported expected maker edge (edge_bps). The maker ledger uses conservative marks (exit price) instead of mid, so a resting fill shows no spread-capture until it can be exited.",
            "marks": "Fade track marked at mid; maker track marked conservatively (longs at bid, shorts at ask). Positions without a two-sided book are valued at cost and counted as unmarked.",
            "sizing": f"Whole contracts; ${params.max_order_notional}/order, ${params.max_market_notional} cash-at-risk per market (strategy) and 75 contracts/market + $75 daily loss (RiskManager). Paper caps mirror the caps decided for a later live canary.",
            "longshot_definition": f"A side priced at or below {params.longshot_threshold} at the touch (YES ask, or 1 - YES bid for NO). Bands are by price: <10c, 10-20c, ..., >=90c; the ex-post table buckets by the price the taker paid.",
            "snapshot_limits": "Open books contain no outcomes; FLB cannot be identified from a snapshot. Snapshot checks quantify take cost by band and event overround only.",
            "ex_post_limits": "Trades page newest-first with a per-market cap (trades_truncated); markets are the inference unit (clustered SE); series fee parameters are as of harvest time.",
        },
        "headline": "",
        "verdict_table": [],
        "snapshot": snapshot_section,
        "tracks": tracks,
        "ex_post": ex_post,
    }
    report["headline"] = _headline(report)
    report["verdict_table"] = _verdict_table(report)
    return report


def persist_flb_run(
    *,
    artifact_dir: Path,
    report: dict[str, Any],
    summaries: list[TrackSummary],
    ledgers: dict[str, PaperLedger],
    mode: str,
    kalshi_env: str | None,
    limit: int,
    measured_at: str,
    run_id: str,
) -> dict[str, Any]:
    for track, ledger in ledgers.items():
        ledger.save(ledger_path(artifact_dir, track))
        if ledger.equity_curve:
            append_jsonl(equity_curve_path(artifact_dir, track), {"run_id": run_id, "track": track, "mode": mode, **asdict(ledger.equity_curve[-1])})
    flb_findings = {
        "kalshi_flb": {
            "headline": report["headline"],
            "verdicts": {row["check"]: row["verdict"] for row in report["verdict_table"]},
            "report": "flb_report_latest.json",
        },
        "divergence_findings_status": "not_applicable_flb_run",
        "arbai_summary": report["headline"],
    }
    artifact = build_scoreboard_artifact(
        summaries,
        mode=mode,
        measured_at=measured_at,
        limit=limit,
        kalshi_env=kalshi_env,
        primary_track=FLB_PRIMARY_TRACK,
        findings=flb_findings,
        venues=("kalshi",),
        label_suffix="KALSHI FLB",
    )
    artifact["meta"]["run_id"] = run_id
    artifact["meta"]["kind"] = "kalshi_flb"
    artifact["meta"]["refresh"] = "python -m apps.measure_flb [--network --kalshi-env prod --harvest-trades] && cd dashboard && npm run sync-artifacts"
    write_json(artifact_dir / "scoreboard_flb.json", artifact)
    write_json(artifact_dir / "flb_report_latest.json", report)
    write_json(
        artifact_dir / "paper" / "runs" / f"{run_id}.json",
        {
            "run_id": run_id,
            "paper_only": True,
            "kind": "kalshi_flb",
            "mode": mode,
            "measured_at": measured_at,
            "venues": ["kalshi"],
            "venue_focus": "kalshi",
            "kalshi_env": kalshi_env,
            "primary_track": FLB_PRIMARY_TRACK,
            "label": artifact["meta"]["label"],
            "flb": flb_findings["kalshi_flb"],
            "tracks": [summary.as_dict() for summary in summaries],
        },
    )
    return artifact


def _print_report(report: dict[str, Any]) -> None:
    print(f"\nkalshi FLB paper measurement ({report['mode']}, run {report['run_id']})")
    print(report["headline"])
    print(f"\n{'scope':<9}{'check':<58}{'verdict':<20}detail")
    print("-" * 124)
    for row in report["verdict_table"]:
        print(f"{row['scope']:<9}{row['check']:<58}{row['verdict']:<20}{(row['detail'] or '')[:60]}")
    print("\nsnapshot band table (YES price bands)")
    print(f"{'band':<8}{'mkts':>6}{'2side':>6}{'spread':>8}{'fee%':>8}{'take%':>8}")
    for band, cell in report["snapshot"]["band_table"].items():
        fee = cell["mean_taker_fee_pct_of_price"]
        take = cell["mean_take_cost_pct_of_price"]
        print(f"{band:<8}{cell['markets']:>6}{cell['two_sided']:>6}{float(cell['mean_spread'] or 0):>8.3f}{(float(fee) * 100 if fee is not None else float('nan')):>8.2f}{(float(take) * 100 if take is not None else float('nan')):>8.2f}")
    print("\npaper tracks")
    print(f"{'track':<24}{'cand':>6}{'long':>6}{'adm':>6}{'fill':>6}{'real':>10}{'unreal':>10}{'fees':>8}")
    for name, track in report["tracks"].items():
        ledger = track["ledger"]
        print(f"{name:<24}{track['candidates']:>6}{track['longshot_candidates']:>6}{track['admitted']:>6}{track['paper_fills']:>6}{float(ledger.get('realized_pnl') or 0):>10.4f}{float(ledger.get('unrealized_pnl') or 0):>10.4f}{float(ledger.get('fees_paid') or 0):>8.2f}")
    ex_post = report["ex_post"]
    if ex_post.get("status") == "measured_from_settled_trades":
        print(f"\nex-post band table by taker purchase price ({ex_post['markets']} settled markets, {ex_post['trades']} trades, {ex_post['contracts']:.0f} contracts)")
        print(f"{'band':<8}{'mkts':>6}{'n_eff':>7}{'trades':>8}{'contracts':>12}{'taker/ct':>10}{'taker ROI':>10}{'maker net':>10}{'t(cw)':>7}{'t(eq)':>7}")
        for band, cell in ex_post["band_table"].items():
            t_cw = cell["t_stat_taker_gross"] if cell["t_stat_taker_gross"] is not None else float("nan")
            t_eq = cell["t_stat_equal_weight"] if cell["t_stat_equal_weight"] is not None else float("nan")
            roi = cell["taker_roi"] if cell["taker_roi"] is not None else float("nan")
            print(f"{band:<8}{cell['n_markets']:>6}{cell['effective_n_markets']:>7.1f}{cell['n_trades']:>8}{cell['contracts']:>12.0f}{cell['taker_gross_per_contract']:>10.4f}{roi:>10.3f}{cell['maker_net_per_contract']:>10.4f}{t_cw:>7.2f}{t_eq:>7.2f}")
        for name, cat in ex_post.get("by_category", {}).items():
            ls = cat["longshot"]
            print(f"category {name:<12} markets={cat['markets']:<4} longshot: n={ls.get('n_markets', 0)} n_eff={ls.get('effective_n_markets', 0)} taker/ct={ls.get('taker_gross_per_contract')} ROI={ls.get('taker_roi')} t(cw)={ls.get('t_stat_taker_gross')} t(eq)={ls.get('t_stat_equal_weight')} -> {cat['flb']['verdict']} / eq {cat['flb_equal_weighted_markets']['verdict']} / excl-final {cat['flb_excluding_final_minutes']['verdict']} (eq {cat['flb_excluding_final_minutes_equal_weighted']['verdict']})")
    else:
        print(f"\nex-post: {ex_post.get('status')} — {ex_post.get('reason', '')}")


async def run(
    *,
    use_network: bool,
    limit: int,
    artifact_dir: Path,
    harvest_dir: Path,
    harvest_trades: bool,
    settled_per_series: int,
    max_trades_per_market: int,
    series: tuple[str, ...],
    expost_series: tuple[str, ...],
    kalshi_env: str | None,
    persist_ledgers: bool,
    reset_ledgers: bool,
    model_fees: bool,
    params: FlbParameters,
    min_markets: int,
    min_contracts: float,
    exclude_final_minutes: int,
) -> dict[str, Any]:
    require_paper_only("measure_flb")
    mode = "network" if use_network else "fixtures"
    measured_at = _now()
    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-flb-{uuid.uuid4().hex[:8]}"
    snapshot = await capture_kalshi_snapshot(use_network=use_network, limit=limit, kalshi_env=kalshi_env, series=series)
    ledgers = {} if reset_ledgers or not persist_ledgers else load_ledgers(artifact_dir, FLB_TRACKS)
    summaries, ledgers_by_track = await run_flb_tracks(
        {Venue.KALSHI: snapshot}, ledgers=ledgers, model_fees=model_fees, flb_parameters=params, cycle_label=f"flb:{mode}:{run_id}",
    )

    fee_model = KalshiFeeModel() if model_fees else KalshiFeeModel.zero()
    if use_network:
        target = harvest_dir / HARVEST_FILE
        if harvest_trades:
            payload = await harvest_settled_trades(kalshi_env=kalshi_env or "prod", series=expost_series, settled_per_series=settled_per_series, max_trades_per_market=max_trades_per_market)
            write_json(target, payload)
        if target.exists():
            markets, meta = load_settled_trades(target)
            ex_post = {**expost_report(markets, fee_model=fee_model, min_markets=min_markets, min_contracts=min_contracts, exclude_final_minutes=exclude_final_minutes), "source": str(target), "harvest_meta": meta}
        else:
            ex_post = not_measured_report(f"no settled-trade harvest at {target}; run with --harvest-trades")
    else:
        markets, meta = load_settled_trades(EXPOST_FIXTURE_PATH)
        ex_post = {**expost_report(markets, fee_model=fee_model, min_markets=min_markets, min_contracts=min_contracts, exclude_final_minutes=exclude_final_minutes), "source": str(EXPOST_FIXTURE_PATH), "harvest_meta": meta, "note": "Synthetic fixture: exercises the pipeline, not evidence about Kalshi."}

    report = build_report(
        snapshot=snapshot, summaries=summaries, ex_post=ex_post, params=params, mode=mode,
        kalshi_env=kalshi_env, measured_at=measured_at, run_id=run_id, model_fees=model_fees,
    )
    report = to_jsonable(report)
    persist_flb_run(
        artifact_dir=artifact_dir, report=report, summaries=summaries,
        ledgers=ledgers_by_track if persist_ledgers else {}, mode=mode, kalshi_env=kalshi_env,
        limit=limit, measured_at=measured_at, run_id=run_id,
    )
    _print_report(report)
    print(f"\nartifacts: {artifact_dir / 'flb_report_latest.json'}  {artifact_dir / 'scoreboard_flb.json'}  {artifact_dir / 'paper'}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", action="store_true", help="read public Kalshi books (paper fills stay local)")
    parser.add_argument("--kalshi-env", choices=("demo", "prod"), default=None, help="Kalshi public API host (default: KALSHI_ENV or demo)")
    parser.add_argument("--limit", type=int, default=200, help="max Kalshi markets in the snapshot")
    parser.add_argument("--series", nargs="*", default=list(DEFAULT_MACRO_SERIES), help="Kalshi series for the open-book snapshot")
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--harvest-dir", type=Path, default=Path("data/harvests"), help=f"directory holding {HARVEST_FILE}")
    parser.add_argument("--harvest-trades", action="store_true", help="fetch settled markets + public trades now (network only)")
    parser.add_argument("--expost-series", nargs="*", default=list(DEFAULT_EXPOST_SERIES), help="series to harvest settled trades from")
    parser.add_argument("--settled-per-series", type=int, default=15)
    parser.add_argument("--max-trades-per-market", type=int, default=2000)
    parser.add_argument("--longshot-threshold", type=Decimal, default=Decimal("0.20"))
    parser.add_argument("--join-fill-probability", type=Decimal, default=Decimal("0.25"))
    parser.add_argument("--improve-fill-probability", type=Decimal, default=Decimal("0.50"))
    parser.add_argument("--adverse-selection-haircut", type=Decimal, default=Decimal("0.25"))
    parser.add_argument("--min-markets", type=int, default=10, help="settled markets required in the longshot bands for a verdict")
    parser.add_argument("--min-contracts", type=float, default=1000.0)
    parser.add_argument("--exclude-final-minutes", type=int, default=60)
    parser.add_argument("--no-persist", action="store_true", help="do not carry ledgers across runs")
    parser.add_argument("--reset-ledgers", action="store_true")
    parser.add_argument("--no-fees", action="store_true", help="disable the Kalshi fee model")
    args = parser.parse_args()
    require_paper_only("measure_flb")
    params = FlbParameters(
        longshot_threshold=args.longshot_threshold,
        join_fill_probability=args.join_fill_probability,
        improve_fill_probability=args.improve_fill_probability,
        adverse_selection_haircut=args.adverse_selection_haircut,
    )
    asyncio.run(
        run(
            use_network=args.network,
            limit=max(1, args.limit),
            artifact_dir=args.artifact_dir,
            harvest_dir=args.harvest_dir,
            harvest_trades=args.harvest_trades,
            settled_per_series=max(1, args.settled_per_series),
            max_trades_per_market=max(1, args.max_trades_per_market),
            series=tuple(args.series),
            expost_series=tuple(args.expost_series),
            kalshi_env=args.kalshi_env,
            persist_ledgers=not args.no_persist,
            reset_ledgers=args.reset_ledgers,
            model_fees=not args.no_fees,
            params=params,
            min_markets=args.min_markets,
            min_contracts=args.min_contracts,
            exclude_final_minutes=args.exclude_final_minutes,
        )
    )


if __name__ == "__main__":
    main()
