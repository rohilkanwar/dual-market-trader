"""Fed outcome-bucket matcher with non-interchangeable match labels."""

from __future__ import annotations

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
