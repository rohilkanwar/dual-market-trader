"""Polymarket taker-fee model.

Published schedule (docs.polymarket.com/polymarket-learn/trading/fees, read
2026-09-14)::

    fee = C * feeRate * p * (1 - p)        # C shares at price p, USDC, 5 dp

Makers pay nothing. The rate depends on the market's category, which Gamma
exposes as ``feeType`` (``politics_fees``, ``sports_fees_v3`` ...). Gamma also
reports ``takerBaseFee = 1000`` on every fee-enabled market; that value is the
exchange contract's fee *ceiling* in bps, not the applied rate, so it is
deliberately ignored here. Unknown fee-enabled types fall back to the highest
published rate (fail-closed for an arbitrage detector).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from core.types import ONE, ZERO
from venues.paper import FeeSchedule

FEE_RATES_BY_CATEGORY: dict[str, Decimal] = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.05"),
    "finance": Decimal("0.04"),
    "politics": Decimal("0.04"),
    "economics": Decimal("0.05"),
    "culture": Decimal("0.05"),
    "weather": Decimal("0.05"),
    "mentions": Decimal("0.04"),
    "tech": Decimal("0.04"),
    "other": Decimal("0.05"),
    "general": Decimal("0.05"),
    "geopolitics": ZERO,
}
UNKNOWN_ENABLED_RATE = Decimal("0.07")
FEE_QUANTUM = Decimal("0.00001")


def taker_fee_rate(fee_type: str | None, fees_enabled: bool | None) -> Decimal:
    """Map Gamma's ``feeType``/``feesEnabled`` to the published taker rate."""
    if not fees_enabled:
        return ZERO
    key = (fee_type or "").strip().lower()
    for category, rate in FEE_RATES_BY_CATEGORY.items():
        if key.startswith(category):
            return rate
    return UNKNOWN_ENABLED_RATE


def polymarket_taker_fee(quantity: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    if rate <= ZERO or quantity <= ZERO:
        return ZERO
    raw = quantity * rate * price * (ONE - price)
    return raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)


def polymarket_fee_schedule(rate: Decimal) -> FeeSchedule:
    def schedule(quantity: Decimal, price: Decimal) -> Decimal:
        return polymarket_taker_fee(quantity, price, rate)

    return schedule
