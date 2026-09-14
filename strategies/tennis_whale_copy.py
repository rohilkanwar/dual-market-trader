"""Tennis whale copy with lag: who counts as a whale, and how a fill is copied.

Pure decision logic over the public taker tape (Polymarket Data API
``/trades`` rows carry ``proxyWallet``, side, outcome, price, size and a
second-resolution timestamp). The tape replay in :mod:`research.tennis_whale`
feeds prints through here in time order and books the resulting paper orders
through the normal risk-gated engine.

Pre-registered rules (defaults in :class:`CopyParameters`, all written into the
report):

* **Walk-forward qualification.** A wallet is a whale from the first moment its
  *prior* history shows ``min_large_fills`` taker fills of at least
  ``large_fill_notional`` USDC across ``min_markets`` distinct tennis markets.
  Only prints *after* qualification are signals, so whale selection never sees
  the fill it is being scored on.
* **Farmer / maker filter.** A wallet whose two-sided share (markets where it
  took both directions) exceeds ``max_two_sided_share`` is refused
  ``two_sided_flow``: volume farmers and hedgers churn both ways and their
  prints carry no directional information.
* **Signal.** A whale's taker print of at least ``signal_min_notional`` USDC.
* **Copy.** At each lag the copy buys the outcome the whale ended up long, at
  the first print on the same market at or after ``signal_time + lag`` (an
  actual executed price, not a mid), plus ``slippage_ticks`` against us. Size is
  ``stake_per_copy`` USDC worth of contracts, capped by the reference print's
  size (the tape shows no depth; the print is the only evidence liquidity was
  there). One copy per wallet, market and direction per ``cooldown_seconds``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from core.risk import RiskLimits
from core.types import ONE, ZERO, Order, OrderType, Outcome, Side, Venue

LAG_LABELS: dict[int, str] = {30: "30s", 120: "2m", 600: "10m"}
COPY_RISK_LIMITS = RiskLimits(
    max_notional_per_order=Decimal("25"),
    max_position_per_market=Decimal("100"),
    max_daily_loss=Decimal("75"),
)


def lag_label(seconds: int) -> str:
    if seconds in LAG_LABELS:
        return LAG_LABELS[seconds]
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def track_for_lag(seconds: int) -> str:
    return f"tennis_whale_copy_{lag_label(seconds)}"


@dataclass(frozen=True, slots=True)
class TapePrint:
    """One taker fill from the public tape, in the traded outcome's terms."""

    market_id: str
    wallet: str
    side: Side
    outcome: Outcome
    price: Decimal
    size: Decimal
    timestamp: datetime
    tx: str = ""

    def __post_init__(self) -> None:
        if not ZERO <= self.price <= ONE:
            raise ValueError(f"price {self.price} must be between 0 and 1")
        if self.size <= ZERO:
            raise ValueError(f"size {self.size} must be positive")

    @property
    def notional(self) -> Decimal:
        return self.size * self.price

    @property
    def yes_equivalent_price(self) -> Decimal:
        return self.price if self.outcome is Outcome.YES else ONE - self.price

    @property
    def long_outcome(self) -> Outcome:
        """The outcome the taker is long after this print (SELL YES == long NO)."""
        increases_yes = (self.side is Side.BUY) == (self.outcome is Outcome.YES)
        return Outcome.YES if increases_yes else Outcome.NO

    @property
    def signed_quantity(self) -> Decimal:
        return self.size if self.long_outcome is Outcome.YES else -self.size


