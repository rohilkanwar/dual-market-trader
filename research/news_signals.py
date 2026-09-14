"""Stub news-signal interface for the ``news_underreaction`` paper track.

A :class:`NewsSignal` is a public signal (headline, data release, official
statement) tied to one venue market. The *hard part* of the lane is the
``implied_probability`` field: the probability the market "should" trade at
once the signal is fully incorporated. No component in this repository
computes that mapping. It is either

* assigned by hand in a fixture (``mapping = "fixture_assigned"``; synthetic,
  exists to prove the measurement math),
* supplied by an operator file (``mapping = "operator_assigned"``), or
* absent (``mapping = "unmapped"``, ``implied_probability = None``), which is
  what the optional RSS headline hook produces. Unmapped signals are counted
  as candidates and refused; they never trade.

Sources never raise on network failure: errors are collected on the
:class:`SignalBatch` and the track reports an honest empty result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from xml.etree import ElementTree

from core.types import ONE, ZERO, Market, Venue

LOGGER = logging.getLogger("news_signals")

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "news_signals.json"
MAPPING_STATES = ("fixture_assigned", "operator_assigned", "unmapped")


def _now() -> datetime:
    return datetime.now(UTC)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{value!r} is not a decimal") from exc


@dataclass(frozen=True, slots=True)
class NewsSignal:
    signal_id: str
    venue: Venue
    market_id: str
    headline: str
    observed_at: datetime
    source: str
    implied_probability: Decimal | None = None
    pre_signal_mid: Decimal | None = None
    confidence: Decimal = ONE
    mapping: str = "unmapped"
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.implied_probability is not None and not ZERO <= self.implied_probability <= ONE:
            raise ValueError(f"implied_probability {self.implied_probability} must be within [0, 1]")
        if self.pre_signal_mid is not None and not ZERO <= self.pre_signal_mid <= ONE:
            raise ValueError(f"pre_signal_mid {self.pre_signal_mid} must be within [0, 1]")
        if not ZERO <= self.confidence <= ONE:
            raise ValueError(f"confidence {self.confidence} must be within [0, 1]")
        if self.mapping not in MAPPING_STATES:
            raise ValueError(f"mapping must be one of {MAPPING_STATES}")
        if self.implied_probability is None and self.mapping != "unmapped":
            raise ValueError("a signal without implied_probability must be 'unmapped'")
        if self.implied_probability is not None and self.mapping == "unmapped":
            raise ValueError("a signal with implied_probability must declare who mapped it")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")

    def age_seconds(self, as_of: datetime) -> Decimal:
        return Decimal(str((as_of - self.observed_at).total_seconds()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "venue": self.venue.value,
            "market_id": self.market_id,
            "headline": self.headline,
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
            "implied_probability": self.implied_probability,
            "pre_signal_mid": self.pre_signal_mid,
            "confidence": self.confidence,
            "mapping": self.mapping,
            "url": self.url,
        }


@dataclass(slots=True)
class SignalBatch:
    source: str
    signals: list[NewsSignal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fetched_at: str = field(default_factory=lambda: _now().isoformat())
    note: str = ""


class SignalSource(Protocol):
    name: str

    async def fetch(self, *, markets: dict[Venue, list[Market]], as_of: datetime) -> SignalBatch: ...


# --------------------------------------------------------------------------
# Null source: the network default. Nothing is fetched, nothing is invented.
# --------------------------------------------------------------------------
class NullSignalSource:
    name = "none"

    async def fetch(self, *, markets: dict[Venue, list[Market]], as_of: datetime) -> SignalBatch:
        del markets, as_of
        return SignalBatch(
            source=self.name,
            note="no signal source configured; pass --news-signals or --news-rss to supply one",
        )


# --------------------------------------------------------------------------
# JSON file sources (fixture + operator)
# --------------------------------------------------------------------------
def parse_signal(item: dict[str, Any], *, as_of: datetime, source: str, default_mapping: str) -> NewsSignal:
    """Parse one JSON record. ``observed_at`` (ISO-8601) or ``age_seconds`` is required.

    ``age_seconds`` is relative to ``as_of`` so committed fixtures stay fresh on
    every run and the staleness rail is still exercised deterministically.
    """
    if "observed_at" in item and item["observed_at"]:
        observed_at = datetime.fromisoformat(str(item["observed_at"]))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
    elif "age_seconds" in item:
        observed_at = as_of - timedelta(seconds=float(item["age_seconds"]))
    else:
        raise ValueError(f"signal {item.get('signal_id')!r} needs observed_at or age_seconds")
    implied = _decimal_or_none(item.get("implied_probability"))
    mapping = str(item.get("mapping") or (default_mapping if implied is not None else "unmapped"))
    confidence = _decimal_or_none(item.get("confidence"))
    return NewsSignal(
        signal_id=str(item["signal_id"]),
        venue=Venue(str(item["venue"])),
        market_id=str(item["market_id"]),
        headline=str(item.get("headline", "")),
        observed_at=observed_at,
        source=source,
        implied_probability=implied,
        pre_signal_mid=_decimal_or_none(item.get("pre_signal_mid")),
        confidence=confidence if confidence is not None else ONE,
        mapping=mapping,
        url=item.get("url"),
        metadata=dict(item.get("metadata") or {}),
    )


def load_signals(path: Path, *, as_of: datetime, source: str, default_mapping: str) -> list[NewsSignal]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("signals", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("signals file must be a list or an object with a 'signals' list")
    return [parse_signal(item, as_of=as_of, source=source, default_mapping=default_mapping) for item in items]


class JsonSignalSource:
    """Operator-supplied signals. The operator owns the probability mapping."""

    name = "operator_file"

    def __init__(self, path: Path, *, source: str | None = None, default_mapping: str = "operator_assigned") -> None:
        self.path = path
        self.source = source or self.name
        self.default_mapping = default_mapping

    async def fetch(self, *, markets: dict[Venue, list[Market]], as_of: datetime) -> SignalBatch:
        del markets
        batch = SignalBatch(source=self.source, note=f"signals loaded from {self.path}")
        try:
            batch.signals = load_signals(
                self.path, as_of=as_of, source=self.source, default_mapping=self.default_mapping
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            batch.errors.append(f"{type(exc).__name__}: {exc}")
        return batch


class FixtureSignalSource(JsonSignalSource):
    """Committed synthetic signals aligned with the venue fixture books."""

    name = "fixture"

    def __init__(self, path: Path = FIXTURE_PATH) -> None:
        super().__init__(path, source=self.name, default_mapping="fixture_assigned")


# --------------------------------------------------------------------------
# Optional public RSS/Atom headline hook (dependency-free, never maps to a probability)
# --------------------------------------------------------------------------
_STOPWORDS = frozenset(
    "the a an and or of to in on at by for with will be is are was were than above below over under "
    "before after into from this that these those it its as vs v about does do did has have had not no yes "
    "who what when where which how new".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN.findall(text.lower()) if len(t) >= 3 and t not in _STOPWORDS)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _parse_feed(xml_text: str) -> list[dict[str, str | None]]:
    """Return ``{title, link, published}`` for RSS 2.0 ``item`` or Atom ``entry`` nodes."""
    root = ElementTree.fromstring(xml_text)
    entries: list[dict[str, str | None]] = []

    def text(node: ElementTree.Element, *names: str) -> str | None:
        for child in node:
            local = child.tag.rsplit("}", 1)[-1]
            if local in names:
                if local == "link" and child.text is None:
                    return child.get("href")
                return (child.text or "").strip() or None
        return None

    for node in root.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if local not in ("item", "entry"):
            continue
        entries.append(
            {
                "title": text(node, "title"),
                "link": text(node, "link"),
                "published": text(node, "pubDate", "published", "updated", "date"),
            }
        )
    return entries


def _parse_published(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def match_headlines_to_markets(
    entries: list[dict[str, str | None]],
    markets: dict[Venue, list[Market]],
    *,
    as_of: datetime,
    source: str,
    min_shared_tokens: int = 2,
) -> list[NewsSignal]:
    """Keyword-overlap matcher. Produces *unmapped* signals only.

    This is deliberately crude: it proves the plumbing (headline -> market ->
    track) without pretending a probability can be read off a headline.
    """
    signals: list[NewsSignal] = []
    for entry in entries:
        title = entry.get("title") or ""
        headline_tokens = tokenize(title)
        if not headline_tokens:
            continue
        published = _parse_published(entry.get("published"))
        for venue, venue_markets in markets.items():
            for market in venue_markets:
                shared = headline_tokens & tokenize(market.title)
                if len(shared) < min_shared_tokens:
                    continue
                signals.append(
                    NewsSignal(
                        signal_id=f"rss-{venue.value}-{market.market_id}-{_digest(title)}",
                        venue=venue,
                        market_id=market.market_id,
                        headline=title,
                        observed_at=published or as_of,
                        source=source,
                        implied_probability=None,
                        pre_signal_mid=None,
                        confidence=Decimal(len(shared)) / Decimal(max(len(headline_tokens), 1)),
                        mapping="unmapped",
                        url=entry.get("link"),
                        metadata={
                            "shared_tokens": sorted(shared),
                            "timestamp_missing": published is None,
                        },
                    )
                )
    return signals


class RssHeadlineSource:
    """Fetch public RSS/Atom feeds and match headlines to snapshot markets.

    Free, unauthenticated and read-only. Any network or parse failure is
    recorded on the batch; the track then reports an empty result.
    """

    name = "rss"

    def __init__(
        self,
        feed_urls: tuple[str, ...] | list[str],
        *,
        timeout: float = 10.0,
        min_shared_tokens: int = 2,
        http_get: Any | None = None,
    ) -> None:
        self.feed_urls = tuple(feed_urls)
        self.timeout = timeout
        self.min_shared_tokens = min_shared_tokens
        self._http_get = http_get

    async def _get(self, url: str) -> str:
        if self._http_get is not None:
            return await self._http_get(url)
        import httpx  # local import keeps fixture runs free of network deps

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "dual-market-trader-paper/0.2"})
            response.raise_for_status()
            return response.text

    async def fetch(self, *, markets: dict[Venue, list[Market]], as_of: datetime) -> SignalBatch:
        batch = SignalBatch(
            source=self.name,
            note="headlines matched by keyword overlap; implied probability is never inferred",
        )
        if not self.feed_urls:
            batch.errors.append("no feed urls configured")
            return batch
        entries: list[dict[str, str | None]] = []
        for url in self.feed_urls:
            try:
                entries.extend(_parse_feed(await self._get(url)))
            except Exception as exc:  # network / parse failures must not kill the run
                batch.errors.append(f"{url}: {type(exc).__name__}: {exc}")
        batch.signals = match_headlines_to_markets(
            entries, markets, as_of=as_of, source=self.name, min_shared_tokens=self.min_shared_tokens
        )
        batch.note += f"; {len(entries)} headlines scanned"
        return batch


# --------------------------------------------------------------------------
# Composition + CLI wiring
# --------------------------------------------------------------------------
class CompositeSignalSource:
    name = "composite"

    def __init__(self, sources: list[SignalSource]) -> None:
        self.sources = sources

    async def fetch(self, *, markets: dict[Venue, list[Market]], as_of: datetime) -> SignalBatch:
        batch = SignalBatch(source="+".join(s.name for s in self.sources) or self.name)
        for source in self.sources:
            part = await source.fetch(markets=markets, as_of=as_of)
            batch.signals.extend(part.signals)
            batch.errors.extend(f"{source.name}: {e}" for e in part.errors)
            if part.note:
                batch.note = f"{batch.note}; {part.note}" if batch.note else part.note
        return batch


def build_signal_source(
    *,
    use_fixtures: bool,
    signals_path: Path | None = None,
    rss_urls: tuple[str, ...] | list[str] | None = None,
) -> SignalSource:
    """Default wiring: fixtures on fixture runs, nothing on network runs."""
    sources: list[SignalSource] = []
    if signals_path is not None:
        sources.append(JsonSignalSource(signals_path))
    if rss_urls:
        sources.append(RssHeadlineSource(rss_urls))
    if sources:
        return sources[0] if len(sources) == 1 else CompositeSignalSource(sources)
    return FixtureSignalSource() if use_fixtures else NullSignalSource()
