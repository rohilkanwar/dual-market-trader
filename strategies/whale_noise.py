"""Make-on-whale, take-on-noise: Kalshi tennis paper strategy primitives.

Two legs share one event stream (public trade prints plus polled L2 books):

* **Maker leg** — when a print classified as a *whale lift* (a large aggressive
  buy of one outcome) arrives, rest a same-direction bid one tick *behind* the
  touch of that outcome (never improving, never joining the whale's level),
  inventory-capped. The order fills only through :class:`ConservativeQueueModel`.
* **Taker leg** — when only *retail longshot flow* is hitting a market (small
  prints buying a side priced below the longshot threshold, no whale print in
  the lookback window), take the fade: buy the favourite at the touch through
  :class:`strategies.flb.LongshotFadeStrategy`.

Everything here is pure: no I/O, no venue calls. The replay engine in
:mod:`research.whale_noise` drives these primitives from a timeline.

What "known +EV whale" means here
---------------------------------
Kalshi's public tape (``GET /markets/trades``) carries ``taker_side``,
``yes_price``, ``count`` and ``created_time`` but **no trader identity**. A
whale is therefore a *size class*, not a person: a non-block print whose
contract count clears an absolute floor, a multiple of the market's recent
median print size, and a notional floor (:class:`WhaleRule`). Whether that size
class is +EV is a separate, measurable question answered ex post from settled
tennis markets (:func:`research.whale_noise.whale_flow_expost`) or asserted by
an operator in a whale registry; the default rule is labelled ``unverified``
and the report carries the status on every run.

Maker fill model (explicit and conservative — no FIFO fantasy)
--------------------------------------------------------------
A resting paper order at YES-equivalent price ``p`` is assumed to sit **behind
every contract displayed at ``p``** in the latest book at placement
(``queue_ahead``). It never gains priority from cancels ahead of it (those are
invisible in public L2), and by default a later book snapshot that shows *more*
size at ``p`` raises ``queue_ahead`` (arrivals between polls are assumed to be
ahead). Fills come only from subsequent public prints on the far side:

* a print *at* ``p`` first consumes ``queue_ahead``, then fills us with the
  remainder (whole contracts, floored);
* a print *through* ``p`` (a worse price for the taker) proves a taker of at
  least that size passed our better level, so we fill ``min(remaining,
  printed size)`` at ``p`` — never the whole level;
* prints within ``reaction_latency_seconds`` of the trigger (including the
  whale's own print) cannot fill us; the quote expires after ``quote_ttl_seconds``.

Sizing reuses the paper risk defaults decided for a later live canary:
$25 notional per order, $75 cash at risk per market, $75 daily loss
(:data:`strategies.flb.FLB_RISK_LIMITS`), $1,000 total collateral, whole
contracts. Unfilled resting quantity reserves collateral while it rests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from statistics import median
from typing import Any, Literal, Sequence

from core.portfolio import Portfolio
from core.risk import RiskManager
from core.types import ONE, ZERO, Market, Order, OrderBook, Outcome, Position, Side
from strategies.flb import (
    FLB_RISK_LIMITS,
    FlbParameters,
    portfolio_cash_at_risk,
    position_cash_at_risk,
    size_order,
    whole_contracts,
)

# Kalshi tennis match-winner series (public API, 2026-09-14). All are
# ``quadratic_with_maker_fees`` (KXATPMATCH / KXWTAMATCH) or ``quadratic``
# with fee_multiplier 1; the fee model reads the series metadata per market.
DEFAULT_TENNIS_SERIES: tuple[str, ...] = (
    "KXATPMATCH",
    "KXWTAMATCH",
    "KXATPCHALLENGERMATCH",
    "KXWTACHALLENGERMATCH",
    "KXITFMATCH",
    "KXITFWMATCH",
)
EvStatus = Literal["unverified", "operator_asserted", "verified_expost", "refuted_expost"]
_WHOLE = Decimal("1")


class PrintClass(StrEnum):
    WHALE_LIFT = "whale_lift"
    RETAIL_LONGSHOT = "retail_longshot"
    RETAIL_OTHER = "retail_other"
    BLOCK_TRADE = "block_trade"


@dataclass(frozen=True, slots=True)
class Print:
    """One public trade print. ``taker_side`` is the outcome the taker bought."""

    ts: datetime
    yes_price: Decimal
    size: Decimal
    taker_side: str
    trade_id: str = ""
    block: bool = False

    def __post_init__(self) -> None:
        if self.taker_side not in ("yes", "no"):
            raise ValueError(f"taker_side must be 'yes' or 'no', got {self.taker_side!r}")
        if not ZERO <= self.yes_price <= ONE:
            raise ValueError(f"yes_price {self.yes_price} must be between 0 and 1")
        if self.size <= ZERO:
            raise ValueError("print size must be positive")

    @property
    def taker_outcome(self) -> Outcome:
        return Outcome.YES if self.taker_side == "yes" else Outcome.NO

    @property
    def taker_price(self) -> Decimal:
        """Price the taker paid for the outcome it bought."""
        return self.yes_price if self.taker_side == "yes" else ONE - self.yes_price

    @property
    def direction(self) -> int:
        """+1 when the taker bought YES (lifted the YES offer), -1 when it bought NO."""
        return 1 if self.taker_side == "yes" else -1

    @property
    def notional(self) -> Decimal:
        return self.size * self.taker_price


@dataclass(frozen=True, slots=True)
class WhaleRule:
    """Size-class definition of a whale print for one series (or the default).

    A print is a whale lift when it is not a block trade and
    ``size >= min_contracts`` and ``notional >= min_notional`` and, once at least
    ``min_history`` earlier prints exist in the market, ``size >= size_multiple *
    median(recent sizes)``. ``ev_status`` records *why* this size class is
    treated as +EV; the default is ``unverified``.

    Defaults were calibrated on the 2026-09-14 public tape of 150 open Kalshi
    tennis markets (28k prints): median print 21 contracts / $9, p98 1,139
    contracts, p99 notional $1,010. ``1000 / 10x / $500`` selects ~2% of prints.
    """

    min_contracts: Decimal = Decimal("1000")
    size_multiple: Decimal = Decimal("10")
    min_notional: Decimal = Decimal("500")
    min_history: int = 5
    ev_status: EvStatus = "unverified"
    note: str = ""

    def __post_init__(self) -> None:
        if self.min_contracts <= ZERO or self.size_multiple <= ZERO or self.min_notional < ZERO:
            raise ValueError("whale thresholds must be positive")
        if self.min_history < 0:
            raise ValueError("min_history must not be negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_contracts": self.min_contracts,
            "size_multiple": self.size_multiple,
            "min_notional": self.min_notional,
            "min_history": self.min_history,
            "ev_status": self.ev_status,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], base: WhaleRule | None = None) -> WhaleRule:
        base = base or cls()
        return cls(
            min_contracts=Decimal(str(raw.get("min_contracts", base.min_contracts))),
            size_multiple=Decimal(str(raw.get("size_multiple", base.size_multiple))),
            min_notional=Decimal(str(raw.get("min_notional", base.min_notional))),
            min_history=int(raw.get("min_history", base.min_history)),
            ev_status=str(raw.get("ev_status", base.ev_status)),  # type: ignore[arg-type]
            note=str(raw.get("note", base.note)),
        )


@dataclass(frozen=True, slots=True)
class WhaleNoiseParameters:
    """Every knob of the two legs. All values are documented assumptions."""

    series: tuple[str, ...] = DEFAULT_TENNIS_SERIES
    default_rule: WhaleRule = field(default_factory=WhaleRule)
    series_rules: dict[str, WhaleRule] = field(default_factory=dict)
    recent_prints_window: int = 50
    # Maker leg
    ticks_behind: int = 1
    tick: Decimal = Decimal("0.01")
    reaction_latency_seconds: Decimal = Decimal("2")
    quote_ttl_seconds: Decimal = Decimal("300")
    later_arrivals_ahead: bool = True
    max_resting_quotes_per_market: int = 1
    require_ev_verified: bool = False
    # Taker leg
    longshot_threshold: Decimal = Decimal("0.20")
    whale_lookback_seconds: Decimal = Decimal("120")
    taker_cooldown_seconds: Decimal = Decimal("60")
    minimum_touch_size: Decimal = Decimal("1")
    # Caps (paper defaults decided for a later live canary; see FLB_RISK_LIMITS)
    max_order_notional: Decimal = FLB_RISK_LIMITS.max_notional_per_order
    max_market_notional: Decimal = FLB_RISK_LIMITS.max_position_per_market
    max_total_cash_at_risk: Decimal = Decimal("1000")
    # Measurement
    markout_horizons_seconds: tuple[int, ...] = (60, 300)
    min_fills_for_verdict: int = 5

    def __post_init__(self) -> None:
        if self.ticks_behind < 1:
            raise ValueError("ticks_behind must be at least 1: the maker never joins or improves the whale's level")
        if self.tick <= ZERO:
            raise ValueError("tick must be positive")
        if self.reaction_latency_seconds < ZERO or self.quote_ttl_seconds <= ZERO:
            raise ValueError("latency must be >= 0 and TTL > 0")
        if not ZERO < self.longshot_threshold < Decimal("0.5"):
            raise ValueError("longshot_threshold must be in (0, 0.5)")
        if self.max_order_notional <= ZERO or self.max_market_notional <= ZERO or self.max_total_cash_at_risk <= ZERO:
            raise ValueError("notional caps must be positive")
        if self.max_resting_quotes_per_market < 1 or self.recent_prints_window < 1:
            raise ValueError("counts must be positive")
        if any(h <= 0 for h in self.markout_horizons_seconds):
            raise ValueError("markout horizons must be positive")

    def rule_for(self, series: str | None) -> WhaleRule:
        if series and series in self.series_rules:
            return self.series_rules[series]
        return self.default_rule

    def fade_parameters(self) -> FlbParameters:
        """The taker leg reuses the FLB longshot fade with these caps."""
        return FlbParameters(
            longshot_threshold=self.longshot_threshold,
            max_order_notional=self.max_order_notional,
            max_market_notional=self.max_market_notional,
            max_total_cash_at_risk=self.max_total_cash_at_risk,
            minimum_touch_size=self.minimum_touch_size,
            tick=self.tick,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "series": list(self.series),
            "default_rule": self.default_rule.as_dict(),
            "series_rules": {k: v.as_dict() for k, v in sorted(self.series_rules.items())},
            "recent_prints_window": self.recent_prints_window,
            "ticks_behind": self.ticks_behind,
            "tick": self.tick,
            "reaction_latency_seconds": self.reaction_latency_seconds,
            "quote_ttl_seconds": self.quote_ttl_seconds,
            "later_arrivals_ahead": self.later_arrivals_ahead,
            "max_resting_quotes_per_market": self.max_resting_quotes_per_market,
            "require_ev_verified": self.require_ev_verified,
            "longshot_threshold": self.longshot_threshold,
            "whale_lookback_seconds": self.whale_lookback_seconds,
            "taker_cooldown_seconds": self.taker_cooldown_seconds,
            "minimum_touch_size": self.minimum_touch_size,
            "max_order_notional": self.max_order_notional,
            "max_market_notional": self.max_market_notional,
            "max_total_cash_at_risk": self.max_total_cash_at_risk,
            "risk_limits": {
                "max_notional_per_order": FLB_RISK_LIMITS.max_notional_per_order,
                "max_position_per_market": FLB_RISK_LIMITS.max_position_per_market,
                "max_daily_loss": FLB_RISK_LIMITS.max_daily_loss,
            },
            "markout_horizons_seconds": list(self.markout_horizons_seconds),
            "min_fills_for_verdict": self.min_fills_for_verdict,
        }


def load_whale_registry(raw: dict[str, Any], base: WhaleRule | None = None) -> tuple[WhaleRule, dict[str, WhaleRule]]:
    """Parse an operator whale registry ``{"default": {...}, "series": {"KXWTAMATCH": {...}}}``.

    The registry is an operator input (like fair-value priors): it may raise a
    series' thresholds or assert ``ev_status``; nothing here infers EV.
    """
    default = WhaleRule.from_dict(raw.get("default") or {}, base)
    series = {str(k): WhaleRule.from_dict(v or {}, default) for k, v in (raw.get("series") or {}).items()}
    return default, series


# --------------------------------------------------------------------------
# Print classification
# --------------------------------------------------------------------------
def is_whale_lift(print_: Print, recent_sizes: Sequence[Decimal], rule: WhaleRule) -> bool:
    if print_.block:
        return False
    if print_.size < rule.min_contracts or print_.notional < rule.min_notional:
        return False
    if len(recent_sizes) >= rule.min_history and rule.min_history > 0:
        return print_.size >= rule.size_multiple * Decimal(str(median(recent_sizes)))
    return True


def classify_print(print_: Print, recent_sizes: Sequence[Decimal], params: WhaleNoiseParameters, *, series: str | None = None) -> PrintClass:
    """Whale lift, retail longshot flow, other retail, or block trade.

    Block trades are negotiated off-book and never "lift the offer", so they are
    neither whales nor noise for this track (counted separately).
    """
    if print_.block:
        return PrintClass.BLOCK_TRADE
    rule = params.rule_for(series)
    if is_whale_lift(print_, recent_sizes, rule):
        return PrintClass.WHALE_LIFT
    if print_.taker_price < params.longshot_threshold:
        return PrintClass.RETAIL_LONGSHOT
    return PrintClass.RETAIL_OTHER


# --------------------------------------------------------------------------
# Resting quotes and the conservative queue model
# --------------------------------------------------------------------------
QuoteStatus = Literal["resting", "filled", "partially_filled_expired", "expired", "refused"]


@dataclass(frozen=True, slots=True)
class QuoteFill:
    ts: datetime
    quantity: Decimal
    yes_price: Decimal
    trade_through: bool
    trade_id: str


@dataclass(slots=True)
class RestingQuote:
    market_id: str
    outcome: Outcome
    price: Decimal
    yes_price: Decimal
    quantity: Decimal
    placed_at: datetime
    active_from: datetime
    expires_at: datetime
    queue_ahead: Decimal
    queue_ahead_initial: Decimal
    trigger_trade_id: str
    whale_direction: int
    whale_size: Decimal
    reference_touch: Decimal
    filled: Decimal = ZERO
    fills: list[QuoteFill] = field(default_factory=list)
    status: QuoteStatus = "resting"
    trade_through: bool = False
    queue_raised_by_books: int = 0
    consumed_ahead: Decimal = ZERO

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.outcome is Outcome.YES else -self.quantity

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
            "placed_at": self.placed_at.isoformat(),
            "active_from": self.active_from.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "queue_ahead_initial": self.queue_ahead_initial,
            "queue_ahead_final": self.queue_ahead,
            "consumed_ahead": self.consumed_ahead,
            "queue_raised_by_books": self.queue_raised_by_books,
            "trade_through": self.trade_through,
            "trigger_trade_id": self.trigger_trade_id,
            "whale_direction": self.whale_direction,
            "whale_size": self.whale_size,
            "reference_touch": self.reference_touch,
            "fills": [
                {"ts": f.ts.isoformat(), "quantity": f.quantity, "yes_price": f.yes_price, "trade_through": f.trade_through, "trade_id": f.trade_id}
                for f in self.fills
            ],
        }


def displayed_at_level(book: OrderBook, outcome: Outcome, yes_price: Decimal) -> Decimal:
    """Size displayed on *our* side of the book at our YES-equivalent price.

    A YES bid competes with the YES bid ladder; a NO bid is a YES ask and
    competes with the YES ask ladder (Kalshi's NO bids at ``1 - p``).
    """
    ladder = book.bids if outcome is Outcome.YES else book.asks
    return sum((level.size for level in ladder if level.price == yes_price), ZERO)


class ConservativeQueueModel:
    """Explicit fill rules for a resting paper order; see the module docstring."""

    def __init__(self, params: WhaleNoiseParameters) -> None:
        self.params = params

    def hits(self, quote: RestingQuote, print_: Print) -> tuple[bool, bool]:
        """(touches our level, trades through it) for a print on the far side."""
        if quote.outcome is Outcome.YES:
            # We bid YES; YES sellers (takers buying NO) hit bids at or below our price.
            if print_.taker_side != "no":
                return False, False
            return print_.yes_price <= quote.yes_price, print_.yes_price < quote.yes_price
        # We bid NO (= ask YES); YES buyers lift asks at or above our YES price.
        if print_.taker_side != "yes":
            return False, False
        return print_.yes_price >= quote.yes_price, print_.yes_price > quote.yes_price

    def on_print(self, quote: RestingQuote, print_: Print) -> Decimal:
        """Apply one print; returns the whole contracts filled for us (possibly 0)."""
        if not quote.active_at(print_.ts):
            return ZERO
        touches, through = self.hits(quote, print_)
        if not touches:
            return ZERO
        if through:
            # Price priority: a taker printing at a worse price must have taken
            # our better level first, but only for as many contracts as it
            # printed. Never assume the whole level was ours.
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

    def on_book(self, quote: RestingQuote, book: OrderBook) -> None:
        """A later snapshot can only *lengthen* the queue ahead of us."""
        if quote.status != "resting" or not self.params.later_arrivals_ahead:
            return
        displayed = displayed_at_level(book, quote.outcome, quote.yes_price)
        if displayed > quote.queue_ahead:
            quote.queue_ahead = displayed
            quote.queue_raised_by_books += 1


# --------------------------------------------------------------------------
# Maker leg: quote one tick behind the whale's side
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QuoteEvaluation:
    market_id: str
    reason: str
    quote: RestingQuote | None = None
    order: Order | None = None
    whale_direction: int = 0
    reference_touch: Decimal | None = None
    yes_price: Decimal | None = None
    mid: Decimal | None = None
    displayed_at_level: Decimal | None = None
    ev_status: str = "unverified"

    @property
    def traded(self) -> bool:
        return self.quote is not None


def _probe(market: Market, outcome: Outcome, price: Decimal) -> Order:
    return Order(venue=market.venue, market_id=market.market_id, side=Side.BUY, quantity=_WHOLE, outcome=outcome, price=price)


def whale_follow_quote(
    market: Market,
    book: OrderBook,
    whale: Print,
    params: WhaleNoiseParameters,
    *,
    position: Position | None,
    risk: RiskManager | None,
    total_cash_at_risk: Decimal,
    active_quotes_in_market: int = 0,
) -> QuoteEvaluation:
    """Rest a same-direction bid ``ticks_behind`` ticks behind the whale's side touch.

    Whale bought YES -> we bid YES at ``best_bid - k*tick``. Whale bought NO ->
    we bid NO one tick behind the NO bid, i.e. a YES ask at ``best_ask +
    k*tick``. The book is the latest snapshot *before* the whale print, so the
    reference touch is pre-impact; sitting behind it is the conservative side
    of "one tick behind".
    """
    base: dict[str, Any] = {"market_id": market.market_id, "whale_direction": whale.direction, "mid": book.mid_price}
    rule = params.rule_for(str(market.metadata.get("series_ticker") or "") or None)
    base["ev_status"] = rule.ev_status
    if not market.active:
        return QuoteEvaluation(reason="market_inactive", **base)
    if params.require_ev_verified and rule.ev_status not in ("verified_expost", "operator_asserted"):
        return QuoteEvaluation(reason="whale_ev_unverified", **base)
    if active_quotes_in_market >= params.max_resting_quotes_per_market:
        return QuoteEvaluation(reason="quote_already_resting", **base)
    step = params.tick * params.ticks_behind
    if whale.direction > 0:
        touch = book.best_bid
        if touch is None:
            return QuoteEvaluation(reason="one_sided_book", **base)
        yes_price = touch.price - step
        outcome, price = Outcome.YES, yes_price
    else:
        touch = book.best_ask
        if touch is None:
            return QuoteEvaluation(reason="one_sided_book", **base)
        yes_price = touch.price + step
        outcome, price = Outcome.NO, ONE - yes_price
    base.update({"reference_touch": touch.price, "yes_price": yes_price})
    if not ZERO < price < ONE:
        return QuoteEvaluation(reason="touch_at_bound", **base)
    displayed = displayed_at_level(book, outcome, yes_price)
    base["displayed_at_level"] = displayed
    headroom = params.max_market_notional - position_cash_at_risk(position)
    if headroom < price:
        return QuoteEvaluation(reason="market_cap_reached", **base)
    if params.max_total_cash_at_risk - total_cash_at_risk < price:
        return QuoteEvaluation(reason="capital_cap_reached", **base)
    probe = _probe(market, outcome, price)
    quantity = size_order(
        price=price,
        touch_size=Decimal("1000000"),  # depth is not a constraint for a resting order
        params=params,  # type: ignore[arg-type]
        position=position,
        risk=risk,
        probe=probe,
        total_cash_at_risk=total_cash_at_risk,
    )
    if quantity <= ZERO:
        if risk is not None and risk.halted:
            return QuoteEvaluation(reason="risk_halted", **base)
        return QuoteEvaluation(reason="risk_position_cap_reached", **base)
    latency = timedelta(seconds=float(params.reaction_latency_seconds))
    ttl = timedelta(seconds=float(params.quote_ttl_seconds))
    quote = RestingQuote(
        market_id=market.market_id,
        outcome=outcome,
        price=price,
        yes_price=yes_price,
        quantity=quantity,
        placed_at=whale.ts,
        active_from=whale.ts + latency,
        expires_at=whale.ts + latency + ttl,
        queue_ahead=displayed,
        queue_ahead_initial=displayed,
        trigger_trade_id=whale.trade_id,
        whale_direction=whale.direction,
        whale_size=whale.size,
        reference_touch=touch.price,
    )
    order = Order(
        venue=market.venue,
        market_id=market.market_id,
        side=Side.BUY,
        quantity=quantity,
        outcome=outcome,
        price=price,
        metadata={
            "strategy": "kalshi_whale_maker_leg",
            "execution": "maker_resting",
            "placement": "behind",
            "ticks_behind": str(params.ticks_behind),
            "whale_trade_id": whale.trade_id,
            "whale_size": str(whale.size),
            "whale_direction": str(whale.direction),
            "queue_ahead": str(displayed),
            "ev_status": rule.ev_status,
        },
    )
    return QuoteEvaluation(reason="quote", quote=quote, order=order, **base)


def reserved_collateral(quotes: Sequence[RestingQuote]) -> Decimal:
    return sum((q.reserved_collateral() for q in quotes), ZERO)


def portfolio_and_resting_cash_at_risk(portfolio: Portfolio | None, quotes: Sequence[RestingQuote]) -> Decimal:
    return portfolio_cash_at_risk(portfolio) + reserved_collateral(quotes)


# --------------------------------------------------------------------------
# Markouts (toxicity)
# --------------------------------------------------------------------------
def markout(direction: int, entry_yes_price: Decimal, later_mid: Decimal) -> Decimal:
    """Signed price move after a fill, positive when the market moved our way."""
    return Decimal(direction) * (later_mid - entry_yes_price)
