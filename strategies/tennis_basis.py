"""Cross-platform tennis basis: venue mid vs. a free public consensus line.

Pre-registered experiment (see ``docs/TENNIS_BASIS.md``)::

    gap_t            = venue_mid_t - outside_p_t          (YES player, both in [0, 1])
    open             when |gap_0| >= gap_threshold (0.03) and the market is admitted
    lean             toward the sharp side: gap > 0 -> sell YES, gap < 0 -> buy YES
    closure_fraction = (gap_0 - gap_T) / gap_0            (gap_T = last observation before start)
    closed_half      = closure_fraction >= closure_target (0.5)
    PASS             when n >= min_sample (30) and closed_half / n >= pass_rate (0.60)

The outside line is de-vigged (multiplicative) per bookmaker; the consensus is
the *sharp* book when present (Pinnacle by default) else the median across at
least ``min_books`` books. Nothing here adjusts for the settlement basis
difference between a bookmaker match-winner bet (void on walkover / retirement)
and a venue contract (walkover -> fair price or 50-50, retirement -> advancing
player); that difference is classified, filtered and reported, not modelled.

All prices are YES probabilities of the venue market's YES player.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from core.portfolio import Portfolio
from core.risk import RiskManager
from core.types import ONE, ZERO, Market, Order, OrderBook, Venue
from strategies.edge import DEFAULT_VENUE_PARAMETERS, CalibratedFairValueStrategy, FairValueEvaluation

TRACK = "tennis_basis"
Q4 = Decimal("0.0001")
_TOKEN = re.compile(r"[a-z0-9]+")


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4, rounding=ROUND_HALF_EVEN) if value is not None else None


# --------------------------------------------------------------------------
# Parameters (pre-registered defaults)
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BasisParameters:
    gap_threshold: Decimal = Decimal("0.03")
    closure_target: Decimal = Decimal("0.5")
    pass_rate: Decimal = Decimal("0.60")
    min_sample: int = 30
    sharp_books: tuple[str, ...] = ("pinnacle",)
    min_books: int = 2
    match_window_hours: Decimal = Decimal("48")
    max_quote_age_seconds: Decimal = Decimal("3600")
    maximum_order_size: Decimal = Decimal("10")
    minimum_edge: Decimal = ZERO

    def __post_init__(self) -> None:
        if not ZERO < self.gap_threshold < ONE:
            raise ValueError("gap_threshold must be within (0, 1)")
        if not ZERO < self.closure_target <= ONE:
            raise ValueError("closure_target must be within (0, 1]")
        if not ZERO < self.pass_rate <= ONE:
            raise ValueError("pass_rate must be within (0, 1]")
        if self.min_sample < 1:
            raise ValueError("min_sample must be at least 1")
        if self.min_books < 1:
            raise ValueError("min_books must be at least 1")
        if self.match_window_hours <= ZERO or self.max_quote_age_seconds <= ZERO:
            raise ValueError("windows must be positive")
        if self.maximum_order_size <= ZERO or self.minimum_edge < ZERO:
            raise ValueError("order size must be positive and minimum_edge non-negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "gap_threshold": self.gap_threshold,
            "closure_target": self.closure_target,
            "pass_rate": self.pass_rate,
            "min_sample": self.min_sample,
            "sharp_books": list(self.sharp_books),
            "min_books": self.min_books,
            "match_window_hours": self.match_window_hours,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "maximum_order_size": self.maximum_order_size,
            "minimum_edge": self.minimum_edge,
        }


# --------------------------------------------------------------------------
# Outside line: de-vig and consensus
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BookQuote:
    """One bookmaker's two-way decimal odds for (player_a, player_b)."""

    book: str
    odds_a: Decimal
    odds_b: Decimal
    last_update: datetime | None = None

    def __post_init__(self) -> None:
        if self.odds_a <= ONE or self.odds_b <= ONE:
            raise ValueError(f"decimal odds must exceed 1.0 ({self.book}: {self.odds_a}, {self.odds_b})")


def devig_two_way(odds_a: Decimal, odds_b: Decimal) -> tuple[Decimal, Decimal]:
    """Multiplicative de-vig: normalise the two implied probabilities to sum to 1."""
    if odds_a <= ONE or odds_b <= ONE:
        raise ValueError("decimal odds must exceed 1.0")
    raw_a, raw_b = ONE / odds_a, ONE / odds_b
    total = raw_a + raw_b
    return raw_a / total, raw_b / total


def overround(odds_a: Decimal, odds_b: Decimal) -> Decimal:
    return ONE / odds_a + ONE / odds_b - ONE


