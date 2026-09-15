"""Free public station observations for the ``weather_dead_bucket`` paper track.

Two documented $0 sources, both unauthenticated, both read-only:

* **aviationweather.gov Data API** (NOAA / NWS Aviation Weather Center):
  ``GET https://aviationweather.gov/api/data/metar?ids=KLGA&format=json&hours=30``
  returns the decoded METAR/SPECI history of any ICAO station worldwide
  (``icaoId``, ``obsTime`` epoch seconds, ``temp`` °C with tenths, ``metarType``,
  ``rawOb``). Primary source; covers every station Polymarket has used.
* **api.weather.gov** (NWS): ``GET /stations/{id}/observations?start=&end=``
  returns the same ASOS observations for US stations (``properties.timestamp``,
  ``properties.temperature.value`` °C, ``properties.rawMessage``). Fallback for
  stations the AWC endpoint fails on; requires a ``User-Agent`` header.

The temperature is always taken from the raw METAR ``T`` group (tenths of °C)
when present, so both sources yield identical :class:`StationObservation`
rows. The market's own resolution table (NOAA WRH time series) is built from
these same observations; the difference between what it prints and what a
METAR carries (rounding at exactly .5, hourly-only filtering) is handled by
the strategy's conservative rules, not here.

Fixture runs read synthetic METAR sequences from the replay file through the
same parser. A network run whose fetch fails reports the error per station and
the runner refuses those events with ``no_obs``; nothing is guessed.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from strategies.weather_dead_bucket import StationObservation, parse_metar_observation, parse_metar_report_type, parse_metar_temperature_tenths_c

LOGGER = logging.getLogger("weather_obs")

AVIATION_WEATHER_URL = "https://aviationweather.gov/api/data/metar"
NWS_API_URL = "https://api.weather.gov"
USER_AGENT = "dual-market-trader-paper/0.2 (paper-only measurement; no trading)"
MAX_HOURS_BACK = 48
FREE_SOURCES = {
    "primary": "aviationweather.gov /api/data/metar (NOAA AWC, no key, worldwide METAR/SPECI history)",
    "fallback": "api.weather.gov /stations/{id}/observations (NWS, no key, User-Agent required)",
    "not_used": "weather.gov/wrh/timeseries (the resolution table itself) has no documented public JSON API; Weather Underground is not scraped",
}


def _now() -> datetime:
    return datetime.now(UTC)


def parse_time(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=UTC)
    text = str(raw).strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(slots=True)
class ObservationBatch:
    source: str  # aviationweather | nws | chained | fixture | file | none
    observations: dict[str, list[StationObservation]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    requests: int = 0
    fetched_at: str = field(default_factory=lambda: _now().isoformat())
    note: str = ""

    def for_station(self, station: str) -> list[StationObservation]:
        """Rows returned *for* ``station`` (keyed by the station requested, so a
        source answering with another station's data is visible as a mismatch)."""
        return sorted(self.observations.get(station.upper(), []), key=lambda o: o.observed_at)

    def add(self, observation: StationObservation, *, station: str | None = None) -> None:
        rows = self.observations.setdefault((station or observation.station_icao).upper(), [])
        if all(r.observed_at != observation.observed_at or r.report_type != observation.report_type for r in rows):
            rows.append(observation)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "stations": {k: len(v) for k, v in sorted(self.observations.items())},
            "errors": list(self.errors),
            "requests": self.requests,
            "fetched_at": self.fetched_at,
            "note": self.note,
        }


class ObservationSource(Protocol):
    name: str

    async def fetch(self, stations: set[str], *, start: datetime, end: datetime) -> ObservationBatch: ...


class NullObservationSource:
    """No observations at all: every event is refused ``no_obs``."""

    name = "none"

    async def fetch(self, stations: set[str], *, start: datetime, end: datetime) -> ObservationBatch:
        del stations, start, end
        return ObservationBatch(source=self.name, note="no observation source configured")


class StaticObservationSource:
    """Serve observations already in memory (fixtures, tests)."""

    def __init__(self, observations: dict[str, list[StationObservation]], *, source: str = "fixture", note: str = "") -> None:
        self.name = source
        self._observations = observations
        self.note = note

    async def fetch(self, stations: set[str], *, start: datetime, end: datetime) -> ObservationBatch:
        batch = ObservationBatch(source=self.name, note=self.note)
        for station in stations:
            for observation in self._observations.get(station.upper(), []):
                if start <= observation.observed_at <= end:
                    batch.add(observation, station=station)
        return batch


