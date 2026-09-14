"""Fail-closed cross-venue admissibility gate.

A Kalshi/Polymarket pair is *admissible* only when every stage below passes.
Each stage is evaluated independently so a refused pair reports **every**
failing reason, not just the first; ``GateResult.reason`` is the first failure
in stage order and is what the track histograms count.

Stage order (and the reason prefixes each one emits):

1. ``match``        curated pairs pass; heuristic pairs need a minimum
                    token-overlap confidence (``match_low_confidence``).
2. ``clauses``      regex-extracted fallback / tie-break / revision clauses must
                    be readable on both sides and identical
                    (``clause_refuse_unreadable``, ``clause_refuse_mismatch``).
3. ``fingerprint``  structured resolution fingerprints must be present and
                    ``EQUIVALENT`` or a safe ``COMPLEMENT``
                    (``fingerprint_indeterminate``, ``fingerprint_not_equivalent``).
4. ``polarity``     the fingerprint relation must agree with the matcher's
                    polarity (``fingerprint_polarity_conflict``).
5. ``fed_bucket``   when either side names an FOMC outcome bucket both must,
                    and they must match ``EXACT`` (domain / union labels are
                    not interchangeable) (``fed_bucket_*``).
6. ``interval``     when either side states a numeric bucket, the other side's
                    bucket (explicit or derived from its fingerprint) must be
                    identical, or an exact complement for inverse polarity
                    (``interval_*``).
7. ``hosts``        resolution sources must be in an allowed tier, the same
                    tier, and the same publisher (``host_*``).
8. ``expiry``       when close times are known on both sides they must agree
                    within a tolerance; one-sided knowledge is refused
                    (``expiry_*``).

Nothing here looks at prices. Whether an admitted pair has a paper edge is a
separate question answered by ``strategies.paper_edge``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Mapping

from settlement.bucket_match import Interval
from settlement.clauses import ClauseSet, ClauseVerdict, detect_clauses, pair_clause_verdict
from settlement.fed_match import FedBucket, FedMatchType, match_fed_buckets
from settlement.fingerprint import (
    Comparator,
    Relation,
    ResolutionFingerprint,
    compare,
    from_mapping,
)
from settlement.hosts import HostTier, classify, registrable_domain, same_publisher

if TYPE_CHECKING:
    from strategies.matching import MatchedMarketPair


class GateStage(StrEnum):
    MATCH = "match"
    CLAUSES = "clauses"
    FINGERPRINT = "fingerprint"
    POLARITY = "polarity"
    FED_BUCKET = "fed_bucket"
    INTERVAL = "interval"
    HOSTS = "hosts"
    EXPIRY = "expiry"


STAGE_ORDER: tuple[GateStage, ...] = tuple(GateStage)
OK = "ok"
NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class GatePolicy:
    """Knobs of the gate. The defaults are the strict policy used by
    ``gated_cross_venue``; loosening any of them is a deliberate operator act
    that shows up in the gate report."""

    name: str = "strict"
    minimum_heuristic_confidence: float = 0.6
    allowed_host_tiers: frozenset[HostTier] = frozenset({HostTier.OFFICIAL})
    require_same_publisher: bool = True
    expiry_tolerance: timedelta = timedelta(days=3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "minimum_heuristic_confidence": self.minimum_heuristic_confidence,
            "allowed_host_tiers": sorted(tier.value for tier in self.allowed_host_tiers),
            "require_same_publisher": self.require_same_publisher,
            "expiry_tolerance_hours": self.expiry_tolerance.total_seconds() / 3600,
        }


STRICT_POLICY = GatePolicy()


@dataclass(frozen=True, slots=True)
class GateCheck:
    stage: GateStage
    passed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "passed": self.passed,
            "reason": self.reason,
            "details": _jsonable(self.details),
        }


@dataclass(frozen=True, slots=True)
class GateResult:
    admitted: bool
    reason: str
    reasons: tuple[str, ...]
    checks: tuple[GateCheck, ...]
    details: dict[str, Any]
    policy: GatePolicy = STRICT_POLICY

    def check(self, stage: GateStage) -> GateCheck:
        return next(check for check in self.checks if check.stage is stage)

    def as_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "checks": [check.as_dict() for check in self.checks],
            "policy": self.policy.name,
            **_jsonable(self.details),
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------
def _check_match(pair: MatchedMarketPair, policy: GatePolicy) -> GateCheck:
    details = {"match_method": pair.method, "match_confidence": pair.confidence}
    if pair.method == "curated":
        return GateCheck(GateStage.MATCH, True, OK, details)
    if pair.confidence < policy.minimum_heuristic_confidence:
        return GateCheck(GateStage.MATCH, False, "match_low_confidence", details)
    return GateCheck(GateStage.MATCH, True, OK, details)


def _check_clauses(k_clauses: ClauseSet, p_clauses: ClauseSet) -> GateCheck:
    verdict = pair_clause_verdict(k_clauses, p_clauses)
    details = {
        "clause_verdict": verdict.value,
        "kalshi_clauses": k_clauses.as_dict(),
        "polymarket_clauses": p_clauses.as_dict(),
    }
    if verdict is ClauseVerdict.ADMIT:
        return GateCheck(GateStage.CLAUSES, True, OK, details)
    return GateCheck(GateStage.CLAUSES, False, f"clause_{verdict.value}", details)


def _parse_fingerprint(raw: Any) -> tuple[ResolutionFingerprint | None, str | None]:
    if not isinstance(raw, Mapping):
        return None, "missing"
    try:
        return from_mapping(raw), None
    except (ValueError, InvalidOperation, TypeError) as exc:
        return None, f"unparseable: {exc}"


def _check_fingerprint(
    k_fp: ResolutionFingerprint | None,
    k_err: str | None,
    p_fp: ResolutionFingerprint | None,
    p_err: str | None,
) -> tuple[GateCheck, Relation]:
    if k_fp is None or p_fp is None:
        errors = [(side, err) for side, err in (("kalshi", k_err), ("polymarket", p_err)) if err]
        details = {
            "fingerprint_relation": Relation.INDETERMINATE.value,
            "fingerprint_reason": "fingerprint " + ", ".join(f"{side}: {err}" for side, err in errors),
            "fingerprint_missing_sides": [side for side, _ in errors],
        }
        return GateCheck(GateStage.FINGERPRINT, False, "fingerprint_indeterminate", details), Relation.INDETERMINATE
    verdict = compare(k_fp, p_fp)
    details = {"fingerprint_relation": verdict.relation.value, "fingerprint_reason": verdict.reason}
    if verdict.relation in (Relation.EQUIVALENT, Relation.COMPLEMENT):
        return GateCheck(GateStage.FINGERPRINT, True, OK, details), verdict.relation
    return (
        GateCheck(GateStage.FINGERPRINT, False, f"fingerprint_{verdict.relation.value}", details),
        verdict.relation,
    )


def _check_polarity(pair: MatchedMarketPair, relation: Relation) -> GateCheck:
    details = {"same_polarity": pair.same_polarity, "fingerprint_relation": relation.value}
    if relation is Relation.EQUIVALENT and pair.same_polarity:
        return GateCheck(GateStage.POLARITY, True, OK, details)
    if relation is Relation.COMPLEMENT and not pair.same_polarity:
        return GateCheck(GateStage.POLARITY, True, OK, details)
    if relation in (Relation.EQUIVALENT, Relation.COMPLEMENT):
        return GateCheck(GateStage.POLARITY, False, "fingerprint_polarity_conflict", details)
    # Without a usable fingerprint the matcher's polarity guess is unverified.
    return GateCheck(GateStage.POLARITY, False, "polarity_unverified", details)


def _fed_buckets(raw: Any) -> tuple[frozenset[FedBucket] | None, str | None]:
    if raw is None or raw == "" or raw == []:
        return None, None
    values = raw if isinstance(raw, (list, tuple, set, frozenset)) else [raw]
    try:
        return frozenset(FedBucket(str(v)) for v in values), None
    except ValueError as exc:
        return None, str(exc)


def _check_fed_bucket(k_raw: Any, p_raw: Any) -> GateCheck:
    k_set, k_err = _fed_buckets(k_raw)
    p_set, p_err = _fed_buckets(p_raw)
    details: dict[str, Any] = {"kalshi_fed_bucket": k_raw, "polymarket_fed_bucket": p_raw}
    if k_err or p_err:
        details["error"] = k_err or p_err
        return GateCheck(GateStage.FED_BUCKET, False, "fed_bucket_unparseable", details)
    if k_set is None and p_set is None:
        return GateCheck(GateStage.FED_BUCKET, True, NOT_APPLICABLE, details)
    if k_set is None or p_set is None:
        return GateCheck(GateStage.FED_BUCKET, False, "fed_bucket_one_side", details)
    match = match_fed_buckets(k_set, p_set)
    details["fed_match_type"] = match.match_type.value
    if match.match_type is FedMatchType.EXACT:
        return GateCheck(GateStage.FED_BUCKET, True, OK, details)
    return GateCheck(GateStage.FED_BUCKET, False, f"fed_bucket_{match.match_type.value}", details)


def _explicit_interval(raw: Any) -> tuple[Interval | None, str | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, Mapping):
        return None, "interval must be a mapping with lower/upper"
    try:
        lower = raw.get("lower")
        upper = raw.get("upper")
        interval = Interval(
            Decimal(str(lower)) if lower is not None else None,
            Decimal(str(upper)) if upper is not None else None,
        )
    except (InvalidOperation, ValueError) as exc:
        return None, f"unparseable interval: {exc}"
    if interval.lower is not None and interval.upper is not None and interval.upper < interval.lower:
        return None, "interval upper bound below lower bound"
    return interval, None


def interval_from_fingerprint(fingerprint: ResolutionFingerprint | None) -> Interval | None:
    """Half-line implied by a threshold comparator; ``None`` when not derivable.

    The ``(lower, upper]`` convention of :class:`Interval` cannot express the
    open/closed distinction between ``>`` and ``>=``; that distinction is
    already enforced by the fingerprint comparator check, so here the two map
    to the same half-line.
    """
    if fingerprint is None or fingerprint.threshold is None:
        return None
    if fingerprint.comparator in (Comparator.GT, Comparator.GE):
        return Interval(fingerprint.threshold, None)
    if fingerprint.comparator in (Comparator.LT, Comparator.LE):
        return Interval(None, fingerprint.threshold)
    return None


def _complementary_half_lines(left: Interval, right: Interval) -> bool:
    return (
        left.lower is None and right.upper is None and left.upper is not None and left.upper == right.lower
    ) or (
        right.lower is None and left.upper is None and right.upper is not None and right.upper == left.lower
    )


def _check_interval(
    pair: MatchedMarketPair,
    k_raw: Any,
    p_raw: Any,
    k_fp: ResolutionFingerprint | None,
    p_fp: ResolutionFingerprint | None,
) -> GateCheck:
    k_explicit, k_err = _explicit_interval(k_raw)
    p_explicit, p_err = _explicit_interval(p_raw)
    details: dict[str, Any] = {}
    if k_err or p_err:
        details["error"] = k_err or p_err
        return GateCheck(GateStage.INTERVAL, False, "interval_unparseable", details)
    if k_explicit is None and p_explicit is None:
        return GateCheck(GateStage.INTERVAL, True, NOT_APPLICABLE, details)
    k_interval = k_explicit or interval_from_fingerprint(k_fp)
    p_interval = p_explicit or interval_from_fingerprint(p_fp)
    details.update(
        {
            "kalshi_interval": k_interval.as_dict() if k_interval else None,
            "polymarket_interval": p_interval.as_dict() if p_interval else None,
            "kalshi_interval_source": "explicit" if k_explicit else ("fingerprint" if k_interval else None),
            "polymarket_interval_source": "explicit" if p_explicit else ("fingerprint" if p_interval else None),
        }
    )
    if k_interval is None or p_interval is None:
        return GateCheck(GateStage.INTERVAL, False, "interval_one_side", details)
    if pair.same_polarity:
        if k_interval == p_interval:
            return GateCheck(GateStage.INTERVAL, True, OK, details)
        return GateCheck(GateStage.INTERVAL, False, "interval_mismatch", details)
    if _complementary_half_lines(k_interval, p_interval):
        return GateCheck(GateStage.INTERVAL, True, OK, details)
    return GateCheck(GateStage.INTERVAL, False, "interval_not_complementary", details)


def _source_url(metadata: Mapping[str, Any], clauses: ClauseSet) -> str:
    explicit = metadata.get("source_url")
    if explicit:
        return str(explicit)
    return clauses.source_urls[0] if clauses.source_urls else ""


def _check_hosts(k_url: str, p_url: str, policy: GatePolicy) -> GateCheck:
    k_tier, p_tier = classify(k_url), classify(p_url)
    legacy_conflict = k_tier != p_tier or HostTier.UNCLASSIFIED in (k_tier, p_tier)
    details: dict[str, Any] = {
        "kalshi_host_tier": k_tier.value,
        "polymarket_host_tier": p_tier.value,
        "kalshi_host": registrable_domain(k_url),
        "polymarket_host": registrable_domain(p_url),
        "host_conflict": legacy_conflict,
        "same_publisher": same_publisher(k_url, p_url),
    }
    if not k_url or not p_url:
        return GateCheck(GateStage.HOSTS, False, "host_missing", details)
    if HostTier.SELF in (k_tier, p_tier):
        return GateCheck(GateStage.HOSTS, False, "host_self_referential", details)
    if k_tier not in policy.allowed_host_tiers or p_tier not in policy.allowed_host_tiers:
        return GateCheck(GateStage.HOSTS, False, "host_tier_not_allowed", details)
    if k_tier != p_tier:
        return GateCheck(GateStage.HOSTS, False, "host_conflict", details)
    if policy.require_same_publisher and not details["same_publisher"]:
        return GateCheck(GateStage.HOSTS, False, "host_publisher_differs", details)
    return GateCheck(GateStage.HOSTS, True, OK, details)


def _parse_time(raw: Any) -> tuple[datetime | None, bool]:
    """(value, present). ``present`` is True when the field carried anything."""
    if raw in (None, ""):
        return None, False
    if isinstance(raw, datetime):
        return raw, True
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None, True
    return parsed, True


def _check_expiry(k_raw: Any, p_raw: Any, policy: GatePolicy) -> GateCheck:
    k_time, k_present = _parse_time(k_raw)
    p_time, p_present = _parse_time(p_raw)
    details: dict[str, Any] = {"kalshi_close_time": k_raw, "polymarket_close_time": p_raw}
    if not k_present and not p_present:
        return GateCheck(GateStage.EXPIRY, True, NOT_APPLICABLE, details)
    if (k_present and k_time is None) or (p_present and p_time is None):
        return GateCheck(GateStage.EXPIRY, False, "expiry_unparseable", details)
    if k_time is None or p_time is None:
        return GateCheck(GateStage.EXPIRY, False, "expiry_one_side", details)
    if (k_time.tzinfo is None) != (p_time.tzinfo is None):
        return GateCheck(GateStage.EXPIRY, False, "expiry_unparseable", {**details, "error": "mixed naive/aware"})
    delta = abs(k_time - p_time)
    details["expiry_delta_hours"] = delta.total_seconds() / 3600
    if delta > policy.expiry_tolerance:
        return GateCheck(GateStage.EXPIRY, False, "expiry_mismatch", details)
    return GateCheck(GateStage.EXPIRY, True, OK, details)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def settlement_gate(pair: MatchedMarketPair, *, policy: GatePolicy = STRICT_POLICY) -> GateResult:
    """Run every stage and fail closed on the first failure in stage order."""
    k_meta, p_meta = pair.kalshi.metadata, pair.polymarket.metadata
    k_clauses = detect_clauses(str(k_meta.get("resolution_text") or ""))
    p_clauses = detect_clauses(str(p_meta.get("resolution_text") or ""))
    k_fp, k_err = _parse_fingerprint(k_meta.get("fingerprint"))
    p_fp, p_err = _parse_fingerprint(p_meta.get("fingerprint"))

    fingerprint_check, relation = _check_fingerprint(k_fp, k_err, p_fp, p_err)
    checks = (
        _check_match(pair, policy),
        _check_clauses(k_clauses, p_clauses),
        fingerprint_check,
        _check_polarity(pair, relation),
        _check_fed_bucket(k_meta.get("fed_bucket"), p_meta.get("fed_bucket")),
        _check_interval(pair, k_meta.get("interval"), p_meta.get("interval"), k_fp, p_fp),
        _check_hosts(_source_url(k_meta, k_clauses), _source_url(p_meta, p_clauses), policy),
        _check_expiry(k_meta.get("close_time"), p_meta.get("close_time"), policy),
    )
    assert tuple(check.stage for check in checks) == STAGE_ORDER
    failures = tuple(check.reason for check in checks if not check.passed)
    admitted = not failures
    details: dict[str, Any] = {}
    for check in checks:
        details.update(check.details)
    details["stages_passed"] = [check.stage.value for check in checks if check.passed]
    details["stages_failed"] = [check.stage.value for check in checks if not check.passed]
    return GateResult(
        admitted=admitted,
        reason="admitted" if admitted else failures[0],
        reasons=failures,
        checks=checks,
        details=details,
        policy=policy,
    )