@dataclass(frozen=True, slots=True)
class ConsensusLine:
    probability_a: Decimal | None
    method: str  # sharp_book | median | insufficient_books | no_quotes
    books_used: tuple[str, ...] = ()
    sharp_book: str | None = None
    overround_median: Decimal | None = None
    per_book: dict[str, Decimal] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.probability_a is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "probability_a": _q(self.probability_a),
            "method": self.method,
            "books_used": list(self.books_used),
            "sharp_book": self.sharp_book,
            "overround_median": _q(self.overround_median),
            "per_book": {k: _q(v) for k, v in sorted(self.per_book.items())},
        }


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def consensus_line(
    quotes: list[BookQuote] | tuple[BookQuote, ...],
    *,
    parameters: BasisParameters | None = None,
    as_of: datetime | None = None,
) -> ConsensusLine:
    """Sharp book when quoted (first match in ``sharp_books``), else the median."""
    params = parameters or BasisParameters()
    fresh: list[BookQuote] = []
    for quote in quotes:
        if as_of is not None and quote.last_update is not None:
            age = Decimal(str((as_of - quote.last_update).total_seconds()))
            if age > params.max_quote_age_seconds:
                continue
        fresh.append(quote)
    if not fresh:
        return ConsensusLine(None, "no_quotes")
    per_book = {q.book: devig_two_way(q.odds_a, q.odds_b)[0] for q in fresh}
    rounds = [overround(q.odds_a, q.odds_b) for q in fresh]
    for sharp in params.sharp_books:
        if sharp in per_book:
            return ConsensusLine(
                per_book[sharp], "sharp_book", (sharp,), sharp, _median(rounds), per_book
            )
    if len(per_book) < params.min_books:
        return ConsensusLine(None, "insufficient_books", tuple(sorted(per_book)), None, _median(rounds), per_book)
    return ConsensusLine(
        _median(list(per_book.values())), "median", tuple(sorted(per_book)), None, _median(rounds), per_book
    )


# --------------------------------------------------------------------------
# Player names
# --------------------------------------------------------------------------
def normalize_name(name: str) -> str:
    stripped = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in stripped if not unicodedata.combining(ch))
    return " ".join(_TOKEN.findall(ascii_only.lower()))


def surname(name: str) -> str:
    tokens = normalize_name(name).split()
    return tokens[-1] if tokens else ""


def same_player(a: str, b: str) -> bool:
    """Surname match, plus first-initial agreement when both names carry one."""
    na, nb = normalize_name(a).split(), normalize_name(b).split()
    if not na or not nb or na[-1] != nb[-1]:
        return False
    if len(na) > 1 and len(nb) > 1 and na[0][0] != nb[0][0]:
        return False
    return True


# --------------------------------------------------------------------------
# Settlement basis (mandatory filter)
# --------------------------------------------------------------------------
BOOKMAKER_BASIS = {
    "walkover": "void",
    "retirement": "void_or_book_specific",
    "note": (
        "Bookmaker match-winner odds are quoted on a 'match completed' basis: walkovers void the bet "
        "and most books (Pinnacle among them) void on retirement. Venue contracts pay the advancing "
        "player on retirement and settle walkovers at a fair price (Kalshi) or 50-50 (Polymarket). "
        "The outside line is therefore P(win | completed); the venue mid prices P(advances). The "
        "difference is bounded by the retirement/withdrawal rate and is NOT modelled here."
    ),
}
_RE_RETIREMENT_ADVANCES = re.compile(
    r"(retire|retirement|default|disqualif)[^.]*?(advance|resolve to the player who advances)", re.I
)
_RE_BALL_PLAYED_WINS = re.compile(r"wins[^.]*after a ball has been played", re.I)
_RE_WALKOVER_5050 = re.compile(r"walkover[^.]*?(50-50|50/50|fifty)", re.I)
_RE_WALKOVER_FAIR = re.compile(r"(walkover|does not occur)[^.]*?fair price", re.I)
_RE_WALKOVER_VOID = re.compile(r"walkover[^.]*?(void|refund)", re.I)
_RE_CANCEL_5050 = re.compile(r"(cancel+ed|not played at all)[^.]*?(50-50|50/50)", re.I)
_RE_ITF = re.compile(r"\bITF\b")


@dataclass(frozen=True, slots=True)
class SettlementBasis:
    retirement: str  # advancing_player | unknown
    walkover: str  # fair_price | fifty_fifty | void | unknown
    itf: bool
    readable: bool
    reason: str  # admitted | settlement_basis_unreadable | settlement_basis_itf | settlement_basis_mismatch

    @property
    def admitted(self) -> bool:
        return self.reason == "admitted"

    def as_dict(self) -> dict[str, Any]:
        return {
            "retirement": self.retirement,
            "walkover": self.walkover,
            "itf": self.itf,
            "readable": self.readable,
            "reason": self.reason,
            "bookmaker_basis": {"walkover": BOOKMAKER_BASIS["walkover"], "retirement": BOOKMAKER_BASIS["retirement"]},
        }


