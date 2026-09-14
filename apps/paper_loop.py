"""Always-on paper measurement loop with append-only cycle history."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.measure_all import (
    json_default,
    load_ledgers,
    persist_run,
    to_jsonable,
    write_json,
)
from core.config import require_paper_only
from research.news_signals import build_signal_source
from research.scoreboard import PRIMARY_TRACK, TRACKS, TrackSummary, measure_all_with_ledgers
from research.specialist_scoreboard import load_specialist_state
from research.specialist_sources import build_trader_source
from strategies.edge import load_priors


LOGGER = logging.getLogger("paper_loop")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _append_json_line(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, default=json_default, sort_keys=True) + "\n")


def _track_log(summary: TrackSummary) -> dict[str, Any]:
    ledger = summary.ledger or {}
    return {
        "candidates": summary.candidates,
        "admitted": summary.admitted,
        "fills": summary.paper_fills,
        "realized_pnl": str(ledger.get("realized_pnl", "0")),
        "unrealized_pnl": str(ledger.get("unrealized_pnl", "0")),
        "equity": str(ledger.get("equity", "0")),
    }


async def run_cycle(
    *,
    use_network: bool,
    artifact_dir: Path,
    cycle: int,
    limit: int = 25,
    persist_ledgers: bool = True,
    priors_path: Path | None = None,
    kalshi_env: str | None = None,
    news_signals_path: Path | None = None,
    news_rss: tuple[str, ...] = (),
    specialist_traders: int | None = None,
) -> dict[str, Any]:
    """Run all isolated paper tracks once and persist one cycle snapshot.

    Ledgers (and the specialist follow log) are loaded from ``artifact_dir/paper/``
    before the cycle and saved after it, so realized/unrealized PnL, cash and
    drawdown carry across cycles.
    """
    require_paper_only("Paper loop")
    started_at = _now()
    started = time.monotonic()
    ledgers = load_ledgers(artifact_dir, TRACKS) if persist_ledgers else {}
    priors = load_priors(priors_path) if priors_path else None
    summaries, ledgers_by_track = await measure_all_with_ledgers(
        use_fixtures=not use_network,
        limit=limit,
        ledgers=ledgers,
        priors=priors,
        kalshi_env=kalshi_env,
        cycle_label=f"cycle:{cycle}",
        news_signals=build_signal_source(
            use_fixtures=not use_network, signals_path=news_signals_path, rss_urls=news_rss
        ),
        specialist_source=build_trader_source(use_fixtures=not use_network, traders=specialist_traders),
        specialist_state=load_specialist_state(artifact_dir) if persist_ledgers else None,
    )
    completed_at = _now()
    mode = "network" if use_network else "fixtures"
    artifact = await asyncio.to_thread(
        persist_run,
        summaries,
        ledgers_by_track if persist_ledgers else {},
        artifact_dir=artifact_dir,
        mode=mode,
        measured_at=completed_at,
        limit=limit,
        kalshi_env=kalshi_env,
        cycle=cycle,
    )
    primary = next((s for s in summaries if s.track == PRIMARY_TRACK), None)
    payload = {
        "paper_only": True,
        "primary_track": PRIMARY_TRACK,
        "mode": mode,
        "cycle": cycle,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": round(time.monotonic() - started, 6),
        "run_id": artifact["meta"]["run_id"],
        "tracks": to_jsonable([summary.as_dict() for summary in summaries]),
        "totals": {
            "candidates": sum(summary.candidates for summary in summaries),
            "admitted": sum(summary.admitted for summary in summaries),
            "paper_fills": sum(summary.paper_fills for summary in summaries),
            "paper_pnl": to_jsonable(artifact["totals"]["paper_pnl"]),
            "realized_pnl": to_jsonable(artifact["totals"]["realized_pnl"]),
            "unrealized_pnl": to_jsonable(artifact["totals"]["unrealized_pnl"]),
            "fees_paid": to_jsonable(artifact["totals"]["fees_paid"]),
        },
        "primary_ledger": to_jsonable(primary.ledger) if primary else {},
        "ledgers_persisted": persist_ledgers,
    }
    await asyncio.to_thread(write_json, artifact_dir / "paper_loop_latest.json", payload)
    await asyncio.to_thread(
        _append_json_line,
        artifact_dir / "paper_loop_history.jsonl",
        payload,
    )
    LOGGER.info(
        json.dumps(
            {
                "event": "paper_loop_cycle_completed",
                "paper_only": True,
                "mode": payload["mode"],
                "cycle": cycle,
                "duration_seconds": payload["duration_seconds"],
                "totals": payload["totals"],
                "tracks": {summary.track: _track_log(summary) for summary in summaries},
            },
            sort_keys=True,
        )
    )
    return payload


def _next_cycle_number(artifact_dir: Path) -> int:
    """Continue numbering from the persisted history so cycles stay monotonic."""
    latest = artifact_dir / "paper_loop_latest.json"
    if not latest.exists():
        return 0
    try:
        return int(json.loads(latest.read_text()).get("cycle", 0))
    except (ValueError, json.JSONDecodeError):
        return 0


async def run_loop(
    *,
    use_network: bool,
    artifact_dir: Path,
    interval_seconds: float = 300,
    once: bool = False,
    limit: int = 25,
    persist_ledgers: bool = True,
    priors_path: Path | None = None,
    kalshi_env: str | None = None,
    news_signals_path: Path | None = None,
    news_rss: tuple[str, ...] = (),
    specialist_traders: int | None = None,
) -> dict[str, Any] | None:
    """Run cycles forever, or exactly once for tests and scheduled invocations."""
    require_paper_only("Paper loop")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be greater than zero")
    cycle = _next_cycle_number(artifact_dir) if persist_ledgers else 0
    latest: dict[str, Any] | None = None
    while True:
        cycle += 1
        try:
            latest = await run_cycle(
                use_network=use_network,
                artifact_dir=artifact_dir,
                cycle=cycle,
                limit=limit,
                persist_ledgers=persist_ledgers,
                priors_path=priors_path,
                kalshi_env=kalshi_env,
                news_signals_path=news_signals_path,
                news_rss=news_rss,
                specialist_traders=specialist_traders,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                json.dumps(
                    {
                        "event": "paper_loop_cycle_failed",
                        "paper_only": True,
                        "mode": "network" if use_network else "fixtures",
                        "cycle": cycle,
                    },
                    sort_keys=True,
                )
            )
            if once:
                raise
        if once:
            return latest
        await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network",
        action="store_true",
        help="read public venue books; fills remain local paper simulations",
    )
    parser.add_argument("--interval-seconds", type=float, default=300)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts"))
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="fresh ledgers every cycle (default carries ledgers in artifact-dir/paper/)",
    )
    parser.add_argument("--priors", type=Path, default=None, help="JSON {market_id: probability}")
    parser.add_argument("--kalshi-env", choices=("demo", "prod"), default=None)
    parser.add_argument("--news-signals", type=Path, default=None, help="JSON signals file for the news_underreaction lane")
    parser.add_argument("--news-rss", action="append", default=[], metavar="URL", help="public RSS/Atom feed for the news lane (headlines only, never mapped; repeatable)")
    parser.add_argument("--specialist-traders", type=int, default=10, help="network cycles: wallets read from the public Polymarket volume leaderboard for the category_specialist lane (0 disables)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Third-party INFO logs (httpx request lines) would break the one-JSON-line
    # contract on stderr that the tests and schedulers rely on.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    require_paper_only("Paper loop")
    try:
        asyncio.run(
            run_loop(
                use_network=args.network,
                artifact_dir=args.artifact_dir,
                interval_seconds=args.interval_seconds,
                once=args.once,
                limit=max(1, args.limit),
                persist_ledgers=not args.no_persist,
                priors_path=args.priors,
                kalshi_env=args.kalshi_env,
                news_signals_path=args.news_signals,
                news_rss=tuple(args.news_rss),
                specialist_traders=args.specialist_traders,
            )
        )
    except KeyboardInterrupt:
        LOGGER.info('{"event":"paper_loop_stopped","reason":"keyboard_interrupt"}')


if __name__ == "__main__":
    main()
