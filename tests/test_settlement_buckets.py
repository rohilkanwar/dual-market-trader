from decimal import Decimal

import pytest

from settlement.bucket_match import (
    CdfPoint,
    NonMonotone,
    kalshi_cdf_to_intervals,
    polymarket_point_bucket,
)
from settlement.fed_match import FedBucket, FedMatchType, match_fed_buckets


def test_fed_exact_and_union_remain_distinct_metrics() -> None:
    exact = match_fed_buckets(FedBucket.C25, FedBucket.C25)
    union = match_fed_buckets(
        (FedBucket.C25, FedBucket.C26),
        FedBucket.C25,
    )

    assert exact.match_type is FedMatchType.EXACT
    assert union.match_type is FedMatchType.UNION


def test_cdf_intervals_contain_point_bucket_and_detect_disjoint() -> None:
    intervals = kalshi_cdf_to_intervals(
        (
            CdfPoint(Decimal("3.0"), Decimal("0.40")),
            CdfPoint(Decimal("3.5"), Decimal("0.70")),
        )
    )
    point = polymarket_point_bucket(Decimal("3.1"), Decimal("3.4"))

    assert intervals[1].contains_interval(point)
    assert intervals[0].disjoint(point)


def test_non_monotone_cdf_is_rejected() -> None:
    with pytest.raises(NonMonotone):
        kalshi_cdf_to_intervals(
            (
                CdfPoint(Decimal("3.0"), Decimal("0.60")),
                CdfPoint(Decimal("3.5"), Decimal("0.50")),
            )
        )
