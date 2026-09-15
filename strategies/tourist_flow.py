"""Fade-the-tourist: classify recreational-looking taker flow on the public
Kalshi trade tape and paper-fade the side it piles into.

Hypothesis under test (paper only): on short-horizon sports (tennis match
winners) and, optionally, 15-minute crypto up/down markets, a slice of taker
flow looks *recreational* — small tickets, longshot buys, and buying a side
after it has already run up (a late chase). If that flow is uninformed, the
side it clusters into is over-bought and the opposite side is cheap, so being
its counterparty earns a positive expected value after fees. The claim is
stronger when the faded side's complement (the favourite we buy) is already
priced at or above 70c.

The **adverse-selection failure mode** is the mirror image: on in-play tennis
the fastest small-clip takers are often court-siders with a faster score feed,
and 15-minute crypto takers may be reacting to a spot move the book has not yet
absorbed. Flow that *looks* recreational can therefore be informed, in which
case the fade is systematically on the wrong side. The research module
measures this with post-trade markouts; this module only encodes the rules.

Everything here is a rule with explicit parameters (:class:`TouristParameters`)
so the report can print exactly what was assumed. Nothing is fitted.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Literal, Sequence

from core.portfolio import Portfolio
from core.risk import RiskLimits, RiskManager
from core.types import ONE, ZERO, Market, Order, OrderBook, Outcome, Position, Side
from strategies.base import Strategy

TOURIST_RISK_LIMITS = RiskLimits(
    max_notional_per_order=Decimal("25"),
    max_position_per_market=Decimal("75"),
    max_daily_loss=Decimal("75"),
)
_WHOLE = Decimal("1")
Q4 = Decimal("0.0001")
Regime = Literal["strong", "weak"]


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TouristParameters:
    """Every threshold behind the classifier, the cluster trigger and the fade.

    Classifier (per taker trade; a trade is *tourist* when at least
    ``min_flags`` of the three flags are set):

    * ``small_ticket_notional`` — the taker paid at most this many dollars
      (contracts x price of the side bought).
    * ``longshot_threshold`` — the side bought was priced strictly below this.
    * ``chase_move`` / ``chase_lookback_trades`` — the bought side's price is at
      least ``chase_move`` above its price ``chase_lookback_trades`` trades
      earlier (the taker is buying *after* the move, in its direction), and
    * ``late_fraction`` — the trade sits in the last ``1 - late_fraction`` of the
      market's open-to-close life (both conditions are needed for ``late_chase``).

    Cluster trigger (per market and side, rolling ``cluster_window_seconds``):
    at least ``cluster_min_trades`` tourist trades carrying at least
    ``cluster_min_notional`` dollars on one side, then a ``cooldown_seconds``
    silence per side before the next signal.

    Fade: buy the *other* side. ``base_notional`` dollars per fade, multiplied by
    ``strong_multiplier`` when that side is priced at or above
    ``strong_favorite_price``; ``max_market_notional`` cash-at-risk per market and
    ``max_total_cash_at_risk`` in total (equal to the paper starting cash so the
    ledger never borrows). ``assumed_spread_ticks`` is only used when replaying
    a tape that has no book: the fade is assumed to pay the last tourist print's
    complement plus this many ticks (a documented, conservative guess).
    ``signal_max_age_seconds`` bounds how stale a cluster may be for the live
    track to act on it against the current book.
    """

    small_ticket_notional: Decimal = Decimal("10")
    longshot_threshold: Decimal = Decimal("0.30")
    chase_move: Decimal = Decimal("0.05")
    chase_lookback_trades: int = 20
    late_fraction: Decimal = Decimal("0.5")
    min_flags: int = 2
    cluster_window_seconds: int = 600
    cluster_min_trades: int = 5
    cluster_min_notional: Decimal = Decimal("50")
    cooldown_seconds: int = 300
    strong_favorite_price: Decimal = Decimal("0.70")
    base_notional: Decimal = Decimal("10")
    strong_multiplier: Decimal = Decimal("2")
    max_order_notional: Decimal = TOURIST_RISK_LIMITS.max_notional_per_order
    max_market_notional: Decimal = TOURIST_RISK_LIMITS.max_position_per_market
    max_total_cash_at_risk: Decimal = Decimal("1000")
    assumed_spread_ticks: int = 2
    tick: Decimal = Decimal("0.01")
    minimum_touch_size: Decimal = Decimal("1")
    signal_max_age_seconds: int = 900

    def __post_init__(self) -> None:
        if self.small_ticket_notional <= ZERO:
            raise ValueError("small_ticket_notional must be positive")
        if not ZERO < self.longshot_threshold < ONE:
            raise ValueError("longshot_threshold must be in (0, 1)")
        if self.chase_move <= ZERO or self.chase_lookback_trades < 1:
            raise ValueError("chase_move must be positive and chase_lookback_trades >= 1")
        if not ZERO <= self.late_fraction < ONE:
            raise ValueError("late_fraction must be in [0, 1)")
        if not 1 <= self.min_flags <= 3:
            raise ValueError("min_flags must be 1, 2 or 3")
        if self.cluster_window_seconds < 1 or self.cluster_min_trades < 1 or self.cluster_min_notional <= ZERO:
            raise ValueError("cluster thresholds must be positive")
        if self.cooldown_seconds < 0 or self.signal_max_age_seconds < 0:
            raise ValueError("cooldown and signal age must not be negative")
        if not ZERO < self.strong_favorite_price < ONE:
            raise ValueError("strong_favorite_price must be in (0, 1)")
        if self.base_notional <= ZERO or self.strong_multiplier < ONE:
            raise ValueError("base_notional must be positive and strong_multiplier >= 1")
        if self.max_order_notional <= ZERO or self.max_market_notional <= ZERO or self.max_total_cash_at_risk <= ZERO:
            raise ValueError("notional caps must be positive")
        if self.assumed_spread_ticks < 0 or self.tick <= ZERO or self.minimum_touch_size <= ZERO:
            raise ValueError("assumed_spread_ticks must be >= 0; tick and minimum_touch_size positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "small_ticket_notional": self.small_ticket_notional,
            "longshot_threshold": self.longshot_threshold,
            "chase_move": self.chase_move,
            "chase_lookback_trades": self.chase_lookback_trades,
            "late_fraction": self.late_fraction,
            "min_flags": self.min_flags,
            "cluster_window_seconds": self.cluster_window_seconds,
            "cluster_min_trades": self.cluster_min_trades,
            "cluster_min_notional": self.cluster_min_notional,
            "cooldown_seconds": self.cooldown_seconds,
            "strong_favorite_price": self.strong_favorite_price,
            "base_notional": self.base_notional,
            "strong_multiplier": self.strong_multiplier,
            "max_order_notional": self.max_order_notional,
            "max_market_notional": self.max_market_notional,
            "max_total_cash_at_risk": self.max_total_cash_at_risk,
            "assumed_spread_ticks": self.assumed_spread_ticks,
            "tick": self.tick,
            "minimum_touch_size": self.minimum_touch_size,
            "signal_max_age_seconds": self.signal_max_age_seconds,
        }


# --------------------------------------------------------------------------
# Tape
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TapeTrade:
    """One public print: who took, at what YES price, how many contracts."""

    created_time: datetime
    taker_side: Outcome
    yes_price: Decimal
    count: Decimal
    trade_id: str | None = None

    def __post_init__(self) -> None:
        if not ZERO <= self.yes_price <= ONE:
            raise ValueError(f"yes_price {self.yes_price} must be between 0 and 1")
        if self.count <= ZERO:
            raise ValueError("count must be positive")

    @property
    def taker_price(self) -> Decimal:
        """Price paid for the side the taker bought."""
        return self.yes_price if self.taker_side is Outcome.YES else ONE - self.yes_price

    @property
    def taker_notional(self) -> Decimal:
        return self.count * self.taker_price

    def side_price(self, side: Outcome) -> Decimal:
        return self.yes_price if side is Outcome.YES else ONE - self.yes_price


def other_side(side: Outcome) -> Outcome:
    return Outcome.NO if side is Outcome.YES else Outcome.YES


@dataclass(frozen=True, slots=True)
class TouristFlags:
    small_ticket: bool
    longshot_buy: bool
    late_chase: bool
    chase_move: Decimal | None = None
    life_fraction: Decimal | None = None

    @property
    def score(self) -> int:
        return int(self.small_ticket) + int(self.longshot_buy) + int(self.late_chase)

    @property
    def combination(self) -> str:
        """Stable label such as ``small+longshot`` for tables keyed by flag mix."""
        parts = [name for name, on in (("small", self.small_ticket), ("longshot", self.longshot_buy), ("chase", self.late_chase)) if on]
        return "+".join(parts) if parts else "none"

    def is_tourist(self, params: TouristParameters) -> bool:
        return self.score >= params.min_flags


def life_fraction(at: datetime, *, open_time: datetime | None, close_time: datetime | None) -> Decimal | None:
    """Where ``at`` sits in the market's open-to-close life, in [0, 1]; ``None`` if unknown."""
    if open_time is None or close_time is None or close_time <= open_time:
        return None
    total = (close_time - open_time).total_seconds()
    elapsed = (at - open_time).total_seconds()
    fraction = Decimal(str(elapsed / total))
    return max(ZERO, min(ONE, fraction)).quantize(Q4)


