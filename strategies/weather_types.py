"""Shared station / city / bucket types for the Polymarket daily-temperature markets.

Polymarket lists one NegRisk event per city and day, ``Highest temperature in
<city> on <Month day>?``, whose legs are contiguous temperature buckets:
``65°F or below``, ``66-67°F`` ... ``84°F or higher`` (US stations, whole °F,
two-degree buckets) or ``26°C or below``, ``27°C`` ... ``36°C or higher``
(everywhere else, one-degree buckets). Each event settles on the resolution
station named in the rule text (NOAA hourly ``Temp`` column at an airport, or
the Hong Kong Observatory daily extract), reported to *whole degrees* except
Hong Kong (one decimal). That precision decides how a label maps onto a
continuous interval: a whole-degree ``74-75°F`` reading means the true maximum
rounded to 74 or 75, i.e. ``[73.5, 75.5)``; a one-decimal ``31°C`` means
``[31.0, 32.0)``.

These types are deliberately strategy-neutral so sibling weather tracks
(bucket edge, late-day METAR) can reuse them; nothing here prices anything.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from core.types import Market

TemperatureUnit = Literal["F", "C"]
Precision = Literal["whole", "tenth"]

CITIES_PATH = Path(__file__).resolve().parents[1] / "data" / "weather" / "cities.json"
POLYMARKET_WEATHER_TAG = 104596  # Gamma tag "Highest temperature"
POLYMARKET_DAILY_TEMPERATURE_TAG = 103040  # Gamma tag "Daily Temperature"

_TITLE = re.compile(r"^\s*Highest temperature in (?P<city>.+?) on (?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})\s*\??\s*$")
_SLUG_DATE = re.compile(r"-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$")
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
        start=1,
    )
}
_BUCKET_RANGE = re.compile(r"^\s*(?:between\s+)?(?P<lo>-?\d+)\s*[-–]\s*(?P<hi>-?\d+)\s*°?\s*(?P<unit>[FC])\s*$", re.I)
_BUCKET_POINT = re.compile(r"^\s*(?P<val>-?\d+)\s*°?\s*(?P<unit>[FC])\s*$", re.I)
_BUCKET_BELOW = re.compile(r"^\s*(?P<val>-?\d+)\s*°?\s*(?P<unit>[FC])\s+or\s+(?:below|lower|less)\s*$", re.I)
_BUCKET_ABOVE = re.compile(r"^\s*(?P<val>-?\d+)\s*°?\s*(?P<unit>[FC])\s+or\s+(?:higher|above|more)\s*$", re.I)
_QUESTION_BUCKET = re.compile(r"\bbe\s+(?P<label>.+?)\s+on\s+[A-Za-z]+\s+\d{1,2}\??\s*$", re.I)


def _norm(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(ch for ch in stripped if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", ascii_only.lower()).strip()


# --------------------------------------------------------------------------
# Cities / stations
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CityStation:
    key: str
    title_names: tuple[str, ...]
    station: str | None
    latitude: float
    longitude: float
    timezone: str
    unit: TemperatureUnit
    precision: Precision = "whole"
    source: str | None = None

    @property
    def display_name(self) -> str:
        return self.title_names[0] if self.title_names else self.key

    def local_date(self, as_of: datetime) -> date:
        return as_of.astimezone(ZoneInfo(self.timezone)).date()

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.display_name,
            "station": self.station,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone,
            "unit": self.unit,
            "precision": self.precision,
            "source": self.source,
        }


class CityRegistry:
    """Title name -> :class:`CityStation`, loaded from ``data/weather/cities.json``."""

    def __init__(self, cities: list[CityStation]) -> None:
        self.cities = {c.key: c for c in cities}
        self._by_name: dict[str, CityStation] = {}
        for city in cities:
            for name in city.title_names:
                self._by_name[_norm(name)] = city

    @classmethod
    def load(cls, path: Path = CITIES_PATH) -> CityRegistry:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cities = [
            CityStation(
                key=str(item["key"]),
                title_names=tuple(str(n) for n in item.get("title_names", [item["key"]])),
                station=item.get("station"),
                latitude=float(item["latitude"]),
                longitude=float(item["longitude"]),
                timezone=str(item["timezone"]),
                unit="F" if str(item.get("unit", "C")).upper() == "F" else "C",
                precision="tenth" if item.get("precision") == "tenth" else "whole",
                source=item.get("source"),
            )
            for item in payload.get("cities", [])
        ]
        return cls(cities)

    def resolve(self, title_name: str) -> CityStation | None:
        return self._by_name.get(_norm(title_name))

    def get(self, key: str) -> CityStation | None:
        return self.cities.get(key)

    def __len__(self) -> int:
        return len(self.cities)


# --------------------------------------------------------------------------
# Buckets
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TemperatureBucket:
    """Inclusive integer bounds in the resolution source's reported unit.

    ``lo is None`` means "or below", ``hi is None`` means "or higher". The
    continuous interval a bucket covers depends on the source precision
    (:meth:`edges`); a ladder of buckets built from one event tiles the line.
    """

    lo: int | None
    hi: int | None
    unit: TemperatureUnit
    label: str = ""

    def __post_init__(self) -> None:
        if self.lo is not None and self.hi is not None and self.lo > self.hi:
            raise ValueError(f"bucket {self.label!r}: lo {self.lo} > hi {self.hi}")
        if self.lo is None and self.hi is None:
            raise ValueError("a bucket needs at least one bound")

    @property
    def open_low(self) -> bool:
        return self.lo is None

    @property
    def open_high(self) -> bool:
        return self.hi is None

    @property
    def width(self) -> int | None:
        if self.lo is None or self.hi is None:
            return None
        return self.hi - self.lo + 1

    def edges(self, precision: Precision = "whole") -> tuple[float, float]:
        """Half-open continuous interval ``[lower, upper)`` in the bucket's unit."""
        half = 0.5 if precision == "whole" else 0.0
        step = 1.0
        lower = -math.inf if self.lo is None else self.lo - half
        upper = math.inf if self.hi is None else self.hi + (step - half)
        return lower, upper

    def contains(self, value: float, precision: Precision = "whole") -> bool:
        lower, upper = self.edges(precision)
        return lower <= value < upper

    def midpoint(self, precision: Precision = "whole", *, ladder_width: int = 1) -> float:
        """Representative value for a resolved bucket (open ends: one width past the edge)."""
        lower, upper = self.edges(precision)
        if math.isinf(lower) and math.isinf(upper):
            raise ValueError("unbounded bucket")
        if math.isinf(lower):
            return upper - ladder_width / 2
        if math.isinf(upper):
            return lower + ladder_width / 2
        return (lower + upper) / 2

    def as_dict(self) -> dict[str, Any]:
        return {"lo": self.lo, "hi": self.hi, "unit": self.unit, "label": self.label}


