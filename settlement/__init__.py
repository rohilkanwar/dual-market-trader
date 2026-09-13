"""Settlement-rule extraction and equivalence policies."""

from settlement.clauses import (
    ClauseSet,
    ClauseVerdict,
    detect_clauses,
    pair_clause_verdict,
)
from settlement.fingerprint import (
    Comparator,
    MatchVerdict,
    Relation,
    ResolutionFingerprint,
    TieBreak,
    compare,
)

__all__ = [
    "ClauseSet",
    "ClauseVerdict",
    "Comparator",
    "MatchVerdict",
    "Relation",
    "ResolutionFingerprint",
    "TieBreak",
    "compare",
    "detect_clauses",
    "pair_clause_verdict",
]