def classify_trade(
    trade: TapeTrade,
    history: Sequence[TapeTrade],
    params: TouristParameters,
    *,
    open_time: datetime | None = None,
    close_time: datetime | None = None,
) -> TouristFlags:
    """Flag one taker trade given the chronological prints before it.

    ``history`` must not include ``trade`` itself. The chase reference is the
    bought side's price ``chase_lookback_trades`` prints earlier (the oldest
    available print when the tape is shorter, but at least one print).
    """
    small = trade.taker_notional <= params.small_ticket_notional
    longshot = trade.taker_price < params.longshot_threshold
    move: Decimal | None = None
    if history:
        reference = history[-params.chase_lookback_trades] if len(history) >= params.chase_lookback_trades else history[0]
        move = (trade.taker_price - reference.side_price(trade.taker_side)).quantize(Q4)
    fraction = life_fraction(trade.created_time, open_time=open_time, close_time=close_time)
    chase = move is not None and move >= params.chase_move
    late = fraction is not None and fraction >= params.late_fraction
    return TouristFlags(small_ticket=small, longshot_buy=longshot, late_chase=chase and late, chase_move=move, life_fraction=fraction)


# --------------------------------------------------------------------------
# Cluster detection
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ClusterSignal:
    """Tourist notional clustered on ``side`` within the rolling window."""

    market_id: str
    side: Outcome
    at: datetime
    trades: int
    notional: Decimal
    contracts: Decimal
    last_taker_price: Decimal
    window_start: datetime
    combinations: dict[str, int] = field(default_factory=dict)

    @property
    def fade_side(self) -> Outcome:
        return other_side(self.side)

    @property
    def favorite_price_estimate(self) -> Decimal:
        """Complement of the last tourist print: what the faded side's opponent costs, before spread."""
        return ONE - self.last_taker_price


