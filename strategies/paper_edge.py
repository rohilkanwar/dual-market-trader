"""Fee- and depth-aware paper edge for a settlement-equivalent pair.

Given two YES-normalised books (the cheap venue's and the dear venue's) the
edge of buying YES on the cheap side and NO on the dear side is, per contract,

    gross_edge = dear_bid - cheap_ask = 1 - (cheap_ask + dear_no_ask)

because a NO bought at ``1 - dear_bid`` plus a YES bought at ``cheap_ask`` pay
exactly ``1`` at settlement **if and only if** both contracts settle on the
same event. That fungibility is what ``settlement.gate`` decides; this module
only prices the admitted pair.

The walk consumes both books level by level and stops as soon as the marginal
contract no longer clears ``min_net_edge`` after a conservative per-contract
fee estimate. Fees are then recomputed exactly on the consumed levels using
each venue's schedule (``venues.paper.kalshi_fee`` rounds *up* to the cent per
fill, so small clips pay proportionally more).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from core.types import ONE, ZERO, OrderBook, Outcome, PriceLevel, Venue
from venues.paper import FeeSchedule

Q = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class LegLevel:
    price: Decimal
    quantity: Decimal
    fee: Decimal

    def as_dict(self) -> dict[str, str]:
        return {"price": str(self.price), "quantity": str(self.quantity), "fee": str(self.fee)}


@dataclass(frozen=True, slots=True)
class Leg:
    """One side of the paper trade in the traded outcome's own price terms."""

    venue: Venue
    market_id: str
    outcome: Outcome
    levels: tuple[LegLevel, ...]

    @property
    def quantity(self) -> Decimal:
        return sum((level.quantity for level in self.levels), ZERO)

    @property
    def cost(self) -> Decimal:
        return sum((level.quantity * level.price for level in self.levels), ZERO)

    @property
    def fees(self) -> Decimal:
        return sum((level.fee for level in self.levels), ZERO)

    @property
    def vwap(self) -> Decimal | None:
        return (self.cost / self.quantity).quantize(Q) if self.quantity > ZERO else None

    @property
    def limit_price(self) -> Decimal | None:
        """Worst consumed price: a limit at this price re-walks exactly these levels."""
        return max((level.price for level in self.levels), default=None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue.value,
            "market_id": self.market_id,
            "outcome": self.outcome.value,
            "quantity": str(self.quantity),
            "vwap": str(self.vwap) if self.vwap is not None else None,
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "fees": str(self.fees),
            "levels": [level.as_dict() for level in self.levels],
        }


@dataclass(frozen=True, slots=True)
class PaperEdge:
    reason: str
    quantity: Decimal = ZERO
    yes_leg: Leg | None = None
    no_leg: Leg | None = None
    touch_gross_edge: Decimal | None = None
    gross_edge_per_contract: Decimal | None = None
    fees_per_contract: Decimal | None = None
    net_edge_per_contract: Decimal | None = None
    levels_walked: int = 0
    depth_limited: bool = False

    @property
    def tradeable(self) -> bool:
        return self.reason == "ok" and self.quantity > ZERO

    @property
    def total_fees(self) -> Decimal:
        return (self.yes_leg.fees if self.yes_leg else ZERO) + (self.no_leg.fees if self.no_leg else ZERO)

    @property
    def expected_paper_pnl(self) -> Decimal | None:
        """Locked-in payout minus cost and fees if both legs settle on the same event."""
        if self.net_edge_per_contract is None:
            return None
        return (self.net_edge_per_contract * self.quantity).quantize(Q)

    def as_dict(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "reason": self.reason,
            "quantity": str(self.quantity),
            "touch_gross_edge": s(self.touch_gross_edge),
            "gross_edge_per_contract": s(self.gross_edge_per_contract),
            "fees_per_contract": s(self.fees_per_contract),
            "net_edge_per_contract": s(self.net_edge_per_contract),
            "expected_paper_pnl": s(self.expected_paper_pnl),
            "levels_walked": self.levels_walked,
            "depth_limited": self.depth_limited,
            "yes_leg": self.yes_leg.as_dict() if self.yes_leg else None,
            "no_leg": self.no_leg.as_dict() if self.no_leg else None,
        }