def classify_settlement_basis(rules_text: str, *, series: str | None = None) -> SettlementBasis:
    """Read the venue's retirement / walkover clauses. Fail-closed on silence.

    Admitted only when retirement pays the advancing player (explicit, or
    Kalshi's 'wins ... after a ball has been played' which defers to the
    governing body's official result) *and* the walkover rule is one of the
    two known non-void forms. ITF matches are refused: Kalshi and Polymarket
    settle ITF walkovers at 0.50 flat and the free odds feed does not cover them.
    """
    text = rules_text or ""
    itf = bool(_RE_ITF.search(text)) or bool(series and "ITF" in series.upper())
    if _RE_RETIREMENT_ADVANCES.search(text) or _RE_BALL_PLAYED_WINS.search(text):
        retirement = "advancing_player"
    else:
        retirement = "unknown"
    # The walkover sentence decides; the generic cancellation clause is only a fallback.
    if _RE_WALKOVER_VOID.search(text):
        walkover = "void"
    elif _RE_WALKOVER_5050.search(text):
        walkover = "fifty_fifty"
    elif _RE_WALKOVER_FAIR.search(text):
        walkover = "fair_price"
    elif _RE_CANCEL_5050.search(text):
        walkover = "fifty_fifty"
    else:
        walkover = "unknown"
    readable = retirement != "unknown" and walkover != "unknown"
    if itf:
        reason = "settlement_basis_itf"
    elif not readable:
        reason = "settlement_basis_unreadable"
    elif retirement != "advancing_player" or walkover not in ("fair_price", "fifty_fifty"):
        reason = "settlement_basis_mismatch"
    else:
        reason = "admitted"
    return SettlementBasis(retirement, walkover, itf, readable, reason)


# --------------------------------------------------------------------------
# Gap measurement, closure and verdict
# --------------------------------------------------------------------------
def venue_mid(book: OrderBook) -> Decimal | None:
    if book.mid_price is not None:
        return book.mid_price
    return None  # a one-sided book has no mid; the experiment is defined on mids


def gap(mid: Decimal, outside_p: Decimal) -> Decimal:
    return mid - outside_p


def sharp_side(gap_value: Decimal) -> str | None:
    """Which way to lean: venue above the line -> sell YES; below -> buy YES."""
    if gap_value > ZERO:
        return "sell"
    if gap_value < ZERO:
        return "buy"
    return None


def closure_fraction(gap_open: Decimal, gap_final: Decimal) -> Decimal | None:
    """Share of the opening gap that closed toward the outside line.

    1.0 = fully closed, 0.5 = half, negative = widened, > 1.0 = crossed through
    the line (still counted as closed; reported separately as an overshoot).
    """
    if gap_open == ZERO:
        return None
    return (gap_open - gap_final) / gap_open


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[Decimal, Decimal] | None:
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (Decimal(str(round(max(0.0, centre - half), 4))), Decimal(str(round(min(1.0, centre + half), 4))))


@dataclass(frozen=True, slots=True)
class Verdict:
    n: int
    closed_half: int
    rate: Decimal | None
    wilson_95: tuple[Decimal, Decimal] | None
    status: str  # PASS | FAIL | insufficient_sample
    pre_registered: dict[str, Any]
    overshoots: int = 0
    excluded_settlement_mismatch: int = 0
    pending_settlement_check: int = 0
    open_records: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "closed_half": self.closed_half,
            "rate": _q(self.rate),
            "wilson_95": list(self.wilson_95) if self.wilson_95 else None,
            "status": self.status,
            "pre_registered": self.pre_registered,
            "overshoots": self.overshoots,
            "excluded_settlement_mismatch": self.excluded_settlement_mismatch,
            "pending_settlement_check": self.pending_settlement_check,
            "open_records": self.open_records,
        }


