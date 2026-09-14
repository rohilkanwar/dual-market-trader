"""Load harvested public settlement data from ``data/harvests/``.

Files (plain ``.json`` or ``.json.gz``), all optional:

* ``kalshi_series.json``           ``{series_ticker: [settled market, ...]}``
* ``polymarket_events.json``       list of Gamma events with nested ``markets``
* ``polymarket_cpi_resolved.json`` list of resolved Gamma markets

Missing files produce warnings, never exceptions, so a measurement run without
harvests simply reports divergence findings as not measured.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


@dataclass(slots=True)
class HarvestBundle:
    kalshi_series: dict[str, list[JsonObject]] = field(default_factory=dict)
    polymarket_events: list[JsonObject] = field(default_factory=list)
    polymarket_cpi_resolved: list[JsonObject] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_dir: str = ""

    @property
    def empty(self) -> bool:
        return not (self.kalshi_series or self.polymarket_events or self.polymarket_cpi_resolved)

    def kalshi_markets(self) -> tuple[JsonObject, ...]:
        out: list[JsonObject] = []
        for series, markets in self.kalshi_series.items():
            for market in markets:
                if isinstance(market, dict):
                    out.append({**market, "series_ticker": market.get("series_ticker", series)})
        return tuple(out)

    def polymarket_markets(self) -> tuple[JsonObject, ...]:
        nested_items: list[JsonObject] = []
        for event in self.polymarket_events:
            if not isinstance(event, dict):
                continue
            for market in event.get("markets", []) or []:
                if not isinstance(market, dict):
                    continue
                enriched = dict(market)
                enriched.setdefault("event_title", event.get("title", ""))
                enriched.setdefault("event_slug", event.get("slug", ""))
                nested_items.append(enriched)
        unique: dict[str, JsonObject] = {}
        for index, market in enumerate((*nested_items, *self.polymarket_cpi_resolved)):
            key = str(
                market.get("conditionId")
                or market.get("condition_id")
                or market.get("id")
                or market.get("slug")
                or index
            )
            unique[key] = market
        return tuple(unique.values())

    @classmethod
    def load(cls, harvest_dir: Path) -> HarvestBundle:
        warnings: list[str] = []
        bundle = cls(
            kalshi_series=_read_json(harvest_dir / "kalshi_series.json", {}, warnings),
            polymarket_events=_read_json(harvest_dir / "polymarket_events.json", [], warnings),
            polymarket_cpi_resolved=_read_json(
                harvest_dir / "polymarket_cpi_resolved.json", [], warnings
            ),
            warnings=warnings,
            source_dir=str(harvest_dir),
        )
        if not isinstance(bundle.kalshi_series, dict):
            warnings.append("kalshi_series.json must be an object keyed by series ticker")
            bundle.kalshi_series = {}
        return bundle


def _read_json(path: Path, default: Any, warnings: list[str]) -> Any:
    candidates = (path, path.with_suffix(path.suffix + ".gz"))
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            if candidate.suffix == ".gz":
                with gzip.open(candidate, "rt", encoding="utf-8") as handle:
                    return json.load(handle)
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"{candidate.name}: {exc}")
            return default
    warnings.append(f"{path.name}: missing")
    return default
