"""Pair Kalshi and Polymarket markets that describe the same event.

Curated pairs are authoritative. The heuristic matcher is a token-overlap
fallback over title + event/slug context; it is a *candidate generator* only,
settlement gates decide admissibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from core.types import Market, Venue


@dataclass(frozen=True, slots=True)
class CuratedPair:
    pair_id: str
    kalshi_market_id: str
    polymarket_market_id: str
    same_polarity: bool = True


@dataclass(frozen=True, slots=True)
class MatchedMarketPair:
    pair_id: str
    kalshi: Market
    polymarket: Market
    same_polarity: bool
    confidence: float
    method: str

    @property
    def category(self) -> str:
        return self.kalshi.category or self.polymarket.category


CURATED_PAIRS: tuple[CuratedPair, ...] = (
    CuratedPair(
        pair_id="fed-rate-cut-september",
        kalshi_market_id="KX-FED-SEP-CUT",
        polymarket_market_id="0xfixture-fed-september",
    ),
    CuratedPair(
        pair_id="august-cpi-over-3",
        kalshi_market_id="KX-CPI-AUG-OVER3",
        polymarket_market_id="0xfixture-cpi-august",
    ),
    CuratedPair(
        pair_id="nba-new-york-boston",
        kalshi_market_id="KX-NBA-NY-BOS-NY",
        polymarket_market_id="0xfixture-nba-ny-boston",
    ),
)

_ALIASES = {
    "fed": ("federal", "reserve"),
    "fomc": ("federal", "reserve"),
    "sep": ("september",),
    "sept": ("september",),
    "oct": ("october",),
    "nov": ("november",),
    "dec": ("december",),
    "jan": ("january",),
    "feb": ("february",),
    "aug": ("august",),
    "jul": ("july",),
    "jun": ("june",),
    "us": ("united", "states"),
    "u.s.": ("united", "states"),
    "ny": ("new", "york"),
    "bos": ("boston",),
}
_STOPWORDS = {
    "a", "an", "the", "will", "be", "in", "on", "at", "of", "to", "by", "for", "is",
    "and", "or", "vs", "market", "contract", "event", "question", "than", "does",
}
_NEGATION = re.compile(r"\b(?:not|no|fail|fails|under|below|less|fewer|lose|loses)\b", re.IGNORECASE)
_TOKEN = re.compile(r"[a-z0-9.%]+")
_MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
)
_YEAR = re.compile(r"\b(20\d{2})\b")
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*%?(?![\w.])")


def period_tokens(*texts: str | None) -> tuple[frozenset[str], frozenset[str]]:
    """``(months, years)`` present in the texts (month aliases expanded)."""
    tokens = _tokens(*texts)
    months = frozenset(token for token in tokens if token in _MONTHS)
    years = frozenset(match for text in texts if text for match in _YEAR.findall(text))
    return months, years


def numeric_tokens(*texts: str | None) -> frozenset[Decimal]:
    """Standalone numbers (thresholds like ``3.0%``), excluding years."""
    out: set[Decimal] = set()
    for text in texts:
        if not text:
            continue
        for raw in _NUMBER.findall(text):
            if _YEAR.fullmatch(raw):
                continue
            out.add(Decimal(raw).normalize())
    return frozenset(out)


def _market_texts(market: Market) -> tuple[str, ...]:
    return (
        market.title,
        str(market.metadata.get("event_title") or ""),
        str(market.metadata.get("subtitle") or ""),
    )


def consistent_reference(left: Market, right: Market) -> tuple[bool, str]:
    """Heuristic-candidate veto: both sides naming a period or a threshold must agree.

    Silence on one side is *not* a veto here; the settlement gate's fingerprint
    stage is where one-sided information fails closed. This check only stops
    the matcher from pairing e.g. a September contract with a December one.
    """
    left_months, left_years = period_tokens(*_market_texts(left))
    right_months, right_years = period_tokens(*_market_texts(right))
    if left_months and right_months and not (left_months & right_months):
        return False, "period_disagrees"
    if left_years and right_years and not (left_years & right_years):
        return False, "period_disagrees"
    left_numbers, right_numbers = numeric_tokens(*_market_texts(left)), numeric_tokens(*_market_texts(right))
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return False, "threshold_disagrees"
    return True, "ok"


def _tokens(*texts: str | None) -> set[str]:
    out: set[str] = set()
    for text in texts:
        if not text:
            continue
        for token in _TOKEN.findall(text.lower().replace("-", " ").replace("_", " ")):
            token = token.strip(".")
            if len(token) <= 1 or token in _STOPWORDS:
                continue
            out.update(_ALIASES.get(token, (token,)))
    return out


def market_tokens(market: Market) -> set[str]:
    return _tokens(
        market.title,
        str(market.metadata.get("event_title") or ""),
        str(market.metadata.get("slug") or ""),
        str(market.metadata.get("subtitle") or ""),
    )


def same_title_polarity(left: str, right: str) -> bool:
    return bool(_NEGATION.search(left or "")) == bool(_NEGATION.search(right or ""))


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass(frozen=True, slots=True)
class VetoedCandidate:
    """A heuristic candidate that cleared the token threshold but was refused."""

    kalshi_market_id: str
    polymarket_market_id: str
    confidence: float
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "kalshi": self.kalshi_market_id,
            "polymarket": self.polymarket_market_id,
            "confidence": self.confidence,
            "reason": self.reason,
        }


class MarketMatcher:
    def __init__(
        self,
        curated_pairs: tuple[CuratedPair, ...] = CURATED_PAIRS,
        *,
        minimum_confidence: float = 0.5,
    ) -> None:
        self.curated_pairs = curated_pairs
        self.minimum_confidence = minimum_confidence
        self.vetoed: list[VetoedCandidate] = []

    def match(
        self,
        kalshi_markets: list[Market],
        polymarket_markets: list[Market],
    ) -> list[MatchedMarketPair]:
        kalshi_by_id = {m.market_id: m for m in kalshi_markets if m.venue is Venue.KALSHI}
        poly_by_id = {m.market_id: m for m in polymarket_markets if m.venue is Venue.POLYMARKET}
        pairs: list[MatchedMarketPair] = []
        used_kalshi: set[str] = set()
        used_poly: set[str] = set()
        self.vetoed = []

        for curated in self.curated_pairs:
            kalshi = kalshi_by_id.get(curated.kalshi_market_id)
            poly = poly_by_id.get(curated.polymarket_market_id)
            if kalshi is None or poly is None:
                continue
            pairs.append(
                MatchedMarketPair(
                    pair_id=curated.pair_id,
                    kalshi=kalshi,
                    polymarket=poly,
                    same_polarity=curated.same_polarity,
                    confidence=1.0,
                    method="curated",
                )
            )
            used_kalshi.add(kalshi.market_id)
            used_poly.add(poly.market_id)

        candidates: list[tuple[float, Market, Market]] = []
        poly_tokens = {m.market_id: market_tokens(m) for m in poly_by_id.values()}
        for kalshi in kalshi_by_id.values():
            if kalshi.market_id in used_kalshi:
                continue
            k_tokens = market_tokens(kalshi)
            for poly in poly_by_id.values():
                if poly.market_id in used_poly:
                    continue
                score = jaccard(k_tokens, poly_tokens[poly.market_id])
                if score < self.minimum_confidence:
                    continue
                consistent, veto_reason = consistent_reference(kalshi, poly)
                if not consistent:
                    self.vetoed.append(
                        VetoedCandidate(kalshi.market_id, poly.market_id, round(score, 4), veto_reason)
                    )
                    continue
                candidates.append((score, kalshi, poly))
        for score, kalshi, poly in sorted(candidates, key=lambda c: c[0], reverse=True):
            if kalshi.market_id in used_kalshi or poly.market_id in used_poly:
                continue
            used_kalshi.add(kalshi.market_id)
            used_poly.add(poly.market_id)
            pairs.append(
                MatchedMarketPair(
                    pair_id=f"heuristic:{kalshi.market_id}:{poly.market_id}",
                    kalshi=kalshi,
                    polymarket=poly,
                    same_polarity=same_title_polarity(kalshi.title, poly.title),
                    confidence=round(score, 4),
                    method="heuristic",
                )
            )
        return pairs