@dataclass(frozen=True, slots=True)
class CopyParameters:
    lags: tuple[int, ...] = (30, 120, 600)
    large_fill_notional: Decimal = Decimal("500")
    min_large_fills: int = 3
    min_markets: int = 2
    signal_min_notional: Decimal = Decimal("200")
    max_two_sided_share: Decimal = Decimal("0.5")
    stake_per_copy: Decimal = Decimal("10")
    max_wait_seconds: int = 1800
    slippage_ticks: int = 1
    cooldown_seconds: int = 600
    min_copy_price: Decimal = Decimal("0.02")
    max_copy_price: Decimal = Decimal("0.98")

    def __post_init__(self) -> None:
        if not self.lags or any(lag <= 0 for lag in self.lags):
            raise ValueError("lags must be positive seconds")
        if self.min_large_fills < 1 or self.min_markets < 1:
            raise ValueError("min_large_fills and min_markets must be at least 1")
        if self.stake_per_copy <= ZERO:
            raise ValueError("stake_per_copy must be positive")
        if not ZERO < self.min_copy_price < self.max_copy_price < ONE:
            raise ValueError("copy price bounds must satisfy 0 < min < max < 1")

    def as_dict(self) -> dict[str, Any]:
        return {
            "lags_seconds": list(self.lags),
            "large_fill_notional_usdc": self.large_fill_notional,
            "min_large_fills": self.min_large_fills,
            "min_markets": self.min_markets,
            "signal_min_notional_usdc": self.signal_min_notional,
            "max_two_sided_share": self.max_two_sided_share,
            "stake_per_copy_usdc": self.stake_per_copy,
            "max_wait_seconds": self.max_wait_seconds,
            "slippage_ticks": self.slippage_ticks,
            "cooldown_seconds": self.cooldown_seconds,
            "min_copy_price": self.min_copy_price,
            "max_copy_price": self.max_copy_price,
        }