@dataclass(slots=True)
class _SideWindow:
    trades: deque = field(default_factory=deque)
    last_signal_at: datetime | None = None


class ClusterDetector:
    """Rolling per-side accumulator of tourist prints for one market."""

    def __init__(self, market_id: str, params: TouristParameters) -> None:
        self.market_id = market_id
        self.params = params
        self._sides: dict[Outcome, _SideWindow] = {Outcome.YES: _SideWindow(), Outcome.NO: _SideWindow()}
        self.tourist_trades = 0
        self.signals: list[ClusterSignal] = []

    def observe(self, trade: TapeTrade, flags: TouristFlags) -> ClusterSignal | None:
        window = self._sides[trade.taker_side]
        horizon = trade.created_time - timedelta(seconds=self.params.cluster_window_seconds)
        for side in self._sides.values():
            while side.trades and side.trades[0][0].created_time < horizon:
                side.trades.popleft()
        if not flags.is_tourist(self.params):
            return None
        self.tourist_trades += 1
        window.trades.append((trade, flags))
        notional = sum((t.taker_notional for t, _ in window.trades), ZERO)
        if len(window.trades) < self.params.cluster_min_trades or notional < self.params.cluster_min_notional:
            return None
        if window.last_signal_at is not None and (trade.created_time - window.last_signal_at).total_seconds() < self.params.cooldown_seconds:
            return None
        combinations: dict[str, int] = {}
        for _, f in window.trades:
            combinations[f.combination] = combinations.get(f.combination, 0) + 1
        signal = ClusterSignal(
            market_id=self.market_id,
            side=trade.taker_side,
            at=trade.created_time,
            trades=len(window.trades),
            notional=notional.quantize(Q4),
            contracts=sum((t.count for t, _ in window.trades), ZERO),
            last_taker_price=trade.taker_price,
            window_start=window.trades[0][0].created_time,
            combinations=dict(sorted(combinations.items())),
        )
        window.last_signal_at = trade.created_time
        self.signals.append(signal)
        return signal


