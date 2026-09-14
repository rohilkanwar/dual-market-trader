"""Category specialist scoreboard: score traders per category, promote, follow, evaluate.

Paper-only measurement of one hypothesis:

    Large traders who have been *good in one category* (positive rolling ROI
    and positive directional Brier skill, in that category only) go on to
    beat the market mid on their next bets in that same category.

Nothing here touches a venue. The module is pure arithmetic over
:class:`TraderBet` records supplied by ``research/specialist_sources.py``
(committed synthetic fixtures or public read-only Polymarket Data API rows).

Definitions
-----------
All prices are probabilities of the market's first outcome ("YES"). A bet has a
``direction`` (YES or NO), an ``entry_price`` paid for that direction and a
``size`` in contracts.

* ``pnl``      resolved: ``size * (1[won] - entry_price)`` unless the venue
               reported ``realized_pnl`` (then that value is used verbatim)
* ``roi``      ``sum(pnl) / sum(cost)`` over the scoring window, ``cost = size * entry_price``
* directional Brier skill for one bet against a YES benchmark ``m``::

      o = 1 if the market resolved YES else 0
      f = m + shade * (1 - m)    if direction is YES
        = m - shade * m          if direction is NO
      skill = (m - o)^2 - (f - o)^2

  ``m`` is the market mid at entry when known and the trader's YES-equivalent
  entry price otherwise (``mid_proxy_is_entry_price``). Positive skill means
  moving the benchmark toward the trader's direction reduced squared error.

* rolling window: the trader's most recent ``window_bets`` resolved bets **in
  that category**. Other categories never enter the score.
* "large": in-category window notional ``>= min_category_notional``.
* promotion: within one category, traders with enough history and notional are
  ranked by ROI (Brier skill breaks ties); the top ``ceil(top_fraction * n)``
  ranks that also have ``roi > 0`` and ``brier_skill > 0`` are the specialists.
  The taxonomy's catch-all ``other`` is scored for the record but never promoted.
* follow: a specialist's currently open in-category bet is paper-followed at the
  touch; the benchmark recorded is the YES mid at follow time. Markets past
  their end date (stale books) and books wider than ``max_spread`` (no
  meaningful mid) are refused.
* evaluation: per resolved follow, ``excess_vs_mid = 1[won] - p_dir`` where
  ``p_dir`` is the mid expressed for the followed direction. The pre-registered
  test is a one-sided exact binomial sign test on the hit rate (H0: 0.5) with
  ``mean_excess_vs_mid > 0``, ``n >= preregistered_n`` pooled across categories.
  Fewer resolved follows than ``preregistered_n`` is reported as
  ``underpowered`` — never as a pass or a fail.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from core.types import ONE, ZERO, Outcome, Venue

Q4 = Decimal("0.0001")
UNCLASSIFIED_CATEGORY = "other"
LITERATURE_NOTE = (
    "Persistence of forecaster skill by domain is documented for human "
    "forecasters (Mellers et al. 2015 'superforecasters'; Tetlock & Gardner 2015) "
    "and for prediction-market traders in aggregate; no published estimate covers "
    "Polymarket wallets by category. Reported for context only, not validated here."
)


def _now() -> datetime:
    return datetime.now(UTC)


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4, rounding=ROUND_HALF_UP) if value is not None else None


# --------------------------------------------------------------------------
# Parameters (pre-registered)
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SpecialistParameters:
    window_bets: int = 20
    min_resolved_bets: int = 10
    min_category_notional: Decimal = Decimal("1000")
    top_fraction: Decimal = Decimal("0.1")
    forecast_shade: Decimal = Decimal("0.5")
    follow_quantity: Decimal = Decimal("10")
    max_spread: Decimal = Decimal("0.10")
    preregistered_n: int = 30
    alpha: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        if self.window_bets <= 0:
            raise ValueError("window_bets must be positive")
        if self.min_resolved_bets <= 0 or self.min_resolved_bets > self.window_bets:
            raise ValueError("min_resolved_bets must be in [1, window_bets]")
        if self.min_category_notional < ZERO:
            raise ValueError("min_category_notional must not be negative")
        if not ZERO < self.top_fraction <= ONE:
            raise ValueError("top_fraction must be in (0, 1]")
        if not ZERO < self.forecast_shade <= ONE:
            raise ValueError("forecast_shade must be in (0, 1]")
        if self.follow_quantity <= ZERO:
            raise ValueError("follow_quantity must be positive")
        if not ZERO < self.max_spread <= ONE:
            raise ValueError("max_spread must be in (0, 1]")
        if self.preregistered_n <= 0:
            raise ValueError("preregistered_n must be positive")
        if not ZERO < self.alpha < ONE:
            raise ValueError("alpha must be in (0, 1)")

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_bets": self.window_bets,
            "min_resolved_bets": self.min_resolved_bets,
            "min_category_notional": self.min_category_notional,
            "top_fraction": self.top_fraction,
            "forecast_shade": self.forecast_shade,
            "follow_quantity": self.follow_quantity,
            "max_spread": self.max_spread,
            "preregistered_n": self.preregistered_n,
            "alpha": self.alpha,
        }


def preregistration(params: SpecialistParameters) -> dict[str, Any]:
    """The test as registered before any network follow resolved."""
    return {
        "registered_at": "2026-09-14",
        "hypothesis": (
            "Top-decile in-category specialists (positive rolling ROI and positive directional "
            "Brier skill, scored on that category only) beat the market mid on their next "
            "in-category bets."
        ),
        "unit_of_analysis": "one paper-followed bet; pooled across categories",
        "primary_metric": "mean_excess_vs_mid (direction-signed resolution minus mid at follow time, per contract)",
        "secondary_metric": "mean directional Brier skill of the followed direction vs the mid at follow time",
        "test": "one-sided exact binomial sign test on hit rate (H0: 0.5) AND mean_excess_vs_mid > 0",
        "n": params.preregistered_n,
        "alpha": params.alpha,
        "wins_required_at_n": wins_required(params.preregistered_n, params.alpha),
        "underpowered_rule": (
            f"fewer than {params.preregistered_n} resolved follows is reported as 'underpowered'; "
            "never as pass or fail"
        ),
        "per_category_results": "reported descriptively; not individually powered",
        "parameters": params.as_dict(),
        "literature": LITERATURE_NOTE,
    }


# --------------------------------------------------------------------------
# Bets
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TraderBet:
    bet_id: str
    trader: str
    venue: Venue
    market_id: str
    category: str
    direction: Outcome
    entry_price: Decimal
    size: Decimal
    placed_at: datetime
    title: str = ""
    resolved: bool = False
    outcome: Outcome | None = None
    resolved_at: datetime | None = None
    realized_pnl: Decimal | None = None
    market_mid_at_entry: Decimal | None = None
    yes_token_id: str | None = None
    no_token_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not ZERO <= self.entry_price <= ONE:
            raise ValueError(f"entry_price {self.entry_price} must be within [0, 1]")
        if self.size <= ZERO:
            raise ValueError(f"size {self.size} must be positive")
        if self.market_mid_at_entry is not None and not ZERO <= self.market_mid_at_entry <= ONE:
            raise ValueError("market_mid_at_entry must be within [0, 1]")
        if self.resolved and self.outcome is None:
            raise ValueError(f"resolved bet {self.bet_id} needs an outcome")
        if not self.resolved and self.outcome is not None:
            raise ValueError(f"open bet {self.bet_id} must not carry an outcome")
        if self.placed_at.tzinfo is None:
            raise ValueError("placed_at must be timezone-aware")
        if not self.category:
            raise ValueError("category is required")

    @property
    def cost(self) -> Decimal:
        return self.size * self.entry_price

    @property
    def yes_equivalent_entry(self) -> Decimal:
        return self.entry_price if self.direction is Outcome.YES else ONE - self.entry_price

    @property
    def won(self) -> bool | None:
        if not self.resolved:
            return None
        return self.outcome is self.direction

    @property
    def pnl(self) -> Decimal | None:
        if not self.resolved:
            return None
        if self.realized_pnl is not None:
            return self.realized_pnl
        return self.size * ((ONE if self.won else ZERO) - self.entry_price)

    @property
    def sort_time(self) -> datetime:
        return self.resolved_at or self.placed_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "bet_id": self.bet_id,
            "trader": self.trader,
            "venue": self.venue.value,
            "market_id": self.market_id,
            "category": self.category,
            "direction": self.direction.value,
            "entry_price": self.entry_price,
            "size": self.size,
            "cost": _q(self.cost),
            "placed_at": self.placed_at.isoformat(),
            "title": self.title,
            "resolved": self.resolved,
            "outcome": self.outcome.value if self.outcome else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "won": self.won,
            "pnl": _q(self.pnl),
            "market_mid_at_entry": self.market_mid_at_entry,
        }


def directional_forecast(benchmark: Decimal, direction: Outcome, shade: Decimal) -> Decimal:
    """Benchmark tilted toward the bet direction by ``shade`` of the remaining distance."""
    if direction is Outcome.YES:
        return benchmark + shade * (ONE - benchmark)
    return benchmark - shade * benchmark


def brier_skill(benchmark: Decimal, direction: Outcome, outcome: Outcome, shade: Decimal) -> Decimal:
    """``(m - o)^2 - (f - o)^2``: how much the tilt toward ``direction`` beat ``benchmark``."""
    o = ONE if outcome is Outcome.YES else ZERO
    f = directional_forecast(benchmark, direction, shade)
    return (benchmark - o) ** 2 - (f - o) ** 2


def bet_brier_skill(bet: TraderBet, shade: Decimal) -> tuple[Decimal, bool] | None:
    """Skill for a resolved bet plus whether the entry price stood in for the mid."""
    if not bet.resolved or bet.outcome is None:
        return None
    proxy = bet.market_mid_at_entry is None
    benchmark = bet.yes_equivalent_entry if proxy else bet.market_mid_at_entry
    assert benchmark is not None
    return brier_skill(benchmark, bet.direction, bet.outcome, shade), proxy


# --------------------------------------------------------------------------
# Per (trader, category) scoring
# --------------------------------------------------------------------------
@dataclass(slots=True)
class CategoryScore:
    trader: str
    category: str
    resolved_bets: int = 0
    total_resolved: int = 0
    open_bets: int = 0
    wins: int = 0
    notional: Decimal = ZERO
    pnl: Decimal = ZERO
    roi: Decimal | None = None
    brier_skill: Decimal | None = None
    hit_rate: Decimal | None = None
    mid_proxy_bets: int = 0
    specialization: Decimal | None = None
    trader_notional_all_categories: Decimal = ZERO
    reasons: list[str] = field(default_factory=list)
    rank: int | None = None
    scored_in_category: int = 0
    promoted: bool = False
    window_from: str | None = None
    window_to: str | None = None

    @property
    def eligible(self) -> bool:
        return not self.reasons

    def as_dict(self) -> dict[str, Any]:
        return {
            "trader": self.trader,
            "category": self.category,
            "resolved_bets": self.resolved_bets,
            "total_resolved": self.total_resolved,
            "open_bets": self.open_bets,
            "wins": self.wins,
            "notional": _q(self.notional),
            "pnl": _q(self.pnl),
            "roi": _q(self.roi),
            "brier_skill": _q(self.brier_skill),
            "hit_rate": _q(self.hit_rate),
            "mid_proxy_bets": self.mid_proxy_bets,
            "specialization": _q(self.specialization),
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "rank": self.rank,
            "scored_in_category": self.scored_in_category,
            "promoted": self.promoted,
            "window_from": self.window_from,
            "window_to": self.window_to,
        }


def score_traders(bets: list[TraderBet], params: SpecialistParameters) -> list[CategoryScore]:
    """One :class:`CategoryScore` per (trader, category) seen in ``bets``.

    Only resolved bets inside the rolling window feed ROI / Brier. A trader's
    bets in other categories only enter through ``specialization`` (share of
    the trader's window notional that sits in this category).
    """
    by_key: dict[tuple[str, str], list[TraderBet]] = {}
    for bet in bets:
        by_key.setdefault((bet.trader, bet.category), []).append(bet)

    scores: list[CategoryScore] = []
    windows: dict[tuple[str, str], list[TraderBet]] = {}
    for (trader, category), rows in sorted(by_key.items()):
        resolved = sorted((b for b in rows if b.resolved), key=lambda b: b.sort_time, reverse=True)
        window = resolved[: params.window_bets]
        windows[(trader, category)] = window
        score = CategoryScore(
            trader=trader,
            category=category,
            resolved_bets=len(window),
            total_resolved=len(resolved),
            open_bets=sum(1 for b in rows if not b.resolved),
        )
        if window:
            score.window_from = min(b.sort_time for b in window).isoformat()
            score.window_to = max(b.sort_time for b in window).isoformat()
            skills: list[Decimal] = []
            for bet in window:
                score.notional += bet.cost
                score.pnl += bet.pnl or ZERO
                score.wins += int(bool(bet.won))
                result = bet_brier_skill(bet, params.forecast_shade)
                if result is not None:
                    skill, proxy = result
                    skills.append(skill)
                    score.mid_proxy_bets += int(proxy)
            if score.notional > ZERO:
                score.roi = score.pnl / score.notional
            score.brier_skill = sum(skills, ZERO) / len(skills) if skills else None
            score.hit_rate = Decimal(score.wins) / Decimal(len(window))
        scores.append(score)

    trader_notional: dict[str, Decimal] = {}
    for (trader, _), window in windows.items():
        trader_notional[trader] = trader_notional.get(trader, ZERO) + sum((b.cost for b in window), ZERO)
    for score in scores:
        total = trader_notional.get(score.trader, ZERO)
        score.trader_notional_all_categories = total
        score.specialization = (score.notional / total) if total > ZERO else None
        if score.resolved_bets < params.min_resolved_bets:
            score.reasons.append("insufficient_history")
        if score.notional < params.min_category_notional:
            score.reasons.append("below_notional")
        if score.roi is not None and score.roi <= ZERO:
            score.reasons.append("negative_roi")
        if score.brier_skill is not None and score.brier_skill <= ZERO:
            score.reasons.append("negative_brier")
    return scores


def promote(scores: list[CategoryScore], params: SpecialistParameters) -> list[CategoryScore]:
    """Rank scored traders inside each category and mark the top decile that is also positive.

    "Scored" means enough history and enough notional; ROI/Brier sign does not
    change the decile boundary (a category with ten scored traders promotes at
    most one), it only decides whether the top-ranked trader is promoted.
    Mutates and returns ``scores``.
    """
    by_category: dict[str, list[CategoryScore]] = {}
    for score in scores:
        score.rank = None
        score.promoted = False
        if score.category == UNCLASSIFIED_CATEGORY and "unclassified_category" not in score.reasons:
            # "other" is the taxonomy's catch-all, not a category anyone can specialise in.
            score.reasons.append("unclassified_category")
        if "insufficient_history" in score.reasons or "below_notional" in score.reasons:
            continue
        if score.category == UNCLASSIFIED_CATEGORY:
            continue
        by_category.setdefault(score.category, []).append(score)
    for _, rows in by_category.items():
        rows.sort(
            key=lambda s: (s.roi if s.roi is not None else Decimal("-1"), s.brier_skill if s.brier_skill is not None else Decimal("-1"), s.trader),
            reverse=True,
        )
        cutoff = max(1, math.ceil(len(rows) * float(params.top_fraction)))
        for index, score in enumerate(rows, start=1):
            score.rank = index
            score.scored_in_category = len(rows)
            if index > cutoff:
                score.reasons.append("below_top_decile")
            score.promoted = score.eligible
    return scores


def specialists(scores: list[CategoryScore]) -> list[CategoryScore]:
    return [s for s in scores if s.promoted]


# --------------------------------------------------------------------------
# Following and evaluating
# --------------------------------------------------------------------------
@dataclass(slots=True)
class FollowedBet:
    follow_id: str
    trader: str
    category: str
    venue: Venue
    market_id: str
    direction: Outcome
    title: str
    followed_at: datetime
    mid_at_follow: Decimal
    quantity: Decimal
    fill_price: Decimal | None = None
    fee: Decimal = ZERO
    source_bet_id: str | None = None
    trader_entry_price: Decimal | None = None
    resolved: bool = False
    outcome: Outcome | None = None
    resolved_at: datetime | None = None
    mode: str = ""

    @property
    def direction_mid(self) -> Decimal:
        """The mid expressed for the followed direction (what a bettor at mid would pay)."""
        return self.mid_at_follow if self.direction is Outcome.YES else ONE - self.mid_at_follow

    @property
    def won(self) -> bool | None:
        if not self.resolved:
            return None
        return self.outcome is self.direction

    @property
    def excess_vs_mid(self) -> Decimal | None:
        """Per-contract return of taking the specialist's side *at the mid*."""
        if not self.resolved:
            return None
        return (ONE if self.won else ZERO) - self.direction_mid

    def brier_skill_vs_mid(self, shade: Decimal) -> Decimal | None:
        if not self.resolved or self.outcome is None:
            return None
        return brier_skill(self.mid_at_follow, self.direction, self.outcome, shade)

    def as_dict(self) -> dict[str, Any]:
        return {
            "follow_id": self.follow_id,
            "trader": self.trader,
            "category": self.category,
            "venue": self.venue.value,
            "market_id": self.market_id,
            "direction": self.direction.value,
            "title": self.title,
            "followed_at": self.followed_at.isoformat(),
            "mid_at_follow": self.mid_at_follow,
            "direction_mid": _q(self.direction_mid),
            "quantity": self.quantity,
            "fill_price": self.fill_price,
            "fee": self.fee,
            "source_bet_id": self.source_bet_id,
            "trader_entry_price": self.trader_entry_price,
            "resolved": self.resolved,
            "outcome": self.outcome.value if self.outcome else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "won": self.won,
            "excess_vs_mid": _q(self.excess_vs_mid),
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> FollowedBet:
        return cls(
            follow_id=str(item["follow_id"]),
            trader=str(item["trader"]),
            category=str(item["category"]),
            venue=Venue(item["venue"]),
            market_id=str(item["market_id"]),
            direction=Outcome(item["direction"]),
            title=str(item.get("title") or ""),
            followed_at=datetime.fromisoformat(item["followed_at"]),
            mid_at_follow=Decimal(str(item["mid_at_follow"])),
            quantity=Decimal(str(item["quantity"])),
            fill_price=Decimal(str(item["fill_price"])) if item.get("fill_price") is not None else None,
            fee=Decimal(str(item.get("fee") or "0")),
            source_bet_id=item.get("source_bet_id"),
            trader_entry_price=(
                Decimal(str(item["trader_entry_price"])) if item.get("trader_entry_price") is not None else None
            ),
            resolved=bool(item.get("resolved", False)),
            outcome=Outcome(item["outcome"]) if item.get("outcome") else None,
            resolved_at=datetime.fromisoformat(item["resolved_at"]) if item.get("resolved_at") else None,
            mode=str(item.get("mode") or ""),
        )


def binomial_tail(wins: int, n: int, p: float = 0.5) -> Decimal:
    """Exact one-sided ``P[X >= wins]`` for ``X ~ Binomial(n, p)``."""
    if n <= 0:
        return ONE
    wins = max(0, min(wins, n))
    total = sum(math.comb(n, k) * (p**k) * ((1 - p) ** (n - k)) for k in range(wins, n + 1))
    return Decimal(str(min(1.0, max(0.0, total))))


def wins_required(n: int, alpha: Decimal) -> int | None:
    """Smallest win count whose one-sided sign-test p-value is below ``alpha``."""
    for k in range(0, n + 1):
        if binomial_tail(k, n) < alpha:
            return k
    return None


STATUS_NO_FOLLOWS = "no_follows"
STATUS_PENDING = "pending_resolutions"
STATUS_UNDERPOWERED = "underpowered"
STATUS_PASS = "pass"
STATUS_FAIL = "fail"


@dataclass(slots=True)
class FollowEvaluation:
    category: str
    n_followed: int = 0
    n_resolved: int = 0
    n_pending: int = 0
    wins: int = 0
    hit_rate: Decimal | None = None
    mean_excess_vs_mid: Decimal | None = None
    pnl_vs_mid_per_contract_sum: Decimal = ZERO
    mean_brier_skill_vs_mid: Decimal | None = None
    sign_test_p: Decimal | None = None
    preregistered_n: int = 0
    status: str = STATUS_NO_FOLLOWS
    powered: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "n_followed": self.n_followed,
            "n_resolved": self.n_resolved,
            "n_pending": self.n_pending,
            "wins": self.wins,
            "hit_rate": _q(self.hit_rate),
            "mean_excess_vs_mid": _q(self.mean_excess_vs_mid),
            "sum_excess_vs_mid": _q(self.pnl_vs_mid_per_contract_sum),
            "mean_brier_skill_vs_mid": _q(self.mean_brier_skill_vs_mid),
            "sign_test_p": _q(self.sign_test_p),
            "preregistered_n": self.preregistered_n,
            "status": self.status,
            "powered": self.powered,
            "note": self.note,
        }


def evaluate_follows(
    followed: list[FollowedBet], params: SpecialistParameters, *, category: str = "all"
) -> FollowEvaluation:
    """Pre-registered readout for the pooled sample (``category="all"``) or one category."""
    rows = followed if category == "all" else [f for f in followed if f.category == category]
    resolved = [f for f in rows if f.resolved]
    ev = FollowEvaluation(category=category, n_followed=len(rows), preregistered_n=params.preregistered_n)
    ev.n_resolved = len(resolved)
    ev.n_pending = len(rows) - len(resolved)
    if not rows:
        ev.status = STATUS_NO_FOLLOWS
        ev.note = "no specialist bet was paper-followed yet"
        return ev
    if not resolved:
        ev.status = STATUS_PENDING
        ev.note = f"{ev.n_pending} follow(s) await resolution; nothing can be concluded"
        return ev
    excess = [f.excess_vs_mid for f in resolved if f.excess_vs_mid is not None]
    skills = [s for f in resolved if (s := f.brier_skill_vs_mid(params.forecast_shade)) is not None]
    ev.wins = sum(1 for f in resolved if f.won)
    ev.hit_rate = Decimal(ev.wins) / Decimal(len(resolved))
    ev.mean_excess_vs_mid = sum(excess, ZERO) / len(excess) if excess else None
    ev.pnl_vs_mid_per_contract_sum = sum(excess, ZERO)
    ev.mean_brier_skill_vs_mid = sum(skills, ZERO) / len(skills) if skills else None
    ev.sign_test_p = binomial_tail(ev.wins, len(resolved))
    ev.powered = category == "all" and len(resolved) >= params.preregistered_n
    if not ev.powered:
        ev.status = STATUS_UNDERPOWERED
        needed = params.preregistered_n - len(resolved)
        ev.note = (
            f"{len(resolved)} resolved follow(s) < pre-registered N={params.preregistered_n}; "
            f"{max(needed, 0)} more needed before pass/fail may be declared"
            if category == "all"
            else "per-category readout is descriptive; only the pooled sample is powered"
        )
        return ev
    beats_mid = ev.mean_excess_vs_mid is not None and ev.mean_excess_vs_mid > ZERO
    significant = ev.sign_test_p is not None and ev.sign_test_p < params.alpha
    ev.status = STATUS_PASS if (beats_mid and significant) else STATUS_FAIL
    ev.note = (
        f"n={len(resolved)} >= {params.preregistered_n}; mean_excess_vs_mid "
        f"{'>' if beats_mid else '<='} 0 and sign-test p={ev.sign_test_p} "
        f"{'<' if significant else '>='} alpha={params.alpha}"
    )
    return ev


def categories_of(followed: list[FollowedBet]) -> list[str]:
    return sorted({f.category for f in followed})