@dataclass(slots=True)
class WalletStats:
    wallet: str
    prints: int = 0
    notional: Decimal = ZERO
    large_fills: int = 0
    large_fill_markets: set[str] = field(default_factory=set)
    markets: set[str] = field(default_factory=set)
    directions: dict[str, set[Outcome]] = field(default_factory=dict)
    qualified_at: datetime | None = None

    @property
    def two_sided_markets(self) -> int:
        return sum(1 for outcomes in self.directions.values() if len(outcomes) > 1)

    @property
    def two_sided_share(self) -> Decimal:
        if not self.markets:
            return ZERO
        return (Decimal(self.two_sided_markets) / Decimal(len(self.markets))).quantize(Decimal("0.0001"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "prints": self.prints,
            "notional_usdc": self.notional.quantize(Decimal("0.01")),
            "large_fills": self.large_fills,
            "large_fill_markets": len(self.large_fill_markets),
            "markets": len(self.markets),
            "two_sided_markets": self.two_sided_markets,
            "two_sided_share": self.two_sided_share,
            "qualified_at": self.qualified_at.isoformat() if self.qualified_at else None,
        }


@dataclass(frozen=True, slots=True)
class SignalDecision:
    print: TapePrint
    signal: bool
    reason: str
    whale: bool
    two_sided_share: Decimal


class WhaleTracker:
    """Walk-forward whale registry. Call :meth:`evaluate` *before* :meth:`observe`."""

    def __init__(self, params: CopyParameters) -> None:
        self.params = params
        self.wallets: dict[str, WalletStats] = {}

    def stats(self, wallet: str) -> WalletStats:
        stats = self.wallets.get(wallet)
        if stats is None:
            stats = self.wallets[wallet] = WalletStats(wallet=wallet)
        return stats

    def is_whale(self, wallet: str) -> bool:
        stats = self.wallets.get(wallet)
        if stats is None:
            return False
        return (
            stats.large_fills >= self.params.min_large_fills
            and len(stats.large_fill_markets) >= self.params.min_markets
        )

    def evaluate(self, tape_print: TapePrint) -> SignalDecision:
        """Is this print a copyable signal given only history strictly before it?"""
        stats = self.wallets.get(tape_print.wallet)
        share = stats.two_sided_share if stats else ZERO
        whale = self.is_whale(tape_print.wallet)
        if not whale:
            return SignalDecision(tape_print, False, "not_whale", False, share)
        if share > self.params.max_two_sided_share:
            return SignalDecision(tape_print, False, "two_sided_flow", True, share)
        if tape_print.notional < self.params.signal_min_notional:
            return SignalDecision(tape_print, False, "below_signal_notional", True, share)
        return SignalDecision(tape_print, True, "signal", True, share)

    def observe(self, tape_print: TapePrint) -> WalletStats:
        stats = self.stats(tape_print.wallet)
        stats.prints += 1
        stats.notional += tape_print.notional
        stats.markets.add(tape_print.market_id)
        stats.directions.setdefault(tape_print.market_id, set()).add(tape_print.long_outcome)
        if tape_print.notional >= self.params.large_fill_notional:
            stats.large_fills += 1
            stats.large_fill_markets.add(tape_print.market_id)
        if stats.qualified_at is None and self.is_whale(tape_print.wallet):
            stats.qualified_at = tape_print.timestamp
        return stats

    def whales(self) -> list[WalletStats]:
        return sorted(
            (s for s in self.wallets.values() if self.is_whale(s.wallet)),
            key=lambda s: (s.notional, s.large_fills),
            reverse=True,
        )


@dataclass(frozen=True, slots=True)
class CopyDecision:
    signal: TapePrint
    lag_seconds: int
    copy: bool
    reason: str
    reference: TapePrint | None = None
    order: Order | None = None
    copy_price: Decimal | None = None
    yes_equivalent_price: Decimal | None = None
    quantity: Decimal | None = None

    @property
    def outcome(self) -> Outcome:
        return self.signal.long_outcome


def whole_contracts(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_DOWN)


def copy_decision(
    signal: TapePrint,
    *,
    lag_seconds: int,
    reference: TapePrint | None,
    params: CopyParameters,
    tick_size: Decimal,
    market_closed_at: datetime | None,
    last_copy_at: datetime | None,
) -> CopyDecision:
    """Turn a whale print plus the first print at/after the lag into a paper order or a refusal."""
    if last_copy_at is not None and signal.timestamp - last_copy_at < timedelta(seconds=params.cooldown_seconds):
        return CopyDecision(signal, lag_seconds, False, "cooldown")
    if reference is None:
        return CopyDecision(signal, lag_seconds, False, "no_print_within_window")
    earliest = signal.timestamp + timedelta(seconds=lag_seconds)
    if reference.timestamp < earliest:
        raise ValueError("reference print predates signal + lag")
    if reference.timestamp - earliest > timedelta(seconds=params.max_wait_seconds):
        return CopyDecision(signal, lag_seconds, False, "no_print_within_window", reference)
    if market_closed_at is not None and reference.timestamp >= market_closed_at:
        return CopyDecision(signal, lag_seconds, False, "market_closed_before_copy", reference)
    outcome = signal.long_outcome
    yes_price = reference.yes_equivalent_price
    outcome_price = yes_price if outcome is Outcome.YES else ONE - yes_price
    copy_price = outcome_price + tick_size * params.slippage_ticks
    if not params.min_copy_price <= copy_price <= params.max_copy_price:
        return CopyDecision(signal, lag_seconds, False, "price_out_of_bounds", reference, copy_price=copy_price)
    quantity = min(whole_contracts(params.stake_per_copy / copy_price), whole_contracts(reference.size))
    if quantity < ONE:
        return CopyDecision(signal, lag_seconds, False, "size_below_one_contract", reference, copy_price=copy_price)
    order = Order(
        venue=Venue.POLYMARKET,
        market_id=signal.market_id,
        side=Side.BUY,
        outcome=outcome,
        quantity=quantity,
        price=copy_price,
        order_type=OrderType.LIMIT,
        metadata={
            "strategy": track_for_lag(lag_seconds),
            "whale": signal.wallet,
            "signal_at": signal.timestamp.isoformat(),
            "copy_at": reference.timestamp.isoformat(),
            "lag_seconds": str(lag_seconds),
        },
    )
    yes_equivalent = copy_price if outcome is Outcome.YES else ONE - copy_price
    return CopyDecision(signal, lag_seconds, True, "copy", reference, order, copy_price, yes_equivalent, quantity)