# --------------------------------------------------------------------------
# Fade sizing
# --------------------------------------------------------------------------
def whole_contracts(quantity: Decimal) -> Decimal:
    return quantity.quantize(_WHOLE, rounding=ROUND_DOWN)


def position_cash_at_risk(position: Position | None) -> Decimal:
    if position is None or position.quantity == ZERO:
        return ZERO
    unit = position.average_price if position.quantity > ZERO else ONE - position.average_price
    return abs(position.quantity) * unit


def portfolio_cash_at_risk(portfolio: Portfolio | None) -> Decimal:
    if portfolio is None:
        return ZERO
    return sum((position_cash_at_risk(p) for p in portfolio.positions()), ZERO)


def regime_for(favorite_price: Decimal, params: TouristParameters) -> Regime:
    return "strong" if favorite_price >= params.strong_favorite_price else "weak"


def fade_notional(favorite_price: Decimal, params: TouristParameters) -> Decimal:
    multiplier = params.strong_multiplier if regime_for(favorite_price, params) == "strong" else ONE
    return min(params.base_notional * multiplier, params.max_order_notional)


def size_fade(
    *,
    favorite_price: Decimal,
    params: TouristParameters,
    position: Position | None,
    total_cash_at_risk: Decimal,
    risk: RiskManager | None = None,
    probe: Order | None = None,
    touch_size: Decimal | None = None,
) -> tuple[Decimal, str]:
    """(whole contracts, refusal reason); the reason is empty when contracts > 0."""
    if not ZERO < favorite_price < ONE:
        return ZERO, "price_at_bound"
    headroom = params.max_market_notional - position_cash_at_risk(position)
    capital = params.max_total_cash_at_risk - total_cash_at_risk
    if headroom < favorite_price:
        return ZERO, "market_cap_reached"
    if capital < favorite_price:
        return ZERO, "capital_cap_reached"
    quantity = min(fade_notional(favorite_price, params) / favorite_price, headroom / favorite_price, capital / favorite_price)
    if touch_size is not None:
        quantity = min(quantity, touch_size)
    if risk is not None and probe is not None:
        if risk.halted:
            return ZERO, "risk_halted"
        quantity = min(quantity, risk.remaining_order_capacity(probe, position))
    quantity = whole_contracts(quantity)
    if quantity <= ZERO:
        if touch_size is not None and touch_size < _WHOLE:
            return ZERO, "insufficient_touch_depth"
        if risk is not None and probe is not None and risk.remaining_order_capacity(probe, position) < _WHOLE:
            return ZERO, "risk_position_cap_reached"
        return ZERO, "no_position_headroom"
    return quantity, ""


def replay_fade_price(signal: ClusterSignal, params: TouristParameters) -> Decimal:
    """Assumed taker price for the favourite when only the tape is known.

    The last tourist print bought ``signal.side`` at ``p`` by lifting that side's
    ask, so the other side's ask is roughly ``1 - p`` plus the spread. The tape
    carries no book, so ``assumed_spread_ticks`` stands in for it.
    """
    price = signal.favorite_price_estimate + params.tick * params.assumed_spread_ticks
    return min(price, ONE - params.tick)


# --------------------------------------------------------------------------
# Live-book strategy
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FadeEvaluation:
    venue: str
    market_id: str
    reason: str
    signal: ClusterSignal | None = None
    fade_side: Outcome | None = None
    favorite_price: Decimal | None = None
    regime: Regime | None = None
    mid: Decimal | None = None
    spread: Decimal | None = None
    tourist_trades: int = 0
    tape_trades: int = 0
    quantity: Decimal = ZERO
    orders: tuple[Order, ...] = ()

    @property
    def traded(self) -> bool:
        return bool(self.orders)


def detect_clusters(
    market_id: str,
    tape: Sequence[TapeTrade],
    params: TouristParameters,
    *,
    open_time: datetime | None = None,
    close_time: datetime | None = None,
) -> tuple[ClusterDetector, list[tuple[TapeTrade, TouristFlags]]]:
    """Run the classifier and cluster detector over a chronological tape."""
    detector = ClusterDetector(market_id, params)
    ordered = sorted(tape, key=lambda t: t.created_time)
    flagged: list[tuple[TapeTrade, TouristFlags]] = []
    for index, trade in enumerate(ordered):
        flags = classify_trade(trade, ordered[:index], params, open_time=open_time, close_time=close_time)
        flagged.append((trade, flags))
        detector.observe(trade, flags)
    return detector, flagged