# --------------------------------------------------------------------------
# Payload parsers (pure)
# --------------------------------------------------------------------------
def observation_from_aviationweather(item: dict[str, Any]) -> StationObservation | None:
    """One row of ``/api/data/metar?format=json``."""
    icao = str(item.get("icaoId") or "").upper()
    when = parse_time(item.get("obsTime")) or parse_time(item.get("reportTime"))
    raw = str(item.get("rawOb") or "")
    if not icao or when is None:
        return None
    tenths = parse_metar_temperature_tenths_c(raw) if raw else None
    if tenths is None:
        temp = item.get("temp")
        if temp in (None, ""):
            return None
        try:
            tenths = int(round(float(temp) * 10))
        except (TypeError, ValueError):
            return None
    report_type = str(item.get("metarType") or parse_metar_report_type(raw) or "METAR").upper()
    if report_type not in ("METAR", "SPECI"):
        report_type = "METAR"
    return StationObservation(icao, when.astimezone(UTC), tenths, report_type, raw)


def parse_aviationweather_payload(payload: Any) -> list[StationObservation]:
    rows = payload if isinstance(payload, list) else []
    out: list[StationObservation] = []
    for item in rows:
        if isinstance(item, dict):
            observation = observation_from_aviationweather(item)
            if observation is not None:
                out.append(observation)
    return out


def observation_from_nws(feature: dict[str, Any], *, station: str) -> StationObservation | None:
    """One ``features[]`` item of ``/stations/{id}/observations``."""
    props = feature.get("properties") if isinstance(feature, dict) else None
    if not isinstance(props, dict):
        return None
    when = parse_time(props.get("timestamp"))
    if when is None:
        return None
    raw = str(props.get("rawMessage") or "")
    tenths = parse_metar_temperature_tenths_c(raw) if raw else None
    if tenths is None:
        temperature = props.get("temperature") or {}
        value = temperature.get("value") if isinstance(temperature, dict) else None
        if value in (None, ""):
            return None
        try:
            tenths = int(round(float(value) * 10))
        except (TypeError, ValueError):
            return None
    icao = str(props.get("station") or "").rsplit("/", 1)[-1].upper() or station.upper()
    return StationObservation(icao, when.astimezone(UTC), tenths, parse_metar_report_type(raw) if raw else "METAR", raw)


def parse_nws_payload(payload: Any, *, station: str) -> list[StationObservation]:
    features = payload.get("features") if isinstance(payload, dict) else None
    out: list[StationObservation] = []
    for feature in features or []:
        observation = observation_from_nws(feature, station=station)
        if observation is not None:
            out.append(observation)
    return out


# --------------------------------------------------------------------------
# Network sources
# --------------------------------------------------------------------------
def hours_back(start: datetime, end: datetime) -> int:
    """Whole hours the AWC ``hours`` parameter must cover, with one hour of slack, capped."""
    span = max(0.0, (end - start).total_seconds() / 3600.0)
    return int(min(MAX_HOURS_BACK, max(1, math.ceil(span) + 1)))


class AviationWeatherMetarSource:
    name = "aviationweather"

    def __init__(self, *, timeout: float = 20.0, http_get: Any | None = None) -> None:
        self.timeout = timeout
        self._http_get = http_get

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        if self._http_get is not None:
            return await self._http_get(url, params)
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def fetch(self, stations: set[str], *, start: datetime, end: datetime) -> ObservationBatch:
        batch = ObservationBatch(source=self.name, note=FREE_SOURCES["primary"])
        hours = hours_back(start, end)
        for station in sorted(s.upper() for s in stations):
            try:
                payload = await self._get(AVIATION_WEATHER_URL, {"ids": station, "format": "json", "hours": hours})
                batch.requests += 1
            except Exception as exc:  # one dead station must not kill the run
                batch.errors.append(f"{station}: {type(exc).__name__}: {exc}")
                continue
            for observation in parse_aviationweather_payload(payload):
                if start <= observation.observed_at <= end:
                    batch.add(observation, station=station)
        return batch


