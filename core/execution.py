"""Single risk-gated route through which strategies submit orders."""

from __future__ import annotations

from core.ledger import PaperLedger
from core.observability import EventSink, Metrics, NullMetrics
from core.portfolio import Portfolio
from core.risk import RiskManager, RiskViolation
from core.types import ExecutionReport, Order, ZERO
from core.venue import VenueClient


class LiveTradingDisabled(PermissionError):
    """Raised if a non-paper client is used without both live-mode gates."""


class ExecutionEngine:
    def __init__(
        self,
        *,
        risk: RiskManager,
        portfolio: Portfolio | None = None,
        events: EventSink,
        metrics: Metrics | None = None,
        live_enabled: bool = False,
        ledger: PaperLedger | None = None,
    ) -> None:
        if ledger is not None and portfolio is not None and ledger.portfolio is not portfolio:
            raise ValueError("ledger and portfolio must share the same position book")
        if ledger is None and portfolio is None:
            portfolio = Portfolio()
        self.risk = risk
        self.ledger = ledger
        self.portfolio = ledger.portfolio if ledger is not None else portfolio  # type: ignore[assignment]
        self.events = events
        self.metrics = metrics or NullMetrics()
        self.live_enabled = live_enabled

    async def submit(self, client: VenueClient, order: Order) -> ExecutionReport:
        tags = {"venue": client.venue.value}
        try:
            if order.venue is not client.venue:
                raise ValueError(
                    f"order venue {order.venue} does not match client {client.venue}"
                )
            if not client.paper and not self.live_enabled:
                raise LiveTradingDisabled(
                    "live client blocked: set TRADING_MODE=live and "
                    "ENABLE_LIVE_TRADING=true"
                )
            position = self.portfolio.get(order.venue, order.market_id)
            self.risk.validate_order(order, position)
        except (RiskViolation, LiveTradingDisabled, ValueError) as exc:
            self.metrics.increment("orders.rejected", **tags)
            self.events.emit(
                "order_rejected",
                venue=order.venue,
                client_order_id=order.client_order_id,
                reason=str(exc),
            )
            raise

        self.events.emit("order_submitted", order=order, paper=client.paper)
        self.metrics.increment("orders.submitted", **tags)
        report = await client.place_order(order)

        for fill in report.fills:
            previous = self.portfolio.get(fill.venue, fill.market_id)
            old_realized = previous.realized_pnl if previous else ZERO
            if self.ledger is not None:
                current = self.ledger.record_fill(fill)
            else:
                current = self.portfolio.apply_fill(fill)
            self.risk.record_realized_pnl(current.realized_pnl - old_realized)
            self.events.emit("fill", fill=fill, paper=client.paper)
            self.metrics.increment("fills.total", **tags)

        self.events.emit("order_acknowledged", order=report.order, paper=client.paper)
        return report
