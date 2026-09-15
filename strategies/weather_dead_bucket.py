"""Polymarket daily-temperature "dead bucket" logic (paper-only).

Polymarket lists one NegRisk event per city per day ("Highest temperature in
NYC on September 15?") whose legs are integer temperature buckets
("65°F or below", "66-67°F", ..., "84°F or higher"). Each leg settles on the
highest reading in the *hourly* observation table of one named airport
station for one *local* calendar date, in whole degrees of one unit.

The station publishes those same observations in real time (METAR / ASOS).
Once the running daily high is in, every bucket entirely **below** it is
impossible; late in the day, when the temperature is falling, every bucket
sufficiently **above** it is impossible too; once the first observation of
the following local date exists, the running high is the final high and the
one bucket that contains it is the (near-)certain YES. Markets often still
quote a few cents of probability on those legs. This module contains the
pure logic that decides *which* legs are dead and whether the quoted ask
leaves a net edge after the venue's taker fee; the track runner in
``research/weather_dead_bucket.py`` does the I/O, ledger and register work.

Shared types (documented so the sibling weather-bucket-edge track can reuse
them without redefining them)
------------------------------------------------------------------------------
* :class:`TemperatureUnit`     ``F`` or ``C`` - the unit the market settles in.
* :class:`TemperatureBucket`   closed integer interval ``[lo, hi]`` in the
  market's unit; ``lo is None`` = open below ("or below"), ``hi is None`` = open
  above ("or higher"). Parsed from Polymarket ``groupItemTitle`` labels.
* :class:`WeatherMarketSpec`   station ICAO, city, local date, unit,
  resolution source kind + URL, kind (``highest``); parsed from the event /
  market description. Fail-closed: any missing piece is a reason string.
* :class:`StationObservation`  one observation: station ICAO, UTC time,
  temperature in tenths of a degree Celsius (as reported by METAR ``T`` groups),
  report type (``METAR`` routine hourly or ``SPECI`` special).
* :class:`RunningHigh`         the day-so-far summary used by every kill rule.

Every rule here is deterministic and fails closed; nothing infers a station or
a date from a city name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.portfolio import Portfolio
from core.risk import RiskLimits, RiskManager
from core.types import ONE, ZERO, Market, Order, OrderBook, Outcome, Position, Side

TRACK = "weather_dead_bucket"
TRACK_LABEL = "Weather dead bucket (late-day METAR)"
Q4 = Decimal("0.0001")
_WHOLE = Decimal("1")

# Paper caps mirror the FLB lane: $25/order, 75 contracts (<= $75) per market, $75 daily loss.
WEATHER_RISK_LIMITS = RiskLimits(
    max_notional_per_order=Decimal("25"),
    max_position_per_market=Decimal("75"),
    max_daily_loss=Decimal("75"),
)
# Polymarket weather markets carry ``feeType = weather_fees`` -> 5 % taker rate
# (``venues/polymarket/fees.py``). Used only when a market has no rate metadata.
DEFAULT_WEATHER_TAKER_FEE_RATE = Decimal("0.05")


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4) if value is not None else None


# --------------------------------------------------------------------------
# Shared types
# --------------------------------------------------------------------------
class TemperatureUnit(StrEnum):
    F = "F"
    C = "C"


class MarketKind(StrEnum):
    HIGHEST = "highest"
    LOWEST = "lowest"


class ResolutionSourceKind(StrEnum):
    NOAA_WRH_TIMESERIES = "noaa_wrh_timeseries"  # https://www.weather.gov/wrh/timeseries?site=klga
    WUNDERGROUND_HISTORY = "wunderground_history"  # https://www.wunderground.com/history/daily/us/ca/los-angeles/KLAX
    UNSUPPORTED = "unsupported"  # Hong Kong Observatory tables, anything without an ICAO


@dataclass(frozen=True, slots=True)
class TemperatureBucket:
    """Closed integer interval in the market's unit. ``None`` marks an open end."""

    lo: int | None
    hi: int | None
    label: str = ""

    def __post_init__(self) -> None:
        if self.lo is None and self.hi is None:
            raise ValueError("a bucket needs at least one finite edge")
        if self.lo is not None and self.hi is not None and self.lo > self.hi:
            raise ValueError(f"bucket lo {self.lo} exceeds hi {self.hi}")

    def contains(self, value: int) -> bool:
        return (self.lo is None or value >= self.lo) and (self.hi is None or value <= self.hi)

    def entirely_below(self, value: int) -> bool:
        """Every temperature in the bucket is strictly below ``value``."""
        return self.hi is not None and self.hi < value

    def entirely_above(self, value: int) -> bool:
        """Every temperature in the bucket is strictly above ``value``."""
        return self.lo is not None and self.lo > value

    def as_dict(self) -> dict[str, Any]:
        return {"lo": self.lo, "hi": self.hi, "label": self.label}


_BUCKET_RANGE = re.compile(r"^\s*(-?\d+)\s*°?\s*(?:-|–|—|to)\s*(-?\d+)\s*°?\s*([FC])?\s*$", re.I)
_BUCKET_BELOW = re.compile(r"^\s*(-?\d+)\s*°?\s*([FC])?\s*(or\s+(below|lower|less|under)|and\s+below)\s*$", re.I)
_BUCKET_ABOVE = re.compile(r"^\s*(-?\d+)\s*°?\s*([FC])?\s*(or\s+(higher|above|more|greater)|and\s+above)\s*$", re.I)
_BUCKET_POINT = re.compile(r"^\s*(-?\d+)\s*°\s*([FC])\s*$", re.I)


