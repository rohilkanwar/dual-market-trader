"""Harvest resolved macro markets from public, unauthenticated venue endpoints.

Writes ``kalshi_series.json``, ``polymarket_events.json`` and
``polymarket_cpi_resolved.json`` into ``--harvest-dir`` for
``research.harvest_scoreboard``. Read-only; no credentials are used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from venues.kalshi.client import HOSTS

GAMMA_ROOT = "https://gamma-api.polymarket.com"
DEFAULT_KALSHI_SERIES = ("KXCPIYOY", "KXCPI", "KXFEDDECISION", "KXFED")


class PublicHarvester:
    def __init__(self, *, kalshi_env: str = "prod", timeout: float = 20.0) -> None:
        self.kalshi_base = HOSTS[kalshi_env]
        self._http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, **params: Any) -> Any:
        response = await self._http.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def kalshi_series(self, tickers: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for ticker in tickers:
            try:
                payload = await self._get(
                    f"{self.kalshi_base}/markets", series_ticker=ticker, status="settled", limit=200
                )
            except httpx.HTTPError as exc:
                out[ticker] = [{"harvest_error": f"{type(exc).__name__}: {exc}"}]
                continue
            out[ticker] = [m for m in payload.get("markets", []) if isinstance(m, dict)]
        return out

    async def polymarket_events(self) -> list[dict[str, Any]]:
        events: dict[str, dict[str, Any]] = {}
        try:
            payload = await self._get(f"{GAMMA_ROOT}/events", closed="true", limit=200, order="volume", ascending="false")
        except httpx.HTTPError:
            payload = []
        for event in payload if isinstance(payload, list) else []:
            if isinstance(event, dict):
                events[str(event.get("id") or event.get("slug"))] = event

        searched: dict[str, dict[str, Any]] = {}
        for query in (
            "Federal Reserve",
            "US CPI inflation",
            "CPI July 2026",
            "CPI June 2026",
            "unemployment payrolls",
            "nonfarm payrolls",
        ):
            try:
                payload = await self._get(f"{GAMMA_ROOT}/public-search", q=query, limit_per_type=50)
            except httpx.HTTPError:
                continue
            raw_events = payload.get("events", []) if isinstance(payload, dict) else []
            for event in raw_events:
                if isinstance(event, dict):
                    searched[str(event.get("id") or event.get("slug"))] = event
        return [
            *searched.values(),
            *(event for key, event in events.items() if key not in searched),
        ]

    @staticmethod
    def _text(value: dict[str, Any]) -> str:
        return " ".join(str(value.get(k) or "") for k in ("question", "title", "slug", "description")).lower()

    async def polymarket_cpi_resolved(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for event in events:
            for market in event.get("markets", []) or []:
                if isinstance(market, dict) and market.get("closed") and "cpi" in self._text(market):
                    resolved.append({**market, "event_title": event.get("title", ""), "event_slug": event.get("slug", "")})
        return resolved


async def harvest(harvest_dir: Path, *, kalshi_env: str, series: tuple[str, ...]) -> dict[str, int]:
    harvester = PublicHarvester(kalshi_env=kalshi_env)
    try:
        kalshi = await harvester.kalshi_series(series)
        events = await harvester.polymarket_events()
        cpi = await harvester.polymarket_cpi_resolved(events)
    finally:
        await harvester.close()
    harvest_dir.mkdir(parents=True, exist_ok=True)
    (harvest_dir / "kalshi_series.json").write_text(json.dumps(kalshi, indent=2))
    (harvest_dir / "polymarket_events.json").write_text(json.dumps(events, indent=2))
    (harvest_dir / "polymarket_cpi_resolved.json").write_text(json.dumps(cpi, indent=2))
    return {
        "kalshi_markets": sum(len(v) for v in kalshi.values()),
        "polymarket_events": len(events),
        "polymarket_cpi_resolved": len(cpi),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harvest-dir", type=Path, default=Path("data/harvests"))
    parser.add_argument("--kalshi-env", choices=tuple(HOSTS), default="prod")
    parser.add_argument("--series", nargs="*", default=list(DEFAULT_KALSHI_SERIES))
    args = parser.parse_args()
    counts = asyncio.run(harvest(args.harvest_dir, kalshi_env=args.kalshi_env, series=tuple(args.series)))
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
