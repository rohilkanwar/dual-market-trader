"""Resolution-source host classification using boundary-safe suffixes."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse


class HostTier(StrEnum):
    OFFICIAL = "official"
    MEDIA = "media"
    AGGREGATOR = "aggregator"
    SELF = "self"
    UNCLASSIFIED = "unclassified"


_OFFICIAL = {
    "bls.gov",
    "bea.gov",
    "census.gov",
    "federalreserve.gov",
    "nba.com",
    "nfl.com",
    "nhl.com",
    "mlb.com",
    "wnba.com",
}
_MEDIA = {
    "espn.com",
    "foxsports.com",
    "cbssports.com",
    "nbcsports.com",
    "reuters.com",
    "apnews.com",
}
_AGGREGATORS = {
    "fred.stlouisfed.org",
    "tradingeconomics.com",
    "statista.com",
    "sports-reference.com",
}
_SELF = {
    "kalshi.com",
    "polymarket.com",
}


def _host(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"//{value}")
    return (parsed.hostname or "").lower().rstrip(".")


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def classify(value: str) -> HostTier:
    host = _host(value)
    if not host:
        return HostTier.UNCLASSIFIED
    if host.endswith(".gov") or host.endswith(".mil"):
        return HostTier.OFFICIAL
    for tier, domains in (
        (HostTier.OFFICIAL, _OFFICIAL),
        (HostTier.MEDIA, _MEDIA),
        (HostTier.AGGREGATOR, _AGGREGATORS),
        (HostTier.SELF, _SELF),
    ):
        if any(_matches(host, domain) for domain in domains):
            return tier
    return HostTier.UNCLASSIFIED
