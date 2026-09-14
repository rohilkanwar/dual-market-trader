"""The strict gate must refuse every clause/fingerprint/host mismatch and admit
only pairs whose settlement semantics are provably identical.

Every mutation below starts from one fully admissible synthetic pair and
changes exactly one thing on the Polymarket side (unless stated otherwise), so
the assertion on ``result.reasons`` pins down which stage caught it.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import pytest

from core.types import Market, Venue
from research.scoreboard import settlement_gate as scoreboard_gate
from settlement.gate import (
    STAGE_ORDER,
    STRICT_POLICY,
    GatePolicy,
    GateStage,
    settlement_gate,
)
from strategies.matching import MarketMatcher, MatchedMarketPair
from tests.synthetic_pairs import FED_TEXT, base_metadata, make_pair
from venues.fixtures import load_fixture
from venues.kalshi.client import FIXTURE_PATH as KALSHI_FIXTURE
from venues.polymarket.client import FIXTURE_PATH as POLY_FIXTURE

Mutation = Callable[[dict[str, Any]], None]


def _set(key: str, value: Any) -> Mutation:
    def apply(meta: dict[str, Any]) -> None:
        meta[key] = value

    return apply


def _drop(key: str) -> Mutation:
    def apply(meta: dict[str, Any]) -> None:
        meta.pop(key, None)

    return apply


def _fp(**changes: Any) -> Mutation:
    def apply(meta: dict[str, Any]) -> None:
        for key, value in changes.items():
            if value is _DROP:
                meta["fingerprint"].pop(key, None)
            else:
                meta["fingerprint"][key] = value

    return apply


def _text(replacement: str) -> Mutation:
    return _set("resolution_text", replacement)


_DROP = object()
TEXT_NO_TIE = FED_TEXT.replace(" Exact boundary ties use the higher bracket.", "")
TEXT_FALLBACK = FED_TEXT.replace(
    "It will not fall back to a prior period.",
    "If publication is delayed, it falls back to the last available month.",
)
TEXT_TIE_LOWER = FED_TEXT.replace("ties use the higher bracket", "ties use the lower bracket")
TEXT_REVISIONS_IN = FED_TEXT.replace("Revisions are excluded.", "Revisions are included.")


# --------------------------------------------------------------------------
# Must admit
# --------------------------------------------------------------------------
def test_identical_settlement_semantics_are_admitted() -> None:
    result = settlement_gate(make_pair())

    assert result.admitted
    assert result.reason == "admitted"
    assert result.reasons == ()
    assert all(check.passed for check in result.checks)
    assert [check.stage for check in result.checks] == list(STAGE_ORDER)
    assert result.details["stages_failed"] == []


def test_complement_comparators_with_inverse_polarity_are_admitted() -> None:
    poly = base_metadata()
    poly["fingerprint"]["comparator"] = "<"
    result = settlement_gate(make_pair(polymarket=poly, same_polarity=False))

    assert result.admitted
    assert result.details["fingerprint_relation"] == "complement"


def test_explicit_identical_intervals_are_admitted() -> None:
    kalshi, poly = base_metadata(), base_metadata()
    kalshi["interval"] = {"lower": "0.25", "upper": None}
    poly["interval"] = {"lower": "0.25", "upper": None}
    result = settlement_gate(make_pair(kalshi=kalshi, polymarket=poly))

    assert result.admitted
    assert result.check(GateStage.INTERVAL).reason == "ok"


def test_heuristic_match_above_confidence_floor_passes_match_stage() -> None:
    result = settlement_gate(make_pair(method="heuristic", confidence=0.75))

    assert result.admitted
    assert result.check(GateStage.MATCH).passed


def test_committed_fixture_fed_pair_is_admitted_and_others_refused() -> None:
    kalshi, _ = load_fixture(KALSHI_FIXTURE, Venue.KALSHI)
    poly, _ = load_fixture(POLY_FIXTURE, Venue.POLYMARKET)
    results = {pair.pair_id: settlement_gate(pair) for pair in MarketMatcher().match(kalshi, poly)}

    assert results["fed-rate-cut-september"].admitted
    cpi = results["august-cpi-over-3"]
    assert not cpi.admitted
    assert cpi.reason == "clause_refuse_mismatch"
    assert "fingerprint_indeterminate" in cpi.reasons  # fallback flag present on one side only
    nba = results["nba-new-york-boston"]
    assert not nba.admitted
    assert nba.reasons == ("fingerprint_indeterminate", "polarity_unverified", "host_tier_not_allowed")
    assert nba.details["host_conflict"] is True


# --------------------------------------------------------------------------
# Must reject: one mutation, one (or a fixed set of) reason(s)
# --------------------------------------------------------------------------
REJECT_CASES: list[tuple[str, Mutation, tuple[str, ...]]] = [
    # clauses -----------------------------------------------------------------
    ("fallback stated on one side", _text(TEXT_FALLBACK), ("clause_refuse_mismatch",)),
    ("tie-break differs", _text(TEXT_TIE_LOWER), ("clause_refuse_mismatch",)),
    ("revisions differ", _text(TEXT_REVISIONS_IN), ("clause_refuse_mismatch",)),
    ("silent on tie-break", _text(TEXT_NO_TIE), ("clause_refuse_mismatch",)),
    (
        "unreadable text (no source URL)",
        _text("Resolves per the Fed announcement. Ties use the higher bracket."),
        ("clause_refuse_unreadable",),
    ),
    # fingerprint ---------------------------------------------------------------
    ("fingerprint missing", _drop("fingerprint"), ("fingerprint_indeterminate", "polarity_unverified")),
    (
        "fingerprint unparseable publisher",
        _fp(publisher="moon"),
        ("fingerprint_indeterminate", "polarity_unverified"),
    ),
    (
        "fingerprint unknown publisher",
        _fp(publisher="unknown"),
        ("fingerprint_indeterminate", "polarity_unverified"),
    ),
    (
        "optional field on one side only",
        _fp(fallback_to_prior_period=_DROP),
        ("fingerprint_indeterminate", "polarity_unverified"),
    ),
    ("threshold differs", _fp(threshold="0.50"), ("fingerprint_not_equivalent", "polarity_unverified")),
    ("reference period differs", _fp(reference_period="2026-12"), ("fingerprint_not_equivalent", "polarity_unverified")),
    ("publisher differs", _fp(publisher="bls", release_id="cpi"), ("fingerprint_not_equivalent", "polarity_unverified")),
    ("release differs", _fp(release_id="cpi"), ("fingerprint_not_equivalent", "polarity_unverified")),
    ("revisions flag differs", _fp(revisions_included=True), ("fingerprint_not_equivalent", "polarity_unverified")),
    ("tie-break flag differs", _fp(tie_break="lower"), ("fingerprint_not_equivalent", "polarity_unverified")),
    (
        ">= vs <= is not a safe complement",
        _fp(comparator="<="),
        ("fingerprint_not_equivalent", "polarity_unverified"),
    ),
    # fed bucket ----------------------------------------------------------------
    ("fed bucket same domain, different size", _set("fed_bucket", "C26"), ("fed_bucket_domain",)),
    ("fed bucket union label", _set("fed_bucket", ["C25", "C26"]), ("fed_bucket_union",)),
    ("fed bucket opposite domain", _set("fed_bucket", "H25"), ("fed_bucket_no_match",)),
    ("fed bucket one side only", _drop("fed_bucket"), ("fed_bucket_one_side",)),
    ("fed bucket unparseable", _set("fed_bucket", "CUT"), ("fed_bucket_unparseable",)),
    # interval ------------------------------------------------------------------
    (
        "point bucket vs threshold half-line",
        _set("interval", {"lower": "0.25", "upper": "0.50"}),
        ("interval_mismatch",),
    ),
    ("interval unparseable", _set("interval", {"lower": "x"}), ("interval_unparseable",)),
    # hosts -----------------------------------------------------------------------
    ("media host", _set("source_url", "https://www.reuters.com/markets/"), ("host_tier_not_allowed",)),
    ("aggregator host", _set("source_url", "https://fred.stlouisfed.org/series/"), ("host_tier_not_allowed",)),
    ("unclassified host", _set("source_url", "https://example.org/fed"), ("host_tier_not_allowed",)),
    ("self-referential host", _set("source_url", "https://polymarket.com/rules"), ("host_self_referential",)),
    (
        "same tier, different official publisher",
        _set("source_url", "https://www.bls.gov/cpi/"),
        ("host_publisher_differs",),
    ),
    # expiry ----------------------------------------------------------------------
    ("expiry months apart", _set("close_time", "2026-12-10T19:00:00Z"), ("expiry_mismatch",)),
    ("expiry known on one side only", _drop("close_time"), ("expiry_one_side",)),
    ("expiry unparseable", _set("close_time", "soon"), ("expiry_unparseable",)),
]


@pytest.mark.parametrize(("label", "mutate", "expected"), REJECT_CASES, ids=[c[0] for c in REJECT_CASES])
def test_single_mismatch_is_refused_with_its_stage_reason(label: str, mutate: Mutation, expected: tuple[str, ...]) -> None:
    poly = base_metadata()
    mutate(poly)
    result = settlement_gate(make_pair(polymarket=poly))

    assert not result.admitted, label
    assert result.reasons == expected, (label, result.reasons)
    assert result.reason == expected[0]


def test_polarity_conflict_equivalent_fingerprints_inverse_polarity() -> None:
    result = settlement_gate(make_pair(same_polarity=False))

    assert not result.admitted
    assert result.reasons == ("fingerprint_polarity_conflict",)


def test_polarity_conflict_complement_fingerprints_same_polarity() -> None:
    poly = base_metadata()
    poly["fingerprint"]["comparator"] = "<"
    result = settlement_gate(make_pair(polymarket=poly, same_polarity=True))

    assert not result.admitted
    assert result.reasons == ("fingerprint_polarity_conflict",)


def test_low_confidence_heuristic_match_is_refused() -> None:
    result = settlement_gate(make_pair(method="heuristic", confidence=0.5))

    assert not result.admitted
    assert result.reasons == ("match_low_confidence",)


def test_interval_stated_on_one_side_without_derivable_counterpart() -> None:
    kalshi, poly = base_metadata(), base_metadata()
    poly["interval"] = {"lower": "0.25", "upper": None}
    kalshi.pop("fingerprint")
    result = settlement_gate(make_pair(kalshi=kalshi, polymarket=poly))

    assert not result.admitted
    assert "interval_one_side" in result.reasons


def test_inverse_polarity_explicit_intervals_must_be_complementary_half_lines() -> None:
    kalshi, poly = base_metadata(), base_metadata()
    poly["fingerprint"]["comparator"] = "<"
    kalshi["interval"] = {"lower": "0.25", "upper": None}
    poly["interval"] = {"lower": None, "upper": "0.50"}  # different boundary
    result = settlement_gate(make_pair(kalshi=kalshi, polymarket=poly, same_polarity=False))

    assert not result.admitted
    assert result.reasons == ("interval_not_complementary",)


def test_fed_bucket_is_derived_from_ticker_and_question_when_metadata_is_absent() -> None:
    kalshi_meta, poly_meta = base_metadata(), base_metadata()
    kalshi_meta.pop("fed_bucket")
    poly_meta.pop("fed_bucket")
    kalshi = Market(Venue.KALSHI, "KXFEDDECISION-26SEP-H25", "Will the Federal Reserve Hike rates by 25bps at their September 2026 meeting?", metadata=kalshi_meta)
    cut = Market(Venue.POLYMARKET, "0xcut", "Will the Fed decrease interest rates by 25 bps after the September 2026 meeting?", metadata=poly_meta)
    hike = Market(Venue.POLYMARKET, "0xhike", "Will the Fed increase interest rates by 25 bps after the September 2026 meeting?", metadata=poly_meta)

    def gate(poly: Market):
        return settlement_gate(MatchedMarketPair("live", kalshi, poly, True, 1.0, "curated"))

    mismatch = gate(cut)
    assert not mismatch.admitted
    assert mismatch.reasons == ("fed_bucket_no_match",)
    assert mismatch.check(GateStage.FED_BUCKET).details["kalshi_fed_bucket_source"] == "derived"
    assert mismatch.check(GateStage.FED_BUCKET).details["polymarket_fed_bucket"] == "C25"

    match = gate(hike)
    assert match.check(GateStage.FED_BUCKET).passed
    assert match.check(GateStage.FED_BUCKET).details["fed_match_type"] == "exact"


def test_explicit_fed_bucket_metadata_overrides_the_derived_one() -> None:
    poly_meta = base_metadata()
    poly_meta["fed_bucket"] = "H25"  # operator says H25 even though the title reads like a cut
    kalshi = Market(Venue.KALSHI, "KXFEDDECISION-26SEP-H25", "Hike 25bps", metadata=base_metadata() | {"fed_bucket": "H25"})
    poly = Market(Venue.POLYMARKET, "0x", "Will the Fed decrease interest rates by 25 bps after the September 2026 meeting?", metadata=poly_meta)
    result = settlement_gate(MatchedMarketPair("live", kalshi, poly, True, 1.0, "curated"))

    check = result.check(GateStage.FED_BUCKET)
    assert check.passed and check.details["polymarket_fed_bucket_source"] == "explicit"


def test_host_missing_on_one_side() -> None:
    poly = base_metadata()
    poly["source_url"] = None
    poly["resolution_text"] = (
        "This market resolves according to the Fed. It will not fall back to a prior period. "
        "Exact boundary ties use the higher bracket. Revisions are excluded."
    )
    result = settlement_gate(make_pair(polymarket=poly))

    assert not result.admitted
    assert result.reasons == ("clause_refuse_unreadable", "host_missing")


# --------------------------------------------------------------------------
# Reporting contract
# --------------------------------------------------------------------------
def test_every_failing_stage_is_reported_in_stage_order() -> None:
    poly = base_metadata()
    _text(TEXT_FALLBACK)(poly)
    _fp(threshold="0.50")(poly)
    _set("fed_bucket", "H25")(poly)
    _set("source_url", "https://www.reuters.com/")(poly)
    _set("close_time", "2027-01-01T00:00:00Z")(poly)
    result = settlement_gate(make_pair(polymarket=poly))

    assert not result.admitted
    assert result.reasons == (
        "clause_refuse_mismatch",
        "fingerprint_not_equivalent",
        "polarity_unverified",
        "fed_bucket_no_match",
        "host_tier_not_allowed",
        "expiry_mismatch",
    )
    assert result.reason == "clause_refuse_mismatch"
    assert result.details["stages_failed"] == ["clauses", "fingerprint", "polarity", "fed_bucket", "hosts", "expiry"]


def test_gate_result_serialises_and_keeps_legacy_detail_keys() -> None:
    result = settlement_gate(make_pair())
    payload = result.as_dict()

    json.dumps(payload)
    for key in (
        "clause_verdict", "fingerprint_relation", "fingerprint_reason", "kalshi_host_tier",
        "polymarket_host_tier", "host_conflict", "kalshi_clauses", "polymarket_clauses",
        "match_method", "match_confidence",
    ):
        assert key in payload, key
    assert payload["policy"] == "strict"
    assert [check["stage"] for check in payload["checks"]] == [stage.value for stage in STAGE_ORDER]


def test_scoreboard_re_exports_the_same_gate() -> None:
    assert scoreboard_gate is settlement_gate


def test_loosened_policy_is_a_visible_operator_choice() -> None:
    poly = base_metadata()
    poly["source_url"] = "https://www.bls.gov/"
    loose = GatePolicy(name="loose", require_same_publisher=False)

    strict_result = settlement_gate(make_pair(polymarket=poly))
    loose_result = settlement_gate(make_pair(polymarket=poly), policy=loose)

    assert not strict_result.admitted
    assert loose_result.admitted
    assert loose_result.as_dict()["policy"] == "loose"
    assert STRICT_POLICY.as_dict()["allowed_host_tiers"] == ["official"]
