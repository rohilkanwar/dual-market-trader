"""Fade-the-tourist paper measurement (Kalshi tennis; crypto 15-minute windows optional).

Runs the ``fade_the_tourist`` live paper track against the open books and
recent public prints of the chosen universe, settles positions carried from
earlier runs once the venue publishes a result, and — from a settled-trade
harvest — replays every tape to measure the fade's settled EV after fees plus
the adverse-selection markouts. Outputs (relative to ``--artifact-dir``):

* ``tourist_fade_report_latest.json``   verdicts, replay tables, markouts, assumptions (headline document)
* ``scoreboard_tourist_fade.json``      scoreboard artifact for the live track (schema 1.3.0, measured)
* ``paper/ledger_fade_the_tourist.json`` ledger (carried across runs unless ``--no-persist``)
* ``paper/equity_curve_fade_the_tourist.jsonl``
* ``paper/runs/<run_id>.json``          run record picked up by the dashboard experiments index

Paper-only: refuses to start if the environment requests live trading. Reads
only unauthenticated Kalshi endpoints; no keys are needed or used.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from apps.measure_all import append_jsonl, equity_curve_path, json_default, ledger_path, load_ledgers, to_jsonable, write_json
from core.config import require_paper_only
from core.ledger import PaperLedger
from core.types import Market, Outcome, Venue
from research.flb import VERDICT_INSUFFICIENT, KalshiFeeModel
from research.flb_expost import harvest_settled_trades, load_settled_trades
from research.scoreboard import TrackSummary, VenueSnapshot, _venue_pnl, capture_snapshot
from research.scoreboard_artifact import build_scoreboard_artifact
from research.tourist_fade import (
    HARVEST_FILE,
    MARKETS_FIXTURE_PATH,
    SETTLED_FIXTURE_PATH,
    TOURIST_FAMILY,
    TOURIST_TRACK,
    UNIVERSES,
    create_tourist_runtime,
    expost_report,
    fetch_market_result,
    fetch_recent_tape,
    finalize_tourist_metrics,
    not_measured_report,
    run_fade_the_tourist_track,
    settle_resolved_positions,
)
from strategies.tourist_flow import TapeTrade, TouristParameters
from venues.kalshi.client import HOSTS, KalshiClient

REPORT_SCHEMA = "1.0.0"
REPORT_KIND = "fade_the_tourist_report"
FADE_ROWS_FILE = "tourist_fade_rows_latest.json"
FADE_ROWS_IN_REPORT = 100
HYPOTHESIS = (
    "Recreational-looking taker flow (small tickets, longshot buys, late chases) is uninformed; when it clusters on one "
    "side of a Kalshi tennis match (or a 15-minute crypto window) the other side is cheap, so paper-fading it earns a "
    "positive settled return after taker fees, more so when the favourite bought is already priced >= 70c."
)
ADVERSE_SELECTION_FAIL_RISK = (
    "Flow that looks recreational can be informed. On in-play tennis the fastest small-clip takers are frequently "
    "court-siders with a faster score feed than the public book; on 15-minute crypto windows the small chase into a "
    "moving side often reflects a spot move the book has not yet absorbed. In both cases the classifier labels informed "
    "flow as tourist and the fade becomes its systematic counterparty, losing the full move plus spread and fee. "
    "The report measures this with markouts (the tourist's bought side +5/+20 prints later and at settlement): "
    "positive markouts mean adverse selection, and the tourist_flow_loses verdict FAILs."
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def capture_universe(*, use_network: bool, limit: int, kalshi_env: str | None, series: tuple[str, ...]) -> tuple[VenueSnapshot, str | None]:
    client = KalshiClient(paper=True, use_fixtures=not use_network, environment=kalshi_env, series_tickers=series if use_network else None, fixture_path=MARKETS_FIXTURE_PATH)
    try:
        snapshot = await capture_snapshot(client, limit=limit, source="network" if use_network else "fixture")
        return snapshot, client.base_url if use_network else None
    finally:
        await client.close()


async def fetch_tapes(snapshot: VenueSnapshot, *, base_url: str, tape_limit: int, http: httpx.AsyncClient | None = None, concurrency: int = 4) -> dict[str, list[TapeTrade]]:
    owns = http is None
    client = http or httpx.AsyncClient(timeout=30.0)
    gate = asyncio.Semaphore(max(1, concurrency))
    tapes: dict[str, list[TapeTrade]] = {}

    async def one(market: Market) -> None:
        async with gate:
            try:
                tapes[market.market_id] = await fetch_recent_tape(client, base_url, market.market_id, limit=tape_limit, max_pages=max(1, -(-tape_limit // 1000)))
            except httpx.HTTPError as exc:
                snapshot.errors.append(f"trades[{market.market_id}]: {type(exc).__name__}: {exc}")
                tapes[market.market_id] = []

    try:
        await asyncio.gather(*(one(m) for m in snapshot.markets))
    finally:
        if owns:
            await client.aclose()
    return tapes


async def settle_carried(ledger: PaperLedger, snapshot: VenueSnapshot, *, base_url: str | None, http: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
    """Settle positions whose market now has a result.

    Fixtures: an inactive market carrying ``paper_settlement_outcome``. Network:
    ``GET /markets/{ticker}`` for every open position (markets still open return
    no result and stay carried).
    """
    fixture_results: dict[str, Outcome] = {}
    for market in snapshot.markets:
        raw = market.metadata.get("paper_settlement_outcome")
        if not market.active and raw in ("yes", "no"):
            fixture_results[market.market_id] = Outcome(raw)
    if base_url is None:
        return settle_resolved_positions(ledger, fixture_results.get)
    owns = http is None
    client = http or httpx.AsyncClient(timeout=30.0)
    results: dict[str, Outcome | None] = {}
    try:
        for position in ledger.open_positions:
            try:
                results[position.market_id] = await fetch_market_result(client, base_url, position.market_id)
            except httpx.HTTPError as exc:
                snapshot.errors.append(f"result[{position.market_id}]: {type(exc).__name__}: {exc}")
                results[position.market_id] = None
    finally:
        if owns:
            await client.aclose()
    return settle_resolved_positions(ledger, results.get)


def _verdict_table(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, section in report["ex_post"]["verdicts"].items():
        rows.append({"scope": "ex_post", "check": key, "verdict": section["verdict"], "detail": section.get("reason")})
    return rows


def _headline(report: dict[str, Any]) -> str:
    v = report["ex_post"]["verdicts"]
    live = report["live_track"]
    adverse = report["ex_post"].get("adverse_selection", {}).get("detected")
    adverse_text = "adverse selection DETECTED" if adverse else ("adverse selection not detected" if adverse is False else "adverse selection not measured")
    return (
        f"Fade EV after fees: {v['fade_ev_positive_after_fees']['verdict']} (all regimes) / "
        f"{v['strong_regime_fade_ev_positive']['verdict']} (favourite >= 70c) / "
        f"{v['fade_ev_positive_excluding_final_minutes']['verdict']} (excluding final minutes); strong beats weak {v['strong_regime_beats_weak']['verdict']}; "
        f"tourist flow loses {v['tourist_flow_loses']['verdict']} ({adverse_text}). "
        f"Live track: {live['candidates']} markets, {live['admitted']} clusters faded, {live['paper_fills']} paper fills."
    )


def build_report(*, snapshot: VenueSnapshot, summary: TrackSummary, ex_post: dict[str, Any], params: TouristParameters, mode: str, universe: str, series: tuple[str, ...], kalshi_env: str | None, measured_at: str, run_id: str, model_fees: bool, settled: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = summary.metrics
    live = {
        "track": summary.track,
        "label": summary.label,
        "candidates": summary.candidates,
        "admitted": summary.admitted,
        "proposed_orders": summary.proposed_orders,
        "paper_fills": summary.paper_fills,
        "refused_by_reason": dict(sorted(summary.refused_by_reason.items())),
        "tape": metrics.get("tape", {}),
        "by_regime": metrics.get("by_regime", {}),
        "paper_pnl_by_regime": metrics.get("paper_pnl_by_regime", {}),
        "settled_this_run": settled,
        "ledger": {k: summary.ledger.get(k) for k in ("starting_cash", "cash", "equity", "realized_pnl", "unrealized_pnl", "total_pnl", "fees_paid", "gross_notional", "max_drawdown", "open_positions", "unmarked_positions", "fills", "settlement_fills", "mark_method")},
        "fills": summary.fills,
        "edges": summary.edges,
        "notes": summary.notes,
    }
    report = {
        "schema_version": REPORT_SCHEMA,
        "kind": REPORT_KIND,
        "paper_only": True,
        "run_id": run_id,
        "mode": mode,
        "universe": universe,
        "series": list(series),
        "kalshi_env": kalshi_env,
        "measured_at": measured_at,
        "generated_at": _now(),
        "pnl_source": "core.ledger.PaperLedger",
        "hypothesis": HYPOTHESIS,
        "pass_criterion": "fade_ev_positive_after_fees = PASS: settled net return per contract > 0 after taker fees with event-clustered t >= 2 on >= 10 events and >= 20 fades",
        "adverse_selection_fail_risk": ADVERSE_SELECTION_FAIL_RISK,
        "assumptions": {
            "classifier": f"A print is tourist when >= {params.min_flags} of: taker notional <= ${params.small_ticket_notional} (small ticket); side bought priced < {params.longshot_threshold} (longshot buy); side bought up >= {params.chase_move} vs {params.chase_lookback_trades} prints earlier and the print sits in the last {1 - params.late_fraction:.0%} of the market's open-to-close life (late chase). Rules, not a fitted model.",
            "cluster": f">= {params.cluster_min_trades} tourist prints carrying >= ${params.cluster_min_notional} on one side within {params.cluster_window_seconds}s; {params.cooldown_seconds}s cooldown per side.",
            "fade": f"Buy the other side at the touch (taker). ${params.base_notional} per fade, x{params.strong_multiplier} when that side is >= {params.strong_favorite_price} (strong regime); ${params.max_market_notional}/market and ${params.max_total_cash_at_risk} total collateral; RiskManager caps $25/order, 75 contracts/market, $75 daily loss. Whole contracts.",
            "replay_fill": f"The tape has no book, so a replayed fade pays 1 - (last tourist print) + {params.assumed_spread_ticks} ticks. Real spreads on in-play tennis are often wider; this is a documented guess, not a measurement.",
            "fees": "Kalshi taker fee round_up(M*0.07*C*P*(1-P)) per fade (M = series fee_multiplier). Maker fees do not apply: the fade takes.",
            "settlement": "Replayed fades settle at the published result; live-track positions are carried and settled on a later run when GET /markets/{ticker} reports yes/no. Open positions are marked at mid.",
            "inference": "Events (matches / windows) are the cluster unit: every print in a market shares one outcome. Means are contract-weighted with an event-clustered SE; equal-weighted means are reported alongside.",
            "tape_limits": "/markets/trades pages newest-first with a per-market cap (trades_truncated reported), so replays over-represent end-game prints. Opening and closing trades are indistinguishable. Small tickets on the public tape may be slices of a larger order.",
            "daily_loss_in_replay": "The $75 daily-loss rail resets per market in the replay because the sample spans many days; per-order and per-market caps apply on every fade.",
        },
        "headline": "",
        "verdict_table": [],
        "live_track": live,
        "snapshot": {"source": snapshot.source, "fetched_at": snapshot.fetched_at, "markets": len(snapshot.markets), "errors": snapshot.errors},
        "ex_post": ex_post,
        "parameters": params.as_dict(),
        "fee_model": {"taker_rate": (KalshiFeeModel() if model_fees else KalshiFeeModel.zero()).taker_rate},
    }
    report["headline"] = _headline(report)
    report["verdict_table"] = _verdict_table(report)
    return report


def persist_run(*, artifact_dir: Path, report: dict[str, Any], summary: TrackSummary, ledger: PaperLedger | None, mode: str, kalshi_env: str | None, limit: int, measured_at: str, run_id: str) -> dict[str, Any]:
    if ledger is not None:
        ledger.save(ledger_path(artifact_dir, TOURIST_TRACK))
        if ledger.equity_curve:
            append_jsonl(equity_curve_path(artifact_dir, TOURIST_TRACK), {"run_id": run_id, "track": TOURIST_TRACK, "mode": mode, **asdict(ledger.equity_curve[-1])})
    findings = {
        "fade_the_tourist": {
            "headline": report["headline"],
            "verdicts": {row["check"]: row["verdict"] for row in report["verdict_table"]},
            "adverse_selection_detected": report["ex_post"].get("adverse_selection", {}).get("detected"),
            "universe": report["universe"],
            "report": "tourist_fade_report_latest.json",
        },
        "divergence_findings_status": "not_applicable_tourist_fade_run",
        "arbai_summary": report["headline"],
    }
    artifact = build_scoreboard_artifact(
        [summary], mode=mode, measured_at=measured_at, limit=limit, kalshi_env=kalshi_env,
        primary_track=TOURIST_TRACK, findings=findings, venues=("kalshi",), label_suffix="FADE THE TOURIST", track_family=TOURIST_FAMILY,
    )
    artifact["meta"]["run_id"] = run_id
    artifact["meta"]["kind"] = "fade_the_tourist"
    artifact["meta"]["refresh"] = "python -m apps.measure_tourist_fade [--network --kalshi-env prod --harvest-trades] && cd dashboard && npm run sync-artifacts"
    write_json(artifact_dir / "scoreboard_tourist_fade.json", artifact)
    # Every replayed fade goes to a sidecar (git-ignored artifacts/); the report keeps a capped sample.
    rows = report["ex_post"].get("fade_rows")
    if isinstance(rows, list):
        write_json(artifact_dir / FADE_ROWS_FILE, {"run_id": run_id, "paper_only": True, "fades": len(rows), "rows": rows})
        report["ex_post"]["fade_rows_total"] = len(rows)
        report["ex_post"]["fade_rows_file"] = FADE_ROWS_FILE
        report["ex_post"]["fade_rows"] = rows[:FADE_ROWS_IN_REPORT]
    write_json(artifact_dir / "tourist_fade_report_latest.json", report)
    write_json(
        artifact_dir / "paper" / "runs" / f"{run_id}.json",
        {
            "run_id": run_id,
            "paper_only": True,
            "kind": "fade_the_tourist",
            "mode": mode,
            "measured_at": measured_at,
            "venues": ["kalshi"],
            "venue_focus": "kalshi",
            "kalshi_env": kalshi_env,
            "primary_track": TOURIST_TRACK,
            "track_family": TOURIST_FAMILY,
            "label": artifact["meta"]["label"],
            "fade_the_tourist": findings["fade_the_tourist"],
            "tracks": [{**summary.as_dict(), "family": TOURIST_FAMILY}],
        },
    )
    return artifact


def _print_report(report: dict[str, Any]) -> None:
    print(f"\nfade-the-tourist paper measurement ({report['mode']}, universe {report['universe']}, run {report['run_id']})")
    print(report["headline"])
    print(f"\n{'scope':<9}{'check':<40}{'verdict':<20}detail")
    print("-" * 120)
    for row in report["verdict_table"]:
        print(f"{row['scope']:<9}{row['check']:<40}{row['verdict']:<20}{(row['detail'] or '')[:70]}")
    live = report["live_track"]
    print(f"\nlive track: {live['candidates']} markets, tape {live['tape'].get('trades', 0)} prints / {live['tape'].get('tourist_trades', 0)} tourist / {live['tape'].get('clusters', 0)} clusters; admitted {live['admitted']}, fills {live['paper_fills']}, refusals {live['refused_by_reason']}")
    ledger = live["ledger"]
    print(f"ledger: realized {float(ledger.get('realized_pnl') or 0):.4f} unrealized {float(ledger.get('unrealized_pnl') or 0):.4f} fees {float(ledger.get('fees_paid') or 0):.4f} open {ledger.get('open_positions')} settled_this_run {len(live['settled_this_run'])}")
    ex_post = report["ex_post"]
    if ex_post.get("status") == "measured_from_settled_trades":
        tape = ex_post["tape"]
        print(f"\nex-post replay: {ex_post['markets']} markets / {ex_post['events']} events, {tape['trades']} prints, tourist share {tape['tourist_share_of_trades']} of prints / {tape['tourist_share_of_notional']} of notional; {ex_post['clusters']} clusters -> {ex_post['fades']['fades']} fades")
        print(f"{'regime':<10}{'fades':>6}{'events':>7}{'win%':>7}{'net/ct':>9}{'t(cw)':>7}{'t(eq)':>7}{'ROS':>8}")
        for name, row in [("all", ex_post["fades"])] + sorted(ex_post["by_regime"].items()):
            if not row.get("fades"):
                continue
            t = row["t_stat"] if row["t_stat"] is not None else float("nan")
            te = row["t_stat_equal_weight"] if row["t_stat_equal_weight"] is not None else float("nan")
            print(f"{name:<10}{row['fades']:>6}{row['n_events']:>7}{(row['win_rate'] or 0) * 100:>6.0f}%{(row['mean'] or 0):>9.4f}{t:>7.2f}{te:>7.2f}{float(row['return_on_stake'] or 0):>8.3f}")
        print("\nmarkouts of the side the taker bought (contract-weighted, event-clustered t):")
        for group in ("tourist", "other"):
            cells = ex_post["markouts"].get(group, {})
            print(f"  {group:<8}" + "  ".join(f"{h}: {c['mean']:+.4f} (t={c['t_stat']})" for h, c in cells.items() if c.get("mean") is not None))
        for name, cat in ex_post.get("adverse_selection", {}).get("by_category", {}).items():
            t_cell = cat.get("tourist", {}).get("settlement", {})
            print(f"  {name:<8}tourist settlement markout {t_cell.get('mean')} (t={t_cell.get('t_stat')}, events={t_cell.get('n_events')})")
    else:
        print(f"\nex-post: {ex_post.get('status')} - {ex_post.get('reason', '')}")


async def run(
    *,
    use_network: bool,
    universe: str,
    series: tuple[str, ...] | None,
    limit: int,
    tape_limit: int,
    artifact_dir: Path,
    harvest_dir: Path,
    harvest_trades: bool,
    settled_per_series: int,
    max_trades_per_market: int,
    kalshi_env: str | None,
    persist_ledgers: bool,
    reset_ledgers: bool,
    model_fees: bool,
    params: TouristParameters,
    min_events: int,
    min_fades: int,
    exclude_final_minutes: int = 30,
    starting_cash: Decimal = Decimal("1000"),
) -> dict[str, Any]:
    require_paper_only("measure_tourist_fade")
    mode = "network" if use_network else "fixtures"
    measured_at = _now()
    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-tourist-{uuid.uuid4().hex[:8]}"
    series = series or UNIVERSES[universe]
    snapshot, base_url = await capture_universe(use_network=use_network, limit=limit, kalshi_env=kalshi_env, series=series)
    tapes = await fetch_tapes(snapshot, base_url=base_url, tape_limit=tape_limit) if base_url else {}
    ledgers = {} if reset_ledgers or not persist_ledgers else load_ledgers(artifact_dir, (TOURIST_TRACK,))
    ledger = ledgers.get(TOURIST_TRACK)
    settled: list[dict[str, Any]] = []
    if ledger is not None and ledger.open_positions:
        settled = await settle_carried(ledger, snapshot, base_url=base_url)
    runtime = create_tourist_runtime({Venue.KALSHI: snapshot}, ledger=ledger, starting_cash=starting_cash, model_fees=model_fees)
    await run_fade_the_tourist_track(runtime, params=params, tapes=tapes, settled=settled)
    runtime.finalize(label=f"tourist:{mode}:{run_id}")
    finalize_tourist_metrics(runtime)
    runtime.summary.metrics["venue_pnl"] = _venue_pnl(runtime.ledger)
    runtime.summary.metrics["snapshot"] = {"kalshi": {"source": snapshot.source, "markets": len(snapshot.markets), "errors": snapshot.errors}}
    runtime.summary.notes = (
        "Taker fade of clustered tourist prints: when >= 5 small-ticket / longshot / late-chase prints carrying >= $50 pile "
        "into one side within 10 minutes, buy the other side at the touch ($10, x2 when it is >= 70c). Positions are marked "
        "at mid, carried across runs and settled when the venue publishes a result. Paper caps $25/order, $75/market, $75 daily."
    )

    fee_model = KalshiFeeModel() if model_fees else KalshiFeeModel.zero()
    if use_network:
        target = harvest_dir / HARVEST_FILE
        if harvest_trades:
            payload = await harvest_settled_trades(kalshi_env=kalshi_env or "prod", series=series, settled_per_series=settled_per_series, max_trades_per_market=max_trades_per_market)
            write_json(target, payload)
        if target.exists():
            markets, meta = load_settled_trades(target)
            ex_post = {**expost_report(markets, params, fee_model=fee_model, min_events=min_events, min_fades=min_fades, exclude_final_minutes=exclude_final_minutes), "source": str(target), "harvest_meta": meta}
        else:
            ex_post = not_measured_report(f"no settled-trade harvest at {target}; run with --harvest-trades")
    else:
        markets, meta = load_settled_trades(SETTLED_FIXTURE_PATH)
        ex_post = {**expost_report(markets, params, fee_model=fee_model, min_events=min_events, min_fades=min_fades, exclude_final_minutes=exclude_final_minutes), "source": str(SETTLED_FIXTURE_PATH), "harvest_meta": meta, "note": "Synthetic fixture: exercises the pipeline, not evidence about Kalshi."}

    report = to_jsonable(build_report(
        snapshot=snapshot, summary=runtime.summary, ex_post=ex_post, params=params, mode=mode, universe=universe, series=series,
        kalshi_env=kalshi_env, measured_at=measured_at, run_id=run_id, model_fees=model_fees, settled=settled,
    ))
    persist_run(artifact_dir=artifact_dir, report=report, summary=runtime.summary, ledger=runtime.ledger if persist_ledgers else None, mode=mode, kalshi_env=kalshi_env, limit=limit, measured_at=measured_at, run_id=run_id)
    _print_report(report)
    print(f"\nartifacts: {artifact_dir / 'tourist_fade_report_latest.json'}  {artifact_dir / 'scoreboard_tourist_fade.json'}  {artifact_dir / 'paper'}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", action="store_true", help="read public Kalshi books and prints (paper fills stay local)")
    parser.add_argument("--kalshi-env", choices=tuple(HOSTS), default=None, help="Kalshi public API host (default: KALSHI_ENV or demo)")
    parser.add_argument("--universe", choices=tuple(UNIVERSES), default="tennis", help="series preset: tennis (default), crypto (15-minute BTC/ETH), both")
    parser.add_argument("--series", nargs="*", default=None, help="explicit series list overriding --universe")
    parser.add_argument("--limit", type=int, default=60, help="max open Kalshi markets in the live snapshot")
    parser.add_argument("--tape-limit", type=int, default=1000, help="recent prints fetched per open market")
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--harvest-dir", type=Path, default=Path("data/harvests"), help=f"directory holding {HARVEST_FILE}")
    parser.add_argument("--harvest-trades", action="store_true", help="fetch settled markets + public prints now (network only)")
    parser.add_argument("--settled-per-series", type=int, default=30)
    parser.add_argument("--max-trades-per-market", type=int, default=4000)
    group = parser.add_argument_group("tourist classifier / fade parameters")
    group.add_argument("--small-ticket-notional", type=Decimal, default=Decimal("10"))
    group.add_argument("--longshot-threshold", type=Decimal, default=Decimal("0.30"))
    group.add_argument("--chase-move", type=Decimal, default=Decimal("0.05"))
    group.add_argument("--chase-lookback-trades", type=int, default=20)
    group.add_argument("--late-fraction", type=Decimal, default=Decimal("0.5"))
    group.add_argument("--min-flags", type=int, default=2)
    group.add_argument("--cluster-window-seconds", type=int, default=600)
    group.add_argument("--cluster-min-trades", type=int, default=5)
    group.add_argument("--cluster-min-notional", type=Decimal, default=Decimal("50"))
    group.add_argument("--cooldown-seconds", type=int, default=300)
    group.add_argument("--strong-favorite-price", type=Decimal, default=Decimal("0.70"))
    group.add_argument("--base-notional", type=Decimal, default=Decimal("10"))
    group.add_argument("--strong-multiplier", type=Decimal, default=Decimal("2"))
    group.add_argument("--assumed-spread-ticks", type=int, default=2)
    group.add_argument("--max-total-cash-at-risk", type=Decimal, default=Decimal("1000"))
    parser.add_argument("--min-events", type=int, default=10, help="events required for a verdict")
    parser.add_argument("--min-fades", type=int, default=20, help="fades required for a verdict")
    parser.add_argument("--exclude-final-minutes", type=int, default=30, help="drop replay fades whose cluster fired this close to market close in the *_excluding_final_minutes verdicts")
    parser.add_argument("--no-persist", action="store_true", help="do not carry the ledger across runs")
    parser.add_argument("--reset-ledgers", action="store_true")
    parser.add_argument("--no-fees", action="store_true", help="disable the Kalshi fee model")
    args = parser.parse_args()
    require_paper_only("measure_tourist_fade")
    params = TouristParameters(
        small_ticket_notional=args.small_ticket_notional,
        longshot_threshold=args.longshot_threshold,
        chase_move=args.chase_move,
        chase_lookback_trades=args.chase_lookback_trades,
        late_fraction=args.late_fraction,
        min_flags=args.min_flags,
        cluster_window_seconds=args.cluster_window_seconds,
        cluster_min_trades=args.cluster_min_trades,
        cluster_min_notional=args.cluster_min_notional,
        cooldown_seconds=args.cooldown_seconds,
        strong_favorite_price=args.strong_favorite_price,
        base_notional=args.base_notional,
        strong_multiplier=args.strong_multiplier,
        assumed_spread_ticks=args.assumed_spread_ticks,
        max_total_cash_at_risk=args.max_total_cash_at_risk,
    )
    asyncio.run(
        run(
            use_network=args.network,
            universe=args.universe,
            series=tuple(args.series) if args.series else None,
            limit=max(1, args.limit),
            tape_limit=max(1, args.tape_limit),
            artifact_dir=args.artifact_dir,
            harvest_dir=args.harvest_dir,
            harvest_trades=args.harvest_trades,
            settled_per_series=max(1, args.settled_per_series),
            max_trades_per_market=max(1, args.max_trades_per_market),
            kalshi_env=args.kalshi_env,
            persist_ledgers=not args.no_persist,
            reset_ledgers=args.reset_ledgers,
            model_fees=not args.no_fees,
            params=params,
            min_events=args.min_events,
            min_fades=args.min_fades,
            exclude_final_minutes=max(0, args.exclude_final_minutes),
        )
    )


if __name__ == "__main__":
    main()
