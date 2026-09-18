"""Queue-aware Kalshi FLB maker fill simulation (paper only).

Additive to the expected-value ``kalshi_maker_quote`` track. Replays L2 books +
public trade prints (fixture, ``apps.book_logger`` archive, or settled-trade
harvest) through :class:`strategies.flb_queue.FlbQueueFillModel`, records
post-fill markouts, and — when a settlement outcome is known — scores net EV
against resolved payouts rather than mid marks alone.

Honesty limits are carried on every report (``honesty_limits``) and in
``docs/ASSUMPTIONS.md`` §10.10. True L3 / FIFO is not available from public
Kalshi data; this model is the best available approximation from aggregated
depth and the public tape.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from core.ledger import PaperLedger
from core.types import ONE, ZERO, Fill, Market, Order, OrderBook, Outcome, Side, Venue
from research.flb import KalshiFeeModel, VERDICT_FAIL, VERDICT_INSUFFICIENT, VERDICT_PASS, band_for
from strategies.flb import FLB_RISK_LIMITS
from research.flb_expost import SettledMarket
from research.scoreboard import SnapshotClient, TrackRuntime, VenueSnapshot
from research.whale_noise import (
    MarketTimeline,
    Print,
    _levels,
    _print_from_raw,
    parse_ts,
    timelines_from_archive as _whale_timelines_from_archive,
)
from strategies.flb_queue import (
    FlbQueueFillModel,
    FlbQueueParameters,
    FlbRestingQuote,
    markout,
    place_flb_maker_quote,
    place_flb_maker_quote_from_longshot_print,
    portfolio_and_resting_cash_at_risk,
)

SCHEMA_VERSION = "1.0.0"
TRACK = "kalshi_maker_queue_sim"
FIXTURE_PATH = Path(__file__).resolve().parents[1] / "research" / "fixtures" / "flb_queue_tape.json"
Q4 = Decimal("0.0001")
Alignment = Literal["time_aligned", "snapshot_after_tape", "settled_trades_only"]

HONESTY_LIMITS = [
    "Public Kalshi books are aggregated L2 (size per price). No order ids, cancels, or FIFO priority — queue position cannot be reconstructed exactly.",
    "Between poll snapshots every book change and every crossing that reverts is invisible; the trade tape fills part of that gap, but the book each print hit is only known to the nearest snapshot.",
    "Kalshi books carry no venue timestamp; capture time plus latency_ms bound staleness.",
    "Cancels ahead of us are never assumed; later books may only lengthen queue_ahead.",
    "Settled-trade-only path (no L2): quotes are bookless and fill only on trade-through prints — never assume we were first at an unknown level.",
    "Self-logged archives from apps.book_logger are the preferred forward source; settled harvests score outcomes but lack books.",
    "Expected-value fills on kalshi_maker_quote remain the snapshot track; this sim does not replace them.",
]

PRE_REGISTRATION = {
    "name": "kalshi_maker_queue_sim",
    "hypothesis": (
        "Resting a favourite-side fade (join / one-tick improve) against longshot flow on Kalshi, "
        "filled only through the conservative L2+tape queue model, earns at least 2¢ net per "
        "contract after maker fees when scored at settlement (or at the longest markout horizon "
        "when outcomes are unavailable)."
    ),
    "pass_criterion": (
        "contract-weighted net EV per filled contract >= pass_net_ev (default 2¢) with "
        "market-clustered t >= t_threshold (default 2), on >= min_fills_for_verdict fills across "
        ">= min_markets_for_verdict markets; stretch bar at stretch_net_ev (default 3¢) is reported "
        "separately and does not change the primary verdict"
    ),
    "fail_criterion": "net EV below pass_net_ev, or t below threshold, once sample floors are met",
    "underpowered": "fewer than min_fills_for_verdict fills or min_markets_for_verdict markets → INSUFFICIENT_DATA / underpowered",
    "adverse_selection_check": "contract-weighted markout at the longest horizon must not be significantly negative (t <= -t_threshold) or the run FAILs maker_fills_not_adversely_selected",
}


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Timelines
# --------------------------------------------------------------------------
def load_fixture_timelines(path: Path = FIXTURE_PATH) -> tuple[list[MarketTimeline], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    timelines: list[MarketTimeline] = []
    for item in payload.get("markets", []):
        market = Market(
            venue=Venue.KALSHI,
            market_id=str(item["market_id"]),
            title=str(item.get("title", "")),
            active=bool(item.get("active", True)),
            volume=Decimal(str(item.get("volume", "0"))),
            metadata={**item.get("metadata", {}), "source": "fixture"},
        )
        raw = payload.get("timeline", {}).get(market.market_id, {})
        timeline = MarketTimeline(market=market, alignment="time_aligned")
        for book in raw.get("books", []):
            ts = parse_ts(book.get("ts"))
            if ts is None:
                continue
            timeline.books.append(
                (ts, OrderBook(market_id=market.market_id, bids=_levels(book.get("bids")), asks=_levels(book.get("asks")), timestamp=ts))
            )
        for trade in raw.get("trades", []):
            print_ = _print_from_raw(trade)
            if print_ is not None:
                timeline.prints.append(print_)
        outcome = item.get("paper_settlement_outcome") or market.metadata.get("paper_settlement_outcome")
        if outcome in ("yes", "no"):
            timeline.settlement = Outcome(outcome)
        timeline.sort()
        if not timeline.books and timeline.prints:
            timeline.alignment = "settled_trades_only"
        timelines.append(timeline)
    meta = {
        "source": "fixture",
        "path": str(path),
        "comment": payload.get("_comment"),
        "markets": len(timelines),
        "alignment": "time_aligned",
    }
    return timelines, meta


def timelines_from_archive(
    root: Path,
    *,
    series: tuple[str, ...] | None = None,
    tickers: tuple[str, ...] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[list[MarketTimeline], dict[str, Any]]:
    timelines, meta = _whale_timelines_from_archive(root, series=series, tickers=tickers, since=since, until=until)
    meta = {**meta, "alignment": "time_aligned", "note": "Settlement outcomes are not in the book archive; markouts only unless joined with a settled harvest."}
    return timelines, meta


def timelines_from_settled(markets: list[SettledMarket]) -> tuple[list[MarketTimeline], dict[str, Any]]:
    """Settled-history path: trade prints + known result, no L2 books.

    Quotes are placed from longshot taker prints and fill only on trade-through
    (see ``FlbQueueParameters.settled_trade_through_only``).
    """
    timelines: list[MarketTimeline] = []
    for settled in markets:
        market = settled.as_market()
        market = Market(
            venue=market.venue,
            market_id=market.market_id,
            title=settled.title or market.title,
            active=True,
            volume=settled.volume,
            metadata={**market.metadata, "source": "settled_harvest"},
        )
        timeline = MarketTimeline(market=market, alignment="settled_trades_only")
        if settled.result in ("yes", "no"):
            timeline.settlement = Outcome(settled.result)
        for i, trade in enumerate(settled.trades):
            timeline.prints.append(
                Print(
                    ts=trade.created_time,
                    yes_price=trade.yes_price,
                    size=trade.count,
                    taker_side=trade.taker_side,
                    trade_id=f"{settled.ticker}-{i}",
                )
            )
        timeline.sort()
        if timeline.prints:
            timelines.append(timeline)
    meta = {
        "source": "settled_trades",
        "markets": len(timelines),
        "alignment": "settled_trades_only",
        "honesty": "No L2 books — fills only on trade-through prints; queue depth unknown.",
    }
    return timelines, meta


# --------------------------------------------------------------------------
# Paper client (maker fills booked at our price with maker fees)
# --------------------------------------------------------------------------
class FlbQueueClient(SnapshotClient):
    def __init__(self, snapshot: VenueSnapshot, fee_model: KalshiFeeModel) -> None:
        super().__init__(
            snapshot,
            lambda q, p: fee_model.fee(q, p, maker=False),
            model_fees=fee_model.taker_rate > ZERO or fee_model.maker_rate > ZERO,
        )
        self.fee_model = fee_model
        self.current_books: dict[str, OrderBook] = {m.market_id: snapshot.book(m) for m in snapshot.markets}

    def _fee_schedule_for(self, market: Market):  # type: ignore[override]
        return lambda quantity, price: self.fee_model.fee(quantity, price, maker=False, market=market)

    async def place_order(self, order: Order):
        if not self.paper:
            raise PermissionError("FlbQueueClient is paper-only")
        market = self._market_cache.get(order.market_id)
        if market is None or not market.active:
            raise ValueError(f"market {order.market_id} is unavailable")
        book = self.current_books.get(order.market_id, self.snapshot.book(market))
        return await self.place_order_with_book(order, market, book)

    async def place_order_with_book(self, order: Order, market: Market, book: OrderBook):
        from dataclasses import replace

        from core.types import ExecutionReport, OrderStatus

        if order.metadata.get("execution") != "maker_fill":
            return await super().place_order_with_book(order, market, book)
        assert order.price is not None
        fill = Fill(
            venue=order.venue,
            market_id=order.market_id,
            order_id=f"paper-qsim-{order.metadata.get('fill_trade_id', '')}",
            side=order.side,
            outcome=order.outcome,
            quantity=order.quantity,
            price=order.price,
            fee=self.fee_model.fee(order.quantity, order.price, maker=True, market=market),
            timestamp=parse_ts(order.metadata.get("fill_ts")) or _now(),
        )
        self._paper_portfolio.apply_fill(fill)
        self._paper_fills.append(fill)
        return ExecutionReport(replace(order, order_id=fill.order_id, status=OrderStatus.FILLED), (fill,))


def _client_factory(fee_model: KalshiFeeModel):
    def factory(snapshot: VenueSnapshot, fee_schedule: Any) -> SnapshotClient:
        return FlbQueueClient(snapshot, fee_model)

    return factory


def _snapshot_from_timelines(timelines: list[MarketTimeline]) -> VenueSnapshot:
    markets = [t.market for t in timelines]
    books = {t.market.market_id: t.final_book for t in timelines}
    return VenueSnapshot(venue=Venue.KALSHI, source="flb_queue_sim", markets=markets, books=books)


def create_queue_sim_runtime(
    timelines: list[MarketTimeline],
    *,
    ledger: PaperLedger | None = None,
    starting_cash: Decimal = Decimal("1000"),
    model_fees: bool = True,
) -> TrackRuntime:
    fee_model = KalshiFeeModel() if model_fees else KalshiFeeModel.zero()
    snapshot = _snapshot_from_timelines(timelines)
    return TrackRuntime.create(
        TRACK,
        {Venue.KALSHI: snapshot},
        ledger=ledger,
        risk_limits=FLB_RISK_LIMITS,
        starting_cash=starting_cash,
        model_fees=model_fees,
        client_factory=_client_factory(fee_model),
        mark_method="conservative",
    )


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------
@dataclass(slots=True)
class FillRecord:
    market_id: str
    ts: datetime
    direction: int
    yes_price: Decimal
    quantity: Decimal
    fee: Decimal
    placement: str
    trade_through: bool
    longshot_band: str | None
    markouts: dict[int, Decimal | None] = field(default_factory=dict)
    settled_pnl: Decimal | None = None


@dataclass(slots=True)
class QueueSimState:
    runtime: TrackRuntime
    params: FlbQueueParameters
    quotes: list[FlbRestingQuote] = field(default_factory=list)
    fills: list[FillRecord] = field(default_factory=list)
    quote_rows: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    dropped_fills: int = 0

    def bump(self, key: str, by: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + by

    def active_quotes(self, market_id: str) -> list[FlbRestingQuote]:
        return [q for q in self.quotes if q.market_id == market_id and q.status == "resting"]

    @property
    def client(self) -> FlbQueueClient:
        client = self.runtime.clients[Venue.KALSHI]
        assert isinstance(client, FlbQueueClient)
        return client


class FlbQueueReplay:
    def __init__(self, timelines: list[MarketTimeline], params: FlbQueueParameters, state: QueueSimState) -> None:
        self.timelines = timelines
        self.params = params
        self.state = state
        self.model = FlbQueueFillModel(params)
        self.prints_seen = 0
        self.alignment: Alignment = timelines[0].alignment if timelines else "time_aligned"

    async def run(self) -> None:
        for timeline in self.timelines:
            await self._replay_market(timeline)
        self._compute_markouts()
        self._compute_settlement()

    async def _replay_market(self, tl: MarketTimeline) -> None:
        market = tl.market
        bookless = tl.alignment == "settled_trades_only" or not tl.books
        current_book: OrderBook | None = None if not bookless else None
        if not bookless:
            current_book = tl.books[0][1] if tl.books else None
        self.state.client.current_books[market.market_id] = current_book or OrderBook(market_id=market.market_id)
        events: list[tuple[datetime, int, Any]] = [(ts, 0, book) for ts, book in tl.books] + [(p.ts, 1, p) for p in tl.prints]
        events.sort(key=lambda e: (e[0], e[1]))
        last_ts: datetime | None = None
        quoted_this_book = False
        for ts, kind, payload in events:
            last_ts = ts
            if kind == 0:
                current_book = payload
                self.state.client.current_books[market.market_id] = payload
                for quote in self.state.active_quotes(market.market_id):
                    self.model.on_book(quote, payload)
                quoted_this_book = False
                # Place (or refresh opportunity) on each new book while longshot.
                if not self.state.active_quotes(market.market_id):
                    self._maybe_quote_from_book(market, payload, ts)
                    quoted_this_book = True
                continue
            print_: Print = payload
            self.prints_seen += 1
            for quote in list(self.state.active_quotes(market.market_id)):
                filled = self.model.on_print(quote, print_)
                if filled > ZERO:
                    await self._book_fill(market, quote, filled, print_)
                if print_.ts >= quote.expires_at:
                    quote.expire(print_.ts)
            if bookless and not self.state.active_quotes(market.market_id):
                self._maybe_quote_from_print(market, print_)
            elif not bookless and current_book is not None and not self.state.active_quotes(market.market_id) and not quoted_this_book:
                # After a quote expires mid-tape, allow re-quote off the latest book.
                self._maybe_quote_from_book(market, current_book, print_.ts)
        if last_ts is not None:
            for quote in self.state.active_quotes(market.market_id):
                quote.expire(max(last_ts, quote.active_from))

    def _maybe_quote_from_book(self, market: Market, book: OrderBook, ts: datetime) -> None:
        summary = self.state.runtime.summary
        summary.candidates += 1
        self.state.bump("book_triggers")
        position = self.state.runtime.ledger.portfolio.get(market.venue, market.market_id)
        total = portfolio_and_resting_cash_at_risk(self.state.runtime.ledger.portfolio, self.state.quotes)
        evaluation = place_flb_maker_quote(
            market,
            book,
            self.params,
            placed_at=ts,
            position=position,
            risk=self.state.runtime.risk,
            total_cash_at_risk=total,
            active_quotes_in_market=len(self.state.active_quotes(market.market_id)),
        )
        self._record_quote(market, evaluation, ts)

    def _maybe_quote_from_print(self, market: Market, print_: Print) -> None:
        summary = self.state.runtime.summary
        summary.candidates += 1
        self.state.bump("print_triggers")
        position = self.state.runtime.ledger.portfolio.get(market.venue, market.market_id)
        total = portfolio_and_resting_cash_at_risk(self.state.runtime.ledger.portfolio, self.state.quotes)
        evaluation = place_flb_maker_quote_from_longshot_print(
            market,
            print_,
            self.params,
            position=position,
            risk=self.state.runtime.risk,
            total_cash_at_risk=total,
            active_quotes_in_market=len(self.state.active_quotes(market.market_id)),
        )
        self._record_quote(market, evaluation, print_.ts)

    def _record_quote(self, market: Market, evaluation: Any, ts: datetime) -> None:
        summary = self.state.runtime.summary
        row = {
            "market": market.market_id,
            "title": market.title,
            "ts": ts.isoformat(),
            "reason": evaluation.reason,
            "placement": evaluation.placement,
            "quote_yes_price": evaluation.yes_price,
            "displayed_at_level": evaluation.displayed_at_level,
            "mid": evaluation.mid,
            "spread": evaluation.spread,
            "longshot_outcome": evaluation.longshot_outcome.value if evaluation.longshot_outcome else None,
            "longshot_price": evaluation.longshot_price,
        }
        if not evaluation.traded or evaluation.quote is None or evaluation.order is None:
            if evaluation.reason != "not_longshot":
                summary.refuse(evaluation.reason)
            else:
                summary.refuse("not_longshot")
            self.state.quote_rows.append(row)
            return
        quote = evaluation.quote
        self.state.quotes.append(quote)
        summary.admitted += 1
        summary.proposed_orders += 1
        self.state.bump("quotes_placed")
        self.state.bump("contracts_quoted", int(quote.quantity))
        row.update({"quantity": quote.quantity, "outcome": quote.outcome.value, "price": quote.price, "bookless": quote.bookless})
        self.state.quote_rows.append(row)
        summary.edges.append(
            {
                "track": TRACK,
                "venue": Venue.KALSHI.value,
                "market": market.market_id,
                "title": market.title,
                "admitted": True,
                "filled": False,
                "reason": "quote_resting",
                "mid": evaluation.mid,
                "placement": evaluation.placement,
                "quote_yes_price": evaluation.yes_price,
                "longshot_price": evaluation.longshot_price,
                "queue_ahead": quote.queue_ahead_initial,
            }
        )

    async def _book_fill(self, market: Market, quote: FlbRestingQuote, filled: Decimal, print_: Print) -> None:
        order = Order(
            venue=market.venue,
            market_id=market.market_id,
            side=Side.BUY,
            quantity=filled,
            outcome=quote.outcome,
            price=quote.price,
            metadata={
                "strategy": TRACK,
                "execution": "maker_fill",
                "placement": quote.placement,
                "fill_ts": print_.ts.isoformat(),
                "trade_through": str(quote.fills[-1].trade_through if quote.fills else False),
                "fill_trade_id": print_.trade_id,
                "fill_model": "conservative_l2_tape" if not quote.bookless else "settled_trade_through_only",
            },
        )
        report = await self.state.runtime.submit(order, edge=None)
        if report is None or not report.fills:
            self.state.dropped_fills += 1
            self.state.bump("fills_dropped_by_risk")
            return
        through = quote.fills[-1].trade_through if quote.fills else False
        for fill in report.fills:
            self.state.fills.append(
                FillRecord(
                    market_id=market.market_id,
                    ts=print_.ts,
                    direction=quote.direction,
                    yes_price=fill.yes_equivalent_price,
                    quantity=fill.quantity,
                    fee=fill.fee,
                    placement=quote.placement,
                    trade_through=through,
                    longshot_band=band_for(quote.longshot_price) if quote.longshot_price is not None else None,
                )
            )
            self.state.bump("fill_events")
            self.state.bump("contracts_filled", int(fill.quantity))
        for row in self.state.runtime.summary.fills[-len(report.fills) :]:
            row.update(
                {
                    "role": "maker",
                    "placement": quote.placement,
                    "filled_at": print_.ts.isoformat(),
                    "trade_through": through,
                    "queue_ahead_initial": quote.queue_ahead_initial,
                    "longshot_band": band_for(quote.longshot_price) if quote.longshot_price is not None else None,
                    "fill_model": "queue_aware",
                }
            )
        for edge in reversed(self.state.runtime.summary.edges):
            if edge["market"] == market.market_id and edge.get("reason") == "quote_resting":
                edge["filled"] = True
                edge["reason"] = "quote_filled"
                break

    def _timeline(self, market_id: str) -> MarketTimeline | None:
        return next((t for t in self.timelines if t.market.market_id == market_id), None)

    def _mid_after(self, market_id: str, ts: datetime, horizon_s: int) -> Decimal | None:
        timeline = self._timeline(market_id)
        if timeline is None or not timeline.books:
            return None
        target = ts.timestamp() + horizon_s
        chosen: Decimal | None = None
        for book_ts, book in timeline.books:
            if book_ts.timestamp() < ts.timestamp():
                continue
            if book.mid_price is None:
                continue
            chosen = book.mid_price
            if book_ts.timestamp() >= target:
                break
        return chosen

    def _compute_markouts(self) -> None:
        for record in self.state.fills:
            for horizon in self.params.markout_horizons_seconds:
                later = self._mid_after(record.market_id, record.ts, horizon)
                record.markouts[horizon] = markout(record.direction, record.yes_price, later) if later is not None else None

    def _compute_settlement(self) -> None:
        for record in self.state.fills:
            timeline = self._timeline(record.market_id)
            if timeline is None or timeline.settlement is None:
                continue
            settle = ONE if timeline.settlement is Outcome.YES else ZERO
            # direction +1 means we are long YES; PnL = direction * (settle - entry) * qty - fee
            record.settled_pnl = (Decimal(record.direction) * record.quantity * (settle - record.yes_price) - record.fee).quantize(Q4)


# --------------------------------------------------------------------------
# Stats / verdicts
# --------------------------------------------------------------------------
def _clustered_mean_se(pairs: list[tuple[str, Decimal, Decimal]]) -> dict[str, Any]:
    """``pairs`` = (market_id, quantity, value_per_contract). Contract-weighted mean + clustered SE."""
    if not pairs:
        return {"n_markets": 0, "contracts": 0.0, "mean": None, "clustered_se": None, "t_stat": None, "effective_n_markets": 0.0}
    by_m: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for mid, qty, val in pairs:
        by_m.setdefault(mid, []).append((qty, val))
    total_q = sum((q for rows in by_m.values() for q, _ in rows), ZERO)
    if total_q <= ZERO:
        return {"n_markets": len(by_m), "contracts": 0.0, "mean": None, "clustered_se": None, "t_stat": None, "effective_n_markets": 0.0}
    mean = sum((q * v for rows in by_m.values() for q, v in rows), ZERO) / total_q
    # Cluster residuals by market
    cluster_sums: list[Decimal] = []
    weights: list[Decimal] = []
    for rows in by_m.values():
        q_m = sum((q for q, _ in rows), ZERO)
        s_m = sum((q * (v - mean) for q, v in rows), ZERO)
        cluster_sums.append(s_m)
        weights.append(q_m)
    n = len(by_m)
    if n < 2 or total_q <= ZERO:
        return {
            "n_markets": n,
            "contracts": float(total_q),
            "mean": float(mean),
            "clustered_se": None,
            "t_stat": None,
            "effective_n_markets": float(n),
        }
    # Var of weighted mean with market clusters: sum(s_m^2) / Q^2 * n/(n-1)
    ss = sum((s * s for s in cluster_sums), ZERO)
    var = (ss / (total_q * total_q)) * Decimal(n) / Decimal(n - 1)
    se = Decimal(math.sqrt(float(var))) if var > ZERO else ZERO
    t = float(mean / se) if se > ZERO else None
    # Kish effective n
    w2 = sum((float(w) ** 2 for w in weights), 0.0)
    n_eff = (float(total_q) ** 2 / w2) if w2 > 0 else float(n)
    return {
        "n_markets": n,
        "contracts": float(total_q),
        "mean": float(mean.quantize(Q4)),
        "clustered_se": float(se.quantize(Q4)) if se is not None else None,
        "t_stat": round(t, 2) if t is not None else None,
        "effective_n_markets": round(n_eff, 2),
    }


def _fill_stats(fills: list[FillRecord], params: FlbQueueParameters) -> dict[str, Any]:
    horizon = max(params.markout_horizons_seconds) if params.markout_horizons_seconds else 300
    settled_pairs = [(f.market_id, f.quantity, (f.settled_pnl / f.quantity) if f.settled_pnl is not None and f.quantity else ZERO) for f in fills if f.settled_pnl is not None]
    # Prefer settlement; else longest markout net of per-contract fee already in settled; for markout use gross markout - fee/qty
    markout_pairs = []
    for f in fills:
        m = f.markouts.get(horizon)
        if m is None:
            continue
        markout_pairs.append((f.market_id, f.quantity, m - (f.fee / f.quantity if f.quantity else ZERO)))
    primary = settled_pairs if settled_pairs else markout_pairs
    primary_label = "settlement" if settled_pairs else f"markout_{horizon}s_net_of_fees"
    stats = _clustered_mean_se(primary)
    tox_pairs = [(f.market_id, f.quantity, f.markouts[horizon]) for f in fills if f.markouts.get(horizon) is not None]
    tox = _clustered_mean_se(tox_pairs)
    toxic = sum(1 for f in fills if (f.markouts.get(horizon) is not None and f.markouts[horizon] < ZERO))  # type: ignore[operator]
    with_m = sum(1 for f in fills if f.markouts.get(horizon) is not None)
    return {
        "primary_scoring": primary_label,
        "net_ev": stats,
        "markout": {str(horizon): tox},
        "toxicity_rate": (toxic / with_m) if with_m else None,
        "fills": len(fills),
        "contracts": float(sum((f.quantity for f in fills), ZERO)),
        "trade_through_fills": sum(1 for f in fills if f.trade_through),
        "markets": len({f.market_id for f in fills}),
        "by_longshot_band": _by_band(fills),
    }


def _by_band(fills: list[FillRecord]) -> dict[str, Any]:
    bands: dict[str, list[FillRecord]] = {}
    for f in fills:
        bands.setdefault(f.longshot_band or "unknown", []).append(f)
    out: dict[str, Any] = {}
    for name, rows in sorted(bands.items()):
        settled = [r for r in rows if r.settled_pnl is not None]
        pnl = sum((r.settled_pnl for r in settled), ZERO) if settled else None
        out[name] = {
            "fills": len(rows),
            "contracts": float(sum((r.quantity for r in rows), ZERO)),
            "settled_pnl": float(pnl.quantize(Q4)) if pnl is not None else None,
            "trade_through": sum(1 for r in rows if r.trade_through),
        }
    return out


def net_ev_verdict(stats: dict[str, Any], params: FlbQueueParameters) -> dict[str, Any]:
    ev = stats["net_ev"]
    base = {
        "question": PRE_REGISTRATION["hypothesis"],
        "scoring": stats["primary_scoring"],
        "net_ev": ev,
        "thresholds": {
            "pass_net_ev": float(params.pass_net_ev),
            "stretch_net_ev": float(params.stretch_net_ev),
            "t_threshold": float(params.t_threshold),
            "min_fills_for_verdict": params.min_fills_for_verdict,
            "min_markets_for_verdict": params.min_markets_for_verdict,
        },
        "stretch_pass": False,
    }
    if stats["fills"] < params.min_fills_for_verdict or ev.get("n_markets", 0) < params.min_markets_for_verdict:
        return {
            "verdict": VERDICT_INSUFFICIENT,
            "reason": (
                f"underpowered: {stats['fills']} fills across {ev.get('n_markets', 0)} markets "
                f"(need >= {params.min_fills_for_verdict} fills, >= {params.min_markets_for_verdict} markets)"
            ),
            "underpowered": True,
            **base,
        }
    mean, t = ev.get("mean"), ev.get("t_stat")
    if mean is None:
        return {"verdict": VERDICT_INSUFFICIENT, "reason": "no scorable fills", "underpowered": True, **base}
    stretch = mean >= float(params.stretch_net_ev) and t is not None and t >= float(params.t_threshold)
    base["stretch_pass"] = stretch
    if mean >= float(params.pass_net_ev) and t is not None and t >= float(params.t_threshold):
        return {
            "verdict": VERDICT_PASS,
            "reason": f"net EV {mean:.4f}/ct (t={t}) meets >= {params.pass_net_ev}¢ bar"
            + (f"; also meets stretch {params.stretch_net_ev}¢" if stretch else ""),
            "underpowered": False,
            **base,
        }
    return {
        "verdict": VERDICT_FAIL,
        "reason": f"net EV {mean:.4f}/ct (t={t}) below pre-registered {params.pass_net_ev}¢ / t>={params.t_threshold} bar",
        "underpowered": False,
        **base,
    }


def adverse_selection_verdict(stats: dict[str, Any], params: FlbQueueParameters) -> dict[str, Any]:
    horizon = str(max(params.markout_horizons_seconds))
    tox = stats["markout"].get(horizon, {})
    base = {
        "question": "Were queue-sim maker fills adversely selected on the longest markout horizon?",
        "horizon_seconds": int(horizon),
        "markout": tox,
        "toxicity_rate": stats.get("toxicity_rate"),
        "thresholds": {"min_fills_for_verdict": params.min_fills_for_verdict, "t_threshold": float(params.t_threshold)},
    }
    n = stats["fills"]
    if n < params.min_fills_for_verdict or tox.get("mean") is None:
        return {"verdict": VERDICT_INSUFFICIENT, "reason": f"{n} fills with markout data below floor or missing mids", **base}
    mean, t = tox["mean"], tox.get("t_stat")
    if mean < 0 and t is not None and t <= -float(params.t_threshold):
        return {"verdict": VERDICT_FAIL, "reason": f"significantly negative markout (mean={mean}, t={t})", **base}
    return {"verdict": VERDICT_PASS, "reason": f"markout not significantly adverse (mean={mean}, t={t})", **base}


# --------------------------------------------------------------------------
# Public entry
# --------------------------------------------------------------------------
async def run_queue_sim(
    timelines: list[MarketTimeline],
    *,
    params: FlbQueueParameters | None = None,
    ledger: PaperLedger | None = None,
    model_fees: bool = True,
    starting_cash: Decimal = Decimal("1000"),
    source_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = params or FlbQueueParameters()
    if not timelines:
        return empty_report(params, source_meta=source_meta, reason="no timelines")
    runtime = create_queue_sim_runtime(timelines, ledger=ledger, starting_cash=starting_cash, model_fees=model_fees)
    state = QueueSimState(runtime=runtime, params=params)
    replay = FlbQueueReplay(timelines, params, state)
    await replay.run()
    # Mark open positions from final books when present
    for tl in timelines:
        book = tl.final_book
        if book.best_bid is not None or book.best_ask is not None:
            runtime.ledger.mark_from_book(Venue.KALSHI, tl.market.market_id, book)
    settled_n = 0
    for tl in timelines:
        if tl.settlement is None:
            continue
        if runtime.ledger.settle(Venue.KALSHI, tl.market.market_id, tl.settlement) is not None:
            settled_n += 1
    summary = runtime.finalize(label="flb_queue_sim")
    stats = _fill_stats(state.fills, params)
    net_v = net_ev_verdict(stats, params)
    adv_v = adverse_selection_verdict(stats, params)
    contracts_quoted = state.counts.get("contracts_quoted", 0)
    fill_rate = (stats["contracts"] / contracts_quoted) if contracts_quoted else None
    ledger_summary = runtime.ledger.summary()
    # Recompute settlement preview from fill records (cleaner than cumulative ledger)
    settled_fill_pnl = sum((f.settled_pnl for f in state.fills if f.settled_pnl is not None), ZERO)
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "kalshi_flb_queue_sim_report",
        "paper_only": True,
        "track": TRACK,
        "parameters": params.as_dict(),
        "pre_registration": PRE_REGISTRATION,
        "honesty_limits": HONESTY_LIMITS,
        "source": source_meta or {},
        "alignment": replay.alignment,
        "timelines": len(timelines),
        "prints_seen": replay.prints_seen,
        "quotes_placed": state.counts.get("quotes_placed", 0),
        "contracts_quoted": contracts_quoted,
        "fill_rate_contracts": fill_rate,
        "dropped_fills_by_risk": state.dropped_fills,
        "counts": dict(sorted(state.counts.items())),
        "refused_by_reason": dict(sorted(summary.refused_by_reason.items())),
        "stats": stats,
        "verdicts": {
            "maker_queue_net_ev": net_v,
            "maker_fills_not_adversely_selected": adv_v,
        },
        "ledger": {k: ledger_summary.get(k) for k in ("starting_cash", "cash", "equity", "realized_pnl", "unrealized_pnl", "total_pnl", "fees_paid", "gross_notional", "max_drawdown", "open_positions", "fills", "mark_method")},
        "settlement": {
            "markets_with_outcome": sum(1 for t in timelines if t.settlement is not None),
            "positions_settled": settled_n,
            "fill_level_settled_pnl": float(settled_fill_pnl.quantize(Q4)) if state.fills else 0.0,
            "note": "Fill-level settled PnL uses paper_settlement_outcome / harvest result; archive-only runs have markouts only.",
        },
        "quotes": [q.as_dict() for q in state.quotes],
        "quote_attempts": state.quote_rows,
        "fills": [
            {
                "market": f.market_id,
                "ts": f.ts.isoformat(),
                "direction": f.direction,
                "yes_price": f.yes_price,
                "quantity": f.quantity,
                "fee": f.fee,
                "placement": f.placement,
                "trade_through": f.trade_through,
                "longshot_band": f.longshot_band,
                "markouts": {str(k): v for k, v in f.markouts.items()},
                "settled_pnl": f.settled_pnl,
            }
            for f in state.fills
        ],
        "summary": summary.as_dict(),
    }
    report["headline"] = _headline(report)
    return report


def empty_report(params: FlbQueueParameters, *, source_meta: dict[str, Any] | None = None, reason: str = "no data") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "kalshi_flb_queue_sim_report",
        "paper_only": True,
        "track": TRACK,
        "parameters": params.as_dict(),
        "pre_registration": PRE_REGISTRATION,
        "honesty_limits": HONESTY_LIMITS,
        "source": source_meta or {},
        "headline": f"Queue sim not run: {reason}",
        "verdicts": {
            "maker_queue_net_ev": {"verdict": VERDICT_INSUFFICIENT, "reason": reason, "underpowered": True},
            "maker_fills_not_adversely_selected": {"verdict": VERDICT_INSUFFICIENT, "reason": reason},
        },
        "stats": {"fills": 0, "contracts": 0.0, "markets": 0},
        "fills": [],
        "quotes": [],
    }


def _headline(report: dict[str, Any]) -> str:
    net = report["verdicts"]["maker_queue_net_ev"]
    adv = report["verdicts"]["maker_fills_not_adversely_selected"]
    stats = report["stats"]
    ev = stats.get("net_ev", {})
    return (
        f"Queue-aware maker sim ({report.get('alignment')}): {stats.get('fills', 0)} fills / "
        f"{stats.get('contracts', 0):.0f} contracts across {stats.get('markets', 0)} markets; "
        f"fill rate {report.get('fill_rate_contracts')}; "
        f"net EV {ev.get('mean')} /ct (t={ev.get('t_stat')}, scoring={stats.get('primary_scoring')}) → {net['verdict']}; "
        f"adverse selection → {adv['verdict']}."
    )


__all__ = [
    "FIXTURE_PATH",
    "HONESTY_LIMITS",
    "PRE_REGISTRATION",
    "TRACK",
    "empty_report",
    "load_fixture_timelines",
    "run_queue_sim",
    "timelines_from_archive",
    "timelines_from_settled",
]
