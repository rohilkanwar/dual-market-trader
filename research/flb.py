"""Kalshi favorite–longshot bias (FLB): fee model, price bands, paper tracks, snapshot verdicts.

What a *snapshot* of open books can and cannot say
-------------------------------------------------
FLB is a statement about realised outcomes versus prices (longshots win less
often than their price implies). A snapshot of open books contains prices but
no outcomes, so it **cannot identify FLB**. It can measure the structure that
makes taking longshots expensive even if prices were fair:

* how much of the universe sits in each price band;
* the cost of *taking* a leg in each band: half-spread plus the taker fee as a
  fraction of the price paid (the fee ``M·0.07·P·(1-P)`` is ``M·0.07·(1-P)`` of
  the price, so it is largest for longshots; the tick makes it worse);
* the overround of mutually exclusive events (``sum of asks - 1``), the amount
  a taker buying every leg loses for certain.

The snapshot verdicts below therefore report ``NOT_IDENTIFIABLE`` for FLB
itself and PASS/FAIL only for those structural checks. True ex-post returns need
settlement outcomes: see :mod:`research.flb_expost`.

Paper tracks
------------
``kalshi_longshot_fade`` (taker) and ``kalshi_maker_quote`` (resting) fade the
longshot side (priced below 20c) of every qualifying Kalshi market in the shared snapshot through
the normal risk-gated :class:`core.execution.ExecutionEngine`, with the paper
risk defaults $25/order, $75/market, $75 daily. The fade track also books the
mirror longshot buy in a shadow ledger. Fills are marked from the same snapshot
(maker track: conservative marks, i.e. the price you could exit at).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any
from uuid import uuid4

from core.ledger import PaperLedger
from core.risk import RiskLimits
from core.types import ONE, ZERO, ExecutionReport, Fill, Market, Order, OrderBook, OrderStatus, Outcome, Venue
from research.scoreboard import SnapshotClient, TrackRuntime, TrackSummary, VenueSnapshot, _bps
from strategies.flb import (
    FLB_RISK_LIMITS,
    FlbEvaluation,
    FlbParameters,
    LongshotFadeStrategy,
    MakerQuoteStrategy,
    longshot_buy_order,
    portfolio_cash_at_risk,
    whole_contracts,
)

FLB_TRACKS: tuple[str, ...] = ("kalshi_longshot_fade", "kalshi_maker_quote")
FLB_TRACK_LABELS = {
    "kalshi_longshot_fade": "Kalshi longshot fade (taker)",
    "kalshi_maker_quote": "Kalshi maker quote (resting fade)",
}
FLB_PRIMARY_TRACK = "kalshi_maker_quote"

# Price bands: lower bound inclusive, upper exclusive; the last band is closed at 1.
PRICE_BANDS: tuple[tuple[str, Decimal, Decimal], ...] = (
    ("<10c", Decimal("0.00"), Decimal("0.10")),
    ("10-20c", Decimal("0.10"), Decimal("0.20")),
    ("20-30c", Decimal("0.20"), Decimal("0.30")),
    ("30-40c", Decimal("0.30"), Decimal("0.40")),
    ("40-50c", Decimal("0.40"), Decimal("0.50")),
    ("50-60c", Decimal("0.50"), Decimal("0.60")),
    ("60-70c", Decimal("0.60"), Decimal("0.70")),
    ("70-80c", Decimal("0.70"), Decimal("0.80")),
    ("80-90c", Decimal("0.80"), Decimal("0.90")),
    (">=90c", Decimal("0.90"), Decimal("1.00")),
)
BAND_ORDER: tuple[str, ...] = tuple(name for name, _, _ in PRICE_BANDS)
LONGSHOT_BANDS: tuple[str, ...] = ("<10c", "10-20c")
FAVORITE_BANDS: tuple[str, ...] = ("80-90c", ">=90c")

TAKER_BASE_RATE = Decimal("0.07")
MAKER_BASE_RATE = Decimal("0.0175")
CENTICENT = Decimal("0.0001")
Q4 = Decimal("0.0001")
VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_INSUFFICIENT = "INSUFFICIENT_DATA"
VERDICT_NOT_IDENTIFIABLE = "NOT_IDENTIFIABLE"


def band_for(price: Decimal) -> str:
    if not ZERO <= price <= ONE:
        raise ValueError(f"price {price} must be between 0 and 1")
    for name, lower, upper in PRICE_BANDS:
        if lower <= price < upper:
            return name
    return BAND_ORDER[-1]


def _q(value: Decimal | None, places: Decimal = Q4) -> Decimal | None:
    return value.quantize(places) if value is not None else None


def _mean(values: list[Decimal]) -> Decimal | None:
    return (sum(values, ZERO) / len(values)).quantize(Q4) if values else None


# --------------------------------------------------------------------------
# Fee model
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class KalshiFeeModel:
    """Kalshi July-2026 schedule: ``round_up(M · rate · C · P · (1-P))``.

    ``rate`` is 0.07 for takers and 0.0175 for makers. ``M`` is the series
    ``fee_multiplier`` (1 default, 0.5 on some game series, 0 on a few). Maker
    fees apply only to ``quadratic_with_maker_fees`` series. When a market has no
    fee metadata (fixtures, failed series lookup) the model assumes maker fees
    apply — the conservative choice, and true for every canary macro series.
    Rounding is to a centicent per the current schedule; ``venues.paper.kalshi_fee``
    rounds to a cent, which is stricter for tiny orders.
    """

    taker_rate: Decimal = TAKER_BASE_RATE
    maker_rate: Decimal = MAKER_BASE_RATE
    default_multiplier: Decimal = ONE
    assume_maker_fees_when_unknown: bool = True
    quantum: Decimal = CENTICENT

    @staticmethod
    def multiplier_of(market: Market | None, default: Decimal = ONE) -> Decimal:
        raw = market.metadata.get("fee_multiplier") if market is not None else None
        if raw is None or raw == "":
            return default
        try:
            value = Decimal(str(raw))
        except ArithmeticError:
            return default
        return value if value >= ZERO else default

    def maker_fees_apply(self, market: Market | None) -> bool:
        fee_type = market.metadata.get("fee_type") if market is not None else None
        if fee_type in (None, ""):
            return self.assume_maker_fees_when_unknown
        return str(fee_type) == "quadratic_with_maker_fees"

    def rate_for(self, market: Market | None, *, maker: bool) -> Decimal:
        multiplier = self.multiplier_of(market, self.default_multiplier)
        if maker:
            return self.maker_rate * multiplier if self.maker_fees_apply(market) else ZERO
        return self.taker_rate * multiplier

    def per_contract(self, price: Decimal, *, maker: bool, market: Market | None = None) -> Decimal:
        """Unrounded fee per contract at ``price`` for the traded outcome."""
        return self.rate_for(market, maker=maker) * price * (ONE - price)

    def fee(self, quantity: Decimal, price: Decimal, *, maker: bool, market: Market | None = None) -> Decimal:
        raw = self.per_contract(price, maker=maker, market=market) * quantity
        return raw.quantize(self.quantum, rounding=ROUND_UP) if raw > ZERO else ZERO

    @classmethod
    def zero(cls) -> KalshiFeeModel:
        return cls(taker_rate=ZERO, maker_rate=ZERO)


# --------------------------------------------------------------------------
# Snapshot client with maker expected-fill simulation
# --------------------------------------------------------------------------
class FlbSnapshotClient(SnapshotClient):
    """Frozen-snapshot paper client. Taker orders walk the book; maker orders get
    an *expected* fill ``floor(quantity * fill_probability)`` at their own price.

    A live resting order fills fully or not at all; the expected-value fill keeps
    the paper run deterministic and is a documented assumption.
    """

    def __init__(self, snapshot: VenueSnapshot, fee_model: KalshiFeeModel) -> None:
        super().__init__(
            snapshot,
            lambda q, p: fee_model.fee(q, p, maker=False),
            model_fees=fee_model.taker_rate > ZERO or fee_model.maker_rate > ZERO,
        )
        self.fee_model = fee_model

    def _fee_schedule_for(self, market: Market):  # type: ignore[override]
        """Taker fee priced per market: the series multiplier lives in market metadata."""
        return lambda quantity, price: self.fee_model.fee(quantity, price, maker=False, market=market)

    async def place_order_with_book(self, order: Order, market: Market, book: OrderBook) -> ExecutionReport:
        if order.metadata.get("execution") != "maker":
            return await super().place_order_with_book(order, market, book)
        if market.market_id != order.market_id or not market.active:
            raise ValueError(f"market {order.market_id} is unavailable")
        assert order.price is not None
        view = book if order.outcome is Outcome.YES else book.for_outcome(Outcome.NO)
        touch = view.best_ask
        if touch is not None and order.price >= touch.price:
            # Would cross: it is a taker order, so fall back to the book walk.
            return await super().place_order_with_book(replace(order, metadata={**order.metadata, "execution": "taker", "placement": "take"}), market, book)
        probability = Decimal(order.metadata.get("fill_probability", "0"))
        filled = whole_contracts(order.quantity * probability)
        order_id = f"paper-maker-{uuid4().hex[:12]}"
        fills: list[Fill] = []
        if filled > ZERO:
            fills.append(
                Fill(
                    venue=order.venue,
                    market_id=order.market_id,
                    order_id=order_id,
                    side=order.side,
                    outcome=order.outcome,
                    quantity=filled,
                    price=order.price,
                    fee=self.fee_model.fee(filled, order.price, maker=True, market=market),
                )
            )
        status = OrderStatus.ACCEPTED if not fills else (OrderStatus.FILLED if filled == order.quantity else OrderStatus.PARTIALLY_FILLED)
        for fill in fills:
            self._paper_portfolio.apply_fill(fill)
            self._paper_fills.append(fill)
        return ExecutionReport(replace(order, order_id=order_id, status=status), tuple(fills))


def flb_client_factory(fee_model: KalshiFeeModel):
    def factory(snapshot: VenueSnapshot, fee_schedule: Any) -> SnapshotClient:
        if snapshot.venue is Venue.KALSHI:
            return FlbSnapshotClient(snapshot, fee_model)
        return SnapshotClient(snapshot, fee_schedule)

    return factory


def create_flb_runtime(
    name: str,
    snapshots: dict[Venue, VenueSnapshot],
    *,
    ledger: PaperLedger | None,
    starting_cash: Decimal,
    model_fees: bool,
    risk_limits: RiskLimits | None = None,
) -> TrackRuntime:
    fee_model = KalshiFeeModel() if model_fees else KalshiFeeModel.zero()
    return TrackRuntime.create(
        name,
        snapshots,
        ledger=ledger,
        risk_limits=risk_limits or FLB_RISK_LIMITS,
        starting_cash=starting_cash,
        model_fees=model_fees,
        client_factory=flb_client_factory(fee_model),
        mark_method="conservative" if name == "kalshi_maker_quote" else "mid",
    )


# --------------------------------------------------------------------------
# Track runners
# --------------------------------------------------------------------------
def _new_band_row() -> dict[str, Any]:
    return {"candidates": 0, "orders": 0, "fills": 0, "contracts": ZERO, "notional": ZERO, "fees": ZERO, "expected_edge_bps": [], "fill_probability": []}


def _edge_row(track: str, market: Market, ev: FlbEvaluation, *, filled: bool) -> dict[str, Any]:
    return {
        "track": track,
        "venue": market.venue.value,
        "market": market.market_id,
        "title": market.title,
        "edge_bps": _bps(ev.expected_edge_after_adverse_selection),
        "raw_edge_bps": _bps(ev.expected_edge_vs_mid),
        "admitted": ev.traded,
        "filled": filled,
        "reason": ev.reason,
        "mid": ev.mid,
        "fair_value": ev.mid,
        "spread": ev.spread,
        "longshot_outcome": ev.longshot_outcome.value if ev.longshot_outcome else None,
        "longshot_price": ev.longshot_price,
        "longshot_band": band_for(ev.longshot_price) if ev.longshot_price is not None else None,
        "placement": ev.placement,
        "quote_yes_price": ev.quote_yes_price,
        "fill_probability": ev.fill_probability,
    }


def _finish_bands(bands: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in BAND_ORDER:
        row = bands.get(name)
        if row is None:
            continue
        out[name] = {
            **{k: v for k, v in row.items() if k not in ("expected_edge_bps", "fill_probability")},
            "expected_edge_bps_mean": (int(sum(row["expected_edge_bps"]) / len(row["expected_edge_bps"])) if row["expected_edge_bps"] else None),
            "fill_probability_mean": _mean(row["fill_probability"]),
        }
    return out


async def _run_flb_track(runtime: TrackRuntime, strategy: LongshotFadeStrategy | MakerQuoteStrategy, *, params: FlbParameters, shadow: bool) -> TrackSummary:
    summary = runtime.summary
    snapshot = runtime.snapshots.get(Venue.KALSHI)
    bands: dict[str, dict[str, Any]] = {}
    shadow_ledger = PaperLedger(starting_cash=runtime.ledger.starting_cash, ledger_id=f"{runtime.name}:shadow_longshot_buyer") if shadow else None
    shadow_rows: list[dict[str, Any]] = []
    client = runtime.clients.get(Venue.KALSHI)
    if snapshot is None or client is None:
        summary.notes = "No Kalshi snapshot available."
        return summary
    fee_model = client.fee_model if isinstance(client, FlbSnapshotClient) else KalshiFeeModel()
    shadow_client = FlbSnapshotClient(snapshot, fee_model) if shadow_ledger is not None else None
    for market in snapshot.markets:
        book = snapshot.book(market)
        if book.best_bid is None and book.best_ask is None:
            summary.refuse("empty_book")
            continue
        summary.candidates += 1
        ev = strategy.evaluate(market, book)
        if ev.longshot_price is not None:
            row = bands.setdefault(band_for(ev.longshot_price), _new_band_row())
            row["candidates"] += 1
        if not ev.traded:
            summary.refuse(ev.reason)
            if ev.reason != "not_longshot":
                summary.edges.append(_edge_row(runtime.name, market, ev, filled=False))
            continue
        summary.admitted += 1
        summary.proposed_orders += len(ev.orders)
        summary.admitted_edges.append(ev.expected_edge_after_adverse_selection or ZERO)
        band = band_for(ev.longshot_price)  # type: ignore[arg-type]
        row = bands[band]
        row["orders"] += len(ev.orders)
        row["expected_edge_bps"].append(_bps(ev.expected_edge_after_adverse_selection) or 0)
        row["fill_probability"].append(ev.fill_probability or ZERO)
        fills_before = len(summary.fills)
        for order in ev.orders:
            report = await runtime.submit(order, edge=ev.expected_edge_after_adverse_selection)
            if isinstance(strategy, MakerQuoteStrategy):
                strategy.note_resting(order, report.filled_quantity if report is not None else ZERO)
        for fill_row in summary.fills[fills_before:]:
            fill_row.update({
                "band": band_for(fill_row["price"]),
                "longshot_band": band,
                "longshot_outcome": ev.longshot_outcome.value if ev.longshot_outcome else None,
                "longshot_price": ev.longshot_price,
                "placement": ev.placement,
                "fill_probability": ev.fill_probability,
                "role": "maker" if ev.placement in ("join", "improve") else "taker",
            })
            row["fills"] += 1
            row["contracts"] += fill_row["qty"]
            row["notional"] += fill_row["qty"] * fill_row["price"]
            row["fees"] += fill_row["fee"]
        summary.edges.append(_edge_row(runtime.name, market, ev, filled=len(summary.fills) > fills_before))
        if shadow_ledger is not None and shadow_client is not None:
            mirror = longshot_buy_order(
                market, book, params,
                position=shadow_ledger.portfolio.get(market.venue, market.market_id),
                total_cash_at_risk=portfolio_cash_at_risk(shadow_ledger.portfolio),
            )
            if mirror is not None:
                report = await shadow_client.place_order_with_book(mirror, market, book)
                for fill in report.fills:
                    shadow_ledger.record_fill(fill)
                    shadow_rows.append({
                        "market": market.market_id,
                        "title": market.title,
                        "outcome": fill.outcome.value,
                        "qty": fill.quantity,
                        "price": fill.price,
                        "yes_equivalent_price": fill.yes_equivalent_price,
                        "fee": fill.fee,
                        "band": band_for(fill.price),
                        "longshot_band": band,
                    })
    summary.metrics["parameters"] = {
        "longshot_threshold": params.longshot_threshold,
        "max_order_notional": params.max_order_notional,
        "max_market_notional": params.max_market_notional,
        "max_total_cash_at_risk": params.max_total_cash_at_risk,
        "join_fill_probability": params.join_fill_probability,
        "improve_fill_probability": params.improve_fill_probability,
        "adverse_selection_haircut": params.adverse_selection_haircut,
        "tick": params.tick,
        "risk_limits": {
            "max_notional_per_order": runtime.risk.limits.max_notional_per_order,
            "max_position_per_market": runtime.risk.limits.max_position_per_market,
            "max_daily_loss": runtime.risk.limits.max_daily_loss,
        },
    }
    summary.metrics["fee_model"] = {
        "taker_rate": fee_model.taker_rate,
        "maker_rate": fee_model.maker_rate,
        "formula": "round_up(M * rate * C * P * (1-P)); M = series fee_multiplier",
        "quantum": fee_model.quantum,
    }
    summary.metrics["longshot_bands"] = _finish_bands(bands)
    summary.metrics["longshot_candidates"] = sum(r["candidates"] for r in bands.values())
    summary.metrics["cash_at_risk"] = {
        "positions": _q(portfolio_cash_at_risk(runtime.ledger.portfolio)),
        "resting_orders_reserved": _q(strategy.resting_collateral) if isinstance(strategy, MakerQuoteStrategy) else ZERO,
        "cap": params.max_total_cash_at_risk,
    }
    summary.metrics["mark_method"] = runtime.ledger.mark_method
    if shadow_ledger is not None:
        # Marks are applied in finalize; stash the ledger and rows for a post-mark summary.
        summary.metrics["_shadow_ledger"] = shadow_ledger
        summary.metrics["_shadow_rows"] = shadow_rows
    summary.settlement_risk_flag = False
    return summary


async def run_longshot_fade_track(runtime: TrackRuntime, *, params: FlbParameters | None = None) -> TrackSummary:
    params = params or FlbParameters()
    strategy = LongshotFadeStrategy(params, portfolio=runtime.ledger.portfolio, risk=runtime.risk)
    return await _run_flb_track(runtime, strategy, params=params, shadow=True)


async def run_maker_quote_track(runtime: TrackRuntime, *, params: FlbParameters | None = None) -> TrackSummary:
    params = params or FlbParameters()
    strategy = MakerQuoteStrategy(params, portfolio=runtime.ledger.portfolio, risk=runtime.risk)
    return await _run_flb_track(runtime, strategy, params=params, shadow=False)


def band_pnl_table(rows: list[dict[str, Any]], *, key: str = "longshot_band") -> dict[str, dict[str, Any]]:
    """Per-band paper PnL from fill rows that already carry ``paper_pnl`` (post-mark)."""
    table: dict[str, dict[str, Any]] = {}
    for row in rows:
        band = row.get(key)
        if band is None:
            continue
        cell = table.setdefault(band, {"fills": 0, "contracts": ZERO, "notional": ZERO, "fees": ZERO, "paper_pnl": ZERO, "marked": 0})
        cell["fills"] += 1
        cell["contracts"] += row["qty"]
        cell["notional"] += row["qty"] * row["price"]
        cell["fees"] += row["fee"]
        if row.get("paper_pnl") is not None:
            cell["paper_pnl"] += row["paper_pnl"]
            cell["marked"] += 1
    for cell in table.values():
        cell["paper_pnl_per_contract"] = _q(cell["paper_pnl"] / cell["contracts"]) if cell["contracts"] else None
        cell["return_on_notional_bps"] = _bps(cell["paper_pnl"] / cell["notional"]) if cell["notional"] else None
        for k in ("contracts", "notional", "fees", "paper_pnl"):
            cell[k] = _q(cell[k])
    return {band: table[band] for band in BAND_ORDER if band in table}


def finalize_flb_metrics(runtime: TrackRuntime) -> None:
    """Called after :meth:`TrackRuntime.finalize` has marked positions."""
    summary = runtime.summary
    summary.metrics["paper_pnl_by_longshot_band"] = band_pnl_table(summary.fills)
    summary.metrics["paper_pnl_by_price_paid_band"] = band_pnl_table(summary.fills, key="band")
    shadow: PaperLedger | None = summary.metrics.pop("_shadow_ledger", None)
    rows: list[dict[str, Any]] = summary.metrics.pop("_shadow_rows", [])
    if shadow is None:
        return
    snapshot = runtime.snapshots.get(Venue.KALSHI)
    for position in shadow.open_positions:
        market = runtime.market_for(position.venue, position.market_id)
        if snapshot is not None and market is not None:
            shadow.mark_from_book(position.venue, position.market_id, snapshot.book(market))
    shadow.snapshot(label="shadow")
    for row in rows:
        mark = shadow.marks.get((Venue.KALSHI, row["market"]))
        if mark is None:
            row["paper_pnl"] = None
            continue
        signed = row["qty"] if row["outcome"] == "yes" else -row["qty"]
        row["mark"] = mark
        row["paper_pnl"] = (signed * (mark - row["yes_equivalent_price"]) - row["fee"]).quantize(Q4)
    ledger_summary = shadow.summary()
    summary.metrics["shadow_longshot_buyer"] = {
        "note": (
            "Mirror of every fade: takes the longshot at its ask with the same $25/$75 caps. "
            "Hypothetical benchmark only; never proposed to the risk-gated engine."
        ),
        "fills": ledger_summary["fills"],
        "fees_paid": ledger_summary["fees_paid"],
        "realized_pnl": ledger_summary["realized_pnl"],
        "unrealized_pnl": ledger_summary["unrealized_pnl"],
        "total_pnl": ledger_summary["total_pnl"],
        "gross_notional": ledger_summary["gross_notional"],
        "mark_method": shadow.mark_method,
        "paper_pnl_by_longshot_band": band_pnl_table(rows),
    }


# --------------------------------------------------------------------------
# Snapshot diagnostics and verdicts
# --------------------------------------------------------------------------
def _leg_price(book: OrderBook) -> Decimal | None:
    if book.mid_price is not None:
        return book.mid_price
    if book.best_ask is not None:
        return book.best_ask.price
    if book.best_bid is not None:
        return book.best_bid.price
    return None


def snapshot_band_table(snapshot: VenueSnapshot, fee_model: KalshiFeeModel | None = None) -> dict[str, dict[str, Any]]:
    """Per-YES-price-band structure of the open books: counts, spreads, cost of taking."""
    fee_model = fee_model or KalshiFeeModel()
    acc: dict[str, dict[str, list[Decimal] | int]] = {}
    for market in snapshot.markets:
        book = snapshot.book(market)
        price = _leg_price(book)
        if price is None:
            continue
        cell = acc.setdefault(band_for(price), {"markets": 0, "two_sided": 0, "spread": [], "rel_half_spread": [], "fee_pct": [], "take_cost_pct": [], "ask_depth": [], "bid_depth": [], "volume": []})
        cell["markets"] += 1  # type: ignore[operator]
        cell["volume"].append(market.volume)  # type: ignore[union-attr]
        ask, bid = book.best_ask, book.best_bid
        if ask is not None:
            cell["ask_depth"].append(ask.size)  # type: ignore[union-attr]
            if ask.price > ZERO:
                fee = fee_model.per_contract(ask.price, maker=False, market=market)
                cell["fee_pct"].append(fee / ask.price)  # type: ignore[union-attr]
                if bid is not None:
                    half = (ask.price - bid.price) / 2
                    cell["take_cost_pct"].append((half + fee) / ask.price)  # type: ignore[union-attr]
        if bid is not None:
            cell["bid_depth"].append(bid.size)  # type: ignore[union-attr]
        if ask is not None and bid is not None:
            cell["two_sided"] += 1  # type: ignore[operator]
            cell["spread"].append(ask.price - bid.price)  # type: ignore[union-attr]
            mid = (ask.price + bid.price) / 2
            if mid > ZERO:
                cell["rel_half_spread"].append((ask.price - bid.price) / 2 / mid)  # type: ignore[union-attr]
    out: dict[str, dict[str, Any]] = {}
    for band in BAND_ORDER:
        cell = acc.get(band)
        if cell is None:
            continue
        out[band] = {
            "markets": cell["markets"],
            "two_sided": cell["two_sided"],
            "mean_spread": _mean(cell["spread"]),  # type: ignore[arg-type]
            "mean_relative_half_spread": _mean(cell["rel_half_spread"]),  # type: ignore[arg-type]
            "mean_taker_fee_pct_of_price": _mean(cell["fee_pct"]),  # type: ignore[arg-type]
            "mean_take_cost_pct_of_price": _mean(cell["take_cost_pct"]),  # type: ignore[arg-type]
            "mean_ask_depth": _mean(cell["ask_depth"]),  # type: ignore[arg-type]
            "mean_bid_depth": _mean(cell["bid_depth"]),  # type: ignore[arg-type]
            "total_volume": _q(sum(cell["volume"], ZERO)),  # type: ignore[arg-type]
        }
    return out


def snapshot_event_overround(snapshot: VenueSnapshot, *, longshot_threshold: Decimal = Decimal("0.20")) -> dict[str, Any]:
    """Overround of mutually exclusive events (``sum of asks - 1``) and its longshot share."""
    legs: dict[str, list[tuple[Market, OrderBook]]] = {}
    for market in snapshot.markets:
        event = market.metadata.get("event_ticker")
        if market.metadata.get("mutually_exclusive") is True and event:
            legs.setdefault(str(event), []).append((market, snapshot.book(market)))
    events: list[dict[str, Any]] = []
    for event, pairs in sorted(legs.items()):
        asks = [(m, b.best_ask.price) for m, b in pairs if b.best_ask is not None]
        mids = [b.mid_price for _, b in pairs if b.mid_price is not None]
        if len(asks) < 2:
            continue
        sum_ask = sum((p for _, p in asks), ZERO)
        longshot_asks = [p for _, p in asks if p <= longshot_threshold]
        events.append({
            "event_ticker": event,
            "legs": len(pairs),
            "legs_with_ask": len(asks),
            "sum_ask": _q(sum_ask),
            "overround_ask": _q(sum_ask - ONE),
            "sum_mid": _q(sum(mids, ZERO)) if len(mids) == len(pairs) else None,
            "overround_mid": _q(sum(mids, ZERO) - ONE) if len(mids) == len(pairs) else None,
            "longshot_legs": len(longshot_asks),
            "sum_longshot_ask": _q(sum(longshot_asks, ZERO)),
            "longshot_share_of_sum_ask": _q(sum(longshot_asks, ZERO) / sum_ask) if sum_ask else None,
        })
    overrounds = [e["overround_ask"] for e in events]
    return {
        "events": events,
        "n_events": len(events),
        "mean_overround_ask": _mean(overrounds),
        "events_with_positive_overround": sum(1 for o in overrounds if o > ZERO),
        "mean_longshot_share_of_sum_ask": _mean([e["longshot_share_of_sum_ask"] for e in events if e["longshot_share_of_sum_ask"] is not None]),
    }


def _band_mean(table: dict[str, dict[str, Any]], bands: tuple[str, ...], key: str) -> tuple[Decimal | None, int]:
    values = [table[b][key] for b in bands if b in table and table[b].get(key) is not None]
    markets = sum(int(table[b]["markets"]) for b in bands if b in table)
    return (_mean(values), markets)


def snapshot_verdicts(snapshot: VenueSnapshot, *, fee_model: KalshiFeeModel | None = None, longshot_threshold: Decimal = Decimal("0.20"), min_markets_per_side: int = 3) -> dict[str, Any]:
    table = snapshot_band_table(snapshot, fee_model)
    overround = snapshot_event_overround(snapshot, longshot_threshold=longshot_threshold)
    n_books = sum(int(c["markets"]) for c in table.values())
    longshot_cost, longshot_n = _band_mean(table, LONGSHOT_BANDS, "mean_take_cost_pct_of_price")
    favorite_cost, favorite_n = _band_mean(table, FAVORITE_BANDS, "mean_take_cost_pct_of_price")

    identifiable = {
        "verdict": VERDICT_INSUFFICIENT if n_books == 0 else VERDICT_NOT_IDENTIFIABLE,
        "question": "Does favorite-longshot bias appear in the current public books?",
        "reason": (
            "A snapshot holds prices but no outcomes; FLB is realised-return versus price and "
            "cannot be identified without settlement results. Use the ex-post section."
        ),
        "markets_with_books": n_books,
        "markets_in_longshot_bands": longshot_n,
        "markets_in_favorite_bands": favorite_n,
    }
    if longshot_cost is None or favorite_cost is None or longshot_n < min_markets_per_side or favorite_n < min_markets_per_side:
        take_cost = {"verdict": VERDICT_INSUFFICIENT}
    else:
        take_cost = {"verdict": VERDICT_PASS if longshot_cost > favorite_cost else VERDICT_FAIL}
    take_cost.update({
        "question": "Is taking a longshot (<20c) more expensive, per dollar paid, than taking a favourite (>=80c)?",
        "definition": "(half-spread + taker fee at the ask) / ask price, averaged over markets in the band",
        "longshot_take_cost_pct": longshot_cost,
        "favorite_take_cost_pct": favorite_cost,
        "longshot_markets": longshot_n,
        "favorite_markets": favorite_n,
        "min_markets_per_side": min_markets_per_side,
    })
    if overround["n_events"] == 0:
        over = {"verdict": VERDICT_INSUFFICIENT}
    else:
        over = {"verdict": VERDICT_PASS if overround["mean_overround_ask"] > ZERO else VERDICT_FAIL}
    over.update({
        "question": "Do mutually exclusive events quote a positive overround (sum of asks > 1)?",
        "n_events": overround["n_events"],
        "mean_overround_ask": overround["mean_overround_ask"],
        "events_with_positive_overround": overround["events_with_positive_overround"],
        "mean_longshot_share_of_sum_ask": overround["mean_longshot_share_of_sum_ask"],
    })
    return {
        "flb_identifiable_from_snapshot": identifiable,
        "longshot_take_cost_exceeds_favorite": take_cost,
        "event_overround_positive": over,
        "band_table": table,
        "event_overround": overround,
    }