def _remaining(levels: tuple[PriceLevel, ...]) -> list[list[Decimal]]:
    return [[level.price, level.size] for level in levels if level.size > ZERO]


def compute_paper_edge(
    *,
    cheap_yes_view: OrderBook,
    dear_yes_view: OrderBook,
    cheap_venue: Venue,
    dear_venue: Venue,
    fee_cheap: FeeSchedule,
    fee_dear: FeeSchedule,
    max_quantity: Decimal,
    min_quantity: Decimal = ONE,
    min_net_edge: Decimal = ZERO,
) -> PaperEdge:
    """Size the YES(cheap)/NO(dear) pair by walking both books.

    Both books must already be in YES-normalised terms for the *same* event
    polarity; the caller maps the legs back to each venue's actual outcome.
    Prices inside the returned legs are the prices of the outcome actually
    bought (YES at ``ask`` on the cheap side, NO at ``1 - bid`` on the dear
    side), which is also what the fee schedules are evaluated at.
    """
    if max_quantity <= ZERO:
        return PaperEdge("max_quantity_not_positive")
    asks = _remaining(cheap_yes_view.asks)
    bids = _remaining(dear_yes_view.bids)
    if not asks or not bids:
        return PaperEdge("missing_touch")
    touch_gross = bids[0][0] - asks[0][0]
    if touch_gross <= ZERO:
        return PaperEdge("no_positive_touch_edge", touch_gross_edge=touch_gross)

    yes_levels: list[LegLevel] = []
    no_levels: list[LegLevel] = []
    remaining = max_quantity
    walked = 0
    depth_limited = False
    a = b = 0
    while remaining > ZERO:
        if a >= len(asks) or b >= len(bids):
            depth_limited = True
            break
        ask_price, ask_size = asks[a]
        bid_price, bid_size = bids[b]
        marginal_gross = bid_price - ask_price
        # Conservative per-contract fee estimate: one-contract fills round up
        # to the cent, so this never understates the fee actually charged.
        marginal_fee = fee_cheap(ONE, ask_price) + fee_dear(ONE, ONE - bid_price)
        if marginal_gross - marginal_fee <= min_net_edge:
            break
        take = min(remaining, ask_size, bid_size)
        if take <= ZERO:
            break
        walked += 1
        yes_levels.append(LegLevel(ask_price, take, fee_cheap(take, ask_price)))
        no_levels.append(LegLevel(ONE - bid_price, take, fee_dear(take, ONE - bid_price)))
        remaining -= take
        asks[a][1] -= take
        bids[b][1] -= take
        if asks[a][1] <= ZERO:
            a += 1
        if bids[b][1] <= ZERO:
            b += 1

    yes_leg = Leg(cheap_venue, cheap_yes_view.market_id, Outcome.YES, tuple(yes_levels))
    no_leg = Leg(dear_venue, dear_yes_view.market_id, Outcome.NO, tuple(no_levels))
    quantity = yes_leg.quantity
    if quantity <= ZERO:
        return PaperEdge(
            "marginal_edge_below_fees",
            touch_gross_edge=touch_gross,
            levels_walked=walked,
        )
    gross_cost = (yes_leg.cost + no_leg.cost) / quantity
    gross_edge = (ONE - gross_cost).quantize(Q)
    fees_pc = ((yes_leg.fees + no_leg.fees) / quantity).quantize(Q)
    net_edge = (gross_edge - fees_pc).quantize(Q)
    common = {
        "quantity": quantity,
        "yes_leg": yes_leg,
        "no_leg": no_leg,
        "touch_gross_edge": touch_gross,
        "gross_edge_per_contract": gross_edge,
        "fees_per_contract": fees_pc,
        "net_edge_per_contract": net_edge,
        "levels_walked": walked,
        "depth_limited": depth_limited,
    }
    if quantity < min_quantity:
        return PaperEdge("insufficient_depth", **common)
    if net_edge <= min_net_edge:
        return PaperEdge("net_edge_below_threshold", **common)
    return PaperEdge("ok", **common)
