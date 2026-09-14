"""Runnable measurement for the ``news_underreaction`` paper lane.

Runs only the news track against the fixture (default) or public network
snapshot, prints one row per signal with the underreaction math, and can write
a JSON report. Paper-only; no order ever reaches a venue.

    python -m research.news_underreaction                      # fixtures + synthetic signals
    python -m research.news_underreaction --json /tmp/news.json
    python -m research.news_underreaction --network --news-signals data/news/signals.json
    python -m research.news_underreaction --network --news-rss https://example.org/feed.xml

Without a source on a network run the lane reports ``no_signal_source`` and
zero candidates. That empty result is the intended, honest output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.config import require_paper_only
from research.news_signals import SignalSource, build_signal_source
from research.scoreboard import (
    DEFAULT_RISK_LIMITS,
    DEFAULT_STARTING_CASH,
    NEWS_TRACK,
    TrackRuntime,
    VenueSnapshot,
    capture_snapshots,
    run_news_underreaction_track,
)
from strategies.news_underreaction import UnderreactionParameters
from core.types import Venue


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


async def measure_news_lane(
    *,
    use_fixtures: bool = True,
    limit: int = 25,
    kalshi_env: str | None = None,
    source: SignalSource | None = None,
    parameters: UnderreactionParameters | None = None,
    snapshots: dict[Venue, VenueSnapshot] | None = None,
    model_fees: bool = True,
) -> dict[str, Any]:
    """Run the news lane in isolation and return a JSON-ready report."""
    require_paper_only("news_underreaction")
    snapshots = snapshots or await capture_snapshots(use_fixtures=use_fixtures, limit=limit, kalshi_env=kalshi_env)
    source = source or build_signal_source(use_fixtures=use_fixtures)
    runtime = TrackRuntime.create(
        NEWS_TRACK,
        snapshots,
        ledger=None,
        risk_limits=DEFAULT_RISK_LIMITS,
        starting_cash=DEFAULT_STARTING_CASH,
        model_fees=model_fees,
    )
    summary = await run_news_underreaction_track(runtime, source=source, parameters=parameters)
    runtime.finalize(label=f"news:{'fixtures' if use_fixtures else 'network'}")
    return {
        "paper_only": True,
        "track": NEWS_TRACK,
        "mode": "fixtures" if use_fixtures else "network",
        "measured_at": datetime.now(UTC).isoformat(),
        "status": summary.metrics["status"],
        "signal_source": summary.metrics["signal_source"],
        "mapping": summary.metrics["mapping"],
        "parameters": summary.metrics["parameters"],
        "literature": summary.metrics["literature"],
        "reaction_ratio": summary.metrics["reaction_ratio"],
        "candidates": summary.candidates,
        "admitted": summary.admitted,
        "proposed_orders": summary.proposed_orders,
        "paper_fills": summary.paper_fills,
        "refused_by_reason": dict(sorted(summary.refused_by_reason.items())),
        "hit_rate": summary.metrics["hit_rate"],
        "settlement_preview": summary.metrics["settlement_preview"],
        "measurements": summary.metrics["measurements"],
        "fills": summary.fills,
        "ledger": summary.ledger,
        "snapshot": {
            venue.value: {"source": snap.source, "markets": len(snap.markets), "errors": snap.errors}
            for venue, snap in snapshots.items()
        },
        "not_validated": [
            "signal -> implied_probability mapping (fixture/operator assigned, never computed)",
            "pre_signal_mid provenance on network (no price history is captured)",
            "drift horizon / max_signal_age for news (paper studied in-play sports signals)",
            "literature pass-through 0.64 on Kalshi/Polymarket news markets",
        ],
    }


def _fmt(value: Any, width: int = 8) -> str:
    if value is None:
        return f"{'-':>{width}}"
    if isinstance(value, Decimal):
        return f"{float(value):>{width}.4f}"
    return f"{str(value):>{width}}"


def print_report(report: dict[str, Any]) -> None:
    print(f"\nnews_underreaction ({report['mode']}) status={report['status']} source={report['signal_source']['name']}")
    if report["signal_source"]["errors"]:
        for error in report["signal_source"]["errors"]:
            print(f"  source error: {error}")
    header = f"{'signal':<44}{'venue':<11}{'p0':>8}{'p1':>8}{'p_fair':>8}{'ratio':>8}{'resid':>8}{'lit':>8}  {'side':<5}{'qty':>6}  reason"
    print(header)
    print("-" * len(header))
    for row in report["measurements"]:
        print(
            f"{row['signal_id']:<44.43}{row['venue']:<11}"
            f"{_fmt(row['pre_signal_mid'])}{_fmt(row['current_mid'])}{_fmt(row['implied_probability'])}"
            f"{_fmt(row['reaction_ratio'])}{_fmt(row['residual_to_fair'])}{_fmt(row['literature_residual'])}"
            f"  {str(row['side'] or '-'):<5}{_fmt(row['quantity'], 6)}  {row['reason']}"
        )
    ratio = report["reaction_ratio"]
    print(
        f"\nreaction ratio: observed_mean={ratio['observed_mean']} (n={ratio['n']}) "
        f"vs literature {ratio['literature']} [{report['literature']['reference']}] -- {ratio['note']}"
    )
    print(
        f"candidates={report['candidates']} admitted={report['admitted']} fills={report['paper_fills']} "
        f"refused={report['refused_by_reason']}"
    )
    ledger = report["ledger"] or {}
    print(
        f"ledger: realized={ledger.get('realized_pnl')} unrealized={ledger.get('unrealized_pnl')} "
        f"fees={ledger.get('fees_paid')} equity={ledger.get('equity')} hit_rate={report['hit_rate']}"
    )
    print("NOT validated: " + "; ".join(report["not_validated"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", action="store_true", help="read public venue books (paper fills stay local)")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--kalshi-env", choices=("demo", "prod"), default=None)
    parser.add_argument("--news-signals", type=Path, default=None, help="operator JSON signals file")
    parser.add_argument("--news-rss", action="append", default=[], metavar="URL", help="public RSS/Atom feed (repeatable)")
    parser.add_argument("--max-age-seconds", type=str, default=None, help="override the staleness window")
    parser.add_argument("--json", type=Path, default=None, help="write the full report here")
    parser.add_argument("--no-fees", action="store_true")
    args = parser.parse_args()
    require_paper_only("news_underreaction")
    parameters = (
        UnderreactionParameters(max_signal_age_seconds=Decimal(args.max_age_seconds))
        if args.max_age_seconds
        else None
    )
    report = asyncio.run(
        measure_news_lane(
            use_fixtures=not args.network,
            limit=max(1, args.limit),
            kalshi_env=args.kalshi_env,
            source=build_signal_source(
                use_fixtures=not args.network, signals_path=args.news_signals, rss_urls=tuple(args.news_rss)
            ),
            parameters=parameters,
            model_fees=not args.no_fees,
        )
    )
    print_report(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, default=_json_default, indent=2, sort_keys=True) + "\n")
        print(f"report: {args.json}")


if __name__ == "__main__":
    main()
