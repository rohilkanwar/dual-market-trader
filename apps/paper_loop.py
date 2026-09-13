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

from apps.measure_all import json_default, to_jsonable, write_json
from core.config import require_paper_only
from research.scoreboard import TrackSummary, measure_all


LOGGER = logging.getLogger("paper_loop")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _append_json_line(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, default=json_default, sort_keys=True) + "\n")


def _track_log(summary: TrackSummary) -> dict[str, Any]:
    return {
        "candidates": summary.candidates,
        "admitted": summary.admitted,
        "fills": summary.paper_fills,
    }


async def run_cycle(
    *,
    use_network: bool,
    artifact_dir: Path,
    cycle: int,
    limit: int = 25,
) -> dict[str, Any]:
    """Run all isolated paper tracks once and persist one cycle snapshot."""
    require_paper_only("Paper loop")
    started_at = _now()
    started = time.monotonic()
    summaries = await measure_all(
        use_fixtures=not use_network,
        limit=limit,
    )
    completed_at = _now()
    payload = {
        "paper_only": True,
        "primary_track": "single_venue_fair_value",
        "mode": "network" if use_network else "fixtures",
        "cycle": cycle,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": round(time.monotonic() - started, 6),
        "tracks": to_jsonable([summary.as_dict() for summary in summaries]),
        "totals": {
            "candidates": sum(summary.candidates for summary in summaries),
            "admitted": sum(summary.admitted for summary in summaries),
            "paper_fills": sum(summary.paper_fills for summary in summaries),
        },
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
                "tracks": {summary.track: _track_log(summary) for summary in summaries},
            },
            sort_keys=True,
        )
    )
    return payload


async def run_loop(
    *,
    use_network: bool,
    artifact_dir: Path,
    interval_seconds: float = 300,
    once: bool = False,
    limit: int = 25,
) -> dict[str, Any] | None:
    """Run cycles forever, or exactly once for tests and scheduled invocations."""
    require_paper_only("Paper loop")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be greater than zero")
    cycle = 0
    latest: dict[str, Any] | None = None
    while True:
        cycle += 1
        try:
            latest = await run_cycle(
                use_network=use_network,
                artifact_dir=artifact_dir,
                cycle=cycle,
                limit=limit,
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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    require_paper_only("Paper loop")
    try:
        asyncio.run(
            run_loop(
                use_network=args.network,
                artifact_dir=args.artifact_dir,
                interval_seconds=args.interval_seconds,
                once=args.once,
                limit=max(1, args.limit),
            )
        )
    except KeyboardInterrupt:
        LOGGER.info('{"event":"paper_loop_stopped","reason":"keyboard_interrupt"}')


if __name__ == "__main__":
    main()
