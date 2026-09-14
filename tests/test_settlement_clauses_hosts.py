from settlement.clauses import (
    ClauseVerdict,
    detect_clauses,
    pair_clause_verdict,
)
from settlement.fingerprint import TieBreak
from settlement.hosts import HostTier, classify, registrable_domain, same_publisher


BASE = (
    "This market resolves according to the initial release published at "
    "https://www.bls.gov/cpi/. "
)


def test_clause_mismatch_refuses_fallback_silence() -> None:
    kalshi = detect_clauses(
        BASE + "Exact boundary ties use the higher bracket. Revisions are excluded."
    )
    polymarket = detect_clauses(
        BASE
        + "If delayed, it falls back to the last available month. "
        + "Exact boundary ties use the higher bracket. Revisions are excluded."
    )

    assert kalshi.usable and polymarket.usable
    assert kalshi.fallback is None
    assert polymarket.fallback is True
    assert pair_clause_verdict(kalshi, polymarket) is ClauseVerdict.REFUSE_MISMATCH


def test_negation_guard_records_explicit_no_fallback() -> None:
    clauses = detect_clauses(
        BASE
        + "It will not fall back to a prior period. "
        + "Exact boundary ties use the lower bracket. Revisions are included."
    )

    assert clauses.fallback is False
    assert clauses.tie_break is TieBreak.LOWER
    assert clauses.revisions_excluded is False


def test_unusable_text_is_refused() -> None:
    unreadable = detect_clauses("Resolves somehow.")
    usable = detect_clauses(BASE + "No tie-break applies and revisions are excluded.")
    assert pair_clause_verdict(unreadable, usable) is ClauseVerdict.REFUSE_UNREADABLE


def test_host_classification_uses_real_suffix_boundaries() -> None:
    assert classify("https://data.bls.gov/cpi") is HostTier.OFFICIAL
    assert classify("scores.espn.com") is HostTier.MEDIA
    assert classify("https://espn.com.evil.example/") is HostTier.UNCLASSIFIED
    assert classify("https://notespn.com/") is HostTier.UNCLASSIFIED


def test_registrable_domain_identifies_the_publisher_not_the_tier() -> None:
    assert registrable_domain("https://data.bls.gov/cpi") == "bls.gov"
    assert registrable_domain("https://www.federalreserve.gov/x") == "federalreserve.gov"
    assert registrable_domain("https://www.ons.gov.uk/") == "ons.gov.uk"
    assert registrable_domain("") == ""
    assert same_publisher("https://data.bls.gov/", "https://www.bls.gov/cpi/")
    # Both OFFICIAL, but different statistical agencies: not the same publisher.
    assert classify("https://www.bls.gov/") is classify("https://www.bea.gov/") is HostTier.OFFICIAL
    assert not same_publisher("https://www.bls.gov/", "https://www.bea.gov/")
    assert not same_publisher("", "")
