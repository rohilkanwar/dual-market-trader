"""Fed outcome-bucket matcher with non-interchangeable match labels."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class FedBucket(StrEnum):
    H0 = "H0"
    C25 = "C25"
    C26 = "C26"
    H25 = "H25"
    H26 = "H26"


class FedMatchType(StrEnum):
    EXACT = "exact"
    DOMAIN = "domain"
    UNION = "union"
    NO_MATCH = "no_match"


@dataclass(frozen=True, slots=True)
class FedMatch:
    match_type: FedMatchType
    left: frozenset[FedBucket]
    right: frozenset[FedBucket]

    @property
    def matched(self) -> bool:
        return self.match_type is not FedMatchType.NO_MATCH


_FED_CONTEXT = re.compile(r"\b(?:fed|fomc|federal reserve|fed's|interest rates?)\b", re.IGNORECASE)
_TICKER_BUCKET = re.compile(r"\b(?:KX)?FED(?:DECISION)?-\w+-(H0|H25|H26|C25|C26)\b", re.IGNORECASE)
_HOLD = re.compile(
    r"\bno change\b|\bmaintains?\b|\bunchanged\b|\bhold(?:s)?\b|\b(?:hike|cut|raise|lower)\w*\s+(?:rates?\s+)?by\s+0\s*bps?\b",
    re.IGNORECASE,
)
_CUT = re.compile(r"\b(?:cut|decrease|lower|reduce)\w*\b", re.IGNORECASE)
_HIKE = re.compile(r"\b(?:hike|increase|raise)\w*\b", re.IGNORECASE)
_MORE_THAN_25 = re.compile(r">\s*25\s*bps?|\b(?:50|75|100)\s*\+?\s*bps?\b|\bmore than 25\s*bps?\b", re.IGNORECASE)
_EXACT_25 = re.compile(r"(?<![>\d])\b25\s*bps?\b", re.IGNORECASE)


def parse_fed_bucket(*texts: str | None) -> FedBucket | None:
    """Best-effort FOMC outcome bucket from a ticker, title or subtitle.

    Returns ``None`` unless the text is unmistakably about a Fed decision and
    names one bucket; ambiguity is ``None`` so the gate treats it as unknown.
    """
    joined = " ".join(text for text in texts if text)
    if not joined:
        return None
    ticker = _TICKER_BUCKET.search(joined)
    if ticker:
        return FedBucket(ticker.group(1).upper())
    if not _FED_CONTEXT.search(joined):
        return None
    if _HOLD.search(joined):
        return FedBucket.H0
    cut, hike = bool(_CUT.search(joined)), bool(_HIKE.search(joined))
    if cut == hike:
        return None
    if _MORE_THAN_25.search(joined):
        return FedBucket.C26 if cut else FedBucket.H26
    if _EXACT_25.search(joined):
        return FedBucket.C25 if cut else FedBucket.H25
    return None


def _domain(bucket: FedBucket) -> str:
    if bucket is FedBucket.H0:
        return "hold"
    return "cut" if bucket.value.startswith("C") else "hike"


def match_fed_buckets(
    left: FedBucket | Iterable[FedBucket],
    right: FedBucket | Iterable[FedBucket],
) -> FedMatch:
    left_set = frozenset((left,)) if isinstance(left, FedBucket) else frozenset(left)
    right_set = frozenset((right,)) if isinstance(right, FedBucket) else frozenset(right)
    if not left_set or not right_set:
        return FedMatch(FedMatchType.NO_MATCH, left_set, right_set)
    if left_set == right_set and len(left_set) == 1:
        return FedMatch(FedMatchType.EXACT, left_set, right_set)

    left_domains = {_domain(bucket) for bucket in left_set}
    right_domains = {_domain(bucket) for bucket in right_set}
    if left_domains != right_domains or len(left_domains) != 1:
        return FedMatch(FedMatchType.NO_MATCH, left_set, right_set)
    if len(left_set) > 1 or len(right_set) > 1:
        return FedMatch(FedMatchType.UNION, left_set, right_set)
    return FedMatch(FedMatchType.DOMAIN, left_set, right_set)