class FadeTouristStrategy(Strategy):
    """Buy the favourite at the touch when tourist flow has just clustered on the other side.

    Holds at most one open paper position per market (``already_positioned``)
    so a ledger carried across runs does not re-fade an unchanged cluster.
    ``propose`` (the :class:`Strategy` contract) has no tape, so it never
    trades; callers use :meth:`evaluate` with the market's recent prints.
    """

    name = "fade_the_tourist"

    def __init__(
        self,
        parameters: TouristParameters | None = None,
        *,
        portfolio: Portfolio | None = None,
        risk: RiskManager | None = None,
    ) -> None:
        self.parameters = parameters or TouristParameters()
        self.portfolio = portfolio
        self.risk = risk

    async def propose(self, market: Market, book: OrderBook) -> list[Order]:
        del market, book
        return []

    def evaluate(
        self,
        market: Market,
        book: OrderBook,
        tape: Sequence[TapeTrade],
        *,
        as_of: datetime | None = None,
        open_time: datetime | None = None,
        close_time: datetime | None = None,
    ) -> FadeEvaluation:
        params = self.parameters
        base = {"venue": market.venue.value, "market_id": market.market_id, "mid": book.mid_price, "spread": book.spread, "tape_trades": len(tape)}
        if not market.active:
            return FadeEvaluation(reason="market_inactive", **base)  # type: ignore[arg-type]
        if not tape:
            return FadeEvaluation(reason="no_tape", **base)  # type: ignore[arg-type]
        if book.best_ask is None and book.best_bid is None:
            return FadeEvaluation(reason="empty_book", **base)  # type: ignore[arg-type]
        detector, _ = detect_clusters(market.market_id, tape, params, open_time=open_time, close_time=close_time)
        base["tourist_trades"] = detector.tourist_trades
        if not detector.signals:
            return FadeEvaluation(reason="no_tourist_cluster", **base)  # type: ignore[arg-type]
        signal = detector.signals[-1]
        reference = as_of or max(t.created_time for t in tape)
        if (reference - signal.at).total_seconds() > params.signal_max_age_seconds:
            return FadeEvaluation(reason="cluster_stale", signal=signal, **base)  # type: ignore[arg-type]
        fade_side = signal.fade_side
        common = {**base, "signal": signal, "fade_side": fade_side}
        view = book if fade_side is Outcome.YES else book.for_outcome(Outcome.NO)
        touch = view.best_ask
        if touch is None:
            return FadeEvaluation(reason="one_sided_book", **common)  # type: ignore[arg-type]
        price = touch.price
        if not ZERO < price < ONE:
            return FadeEvaluation(reason="touch_at_bound", **common)  # type: ignore[arg-type]
        if touch.size < params.minimum_touch_size:
            return FadeEvaluation(reason="insufficient_touch_depth", **common)  # type: ignore[arg-type]
        regime = regime_for(price, params)
        common.update({"favorite_price": price, "regime": regime})
        position = self.portfolio.get(market.venue, market.market_id) if self.portfolio else None
        if position is not None and position.quantity != ZERO:
            # One paper position per market until it settles: a carried ledger must
            # not re-fade the same cluster on every run.
            return FadeEvaluation(reason="already_positioned", **common)  # type: ignore[arg-type]
        probe = Order(venue=market.venue, market_id=market.market_id, side=Side.BUY, quantity=_WHOLE, outcome=fade_side, price=price)
        quantity, refusal = size_fade(
            favorite_price=price,
            params=params,
            position=position,
            total_cash_at_risk=portfolio_cash_at_risk(self.portfolio),
            risk=self.risk,
            probe=probe,
            touch_size=touch.size,
        )
        if quantity <= ZERO:
            return FadeEvaluation(reason=refusal, **common)  # type: ignore[arg-type]
        order = Order(
            venue=market.venue,
            market_id=market.market_id,
            side=Side.BUY,
            quantity=quantity,
            outcome=fade_side,
            price=price,
            metadata={
                "strategy": self.name,
                "execution": "taker",
                "placement": "take",
                "faded_side": signal.side.value,
                "regime": regime,
                "cluster_trades": str(signal.trades),
                "cluster_notional": str(signal.notional),
                "cluster_at": signal.at.isoformat(),
            },
        )
        return FadeEvaluation(reason="trade", quantity=quantity, orders=(order,), **common)  # type: ignore[arg-type]