def parse_bucket_label(label: str, unit: TemperatureUnit | None = None) -> TemperatureBucket | None:
    """``"66-67°F"`` -> [66, 67]; ``"65°F or below"`` -> [None, 65]; ``"84°F or higher"`` -> [84, None].

    Returns ``None`` when the label is not a temperature bucket or names a
    different unit than ``unit`` (fail closed on a unit mismatch).
    """
    text = label.replace("\u00b0", "°").replace("º", "°").strip()
    for pattern, kind in ((_BUCKET_RANGE, "range"), (_BUCKET_BELOW, "below"), (_BUCKET_ABOVE, "above"), (_BUCKET_POINT, "point")):
        match = pattern.match(text)
        if match is None:
            continue
        groups = match.groups()
        found_unit = groups[2] if kind == "range" else groups[1]
        if found_unit and unit is not None and found_unit.upper() != unit.value:
            return None
        try:
            if kind == "range":
                return TemperatureBucket(int(groups[0]), int(groups[1]), label)
            if kind == "below":
                return TemperatureBucket(None, int(groups[0]), label)
            if kind == "above":
                return TemperatureBucket(int(groups[0]), None, label)
            return TemperatureBucket(int(groups[0]), int(groups[0]), label)
        except ValueError:
            return None
    return None


@dataclass(frozen=True, slots=True)
class WeatherMarketSpec:
    """What one daily-temperature event settles on. Every field is parsed, never inferred."""

    station_icao: str
    city: str
    local_date: date
    unit: TemperatureUnit
    kind: MarketKind
    resolution_source: ResolutionSourceKind
    source_url: str | None
    timezone: str
    hourly_only: bool = True  # "resolve off of the Hourly Data" clause present

    def as_dict(self) -> dict[str, Any]:
        return {
            "station_icao": self.station_icao,
            "city": self.city,
            "local_date": self.local_date.isoformat(),
            "unit": self.unit.value,
            "kind": self.kind.value,
            "resolution_source": self.resolution_source.value,
            "source_url": self.source_url,
            "timezone": self.timezone,
            "hourly_only": self.hourly_only,
        }


@dataclass(frozen=True, slots=True)
class SpecParse:
    spec: WeatherMarketSpec | None
    reason: str  # parsed | market_kind_unsupported | station_unparsed | date_unparsed | unit_unparsed | station_timezone_unknown
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.spec is not None


_TITLE = re.compile(r"^\s*(Highest|Lowest)\s+temperature\s+in\s+(.+?)\s+on\s+([A-Za-z]+\s+\d{1,2})\??\s*$", re.I)
_WRH_SITE = re.compile(r"weather\.gov/wrh/timeseries\?[^\s\"')]*?site=([A-Za-z0-9]{3,5})", re.I)
_WUNDERGROUND = re.compile(r"wunderground\.com/history/daily/[^\s\"')]*?/([A-Z0-9]{4})(?:[/?\s\"'),.;]|$)")
_DESC_DATE = re.compile(r"\bon\s+(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+'?(\d{2,4})\b")
_DESC_UNIT = re.compile(r"degrees?\s+(Fahrenheit|Celsius)", re.I)
_MONTHS = {m.lower(): i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}
_MONTHS.update({m.lower(): i for i, m in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), start=1)})


def _parse_desc_date(text: str) -> date | None:
    match = _DESC_DATE.search(text)
    if match is None:
        return None
    day, month_name, year = match.groups()
    month = _MONTHS.get(month_name.lower()) or _MONTHS.get(month_name[:3].lower())
    if month is None:
        return None
    year_int = int(year)
    if year_int < 100:
        year_int += 2000
    try:
        return date(year_int, month, int(day))
    except ValueError:
        return None


def parse_weather_market(
    *,
    event_title: str,
    description: str,
    resolution_source_url: str | None = None,
    station_timezones: dict[str, str] | None = None,
) -> SpecParse:
    """Parse the settlement spec of one Polymarket daily-temperature event.

    Station comes only from a recognised resolution URL (NOAA WRH ``site=``
    or a Weather Underground history path ending in an ICAO); the date and
    the unit come only from the description's own resolution sentence. Any
    gap is a refusal reason; nothing is guessed from the city name.
    """
    zones = station_timezones if station_timezones is not None else STATION_TIMEZONES
    title_match = _TITLE.match(event_title or "")
    kind = MarketKind.LOWEST if title_match and title_match.group(1).lower() == "lowest" else MarketKind.HIGHEST
    city = title_match.group(2).strip() if title_match else (event_title or "").strip()
    if title_match is None and not re.search(r"highest temperature", description or "", re.I):
        return SpecParse(None, "market_kind_unsupported", "title is not a daily highest/lowest temperature question")
    if kind is MarketKind.LOWEST or re.search(r"\blowest temperature\b", description or "", re.I):
        return SpecParse(None, "market_kind_unsupported", "lowest-temperature markets are not covered by the dead-bucket rules")

    haystack = " ".join(part for part in (resolution_source_url or "", description or "") if part)
    station: str | None = None
    source_kind = ResolutionSourceKind.UNSUPPORTED
    source_url: str | None = None
    if (m := _WRH_SITE.search(haystack)) is not None:
        station, source_kind = m.group(1).upper(), ResolutionSourceKind.NOAA_WRH_TIMESERIES
        source_url = f"https://www.weather.gov/wrh/timeseries?site={station.lower()}"
    elif (m := _WUNDERGROUND.search(haystack)) is not None:
        station, source_kind = m.group(1).upper(), ResolutionSourceKind.WUNDERGROUND_HISTORY
        url_match = re.search(r"https?://www\.wunderground\.com/history/daily/\S+?" + m.group(1), haystack)
        source_url = url_match.group(0) if url_match else None
    if station is None or len(station) != 4:
        return SpecParse(None, "station_unparsed", "no NOAA WRH site= or Weather Underground ICAO in the resolution text")

    local_date = _parse_desc_date(description or "")
    if local_date is None:
        return SpecParse(None, "date_unparsed", "no \"on DD Mon 'YY\" date in the resolution text")
    unit_match = _DESC_UNIT.search(description or "")
    if unit_match is None:
        return SpecParse(None, "unit_unparsed", "no 'degrees Fahrenheit/Celsius' in the resolution text")
    unit = TemperatureUnit.F if unit_match.group(1).lower().startswith("f") else TemperatureUnit.C
    timezone = zones.get(station)
    if timezone is None:
        return SpecParse(None, "station_timezone_unknown", f"{station} is not in the station timezone registry")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        return SpecParse(None, "station_timezone_unknown", f"{timezone} is not an installed IANA zone")
    hourly_only = bool(re.search(r"hourly data", description or "", re.I))
    return SpecParse(
        WeatherMarketSpec(
            station_icao=station,
            city=city,
            local_date=local_date,
            unit=unit,
            kind=kind,
            resolution_source=source_kind,
            source_url=source_url,
            timezone=timezone,
            hourly_only=hourly_only,
        ),
        "parsed",
    )


