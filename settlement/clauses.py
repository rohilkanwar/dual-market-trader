"""Regex extraction of settlement clauses that decide cross-venue admissibility.

The pair verdict is fail-closed: silence on one side about a clause the other
side states explicitly is a mismatch, not a match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from settlement.fingerprint import TieBreak

_URL = re.compile(r"https?://[^\s)\]]+", re.IGNORECASE)
_ACCORDING = re.compile(r"\b(?:according to|based on|as (?:reported|published) by)\b", re.IGNORECASE)
_NO_FALLBACK = re.compile(
    r"\b(?:will|does|shall|would) not fall back\b|\bno fallback\b|\bwithout (?:any )?fallback\b",
    re.IGNORECASE,
)
_FALLBACK = (
    re.compile(
        r"\bfall(?:s|ing)? back to (?:(?:the|a) )?"
        r"(?:last|prior|previous|most recent) (?:available )?"
        r"(?:month|period|release|observation|data)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\buse(?:s|d)? the (?:last|prior|previous|most recent) available\b", re.IGNORECASE),
)
_TIE_HIGHER = re.compile(r"\bties? (?:use|go to|resolve to) the higher\b|\bround(?:ed|s)? up\b", re.IGNORECASE)
_TIE_LOWER = re.compile(r"\bties? (?:use|go to|resolve to) the lower\b|\bround(?:ed|s)? down\b", re.IGNORECASE)
_TIE_NONE = re.compile(r"\bno tie-?break\b", re.IGNORECASE)
_REVISIONS_EXCLUDED = re.compile(
    r"\brevisions? (?:are|is|will be) (?:excluded|ignored|not considered)\b|\binitial(?:ly)? (?:release|published|reported)\b",
    re.IGNORECASE,
)
_REVISIONS_INCLUDED = re.compile(
    r"\brevisions? (?:are|is|will be) (?:included|considered)\b|\b(?:final|revised) (?:release|value|figure)\b",
    re.IGNORECASE,
)


class ClauseVerdict(StrEnum):
    ADMIT = "admit"
    REFUSE_MISMATCH = "refuse_mismatch"
    REFUSE_UNREADABLE = "refuse_unreadable"


@dataclass(frozen=True, slots=True)
class ClauseSet:
    fallback: bool | None
    tie_break: TieBreak | None
    revisions_excluded: bool | None
    source_urls: tuple[str, ...]
    usable: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "fallback": self.fallback,
            "tie_break": self.tie_break.value if self.tie_break else None,
            "revisions_excluded": self.revisions_excluded,
            "source_urls": list(self.source_urls),
            "usable": self.usable,
        }


def detect_clauses(text: str) -> ClauseSet:
    text = text or ""
    urls = tuple(match.rstrip(".,;") for match in _URL.findall(text))

    if _NO_FALLBACK.search(text):
        fallback: bool | None = False
    elif any(pattern.search(text) for pattern in _FALLBACK):
        fallback = True
    else:
        fallback = None

    if _TIE_NONE.search(text):
        tie_break: TieBreak | None = TieBreak.NONE
    elif _TIE_HIGHER.search(text):
        tie_break = TieBreak.HIGHER
    elif _TIE_LOWER.search(text):
        tie_break = TieBreak.LOWER
    else:
        tie_break = None

    if _REVISIONS_INCLUDED.search(text):
        revisions_excluded: bool | None = False
    elif _REVISIONS_EXCLUDED.search(text):
        revisions_excluded = True
    else:
        revisions_excluded = None

    has_source = bool(urls) or bool(_ACCORDING.search(text))
    has_clause = any(v is not None for v in (fallback, tie_break, revisions_excluded))
    usable = bool(urls) and (has_source or has_clause)
    return ClauseSet(fallback, tie_break, revisions_excluded, urls, usable)


def pair_clause_verdict(left: ClauseSet, right: ClauseSet) -> ClauseVerdict:
    if not (left.usable and right.usable):
        return ClauseVerdict.REFUSE_UNREADABLE
    for name in ("fallback", "tie_break", "revisions_excluded"):
        if getattr(left, name) != getattr(right, name):
            return ClauseVerdict.REFUSE_MISMATCH
    return ClauseVerdict.ADMIT