def parse_bucket_label(label: str, *, unit: TemperatureUnit | None = None) -> TemperatureBucket | None:
    """``"66-67°F"`` / ``"27°C"`` / ``"65°F or below"`` / ``"84°F or higher"`` -> bucket."""
    text = label.replace("º", "°").strip()
    for pattern, kind in ((_BUCKET_BELOW, "below"), (_BUCKET_ABOVE, "above"), (_BUCKET_RANGE, "range"), (_BUCKET_POINT, "point")):
        m = pattern.match(text)
        if not m:
            continue
        u = m.group("unit").upper()
        if unit is not None and u != unit:
            return None
        if kind == "below":
            return TemperatureBucket(None, int(m.group("val")), u, text)  # type: ignore[arg-type]
        if kind == "above":
            return TemperatureBucket(int(m.group("val")), None, u, text)  # type: ignore[arg-type]
        if kind == "range":
            return TemperatureBucket(int(m.group("lo")), int(m.group("hi")), u, text)  # type: ignore[arg-type]
        value = int(m.group("val"))
        return TemperatureBucket(value, value, u, text)  # type: ignore[arg-type]
    return None


def bucket_from_market(market: Market) -> TemperatureBucket | None:
    """Prefer Gamma's ``groupItemTitle``; fall back to the question text."""
    meta = market.metadata
    label = meta.get("group_item_title")
    if not label and isinstance(meta.get("raw"), dict):
        label = meta["raw"].get("groupItemTitle")
    if label:
        bucket = parse_bucket_label(str(label))
        if bucket is not None:
            return bucket
    m = _QUESTION_BUCKET.search(market.title)
    if m:
        return parse_bucket_label(m.group("label"))
    return None