# ICAO -> IANA zone for the stations Polymarket has used (read from the live
# descriptions on 2026-09-15) plus a few obvious neighbours. Unknown stations
# are refused (``station_timezone_unknown``); nothing is looked up online.
STATION_TIMEZONES: dict[str, str] = {
    # United States
    "KLGA": "America/New_York", "KJFK": "America/New_York", "KNYC": "America/New_York", "KEWR": "America/New_York",
    "KATL": "America/New_York", "KMIA": "America/New_York", "KFLL": "America/New_York", "KBOS": "America/New_York",
    "KDCA": "America/New_York", "KPHL": "America/New_York",
    "KORD": "America/Chicago", "KMDW": "America/Chicago", "KDAL": "America/Chicago", "KDFW": "America/Chicago",
    "KAUS": "America/Chicago", "KHOU": "America/Chicago", "KIAH": "America/Chicago", "KMSP": "America/Chicago",
    "KBKF": "America/Denver", "KDEN": "America/Denver", "KPHX": "America/Phoenix", "KLAS": "America/Los_Angeles",
    "KSEA": "America/Los_Angeles", "KLAX": "America/Los_Angeles", "KSFO": "America/Los_Angeles", "KSAN": "America/Los_Angeles",
    "KPDX": "America/Los_Angeles",
    # Americas
    "CYYZ": "America/Toronto", "CYVR": "America/Vancouver", "MMMX": "America/Mexico_City", "MPMG": "America/Panama",
    "SBGR": "America/Sao_Paulo", "SAEZ": "America/Argentina/Buenos_Aires", "SCEL": "America/Santiago", "SKBO": "America/Bogota",
    # Europe / Middle East / Africa
    "EGLC": "Europe/London", "EGLL": "Europe/London", "LFPB": "Europe/Paris", "LFPG": "Europe/Paris", "EHAM": "Europe/Amsterdam",
    "EDDM": "Europe/Berlin", "EDDB": "Europe/Berlin", "LIMC": "Europe/Rome", "LEMD": "Europe/Madrid", "EPWA": "Europe/Warsaw",
    "EFHK": "Europe/Helsinki", "LTAC": "Europe/Istanbul", "LTFM": "Europe/Istanbul", "UUWW": "Europe/Moscow", "UUEE": "Europe/Moscow",
    "LLBG": "Asia/Jerusalem", "OMDB": "Asia/Dubai", "FACT": "Africa/Johannesburg", "HECA": "Africa/Cairo",
    # Asia / Pacific
    "RJTT": "Asia/Tokyo", "RJAA": "Asia/Tokyo", "RKSI": "Asia/Seoul", "RKPK": "Asia/Seoul", "ZBAA": "Asia/Shanghai",
    "ZSPD": "Asia/Shanghai", "ZGSZ": "Asia/Shanghai", "ZHHH": "Asia/Shanghai", "ZUUU": "Asia/Shanghai", "ZUCK": "Asia/Shanghai",
    "ZSJN": "Asia/Shanghai", "ZHCC": "Asia/Shanghai", "VHHH": "Asia/Hong_Kong", "RCSS": "Asia/Taipei", "RCTP": "Asia/Taipei",
    "WSSS": "Asia/Singapore", "WMKK": "Asia/Kuala_Lumpur", "VILK": "Asia/Kolkata", "VIDP": "Asia/Kolkata", "VABB": "Asia/Kolkata",
    "NZWN": "Pacific/Auckland", "NZAA": "Pacific/Auckland", "YSSY": "Australia/Sydney",
}


@dataclass(frozen=True, slots=True)
class StationObservation:
    """One station observation. ``temp_tenths_c`` is the METAR ``T`` group value (tenths of °C)."""

    station_icao: str
    observed_at: datetime  # UTC
    temp_tenths_c: int
    report_type: str = "METAR"  # METAR (routine hourly) | SPECI (special)
    raw: str = ""

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.report_type not in ("METAR", "SPECI"):
            raise ValueError(f"report_type {self.report_type!r} must be METAR or SPECI")

    @property
    def temp_c(self) -> Decimal:
        return Decimal(self.temp_tenths_c) / Decimal(10)

    def local_time(self, timezone: str) -> datetime:
        return self.observed_at.astimezone(ZoneInfo(timezone))

    def as_dict(self, timezone: str | None = None) -> dict[str, Any]:
        out = {
            "station": self.station_icao,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "temp_c": str(self.temp_c),
            "report_type": self.report_type,
            "raw": self.raw,
        }
        if timezone:
            out["local_time"] = self.local_time(timezone).isoformat()
        return out


# --------------------------------------------------------------------------
# METAR temperature parsing and unit conversion
# --------------------------------------------------------------------------
_T_GROUP = re.compile(r"(?:^|\s)T([01])(\d{3})([01])(\d{3})(?=\s|$)")
_BODY_TEMP = re.compile(r"(?:^|\s)(M?\d{2})/(M?\d{2})?(?=\s|$)")
_OBS_TIME = re.compile(r"(?:^|\s)(\d{2})(\d{2})(\d{2})Z(?=\s|$)")


