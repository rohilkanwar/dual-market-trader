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


# Public suffixes that take a third label to identify the registrant. Kept
# deliberately short: a wrong guess here yields a *mismatch* (fail-closed), not
# a false match.
_MULTI_LABEL_SUFFIXES = {
    "co.uk", "org.uk", "gov.uk", "ac.uk",
    "com.au", "gov.au",
    "co.jp", "go.jp",
    "gc.ca",
}


def _host(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"//{value}")
    return (parsed.hostname or "").lower().rstrip(".")


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def registrable_domain(value: str) -> str:
    """The registrant-owned part of a host (``data.bls.gov`` -> ``bls.gov``).

    Empty when the value carries no host. Two resolution URLs are treated as the
    same publisher only when this value is identical on both sides.
    """
    host = _host(value)
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_publisher(left: str, right: str) -> bool:
    """True only when both URLs resolve to one non-empty registrable domain."""
    left_domain, right_domain = registrable_domain(left), registrable_domain(right)
    return bool(left_domain) and left_domain == right_domain


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