def verdict(
    closures: list[Decimal],
    *,
    parameters: BasisParameters | None = None,
    excluded_settlement_mismatch: int = 0,
    pending_settlement_check: int = 0,
    open_records: int = 0,
) -> Verdict:
    """Pre-registered pass rule over the closure fractions of eligible records."""
    params = parameters or BasisParameters()
    n = len(closures)
    closed = sum(1 for c in closures if c >= params.closure_target)
    overshoots = sum(1 for c in closures if c > ONE)
    rate = (Decimal(closed) / Decimal(n)) if n else None
    if n < params.min_sample:
        status = "insufficient_sample"
    elif rate is not None and rate >= params.pass_rate:
        status = "PASS"
    else:
        status = "FAIL"
    return Verdict(
        n=n,
        closed_half=closed,
        rate=rate,
        wilson_95=wilson_interval(closed, n),
        status=status,
        pre_registered={
            "rule": "PASS when n >= min_sample and closed_half / n >= pass_rate",
            "gap_threshold": params.gap_threshold,
            "closure_target": params.closure_target,
            "pass_rate": params.pass_rate,
            "min_sample": params.min_sample,
            "gap_definition": "venue_mid - outside_consensus (YES player), contemporaneous at each observation",
            "final_observation": "last observation strictly before the scheduled start",
            "overshoot": "closure_fraction > 1 counts as closed (reported separately)",
            "exclusions": "settlement-basis unreadable/ITF/mismatch at admission; walkover / cancellation / "
            "retirement / non-binary settlement after the fact",
        },
        overshoots=overshoots,
        excluded_settlement_mismatch=excluded_settlement_mismatch,
        pending_settlement_check=pending_settlement_check,
        open_records=open_records,
    )


# --------------------------------------------------------------------------
# Paper lean: the venue mid is compared to the line; the order is built by the
# primary track's fair-value engine with the outside line as the prior.
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LeanEvaluation:
    reason: str
    gap: Decimal
    side: str | None
    fair_value: FairValueEvaluation | None = None
    orders: tuple[Order, ...] = ()

    @property
    def traded(self) -> bool:
        return bool(self.orders)

    @property
    def cost_adjusted_edge(self) -> Decimal | None:
        return self.fair_value.cost_adjusted_edge if self.fair_value else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "gap": _q(self.gap),
            "side": self.side,
            "touch_price": _q(self.fair_value.touch.price) if self.fair_value and self.fair_value.touch else None,
            "raw_edge": _q(self.fair_value.raw_edge) if self.fair_value else None,
            "cost_adjusted_edge": _q(self.cost_adjusted_edge),
            "quantity": self.fair_value.quantity if self.fair_value else ZERO,
            "orders": len(self.orders),
        }


class TennisBasisLean:
    """Build the paper lean toward the sharp side through the fair-value engine."""

    name = TRACK

    def __init__(
        self,
        *,
        parameters: BasisParameters | None = None,
        portfolio: Portfolio | None = None,
        risk: RiskManager | None = None,
    ) -> None:
        self.parameters = parameters or BasisParameters()
        self.venue_parameters = {
            venue: replace(
                params,
                minimum_edge=self.parameters.minimum_edge,
                maximum_order_size=self.parameters.maximum_order_size,
            )
            for venue, params in DEFAULT_VENUE_PARAMETERS.items()
        }
        self.portfolio = portfolio
        self.risk = risk

    def evaluate(
        self,
        market: Market,
        book: OrderBook,
        outside_p: Decimal,
        *,
        record_id: str,
        consensus: ConsensusLine,
    ) -> LeanEvaluation:
        mid = venue_mid(book)
        if mid is None:
            return LeanEvaluation("no_two_sided_mid", ZERO, None)
        g = gap(mid, outside_p)
        side = sharp_side(g)
        if abs(g) < self.parameters.gap_threshold:
            return LeanEvaluation("gap_below_threshold", g, side)
        engine = CalibratedFairValueStrategy(
            {market.market_id: outside_p},
            venue_parameters=self.venue_parameters,
            portfolio=self.portfolio,
            risk=self.risk,
        )
        fair = engine.evaluate(market, book)
        if fair.side is not None and fair.side.value != side:
            # The engine picks the larger touch edge; with a gap this wide it must agree with the mid sign.
            return LeanEvaluation("touch_disagrees_with_mid", g, side, fair)
        orders = tuple(
            replace(
                order,
                metadata={
                    **order.metadata,
                    "strategy": self.name,
                    "price_signal_status": "free_public_consensus_line",
                    "record_id": record_id,
                    "gap": str(_q(g)),
                    "consensus_method": consensus.method,
                    "consensus_books": ",".join(consensus.books_used),
                    "basis_note": "outside line is P(win | completed); venue prices P(advances)",
                },
            )
            for order in fair.orders
        )
        return LeanEvaluation(fair.reason, g, side, fair, orders)


__all__ = [
    "BOOKMAKER_BASIS",
    "BasisParameters",
    "BookQuote",
    "ConsensusLine",
    "LeanEvaluation",
    "SettlementBasis",
    "TRACK",
    "TennisBasisLean",
    "Verdict",
    "classify_settlement_basis",
    "closure_fraction",
    "consensus_line",
    "devig_two_way",
    "gap",
    "normalize_name",
    "overround",
    "same_player",
    "sharp_side",
    "surname",
    "venue_mid",
    "verdict",
    "wilson_interval",
]
