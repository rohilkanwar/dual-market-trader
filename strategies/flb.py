"""Kalshi favorite–longshot bias (FLB) paper strategies.

Two hypothetical ways to be the counterparty of a longshot buyer, plus the
longshot buyer itself as a shadow benchmark:

* :class:`LongshotFadeStrategy` — a *taker* fade. When one side of a binary
  Kalshi market is a longshot (YES ask below ``longshot_threshold``, or
  YES bid above ``1 - longshot_threshold`` so NO is the longshot), buy
  the favourite side at the touch, crossing the spread and paying taker fees.
* :class:`MakerQuoteStrategy` — a *maker* fade. Same trigger, but the order
  rests: it improves the favourite-side bid by one tick when the spread allows,
  otherwise joins the touch behind the displayed queue. Fills are simulated by
  :class:`research.flb.FlbSnapshotClient` from the ``fill_probability`` the
  strategy attaches to the order (documented assumption, not a measurement).
* :func:`longshot_buy_order` — the mirror trade (buy the longshot at its ask).
  Never proposed for the ledger; the track books it in a shadow ledger so the
  artifact shows "longshot buyer vs fade" side by side.

Sizing encodes the caps decided for a later live canary as **paper** defaults:
$25 notional per order, $75 per market, $75 daily loss (:data:`FLB_RISK_LIMITS`).
``RiskManager`` enforces the position cap in contracts (75 contracts is at most
$75 because every price is <= $1); the strategy additionally enforces the $75
cash-at-risk cap per market. Whole contracts only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from core.portfolio import Portfolio
from core.risk import RiskLimits, RiskManager
from core.types import ONE, ZERO, Market, Order, OrderBook, Outcome, Position, PriceLevel, Side
from strategies.base import Strategy

FLB_RISK_LIMITS = RiskLimits(
    max_notional_per_order=Decimal("25"),
    max_position_per_market=Decimal("75"),
    max_daily_loss=Decimal("75"),
)
Placement = Literal["take", "join", "improve"]
_WHOLE = Decimal("1")


@dataclass(frozen=True, slots=True)
class FlbParameters:
    """Assumptions behind the two paper fades. Every value is a documented guess.

    ``longshot_threshold``: the longshot side must be priced strictly below this.
    ``join_fill_probability``: P(fill) for a resting order that joins the touch;
    scaled by ``own / (own + displayed)`` to approximate queue position.
    ``improve_fill_probability``: P(fill) when the order improves the touch by a
    tick (it is first in queue at a new best price).
    ``adverse_selection_haircut``: fraction of the half-spread assumed lost to
    informed takers; used in the reported expected edge, not in the ledger.
    """

    longshot_threshold: Decimal = Decimal("0.20")
    max_order_notional: Decimal = FLB_RISK_LIMITS.max_notional_per_order
    max_market_notional: Decimal = FLB_RISK_LIMITS.max_position_per_market
    # Total collateral the track may tie up; defaults to the paper starting cash so
    # the ledger never simulates borrowing (Kalshi requires full collateral).
    max_total_cash_at_risk: Decimal = Decimal("1000")
    minimum_touch_size: Decimal = Decimal("1")
    tick: Decimal = Decimal("0.01")
    join_fill_probability: Decimal = Decimal("0.25")
    improve_fill_probability: Decimal = Decimal("0.50")
    adverse_selection_haircut: Decimal = Decimal("0.25")

    def __post_init__(self) -> None:
        if not ZERO < self.longshot_threshold < Decimal("0.5"):
            raise ValueError("longshot_threshold must be in (0, 0.5)")
        if self.max_order_notional <= ZERO or self.max_market_notional <= ZERO or self.max_total_cash_at_risk <= ZERO:
            raise ValueError("notional caps must be positive")
        if self.tick <= ZERO or self.minimum_touch_size <= ZERO:
            raise ValueError("tick and minimum_touch_size must be positive")
        for name in ("join_fill_probability", "improve_fill_probability", "adverse_selection_haircut"):
            if not ZERO <= getattr(self, name) <= ONE:
                raise ValueError(f"{name} must be a probability")


@dataclass(frozen=True, slots=True)
class FlbEvaluation:
    venue: str
    market_id: str
    reason: str
    longshot_outcome: Outcome | None = None
    longshot_price: Decimal | None = None
    mid: Decimal | None = None
    spread: Decimal | None = None
    placement: Placement | None = None
    quote_yes_price: Decimal | None = None
    fill_probability: Decimal | None = None
    expected_edge_vs_mid: Decimal | None = None
    expected_edge_after_adverse_selection: Decimal | None = None
    quantity: Decimal = ZERO
    orders: tuple[Order, ...] = ()

    @property
    def traded(self) -> bool:
        return bool(self.orders)


def position_cash_at_risk(position: Position | None) -> Decimal:
    """Collateral tied up by a net-YES position: longs pay ``p``, shorts pay ``1 - p``."""
    if position is None or position.quantity == ZERO:
        return ZERO
    unit = position.average_price if position.quantity > ZERO else ONE - position.average_price
    return abs(position.quantity) * unit


def portfolio_cash_at_risk(portfolio: Portfolio | None) -> Decimal:
    if portfolio is None:
        return ZERO
    return sum((position_cash_at_risk(p) for p in portfolio.positions()), ZERO)


def identify_longshot(book: OrderBook, threshold: Decimal) -> tuple[Outcome, Decimal] | None:
    """Return (longshot outcome, its price) or ``None`` when neither side qualifies.

    Strictly below ``threshold`` so that, with the default 0.20, the longshot set
    is exactly the ``<10c`` and ``10-20c`` price bands.
    """
    ask, bid = book.best_ask, book.best_bid
    if ask is not None and ask.price < threshold:
        return Outcome.YES, ask.price
    if bid is not None and bid.price > ONE - threshold:
        return Outcome.NO, ONE - bid.price
    return None


def whole_contracts(quantity: Decimal) -> Decimal:
    return quantity.quantize(_WHOLE, rounding=ROUND_DOWN)


def size_order(
    *,
    price: Decimal,
    touch_size: Decimal,
    params: FlbParameters,
    position: Position | None,
    risk: RiskManager | None,
    probe: Order,
    total_cash_at_risk: Decimal = ZERO,
) -> Decimal:
    """Whole contracts allowed by touch depth, per-order/market/total caps, and risk rails."""
    headroom = params.max_market_notional - position_cash_at_risk(position)
    capital = params.max_total_cash_at_risk - total_cash_at_risk
    if headroom <= ZERO or capital <= ZERO:
        return ZERO
    quantity = min(touch_size, params.max_order_notional / price, headroom / price, capital / price)
    if risk is not None:
        quantity = min(quantity, risk.remaining_order_capacity(probe, position))
    return max(ZERO, whole_contracts(quantity))


def _probe(market: Market, side: Side, outcome: Outcome, price: Decimal) -> Order:
    return Order(venue=market.venue, market_id=market.market_id, side=side, quantity=_WHOLE, outcome=outcome, price=price)


def _base(market: Market, book: OrderBook) -> dict[str, object]:
    return {"venue": market.venue.value, "market_id": market.market_id, "mid": book.mid_price, "spread": book.spread}


class _FlbStrategy(Strategy):
    def __init__(
        self,
        parameters: FlbParameters | None = None,
        *,
        portfolio: Portfolio | None = None,
        risk: RiskManager | None = None,
    ) -> None:
        self.parameters = parameters or FlbParameters()
        self.portfolio = portfolio
        self.risk = risk

    def _position(self, market: Market) -> Position | None:
        return self.portfolio.get(market.venue, market.market_id) if self.portfolio else None

    def _reserved_collateral(self) -> Decimal:
        return ZERO

    def _size(self, *, price: Decimal, touch_size: Decimal, position: Position | None, probe: Order) -> tuple[Decimal, str]:
        """(quantity, refusal reason). Reason is empty when quantity is positive."""
        total = portfolio_cash_at_risk(self.portfolio) + self._reserved_collateral()
        if self.parameters.max_market_notional - position_cash_at_risk(position) < price:
            return ZERO, "market_cap_reached"
        if self.parameters.max_total_cash_at_risk - total < price:
            return ZERO, "capital_cap_reached"
        quantity = size_order(price=price, touch_size=touch_size, params=self.parameters, position=position, risk=self.risk, probe=probe, total_cash_at_risk=total)
        if quantity <= ZERO:
            if self.risk is not None and self.risk.halted:
                return ZERO, "risk_halted"
            if self.risk is not None and self.risk.remaining_order_capacity(probe, position) < _WHOLE:
                return ZERO, "risk_position_cap_reached"
            return ZERO, "no_position_headroom"
        return quantity, ""

    def evaluate(self, market: Market, book: OrderBook) -> FlbEvaluation:  # pragma: no cover - abstract
        raise NotImplementedError

    async def propose(self, market: Market, book: OrderBook) -> list[Order]:
        return list(self.evaluate(market, book).orders)


class LongshotFadeStrategy(_FlbStrategy):
    """Taker fade: buy the favourite side at its ask when the other side is a longshot."""

    name = "kalshi_longshot_fade"

    def evaluate(self, market: Market, book: OrderBook) -> FlbEvaluation:
        params = self.parameters
        base = _base(market, book)
        if not market.active:
            return FlbEvaluation(reason="market_inactive", **base)  # type: ignore[arg-type]
        if book.best_ask is None and book.best_bid is None:
            return FlbEvaluation(reason="empty_book", **base)  # type: ignore[arg-type]
        found = identify_longshot(book, params.longshot_threshold)
        if found is None:
            return FlbEvaluation(reason="not_longshot", **base)  # type: ignore[arg-type]
        longshot, longshot_price = found
        common = {**base, "longshot_outcome": longshot, "longshot_price": longshot_price}

        # Fading a YES longshot means buying NO at the NO ask (= 1 - YES bid);
        # fading a NO longshot means buying YES at the YES ask.
        if longshot is Outcome.YES:
            if book.best_bid is None:
                return FlbEvaluation(reason="one_sided_book", **common)  # type: ignore[arg-type]
            touch: PriceLevel = book.best_bid
            outcome, price, yes_price = Outcome.NO, ONE - touch.price, touch.price
        else:
            if book.best_ask is None:
                return FlbEvaluation(reason="one_sided_book", **common)  # type: ignore[arg-type]
            touch = book.best_ask
            outcome, price, yes_price = Outcome.YES, touch.price, touch.price
        if not ZERO < price < ONE:
            return FlbEvaluation(reason="touch_at_bound", **common)  # type: ignore[arg-type]
        if touch.size < params.minimum_touch_size:
            return FlbEvaluation(reason="insufficient_touch_depth", **common)  # type: ignore[arg-type]
        mid = book.mid_price
        half_spread = (book.spread or ZERO) / 2
        # Taking pays the half-spread relative to mid regardless of direction.
        edge = -half_spread if mid is not None else None
        common.update({"placement": "take", "quote_yes_price": yes_price, "fill_probability": ONE, "expected_edge_vs_mid": edge, "expected_edge_after_adverse_selection": edge})
        position = self._position(market)
        probe = _probe(market, Side.BUY, outcome, price)
        quantity, refusal = self._size(price=price, touch_size=touch.size, position=position, probe=probe)
        if quantity <= ZERO:
            return FlbEvaluation(reason=refusal, **common)  # type: ignore[arg-type]
        order = Order(
            venue=market.venue,
            market_id=market.market_id,
            side=Side.BUY,
            quantity=quantity,
            outcome=outcome,
            price=price,
            metadata={
                "strategy": self.name,
                "execution": "taker",
                "placement": "take",
                "longshot_outcome": longshot.value,
                "longshot_price": str(longshot_price),
                "fill_probability": "1",
            },
        )
        return FlbEvaluation(reason="trade", quantity=quantity, orders=(order,), **common)  # type: ignore[arg-type]


class MakerQuoteStrategy(_FlbStrategy):
    """Maker fade: rest a favourite-side bid one tick inside the spread, else join the touch.

    Kalshi locks collateral on resting orders, so the unfilled remainder of every
    order proposed in this run counts against ``max_total_cash_at_risk`` via
    :meth:`note_resting` (called by the track runner after each submission).
    """

    name = "kalshi_maker_quote"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.resting_collateral = ZERO

    def _reserved_collateral(self) -> Decimal:
        return self.resting_collateral

    def note_resting(self, order: Order, filled_quantity: Decimal) -> None:
        unfilled = max(ZERO, order.quantity - filled_quantity)
        self.resting_collateral += unfilled * (order.price or ONE)

    def evaluate(self, market: Market, book: OrderBook) -> FlbEvaluation:
        params = self.parameters
        base = _base(market, book)
        if not market.active:
            return FlbEvaluation(reason="market_inactive", **base)  # type: ignore[arg-type]
        if book.best_ask is None and book.best_bid is None:
            return FlbEvaluation(reason="empty_book", **base)  # type: ignore[arg-type]
        found = identify_longshot(book, params.longshot_threshold)
        if found is None:
            return FlbEvaluation(reason="not_longshot", **base)  # type: ignore[arg-type]
        longshot, longshot_price = found
        common = {**base, "longshot_outcome": longshot, "longshot_price": longshot_price}
        ask, bid = book.best_ask, book.best_bid
        if ask is None or bid is None:
            return FlbEvaluation(reason="one_sided_book", **common)  # type: ignore[arg-type]
        spread, mid = ask.price - bid.price, (ask.price + bid.price) / 2
        improve = spread >= 2 * params.tick
        # Rest on the favourite side. YES longshot -> we want to sell YES / buy NO:
        # improving means a YES ask one tick below the current ask (a NO bid one
        # tick above the current NO bid); joining means sitting at the YES ask.
        if longshot is Outcome.YES:
            yes_price = ask.price - params.tick if improve else ask.price
            outcome, price, displayed = Outcome.NO, ONE - yes_price, ask.size
        else:
            yes_price = bid.price + params.tick if improve else bid.price
            outcome, price, displayed = Outcome.YES, yes_price, bid.size
        if not ZERO < price < ONE:
            return FlbEvaluation(reason="touch_at_bound", **common)  # type: ignore[arg-type]
        placement: Placement = "improve" if improve else "join"
        position = self._position(market)
        probe = _probe(market, Side.BUY, outcome, price)
        # Depth is not a constraint for a resting order; size by caps only.
        quantity, refusal = self._size(price=price, touch_size=Decimal("1000000"), position=position, probe=probe)
        if quantity <= ZERO:
            return FlbEvaluation(reason=refusal, **common)  # type: ignore[arg-type]
        if improve:
            fill_probability = params.improve_fill_probability
        else:
            fill_probability = params.join_fill_probability * (quantity / (quantity + displayed) if displayed > ZERO else ONE)
        fill_probability = fill_probability.quantize(Decimal("0.0001"))
        # Selling YES at yes_price when fair is mid earns yes_price - mid for a YES
        # longshot; buying YES at yes_price earns mid - yes_price for a NO longshot.
        gross = (yes_price - mid) if longshot is Outcome.YES else (mid - yes_price)
        adverse = params.adverse_selection_haircut * spread / 2
        common.update({
            "placement": placement,
            "quote_yes_price": yes_price,
            "fill_probability": fill_probability,
            "expected_edge_vs_mid": gross,
            "expected_edge_after_adverse_selection": gross - adverse,
        })
        order = Order(
            venue=market.venue,
            market_id=market.market_id,
            side=Side.BUY,
            quantity=quantity,
            outcome=outcome,
            price=price,
            metadata={
                "strategy": self.name,
                "execution": "maker",
                "placement": placement,
                "longshot_outcome": longshot.value,
                "longshot_price": str(longshot_price),
                "fill_probability": str(fill_probability),
                "displayed_at_touch": str(displayed),
            },
        )
        return FlbEvaluation(reason="trade", quantity=quantity, orders=(order,), **common)  # type: ignore[arg-type]


def longshot_buy_order(
    market: Market,
    book: OrderBook,
    params: FlbParameters,
    *,
    position: Position | None = None,
    total_cash_at_risk: Decimal = ZERO,
) -> Order | None:
    """The literature's losing trade: take the longshot at its ask, same caps, no risk gate."""
    found = identify_longshot(book, params.longshot_threshold)
    if found is None or not market.active:
        return None
    longshot, _ = found
    if longshot is Outcome.YES:
        touch = book.best_ask
        outcome, price = Outcome.YES, touch.price if touch else None
    else:
        touch = book.best_bid
        outcome, price = Outcome.NO, (ONE - touch.price) if touch else None
    if touch is None or price is None or not ZERO < price < ONE or touch.size < params.minimum_touch_size:
        return None
    probe = _probe(market, Side.BUY, outcome, price)
    quantity = size_order(price=price, touch_size=touch.size, params=params, position=position, risk=None, probe=probe, total_cash_at_risk=total_cash_at_risk)
    if quantity <= ZERO:
        return None
    return Order(
        venue=market.venue,
        market_id=market.market_id,
        side=Side.BUY,
        quantity=quantity,
        outcome=outcome,
        price=price,
        metadata={"strategy": "shadow_longshot_buyer", "execution": "taker", "placement": "take"},
    )
