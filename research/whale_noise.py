"""Make-on-whale, take-on-noise: replay engine, three paper legs, verdicts.

Tracks (all Kalshi tennis, all paper, each with its own risk manager, engine
and :class:`core.ledger.PaperLedger`):

* ``kalshi_whale_noise_combined`` — both legs in one book (the experiment)
* ``kalshi_whale_maker_leg``     — maker leg alone (rest one tick behind a whale lift)
* ``kalshi_noise_taker_leg``     — taker leg alone (fade retail longshot flow)

All three replay the *same* per-market timeline of polled L2 books and public
trade prints, so the comparison "combined vs either leg alone" is apples to
apples: same events, same caps ($25/order, $75/market, $75 daily,
$1,000 collateral), same fee model, same conservative marks.

Timelines come from three sources:

* the committed synthetic fixture (deterministic; CI and tests),
* the self-logged archive written by :mod:`apps.book_logger`
  (``artifacts/books/kalshi/<day>/{books,trades}.jsonl``) — the $0 forward
  tape; the only source on which maker fills are *measured*,
* one live public snapshot plus the recent tape (``--network``): the tape
  precedes the book, so maker fills are **not** simulated there (quotes are
  recorded as resting and the report says ``awaiting_forward_tape``).

Pass criterion (pre-registered): combined paper PnL > PnL of either leg alone,
with at least ``min_fills_for_verdict`` combined fills; otherwise FAIL, or
INSUFFICIENT_DATA when the event stream did not produce enough fills. Fill
rate and toxicity (post-fill markouts against later book mids) are logged for
both legs on every run.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx

from core.ledger import PaperLedger
from core.risk import RiskLimits
from core.types import ONE, ZERO, ExecutionReport, Fill, Market, Order, OrderBook, OrderStatus, Outcome, PriceLevel, Side, Venue
from research.flb import KalshiFeeModel, Q4
from research.flb_expost import (
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT,
    VERDICT_PASS,
    Cluster,
    SettledMarket,
    weighted_cluster_stats,
)
from research.scoreboard import SnapshotClient, TrackRuntime, TrackSummary, VenueSnapshot, _bps, _venue_pnl
from strategies.flb import FLB_RISK_LIMITS, LongshotFadeStrategy, portfolio_cash_at_risk
from strategies.whale_noise import (
    ConservativeQueueModel,
    Print,
    PrintClass,
    RestingQuote,
    WhaleNoiseParameters,
    WhaleRule,
    classify_print,
    is_whale_lift,
    markout,
    reserved_collateral,
    whale_follow_quote,
)
from venues.kalshi.client import KalshiClient

COMBINED = "kalshi_whale_noise_combined"
MAKER_LEG = "kalshi_whale_maker_leg"
TAKER_LEG = "kalshi_noise_taker_leg"
WHALE_NOISE_TRACKS: tuple[str, ...] = (COMBINED, MAKER_LEG, TAKER_LEG)
WHALE_NOISE_PRIMARY = COMBINED
WHALE_NOISE_LABELS = {
    COMBINED: "Kalshi whale-follow maker + noise fade (combined)",
    MAKER_LEG: "Kalshi whale-follow maker leg",
    TAKER_LEG: "Kalshi retail-longshot taker fade leg",
}
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "whale_noise_tape.json"
EXPOST_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "venues" / "kalshi" / "fixtures" / "whale_noise_settled_trades.json"
HARVEST_FILE = "kalshi_tennis_settled_trades.json"
Alignment = Literal["time_aligned", "single_snapshot_after_tape"]
VERDICT_NOT_SIMULATED = "NOT_SIMULATED"


@dataclass(frozen=True, slots=True)
class LegConfig:
    name: str
    maker: bool
    taker: bool


LEGS: dict[str, LegConfig] = {
    COMBINED: LegConfig(COMBINED, maker=True, taker=True),
    MAKER_LEG: LegConfig(MAKER_LEG, maker=True, taker=False),
    TAKER_LEG: LegConfig(TAKER_LEG, maker=False, taker=True),
}


def _now() -> datetime:
    return datetime.now(UTC)


def parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _dec(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return default


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4) if value is not None else None


def series_of(market: Market) -> str | None:
    explicit = market.metadata.get("series_ticker")
    if explicit:
        return str(explicit)
    head = market.market_id.split("-", 1)[0]
    return head or None


# --------------------------------------------------------------------------
# Timelines
# --------------------------------------------------------------------------
@dataclass(slots=True)
class MarketTimeline:
    market: Market
    books: list[tuple[datetime, OrderBook]] = field(default_factory=list)
    prints: list[Print] = field(default_factory=list)
    settlement: Outcome | None = None
    alignment: Alignment = "time_aligned"

    def sort(self) -> None:
        self.books.sort(key=lambda item: item[0])
        self.prints.sort(key=lambda p: p.ts)

    def book_at(self, ts: datetime) -> OrderBook | None:
        latest: OrderBook | None = None
        for book_ts, book in self.books:
            if book_ts <= ts:
                latest = book
            else:
                break
        return latest

    def book_after(self, ts: datetime) -> tuple[datetime, OrderBook] | None:
        for book_ts, book in self.books:
            if book_ts >= ts:
                return book_ts, book
        return None

    @property
    def final_book(self) -> OrderBook:
        return self.books[-1][1] if self.books else OrderBook(market_id=self.market.market_id)

    @property
    def span_seconds(self) -> float:
        stamps = [ts for ts, _ in self.books] + [p.ts for p in self.prints]
        return (max(stamps) - min(stamps)).total_seconds() if stamps else 0.0


def _levels(raw: Any) -> tuple[PriceLevel, ...]:
    out: list[PriceLevel] = []
    for level in raw or []:
        if isinstance(level, dict):
            price, size = level.get("price"), level.get("size")
        else:
            price, size = level[0], level[1]
        p, s = _dec(price), _dec(size)
        if p is not None and s is not None and ZERO <= p <= ONE and s >= ZERO:
            out.append(PriceLevel(p, s))
    return tuple(out)


def _print_from_raw(raw: dict[str, Any], *, default_ts: datetime | None = None) -> Print | None:
    ts = parse_ts(raw.get("ts_venue")) or parse_ts(raw.get("ts")) or parse_ts(raw.get("created_time")) or default_ts
    price = _dec(raw.get("yes_price"), None)
    if price is None:
        price = _dec(raw.get("yes_price_dollars"), None)
    if price is None:
        price = _dec(raw.get("price"), None)
    if price is None and raw.get("yes_price_cents") not in (None, ""):
        price = _dec(raw.get("yes_price_cents")) / Decimal("100")  # type: ignore[operator]
    size = _dec(raw.get("size"), None)
    if size is None:
        size = _dec(raw.get("count_fp"), None)
    if size is None:
        size = _dec(raw.get("count"), None)
    side = str(raw.get("taker_side") or "").lower()
    if ts is None or price is None or size is None or size <= ZERO or side not in ("yes", "no") or not ZERO <= price <= ONE:
        return None
    return Print(
        ts=ts,
        yes_price=price,
        size=size,
        taker_side=side,
        trade_id=str(raw.get("trade_id") or ""),
        block=bool(raw.get("is_block_trade") or raw.get("block") or False),
    )


def load_fixture_timelines(path: Path = FIXTURE_PATH) -> tuple[list[MarketTimeline], dict[str, Any]]:
    """Committed synthetic timeline: markets, books over time, prints, settlement."""
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
        timeline = MarketTimeline(market=market)
        for book in raw.get("books", []):
            ts = parse_ts(book.get("ts"))
            if ts is None:
                continue
            timeline.books.append((ts, OrderBook(market_id=market.market_id, bids=_levels(book.get("bids")), asks=_levels(book.get("asks")), timestamp=ts)))
        for trade in raw.get("trades", []):
            print_ = _print_from_raw(trade)
            if print_ is not None:
                timeline.prints.append(print_)
        outcome = item.get("paper_settlement_outcome") or market.metadata.get("paper_settlement_outcome")
        if outcome in ("yes", "no"):
            timeline.settlement = Outcome(outcome)
        timeline.sort()
        timelines.append(timeline)
    meta = {"source": "fixture", "path": str(path), "comment": payload.get("_comment"), "markets": len(timelines)}
    return timelines, meta


def timelines_from_archive(
    root: Path,
    *,
    series: tuple[str, ...] | None = None,
    tickers: tuple[str, ...] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[list[MarketTimeline], dict[str, Any]]:
    """Replay input from the ``apps.book_logger`` JSONL archive (Kalshi only).

    Markets are filtered by explicit ``tickers`` or by series prefix; fee
    metadata is not in the archive, so the fee model falls back to its
    conservative default (maker fees assumed). Duplicate trade ids (cron-style
    ``--once`` runs re-fetch the newest page) are dropped.
    """
    from research.book_log import JsonlArchive, iter_records

    archive = JsonlArchive(root)
    market_meta: dict[str, dict[str, Any]] = {}
    for record in iter_records(archive.files("kalshi", "markets")):
        market_meta[str(record.get("market_id"))] = record

    def admitted(ticker: str) -> bool:
        if tickers:
            return ticker in tickers
        if series:
            head = ticker.split("-", 1)[0]
            return head in series
        return True

    def in_window(ts: datetime | None) -> bool:
        if ts is None:
            return False
        if since is not None and ts < since:
            return False
        return not (until is not None and ts > until)

    timelines: dict[str, MarketTimeline] = {}
    seen_trades: set[str] = set()
    books = trades = dropped_dupes = 0

    def timeline_for(ticker: str) -> MarketTimeline:
        if ticker not in timelines:
            meta = market_meta.get(ticker, {})
            market = Market(
                venue=Venue.KALSHI,
                market_id=ticker,
                title=str(meta.get("title") or ticker),
                volume=_dec(meta.get("volume"), ZERO) or ZERO,
                metadata={
                    "source": "archive",
                    "series_ticker": meta.get("series_ticker") or ticker.split("-", 1)[0],
                    "event_ticker": meta.get("event_ticker"),
                    "close_time": meta.get("close_time"),
                    "category": "sports",
                },
            )
            timelines[ticker] = MarketTimeline(market=market)
        return timelines[ticker]

    for record in iter_records(archive.files("kalshi", "books")):
        ticker = str(record.get("market_id") or "")
        ts = parse_ts(record.get("ts"))
        if not ticker or not admitted(ticker) or not in_window(ts):
            continue
        assert ts is not None
        timeline_for(ticker).books.append((ts, OrderBook(market_id=ticker, bids=_levels(record.get("bids")), asks=_levels(record.get("asks")), timestamp=ts)))
        books += 1
    for record in iter_records(archive.files("kalshi", "trades")):
        ticker = str(record.get("market_id") or "")
        if not ticker or not admitted(ticker):
            continue
        print_ = _print_from_raw(record)
        if print_ is None or not in_window(print_.ts):
            continue
        key = print_.trade_id or f"{ticker}|{print_.ts.isoformat()}|{print_.yes_price}|{print_.size}|{print_.taker_side}"
        if key in seen_trades:
            dropped_dupes += 1
            continue
        seen_trades.add(key)
        timeline_for(ticker).prints.append(print_)
        trades += 1
    out = []
    for timeline in timelines.values():
        timeline.sort()
        if timeline.books:
            out.append(timeline)
    out.sort(key=lambda t: t.market.market_id)
    meta = {
        "source": "archive",
        "root": str(root),
        "markets": len(out),
        "markets_without_books_dropped": len(timelines) - len(out),
        "books": books,
        "trades": trades,
        "duplicate_trades_dropped": dropped_dupes,
        "series_filter": list(series) if series else None,
        "tickers_filter": list(tickers) if tickers else None,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
    }
    return out, meta


async def fetch_recent_prints(http: httpx.AsyncClient, base_url: str, ticker: str, *, limit: int = 1000, pages: int = 1) -> list[Print]:
    """Public ``GET /markets/trades`` (newest first) for one market."""
    out: list[Print] = []
    cursor: str | None = None
    for _ in range(max(1, pages)):
        params: dict[str, Any] = {"ticker": ticker, "limit": min(max(limit, 1), 1000)}
        if cursor:
            params["cursor"] = cursor
        response = await http.get(f"{base_url}/markets/trades", params=params)
        response.raise_for_status()
        payload = response.json()
        for raw in payload.get("trades", []) or []:
            if isinstance(raw, dict):
                print_ = _print_from_raw({**raw, "ts": raw.get("created_time")})
                if print_ is not None:
                    out.append(print_)
        cursor = payload.get("cursor") or None
        if not cursor:
            break
    return out


async def timelines_from_network(
    *,
    kalshi_env: str | None,
    series: tuple[str, ...],
    limit: int,
    tape_limit: int = 1000,
    tape_pages: int = 1,
    tape_window_seconds: int | None = 1800,
    http: httpx.AsyncClient | None = None,
) -> tuple[list[MarketTimeline], dict[str, Any]]:
    """One public snapshot per market plus its recent tape (single_snapshot_after_tape).

    Only prints inside ``tape_window_seconds`` before the capture are kept as
    triggers: a whale lift from hours ago is not a reason to quote now.
    """
    client = KalshiClient(paper=True, use_fixtures=False, environment=kalshi_env, series_tickers=series, http=http)
    errors: list[str] = []
    timelines: list[MarketTimeline] = []
    captured = _now()
    cutoff = captured - timedelta(seconds=tape_window_seconds) if tape_window_seconds else None
    dropped_old = 0
    try:
        markets = await client.list_markets(limit=limit)
        for market in markets:
            timeline = MarketTimeline(market=market, alignment="single_snapshot_after_tape")
            try:
                book = await client.get_order_book(market)
                timeline.books.append((captured, book))
            except Exception as exc:  # one dead market must not kill the run
                errors.append(f"order_book[{market.market_id}]: {type(exc).__name__}: {exc}")
                continue
            try:
                prints = await fetch_recent_prints(client._http, client.base_url, market.market_id, limit=tape_limit, pages=tape_pages)
            except Exception as exc:
                errors.append(f"trades[{market.market_id}]: {type(exc).__name__}: {exc}")
                prints = []
            if cutoff is not None:
                kept = [p for p in prints if p.ts >= cutoff]
                dropped_old += len(prints) - len(kept)
                prints = kept
            timeline.prints = prints
            timeline.sort()
            timelines.append(timeline)
    finally:
        await client.close()
    meta = {
        "source": "network",
        "kalshi_env": client.environment,
        "series": list(series),
        "markets": len(timelines),
        "trades": sum(len(t.prints) for t in timelines),
        "trades_outside_window_dropped": dropped_old,
        "tape_window_seconds": tape_window_seconds,
        "captured_at": captured.isoformat(),
        "errors": errors,
        "alignment": "single_snapshot_after_tape",
        "note": "The public tape precedes the single book snapshot, so maker fills are not simulated; quotes are recorded as resting.",
    }
    return timelines, meta


# --------------------------------------------------------------------------
# Replay client: taker orders walk the *current* book, maker fills book at the quote
# --------------------------------------------------------------------------
class WhaleNoiseClient(SnapshotClient):
    def __init__(self, snapshot: VenueSnapshot, fee_model: KalshiFeeModel) -> None:
        super().__init__(snapshot, lambda q, p: fee_model.fee(q, p, maker=False), model_fees=fee_model.taker_rate > ZERO or fee_model.maker_rate > ZERO)
        self.fee_model = fee_model
        self.current_books: dict[str, OrderBook] = {}

    def _fee_schedule_for(self, market: Market):  # type: ignore[override]
        return lambda quantity, price: self.fee_model.fee(quantity, price, maker=False, market=market)

    async def place_order(self, order: Order) -> ExecutionReport:
        if not self.paper:
            raise PermissionError("WhaleNoiseClient is paper-only")
        market = self._market_cache.get(order.market_id)
        if market is None or not market.active:
            raise ValueError(f"market {order.market_id} is unavailable")
        book = self.current_books.get(order.market_id, self.snapshot.book(market))
        return await self.place_order_with_book(order, market, book)

    async def place_order_with_book(self, order: Order, market: Market, book: OrderBook) -> ExecutionReport:
        if order.metadata.get("execution") != "maker_fill":
            return await super().place_order_with_book(order, market, book)
        assert order.price is not None
        fill = Fill(
            venue=order.venue,
            market_id=order.market_id,
            order_id=f"paper-maker-{order.metadata.get('quote_id', '')}",
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
        return WhaleNoiseClient(snapshot, fee_model)

    return factory


def create_whale_noise_runtime(
    name: str,
    snapshot: VenueSnapshot,
    *,
    ledger: PaperLedger | None,
    starting_cash: Decimal,
    model_fees: bool,
    risk_limits: RiskLimits | None = None,
) -> TrackRuntime:
    fee_model = KalshiFeeModel() if model_fees else KalshiFeeModel.zero()
    return TrackRuntime.create(
        name,
        {Venue.KALSHI: snapshot},
        ledger=ledger,
        risk_limits=risk_limits or FLB_RISK_LIMITS,
        starting_cash=starting_cash,
        model_fees=model_fees,
        client_factory=_client_factory(fee_model),
        mark_method="conservative",
    )


# --------------------------------------------------------------------------
# Replay engine
# --------------------------------------------------------------------------
@dataclass(slots=True)
class FillRecord:
    leg: str
    role: Literal["maker", "taker"]
    market_id: str
    ts: datetime
    direction: int
    yes_price: Decimal
    quantity: Decimal
    fee: Decimal
    trade_through: bool = False
    markouts: dict[int, Decimal | None] = field(default_factory=dict)


@dataclass(slots=True)
class WhaleEvent:
    market_id: str
    ts: datetime
    direction: int
    size: Decimal
    yes_price: Decimal
    mid_before: Decimal | None
    drift: dict[int, Decimal | None] = field(default_factory=dict)


class _FadeWithReserve(LongshotFadeStrategy):
    """The fade must respect collateral reserved by resting quotes in the same book."""

    def __init__(self, *args: Any, quotes: list[RestingQuote], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._quotes = quotes

    def _reserved_collateral(self) -> Decimal:
        return reserved_collateral(self._quotes)


@dataclass(slots=True)
class LegState:
    config: LegConfig
    runtime: TrackRuntime
    params: WhaleNoiseParameters
    quotes: list[RestingQuote] = field(default_factory=list)
    fills: list[FillRecord] = field(default_factory=list)
    last_taker_at: dict[str, datetime] = field(default_factory=dict)
    dropped_maker_fills: int = 0
    quote_rows: list[dict[str, Any]] = field(default_factory=list)
    fade_rows: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    fade: _FadeWithReserve = field(init=False)

    def __post_init__(self) -> None:
        self.fade = _FadeWithReserve(self.params.fade_parameters(), portfolio=self.runtime.ledger.portfolio, risk=self.runtime.risk, quotes=self.quotes)

    def bump(self, key: str, by: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + by

    def active_quotes(self, market_id: str) -> list[RestingQuote]:
        return [q for q in self.quotes if q.market_id == market_id and q.status == "resting"]

    @property
    def client(self) -> WhaleNoiseClient:
        client = self.runtime.clients[Venue.KALSHI]
        assert isinstance(client, WhaleNoiseClient)
        return client


class WhaleNoiseReplay:
    def __init__(self, timelines: list[MarketTimeline], params: WhaleNoiseParameters, legs: dict[str, LegState]) -> None:
        self.timelines = timelines
        self.params = params
        self.legs = legs
        self.model = ConservativeQueueModel(params)
        self.whale_events: list[WhaleEvent] = []
        self.print_classes: dict[str, int] = {}
        self.prints_seen = 0
        self.fills_not_simulated = 0

    # ----------------------------------------------------------------- run
    async def run(self) -> None:
        for timeline in self.timelines:
            await self._replay_market(timeline)
        for leg in self.legs.values():
            self._compute_markouts(leg)
        self._compute_whale_drift()

    async def _replay_market(self, tl: MarketTimeline) -> None:
        market = tl.market
        series = series_of(market)
        recent: deque[Decimal] = deque(maxlen=self.params.recent_prints_window)
        lookback = timedelta(seconds=float(self.params.whale_lookback_seconds))
        cooldown = timedelta(seconds=float(self.params.taker_cooldown_seconds))
        simulate_fills = tl.alignment == "time_aligned"
        current_book: OrderBook | None = None if simulate_fills else tl.final_book
        for leg in self.legs.values():
            leg.client.current_books[market.market_id] = current_book or OrderBook(market_id=market.market_id)
        events: list[tuple[datetime, int, Any]] = [(ts, 0, book) for ts, book in tl.books] + [(p.ts, 1, p) for p in tl.prints]
        events.sort(key=lambda e: (e[0], e[1]))
        last_whale_ts: datetime | None = None
        last_ts: datetime | None = None
        for ts, kind, payload in events:
            last_ts = ts
            if kind == 0:
                current_book = payload
                for leg in self.legs.values():
                    leg.client.current_books[market.market_id] = payload
                    for quote in leg.active_quotes(market.market_id):
                        self.model.on_book(quote, payload)
                continue
            print_: Print = payload
            self.prints_seen += 1
            # 1. Resting quotes see the print first (fills, then expiry).
            for leg in self.legs.values():
                if not leg.config.maker:
                    continue
                for quote in leg.active_quotes(market.market_id):
                    if simulate_fills:
                        filled = self.model.on_print(quote, print_)
                        if filled > ZERO:
                            await self._book_maker_fill(leg, market, quote, filled, print_)
                    elif quote.active_at(print_.ts):
                        self.fills_not_simulated += 1
                    if print_.ts >= quote.expires_at:
                        quote.expire(print_.ts)
            # 2. Classify and act.
            cls = classify_print(print_, list(recent), self.params, series=series)
            self.print_classes[cls.value] = self.print_classes.get(cls.value, 0) + 1
            if cls is PrintClass.WHALE_LIFT:
                last_whale_ts = print_.ts
                self.whale_events.append(WhaleEvent(market.market_id, print_.ts, print_.direction, print_.size, print_.yes_price, current_book.mid_price if current_book else None))
                for leg in self.legs.values():
                    if leg.config.maker:
                        self._maybe_quote(leg, market, current_book, print_)
            elif cls is PrintClass.RETAIL_LONGSHOT:
                for leg in self.legs.values():
                    if not leg.config.taker:
                        continue
                    leg.runtime.summary.candidates += 1
                    leg.bump("taker_triggers")
                    if last_whale_ts is not None and print_.ts - last_whale_ts <= lookback:
                        leg.runtime.summary.refuse("whale_flow_in_lookback")
                        continue
                    last = leg.last_taker_at.get(market.market_id)
                    if last is not None and print_.ts - last < cooldown:
                        leg.runtime.summary.refuse("taker_cooldown")
                        continue
                    if current_book is None:
                        leg.runtime.summary.refuse("no_book_before_print")
                        continue
                    await self._maybe_fade(leg, market, current_book, print_)
            recent.append(print_.size)
        if last_ts is not None:
            for leg in self.legs.values():
                for quote in leg.active_quotes(market.market_id):
                    quote.expire(max(last_ts, quote.active_from))

    # --------------------------------------------------------------- maker
    def _maybe_quote(self, leg: LegState, market: Market, book: OrderBook | None, whale: Print) -> None:
        summary = leg.runtime.summary
        summary.candidates += 1
        leg.bump("whale_triggers")
        if book is None:
            summary.refuse("no_book_before_print")
            return
        position = leg.runtime.ledger.portfolio.get(market.venue, market.market_id)
        total = portfolio_cash_at_risk(leg.runtime.ledger.portfolio) + reserved_collateral(leg.quotes)
        evaluation = whale_follow_quote(
            market, book, whale, self.params,
            position=position, risk=leg.runtime.risk, total_cash_at_risk=total,
            active_quotes_in_market=len(leg.active_quotes(market.market_id)),
        )
        row = {
            "market": market.market_id,
            "title": market.title,
            "ts": whale.ts.isoformat(),
            "whale_trade_id": whale.trade_id,
            "whale_direction": whale.direction,
            "whale_size": whale.size,
            "whale_yes_price": whale.yes_price,
            "reason": evaluation.reason,
            "reference_touch": evaluation.reference_touch,
            "quote_yes_price": evaluation.yes_price,
            "displayed_at_level": evaluation.displayed_at_level,
            "mid": evaluation.mid,
            "ev_status": evaluation.ev_status,
        }
        if not evaluation.traded or evaluation.quote is None or evaluation.order is None:
            summary.refuse(evaluation.reason)
            leg.quote_rows.append(row)
            return
        quote = evaluation.quote
        leg.quotes.append(quote)
        summary.admitted += 1
        summary.proposed_orders += 1
        leg.bump("quotes_placed")
        leg.bump("contracts_quoted", int(quote.quantity))
        row.update({"quantity": quote.quantity, "outcome": quote.outcome.value, "price": quote.price})
        leg.quote_rows.append(row)
        # Expected edge vs pre-impact mid (reported, not booked): we buy one tick behind the touch.
        if evaluation.mid is not None and evaluation.yes_price is not None:
            summary.admitted_edges.append(Decimal(whale.direction) * (evaluation.mid - evaluation.yes_price))
        summary.edges.append({
            "track": leg.config.name,
            "venue": Venue.KALSHI.value,
            "market": market.market_id,
            "title": market.title,
            "edge_bps": _bps(Decimal(whale.direction) * (evaluation.mid - evaluation.yes_price)) if evaluation.mid is not None and evaluation.yes_price is not None else None,
            "admitted": True,
            "filled": False,
            "reason": "quote_resting",
            "mid": evaluation.mid,
            "fair_value": evaluation.mid,
            "quote_yes_price": evaluation.yes_price,
            "whale_size": whale.size,
            "placement": "behind",
        })

    async def _book_maker_fill(self, leg: LegState, market: Market, quote: RestingQuote, filled: Decimal, print_: Print) -> None:
        order = Order(
            venue=market.venue,
            market_id=market.market_id,
            side=Side.BUY,
            quantity=filled,
            outcome=quote.outcome,
            price=quote.price,
            metadata={
                "strategy": leg.config.name,
                "execution": "maker_fill",
                "placement": "behind",
                "quote_id": f"{quote.trigger_trade_id or 'q'}-{len(quote.fills)}",
                "fill_ts": print_.ts.isoformat(),
                "trade_through": str(quote.trade_through),
                "fill_trade_id": print_.trade_id,
            },
        )
        report = await leg.runtime.submit(order, edge=None)
        if report is None or not report.fills:
            leg.dropped_maker_fills += 1
            leg.bump("maker_fills_dropped_by_risk")
            return
        through = quote.fills[-1].trade_through if quote.fills else False
        for fill in report.fills:
            leg.fills.append(FillRecord(leg.config.name, "maker", market.market_id, print_.ts, 1 if fill.signed_quantity > ZERO else -1, fill.yes_equivalent_price, fill.quantity, fill.fee, through))
            leg.bump("maker_fill_events")
            leg.bump("contracts_filled", int(fill.quantity))
        for row in leg.runtime.summary.fills[-len(report.fills):]:
            row.update({"role": "maker", "placement": "behind", "filled_at": print_.ts.isoformat(), "trade_through": through, "queue_ahead_initial": quote.queue_ahead_initial, "whale_size": quote.whale_size})
        for edge in reversed(leg.runtime.summary.edges):
            if edge["market"] == market.market_id and edge.get("reason") == "quote_resting":
                edge["filled"] = True
                edge["reason"] = "quote_filled"
                break

    # --------------------------------------------------------------- taker
    async def _maybe_fade(self, leg: LegState, market: Market, book: OrderBook, print_: Print) -> None:
        summary = leg.runtime.summary
        evaluation = leg.fade.evaluate(market, book)
        row = {
            "market": market.market_id,
            "title": market.title,
            "ts": print_.ts.isoformat(),
            "trigger_trade_id": print_.trade_id,
            "trigger_size": print_.size,
            "trigger_taker_side": print_.taker_side,
            "trigger_price_paid": print_.taker_price,
            "reason": evaluation.reason,
            "longshot_outcome": evaluation.longshot_outcome.value if evaluation.longshot_outcome else None,
            "longshot_price": evaluation.longshot_price,
            "quote_yes_price": evaluation.quote_yes_price,
            "mid": evaluation.mid,
            "spread": evaluation.spread,
        }
        if not evaluation.traded:
            summary.refuse(f"fade_{evaluation.reason}")
            leg.fade_rows.append(row)
            return
        summary.admitted += 1
        summary.proposed_orders += len(evaluation.orders)
        summary.admitted_edges.append(evaluation.expected_edge_after_adverse_selection or ZERO)
        leg.bump("fades_proposed")
        fills_before = len(summary.fills)
        for order in evaluation.orders:
            order.metadata["strategy"] = leg.config.name
            order.metadata["fill_ts"] = print_.ts.isoformat()
            report = await leg.runtime.submit(order, edge=evaluation.expected_edge_after_adverse_selection)
            if report is None:
                continue
            for fill in report.fills:
                leg.fills.append(FillRecord(leg.config.name, "taker", market.market_id, print_.ts, 1 if fill.signed_quantity > ZERO else -1, fill.yes_equivalent_price, fill.quantity, fill.fee))
                leg.bump("taker_fill_events")
        for fill_row in summary.fills[fills_before:]:
            fill_row.update({"role": "taker", "placement": "take", "filled_at": print_.ts.isoformat(), "trigger_size": print_.size})
        leg.last_taker_at[market.market_id] = print_.ts
        row["paper_fills"] = len(summary.fills) - fills_before
        leg.fade_rows.append(row)
        summary.edges.append({
            "track": leg.config.name,
            "venue": Venue.KALSHI.value,
            "market": market.market_id,
            "title": market.title,
            "edge_bps": _bps(evaluation.expected_edge_after_adverse_selection),
            "admitted": True,
            "filled": len(summary.fills) > fills_before,
            "reason": evaluation.reason,
            "mid": evaluation.mid,
            "fair_value": evaluation.mid,
            "placement": "take",
            "longshot_price": evaluation.longshot_price,
        })

    # ------------------------------------------------------------ markouts
    def _timeline(self, market_id: str) -> MarketTimeline | None:
        return next((t for t in self.timelines if t.market.market_id == market_id), None)

    def _mid_after(self, market_id: str, ts: datetime) -> Decimal | None:
        timeline = self._timeline(market_id)
        if timeline is None or timeline.alignment != "time_aligned":
            return None
        found = timeline.book_after(ts)
        return found[1].mid_price if found is not None else None

    def _compute_markouts(self, leg: LegState) -> None:
        for record in leg.fills:
            for horizon in self.params.markout_horizons_seconds:
                mid = self._mid_after(record.market_id, record.ts + timedelta(seconds=horizon))
                record.markouts[horizon] = markout(record.direction, record.yes_price, mid) if mid is not None else None

    def _compute_whale_drift(self) -> None:
        for event in self.whale_events:
            for horizon in self.params.markout_horizons_seconds:
                mid = self._mid_after(event.market_id, event.ts + timedelta(seconds=horizon))
                event.drift[horizon] = Decimal(event.direction) * (mid - event.mid_before) if (mid is not None and event.mid_before is not None) else None


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
def _markout_table(records: list[FillRecord], horizons: tuple[int, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for horizon in horizons:
        values = [(r.markouts.get(horizon), r.quantity) for r in records if r.markouts.get(horizon) is not None]
        contracts = sum((q for _, q in values), ZERO)
        if not values:
            out[str(horizon)] = {"fills_with_markout": 0, "toxic_fills": 0, "toxicity_rate": None, "mean_markout_cents": None, "contract_weighted_markout_cents": None}
            continue
        toxic = sum(1 for m, _ in values if m is not None and m < ZERO)
        mean = sum((m for m, _ in values), ZERO) / len(values)  # type: ignore[arg-type]
        weighted = sum((m * q for m, q in values), ZERO) / contracts if contracts else None  # type: ignore[operator]
        out[str(horizon)] = {
            "fills_with_markout": len(values),
            "toxic_fills": toxic,
            "toxicity_rate": (Decimal(toxic) / Decimal(len(values))).quantize(Q4),
            "mean_markout_cents": (mean * 100).quantize(Q4),
            "contract_weighted_markout_cents": (weighted * 100).quantize(Q4) if weighted is not None else None,
        }
    return out


def _whale_drift_table(events: list[WhaleEvent], horizons: tuple[int, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {"events": len(events)}
    for horizon in horizons:
        values = [e.drift.get(horizon) for e in events if e.drift.get(horizon) is not None]
        if not values:
            out[str(horizon)] = {"n": 0, "mean_drift_cents": None, "continued": 0, "reverted": 0, "reversal_rate": None}
            continue
        reverted = sum(1 for v in values if v is not None and v < ZERO)
        continued = sum(1 for v in values if v is not None and v > ZERO)
        mean = sum(values, ZERO) / len(values)  # type: ignore[arg-type]
        out[str(horizon)] = {
            "n": len(values),
            "mean_drift_cents": (mean * 100).quantize(Q4),
            "continued": continued,
            "reverted": reverted,
            "reversal_rate": (Decimal(reverted) / Decimal(len(values))).quantize(Q4),
        }
    return out


def leg_metrics(leg: LegState, replay: WhaleNoiseReplay) -> dict[str, Any]:
    params = leg.params
    quotes = leg.quotes
    quoted = sum((q.quantity for q in quotes), ZERO)
    filled = sum((q.filled for q in quotes), ZERO)
    maker_fills = [f for f in leg.fills if f.role == "maker"]
    taker_fills = [f for f in leg.fills if f.role == "taker"]
    first_fill_seconds = [
        (q.fills[0].ts - q.placed_at).total_seconds() for q in quotes if q.fills
    ]
    return {
        "leg": {"maker": leg.config.maker, "taker": leg.config.taker},
        "prints_seen": replay.prints_seen,
        "print_classes": dict(sorted(replay.print_classes.items())),
        "maker": {
            "enabled": leg.config.maker,
            "whale_triggers": leg.counts.get("whale_triggers", 0),
            "quotes_placed": len(quotes),
            "quotes_filled": sum(1 for q in quotes if q.status == "filled"),
            "quotes_partially_filled": sum(1 for q in quotes if q.status == "partially_filled_expired"),
            "quotes_expired_unfilled": sum(1 for q in quotes if q.status == "expired"),
            "quotes_resting_at_end": sum(1 for q in quotes if q.status == "resting"),
            "contracts_quoted": quoted,
            "contracts_filled": filled,
            "fill_rate_contracts": (filled / quoted).quantize(Q4) if quoted else None,
            "fill_rate_quotes": (Decimal(sum(1 for q in quotes if q.filled > ZERO)) / Decimal(len(quotes))).quantize(Q4) if quotes else None,
            "fill_events": len(maker_fills),
            "trade_through_fills": sum(1 for f in maker_fills if f.trade_through),
            "queue_raised_by_later_books": sum(q.queue_raised_by_books for q in quotes),
            "queue_ahead_initial_mean": _q(sum((q.queue_ahead_initial for q in quotes), ZERO) / len(quotes)) if quotes else None,
            "consumed_ahead_total": sum((q.consumed_ahead for q in quotes), ZERO),
            "seconds_to_first_fill_mean": round(sum(first_fill_seconds) / len(first_fill_seconds), 1) if first_fill_seconds else None,
            "fills_dropped_by_risk_at_fill_time": leg.dropped_maker_fills,
            "fills_not_simulated_single_snapshot": replay.fills_not_simulated if leg.config.maker else 0,
            "toxicity": _markout_table(maker_fills, params.markout_horizons_seconds),
            "fill_model": {
                "name": "conservative_queue",
                "queue_ahead": "all displayed size at our level at placement; later snapshots can only raise it" if params.later_arrivals_ahead else "all displayed size at our level at placement",
                "fill_from": "public prints at our level after the queue ahead is consumed; a print through our level fills at most its printed size",
                "never": "no priority from cancels ahead, no fills inside reaction latency, no fills after TTL, no fills without a forward tape",
            },
        },
        "taker": {
            "enabled": leg.config.taker,
            "retail_longshot_triggers": leg.counts.get("taker_triggers", 0),
            "fades_proposed": leg.counts.get("fades_proposed", 0),
            "fill_events": len(taker_fills),
            "contracts_filled": sum((f.quantity for f in taker_fills), ZERO),
            "fill_rate_orders": (Decimal(len(taker_fills)) / Decimal(leg.counts["fades_proposed"])).quantize(Q4) if leg.counts.get("fades_proposed") else None,
            "toxicity": _markout_table(taker_fills, params.markout_horizons_seconds),
        },
        "whale_follow_through": _whale_drift_table(replay.whale_events, params.markout_horizons_seconds),
        "quotes": [q.as_dict() for q in quotes],
        "quote_evaluations": leg.quote_rows,
        "fade_evaluations": leg.fade_rows,
        "cash_at_risk": {
            "positions": _q(portfolio_cash_at_risk(leg.runtime.ledger.portfolio)),
            "resting_orders_reserved": _q(reserved_collateral(quotes)),
            "cap": params.max_total_cash_at_risk,
        },
        "parameters": params.as_dict(),
        "mark_method": leg.runtime.ledger.mark_method,
    }


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------
def combined_verdict(summaries: dict[str, TrackSummary], params: WhaleNoiseParameters, *, alignment: Alignment = "time_aligned") -> dict[str, Any]:
    """Pre-registered pass criterion: combined paper PnL > each leg alone."""
    combined, maker, taker = summaries[COMBINED], summaries[MAKER_LEG], summaries[TAKER_LEG]

    def pnl(summary: TrackSummary) -> Decimal:
        return Decimal(str(summary.ledger.get("total_pnl", "0")))

    c, m, t = pnl(combined), pnl(maker), pnl(taker)
    base = {
        "question": "Does running the whale-follow maker leg and the retail-longshot taker fade in one book beat either leg alone (paper PnL after fees, conservative marks)?",
        "combined_pnl": c,
        "maker_leg_pnl": m,
        "taker_leg_pnl": t,
        "sum_of_legs_pnl": m + t,
        "interaction_pnl": c - (m + t),
        "combined_fills": combined.paper_fills,
        "maker_leg_fills": maker.paper_fills,
        "taker_leg_fills": taker.paper_fills,
        "thresholds": {"min_fills_for_verdict": params.min_fills_for_verdict, "rule": "combined_pnl > max(maker_leg_pnl, taker_leg_pnl)"},
    }
    if alignment != "time_aligned":
        return {"verdict": VERDICT_NOT_SIMULATED, "reason": "single snapshot after the tape: maker fills are not simulated; capture a forward tape with apps.book_logger and replay with --archive", **base}
    if combined.paper_fills < params.min_fills_for_verdict or maker.proposed_orders == 0 or taker.proposed_orders == 0:
        return {"verdict": VERDICT_INSUFFICIENT, "reason": f"need >= {params.min_fills_for_verdict} combined fills and at least one order on each leg", **base}
    if c > max(m, t):
        return {"verdict": VERDICT_PASS, "reason": "combined book out-earned both single legs on the same event stream", **base}
    return {"verdict": VERDICT_FAIL, "reason": "combined book did not beat the better single leg", **base}


def toxicity_verdict(summary: TrackSummary, params: WhaleNoiseParameters) -> dict[str, Any]:
    """Informational: are maker fills adversely selected at the longest horizon?"""
    horizon = str(max(params.markout_horizons_seconds))
    tox = summary.metrics.get("maker", {}).get("toxicity", {}).get(horizon, {})
    n = int(tox.get("fills_with_markout") or 0)
    base = {"question": f"Do maker fills show negative {horizon}s markouts (adverse selection) on average?", "horizon_seconds": int(horizon), **tox}
    if n < params.min_fills_for_verdict:
        return {"verdict": VERDICT_INSUFFICIENT, "reason": f"{n} maker fills with a {horizon}s markout (< {params.min_fills_for_verdict})", **base}
    mean = tox.get("contract_weighted_markout_cents")
    if mean is not None and mean < ZERO:
        return {"verdict": VERDICT_FAIL, "reason": "maker fills were adversely selected (negative contract-weighted markout)", **base}
    return {"verdict": VERDICT_PASS, "reason": "maker fills were not adversely selected on average", **base}


# --------------------------------------------------------------------------
# Ex post: is whale flow +EV, is fading retail longshot flow +EV (settled tennis)
# --------------------------------------------------------------------------
def whale_flow_expost(
    markets: list[SettledMarket],
    params: WhaleNoiseParameters,
    *,
    fee_model: KalshiFeeModel | None = None,
    min_markets: int = 10,
    min_contracts: float = 1000.0,
    t_threshold: float = 2.0,
) -> dict[str, Any]:
    """Taker return of whale-class prints vs retail prints in settled tennis markets.

    Same market-clustered accounting as :mod:`research.flb_expost`; prints are
    classified with the same rolling-median rule the live replay uses, so
    ``whale`` here is exactly the size class the maker leg follows.
    """
    fee_model = fee_model or KalshiFeeModel()
    clusters: dict[str, dict[str, Cluster]] = {"whale": {}, "retail_longshot": {}, "retail_other": {}, "block": {}}
    for market in markets:
        proxy = market.as_market()
        rule: WhaleRule = params.rule_for(market.series_ticker)
        recent: deque[Decimal] = deque(maxlen=params.recent_prints_window)
        for trade in sorted(market.trades, key=lambda t: t.created_time):
            price = trade.taker_price
            if not ZERO < price < ONE:
                continue
            print_ = Print(ts=trade.created_time, yes_price=trade.yes_price, size=trade.count, taker_side=trade.taker_side)
            if is_whale_lift(print_, list(recent), rule):
                key = "whale"
            elif price < params.longshot_threshold:
                key = "retail_longshot"
            else:
                key = "retail_other"
            recent.append(trade.count)
            cluster = clusters[key].setdefault(market.ticker, Cluster())
            won = trade.taker_side == market.result
            count = float(trade.count)
            cluster.contracts += count
            cluster.trades += 1
            cluster.wins += count if won else 0.0
            cluster.taker_gross += count * ((1.0 - float(price)) if won else -float(price))
            cluster.taker_fee += count * float(fee_model.per_contract(price, maker=False, market=proxy))
            cluster.maker_fee += count * float(fee_model.per_contract(price, maker=True, market=proxy))
            cluster.taker_stake += count * float(price)
            cluster.maker_collateral += count * (1.0 - float(price))
    table = {key: weighted_cluster_stats(list(group.values())) for key, group in clusters.items()}
    whale = table["whale"]
    retail = table["retail_longshot"]
    thresholds = {"min_markets": min_markets, "min_contracts": min_contracts, "t_stat": t_threshold}

    def enough(stats: dict[str, Any]) -> bool:
        return stats.get("n_markets", 0) >= min_markets and stats.get("contracts", 0.0) >= min_contracts

    if not enough(whale):
        whale_v = {"verdict": VERDICT_INSUFFICIENT, "reason": f"whale-class prints need >= {min_markets} markets and >= {min_contracts:g} contracts", "ev_status": "unverified"}
    else:
        net, se = whale["taker_net_per_contract"], whale.get("clustered_se")
        t = net / se if se else None
        if net > 0 and t is not None and t >= t_threshold:
            whale_v = {"verdict": VERDICT_PASS, "reason": "whale-class takers earned a positive net return per contract, significant at the market-cluster level", "ev_status": "verified_expost", "t_stat_taker_net": round(t, 2)}
        elif net < 0 and t is not None and t <= -t_threshold:
            whale_v = {"verdict": VERDICT_FAIL, "reason": "whale-class takers lost significantly: this size class is not +EV in the sample", "ev_status": "refuted_expost", "t_stat_taker_net": round(t, 2)}
        else:
            whale_v = {"verdict": VERDICT_FAIL, "reason": f"whale-class taker return not significantly positive (t={round(t, 2) if t is not None else None})", "ev_status": "unverified", "t_stat_taker_net": round(t, 2) if t is not None else None}
    whale_v.update({"question": "Do whale-class prints (the size class the maker leg follows) earn a positive taker return after fees in settled tennis markets?", "whale": whale, "thresholds": thresholds})
    if not enough(retail):
        fade_v = {"verdict": VERDICT_INSUFFICIENT, "reason": "retail longshot prints need enough markets and contracts"}
    else:
        net, se = retail["maker_net_per_contract"], retail.get("clustered_se")
        t = net / se if se else None
        if net > 0 and t is not None and t >= t_threshold:
            fade_v = {"verdict": VERDICT_PASS, "reason": "the counterparty of retail longshot flow earned a positive net return per contract", "t_stat_maker_net": round(t, 2)}
        else:
            fade_v = {"verdict": VERDICT_FAIL, "reason": f"fading retail longshot flow was not significantly positive after fees (t={round(t, 2) if t is not None else None})", "t_stat_maker_net": round(t, 2) if t is not None else None}
    fade_v.update({"question": "Did the maker side of retail longshot prints (<20c, below whale size) earn a positive return after maker fees?", "retail_longshot": retail, "thresholds": thresholds})
    return {
        "status": "measured_from_settled_trades" if markets else "no_settled_trades",
        "markets": len(markets),
        "trades": sum(len(m.trades) for m in markets),
        "contracts": round(sum(float(t.count) for m in markets for t in m.trades), 2),
        "series": sorted({m.series_ticker for m in markets}),
        "classes": table,
        "verdicts": {"whale_flow_ev_positive": whale_v, "retail_longshot_fade_ev_positive": fade_v},
        "whale_rule": params.default_rule.as_dict(),
    }


def not_measured_expost(reason: str) -> dict[str, Any]:
    return {
        "status": "not_measured",
        "reason": reason,
        "verdicts": {
            "whale_flow_ev_positive": {"verdict": VERDICT_INSUFFICIENT, "reason": reason, "ev_status": "unverified"},
            "retail_longshot_fade_ev_positive": {"verdict": VERDICT_INSUFFICIENT, "reason": reason},
        },
        "how_to_measure": "python -m apps.measure_whale_noise --network --kalshi-env prod --harvest-trades",
    }


# --------------------------------------------------------------------------
# Entry point used by the CLI and tests
# --------------------------------------------------------------------------
async def run_whale_noise_tracks(
    timelines: list[MarketTimeline],
    *,
    params: WhaleNoiseParameters | None = None,
    ledgers: dict[str, PaperLedger] | None = None,
    starting_cash: Decimal = Decimal("1000"),
    model_fees: bool = True,
    risk_limits: RiskLimits | None = None,
    cycle_label: str = "",
    source: str = "fixture",
) -> tuple[list[TrackSummary], dict[str, PaperLedger], WhaleNoiseReplay]:
    """Replay ``timelines`` through the three legs; returns summaries, ledgers, replay."""
    params = params or WhaleNoiseParameters()
    ledgers = ledgers or {}
    snapshot = VenueSnapshot(
        venue=Venue.KALSHI,
        source=source,
        markets=[t.market for t in timelines],
        books={t.market.market_id: t.final_book for t in timelines},
    )
    legs: dict[str, LegState] = {}
    for name, config in LEGS.items():
        runtime = create_whale_noise_runtime(name, snapshot, ledger=ledgers.get(name), starting_cash=starting_cash, model_fees=model_fees, risk_limits=risk_limits)
        runtime.summary.label = WHALE_NOISE_LABELS[name]
        legs[name] = LegState(config, runtime, params)
    replay = WhaleNoiseReplay(timelines, params, legs)
    await replay.run()
    alignment: Alignment = "time_aligned" if all(t.alignment == "time_aligned" for t in timelines) else "single_snapshot_after_tape"
    label = cycle_label or f"whale_noise:{source}:{_now().isoformat()}"
    summaries: list[TrackSummary] = []
    for name, leg in legs.items():
        leg.runtime.summary.metrics.update(leg_metrics(leg, replay))
        leg.runtime.finalize(label=label)
        _attach_fill_markouts(leg)
        leg.runtime.summary.metrics["venue_pnl"] = _venue_pnl(leg.runtime.ledger)
        leg.runtime.summary.metrics["snapshot"] = {Venue.KALSHI.value: {"source": source, "markets": len(timelines), "alignment": alignment, "errors": []}}
        leg.runtime.summary.metrics["alignment"] = alignment
        leg.runtime.summary.metrics["settlement_preview"] = _settlement_preview(leg, timelines)
        leg.runtime.summary.settlement_risk_flag = False
        summaries.append(leg.runtime.summary)
    by_track = {s.track: s for s in summaries}
    verdict = combined_verdict(by_track, params, alignment=alignment)
    for summary in summaries:
        summary.metrics["combined_vs_legs"] = verdict
        summary.metrics["maker_toxicity_verdict"] = toxicity_verdict(summary, params) if summary.metrics["maker"]["enabled"] else None
    legs[COMBINED].runtime.summary.notes = (
        "The experiment. Whale-class lifts (large non-block aggressive prints) trigger a same-direction "
        "resting bid one tick behind the touch, filled only by the conservative queue model; retail longshot "
        "prints with no whale in the lookback trigger the taker fade of the favourite. One ledger, paper caps "
        "$25/order, $75/market, $75 daily, $1,000 collateral, conservative marks."
    )
    legs[MAKER_LEG].runtime.summary.notes = (
        "Control: maker leg alone. Fill rate and markouts come from public prints after placement; a whale's "
        "own print never fills us and cancels ahead of us are never assumed."
    )
    legs[TAKER_LEG].runtime.summary.notes = (
        "Control: taker leg alone. Fades retail longshot flow at the touch (taker fee), skipped whenever a "
        "whale print hit the market inside the lookback window."
    )
    return summaries, {name: leg.runtime.ledger for name, leg in legs.items()}, replay


def _attach_fill_markouts(leg: LegState) -> None:
    """Copy per-fill markouts onto the scoreboard fill rows (same order as booked)."""
    rows = leg.runtime.summary.fills
    records = leg.fills
    if len(rows) != len(records):
        return
    for row, record in zip(rows, records, strict=True):
        row["markout_cents"] = {str(h): (_q(v * 100) if v is not None else None) for h, v in record.markouts.items()}
        longest = record.markouts.get(max(record.markouts)) if record.markouts else None
        row["toxic_at_longest_horizon"] = (longest < ZERO) if longest is not None else None


def _settlement_preview(leg: LegState, timelines: list[MarketTimeline]) -> dict[str, Any]:
    outcomes = {t.market.market_id: t.settlement for t in timelines if t.settlement is not None}
    scored = 0
    pnl = ZERO
    for record in leg.fills:
        outcome = outcomes.get(record.market_id)
        if outcome is None:
            continue
        settle = ONE if outcome is Outcome.YES else ZERO
        scored += 1
        pnl += Decimal(record.direction) * record.quantity * (settle - record.yes_price) - record.fee
    return {
        "scored_fills": scored,
        "hypothetical_pnl_at_fixture_settlement": pnl.quantize(Q4),
        "note": "Only fixture markets carry paper_settlement_outcome; archive and network fills are not scored.",
    }