def parse_metar_temperature_tenths_c(raw: str) -> int | None:
    """Temperature in tenths of °C from a raw METAR: the ``T`` group when present, else the body.

    ``T02330178`` -> 233 (23.3 °C); body ``23/07`` -> 230. Returns ``None`` when
    neither is readable (fail closed; the observation is dropped).
    """
    if (m := _T_GROUP.search(raw)) is not None:
        sign = -1 if m.group(1) == "1" else 1
        return sign * int(m.group(2))
    if (m := _BODY_TEMP.search(raw)) is not None:
        body = m.group(1)
        value = int(body.lstrip("M"))
        return (-value if body.startswith("M") else value) * 10
    return None


def parse_metar_report_type(raw: str) -> str:
    return "SPECI" if raw.strip().upper().startswith("SPECI") else "METAR"


def parse_metar_observation(raw: str, *, station: str | None = None, observed_at: datetime | None = None, month_anchor: datetime | None = None) -> StationObservation | None:
    """Build a :class:`StationObservation` from raw METAR text.

    ``observed_at`` is used when given; otherwise the ``DDHHMMZ`` group is
    resolved against ``month_anchor`` (a UTC datetime in the observation's
    month). Missing station / time / temperature returns ``None``.
    """
    tokens = raw.split()
    if not tokens:
        return None
    # The station named in the report text wins; ``station`` only fills a gap
    # (so a source answering with another station's report stays visible).
    icao = None
    for token in tokens[:3]:
        if token in ("METAR", "SPECI"):
            continue
        if re.fullmatch(r"[A-Z][A-Z0-9]{3}", token):
            icao = token
        break
    if icao is None:
        icao = station
    if icao is None:
        return None
    when = observed_at
    if when is None:
        if month_anchor is None or (m := _OBS_TIME.search(raw)) is None:
            return None
        day, hour, minute = (int(g) for g in m.groups())
        try:
            when = month_anchor.astimezone(UTC).replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
    tenths = parse_metar_temperature_tenths_c(raw)
    if tenths is None:
        return None
    return StationObservation(icao.upper(), when.astimezone(UTC), tenths, parse_metar_report_type(raw), raw.strip())


def tenths_c_to_unit(tenths_c: int, unit: TemperatureUnit) -> tuple[int, int]:
    """Whole degrees in ``unit`` as ``(low, high)``: equal unless the exact value sits on a half.

    The resolution table shows whole degrees rounded from the tenths-precision
    observation; its rounding convention at exactly .5 is not documented, so
    both candidates are carried and every rule below uses the conservative one.
    """
    celsius = Decimal(tenths_c) / Decimal(10)
    value = celsius * Decimal(9) / Decimal(5) + Decimal(32) if unit is TemperatureUnit.F else celsius
    # Ties round toward zero under HALF_DOWN and away from it under HALF_UP, so
    # order the pair explicitly (matters below zero: -0.5 -> (-1, 0)).
    candidates = sorted(int(value.quantize(_WHOLE, rounding=mode)) for mode in (ROUND_HALF_DOWN, ROUND_HALF_UP))
    return candidates[0], candidates[-1]


# --------------------------------------------------------------------------
# Running high
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RunningHigh:
    """Day-so-far summary of one station for one local date, in the market's unit.

    ``high_low``/``high_high`` are the conservative pair for the hourly (routine
    METAR) running high; ``all_high_high`` includes SPECI specials (a special can
    show a warmer air mass that the hourly table will not print, so the upper
    kill uses it). ``day_complete`` is true once an hourly observation from the
    following local date exists - the market's own resolution trigger.
    """

    station_icao: str
    local_date: date
    unit: TemperatureUnit
    observations: int
    hourly_observations: int
    high_low: int | None
    high_high: int | None
    all_high_high: int | None
    latest_observed_at: datetime | None
    latest_temp_low: int | None
    latest_temp_high: int | None
    trend: str  # falling | flat | rising | unknown
    consecutive_nonrising: int
    day_complete: bool
    first_next_day_observation: datetime | None
    peak_observed_at: datetime | None

    @property
    def has_high(self) -> bool:
        return self.high_low is not None and self.high_high is not None

    @property
    def high_ambiguous(self) -> bool:
        return self.has_high and self.high_low != self.high_high

    def as_dict(self, timezone: str | None = None) -> dict[str, Any]:
        def local(ts: datetime | None) -> str | None:
            if ts is None:
                return None
            return ts.astimezone(ZoneInfo(timezone)).isoformat() if timezone else ts.isoformat()

        return {
            "station": self.station_icao,
            "local_date": self.local_date.isoformat(),
            "unit": self.unit.value,
            "observations": self.observations,
            "hourly_observations": self.hourly_observations,
            "running_high": self.high_low if not self.high_ambiguous else None,
            "running_high_low": self.high_low,
            "running_high_high": self.high_high,
            "running_high_all_reports_high": self.all_high_high,
            "latest_observed_at_local": local(self.latest_observed_at),
            "latest_temp_low": self.latest_temp_low,
            "latest_temp_high": self.latest_temp_high,
            "trend": self.trend,
            "consecutive_nonrising": self.consecutive_nonrising,
            "day_complete": self.day_complete,
            "first_next_day_observation_local": local(self.first_next_day_observation),
            "peak_observed_at_local": local(self.peak_observed_at),
        }


