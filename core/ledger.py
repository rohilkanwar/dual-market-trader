"""Paper PnL ledger: cash, marks, realized/unrealized PnL, equity curve, drawdown.

Accounting model
----------------
Everything is expressed in YES-equivalent terms. A fill with signed quantity
``d`` (positive = long YES, negative = long NO / short YES) at YES-equivalent
price ``p`` moves cash by ``-d * p - fee``. A position of ``q`` contracts marked
at YES price ``m`` is worth ``q * m``. Because a short YES position carries a
negative value, ``equity = cash + sum(q * m)`` holds for both signs without a
separate collateral account:

* buy 10 NO at 0.40 == sell 10 YES at 0.60: cash ``+6``, value ``-10 * m``.
  If YES settles at 0 the position is worth 0 and equity is ``+6`` (paid 4,
  received 10); if YES settles at 1 equity is ``-4``.

Realized PnL comes from :class:`core.portfolio.Portfolio` (average-cost method,
fees deducted). Unrealized PnL is ``q * (m - average_price)``. The identity

    equity == starting_cash + realized_pnl + unrealized_pnl

is asserted by the tests and is the main internal consistency check.

Marks default to the book mid. Positions without any mark are valued at their
average price (zero unrealized) and counted in ``unmarked_positions`` so the
gap is visible rather than silently flattering the curve.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from core.portfolio import Portfolio
from core.types import ONE, ZERO, Fill, Order, OrderBook, Outcome, Position, Side, Venue

MarkMethod = Literal["mid", "conservative"]
SETTLEMENT_ORDER_ID = "settlement"


def _now() -> datetime:
    return datetime.now(UTC)


def _q(value: Decimal, places: str = "0.00001") -> Decimal:
    # 5 dp: Polymarket fees are published to 5 decimals, so summaries stay
    # exact and cross-ledger sums in the scoreboard do not drift by rounding.
    return value.quantize(Decimal(places))


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: str
    label: str
    cash: Decimal
    position_value: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    fees_paid: Decimal
    gross_notional: Decimal
    net_exposure: Decimal
    open_positions: int
    unmarked_positions: int
    drawdown: Decimal
    peak_equity: Decimal
    fills: int


class PaperLedger:
    def __init__(
        self,
        *,
        starting_cash: Decimal = Decimal("1000"),
        portfolio: Portfolio | None = None,
        ledger_id: str | None = None,
        mark_method: MarkMethod = "mid",
    ) -> None:
        if starting_cash < ZERO:
            raise ValueError("starting_cash must not be negative")
        self.ledger_id = ledger_id or str(uuid4())
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.portfolio = portfolio or Portfolio()
        self.mark_method: MarkMethod = mark_method
        self.marks: dict[tuple[Venue, str], Decimal] = {}
        self.fills: list[Fill] = []
        self.equity_curve: list[EquityPoint] = []
        self.peak_equity = starting_cash
        self.max_drawdown = ZERO
        self.created_at = _now().isoformat()
        self.updated_at = self.created_at

    # ------------------------------------------------------------------ fills
    def record_fill(self, fill: Fill) -> Position:
        position = self.portfolio.apply_fill(fill)
        self.cash -= fill.signed_quantity * fill.yes_equivalent_price + fill.fee
        self.fills.append(fill)
        self.marks.setdefault((fill.venue, fill.market_id), fill.yes_equivalent_price)
        self.updated_at = fill.timestamp.isoformat()
        return position

    def record_fills(self, fills: list[Fill] | tuple[Fill, ...]) -> None:
        for fill in fills:
            self.record_fill(fill)

    # ------------------------------------------------------------------ marks
    def mark(self, venue: Venue, market_id: str, yes_price: Decimal) -> None:
        if not ZERO <= yes_price <= ONE:
            raise ValueError(f"mark {yes_price} must be between 0 and 1")
        self.marks[(venue, market_id)] = yes_price

    def mark_from_book(
        self,
        venue: Venue,
        market_id: str,
        book: OrderBook,
        *,
        method: MarkMethod | None = None,
    ) -> Decimal | None:
        """Mark from a YES book. Returns the mark used, or ``None`` if unusable.

        ``mid`` uses the midpoint. ``conservative`` marks longs at the best bid
        and shorts at the best ask (the price you could actually exit at).
        """
        method = method or self.mark_method
        position = self.portfolio.get(venue, market_id)
        bid = book.best_bid.price if book.best_bid else None
        ask = book.best_ask.price if book.best_ask else None
        price: Decimal | None
        if method == "conservative" and position is not None and position.quantity != ZERO:
            price = bid if position.quantity > ZERO else ask
        else:
            price = book.mid_price
        if price is None:
            price = bid if bid is not None else ask
        if price is None:
            return None
        self.mark(venue, market_id, price)
        return price

    def mark_of(self, position: Position) -> Decimal | None:
        return self.marks.get((position.venue, position.market_id))

    # ------------------------------------------------------------- settlement
    def close_position(
        self,
        venue: Venue,
        market_id: str,
        *,
        yes_price: Decimal,
        quantity: Decimal | None = None,
        order_id: str,
        fee: Decimal = ZERO,
    ) -> Fill | None:
        """Close ``quantity`` (default: all) of a position at a YES-equivalent price.

        Used for settlement and for off-book primitives such as the Polymarket
        NegRisk NO->collateral conversion, where the ``order_id`` records the
        mechanism. Returns ``None`` when there is nothing to close.
        """
        if not ZERO <= yes_price <= ONE:
            raise ValueError(f"close price {yes_price} must be between 0 and 1")
        position = self.portfolio.get(venue, market_id)
        if position is None or position.quantity == ZERO:
            return None
        closing = abs(position.quantity) if quantity is None else min(abs(position.quantity), quantity)
        if closing <= ZERO:
            return None
        fill = Fill(
            venue=venue,
            market_id=market_id,
            order_id=order_id,
            side=Side.SELL if position.quantity > ZERO else Side.BUY,
            quantity=closing,
            price=yes_price,
            outcome=Outcome.YES,
            fee=fee,
        )
        self.record_fill(fill)
        return fill

    def settle(self, venue: Venue, market_id: str, outcome: Outcome) -> Fill | None:
        """Close an open position at the settlement price (YES=1, NO=0)."""
        settle_price = ONE if outcome is Outcome.YES else ZERO
        fill = self.close_position(
            venue, market_id, yes_price=settle_price, order_id=SETTLEMENT_ORDER_ID
        )
        self.mark(venue, market_id, settle_price)
        return fill

    # ------------------------------------------------------------- valuation
    def position_value(self, position: Position) -> Decimal:
        mark = self.mark_of(position)
        if mark is None:
            mark = position.average_price
        return position.quantity * mark

    def unrealized_pnl_of(self, position: Position) -> Decimal:
        mark = self.mark_of(position)
        if mark is None or position.quantity == ZERO:
            return ZERO
        return position.quantity * (mark - position.average_price)

    @property
    def open_positions(self) -> list[Position]:
        return self.portfolio.positions()

    @property
    def realized_pnl(self) -> Decimal:
        return self.portfolio.realized_pnl

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum((self.unrealized_pnl_of(p) for p in self.open_positions), ZERO)

    @property
    def total_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def fees_paid(self) -> Decimal:
        return self.portfolio.fees_paid

    @property
    def position_value_total(self) -> Decimal:
        return sum((self.position_value(p) for p in self.open_positions), ZERO)

    @property
    def equity(self) -> Decimal:
        return self.cash + self.position_value_total

    @property
    def gross_notional(self) -> Decimal:
        """Sum of |q| * mark across open positions (exposure at current marks)."""
        return sum((abs(self.position_value(p)) for p in self.open_positions), ZERO)

    @property
    def net_exposure(self) -> Decimal:
        return self.position_value_total

    @property
    def unmarked_positions(self) -> int:
        return sum(1 for p in self.open_positions if self.mark_of(p) is None)

    # ------------------------------------------------------------- snapshots
    def snapshot(self, *, timestamp: datetime | None = None, label: str = "") -> EquityPoint:
        equity = self.equity
        if equity > self.peak_equity:
            self.peak_equity = equity
        drawdown = self.peak_equity - equity
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown
        point = EquityPoint(
            timestamp=(timestamp or _now()).isoformat(),
            label=label,
            cash=_q(self.cash),
            position_value=_q(self.position_value_total),
            equity=_q(equity),
            realized_pnl=_q(self.realized_pnl),
            unrealized_pnl=_q(self.unrealized_pnl),
            total_pnl=_q(self.total_pnl),
            fees_paid=_q(self.fees_paid),
            gross_notional=_q(self.gross_notional),
            net_exposure=_q(self.net_exposure),
            open_positions=len(self.open_positions),
            unmarked_positions=self.unmarked_positions,
            drawdown=_q(drawdown),
            peak_equity=_q(self.peak_equity),
            fills=len(self.fills),
        )
        self.equity_curve.append(point)
        self.updated_at = point.timestamp
        return point

    def summary(self) -> dict[str, Any]:
        """Scoreboard-ready portfolio summary. Every number comes from the book."""
        positions = self.open_positions
        by_venue: dict[str, Decimal] = {}
        for position in positions:
            by_venue[position.venue.value] = by_venue.get(position.venue.value, ZERO) + abs(
                self.position_value(position)
            )
        gross = sum(by_venue.values(), ZERO)
        concentration = [
            {"venue": venue, "weight": _q(value / gross) if gross else ZERO}
            for venue, value in sorted(by_venue.items())
        ]
        return {
            "paper_only": True,
            "ledger_id": self.ledger_id,
            "mark_method": self.mark_method,
            "starting_cash": _q(self.starting_cash),
            "cash": _q(self.cash),
            "position_value": _q(self.position_value_total),
            "equity": _q(self.equity),
            "realized_pnl": _q(self.realized_pnl),
            "unrealized_pnl": _q(self.unrealized_pnl),
            "total_pnl": _q(self.total_pnl),
            "fees_paid": _q(self.fees_paid),
            "gross_notional": _q(self.gross_notional),
            "net_exposure": _q(self.net_exposure),
            "peak_equity": _q(self.peak_equity),
            "max_drawdown": _q(self.max_drawdown),
            "return_pct": (
                _q(self.total_pnl / self.starting_cash * 100) if self.starting_cash else ZERO
            ),
            "open_positions": len(positions),
            "unmarked_positions": self.unmarked_positions,
            "fills": len(self.fills),
            "settlement_fills": sum(1 for f in self.fills if f.order_id == SETTLEMENT_ORDER_ID),
            "equity_points": len(self.equity_curve),
            "concentration": concentration,
            "positions": [
                {
                    "venue": p.venue.value,
                    "market_id": p.market_id,
                    "quantity": _q(p.quantity),
                    "average_price": _q(p.average_price),
                    "mark": (_q(m) if (m := self.mark_of(p)) is not None else None),
                    "value": _q(self.position_value(p)),
                    "unrealized_pnl": _q(self.unrealized_pnl_of(p)),
                    "realized_pnl": _q(p.realized_pnl),
                    "fees_paid": _q(p.fees_paid),
                }
                for p in positions
            ],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "paper_only": True,
            "ledger_id": self.ledger_id,
            "mark_method": self.mark_method,
            "starting_cash": str(self.starting_cash),
            "cash": str(self.cash),
            "peak_equity": str(self.peak_equity),
            "max_drawdown": str(self.max_drawdown),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "marks": [
                {"venue": venue.value, "market_id": market_id, "yes_price": str(price)}
                for (venue, market_id), price in sorted(self.marks.items())
            ],
            "positions": [
                {
                    "venue": p.venue.value,
                    "market_id": p.market_id,
                    "quantity": str(p.quantity),
                    "average_price": str(p.average_price),
                    "realized_pnl": str(p.realized_pnl),
                    "fees_paid": str(p.fees_paid),
                }
                for p in self.portfolio.positions(include_flat=True)
            ],
            "fills": [
                {
                    "venue": f.venue.value,
                    "market_id": f.market_id,
                    "order_id": f.order_id,
                    "side": f.side.value,
                    "outcome": f.outcome.value,
                    "quantity": str(f.quantity),
                    "price": str(f.price),
                    "fee": str(f.fee),
                    "timestamp": f.timestamp.isoformat(),
                }
                for f in self.fills
            ],
            "equity_curve": [
                {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(pt).items()}
                for pt in self.equity_curve
            ],
            "summary": self.summary(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PaperLedger:
        if payload.get("paper_only") is not True:
            raise ValueError("refusing to load a ledger that is not marked paper_only")
        ledger = cls(
            starting_cash=Decimal(payload["starting_cash"]),
            ledger_id=payload.get("ledger_id"),
            mark_method=payload.get("mark_method", "mid"),
        )
        ledger.cash = Decimal(payload["cash"])
        ledger.peak_equity = Decimal(payload.get("peak_equity", payload["starting_cash"]))
        ledger.max_drawdown = Decimal(payload.get("max_drawdown", "0"))
        ledger.created_at = payload.get("created_at", ledger.created_at)
        ledger.updated_at = payload.get("updated_at", ledger.updated_at)
        for item in payload.get("marks", []):
            ledger.marks[(Venue(item["venue"]), item["market_id"])] = Decimal(item["yes_price"])
        for item in payload.get("positions", []):
            position = Position(
                venue=Venue(item["venue"]),
                market_id=item["market_id"],
                quantity=Decimal(item["quantity"]),
                average_price=Decimal(item["average_price"]),
                realized_pnl=Decimal(item["realized_pnl"]),
                fees_paid=Decimal(item.get("fees_paid", "0")),
            )
            ledger.portfolio._positions[(position.venue, position.market_id)] = position
        for item in payload.get("fills", []):
            ledger.fills.append(
                Fill(
                    venue=Venue(item["venue"]),
                    market_id=item["market_id"],
                    order_id=item["order_id"],
                    side=Side(item["side"]),
                    outcome=Outcome(item["outcome"]),
                    quantity=Decimal(item["quantity"]),
                    price=Decimal(item["price"]),
                    fee=Decimal(item.get("fee", "0")),
                    timestamp=datetime.fromisoformat(item["timestamp"]),
                )
            )
        for item in payload.get("equity_curve", []):
            ledger.equity_curve.append(
                EquityPoint(
                    **{
                        k: (
                            Decimal(v)
                            if k
                            not in {
                                "timestamp",
                                "label",
                                "open_positions",
                                "unmarked_positions",
                                "fills",
                            }
                            else v
                        )
                        for k, v in item.items()
                    }
                )
            )
        return ledger

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def load(cls, path: Path) -> PaperLedger:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def load_or_create(cls, path: Path, **kwargs: Any) -> PaperLedger:
        if path.exists():
            return cls.load(path)
        return cls(**kwargs)


def expected_fill_cash_flow(order: Order, quantity: Decimal, price: Decimal) -> Decimal:
    """Cash change if ``quantity`` of ``order`` fills at ``price`` (fee excluded)."""
    signed = quantity if order.signed_quantity > ZERO else -quantity
    yes_price = price if order.outcome is Outcome.YES else ONE - price
    return -signed * yes_price
