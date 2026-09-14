from decimal import Decimal

import pytest

from settlement.bucket_match import (
    CdfPoint,
    NonMonotone,
    kalshi_cdf_to_intervals,
    polymarket_point_bucket,
)
from settlement.fed_match import FedBucket, FedMatchType, match_fed_buckets, parse_fed_bucket


def test_fed_exact_and_union_remain_distinct_metrics() -> None:
    exact = match_fed_buckets(FedBucket.C25, FedBucket.C25)
    union = match_fed_buckets(
        (FedBucket.C25, FedBucket.C26),
        FedBucket.C25,
    )

    assert exact.match_type is FedMatchType.EXACT
    assert union.match_type is FedMatchType.UNION


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (("KXFEDDECISION-26SEP-H26", "Will the Federal Reserve Hike rates by >25bps at their September 2026 meeting?"), FedBucket.H26),
        (("KXFEDDECISION-26SEP-H0", "Will the Federal Reserve Hike rates by 0bps at their September 2026 meeting?"), FedBucket.H0),
        (("Will the Fed decrease interest rates by 50+ bps after the September 2026 meeting?",), FedBucket.C26),
        (("Will the Fed decrease interest rates by 25 bps after the September 2026 meeting?",), FedBucket.C25),
        (("Will there be no change in Fed interest rates after the September 2026 meeting?",), FedBucket.H0),
        (("Will the Fed increase interest rates by 25 bps after the September 2026 meeting?",), FedBucket.H25),
        # Ambiguous or off-topic text is unknown, never guessed.
        (("Fed Rate Hike by September 2026 Meeting?",), None),
        (("Will the Fed cut rates at the September meeting?",), None),
        (("Will August CPI be above 3.0%?",), None),
        (("Will New York beat Boston?",), None),
        ((None, ""), None),
    ],
)
def test_parse_fed_bucket_from_live_style_tickers_and_questions(texts: tuple[str | None, ...], expected: FedBucket | None) -> None:
    assert parse_fed_bucket(*texts) is expected


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
