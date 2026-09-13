    def record_realized_pnl(self, pnl: Decimal) -> None:
        self.daily_realized_pnl += pnl

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
