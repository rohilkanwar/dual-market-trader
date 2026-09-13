
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise


class NonMonotone(ValueError):
    intervals = [Interval(None, ordered[0].threshold)]
    intervals.extend(
        Interval(left.threshold, right.threshold)
        for left, right in pairwise(ordered)
    )
    intervals.append(Interval(ordered[-1].threshold, None))
    return tuple(intervals)
