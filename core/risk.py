"""Hard pre-trade limits. Every order passes through ``validate_order`` first."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.types import ZERO, Order, Position


class RiskViolation(ValueError):
    """Raised when an order would breach a hard limit."""


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_notional_per_order: Decimal
    max_position_per_market: Decimal
    max_daily_loss: Decimal

    def __post_init__(self) -> None:
        for name in ("max_notional_per_order", "max_position_per_market", "max_daily_loss"):
            if getattr(self, name) <= ZERO:
                raise ValueError(f"{name} must be positive")


class RiskManager:
    def __init__(self, limits: RiskLimits, *, kill_switch: bool = False) -> None:
        self.limits = limits
        self.kill_switch = kill_switch
        self.daily_realized_pnl = ZERO

    def record_realized_pnl(self, pnl: Decimal) -> None:
        self.daily_realized_pnl += pnl

    @property
    def halted(self) -> bool:
        return self.kill_switch or self.daily_realized_pnl <= -self.limits.max_daily_loss

    def remaining_order_capacity(
        self,
        order: Order,
        position: Position | None = None,
    ) -> Decimal:
        """Maximum additional contracts allowed by current hard limits."""
        if self.kill_switch or self.daily_realized_pnl <= -self.limits.max_daily_loss:
            return ZERO

        current_quantity = position.quantity if position else ZERO
        direction = Decimal("1") if order.signed_quantity > ZERO else Decimal("-1")
        if direction > ZERO:
            position_capacity = self.limits.max_position_per_market - current_quantity
        else:
            position_capacity = self.limits.max_position_per_market + current_quantity
        unit_notional = order.price if order.price is not None else Decimal("1")
        notional_capacity = self.limits.max_notional_per_order / unit_notional
        return max(ZERO, min(position_capacity, notional_capacity))

    def validate_order(self, order: Order, position: Position | None = None) -> None:
        violations: list[str] = []
        current_quantity = position.quantity if position else ZERO

        if self.kill_switch:
            violations.append("kill switch engaged")
        if self.daily_realized_pnl <= -self.limits.max_daily_loss:
            violations.append(
                f"daily loss limit reached ({self.daily_realized_pnl} <= "
                f"-{self.limits.max_daily_loss})"
            )
        if order.notional > self.limits.max_notional_per_order:
            violations.append(
                f"order notional {order.notional} exceeds "
                f"{self.limits.max_notional_per_order}"
            )
        projected = current_quantity + order.signed_quantity
        reduces_exposure = abs(projected) < abs(current_quantity)
        if abs(projected) > self.limits.max_position_per_market and not reduces_exposure:
            violations.append(
                f"projected position {projected} exceeds "
                f"{self.limits.max_position_per_market}"
            )
        if violations:
            raise RiskViolation("; ".join(violations))
