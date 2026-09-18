"""Queue-aware fill model for Kalshi FLB maker quotes (paper only).

The existing ``kalshi_maker_quote`` track uses an *expected-value* fill
``floor(qty * p)`` with documented ``join`` / ``improve`` probabilities
(:class:`strategies.flb.MakerQuoteStrategy`). That is deterministic and honest
about being an assumption, but it cannot speak to queue position, trade-through
fills, or post-fill adverse selection.

This module is the additive alternative: rest the same favourite-side fade quote
(join / improve) and fill it only through a **conservative L2+tape model** when
true L3 / FIFO is unavailable:

* sit **behind every contract displayed** at the quote's YES-equivalent price in
  the latest book at placement;
* later book snapshots may only *lengthen* that queue (arrivals between polls
  are assumed ahead of us; cancels ahead are never credited);
* fills come only from subsequent public prints on the far side — at our level
  after ``queue_ahead`` is consumed, or on a print *through* our level for at
  most the printed size;
* nothing fills inside ``reaction_latency_seconds`` or after ``quote_ttl_seconds``.

Honesty limits (also in ``docs/FLB_RUNBOOK.md`` and ``docs/ASSUMPTIONS.md`` §10.10):

* Public Kalshi books are aggregated size per level — no order ids, no FIFO.
* Between polls every crossing that reverts is invisible; the trade tape fills
  part of that gap, but the book each print hit is only known to the nearest
  snapshot (or not at all on a settled-trade-only path).
* Settled-history without L2 books fills **only on trade-through** prints
  (queue depth unknown → never assume we were first).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Sequence

from core.portfolio import Portfolio
from core.risk import RiskManager
from core.types import ONE, ZERO, Market, Order, OrderBook, Outcome, Position, Side
from strategies.flb import (
    FLB_RISK_LIMITS,
    FlbParameters,
    identify_longshot,
    portfolio_cash_at_risk,
    position_cash_at_risk,
    size_order,
    whole_contracts,
)
from strategies.whale_noise import Print, QuoteFill, displayed_at_level, markout

Placement = Literal["join", "improve"]
QuoteStatus = Literal["resting", "filled", "partially_filled_expired", "expired", "refused"]
_WHOLE = Decimal("1")

# Pre-registered net EV bar for the queue-aware maker fade (cents per contract
# after maker fees, settled when outcomes exist else contract-weighted markout).
DEFAULT_PASS_NET_EV = Decimal("0.02")  # 2¢
DEFAULT_STRETCH_NET_EV = Decimal("0.03")  # 3¢


@dataclass(frozen=True, slots=True)
class FlbQueueParameters:
    """Knobs for the queue-aware FLB maker sim. All values are documented guesses."""

    flb: FlbParameters = field(default_factory=FlbParameters)
    reaction_latency_seconds: Decimal = Decimal("2")
    quote_ttl_seconds: Decimal = Decimal("300")
    later_arrivals_ahead: bool = True
    max_resting_quotes_per_market: int = 1
    # When no L2 book is available (settled-trade harvest), only trade-through
    # prints may fill us — never assume we were at the front of an unknown queue.
    settled_trade_through_only: bool = True
    markout_horizons_seconds: tuple[int, ...] = (60, 300)
    min_fills_for_verdict: int = 30
    min_markets_for_verdict: int = 10
    pass_net_ev: Decimal = DEFAULT_PASS_NET_EV
    stretch_net_ev: Decimal = DEFAULT_STRETCH_NET_EV
    t_threshold: Decimal = Decimal("2")

    def __post_init__(self) -> None:
        if self.reaction_latency_seconds < ZERO or self.quote_ttl_seconds <= ZERO:
            raise ValueError("latency must be >= 0 and TTL > 0")
        if self.max_resting_quotes_per_market < 1:
            raise ValueError("max_resting_quotes_per_market must be positive")
        if any(h <= 0 for h in self.markout_horizons_seconds):
            raise ValueError("markout horizons must be positive")
        if self.min_fills_for_verdict < 1 or self.min_markets_for_verdict < 1:
            raise ValueError("verdict floors must be positive")
        if self.pass_net_ev <= ZERO or self.stretch_net_ev < self.pass_net_ev:
            raise ValueError("pass_net_ev must be positive and <= stretch_net_ev")

    @property
    def tick(self) -> Decimal:
        return self.flb.tick

    @property
    def longshot_threshold(self) -> Decimal:
        return self.flb.longshot_threshold

    @property
    def max_order_notional(self) -> Decimal:
        return self.flb.max_order_notional

    @property
    def max_market_notional(self) -> Decimal:
        return self.flb.max_market_notional

    @property
    def max_total_cash_at_risk(self) -> Decimal:
        return self.flb.max_total_cash_at_risk

    def as_dict(self) -> dict[str, Any]:
        return {
            "flb": {
                "longshot_threshold": self.flb.longshot_threshold,
                "max_order_notional": self.flb.max_order_notional,
                "max_market_notional": self.flb.max_market_notional,
                "max_total_cash_at_risk": self.flb.max_total_cash_at_risk,
                "tick": self.flb.tick,
                "join_fill_probability": self.flb.join_fill_probability,
                "improve_fill_probability": self.flb.improve_fill_probability,
                "adverse_selection_haircut": self.flb.adverse_selection_haircut,
            },
            "reaction_latency_seconds": self.reaction_latency_seconds,
            "quote_ttl_seconds": self.quote_ttl_seconds,
            "later_arrivals_ahead": self.later_arrivals_ahead,
            "max_resting_quotes_per_market": self.max_resting_quotes_per_market,
            "settled_trade_through_only": self.settled_trade_through_only,
            "markout_horizons_seconds": list(self.markout_horizons_seconds),
            "min_fills_for_verdict": self.min_fills_for_verdict,
            "min_markets_for_verdict": self.min_markets_for_verdict,
            "pass_net_ev": self.pass_net_ev,
            "stretch_net_ev": self.stretch_net_ev,
            "t_threshold": self.t_threshold,
            "risk_limits": {
                "max_notional_per_order": FLB_RISK_LIMITS.max_notional_per_order,
                "max_position_per_market": FLB_RISK_LIMITS.max_position_per_market,
                "max_daily_loss": FLB_RISK_LIMITS.max_daily_loss,
            },
        }


@dataclass(slots=True)
class FlbRestingQuote:
    """A favourite-side fade resting on the book under the conservative queue model."""

    market_id: str
    outcome: Outcome
    price: Decimal
    yes_price: Decimal
    quantity: Decimal
    placement: Placement
    placed_at: datetime
    active_from: datetime
    expires_at: datetime
    queue_ahead: Decimal
    queue_ahead_initial: Decimal
    longshot_outcome: Outcome
    longshot_price: Decimal
    reference_touch_yes: Decimal
    # +1 when we buy YES (fade a NO longshot); -1 when we buy NO (fade a YES longshot).
    direction: int
    filled: Decimal = ZERO
    fills: list[QuoteFill] = field(default_factory=list)
    status: QuoteStatus = "resting"
    trade_through: bool = False
    queue_raised_by_books: int = 0
    consumed_ahead: Decimal = ZERO
    # True when the quote was placed without an L2 book (settled-trade path).
    bookless: bool = False

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled

    def active_at(self, ts: datetime) -> bool:
        return self.status == "resting" and self.active_from <= ts < self.expires_at

    def reserved_collateral(self) -> Decimal:
        return self.remaining * self.price if self.status == "resting" else ZERO

    def expire(self, at: datetime) -> None:
        if self.status != "resting":
            return
        self.status = "partially_filled_expired" if self.filled > ZERO else "expired"
        self.expires_at = min(self.expires_at, at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market_id,
            "outcome": self.outcome.value,
            "price": self.price,
            "yes_price": self.yes_price,
            "quantity": self.quantity,
            "filled": self.filled,
            "status": self.status,
            "placement": self.placement,
            "placed_at": self.placed_at.isoformat(),
            "active_from": self.active_from.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "queue_ahead_initial": self.queue_ahead_initial,
            "queue_ahead_final": self.queue_ahead,
            "consumed_ahead": self.consumed_ahead,
            "queue_raised_by_books": self.queue_raised_by_books,
            "trade_through": self.trade_through,
            "longshot_outcome": self.longshot_outcome.value,
            "longshot_price": self.longshot_price,
            "reference_touch_yes": self.reference_touch_yes,
            "direction": self.direction,
            "bookless": self.bookless,
            "fills": [
                {
                    "ts": f.ts.isoformat(),
                    "quantity": f.quantity,
                    "yes_price": f.yes_price,
                    "trade_through": f.trade_through,
                    "trade_id": f.trade_id,
                }
                for f in self.fills
            ],
        }


class FlbQueueFillModel:
    """Conservative fill rules for an FLB resting quote; see the module docstring."""

    def __init__(self, params: FlbQueueParameters) -> None:
        self.params = params

    def hits(self, quote: FlbRestingQuote, print_: Print) -> tuple[bool, bool]:
        """(touches our level, trades through it) for a print on the far side."""
        if quote.outcome is Outcome.YES:
            if print_.taker_side != "no":
                return False, False
            return print_.yes_price <= quote.yes_price, print_.yes_price < quote.yes_price
        if print_.taker_side != "yes":
            return False, False
        return print_.yes_price >= quote.yes_price, print_.yes_price > quote.yes_price

    def on_print(self, quote: FlbRestingQuote, print_: Print) -> Decimal:
        if not quote.active_at(print_.ts):
            return ZERO
        touches, through = self.hits(quote, print_)
        if not touches:
            return ZERO
        if quote.bookless and self.params.settled_trade_through_only and not through:
            # Without L2 we refuse to assume we were ahead of any unknown queue.
            return ZERO
        if through:
            filled = min(quote.remaining, whole_contracts(print_.size))
            quote.trade_through = True
        else:
            ahead = min(quote.queue_ahead, print_.size)
            quote.queue_ahead -= ahead
            quote.consumed_ahead += ahead
            filled = min(quote.remaining, whole_contracts(print_.size - ahead))
        if filled <= ZERO:
            return ZERO
        quote.filled += filled
        quote.fills.append(QuoteFill(print_.ts, filled, quote.yes_price, through, print_.trade_id))
        if quote.remaining <= ZERO:
            quote.status = "filled"
        return filled

    def on_book(self, quote: FlbRestingQuote, book: OrderBook) -> None:
        if quote.status != "resting" or not self.params.later_arrivals_ahead or quote.bookless:
            return
        displayed = displayed_at_level(book, quote.outcome, quote.yes_price)
        if displayed > quote.queue_ahead:
            quote.queue_ahead = displayed
            quote.queue_raised_by_books += 1


@dataclass(frozen=True, slots=True)
class FlbQuoteEvaluation:
    market_id: str
    reason: str
    quote: FlbRestingQuote | None = None
    order: Order | None = None
    placement: Placement | None = None
    yes_price: Decimal | None = None
    mid: Decimal | None = None
    spread: Decimal | None = None
    displayed_at_level: Decimal | None = None
    longshot_outcome: Outcome | None = None
    longshot_price: Decimal | None = None

    @property
    def traded(self) -> bool:
        return self.quote is not None


def _probe(market: Market, outcome: Outcome, price: Decimal) -> Order:
    return Order(venue=market.venue, market_id=market.market_id, side=Side.BUY, quantity=_WHOLE, outcome=outcome, price=price)


def place_flb_maker_quote(
    market: Market,
    book: OrderBook,
    params: FlbQueueParameters,
    *,
    placed_at: datetime,
    position: Position | None,
    risk: RiskManager | None,
    total_cash_at_risk: Decimal,
    active_quotes_in_market: int = 0,
) -> FlbQuoteEvaluation:
    """Rest the FLB favourite-side fade (join / one-tick improve) under queue rules.

    Placement matches :class:`strategies.flb.MakerQuoteStrategy`; fill probability
    metadata is omitted — fills come only from :class:`FlbQueueFillModel`.
    """
    base: dict[str, Any] = {
        "market_id": market.market_id,
        "mid": book.mid_price,
        "spread": book.spread,
    }
    if not market.active:
        return FlbQuoteEvaluation(reason="market_inactive", **base)
    if active_quotes_in_market >= params.max_resting_quotes_per_market:
        return FlbQuoteEvaluation(reason="quote_already_resting", **base)
    found = identify_longshot(book, params.longshot_threshold)
    if found is None:
        return FlbQuoteEvaluation(reason="not_longshot", **base)
    longshot, longshot_price = found
    base.update({"longshot_outcome": longshot, "longshot_price": longshot_price})
    ask, bid = book.best_ask, book.best_bid
    if ask is None or bid is None:
        return FlbQuoteEvaluation(reason="one_sided_book", **base)
    spread = ask.price - bid.price
    improve = spread >= 2 * params.tick
    if longshot is Outcome.YES:
        yes_price = ask.price - params.tick if improve else ask.price
        outcome, price, displayed_touch = Outcome.NO, ONE - yes_price, ask.size
        direction = -1
    else:
        yes_price = bid.price + params.tick if improve else bid.price
        outcome, price, displayed_touch = Outcome.YES, yes_price, bid.size
        direction = 1
    base["yes_price"] = yes_price
    if not ZERO < price < ONE:
        return FlbQuoteEvaluation(reason="touch_at_bound", **base)
    placement: Placement = "improve" if improve else "join"
    displayed = displayed_at_level(book, outcome, yes_price)
    # Joining the touch: queue is the full displayed size at that level (we go last).
    # Improving: we are first at a new price, so queue_ahead starts at 0; displayed
    # at the *old* touch is not ahead of us.
    queue_ahead = ZERO if improve else (displayed if displayed > ZERO else displayed_touch)
    base.update({"placement": placement, "displayed_at_level": queue_ahead})
    headroom = params.max_market_notional - position_cash_at_risk(position)
    if headroom < price:
        return FlbQuoteEvaluation(reason="market_cap_reached", **base)
    if params.max_total_cash_at_risk - total_cash_at_risk < price:
        return FlbQuoteEvaluation(reason="capital_cap_reached", **base)
    probe = _probe(market, outcome, price)
    quantity = size_order(
        price=price,
        touch_size=Decimal("1000000"),
        params=params.flb,
        position=position,
        risk=risk,
        probe=probe,
        total_cash_at_risk=total_cash_at_risk,
    )
    if quantity <= ZERO:
        if risk is not None and risk.halted:
            return FlbQuoteEvaluation(reason="risk_halted", **base)
        return FlbQuoteEvaluation(reason="risk_position_cap_reached", **base)
    latency = timedelta(seconds=float(params.reaction_latency_seconds))
    ttl = timedelta(seconds=float(params.quote_ttl_seconds))
    quote = FlbRestingQuote(
        market_id=market.market_id,
        outcome=outcome,
        price=price,
        yes_price=yes_price,
        quantity=quantity,
        placement=placement,
        placed_at=placed_at,
        active_from=placed_at + latency,
        expires_at=placed_at + latency + ttl,
        queue_ahead=queue_ahead,
        queue_ahead_initial=queue_ahead,
        longshot_outcome=longshot,
        longshot_price=longshot_price,
        reference_touch_yes=ask.price if longshot is Outcome.YES else bid.price,
        direction=direction,
    )
    order = Order(
        venue=market.venue,
        market_id=market.market_id,
        side=Side.BUY,
        quantity=quantity,
        outcome=outcome,
        price=price,
        metadata={
            "strategy": "kalshi_maker_queue_sim",
            "execution": "maker_resting",
            "placement": placement,
            "longshot_outcome": longshot.value,
            "longshot_price": str(longshot_price),
            "queue_ahead": str(queue_ahead),
            "fill_model": "conservative_l2_tape",
        },
    )
    return FlbQuoteEvaluation(reason="quote", quote=quote, order=order, **base)


def place_flb_maker_quote_from_longshot_print(
    market: Market,
    print_: Print,
    params: FlbQueueParameters,
    *,
    position: Position | None,
    risk: RiskManager | None,
    total_cash_at_risk: Decimal,
    active_quotes_in_market: int = 0,
) -> FlbQuoteEvaluation:
    """Bookless settled-history placement: fade a longshot *taker* print.

    When the taker buys a side priced below ``longshot_threshold``, we rest on
    the favourite side at one tick better than the complementary price. Without
    an L2 book the quote is marked ``bookless`` so only trade-through prints fill.
    """
    base: dict[str, Any] = {"market_id": market.market_id, "mid": None, "spread": None}
    if not market.active:
        return FlbQuoteEvaluation(reason="market_inactive", **base)
    if active_quotes_in_market >= params.max_resting_quotes_per_market:
        return FlbQuoteEvaluation(reason="quote_already_resting", **base)
    if print_.taker_price >= params.longshot_threshold:
        return FlbQuoteEvaluation(reason="not_longshot", **base)
    longshot = print_.taker_outcome
    longshot_price = print_.taker_price
    base.update({"longshot_outcome": longshot, "longshot_price": longshot_price})
    # Favourite side: one tick inside the longshot print's complementary price.
    if longshot is Outcome.YES:
        # Taker bought YES cheap → we sell YES / buy NO at 1 - (yes_print - tick) when possible.
        yes_price = min(ONE - params.tick, print_.yes_price + params.tick)
        outcome, price = Outcome.NO, ONE - yes_price
        direction = -1
    else:
        yes_price = max(params.tick, print_.yes_price - params.tick)
        outcome, price = Outcome.YES, yes_price
        direction = 1
    base.update({"yes_price": yes_price, "placement": "improve", "displayed_at_level": ZERO})
    if not ZERO < price < ONE:
        return FlbQuoteEvaluation(reason="touch_at_bound", **base)
    headroom = params.max_market_notional - position_cash_at_risk(position)
    if headroom < price:
        return FlbQuoteEvaluation(reason="market_cap_reached", **base)
    if params.max_total_cash_at_risk - total_cash_at_risk < price:
        return FlbQuoteEvaluation(reason="capital_cap_reached", **base)
    probe = _probe(market, outcome, price)
    quantity = size_order(
        price=price,
        touch_size=Decimal("1000000"),
        params=params.flb,
        position=position,
        risk=risk,
        probe=probe,
        total_cash_at_risk=total_cash_at_risk,
    )
    if quantity <= ZERO:
        if risk is not None and risk.halted:
            return FlbQuoteEvaluation(reason="risk_halted", **base)
        return FlbQuoteEvaluation(reason="risk_position_cap_reached", **base)
    latency = timedelta(seconds=float(params.reaction_latency_seconds))
    ttl = timedelta(seconds=float(params.quote_ttl_seconds))
    quote = FlbRestingQuote(
        market_id=market.market_id,
        outcome=outcome,
        price=price,
        yes_price=yes_price,
        quantity=quantity,
        placement="improve",
        placed_at=print_.ts,
        active_from=print_.ts + latency,
        expires_at=print_.ts + latency + ttl,
        queue_ahead=ZERO,
        queue_ahead_initial=ZERO,
        longshot_outcome=longshot,
        longshot_price=longshot_price,
        reference_touch_yes=print_.yes_price,
        direction=direction,
        bookless=True,
    )
    order = Order(
        venue=market.venue,
        market_id=market.market_id,
        side=Side.BUY,
        quantity=quantity,
        outcome=outcome,
        price=price,
        metadata={
            "strategy": "kalshi_maker_queue_sim",
            "execution": "maker_resting",
            "placement": "improve",
            "longshot_outcome": longshot.value,
            "longshot_price": str(longshot_price),
            "queue_ahead": "0",
            "fill_model": "settled_trade_through_only",
            "trigger_trade_id": print_.trade_id,
        },
    )
    return FlbQuoteEvaluation(reason="quote", quote=quote, order=order, **base)


def reserved_collateral(quotes: Sequence[FlbRestingQuote]) -> Decimal:
    return sum((q.reserved_collateral() for q in quotes), ZERO)


def portfolio_and_resting_cash_at_risk(portfolio: Portfolio | None, quotes: Sequence[FlbRestingQuote]) -> Decimal:
    return portfolio_cash_at_risk(portfolio) + reserved_collateral(quotes)


__all__ = [
    "DEFAULT_PASS_NET_EV",
    "DEFAULT_STRETCH_NET_EV",
    "FlbQueueFillModel",
    "FlbQueueParameters",
    "FlbQuoteEvaluation",
    "FlbRestingQuote",
    "markout",
    "place_flb_maker_quote",
    "place_flb_maker_quote_from_longshot_print",
    "portfolio_and_resting_cash_at_risk",
    "reserved_collateral",
]
