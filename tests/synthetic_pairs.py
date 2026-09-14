"""Builders for a fully admissible synthetic Kalshi/Polymarket pair.

Every gate test starts from ``base_metadata()`` (identical on both sides,
passes all eight stages) and mutates one field, so a refusal pins down the
stage that caught it. Kept outside the test modules so several suites share
one definition of "admissible".
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from core.types import Market, Venue
from strategies.matching import MatchedMarketPair

FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_TEXT = (
    "This market resolves according to the target range published at "
    "https://www.federalreserve.gov/. It will not fall back to a prior period. "
    "Exact boundary ties use the higher bracket. Revisions are excluded."
)
FED_FINGERPRINT: dict[str, Any] = {
    "publisher": "federal_reserve",
    "release_id": "fomc_target_rate",
    "comparator": ">=",
    "threshold": "0.25",
    "reference_period": "2026-09",
    "revisions_included": False,
    "fallback_to_prior_period": False,
    "tie_break": "higher",
}
CLOSE = "2026-09-17T18:00:00Z"
TITLE = "Will the Fed cut rates at the September 2026 meeting?"


def base_metadata() -> dict[str, Any]:
    return {
        "category": "macro",
        "source_url": FED_URL,
        "resolution_text": FED_TEXT,
        "fingerprint": deepcopy(FED_FINGERPRINT),
        "fed_bucket": "C25",
        "close_time": CLOSE,
    }


def market(venue: Venue, metadata: dict[str, Any], market_id: str = "", title: str = TITLE) -> Market:
    return Market(
        venue=venue,
        market_id=market_id or ("K-FED" if venue is Venue.KALSHI else "P-FED"),
        title=title,
        metadata=metadata,
    )


def make_pair(
    *,
    kalshi: dict[str, Any] | None = None,
    polymarket: dict[str, Any] | None = None,
    same_polarity: bool = True,
    method: str = "curated",
    confidence: float = 1.0,
) -> MatchedMarketPair:
    return MatchedMarketPair(
        pair_id="synthetic-fed",
        kalshi=market(Venue.KALSHI, kalshi if kalshi is not None else base_metadata()),
        polymarket=market(Venue.POLYMARKET, polymarket if polymarket is not None else base_metadata()),
        same_polarity=same_polarity,
        confidence=confidence,
        method=method,
    )
