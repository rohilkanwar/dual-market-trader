"""Free public weather feeds for the ``weather_bucket_edge`` paper track.

Two documented, keyless, $0 sources and nothing else:

* **Open-Meteo ensemble API** (``https://ensemble-api.open-meteo.com/v1/ensemble``):
  per-member daily ``temperature_2m_max`` / ``temperature_2m_min`` for the
  station coordinates from several global ensembles (``gfs_seamless`` = NCEP
  GEFS 31 members, ``ecmwf_ifs025`` = ECMWF IFS 51 members, ``icon_seamless`` =
  DWD ICON-EPS 40 members). Non-commercial use is free without a key and rate
  limited (10,000 calls/day); ``timezone=auto`` resolves the station's local day.
* **aviationweather.gov data API** (``https://aviationweather.gov/api/data``):
  public METAR history (``/metar?ids=KLGA&format=json&hours=48``) and station
  metadata (``/stationinfo``). The METAR temperature in tenths of a degree
  (``T02170078`` remark group) is what NOAA's ``weather.gov/wrh/timeseries``
  page, Polymarket's resolution source, renders as its whole-degree "Temp"
  column, so the day's hourly maximum / minimum can be reconstructed from it.

Paid or keyed vendors (Visual Crossing, commercial NWP feeds) are **not
implemented**. :func:`paid_source_policy` reports whether such a key is even
present in the environment; the answer never changes what runs (default OFF,
and there is no code path to switch on).

Fixture runs read the same shapes from ``research/fixtures/weather_buckets_replay.json``
through :class:`FixtureWeatherFeed`; :class:`NullWeatherFeed` makes a network
run an honest empty when no feed is configured.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from strategies.weather_buckets import round_half_up

OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
AVIATIONWEATHER_URL = "https://aviationweather.gov/api/data"
DEFAULT_MODELS: tuple[str, ...] = ("gfs_seamless", "ecmwf_ifs025", "icon_seamless")
STATION_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "data" / "weather" / "stations.json"
PAID_SOURCE_KEY_ENVS: tuple[str, ...] = ("VISUAL_CROSSING_KEY", "WEATHER_PAID_API_KEY")
PAID_SOURCE_ENABLE_ENV = "WEATHER_ALLOW_PAID_SOURCES"
USER_AGENT = "dual-market-trader-paper/0.2 (weather_bucket_edge; paper-only measurement)"
FREE_SOURCES = {
    "forecast": {
        "provider": "Open-Meteo ensemble API (open-meteo.com)",
        "endpoint": OPEN_METEO_ENSEMBLE_URL,
        "models": list(DEFAULT_MODELS),
        "cost": "free for non-commercial use, no key, 10,000 calls/day",
        "fields": "daily temperature_2m_max / temperature_2m_min per ensemble member at the station coordinates",
        "terms": "https://open-meteo.com/en/terms (CC BY 4.0 data, non-commercial free tier)",
    },
    "observations": {
        "provider": "NOAA/NWS aviationweather.gov data API",
        "endpoint": f"{AVIATIONWEATHER_URL}/metar",
        "cost": "free, no key",
        "fields": "METAR temperature (tenths of °C from the T-group), report time, station coordinates",
        "terms": "https://aviationweather.gov/data/api/",
    },
}
_RE_T_GROUP = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")
_RE_MEMBER_KEY = re.compile(r"^temperature_2m_(max|min)(?:_member(\d+))?_(.+)$")


def _now() -> datetime:
    return datetime.now(UTC)


def parse_time(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=UTC)
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def zone_for(timezone_name: str | None, utc_offset_seconds: int | None) -> tzinfo:
    """IANA zone when the name resolves, else the fixed offset Open-Meteo reported, else UTC."""
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    if utc_offset_seconds:
        return _FixedOffset(utc_offset_seconds)
    return UTC


class _FixedOffset(tzinfo):
    def __init__(self, seconds: int) -> None:
        self._offset = timedelta(seconds=seconds)

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return self._offset

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        sign = "+" if self._offset >= timedelta(0) else "-"
        total = abs(int(self._offset.total_seconds()))
        return f"UTC{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def celsius_to(value_c: float, unit: str) -> float:
    return value_c * 9.0 / 5.0 + 32.0 if unit == "F" else value_c


# --------------------------------------------------------------------------
# Stations
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Station:
    icao: str
    name: str | None
    latitude: float
    longitude: float
    country: str | None = None
    elevation_m: float | None = None
    source: str = "registry"

    def as_dict(self) -> dict[str, Any]:
        return {
            "icao": self.icao,
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "country": self.country,
            "elevation_m": self.elevation_m,
            "source": self.source,
        }


class StationRegistry:
    """Committed ``data/weather/stations.json`` (public stationinfo coordinates)."""

    def __init__(self, stations: dict[str, Station], *, path: Path | None = None) -> None:
        self.stations = stations
        self.path = path

    @classmethod
    def load(cls, path: Path = STATION_REGISTRY_PATH) -> StationRegistry:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stations: dict[str, Station] = {}
        for icao, item in (payload.get("stations") or {}).items():
            try:
                stations[icao.upper()] = Station(
                    icao=icao.upper(),
                    name=item.get("name"),
                    latitude=float(item["lat"]),
                    longitude=float(item["lon"]),
                    country=item.get("country"),
                    elevation_m=float(item["elevation_m"]) if item.get("elevation_m") is not None else None,
                )
            except (KeyError, TypeError, ValueError):
                continue
        return cls(stations, path=path)

    def get(self, icao: str) -> Station | None:
        return self.stations.get(icao.upper())

    def __len__(self) -> int:
        return len(self.stations)


def parse_station_info(payload: Any) -> dict[str, Station]:
    """``/stationinfo?format=json`` items -> stations keyed by ICAO."""
    out: dict[str, Station] = {}
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        icao = str(item.get("icaoId") or "").upper()
        try:
            latitude, longitude = float(item["lat"]), float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not icao:
            continue
        elevation = item.get("elev")
        out[icao] = Station(
            icao=icao,
            name=item.get("site"),
            latitude=latitude,
            longitude=longitude,
            country=item.get("country"),
            elevation_m=float(elevation) if elevation is not None else None,
            source="aviationweather_stationinfo",
        )
    return out


# --------------------------------------------------------------------------
# Ensemble forecasts
# --------------------------------------------------------------------------
@dataclass(slots=True)
class EnsembleForecast:
    station: str
    target_date: date
    kind: str  # high | low
    unit: str  # F | C
    members_by_model: dict[str, list[float]] = field(default_factory=dict)
    timezone: str | None = None
    utc_offset_seconds: int | None = None
    fetched_at: str = field(default_factory=lambda: _now().isoformat())
    source: str = "open_meteo_ensemble"
    errors: list[str] = field(default_factory=list)

    @property
    def members(self) -> list[float]:
        return [value for values in self.members_by_model.values() for value in values]

    @property
    def n_members(self) -> int:
        return len(self.members)

    @property
    def n_models(self) -> int:
        return sum(1 for values in self.members_by_model.values() if values)

    @property
    def available(self) -> bool:
        return self.n_members > 0 and not self.errors

    def age_hours(self, as_of: datetime) -> Decimal | None:
        fetched = parse_time(self.fetched_at)
        if fetched is None:
            return None
        return Decimal(str(round((as_of - fetched).total_seconds() / 3600, 4)))

    def as_dict(self) -> dict[str, Any]:
        members = self.members
        mean = sum(members) / len(members) if members else None
        return {
            "station": self.station,
            "target_date": self.target_date.isoformat(),
            "kind": self.kind,
            "unit": self.unit,
            "source": self.source,
            "models": {model: len(values) for model, values in self.members_by_model.items()},
            "n_members": self.n_members,
            "n_models": self.n_models,
            "mean": round(mean, 4) if mean is not None else None,
            "min": min(members) if members else None,
            "max": max(members) if members else None,
            "timezone": self.timezone,
            "utc_offset_seconds": self.utc_offset_seconds,
            "fetched_at": self.fetched_at,
            "errors": list(self.errors),
        }


def parse_ensemble_payload(payload: dict[str, Any], *, station: str, target_date: date, kind: str, unit: str, source: str = "open_meteo_ensemble") -> EnsembleForecast:
    """Open-Meteo ``daily`` block -> per-model member lists for ``target_date``."""
    forecast = EnsembleForecast(
        station=station,
        target_date=target_date,
        kind=kind,
        unit=unit,
        timezone=payload.get("timezone"),
        utc_offset_seconds=int(payload["utc_offset_seconds"]) if payload.get("utc_offset_seconds") is not None else None,
        source=source,
    )
    if payload.get("error"):
        forecast.errors.append(str(payload.get("reason") or "open-meteo error"))
        return forecast
    daily = payload.get("daily") or {}
    times = [str(t) for t in daily.get("time") or []]
    if target_date.isoformat() not in times:
        forecast.errors.append(f"target date {target_date.isoformat()} not in daily.time {times}")
        return forecast
    index = times.index(target_date.isoformat())
    wanted = "max" if kind == "high" else "min"
    for key, values in daily.items():
        match = _RE_MEMBER_KEY.match(key)
        if match is None or match.group(1) != wanted or not isinstance(values, list) or index >= len(values):
            continue
        value = values[index]
        if value is None or not isinstance(value, (int, float)) or math.isnan(float(value)):
            continue
        forecast.members_by_model.setdefault(match.group(3), []).append(float(value))
    if forecast.n_members == 0:
        forecast.errors.append("no ensemble members in the response")
    return forecast


# --------------------------------------------------------------------------
# METAR observations
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MetarObservation:
    station: str
    time: datetime
    temp_c: float
    raw: str = ""
    precise: bool = False  # tenths from the T-group

    def as_dict(self) -> dict[str, Any]:
        return {"station": self.station, "time": self.time.isoformat(), "temp_c": self.temp_c, "precise": self.precise, "raw": self.raw}


def metar_temperature_c(item: dict[str, Any]) -> tuple[float, bool] | None:
    """Tenths of °C from the ``T`` remark group when present, else the whole-degree field."""
    raw = str(item.get("rawOb") or "")
    if (match := _RE_T_GROUP.search(raw)) is not None:
        sign, tenths, _, _ = match.groups()
        value = int(tenths) / 10.0
        return (-value if sign == "1" else value), True
    temp = item.get("temp")
    if temp is None:
        return None
    try:
        return float(temp), False
    except (TypeError, ValueError):
        return None


def parse_metar_payload(payload: Any, *, station: str | None = None) -> list[MetarObservation]:
    out: list[MetarObservation] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        icao = str(item.get("icaoId") or item.get("station") or station or "").upper()
        if station and icao != station.upper():
            continue
        when = parse_time(item.get("obsTime") if item.get("obsTime") is not None else item.get("reportTime") or item.get("time"))
        temperature = metar_temperature_c(item) if "rawOb" in item or "temp" in item else None
        if temperature is None and item.get("temp_c") is not None:
            temperature = (float(item["temp_c"]), bool(item.get("precise", True)))
        if when is None or temperature is None:
            continue
        out.append(MetarObservation(icao, when, temperature[0], str(item.get("rawOb") or item.get("raw") or ""), temperature[1]))
    return sorted(out, key=lambda o: o.time)


@dataclass(frozen=True, slots=True)
class ObservedExtreme:
    value: int | None  # whole degrees in the market unit
    unit: str
    kind: str
    n_observations: int
    first: str | None
    last: str | None
    complete: bool
    reason: str  # observed | no_observations | day_not_over | partial_day

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "kind": self.kind,
            "n_observations": self.n_observations,
            "first": self.first,
            "last": self.last,
            "complete": self.complete,
            "reason": self.reason,
        }


def observed_extreme(
    observations: list[MetarObservation],
    *,
    local_day: date,
    zone: tzinfo,
    unit: str,
    kind: str,
    as_of: datetime,
    min_observations: int = 18,
) -> ObservedExtreme:
    """Whole-degree max / min of the METAR temperatures whose local date is ``local_day``.

    Conversion (°C tenths -> °F, round half up) mirrors what the NOAA timeseries
    page displays; ``complete`` requires the local day to have ended and at
    least ``min_observations`` reports (a full day has ~24 hourlies plus SPECIs).
    """
    day_obs = [o for o in observations if o.time.astimezone(zone).date() == local_day]
    values = [round_half_up(celsius_to(o.temp_c, unit)) for o in day_obs]
    day_end = datetime.combine(local_day + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    day_over = as_of >= day_end
    if not values:
        return ObservedExtreme(None, unit, kind, 0, None, None, False, "no_observations")
    value = max(values) if kind == "high" else min(values)
    if not day_over:
        reason, complete = "day_not_over", False
    elif len(values) < min_observations:
        reason, complete = "partial_day", False
    else:
        reason, complete = "observed", True
    return ObservedExtreme(value, unit, kind, len(values), day_obs[0].time.isoformat(), day_obs[-1].time.isoformat(), complete, reason)


# --------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------
class WeatherFeed(Protocol):
    name: str

    async def station(self, icao: str) -> Station | None: ...

    async def forecast(self, station: Station, *, target_date: date, kind: str, unit: str, as_of: datetime) -> EnsembleForecast: ...

    async def observations(self, station: Station, *, as_of: datetime, hours: int = 48) -> list[MetarObservation]: ...


class NullWeatherFeed:
    """No feed configured: forecasts carry an error, observations are empty."""

    name = "none"

    def __init__(self, registry: StationRegistry | None = None) -> None:
        self.registry = registry

    async def station(self, icao: str) -> Station | None:
        return self.registry.get(icao) if self.registry is not None else None

    async def forecast(self, station: Station, *, target_date: date, kind: str, unit: str, as_of: datetime) -> EnsembleForecast:
        del as_of
        forecast = EnsembleForecast(station=station.icao, target_date=target_date, kind=kind, unit=unit, source=self.name)
        forecast.errors.append("no weather feed configured")
        return forecast

    async def observations(self, station: Station, *, as_of: datetime, hours: int = 48) -> list[MetarObservation]:
        del station, as_of, hours
        return []


class FixtureWeatherFeed:
    """In-memory forecasts / METARs / stations from a replay step (fixture runs and tests)."""

    name = "fixture"

    def __init__(
        self,
        *,
        stations: dict[str, Station] | None = None,
        forecasts: dict[tuple[str, str, str], EnsembleForecast] | None = None,
        metars: dict[str, list[MetarObservation]] | None = None,
        registry: StationRegistry | None = None,
    ) -> None:
        self.stations = stations or {}
        self.forecasts = forecasts or {}
        self.metars = metars or {}
        self.registry = registry

    @classmethod
    def from_step(cls, step: dict[str, Any], *, registry: StationRegistry | None = None) -> FixtureWeatherFeed:
        stations = {
            str(item["icao"]).upper(): Station(
                icao=str(item["icao"]).upper(),
                name=item.get("name"),
                latitude=float(item["lat"]),
                longitude=float(item["lon"]),
                country=item.get("country"),
                source="fixture",
            )
            for item in step.get("stations") or []
        }
        forecasts: dict[tuple[str, str, str], EnsembleForecast] = {}
        for item in step.get("forecasts") or []:
            forecast = EnsembleForecast(
                station=str(item["station"]).upper(),
                target_date=date.fromisoformat(str(item["date"])),
                kind=str(item["kind"]),
                unit=str(item["unit"]).upper(),
                members_by_model={str(k): [float(v) for v in vs] for k, vs in (item.get("members_by_model") or {}).items()},
                timezone=item.get("timezone"),
                utc_offset_seconds=item.get("utc_offset_seconds"),
                fetched_at=str(item.get("fetched_at") or step.get("as_of") or _now().isoformat()),
                source="fixture",
                errors=[str(e) for e in item.get("errors") or []],
            )
            forecasts[(forecast.station, forecast.target_date.isoformat(), forecast.kind)] = forecast
        metars = {
            str(icao).upper(): parse_metar_payload(items, station=str(icao))
            for icao, items in (step.get("metars") or {}).items()
        }
        return cls(stations=stations, forecasts=forecasts, metars=metars, registry=registry)

    async def station(self, icao: str) -> Station | None:
        found = self.stations.get(icao.upper())
        if found is None and self.registry is not None:
            found = self.registry.get(icao)
        return found

    async def forecast(self, station: Station, *, target_date: date, kind: str, unit: str, as_of: datetime) -> EnsembleForecast:
        del as_of
        found = self.forecasts.get((station.icao, target_date.isoformat(), kind))
        if found is None:
            missing = EnsembleForecast(station=station.icao, target_date=target_date, kind=kind, unit=unit, source=self.name)
            missing.errors.append("fixture has no forecast for this station/date/kind")
            return missing
        if found.unit != unit:
            missing = EnsembleForecast(station=station.icao, target_date=target_date, kind=kind, unit=unit, source=self.name)
            missing.errors.append(f"fixture forecast unit {found.unit} != market unit {unit}")
            return missing
        return found

    async def observations(self, station: Station, *, as_of: datetime, hours: int = 48) -> list[MetarObservation]:
        del hours
        return [o for o in self.metars.get(station.icao, []) if o.time <= as_of]


class PublicWeatherFeed:
    """Open-Meteo ensemble + aviationweather.gov METAR/stationinfo, read-only, keyless."""

    name = "open_meteo_plus_metar"

    def __init__(
        self,
        *,
        registry: StationRegistry | None = None,
        models: tuple[str, ...] = DEFAULT_MODELS,
        timeout: float = 20.0,
        http_get: Any | None = None,
        max_requests: int = 400,
    ) -> None:
        self.registry = registry
        self.models = models
        self.timeout = timeout
        self._http_get = http_get
        self.max_requests = max_requests
        self.requests_made = 0
        self.errors: list[str] = []
        self._station_cache: dict[str, Station | None] = {}
        self._metar_cache: dict[tuple[str, int], list[MetarObservation]] = {}

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        if self.requests_made >= self.max_requests:
            raise RuntimeError(f"request budget exhausted (max_requests={self.max_requests})")
        self.requests_made += 1
        if self._http_get is not None:
            return await self._http_get(url, params)
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    async def station(self, icao: str) -> Station | None:
        icao = icao.upper()
        if self.registry is not None and (found := self.registry.get(icao)) is not None:
            return found
        if icao in self._station_cache:
            return self._station_cache[icao]
        try:
            payload = await self._get(f"{AVIATIONWEATHER_URL}/stationinfo", {"ids": icao, "format": "json"})
            station = parse_station_info(payload).get(icao)
        except Exception as exc:  # a dead endpoint refuses the city-day, never kills the run
            self.errors.append(f"stationinfo[{icao}]: {type(exc).__name__}: {exc}")
            station = None
        self._station_cache[icao] = station
        return station

    async def forecast(self, station: Station, *, target_date: date, kind: str, unit: str, as_of: datetime) -> EnsembleForecast:
        del as_of
        params = {
            "latitude": station.latitude,
            "longitude": station.longitude,
            "daily": "temperature_2m_max" if kind == "high" else "temperature_2m_min",
            "models": ",".join(self.models),
            "temperature_unit": "fahrenheit" if unit == "F" else "celsius",
            "timezone": "auto",
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
        }
        try:
            payload = await self._get(OPEN_METEO_ENSEMBLE_URL, params)
        except Exception as exc:
            forecast = EnsembleForecast(station=station.icao, target_date=target_date, kind=kind, unit=unit)
            forecast.errors.append(f"open-meteo: {type(exc).__name__}: {exc}")
            self.errors.append(f"open-meteo[{station.icao} {target_date}]: {type(exc).__name__}: {exc}")
            return forecast
        if not isinstance(payload, dict):
            forecast = EnsembleForecast(station=station.icao, target_date=target_date, kind=kind, unit=unit)
            forecast.errors.append("open-meteo: unexpected payload shape")
            return forecast
        return parse_ensemble_payload(payload, station=station.icao, target_date=target_date, kind=kind, unit=unit)

    async def observations(self, station: Station, *, as_of: datetime, hours: int = 48) -> list[MetarObservation]:
        key = (station.icao, hours)
        if key in self._metar_cache:
            return self._metar_cache[key]
        try:
            payload = await self._get(f"{AVIATIONWEATHER_URL}/metar", {"ids": station.icao, "format": "json", "hours": hours})
            observations = [o for o in parse_metar_payload(payload, station=station.icao) if o.time <= as_of]
        except Exception as exc:
            self.errors.append(f"metar[{station.icao}]: {type(exc).__name__}: {exc}")
            observations = []
        self._metar_cache[key] = observations
        return observations

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "models": list(self.models),
            "requests_made": self.requests_made,
            "max_requests": self.max_requests,
            "errors": list(self.errors),
            "sources": FREE_SOURCES,
        }


def paid_source_policy(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Report (never act on) optional paid-vendor keys. Default and only state: disabled."""
    env = os.environ if environ is None else environ
    keys_present = [name for name in PAID_SOURCE_KEY_ENVS if (env.get(name) or "").strip()]
    requested = (env.get(PAID_SOURCE_ENABLE_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "enabled": False,
        "requested": requested,
        "keys_present": keys_present,
        "implemented": False,
        "note": (
            "Paid or keyed weather vendors are not implemented in this build; the default path is Open-Meteo "
            f"+ aviationweather.gov only. Setting {PAID_SOURCE_ENABLE_ENV}=true is recorded here and changes nothing."
        ),
    }


def build_weather_feed(*, use_fixtures: bool, registry: StationRegistry | None = None, models: tuple[str, ...] | None = None, max_requests: int = 400) -> WeatherFeed | None:
    """``None`` on fixture runs (the replay carries its own feed); the public feed otherwise."""
    if use_fixtures:
        return None
    return PublicWeatherFeed(registry=registry, models=models or DEFAULT_MODELS, max_requests=max_requests)


__all__ = [
    "AVIATIONWEATHER_URL",
    "DEFAULT_MODELS",
    "EnsembleForecast",
    "FREE_SOURCES",
    "FixtureWeatherFeed",
    "MetarObservation",
    "NullWeatherFeed",
    "OPEN_METEO_ENSEMBLE_URL",
    "ObservedExtreme",
    "PAID_SOURCE_ENABLE_ENV",
    "PAID_SOURCE_KEY_ENVS",
    "PublicWeatherFeed",
    "STATION_REGISTRY_PATH",
    "Station",
    "StationRegistry",
    "WeatherFeed",
    "build_weather_feed",
    "celsius_to",
    "metar_temperature_c",
    "observed_extreme",
    "paid_source_policy",
    "parse_ensemble_payload",
    "parse_metar_payload",
    "parse_station_info",
    "parse_time",
    "zone_for",
]