class NwsObservationSource:
    name = "nws"

    def __init__(self, *, timeout: float = 20.0, http_get: Any | None = None) -> None:
        self.timeout = timeout
        self._http_get = http_get

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        if self._http_get is not None:
            return await self._http_get(url, params)
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"}) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def fetch(self, stations: set[str], *, start: datetime, end: datetime) -> ObservationBatch:
        batch = ObservationBatch(source=self.name, note=FREE_SOURCES["fallback"])
        for station in sorted(s.upper() for s in stations):
            try:
                payload = await self._get(
                    f"{NWS_API_URL}/stations/{station}/observations",
                    {"start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 500},
                )
                batch.requests += 1
            except Exception as exc:
                batch.errors.append(f"{station}: {type(exc).__name__}: {exc}")
                continue
            for observation in parse_nws_payload(payload, station=station):
                if start <= observation.observed_at <= end:
                    batch.add(observation, station=station)
        return batch


class ChainedObservationSource:
    """Primary first; stations that came back empty or errored are retried on the fallback."""

    name = "chained"

    def __init__(self, primary: ObservationSource, fallback: ObservationSource) -> None:
        self.primary = primary
        self.fallback = fallback

    async def fetch(self, stations: set[str], *, start: datetime, end: datetime) -> ObservationBatch:
        wanted = {s.upper() for s in stations}
        first = await self.primary.fetch(wanted, start=start, end=end)
        missing = {s for s in wanted if not first.observations.get(s)}
        batch = ObservationBatch(source=f"{self.primary.name}+{self.fallback.name}", observations=dict(first.observations), errors=list(first.errors), requests=first.requests, note=first.note)
        if missing:
            second = await self.fallback.fetch(missing, start=start, end=end)
            batch.requests += second.requests
            batch.errors.extend(f"fallback {e}" for e in second.errors)
            for station, rows in second.observations.items():
                for row in rows:
                    batch.add(row, station=station)
            if second.observations:
                batch.note = f"{first.note}; fallback used for {sorted(second.observations)}"
        return batch


class JsonObservationSource:
    """A saved ``/api/data/metar?format=json`` response (or a fixture in that shape)."""

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path

    async def fetch(self, stations: set[str], *, start: datetime, end: datetime) -> ObservationBatch:
        batch = ObservationBatch(source=self.name, note=str(self.path))
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            batch.errors.append(f"{self.path}: {type(exc).__name__}: {exc}")
            return batch
        wanted = {s.upper() for s in stations}
        for observation in parse_aviationweather_payload(payload if isinstance(payload, list) else payload.get("observations", [])):
            if observation.station_icao in wanted and start <= observation.observed_at <= end:
                batch.add(observation)
        return batch


def build_observation_source(*, use_fixtures: bool, obs_file: Path | None = None) -> ObservationSource | None:
    """Fixtures: ``None`` (the replay supplies its own). Network: AWC with NWS fallback, or a saved file."""
    if obs_file is not None:
        return JsonObservationSource(obs_file)
    if use_fixtures:
        return None
    return ChainedObservationSource(AviationWeatherMetarSource(), NwsObservationSource())


# --------------------------------------------------------------------------
# Fixture observations
# --------------------------------------------------------------------------
def parse_fixture_observations(payload: dict[str, Any], *, month_anchor: datetime | None = None) -> dict[str, list[StationObservation]]:
    """``{"KLGA": [{"raw": "METAR KLGA 151851Z ... T02330178", "observed_at": "2026-09-15T18:51:00Z"}, ...]}``.

    Keyed by the station the rows were *requested for* (the dict key); each
    observation keeps the ICAO found in its own text, so a fixture can stage a
    station mismatch. ``observed_at`` may be omitted when ``month_anchor`` lets
    the ``DDHHMMZ`` group be resolved. Unparseable rows are dropped, never guessed.
    """
    out: dict[str, list[StationObservation]] = {}
    for station, rows in payload.items():
        for row in rows or []:
            if isinstance(row, str):
                observation = parse_metar_observation(row, station=station.upper(), month_anchor=month_anchor)
            elif isinstance(row, dict):
                observation = parse_metar_observation(
                    str(row.get("raw") or ""),
                    station=str(row.get("station") or station).upper(),
                    observed_at=parse_time(row.get("observed_at")),
                    month_anchor=month_anchor,
                )
            else:
                observation = None
            if observation is not None:
                out.setdefault(station.upper(), []).append(observation)
    for rows in out.values():
        rows.sort(key=lambda o: o.observed_at)
    return out


def window_for(specs_start: datetime, as_of: datetime) -> tuple[datetime, datetime]:
    """Fetch window: from the earliest local-day start requested to ``as_of`` (plus a minute of slack)."""
    return specs_start, as_of + timedelta(minutes=1)


__all__ = [
    "AVIATION_WEATHER_URL",
    "AviationWeatherMetarSource",
    "ChainedObservationSource",
    "FREE_SOURCES",
    "JsonObservationSource",
    "NWS_API_URL",
    "NullObservationSource",
    "NwsObservationSource",
    "ObservationBatch",
    "ObservationSource",
    "StaticObservationSource",
    "build_observation_source",
    "hours_back",
    "observation_from_aviationweather",
    "observation_from_nws",
    "parse_aviationweather_payload",
    "parse_fixture_observations",
    "parse_nws_payload",
    "parse_time",
    "window_for",
]
