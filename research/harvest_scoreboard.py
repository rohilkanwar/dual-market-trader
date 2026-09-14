"""Settlement-divergence findings from harvested, already-resolved markets.

A *divergence* is a Kalshi/Polymarket pair about the same release and period
whose resolved outcomes disagree once the Polymarket point bucket is mapped
onto the Kalshi cumulative threshold. Findings are only emitted when there is
data; there are no default counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from research.harvests import HarvestBundle, JsonObject
from settlement.bucket_match import Interval
from settlement.fed_match import FedBucket, FedMatchType, match_fed_buckets

_THRESHOLD = re.compile(
    r"\b(above|over|greater than|more than|at least|below|under|less than|at most)\s+(\d+(?:\.\d+)?)\s*%?",
    re.IGNORECASE,
)
_RANGE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*%?\s*(?:-|–|to|and)\s*(\d+(?:\.\d+)?)\s*%?",
    re.IGNORECASE,
)
_MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
)
_PERIOD = re.compile(
    r"\b(" + "|".join(m[:3] for m in _MONTHS) + r")[a-z]*\.?\s*(20\d{2})\b", re.IGNORECASE
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _text(market: JsonObject) -> str:
    return " ".join(
        str(market.get(key) or "")
        for key in ("title", "question", "subtitle", "event_title", "groupItemTitle")
    )


def _interval(market: JsonObject) -> Interval | None:
    text = _text(market)
    match = _THRESHOLD.search(text)
    if match:
        word, number = match.group(1).lower(), Decimal(match.group(2))
        if word in {"above", "over", "greater than", "more than", "at least"}:
            return Interval(number, None)
        return Interval(None, number)
    match = _RANGE.search(text)
    if match:
        low, high = Decimal(match.group(1)), Decimal(match.group(2))
        if low <= high:
            return Interval(low, high)
    return None


def _point_value(market: JsonObject) -> Decimal | None:
    for key in ("expiration_value", "resolved_value", "settlement_value", "result_value"):
        value = _decimal(market.get(key))
        if value is not None:
            return value
    return None


def _bucket_relation(left: JsonObject, right: JsonObject) -> str:
    left_interval, right_interval = _interval(left), _interval(right)
    if left_interval is not None and _point_value(right) is not None:
        return "kalshi_cumulative:polymarket_point"
    if left_interval is None or right_interval is None:
        return "unparsed"
    if left_interval == right_interval:
        return "identical"
    if left_interval.contains_interval(right_interval):
        return "kalshi_contains_polymarket"
    if right_interval.contains_interval(left_interval):
        return "polymarket_contains_kalshi"
    if left_interval.disjoint(right_interval):
        return "disjoint"
    return "overlapping"


def _period(market: JsonObject) -> str | None:
    match = _PERIOD.search(_text(market))
    if not match:
        return None
    month = match.group(1).lower()[:3]
    index = next(i for i, name in enumerate(_MONTHS) if name.startswith(month))
    return f"{match.group(2)}-{index + 1:02d}"


def _kalshi_outcome(market: JsonObject) -> bool | None:
    result = str(market.get("result") or "").lower()
    if result in {"yes", "no"}:
        return result == "yes"
    return None


def _polymarket_outcome(market: JsonObject) -> bool | None:
    prices = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(prices, str):
        prices = [p.strip().strip('"') for p in prices.strip("[]").split(",")]
    if isinstance(prices, list) and len(prices) >= 2:
        yes, no = _decimal(prices[0]), _decimal(prices[1])
        if yes is not None and no is not None and yes != no:
            return yes > no
    winner = str(market.get("resolved_outcome") or market.get("winner") or "").lower()
    if winner in {"yes", "no"}:
        return winner == "yes"
    return None


def _implied_kalshi_outcome_from_point(interval: Interval, value: Decimal) -> bool:
    if interval.lower is not None and interval.upper is None:
        return value > interval.lower
    if interval.upper is not None and interval.lower is None:
        return value < interval.upper
    return (interval.lower is None or value > interval.lower) and (
        interval.upper is None or value <= interval.upper
    )


@dataclass(frozen=True, slots=True)
class DivergenceRow:
    period: str
    kalshi_ticker: str
    polymarket_id: str
    relation: str
    kalshi_result: bool | None
    polymarket_result: bool | None
    diverged: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "kalshi_ticker": self.kalshi_ticker,
            "polymarket_id": self.polymarket_id,
            "relation": self.relation,
            "kalshi_result": self.kalshi_result,
            "polymarket_result": self.polymarket_result,
            "diverged": self.diverged,
        }


def macro_bucket_divergences(bundle: HarvestBundle) -> list[DivergenceRow]:
    """Pair resolved CPI markets by period and compare implied outcomes."""
    poly_by_period: dict[str, list[JsonObject]] = {}
    for market in bundle.polymarket_markets():
        period = _period(market)
        if period and "cpi" in _text(market).lower():
            poly_by_period.setdefault(period, []).append(market)
    rows: list[DivergenceRow] = []
    for kalshi in bundle.kalshi_markets():
        if "cpi" not in (_text(kalshi) + str(kalshi.get("series_ticker", ""))).lower():
            continue
        period = _period(kalshi)
        kalshi_interval = _interval(kalshi)
        kalshi_result = _kalshi_outcome(kalshi)
        if not period or kalshi_interval is None or kalshi_result is None:
            continue
        for poly in poly_by_period.get(period, []):
            relation = _bucket_relation(kalshi, poly)
            poly_interval = _interval(poly)
            poly_result = _polymarket_outcome(poly)
            diverged: bool | None = None
            if relation == "kalshi_contains_polymarket" and poly_result is True:
                # Polymarket resolved into a bucket inside Kalshi's range: Kalshi must be YES.
                diverged = kalshi_result is not True
            elif relation == "disjoint" and poly_result is True:
                diverged = kalshi_result is not False
            elif relation == "kalshi_cumulative:polymarket_point":
                value = _point_value(poly)
                if value is not None:
                    diverged = _implied_kalshi_outcome_from_point(kalshi_interval, value) != kalshi_result
            elif poly_interval is not None and poly_interval == kalshi_interval and poly_result is not None:
                diverged = poly_result != kalshi_result
            rows.append(
                DivergenceRow(
                    period=period,
                    kalshi_ticker=str(kalshi.get("ticker", "")),
                    polymarket_id=str(poly.get("conditionId") or poly.get("id") or poly.get("slug") or ""),
                    relation=relation,
                    kalshi_result=kalshi_result,
                    polymarket_result=poly_result,
                    diverged=diverged,
                )
            )
    return rows


def fed_exact_divergences(bundle: HarvestBundle) -> tuple[int, int]:
    """(observed divergences, sample size) over resolved Fed bucket pairs."""
    kalshi_fed = [
        m for m in bundle.kalshi_markets() if str(m.get("fed_bucket") or "") in FedBucket.__members__
    ]
    poly_fed = [
        m for m in bundle.polymarket_markets() if str(m.get("fed_bucket") or "") in FedBucket.__members__
    ]
    observed = sample = 0
    for kalshi in kalshi_fed:
        for poly in poly_fed:
            if _period(kalshi) != _period(poly):
                continue
            match = match_fed_buckets(FedBucket(kalshi["fed_bucket"]), FedBucket(poly["fed_bucket"]))
            if match.match_type is not FedMatchType.EXACT:
                continue
            k_res, p_res = _kalshi_outcome(kalshi), _polymarket_outcome(poly)
            if k_res is None or p_res is None:
                continue
            sample += 1
            observed += int(k_res != p_res)
    return observed, sample


def findings_from_harvest(bundle: HarvestBundle) -> dict[str, Any]:
    """Artifact ``findings`` fields. Empty dict when there is nothing to measure."""
    if bundle.empty:
        return {}
    rows = macro_bucket_divergences(bundle)
    scored = [row for row in rows if row.diverged is not None]
    observed = sum(1 for row in scored if row.diverged)
    fed_observed, fed_sample = fed_exact_divergences(bundle)
    findings: dict[str, Any] = {
        "divergence_findings_status": "measured_from_harvest",
        "harvest_dir": bundle.source_dir,
        "harvest_warnings": list(bundle.warnings),
        "macro_admitted_bucket_divergences": {
            "observed": observed,
            "sample_size": len(scored),
            "label": f"macro {observed}/{len(scored)} bucket divergences",
            "note": "Resolved CPI pairs by period; Polymarket buckets mapped onto Kalshi thresholds.",
            "rows": [row.as_dict() for row in rows],
        },
    }
    if fed_sample:
        findings["fed_exact_divergences"] = {
            "observed": fed_observed,
            "sample_size": fed_sample,
            "label": f"Fed EXACT {fed_observed}/{fed_sample} divergences",
            "note": "Only EXACT bucket matches are counted; DOMAIN/UNION matches are excluded.",
        }
    return findings
