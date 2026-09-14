"""Map Kalshi cumulative-threshold ladders onto Polymarket point buckets."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise


class NonMonotone(ValueError):
    """Raised when a cumulative ladder's probabilities decrease with the threshold."""


@dataclass(frozen=True, slots=True)
class CdfPoint:
    """``probability`` that the outcome is at or below ``threshold``."""

    threshold: Decimal
    probability: Decimal


@dataclass(frozen=True, slots=True)
class Interval:
    """Half-open numeric bucket ``(lower, upper]``; ``None`` means unbounded."""

    lower: Decimal | None
    upper: Decimal | None

    def contains_interval(self, other: Interval) -> bool:
        lower_ok = self.lower is None or (other.lower is not None and other.lower >= self.lower)
        upper_ok = self.upper is None or (other.upper is not None and other.upper <= self.upper)
        return lower_ok and upper_ok

    def disjoint(self, other: Interval) -> bool:
        if self.upper is not None and other.lower is not None and other.lower >= self.upper:
            return True
        if self.lower is not None and other.upper is not None and other.upper <= self.lower:
            return True
        return False

    def as_dict(self) -> dict[str, str | None]:
        return {
            "lower": str(self.lower) if self.lower is not None else None,
            "upper": str(self.upper) if self.upper is not None else None,
        }


def kalshi_cdf_to_intervals(points: Iterable[CdfPoint]) -> tuple[Interval, ...]:
    ordered = sorted(points, key=lambda point: point.threshold)
    if not ordered:
        return ()
    for left, right in pairwise(ordered):
        if right.probability < left.probability:
            raise NonMonotone(
                f"P(<= {right.threshold}) = {right.probability} is below "
                f"P(<= {left.threshold}) = {left.probability}"
            )
    intervals = [Interval(None, ordered[0].threshold)]
    intervals.extend(
        Interval(left.threshold, right.threshold)
        for left, right in pairwise(ordered)
    )
    intervals.append(Interval(ordered[-1].threshold, None))
    return tuple(intervals)


def polymarket_point_bucket(lower: Decimal, upper: Decimal) -> Interval:
    if upper < lower:
        raise ValueError("bucket upper bound must not be below its lower bound")
    return Interval(lower, upper)
