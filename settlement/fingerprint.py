"""Fail-closed comparison of contract resolution semantics."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping


class PublisherID(StrEnum):
    FEDERAL_RESERVE = "federal_reserve"
    BLS = "bls"
    BEA = "bea"
    CENSUS = "census"
    SPORTS_LEAGUE = "sports_league"
    UNKNOWN = "unknown"


class ReleaseID(StrEnum):
    FOMC_TARGET_RATE = "fomc_target_rate"
    CPI = "cpi"
    PAYROLLS = "payrolls"
    UNEMPLOYMENT = "unemployment"
    SPORTS_RESULT = "sports_result"
    UNKNOWN = "unknown"


class Comparator(StrEnum):
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    EQ = "=="
    UNKNOWN = "unknown"


class TieBreak(StrEnum):
    HIGHER = "higher"
    LOWER = "lower"
    NONE = "none"
    UNKNOWN = "unknown"


class Relation(StrEnum):
    EQUIVALENT = "equivalent"
    COMPLEMENT = "complement"
    NOT_EQUIVALENT = "not_equivalent"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class ResolutionFingerprint:
    publisher: PublisherID
    release_id: ReleaseID
    comparator: Comparator
    threshold: Decimal | None = None
    reference_period: str | None = None
    revisions_included: bool | None = None
    fallback_to_prior_period: bool | None = None
    tie_break: TieBreak | None = None


@dataclass(frozen=True, slots=True)
class MatchVerdict:
    relation: Relation
    reason: str


_OPTIONAL_FIELDS = (
    "threshold",
    "reference_period",
    "revisions_included",
    "fallback_to_prior_period",
    "tie_break",
)
_COMPLEMENTS = {
    frozenset((Comparator.GE, Comparator.LT)),
    frozenset((Comparator.GT, Comparator.LE)),
}


def compare(
    left: ResolutionFingerprint,
    right: ResolutionFingerprint,
) -> MatchVerdict:
    """Apply the arbAI comparison policy in its prescribed order."""
    enum_values = (
        left.publisher,
        left.release_id,
        left.comparator,
        left.tie_break,
        right.publisher,
        right.release_id,
        right.comparator,
        right.tie_break,
    )
    if any(getattr(value, "value", None) == "unknown" for value in enum_values):
        return MatchVerdict(Relation.INDETERMINATE, "unknown taxonomy value")

    if left.publisher is not right.publisher or left.release_id is not right.release_id:
        return MatchVerdict(Relation.NOT_EQUIVALENT, "publisher or release differs")

    for field_name in _OPTIONAL_FIELDS:
        left_value = getattr(left, field_name)
        right_value = getattr(right, field_name)
        if (left_value is None) != (right_value is None):
            return MatchVerdict(
                Relation.INDETERMINATE,
                f"{field_name} present on one side only",
            )

    for field_name in _OPTIONAL_FIELDS:
        left_value = getattr(left, field_name)
        right_value = getattr(right, field_name)
        if left_value is not None and left_value != right_value:
            return MatchVerdict(Relation.NOT_EQUIVALENT, f"{field_name} differs")

    if left.comparator is right.comparator:
        return MatchVerdict(Relation.EQUIVALENT, "resolution fingerprints match")
    if frozenset((left.comparator, right.comparator)) in _COMPLEMENTS:
        return MatchVerdict(Relation.COMPLEMENT, "comparators partition the boundary")
    return MatchVerdict(
        Relation.NOT_EQUIVALENT,
        "comparators are neither equal nor a safe complement",
    )


def from_mapping(value: Mapping[str, Any]) -> ResolutionFingerprint:
    threshold = value.get("threshold")
    tie_break = value.get("tie_break")
    return ResolutionFingerprint(
        publisher=PublisherID(value.get("publisher", "unknown")),
        release_id=ReleaseID(value.get("release_id", "unknown")),
        comparator=Comparator(value.get("comparator", "unknown")),
        threshold=Decimal(str(threshold)) if threshold is not None else None,
        reference_period=value.get("reference_period"),
        revisions_included=value.get("revisions_included"),
        fallback_to_prior_period=value.get("fallback_to_prior_period"),
        tie_break=TieBreak(tie_break) if tie_break is not None else None,
    )