def ladder_is_contiguous(buckets: list[TemperatureBucket]) -> bool:
    """One open-low, one open-high, and every neighbour touches (``hi + 1 == next lo``)."""
    if len(buckets) < 2:
        return False
    ordered = sorted(buckets, key=lambda b: (-math.inf if b.lo is None else b.lo))
    if not ordered[0].open_low or not ordered[-1].open_high:
        return False
    if any(b.open_low for b in ordered[1:]) or any(b.open_high for b in ordered[:-1]):
        return False
    for a, b in zip(ordered, ordered[1:]):
        if a.hi is None or b.lo is None or a.hi + 1 != b.lo:
            return False
    return True


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------
def parse_weather_event_title(title: str) -> tuple[str, int, int] | None:
    """``"Highest temperature in NYC on September 14?"`` -> ``("NYC", 9, 14)``."""
    m = _TITLE.match(title or "")
    if not m:
        return None
    month = _MONTHS.get(m.group("month").lower())
    if month is None:
        return None
    return m.group("city").strip(), month, int(m.group("day"))


def parse_target_date(title: str, *, slug: str | None = None, end_date: str | None = None) -> date | None:
    """Observation date: month/day from the title, year from the slug (else the end date)."""
    parsed = parse_weather_event_title(title)
    if parsed is None:
        return None
    _, month, day = parsed
    year: int | None = None
    if slug:
        m = _SLUG_DATE.search(slug.lower())
        if m:
            year = int(m.group("year"))
    if year is None and end_date:
        try:
            year = datetime.fromisoformat(end_date.replace("Z", "+00:00")).astimezone(UTC).year
        except ValueError:
            year = None
    if year is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class WeatherLeg:
    market: Market
    bucket: TemperatureBucket
    resolved: bool | None = None  # True = this leg paid YES, False = paid NO, None = open

    @property
    def market_id(self) -> str:
        return self.market.market_id


@dataclass(frozen=True, slots=True)
class WeatherEvent:
    slug: str
    event_id: str
    title: str
    city_name: str
    city: CityStation | None
    target_date: date
    legs: tuple[WeatherLeg, ...]
    closed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def unit(self) -> TemperatureUnit:
        return self.legs[0].bucket.unit if self.legs else (self.city.unit if self.city else "C")

    @property
    def precision(self) -> Precision:
        return self.city.precision if self.city else "whole"

    @property
    def ladder_width(self) -> int:
        widths = [b.width for b in (leg.bucket for leg in self.legs) if b.width is not None]
        return max(widths) if widths else 1

    @property
    def buckets(self) -> list[TemperatureBucket]:
        return [leg.bucket for leg in self.legs]

    @property
    def contiguous(self) -> bool:
        return ladder_is_contiguous(self.buckets)

    @property
    def resolved_leg(self) -> WeatherLeg | None:
        winners = [leg for leg in self.legs if leg.resolved is True]
        return winners[0] if len(winners) == 1 else None

    @property
    def resolved_bucket(self) -> TemperatureBucket | None:
        leg = self.resolved_leg
        return leg.bucket if leg else None

    def truth_value(self) -> float | None:
        """Continuous stand-in for the settled reading: the resolved bucket's midpoint."""
        bucket = self.resolved_bucket
        if bucket is None:
            return None
        return bucket.midpoint(self.precision, ladder_width=self.ladder_width)

    def lead_days(self, as_of: datetime) -> int | None:
        if self.city is None:
            return None
        return (self.target_date - self.city.local_date(as_of)).days

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "event_id": self.event_id,
            "title": self.title,
            "city_name": self.city_name,
            "city": self.city.key if self.city else None,
            "target_date": self.target_date.isoformat(),
            "unit": self.unit,
            "precision": self.precision,
            "legs": len(self.legs),
            "contiguous": self.contiguous,
            "closed": self.closed,
            "resolved_bucket": self.resolved_bucket.label if self.resolved_bucket else None,
        }


__all__ = [
    "CITIES_PATH",
    "POLYMARKET_DAILY_TEMPERATURE_TAG",
    "POLYMARKET_WEATHER_TAG",
    "CityRegistry",
    "CityStation",
    "Precision",
    "TemperatureBucket",
    "TemperatureUnit",
    "WeatherEvent",
    "WeatherLeg",
    "bucket_from_market",
    "ladder_is_contiguous",
    "parse_bucket_label",
    "parse_target_date",
    "parse_weather_event_title",
]
