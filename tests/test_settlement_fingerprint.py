from decimal import Decimal

import pytest

from settlement.fingerprint import (
    Comparator,
    PublisherID,
    Relation,
    ReleaseID,
    ResolutionFingerprint,
    TieBreak,
    compare,
)


def fingerprint(comparator: Comparator, **changes: object) -> ResolutionFingerprint:
    values = {
        "publisher": PublisherID.BLS,
        "release_id": ReleaseID.CPI,
        "comparator": comparator,
        "threshold": Decimal("3.0"),
        "reference_period": "2026-08",
        "revisions_included": False,
        "fallback_to_prior_period": False,
        "tie_break": TieBreak.HIGHER,
        **changes,
    }
    return ResolutionFingerprint(**values)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (Comparator.GE, Comparator.LT),
        (Comparator.GT, Comparator.LE),
    ],
)
def test_only_boundary_partition_comparators_are_complements(
    left: Comparator,
    right: Comparator,
) -> None:
    assert compare(fingerprint(left), fingerprint(right)).relation is Relation.COMPLEMENT


def test_greater_equal_and_less_equal_are_not_complements() -> None:
    verdict = compare(fingerprint(Comparator.GE), fingerprint(Comparator.LE))
    assert verdict.relation is Relation.NOT_EQUIVALENT


def test_optional_field_on_one_side_is_indeterminate() -> None:
    verdict = compare(
        fingerprint(Comparator.GT),
        fingerprint(Comparator.GT, fallback_to_prior_period=None),
    )
    assert verdict.relation is Relation.INDETERMINATE


def test_known_optional_mismatch_is_not_equivalent() -> None:
    verdict = compare(
        fingerprint(Comparator.GT),
        fingerprint(Comparator.GT, fallback_to_prior_period=True),
    )
    assert verdict.relation is Relation.NOT_EQUIVALENT


def test_unknown_enum_fails_closed_before_other_checks() -> None:
    verdict = compare(
        fingerprint(Comparator.UNKNOWN),
        fingerprint(Comparator.UNKNOWN),
    )
    assert verdict.relation is Relation.INDETERMINATE
