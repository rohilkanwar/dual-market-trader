"""Run only the Polymarket intra-venue arbitrage paper tracks.

Tracks: ``polymarket_rebalancing_arb`` (binary YES+NO merge/split),
``polymarket_negrisk_arb`` (buy-all-NO + NegRiskAdapter conversion) and
``polymarket_combinatorial_arb`` (buy-all-YES held to resolution).

Outputs (relative to ``--artifact-dir``, default ``artifacts/``):

* ``scoreboard_polymarket_arb.json``      dashboard artifact (schema 1.2.0, source=measured,
                                         track_family=polymarket_arb); ``scoreboard_latest.json``
                                         only with ``--publish-latest``
* ``polymarket_arb_latest.json``          full opportunity report (every group, every binary
                                         market, mirror statistics, conversions, holdings)
* ``paper/ledger_<track>.json``           ledger per track; ``paper/runs/<run_id>.json`` manifest

Read-only against the public Gamma ``/events`` and CLOB ``POST /books``
endpoints; no credentials are read. Paper-only: refuses live flags.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.measure_all import (
    add_arb_arguments,
    arb_parameters_from_args,
    load_ledgers,
    persist_run,
    to_jsonable,
    write_json,
)
from core.config import require_paper_only
from research.polymarket_arb_tracks import NEGRISK, measure_polymarket_arb_with_ledgers
from research.scoreboard import POLYMARKET_ARB_TRACKS, TrackSummary, VenueSnapshot
from strategies.polymarket_arb import ArbParameters

SCOREBOARD_NAME = "scoreboard_polymarket_arb.json"
REPORT_NAME = "polymarket_arb_latest.json"


def opportunity_report(
    summaries: list[TrackSummary],
    snapshot: VenueSnapshot,
    *,
    mode: str,
    measured_at: str,
    run_id: str,
    parameters: ArbParameters | None,
) -> dict[str, Any]:
    by_track = {s.track: s for s in summaries}
    reb = by_track["polymarket_rebalancing_arb"].metrics
    neg = by_track[NEGRISK].metrics
    comb = by_track["polymarket_combinatorial_arb"].metrics
    return to_jsonable(
        {
            "schema_version": "1.0.0",
            "paper_only": True,
            "source": "measured",
            "run_id": run_id,
            "mode": mode,
            "measured_at": measured_at,
            "venue": "polymarket",
            "endpoints": [
                "GET gamma-api.polymarket.com/events?active=true&closed=false&order=liquidity",
                "POST clob.polymarket.com/books",
            ],
            "snapshot": {
                "source": snapshot.source,
                "groups": len(snapshot.groups),
                "markets": len(snapshot.markets),
                "errors": snapshot.errors,
                "fetched_at": snapshot.fetched_at,
            },
            "parameters": neg.get("parameters"),
            "rebalancing": {
                "markets_checked": reb.get("markets_checked"),
                "mirror_consistent": reb.get("mirror_consistent"),
                "mirror_inconsistent": reb.get("mirror_inconsistent"),
                "top_of_book_ask_sum": reb.get("top_of_book_ask_sum"),
                "top_of_book_bid_sum": reb.get("top_of_book_bid_sum"),
                "refused_by_reason": by_track["polymarket_rebalancing_arb"].refused_by_reason,
                "opportunities": reb.get("opportunities"),
                "executions": reb.get("executions"),
            },
            "negrisk": {
                "groups_checked": neg.get("groups_checked"),
                "convertible_groups": neg.get("convertible_groups"),
                "augmented_groups": neg.get("augmented_groups"),
                "refused_by_reason": by_track[NEGRISK].refused_by_reason,
                "groups": neg.get("groups"),
                "conversions": neg.get("conversions"),
                "legging_residual_contracts": neg.get("legging_residual_contracts"),
            },
            "combinatorial": {
                "groups_checked": comb.get("groups_checked"),
                "refused_by_reason": by_track["polymarket_combinatorial_arb"].refused_by_reason,
                "groups": comb.get("groups"),
                "holdings": comb.get("holdings"),
                "locked_capital": comb.get("locked_capital"),
                "payoff_at_resolution": comb.get("payoff_at_resolution"),
                "lockup_until": comb.get("lockup_until"),
            },
            "ledgers": {s.track: s.ledger for s in summaries},
        }
    )


def _print(summaries: list[TrackSummary], snapshot: VenueSnapshot, mode: str) -> None:
    print(f"\npolymarket arbitrage paper tracks ({mode}: {len(snapshot.groups)} events, {len(snapshot.markets)} markets)")
    header = f"{'track':<32}{'cand':>6}{'adm':>6}{'ord':>6}{'fill':>6}{'real':>10}{'unreal':>10}{'fees':>8}"
    print(header)
    print("-" * len(header))
    for s in summaries:
        ledger = s.ledger
        print(
            f"{s.track:<32}{s.candidates:>6}{s.admitted:>6}{s.proposed_orders:>6}{s.paper_fills:>6}"
            f"{float(ledger.get('realized_pnl', 0)):>10.4f}{float(ledger.get('unrealized_pnl', 0)):>10.4f}"
            f"{float(ledger.get('fees_paid', 0)):>8.4f}"
        )
        if s.refused_by_reason:
            print(f"{'':<32}refused: {', '.join(f'{k}={v}' for k, v in sorted(s.refused_by_reason.items()))}")
    reb = next(s for s in summaries if s.track == "polymarket_rebalancing_arb").metrics
    print(
        f"\nmirror check: {reb.get('mirror_consistent', 0)} consistent / {reb.get('mirror_inconsistent', 0)} inconsistent; "
        f"top-of-book YES_ask+NO_ask min={reb.get('top_of_book_ask_sum', {}).get('min')} "
        f"median={reb.get('top_of_book_ask_sum', {}).get('median')}"
    )
    neg = next(s for s in summaries if s.track == NEGRISK).metrics
    for row in (neg.get("groups") or [])[:8]:
        print(
            f"  negrisk {row['legs']:>3} legs  yes_bid_sum={row['yes_bid_sum']}  no_ask_sum={row['no_ask_sum']}  "
            f"gross/set={row['gross_edge_per_set']}  {row['reason']}  {str(row['title'])[:48]}"
        )
    if snapshot.errors:
        print(f"\nsnapshot errors ({len(snapshot.errors)}): {snapshot.errors[:3]}")


async def run(
    *,
    use_network: bool,
    limit: int,
    artifact_dir: Path,
    persist_ledgers: bool,
    reset_ledgers: bool,
    model_fees: bool,
    publish_latest: bool,
    parameters: ArbParameters | None,
) -> dict[str, Any]:
    require_paper_only("measure_polymarket_arb")
    mode = "network" if use_network else "fixtures"
    ledgers = {} if reset_ledgers or not persist_ledgers else load_ledgers(artifact_dir, POLYMARKET_ARB_TRACKS)
    measured_at = datetime.now(UTC).isoformat()
    summaries, ledgers_by_track, snapshot = await measure_polymarket_arb_with_ledgers(
        use_fixtures=not use_network,
        limit=limit,
        ledgers=ledgers,
        model_fees=model_fees,
        parameters=parameters,
    )
    artifact = persist_run(
        summaries,
        ledgers_by_track if persist_ledgers else {},
        artifact_dir=artifact_dir,
        mode=mode,
        measured_at=measured_at,
        limit=limit,
        kalshi_env=None,
        scoreboard_name=SCOREBOARD_NAME,
        write_latest=publish_latest,
        artifact_kwargs={
            "primary_track": NEGRISK,
            "venues": ("polymarket",),
            "venue_focus": "polymarket",
            "label_suffix": "POLYMARKET ARB",
            "track_family": "polymarket_arb",
        },
    )
    report = opportunity_report(
        summaries, snapshot, mode=mode, measured_at=measured_at,
        run_id=artifact["meta"]["run_id"], parameters=parameters,
    )
    write_json(artifact_dir / REPORT_NAME, report)
    _print(summaries, snapshot, mode)
    print(
        f"\nledger totals: realized={artifact['totals']['realized_pnl']} unrealized={artifact['totals']['unrealized_pnl']} "
        f"fees={artifact['totals']['fees_paid']} paper_pnl={artifact['totals']['paper_pnl']}"
    )
    print(f"artifacts: {artifact_dir / SCOREBOARD_NAME}  {artifact_dir / REPORT_NAME}  {artifact_dir / 'paper'}")
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", action="store_true", help="read public Gamma/CLOB books (paper fills stay local)")
    parser.add_argument("--limit", type=int, default=25, help="events (top by liquidity)")
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--no-persist", action="store_true", help="do not carry ledgers across runs")
    parser.add_argument("--reset-ledgers", action="store_true", help="start every track from a fresh ledger")
    parser.add_argument("--no-fees", action="store_true", help="disable the Polymarket taker-fee model")
    parser.add_argument("--publish-latest", action="store_true", help="also overwrite scoreboard_latest.json")
    add_arb_arguments(parser)
    args = parser.parse_args()
    require_paper_only("measure_polymarket_arb")
    asyncio.run(
        run(
            use_network=args.network,
            limit=max(1, args.limit),
            artifact_dir=args.artifact_dir,
            persist_ledgers=not args.no_persist,
            reset_ledgers=args.reset_ledgers,
            model_fees=not args.no_fees,
            publish_latest=args.publish_latest,
            parameters=arb_parameters_from_args(args),
        )
    )


if __name__ == "__main__":
    main()