def local_day_window(local_date: date, timezone: str) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` of a station's local calendar date."""
    zone = ZoneInfo(timezone)
    start = datetime(local_date.year, local_date.month, local_date.day, tzinfo=zone)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def running_high(observations: list[StationObservation] | tuple[StationObservation, ...], spec: WeatherMarketSpec) -> RunningHigh:
    """Fold a station's observations into the day-so-far summary for ``spec``.

    Observations from other stations are ignored here (the runner refuses the
    whole event with ``station_mismatch`` before calling this); observations
    outside the local date only matter as the "first data point of the
    following date" that completes the day.
    """
    zone = ZoneInfo(spec.timezone)
    start, end = local_day_window(spec.local_date, spec.timezone)
    same_station = sorted((o for o in observations if o.station_icao == spec.station_icao), key=lambda o: o.observed_at)
    in_day = [o for o in same_station if start <= o.observed_at < end]
    hourly = [o for o in in_day if o.report_type == "METAR"] if spec.hourly_only else in_day
    next_day = [o for o in same_station if o.observed_at >= end and (o.report_type == "METAR" or not spec.hourly_only)]

    def pair(o: StationObservation) -> tuple[int, int]:
        return tenths_c_to_unit(o.temp_tenths_c, spec.unit)

    high_low = high_high = all_high_high = None
    peak_at: datetime | None = None
    for o in hourly:
        lo, hi = pair(o)
        if high_high is None or hi > high_high or (hi == high_high and lo > (high_low or lo)):
            high_low, high_high, peak_at = lo, hi, o.observed_at
    for o in in_day:
        _, hi = pair(o)
        all_high_high = hi if all_high_high is None else max(all_high_high, hi)

    latest = hourly[-1] if hourly else None
    latest_low, latest_high = pair(latest) if latest else (None, None)
    trend = "unknown"
    nonrising = 0
    if len(hourly) >= 2:
        series = [pair(o)[1] for o in hourly]  # compare on the same rounding side
        for prev, cur in zip(reversed(series[:-1]), reversed(series[1:]), strict=False):
            if cur <= prev:
                nonrising += 1
            else:
                break
        last, before = series[-1], series[-2]
        trend = "falling" if last < before else ("rising" if last > before else "flat")
    return RunningHigh(
        station_icao=spec.station_icao,
        local_date=spec.local_date,
        unit=spec.unit,
        observations=len(in_day),
        hourly_observations=len(hourly),
        high_low=high_low,
        high_high=high_high,
        all_high_high=all_high_high,
        latest_observed_at=latest.observed_at if latest else None,
        latest_temp_low=latest_low,
        latest_temp_high=latest_high,
        trend=trend,
        consecutive_nonrising=nonrising,
        day_complete=bool(next_day),
        first_next_day_observation=next_day[0].observed_at if next_day else None,
        peak_observed_at=peak_at,
    )


# --------------------------------------------------------------------------
# Parameters (pre-registered defaults; see docs/WEATHER_DEAD_BUCKET.md)
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DeadBucketParameters:
    min_net_edge: Decimal = Decimal("0.02")  # per contract, after the venue taker fee
    late_day_local_hour: int = 17  # upper kill only at or after this local hour
    falling_margin_f: int = 2  # latest hourly temp must be this far below the running high (°F markets)
    falling_margin_c: int = 1  # (°C markets)
    min_consecutive_nonrising: int = 2  # last N hourly observations must not rise
    upper_headroom_degrees: int = 1  # buckets must start more than this above the (all-report) high
    max_obs_age_minutes: int = 90  # latest hourly observation must be at most this old (unless the day is complete)
    min_hourly_observations: int = 3
    max_order_notional: Decimal = WEATHER_RISK_LIMITS.max_notional_per_order
    min_order_size: Decimal = Decimal("5")  # Polymarket CLOB minimum
    # Pre-registered evaluation
    min_settled_positions: int = 30
    min_station_days: int = 10
    pass_mean_net_per_contract: Decimal = Decimal("0.02")

    def __post_init__(self) -> None:
        if not ZERO <= self.min_net_edge < ONE:
            raise ValueError("min_net_edge must be in [0, 1)")
        if not 0 <= self.late_day_local_hour <= 23:
            raise ValueError("late_day_local_hour must be an hour of the day")
        for name in ("falling_margin_f", "falling_margin_c", "upper_headroom_degrees", "min_consecutive_nonrising", "max_obs_age_minutes", "min_hourly_observations", "min_settled_positions", "min_station_days"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if self.max_order_notional <= ZERO or self.min_order_size <= ZERO:
            raise ValueError("order sizes must be positive")

    def falling_margin(self, unit: TemperatureUnit) -> int:
        return self.falling_margin_f if unit is TemperatureUnit.F else self.falling_margin_c

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_net_edge": self.min_net_edge,
            "late_day_local_hour": self.late_day_local_hour,
            "falling_margin_f": self.falling_margin_f,
            "falling_margin_c": self.falling_margin_c,
            "min_consecutive_nonrising": self.min_consecutive_nonrising,
            "upper_headroom_degrees": self.upper_headroom_degrees,
            "max_obs_age_minutes": self.max_obs_age_minutes,
            "min_hourly_observations": self.min_hourly_observations,
            "max_order_notional": self.max_order_notional,
            "min_order_size": self.min_order_size,
            "min_settled_positions": self.min_settled_positions,
            "min_station_days": self.min_station_days,
            "pass_mean_net_per_contract": self.pass_mean_net_per_contract,
        }


# --------------------------------------------------------------------------
# Kill rules
# --------------------------------------------------------------------------
class KillRule(StrEnum):
    DEAD_BELOW_RUNNING_HIGH = "dead_below_running_high"  # bucket.hi < running high (any time of day)
    DEAD_ABOVE_LATE_DAY = "dead_above_late_day"  # late day, falling, bucket.lo > high + headroom
    DEAD_DAY_COMPLETE = "dead_day_complete"  # next-day observation exists, bucket does not contain the final high
    CERTAIN_YES_DAY_COMPLETE = "certain_yes_day_complete"  # next-day observation exists, bucket contains the final high
    CERTAIN_YES_LATE_DAY = "certain_yes_late_day"  # late day, falling, bucket covers [high, all_high + headroom]


@dataclass(frozen=True, slots=True)
class BucketVerdict:
    bucket: TemperatureBucket
    status: str  # dead | certain_yes | live | too_early_in_day | not_falling | bucket_edge_ambiguous | insufficient_observations
    rule: KillRule | None = None
    outcome: Outcome | None = None  # the outcome we would BUY (NO on dead, YES on certain)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"bucket": self.bucket.as_dict(), "status": self.status, "rule": self.rule.value if self.rule else None, "buy_outcome": self.outcome.value if self.outcome else None, "detail": self.detail}


def _late_day(high: RunningHigh, spec: WeatherMarketSpec, params: DeadBucketParameters, as_of: datetime) -> tuple[bool, str]:
    """(late-day-and-falling, reason when not)."""
    local_now = as_of.astimezone(ZoneInfo(spec.timezone))
    if local_now.date() < spec.local_date:
        return False, "too_early_in_day"
    if local_now.date() == spec.local_date and local_now.hour < params.late_day_local_hour:
        return False, "too_early_in_day"
    if high.latest_temp_high is None or high.high_low is None:
        return False, "insufficient_observations"
    if high.latest_temp_high > high.high_low - params.falling_margin(spec.unit):
        return False, "not_falling"
    if high.consecutive_nonrising < params.min_consecutive_nonrising:
        return False, "not_falling"
    return True, ""


def classify_bucket(bucket: TemperatureBucket, high: RunningHigh, spec: WeatherMarketSpec, params: DeadBucketParameters, *, as_of: datetime) -> BucketVerdict:
    """Decide whether one bucket is dead (buy NO), certain (buy YES) or must be left alone.

    Order of the rules is fixed and every ambiguity refuses:

    1. ``day_complete``: the final high is known. Bucket contains both rounding
       candidates -> certain YES; contains neither -> dead; straddles -> refuse.
    2. Bucket entirely below the *low* rounding candidate of the hourly running
       high -> dead (holds at any hour; the high can only rise).
    3. Late day and falling: bucket entirely above ``all_high_high + headroom``
       -> dead; bucket covering ``[high_low, all_high_high + headroom]`` -> certain
       YES; otherwise live.
    4. Anything else is live / too early / not falling and never traded.
    """
    if not high.has_high or high.hourly_observations < params.min_hourly_observations:
        return BucketVerdict(bucket, "insufficient_observations", detail=f"{high.hourly_observations} hourly observations")
    assert high.high_low is not None and high.high_high is not None
    if high.day_complete:
        contains_low, contains_high = bucket.contains(high.high_low), bucket.contains(high.high_high)
        if contains_low and contains_high:
            return BucketVerdict(bucket, "certain_yes", KillRule.CERTAIN_YES_DAY_COMPLETE, Outcome.YES, f"final high {high.high_low}..{high.high_high} inside bucket")
        if not contains_low and not contains_high:
            return BucketVerdict(bucket, "dead", KillRule.DEAD_DAY_COMPLETE, Outcome.NO, f"final high {high.high_low}..{high.high_high} outside bucket")
        return BucketVerdict(bucket, "bucket_edge_ambiguous", detail=f"final high rounds to {high.high_low} or {high.high_high} across the bucket edge")
    if bucket.entirely_below(high.high_low):
        return BucketVerdict(bucket, "dead", KillRule.DEAD_BELOW_RUNNING_HIGH, Outcome.NO, f"bucket hi {bucket.hi} < running high {high.high_low}")
    if bucket.hi is not None and high.high_low <= bucket.hi < high.high_high:
        return BucketVerdict(bucket, "bucket_edge_ambiguous", detail=f"running high rounds to {high.high_low} or {high.high_high} across the bucket edge")
    late, why = _late_day(high, spec, params, as_of)
    if not late:
        return BucketVerdict(bucket, why, detail=f"latest {high.latest_temp_high} vs high {high.high_low}, trend {high.trend}")
    ceiling = (high.all_high_high if high.all_high_high is not None else high.high_high) + params.upper_headroom_degrees
    if bucket.entirely_above(ceiling):
        return BucketVerdict(bucket, "dead", KillRule.DEAD_ABOVE_LATE_DAY, Outcome.NO, f"bucket lo {bucket.lo} > high {high.all_high_high} + headroom {params.upper_headroom_degrees}")
    if bucket.contains(high.high_low) and bucket.contains(ceiling):
        return BucketVerdict(bucket, "certain_yes", KillRule.CERTAIN_YES_LATE_DAY, Outcome.YES, f"bucket covers {high.high_low}..{ceiling}")
    return BucketVerdict(bucket, "live", detail=f"bucket overlaps the plausible range {high.high_low}..{ceiling}")


# --------------------------------------------------------------------------
# Edge after fees and order construction
# --------------------------------------------------------------------------
def taker_fee_rate_for(market: Market) -> Decimal:
    raw = market.metadata.get("taker_fee_rate")
    if raw in (None, ""):
        return DEFAULT_WEATHER_TAKER_FEE_RATE
    try:
        rate = Decimal(str(raw))
    except ArithmeticError:
        return DEFAULT_WEATHER_TAKER_FEE_RATE
    return rate if rate >= ZERO else DEFAULT_WEATHER_TAKER_FEE_RATE


def fee_per_contract(price: Decimal, rate: Decimal) -> Decimal:
    """Polymarket taker fee per contract at ``price``: ``rate * p * (1 - p)``."""
    return rate * price * (ONE - price)


def net_edge_per_contract(ask: Decimal, rate: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """``(gross, fee, net)`` for buying one contract of a certain outcome at ``ask``."""
    gross = ONE - ask
    fee = fee_per_contract(ask, rate)
    return gross, fee, gross - fee


def whole_contracts(quantity: Decimal) -> Decimal:
    return quantity.quantize(_WHOLE, rounding=ROUND_DOWN)


@dataclass(frozen=True, slots=True)
class DeadBucketEvaluation:
    market_id: str
    verdict: BucketVerdict
    reason: str  # trade | <verdict.status> | no_ask | edge_below_threshold | insufficient_depth | below_min_order_size | ...
    ask: Decimal | None = None
    ask_size: Decimal | None = None
    # Best bid in the traded outcome's view: on live weather books the dead
    # legs usually show NO bids (= YES asks at a few tenths of a cent) and no
    # NO ask at all, so a resting order is the only way in. Reported, not traded.
    bid: Decimal | None = None
    bid_size: Decimal | None = None
    gross_edge: Decimal | None = None
    fee_per_contract: Decimal | None = None
    net_edge: Decimal | None = None
    fee_rate: Decimal | None = None
    quantity: Decimal = ZERO
    orders: tuple[Order, ...] = ()

    @property
    def traded(self) -> bool:
        return bool(self.orders)

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            **self.verdict.as_dict(),
            "reason": self.reason,
            "ask": _q(self.ask),
            "ask_size": self.ask_size,
            "bid": _q(self.bid),
            "bid_size": self.bid_size,
            "gross_edge": _q(self.gross_edge),
            "fee_per_contract": _q(self.fee_per_contract),
            "net_edge": _q(self.net_edge),
            "fee_rate": self.fee_rate,
            "quantity": self.quantity,
            "orders": len(self.orders),
        }


class DeadBucketStrategy:
    """Turn a bucket verdict plus the venue's book into at most one taker order.

    Dead buckets are bought as NO at the NO ask (the venue's real NO ladder
    when the snapshot has one; the complement of the YES bid otherwise);
    certain buckets are bought as YES at the YES ask. Sizing is whole
    contracts limited by touch depth, ``$25`` per order, the ``RiskManager``
    (75 contracts per market, $75 daily loss) and the venue minimum.
    """

    name = TRACK

    def __init__(self, parameters: DeadBucketParameters | None = None, *, portfolio: Portfolio | None = None, risk: RiskManager | None = None) -> None:
        self.parameters = parameters or DeadBucketParameters()
        self.portfolio = portfolio
        self.risk = risk

    def evaluate(
        self,
        market: Market,
        yes_book: OrderBook,
        no_book: OrderBook | None,
        verdict: BucketVerdict,
        *,
        spec: WeatherMarketSpec,
        high: RunningHigh,
    ) -> DeadBucketEvaluation:
        if verdict.outcome is None:
            return DeadBucketEvaluation(market.market_id, verdict, verdict.status)
        if not market.active:
            return DeadBucketEvaluation(market.market_id, verdict, "market_inactive")
        if verdict.outcome is Outcome.YES:
            view = yes_book
        elif no_book is not None and (no_book.best_ask is not None or no_book.best_bid is not None):
            view = no_book
        else:
            # The CLOB matches complementary orders, so a YES bid *is* a NO ask at 1 - p.
            view = yes_book.for_outcome(Outcome.NO)
        bid = view.best_bid
        side_info = {"bid": bid.price if bid else None, "bid_size": bid.size if bid else None}
        touch = view.best_ask
        if touch is None or not ZERO < touch.price < ONE:
            return DeadBucketEvaluation(market.market_id, verdict, "no_ask", **side_info)
        rate = taker_fee_rate_for(market)
        gross, fee, net = net_edge_per_contract(touch.price, rate)
        common = {"ask": touch.price, "ask_size": touch.size, "gross_edge": gross, "fee_per_contract": fee, "net_edge": net, "fee_rate": rate, **side_info}
        params = self.parameters
        if net < params.min_net_edge:
            return DeadBucketEvaluation(market.market_id, verdict, "edge_below_threshold", **common)
        if touch.size < params.min_order_size:
            return DeadBucketEvaluation(market.market_id, verdict, "insufficient_depth", **common)
        position = self.portfolio.get(market.venue, market.market_id) if self.portfolio else None
        probe = Order(venue=market.venue, market_id=market.market_id, side=Side.BUY, quantity=_WHOLE, outcome=verdict.outcome, price=touch.price)
        quantity = min(touch.size, params.max_order_notional / touch.price)
        if self.risk is not None:
            quantity = min(quantity, self.risk.remaining_order_capacity(probe, position))
        quantity = whole_contracts(quantity)
        if quantity <= ZERO:
            if self.risk is not None and self.risk.halted:
                return DeadBucketEvaluation(market.market_id, verdict, "risk_halted", **common)
            return DeadBucketEvaluation(market.market_id, verdict, "no_position_headroom", **common)
        if quantity < params.min_order_size:
            return DeadBucketEvaluation(market.market_id, verdict, "below_min_order_size", quantity=quantity, **common)
        order = Order(
            venue=market.venue,
            market_id=market.market_id,
            side=Side.BUY,
            quantity=quantity,
            outcome=verdict.outcome,
            price=touch.price,
            metadata={
                "strategy": self.name,
                "execution": "taker",
                "kill_rule": verdict.rule.value if verdict.rule else "",
                "bucket": verdict.bucket.label,
                "station": spec.station_icao,
                "local_date": spec.local_date.isoformat(),
                "unit": spec.unit.value,
                "running_high_low": str(high.high_low),
                "running_high_high": str(high.high_high),
                "gross_edge": str(_q(gross)),
                "net_edge": str(_q(net)),
                "fee_rate": str(rate),
                "price_signal_status": "station_observations_public",
            },
        )
        return DeadBucketEvaluation(market.market_id, verdict, "trade", quantity=quantity, orders=(order,), **common)


def position_cash_at_risk(position: Position | None) -> Decimal:
    if position is None or position.quantity == ZERO:
        return ZERO
    unit = position.average_price if position.quantity > ZERO else ONE - position.average_price
    return abs(position.quantity) * unit


# --------------------------------------------------------------------------
# Pre-registered verdict over settled records
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SettledRecord:
    """The slice of a register record the verdict needs."""

    station_day: str  # "KLGA:2026-09-15"
    rule: KillRule
    buy_outcome: Outcome
    quantity: Decimal
    net_pnl: Decimal  # realised, fees deducted
    won: bool


@dataclass(frozen=True, slots=True)
class Verdict:
    status: str  # PASS | FAIL | INSUFFICIENT_DATA
    n: int
    station_days: int
    mean_net_per_contract: Decimal | None
    median_net_per_contract: Decimal | None
    losses: int
    loss_rate: Decimal | None
    kill_rule_triggered: bool
    total_net_pnl: Decimal
    contracts: Decimal
    by_rule: dict[str, dict[str, Any]]
    pre_registered: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "n": self.n,
            "station_days": self.station_days,
            "mean_net_per_contract": _q(self.mean_net_per_contract),
            "median_net_per_contract": _q(self.median_net_per_contract),
            "losses": self.losses,
            "loss_rate": _q(self.loss_rate),
            "kill_rule_triggered": self.kill_rule_triggered,
            "total_net_pnl": _q(self.total_net_pnl),
            "contracts": self.contracts,
            "by_rule": self.by_rule,
            "pre_registered": self.pre_registered,
        }


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def verdict(records: list[SettledRecord], *, parameters: DeadBucketParameters | None = None, side: Outcome | None = Outcome.NO) -> Verdict:
    """Pre-registered rule over settled positions (default: the dead-bucket NO sample).

    PASS when ``n >= 30`` settled positions across ``>= 10`` station-days, mean
    net PnL per contract ``>= 0.02`` **and** zero losses. A single loss on a
    "dead" bucket (which pays ~97c to win ~3c) triggers the kill rule at any
    ``n`` and makes the verdict FAIL once ``n`` is reached. Below ``n`` or the
    station-day floor the verdict is INSUFFICIENT_DATA, never pass/fail.
    """
    params = parameters or DeadBucketParameters()
    sample = [r for r in records if side is None or r.buy_outcome is side]
    n = len(sample)
    contracts = sum((r.quantity for r in sample), ZERO)
    total = sum((r.net_pnl for r in sample), ZERO)
    per_contract = [r.net_pnl / r.quantity for r in sample if r.quantity > ZERO]
    mean = (total / contracts) if contracts > ZERO else None
    losses = sum(1 for r in sample if not r.won)
    station_days = len({r.station_day for r in sample})
    kill = losses > 0
    if n < params.min_settled_positions or station_days < params.min_station_days:
        status = "INSUFFICIENT_DATA"
    elif kill or mean is None or mean < params.pass_mean_net_per_contract:
        status = "FAIL"
    else:
        status = "PASS"
    by_rule: dict[str, dict[str, Any]] = {}
    for rule in KillRule:
        rows = [r for r in sample if r.rule is rule]
        if not rows:
            continue
        qty = sum((r.quantity for r in rows), ZERO)
        pnl = sum((r.net_pnl for r in rows), ZERO)
        by_rule[rule.value] = {"n": len(rows), "contracts": qty, "net_pnl": _q(pnl), "net_per_contract": _q(pnl / qty) if qty else None, "losses": sum(1 for r in rows if not r.won)}
    return Verdict(
        status=status,
        n=n,
        station_days=station_days,
        mean_net_per_contract=mean,
        median_net_per_contract=_median(per_contract),
        losses=losses,
        loss_rate=(Decimal(losses) / Decimal(n)) if n else None,
        kill_rule_triggered=kill,
        total_net_pnl=total,
        contracts=contracts,
        by_rule=by_rule,
        pre_registered={
            "rule": "PASS when n >= min_settled_positions across >= min_station_days station-days, mean net PnL per contract >= pass_mean_net_per_contract and zero dead-bucket losses; any loss triggers the kill rule",
            "sample": "settled paper positions bought as " + (side.value.upper() if side else "either outcome") + " on buckets the kill rules declared dead/certain",
            "min_settled_positions": params.min_settled_positions,
            "min_station_days": params.min_station_days,
            "pass_mean_net_per_contract": params.pass_mean_net_per_contract,
            "min_net_edge_at_entry": params.min_net_edge,
            "kill_rule": "one settled position on a dead bucket that resolved YES (or a certain bucket that resolved NO)",
            "fees": "Polymarket taker fee rate * p * (1 - p) per contract, from the market's Gamma feeType (weather_fees = 5 %)",
            "settlement": "venue resolution (Gamma outcomePrices) books the position at 1/0 on the PaperLedger; observation-implied outcomes are reported alongside for basis-risk audit",
        },
    )


__all__ = [
    "BucketVerdict",
    "DEFAULT_WEATHER_TAKER_FEE_RATE",
    "DeadBucketEvaluation",
    "DeadBucketParameters",
    "DeadBucketStrategy",
    "KillRule",
    "MarketKind",
    "ResolutionSourceKind",
    "RunningHigh",
    "STATION_TIMEZONES",
    "SettledRecord",
    "SpecParse",
    "StationObservation",
    "TRACK",
    "TRACK_LABEL",
    "TemperatureBucket",
    "TemperatureUnit",
    "Verdict",
    "WEATHER_RISK_LIMITS",
    "WeatherMarketSpec",
    "classify_bucket",
    "fee_per_contract",
    "local_day_window",
    "net_edge_per_contract",
    "parse_bucket_label",
    "parse_metar_observation",
    "parse_metar_report_type",
    "parse_metar_temperature_tenths_c",
    "parse_weather_market",
    "running_high",
    "taker_fee_rate_for",
    "tenths_c_to_unit",
    "verdict",
    "whole_contracts",
]
