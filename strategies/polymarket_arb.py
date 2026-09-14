"""Intra-venue Polymarket arbitrage detectors (paper measurement only).

Three structural relations are checked against *depth-aware* book walks, with
the published taker fee and a per-leg slippage buffer deducted before anything
is admitted:

``merge`` / ``split`` (binary rebalancing, arXiv:2508.03474)
    Buy one YES and one NO for less than 1 USDC and merge them through the
    CTF, or split 1 USDC and sell both legs for more than 1. Both are
    executable immediately. On the live CLOB the YES and NO books are exact
    mirrors, so top-of-book ``YES_ask + NO_ask == 1 + spread`` and this never
    triggers; the detector still verifies the mirror on every market.

``negrisk_convert`` (arXiv:2608.00666)
    Buy one NO on every visible leg of a NegRisk event for less than ``K - 1``
    and convert the NO set to ``K - 1`` USDC through the NegRiskAdapter. This
    is the *executable* direction: the converter is one-way (NO -> YES +
    collateral), so nothing needs to be held to resolution. Hidden placeholder
    outcomes (``augmented`` events) can only help this side.

``long_all_yes`` (sum-to-one combinatorial constraint)
    Buy one YES on every leg for less than 1. There is no YES -> collateral
    converter, so the payoff arrives at resolution: capital is locked until
    ``end_date``. Refused when the group is not known to be exclusive or when
    hidden placeholder outcomes exist (a placeholder win zeroes every visible
    YES).

Nothing here touches a venue; the output is a plan of limit orders that the
paper fill simulator executes against the same frozen books.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from core.types import ONE, ZERO, Market, MarketGroup, Order, OrderBook, Outcome, PriceLevel, Side
from venues.polymarket.fees import polymarket_taker_fee

BPS = Decimal("10000")
Q5 = Decimal("0.00001")
ADMITTED = "admitted"


class ArbKind(StrEnum):
    MERGE = "merge"
    SPLIT = "split"
    NEGRISK_CONVERT = "negrisk_convert"
    LONG_ALL_YES = "long_all_yes"


@dataclass(frozen=True, slots=True)
class ArbParameters:
    slippage_ticks: Decimal = Decimal("1")
    maximum_sets: Decimal = Decimal("100")
    maximum_capital: Decimal = Decimal("250")
    minimum_net_edge_per_set: Decimal = Decimal("0.001")
    converter_fee_bps: Decimal = ZERO
    allow_unverified_exclusivity: bool = False
    allow_hidden_outcome_long_yes: bool = False

    def __post_init__(self) -> None:
        if self.slippage_ticks < ZERO or self.minimum_net_edge_per_set < ZERO or self.converter_fee_bps < ZERO:
            raise ValueError("buffers must not be negative")
        if self.maximum_sets <= ZERO or self.maximum_capital <= ZERO:
            raise ValueError("caps must be positive")


def _dec(value: object, default: Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return default
    return parsed if parsed > ZERO else default


def fee_rate_of(market: Market) -> Decimal:
    return _dec(market.metadata.get("taker_fee_rate", "0"), ZERO)


def tick_of(market: Market) -> Decimal:
    return _dec(market.metadata.get("tick_size"), Decimal("0.001"))


def min_order_size_of(market: Market) -> Decimal:
    return _dec(market.metadata.get("min_order_size"), Decimal("5"))


@dataclass(frozen=True, slots=True)
class ArbLeg:
    market: Market
    outcome: Outcome
    side: Side
    ladder: tuple[PriceLevel, ...]
    fee_rate: Decimal
    tick: Decimal
    min_order_size: Decimal

    @property
    def top(self) -> PriceLevel | None:
        return self.ladder[0] if self.ladder else None

    def unit_flow(self, price: Decimal, slippage_ticks: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        """(signed cash before costs, fee, slippage) for one contract at ``price``."""
        fee = self.fee_rate * price * (ONE - price)
        slippage = slippage_ticks * self.tick
        cash = -price if self.side is Side.BUY else price
        return cash, fee, slippage

    def capital(self, price: Decimal) -> Decimal:
        return price if self.side is Side.BUY else ZERO


@dataclass(frozen=True, slots=True)
class LegPlan:
    leg: ArbLeg
    quantity: Decimal
    limit_price: Decimal
    average_price: Decimal
    cash: Decimal
    fees: Decimal
    slippage: Decimal

    @property
    def order(self) -> Order:
        return Order(
            venue=self.leg.market.venue,
            market_id=self.leg.market.market_id,
            side=self.leg.side,
            outcome=self.leg.outcome,
            quantity=self.quantity,
            price=self.limit_price,
            metadata={"strategy": "polymarket_arb"},
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "market": self.leg.market.market_id,
            "title": self.leg.market.title,
            "outcome": self.leg.outcome.value,
            "side": self.leg.side.value,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "average_price": self.average_price.quantize(Q5),
            "fees": self.fees.quantize(Q5),
            "slippage": self.slippage.quantize(Q5),
            "fee_rate": self.leg.fee_rate,
        }


@dataclass(frozen=True, slots=True)
class ArbEvaluation:
    kind: ArbKind
    group_id: str
    title: str
    reason: str
    legs: tuple[LegPlan, ...] = ()
    quantity: Decimal = ZERO
    payoff_per_set: Decimal = ZERO
    top_of_book_sum: Decimal | None = None
    gross_edge_per_set: Decimal | None = None
    net_edge_per_set: Decimal | None = None
    net_profit: Decimal = ZERO
    total_fees: Decimal = ZERO
    total_slippage: Decimal = ZERO
    capital_required: Decimal = ZERO
    executable_now: bool = False
    lockup: str = "none"
    lockup_until: str | None = None
    hidden_outcome_risk: bool = False
    mirror_consistent: bool | None = None
    leg_count: int = 0

    @property
    def traded(self) -> bool:
        return self.reason == ADMITTED and self.quantity > ZERO

    @property
    def orders(self) -> tuple[Order, ...]:
        return tuple(plan.order for plan in self.legs) if self.traded else ()

    @property
    def edge_bps(self) -> int | None:
        edge = self.net_edge_per_set if self.net_edge_per_set is not None else self.gross_edge_per_set
        return int((edge * BPS).to_integral_value()) if edge is not None else None

    @property
    def return_on_capital_bps(self) -> int | None:
        if self.capital_required <= ZERO:
            return None
        return int((self.net_profit / self.capital_required * BPS).to_integral_value())

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "group": self.group_id,
            "title": self.title,
            "reason": self.reason,
            "admitted": self.traded,
            "legs": [plan.as_dict() for plan in self.legs],
            "leg_count": self.leg_count,
            "sets": self.quantity,
            "payoff_per_set": self.payoff_per_set,
            "top_of_book_sum": self.top_of_book_sum,
            "gross_edge_per_set": self.gross_edge_per_set,
            "net_edge_per_set": self.net_edge_per_set.quantize(Q5) if self.net_edge_per_set is not None else None,
            "edge_bps": self.edge_bps,
            "return_on_capital_bps": self.return_on_capital_bps,
            "net_profit": self.net_profit.quantize(Q5),
            "total_fees": self.total_fees.quantize(Q5),
            "total_slippage": self.total_slippage.quantize(Q5),
            "capital_required": self.capital_required.quantize(Q5),
            "executable_now": self.executable_now,
            "lockup": self.lockup,
            "lockup_until": self.lockup_until,
            "hidden_outcome_risk": self.hidden_outcome_risk,
            "mirror_consistent": self.mirror_consistent,
        }


# --------------------------------------------------------------------------
# Book helpers
# --------------------------------------------------------------------------
def books_are_mirrors(yes_book: OrderBook, no_book: OrderBook) -> bool:
    """True when the NO book is exactly the complement of the YES book (live CLOB shape)."""
    mirrored = yes_book.for_outcome(Outcome.NO)
    as_pairs = lambda levels: tuple((lvl.price, lvl.size) for lvl in levels)  # noqa: E731
    return as_pairs(mirrored.bids) == as_pairs(no_book.bids) and as_pairs(mirrored.asks) == as_pairs(no_book.asks)


def _leg(market: Market, outcome: Outcome, side: Side, book: OrderBook) -> ArbLeg:
    ladder = book.asks if side is Side.BUY else book.bids
    return ArbLeg(
        market=market,
        outcome=outcome,
        side=side,
        ladder=ladder,
        fee_rate=fee_rate_of(market),
        tick=tick_of(market),
        min_order_size=min_order_size_of(market),
    )


# --------------------------------------------------------------------------
# Depth-aware sizing
# --------------------------------------------------------------------------
@dataclass(slots=True)
class _Accumulator:
    quantity: Decimal = ZERO
    cash: Decimal = ZERO
    fees: Decimal = ZERO
    slippage: Decimal = ZERO
    worst: Decimal | None = None


def plan_sets(
    legs: tuple[ArbLeg, ...],
    *,
    payoff_per_set: Decimal,
    fixed_capital_per_set: Decimal,
    params: ArbParameters,
) -> tuple[tuple[LegPlan, ...], Decimal, str]:
    """Walk every leg's ladder simultaneously and stop when the marginal set stops paying.

    Returns the per-leg plans, the number of sets, and the reason sizing
    stopped when it never started (``empty_book``, ``no_edge``,
    ``fees_exceed_edge``, ``slippage_exceeds_edge``, ``capital_cap``).
    """
    n = len(legs)
    index = [0] * n
    used = [ZERO] * n
    acc = [_Accumulator() for _ in range(n)]
    sets = ZERO
    capital = ZERO
    stop_reason = "no_edge"
    first = True
    while sets < params.maximum_sets:
        levels: list[PriceLevel] = []
        for i, leg in enumerate(legs):
            if index[i] >= len(leg.ladder):
                stop_reason = "empty_book" if first else "depth_exhausted"
                levels = []
                break
            levels.append(leg.ladder[index[i]])
        if not levels:
            break
        marginal = payoff_per_set
        fee_total = ZERO
        slip_total = ZERO
        capital_per_set = fixed_capital_per_set
        for leg, level in zip(legs, levels, strict=True):
            cash, fee, slip = leg.unit_flow(level.price, params.slippage_ticks)
            marginal += cash - fee - slip
            fee_total += fee
            slip_total += slip
            capital_per_set += leg.capital(level.price)
        if marginal <= params.minimum_net_edge_per_set:
            if first:
                gross = marginal + fee_total + slip_total
                if gross <= ZERO:
                    stop_reason = "no_edge"
                elif gross - fee_total <= params.minimum_net_edge_per_set:
                    stop_reason = "fees_exceed_edge"
                else:
                    stop_reason = "slippage_exceeds_edge"
            else:
                stop_reason = "marginal_edge_exhausted"
            break
        step = min(
            [level.size - used[i] for i, level in enumerate(levels)] + [params.maximum_sets - sets]
        )
        if capital_per_set > ZERO:
            headroom = ((params.maximum_capital - capital) / capital_per_set).to_integral_value(rounding=ROUND_DOWN)
            if headroom < step:
                step = headroom
                stop_reason = "capital_cap"
        step = step.to_integral_value(rounding=ROUND_DOWN)
        if step <= ZERO:
            if first:
                stop_reason = "capital_cap" if capital_per_set > ZERO and params.maximum_capital < capital_per_set else "empty_book"
            break
        first = False
        for i, (leg, level) in enumerate(zip(legs, levels, strict=True)):
            cash, fee, slip = leg.unit_flow(level.price, params.slippage_ticks)
            acc[i].quantity += step
            acc[i].cash += cash * step
            acc[i].fees += fee * step
            acc[i].slippage += slip * step
            acc[i].worst = level.price if acc[i].worst is None else (
                max(acc[i].worst, level.price) if leg.side is Side.BUY else min(acc[i].worst, level.price)
            )
            used[i] += step
            if used[i] >= level.size:
                index[i] += 1
                used[i] = ZERO
        sets += step
        capital += capital_per_set * step
        if stop_reason == "capital_cap":
            break
    if sets <= ZERO:
        return (), ZERO, stop_reason
    plans = tuple(
        LegPlan(
            leg=leg,
            quantity=a.quantity,
            limit_price=a.worst if a.worst is not None else ZERO,
            average_price=abs(a.cash) / a.quantity,
            cash=a.cash,
            fees=a.fees,
            slippage=a.slippage,
        )
        for leg, a in zip(legs, acc, strict=True)
    )
    return plans, sets, ADMITTED


def _evaluate(
    kind: ArbKind,
    group_id: str,
    title: str,
    legs: tuple[ArbLeg, ...],
    *,
    payoff_per_set: Decimal,
    fixed_capital_per_set: Decimal,
    params: ArbParameters,
    executable_now: bool,
    lockup: str,
    lockup_until: str | None,
    hidden_outcome_risk: bool,
    mirror_consistent: bool | None = None,
    refuse: str | None = None,
) -> ArbEvaluation:
    tops = [leg.top for leg in legs]
    top_sum = sum((t.price for t in tops if t is not None), ZERO) if all(t is not None for t in tops) else None
    if top_sum is not None:
        gross = (payoff_per_set + sum((leg.unit_flow(t.price, ZERO)[0] for leg, t in zip(legs, tops, strict=True) if t is not None), ZERO))
    else:
        gross = None
    base = dict(
        kind=kind,
        group_id=group_id,
        title=title,
        payoff_per_set=payoff_per_set,
        top_of_book_sum=top_sum,
        gross_edge_per_set=gross,
        executable_now=executable_now,
        lockup=lockup,
        lockup_until=lockup_until,
        hidden_outcome_risk=hidden_outcome_risk,
        mirror_consistent=mirror_consistent,
        leg_count=len(legs),
    )
    if refuse is not None:
        return ArbEvaluation(reason=refuse, **base)
    if top_sum is None:
        return ArbEvaluation(reason="empty_book", **base)
    plans, sets, reason = plan_sets(
        legs, payoff_per_set=payoff_per_set, fixed_capital_per_set=fixed_capital_per_set, params=params
    )
    if reason != ADMITTED:
        return ArbEvaluation(reason=reason, **base)
    min_size = max(leg.min_order_size for leg in legs)
    if sets < min_size:
        return ArbEvaluation(reason="below_min_order_size", quantity=sets, **base)
    if any(not ZERO < plan.limit_price < ONE for plan in plans):
        return ArbEvaluation(reason="limit_price_out_of_range", quantity=sets, **base)
    fees = sum((p.fees for p in plans), ZERO)
    slippage = sum((p.slippage for p in plans), ZERO)
    cash = sum((p.cash for p in plans), ZERO)
    net_profit = payoff_per_set * sets + cash - fees - slippage
    capital = fixed_capital_per_set * sets + sum((abs(p.cash) for p in plans if p.leg.side is Side.BUY), ZERO)
    return ArbEvaluation(
        reason=ADMITTED,
        legs=plans,
        quantity=sets,
        net_edge_per_set=net_profit / sets,
        net_profit=net_profit,
        total_fees=fees,
        total_slippage=slippage,
        capital_required=capital,
        **base,
    )


# --------------------------------------------------------------------------
# Public detectors
# --------------------------------------------------------------------------
def evaluate_merge(market: Market, yes_book: OrderBook, no_book: OrderBook, params: ArbParameters) -> ArbEvaluation:
    """Buy YES + buy NO below 1, merge to 1 USDC through the CTF."""
    legs = (_leg(market, Outcome.YES, Side.BUY, yes_book), _leg(market, Outcome.NO, Side.BUY, no_book))
    return _evaluate(
        ArbKind.MERGE, market.market_id, market.title, legs,
        payoff_per_set=ONE, fixed_capital_per_set=ZERO, params=params,
        executable_now=True, lockup="none", lockup_until=None, hidden_outcome_risk=False,
        mirror_consistent=books_are_mirrors(yes_book, no_book),
    )


def evaluate_split(market: Market, yes_book: OrderBook, no_book: OrderBook, params: ArbParameters) -> ArbEvaluation:
    """Split 1 USDC into YES + NO, sell both into the bids for more than 1."""
    legs = (_leg(market, Outcome.YES, Side.SELL, yes_book), _leg(market, Outcome.NO, Side.SELL, no_book))
    return _evaluate(
        ArbKind.SPLIT, market.market_id, market.title, legs,
        payoff_per_set=-ONE, fixed_capital_per_set=ONE, params=params,
        executable_now=True, lockup="none", lockup_until=None, hidden_outcome_risk=False,
        mirror_consistent=books_are_mirrors(yes_book, no_book),
    )


def evaluate_binary(market: Market, yes_book: OrderBook, no_book: OrderBook, params: ArbParameters) -> ArbEvaluation:
    """Best of merge / split for one binary market (admitted beats refused)."""
    merge = evaluate_merge(market, yes_book, no_book, params)
    split = evaluate_split(market, yes_book, no_book, params)
    if merge.traded and split.traded:
        return merge if merge.net_profit >= split.net_profit else split
    if merge.traded:
        return merge
    if split.traded:
        return split
    merge_gross = merge.gross_edge_per_set if merge.gross_edge_per_set is not None else Decimal("-1")
    split_gross = split.gross_edge_per_set if split.gross_edge_per_set is not None else Decimal("-1")
    return merge if merge_gross >= split_gross else split


def evaluate_negrisk_convert(
    group: MarketGroup,
    no_books: dict[str, OrderBook],
    params: ArbParameters,
) -> ArbEvaluation:
    """Buy NO on every visible leg, convert the set to ``K - 1`` USDC."""
    legs = tuple(
        _leg(market, Outcome.NO, Side.BUY, no_books.get(market.market_id, OrderBook(market_id=market.market_id)))
        for market in group.markets
    )
    k = Decimal(len(legs))
    payoff = (k - ONE) * (ONE - params.converter_fee_bps / BPS)
    refuse = None
    if len(legs) < 2:
        refuse = "single_outcome"
    elif not group.convertible:
        refuse = "converter_unavailable"
    return _evaluate(
        ArbKind.NEGRISK_CONVERT, group.group_id, group.title, legs,
        payoff_per_set=payoff, fixed_capital_per_set=ZERO, params=params,
        executable_now=group.convertible, lockup="none", lockup_until=None,
        hidden_outcome_risk=False, refuse=refuse,
    )


def evaluate_long_all_yes(
    group: MarketGroup,
    yes_books: dict[str, OrderBook],
    params: ArbParameters,
) -> ArbEvaluation:
    """Buy YES on every leg below 1; payoff 1 at resolution (capital locked)."""
    legs = tuple(
        _leg(market, Outcome.YES, Side.BUY, yes_books.get(market.market_id, OrderBook(market_id=market.market_id)))
        for market in group.markets
    )
    refuse = None
    if len(legs) < 2:
        refuse = "single_outcome"
    elif not group.exclusive and not params.allow_unverified_exclusivity:
        refuse = "exclusivity_unverified"
    elif group.augmented and not params.allow_hidden_outcome_long_yes:
        refuse = "hidden_outcome_risk"
    end_date = group.metadata.get("end_date")
    return _evaluate(
        ArbKind.LONG_ALL_YES, group.group_id, group.title, legs,
        payoff_per_set=ONE, fixed_capital_per_set=ZERO, params=params,
        executable_now=False, lockup="until_resolution",
        lockup_until=str(end_date) if end_date else None,
        hidden_outcome_risk=group.augmented, refuse=refuse,
    )


def realized_fee(quantity: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    """Rounded fee the paper fill simulator will actually charge."""
    return polymarket_taker_fee(quantity, price, rate)
