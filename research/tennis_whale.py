"""Tennis whale copy with lag: public tape -> whales -> lagged paper copies -> EV / CLV.

Data ($0, unauthenticated)
--------------------------
* Gamma ``/markets?tag_id=864`` (Tennis): resolved markets (``outcomePrices``,
  ``closedTime``) and open ones (``volume24hr``), with fee type and tick size.
* Data API ``/trades?market=<conditionId>&takerOnly=true``: the taker tape with
  ``proxyWallet``, side, outcome, price, size and a second-resolution timestamp.
  This is what makes wallets identifiable at all. Kalshi's public tape carries
  no account identity, so a Kalshi leg of this experiment is *not identifiable*
  even though Kalshi lists tennis series; the report records both facts.

Measurement
-----------
Prints are replayed in time order. :class:`strategies.tennis_whale_copy.WhaleTracker`
qualifies whales walk-forward (only history before a print counts), so the
copies are out-of-sample with respect to whale selection. Every signal is copied
at each lag through a per-lag :class:`core.ledger.PaperLedger` (one paper track
per lag) at the first *executed* print on the market at or after
``signal + lag``, plus slippage and the venue taker fee. Copies on resolved
markets are settled in the ledger; open positions are marked at the last print.

Two statistics per lag, both as return on stake per copy, equal-weighted across
copies and bootstrapped by **market cluster** (every copy in a market shares one
outcome and one closing line): settlement ROI after fees, and closing-line value
(CLV) ROI after fees, where the closing line is the last print before the
market closed. Confidence intervals are Bonferroni-corrected across the lags.

The pre-registered pass rule is *CI lower bound > 0 for settlement ROI or CLV ROI
at one or more lags*; the kill rule is *CI upper bound < 0 for both statistics at
every lag with sufficient data*. Neither is ever inferred from an empty tape:
no tennis markets, or no wallet qualifying as a whale, is an honest
``INSUFFICIENT_DATA`` with zero copies.
"""

from __future__ import annotations

import asyncio
import json
import random
from bisect import bisect_left
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from core.ledger import PaperLedger
from core.risk import RiskLimits
from core.types import ONE, ZERO, ExecutionReport, Fill, Market, Order, OrderStatus, Outcome, Side, Venue
from research.scoreboard import SnapshotClient, TrackRuntime, TrackSummary, VenueSnapshot
from strategies.tennis_whale_copy import (
    COPY_RISK_LIMITS,
    CopyDecision,
    CopyParameters,
    TapePrint,
    WhaleTracker,
    copy_decision,
    lag_label,
    track_for_lag,
)
from venues.paper import FeeSchedule
from venues.polymarket.fees import polymarket_taker_fee, taker_fee_rate

GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
KALSHI_PUBLIC_URL = "https://api.elections.kalshi.com/trade-api/v2"
TENNIS_TAG_ID = 864
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "tennis_whale_tape.json"
HARVEST_FILE = "polymarket_tennis_tape.json"
COPIES_FILE = "tennis_whale_copies_latest.json"
TAPE_SCHEMA = "1.0.0"
REPORT_SCHEMA = "1.0.0"
REPORT_KIND = "tennis_whale_copy_report"
TRACK_FAMILY = "tennis_copy"
DEFAULT_LAGS: tuple[int, ...] = (30, 120, 600)
VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_INSUFFICIENT = "INSUFFICIENT_DATA"
VERDICT_NOT_IDENTIFIABLE = "NOT_IDENTIFIABLE"
Q4 = Decimal("0.0001")
Q2 = Decimal("0.01")
DEFAULT_TICK = Decimal("0.01")
_KALSHI_TENNIS_HINTS = ("tennis", "atp", "wta", "us open", "wimbledon", "roland garros", "australian open", "davis cup")


def _now() -> datetime:
    return datetime.now(UTC)


def _dec(value: Any, default: Decimal = ZERO) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return default


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=UTC)
    text = text.replace(" ", "T").replace("Z", "+00:00")
    if text.endswith("+00"):
        text += ":00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


# --------------------------------------------------------------------------
# Tape model
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TennisMarket:
    market_id: str
    question: str
    event_slug: str = ""
    yes_token_id: str = ""
    no_token_id: str = ""
    market_type: str = ""
    fee_type: str | None = None
    fees_enabled: bool = False
    tick_size: Decimal = DEFAULT_TICK
    closed: bool = False
    closed_at: datetime | None = None
    resolved_outcome: Outcome | None = None
    volume: Decimal = ZERO
    source: str = "fixture"

    @property
    def taker_fee_rate(self) -> Decimal:
        return taker_fee_rate(self.fee_type, self.fees_enabled)

    def as_market(self) -> Market:
        return Market(
            venue=Venue.POLYMARKET,
            market_id=self.market_id,
            title=self.question,
            active=True,  # replay: every print happened while the market was open
            volume=self.volume,
            yes_token_id=self.yes_token_id or None,
            no_token_id=self.no_token_id or None,
            metadata={
                "source": self.source,
                "category": "sports",
                "sport": "tennis",
                "event_slug": self.event_slug,
                "market_type": self.market_type,
                "fee_type": self.fee_type,
                "fees_enabled": self.fees_enabled,
                "taker_fee_rate": str(self.taker_fee_rate),
                "tick_size": str(self.tick_size),
                "closed": self.closed,
                "closed_at": self.closed_at.isoformat() if self.closed_at else None,
                "resolved_outcome": self.resolved_outcome.value if self.resolved_outcome else None,
            },
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "event_slug": self.event_slug,
            "yes_token_id": self.yes_token_id,
            "no_token_id": self.no_token_id,
            "market_type": self.market_type,
            "fee_type": self.fee_type,
            "fees_enabled": self.fees_enabled,
            "tick_size": str(self.tick_size),
            "closed": self.closed,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "resolved_outcome": self.resolved_outcome.value if self.resolved_outcome else None,
            "volume": str(self.volume),
            "source": self.source,
        }


@dataclass(slots=True)
class TennisTape:
    markets: dict[str, TennisMarket]
    prints: list[TapePrint]
    meta: dict[str, Any] = field(default_factory=dict)
    _by_market: dict[str, list[TapePrint]] = field(default_factory=dict, repr=False)
    _times: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.prints.sort(key=lambda p: (p.timestamp, p.market_id, p.tx))
        for tape_print in self.prints:
            self._by_market.setdefault(tape_print.market_id, []).append(tape_print)
        self._times = {mid: [p.timestamp.timestamp() for p in rows] for mid, rows in self._by_market.items()}

    @property
    def source(self) -> str:
        return str(self.meta.get("source") or "fixture")

    def prints_for(self, market_id: str) -> list[TapePrint]:
        return self._by_market.get(market_id, [])

    def first_print_at_or_after(self, market_id: str, when: datetime) -> TapePrint | None:
        rows = self._by_market.get(market_id)
        if not rows:
            return None
        index = bisect_left(self._times[market_id], when.timestamp())
        return rows[index] if index < len(rows) else None

    def last_print_before(self, market_id: str, when: datetime | None) -> TapePrint | None:
        rows = self._by_market.get(market_id)
        if not rows:
            return None
        if when is None:
            return rows[-1]
        index = bisect_left(self._times[market_id], when.timestamp())
        return rows[index - 1] if index > 0 else None

    def wallets(self) -> set[str]:
        return {p.wallet for p in self.prints}


def _market_from_record(item: dict[str, Any], *, source: str) -> TennisMarket | None:
    market_id = str(item.get("market_id") or item.get("conditionId") or "")
    if not market_id:
        return None
    tokens = _json_list(item.get("clobTokenIds")) or [item.get("yes_token_id"), item.get("no_token_id")]
    outcome_prices = _json_list(item.get("outcomePrices"))
    resolved = item.get("resolved_outcome")
    if resolved is None and item.get("umaResolutionStatus") == "resolved" and len(outcome_prices) >= 2:
        yes_price, no_price = _dec(outcome_prices[0]), _dec(outcome_prices[1])
        if yes_price == ONE and no_price == ZERO:
            resolved = "yes"
        elif yes_price == ZERO and no_price == ONE:
            resolved = "no"
    events = item.get("events") or []
    event_slug = item.get("event_slug") or (events[0].get("slug") if events and isinstance(events[0], dict) else "") or ""
    fees_enabled = item.get("fees_enabled", item.get("feesEnabled"))
    closed_at = _dt(item.get("closed_at") or item.get("closedTime"))
    return TennisMarket(
        market_id=market_id,
        question=str(item.get("question") or market_id),
        event_slug=str(event_slug),
        yes_token_id=str(tokens[0] or "") if tokens else "",
        no_token_id=str(tokens[1] or "") if len(tokens) > 1 else "",
        market_type=str(item.get("market_type") or item.get("sportsMarketType") or ""),
        fee_type=item.get("fee_type", item.get("feeType")),
        fees_enabled=bool(fees_enabled) if fees_enabled is not None else False,
        tick_size=_dec(item.get("tick_size") or item.get("orderPriceMinTickSize"), DEFAULT_TICK) or DEFAULT_TICK,
        closed=bool(item.get("closed", False)) or resolved is not None,
        closed_at=closed_at,
        resolved_outcome=Outcome(resolved) if resolved in ("yes", "no") else None,
        volume=_dec(item.get("volume") or item.get("volumeNum")),
        source=source,
    )


def _print_from_record(item: dict[str, Any]) -> TapePrint | None:
    market_id = str(item.get("market") or item.get("conditionId") or "")
    wallet = str(item.get("wallet") or item.get("proxyWallet") or "").lower()
    when = _dt(item.get("timestamp"))
    if not market_id or not wallet or when is None:
        return None
    side_raw = str(item.get("side") or "").lower()
    if side_raw not in ("buy", "sell"):
        return None
    outcome_index = item.get("outcome_index", item.get("outcomeIndex"))
    if outcome_index not in (0, 1, "0", "1"):
        return None
    price, size = _dec(item.get("price")), _dec(item.get("size"))
    if size <= ZERO or not ZERO <= price <= ONE:
        return None
    return TapePrint(
        market_id=market_id,
        wallet=wallet,
        side=Side(side_raw),
        outcome=Outcome.YES if int(outcome_index) == 0 else Outcome.NO,
        price=price,
        size=size,
        timestamp=when,
        tx=str(item.get("tx") or item.get("transactionHash") or ""),
    )


def tape_from_payload(payload: dict[str, Any]) -> TennisTape:
    meta = dict(payload.get("meta") or {})
    source = str(meta.get("source") or "fixture")
    markets: dict[str, TennisMarket] = {}
    for item in payload.get("markets") or []:
        market = _market_from_record(item, source=source)
        if market is not None:
            markets[market.market_id] = market
    prints = [p for item in payload.get("trades") or [] if (p := _print_from_record(item)) is not None and p.market_id in markets]
    meta.setdefault("source", source)
    return TennisTape(markets=markets, prints=prints, meta=meta)


def load_tape(path: Path = FIXTURE_PATH) -> TennisTape:
    return tape_from_payload(json.loads(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# Harvest (network, public endpoints only)
# --------------------------------------------------------------------------
async def kalshi_tennis_listing(http: httpx.AsyncClient, *, base_url: str = KALSHI_PUBLIC_URL) -> dict[str, Any]:
    """Does Kalshi list tennis at all? (Its public tape carries no account identity either way.)"""
    try:
        response = await http.get(f"{base_url}/series", params={"category": "Sports", "limit": 500})
        response.raise_for_status()
        series = response.json().get("series") or []
    except (httpx.HTTPError, ValueError) as exc:
        return {"checked": False, "error": f"{type(exc).__name__}: {exc}", "tennis_series": [], "whale_identifiable": False}
    tennis = [
        {"ticker": s.get("ticker"), "title": s.get("title")}
        for s in series
        if isinstance(s, dict) and any(h in f"{s.get('title', '')} {s.get('ticker', '')}".lower() for h in _KALSHI_TENNIS_HINTS)
    ]
    return {
        "checked": True,
        "sports_series": len(series),
        "tennis_series": tennis[:50],
        "tennis_series_count": len(tennis),
        "whale_identifiable": False,
        "reason": "Kalshi public /markets/trades exposes taker_side, price and count only; no account identity, so whales cannot be tracked from public data.",
    }


async def harvest_tennis_tape(
    *,
    resolved_markets: int = 150,
    open_markets: int = 40,
    min_volume: Decimal = Decimal("2000"),
    max_trades_per_market: int = 4000,
    page_size: int = 1000,
    max_listing_pages: int = 20,  # Gamma /markets answers 422 beyond offset ~2000
    tag_id: int = TENNIS_TAG_ID,
    request_pause: float = 0.05,
    check_kalshi: bool = True,
    http: httpx.AsyncClient | None = None,
    gamma_url: str = GAMMA_URL,
    data_api_url: str = DATA_API_URL,
    kalshi_url: str = KALSHI_PUBLIC_URL,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Public Gamma + Data API reads; returns the tape payload (``tape_from_payload`` shape)."""
    client = http or httpx.AsyncClient(timeout=timeout)
    owns = http is None
    requests = 0
    errors: list[str] = []
    started = _now()

    async def get(url: str, **params: Any) -> Any:
        nonlocal requests
        requests += 1
        response = await client.get(url, params=params)
        response.raise_for_status()
        if request_pause:
            await asyncio.sleep(request_pause)
        return response.json()

    try:
        listed: dict[str, dict[str, Any]] = {}
        page = 0
        while len(listed) < resolved_markets and page < max_listing_pages:
            try:
                rows = await get(
                    f"{gamma_url}/markets", closed="true", tag_id=tag_id, order="closedTime",
                    ascending="false", limit=100, offset=page * 100,
                )
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"resolved_listing[page {page}]: {type(exc).__name__}: {exc}")
                break
            if not isinstance(rows, list) or not rows:
                break
            for item in rows:
                if not isinstance(item, dict) or item.get("umaResolutionStatus") != "resolved":
                    continue
                if _dec(item.get("volumeNum") or item.get("volume")) < min_volume:
                    continue
                market = _market_from_record(item, source="network")
                if market is not None and market.resolved_outcome is not None:
                    listed.setdefault(market.market_id, {**item, "_kind": "resolved"})
                if len(listed) >= resolved_markets:
                    break
            page += 1
        if open_markets > 0:
            try:
                rows = await get(
                    f"{gamma_url}/markets", closed="false", active="true", tag_id=tag_id,
                    order="volume24hr", ascending="false", limit=min(open_markets, 100),
                )
                for item in rows if isinstance(rows, list) else []:
                    if isinstance(item, dict) and item.get("conditionId"):
                        listed.setdefault(str(item["conditionId"]), {**item, "_kind": "open"})
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"open_listing: {type(exc).__name__}: {exc}")

        trades: list[dict[str, Any]] = []
        truncated = 0
        per_market_counts: dict[str, int] = {}
        for market_id, item in listed.items():
            collected: list[dict[str, Any]] = []
            offset = 0
            while len(collected) < max_trades_per_market:
                try:
                    rows = await get(
                        f"{data_api_url}/trades", market=market_id, limit=min(page_size, max_trades_per_market - len(collected)),
                        offset=offset, takerOnly="true",
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(f"trades[{market_id}]: {type(exc).__name__}: {exc}")
                    break
                if not isinstance(rows, list) or not rows:
                    break
                collected.extend(r for r in rows if isinstance(r, dict))
                if len(rows) < min(page_size, max_trades_per_market - (len(collected) - len(rows))):
                    break
                offset += len(rows)
            if len(collected) >= max_trades_per_market:
                truncated += 1
            per_market_counts[market_id] = len(collected)
            for row in collected:
                trades.append(
                    {
                        "market": market_id,
                        "wallet": str(row.get("proxyWallet") or "").lower(),
                        "side": row.get("side"),
                        "outcome_index": row.get("outcomeIndex"),
                        "price": str(_dec(row.get("price"))),
                        "size": str(_dec(row.get("size"))),
                        "timestamp": int(_dec(row.get("timestamp"))),
                        "tx": row.get("transactionHash") or "",
                    }
                )
        kalshi = await kalshi_tennis_listing(client, base_url=kalshi_url) if check_kalshi else {"checked": False, "whale_identifiable": False, "tennis_series": []}
    finally:
        if owns:
            await client.aclose()

    markets_out = []
    for market_id, item in listed.items():
        market = _market_from_record(item, source="network")
        if market is not None:
            markets_out.append(market.as_dict())
    return {
        "schema_version": TAPE_SCHEMA,
        "meta": {
            "source": "network",
            "harvested_at": started.isoformat(),
            "finished_at": _now().isoformat(),
            "endpoints": [f"{gamma_url}/markets?tag_id={tag_id}", f"{data_api_url}/trades?takerOnly=true", f"{kalshi_url}/series"],
            "authenticated": False,
            "requests": requests,
            "errors": errors,
            "resolved_markets_requested": resolved_markets,
            "open_markets_requested": open_markets,
            "min_volume_usdc": str(min_volume),
            "max_trades_per_market": max_trades_per_market,
            "markets_truncated": truncated,
            "trades_per_market": per_market_counts,
            "kalshi": kalshi,
        },
        "markets": markets_out,
        "trades": trades,
    }


# --------------------------------------------------------------------------
# Replay into per-lag paper ledgers
# --------------------------------------------------------------------------
class TapeReplayClient(SnapshotClient):
    """Paper client that fills a copy order at its limit price at the replay clock.

    The tape carries prints, not books, so there is no ladder to walk: the copy
    fills fully at the reference print's price plus slippage (size is already
    capped by the print's size upstream) and pays the market's taker fee.
    """

    def __init__(self, snapshot: VenueSnapshot, fee_schedule: FeeSchedule, *, model_fees: bool = True) -> None:
        super().__init__(snapshot, fee_schedule, model_fees=model_fees)
        self.clock: datetime | None = None

    async def place_order(self, order: Order) -> ExecutionReport:
        if not self.paper:
            raise PermissionError("TapeReplayClient is paper-only")
        market = self._market_cache.get(order.market_id)
        if market is None or not market.active:
            raise ValueError(f"market {order.market_id} is unavailable")
        if order.price is None:
            raise ValueError("replay copies need a limit price")
        fee = self._fee_schedule_for(market)(order.quantity, order.price) if self.model_fees else ZERO
        order_id = f"paper-copy-{uuid4().hex[:12]}"
        fill = Fill(
            venue=order.venue,
            market_id=order.market_id,
            order_id=order_id,
            side=order.side,
            outcome=order.outcome,
            quantity=order.quantity,
            price=order.price,
            timestamp=self.clock or _now(),
            fee=fee,
        )
        self._paper_portfolio.apply_fill(fill)
        self._paper_fills.append(fill)
        return ExecutionReport(replace(order, order_id=order_id, status=OrderStatus.FILLED), (fill,))


def create_copy_runtime(
    lag_seconds: int,
    snapshot: VenueSnapshot,
    *,
    starting_cash: Decimal = Decimal("1000"),
    model_fees: bool = True,
    risk_limits: RiskLimits | None = None,
) -> TrackRuntime:
    return TrackRuntime.create(
        track_for_lag(lag_seconds),
        {Venue.POLYMARKET: snapshot},
        ledger=None,
        risk_limits=risk_limits or COPY_RISK_LIMITS,
        starting_cash=starting_cash,
        model_fees=model_fees,
        client_factory=lambda snap, fee: TapeReplayClient(snap, fee, model_fees=model_fees),
    )


@dataclass(slots=True)
class CopyRow:
    track: str
    lag_seconds: int
    wallet: str
    market_id: str
    title: str
    outcome: Outcome
    signal_at: datetime
    signal_yes_price: Decimal
    signal_notional: Decimal
    copy_at: datetime
    copy_price: Decimal
    yes_equivalent_price: Decimal
    quantity: Decimal
    fee: Decimal
    stake: Decimal
    market_type: str = ""
    resolved_outcome: Outcome | None = None
    close_yes_price: Decimal | None = None
    settlement_pnl: Decimal | None = None
    settlement_roi: Decimal | None = None
    clv_pnl: Decimal | None = None
    clv_roi: Decimal | None = None

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.outcome is Outcome.YES else -self.quantity

    def score(self, market: TennisMarket, close_yes: Decimal | None) -> None:
        self.resolved_outcome = market.resolved_outcome
        if market.resolved_outcome is not None:
            settle = ONE if market.resolved_outcome is Outcome.YES else ZERO
            self.settlement_pnl = (self.signed_quantity * (settle - self.yes_equivalent_price) - self.fee).quantize(Q4)
            self.settlement_roi = (self.settlement_pnl / self.stake).quantize(Q4)
        if market.closed and close_yes is not None:
            self.close_yes_price = close_yes
            self.clv_pnl = (self.signed_quantity * (close_yes - self.yes_equivalent_price) - self.fee).quantize(Q4)
            self.clv_roi = (self.clv_pnl / self.stake).quantize(Q4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "track": self.track,
            "lag_seconds": self.lag_seconds,
            "wallet": self.wallet,
            "market": self.market_id,
            "title": self.title,
            "market_type": self.market_type,
            "outcome": self.outcome.value,
            "signal_at": self.signal_at.isoformat(),
            "signal_yes_price": self.signal_yes_price,
            "signal_notional_usdc": self.signal_notional.quantize(Q2),
            "copy_at": self.copy_at.isoformat(),
            "copy_price": self.copy_price,
            "yes_equivalent_price": self.yes_equivalent_price,
            "quantity": self.quantity,
            "fee": self.fee,
            "stake_usdc": self.stake.quantize(Q4),
            "resolved_outcome": self.resolved_outcome.value if self.resolved_outcome else None,
            "close_yes_price": self.close_yes_price,
            "settlement_pnl": self.settlement_pnl,
            "settlement_roi": self.settlement_roi,
            "clv_pnl": self.clv_pnl,
            "clv_roi": self.clv_roi,
        }


@dataclass(slots=True)
class ReplayResult:
    params: CopyParameters
    tracker: WhaleTracker
    runtimes: dict[int, TrackRuntime]
    copies: list[CopyRow]
    signals: list[TapePrint]
    signal_reasons: dict[str, int]
    copy_reasons: dict[int, dict[str, int]]
    whale_own: list[dict[str, Any]]
    close_prices: dict[str, Decimal | None]

    @property
    def summaries(self) -> list[TrackSummary]:
        return [rt.summary for rt in self.runtimes.values()]

    @property
    def ledgers(self) -> dict[str, PaperLedger]:
        return {rt.name: rt.ledger for rt in self.runtimes.values()}


def _whale_own_row(signal: TapePrint, market: TennisMarket, close_yes: Decimal | None) -> dict[str, Any]:
    """The whale's own fill scored the same way as a copy (lag 0, its own price, taker fee)."""
    yes_price = signal.yes_equivalent_price
    stake = signal.size * signal.price
    fee = polymarket_taker_fee(signal.size, signal.price, market.taker_fee_rate)
    row: dict[str, Any] = {
        "wallet": signal.wallet, "market": signal.market_id, "outcome": signal.long_outcome.value,
        "signal_at": signal.timestamp.isoformat(), "yes_price": yes_price, "size": signal.size,
        "stake_usdc": stake.quantize(Q2), "settlement_roi": None, "clv_roi": None,
    }
    if stake <= ZERO:
        return row
    if market.resolved_outcome is not None:
        settle = ONE if market.resolved_outcome is Outcome.YES else ZERO
        row["settlement_roi"] = ((signal.signed_quantity * (settle - yes_price) - fee) / stake).quantize(Q4)
    if market.closed and close_yes is not None:
        row["clv_roi"] = ((signal.signed_quantity * (close_yes - yes_price) - fee) / stake).quantize(Q4)
    return row


async def replay_copy_tracks(
    tape: TennisTape,
    *,
    params: CopyParameters | None = None,
    starting_cash: Decimal = Decimal("1000"),
    model_fees: bool = True,
    risk_limits: RiskLimits | None = None,
    cycle_label: str = "",
) -> ReplayResult:
    """Replay the tape once; one paper track (ledger) per lag."""
    params = params or CopyParameters()
    snapshot = VenueSnapshot(
        venue=Venue.POLYMARKET, source=tape.source,
        markets=[m.as_market() for m in tape.markets.values()],
        fetched_at=str(tape.meta.get("harvested_at") or _now().isoformat()),
    )
    runtimes = {
        lag: create_copy_runtime(lag, snapshot, starting_cash=starting_cash, model_fees=model_fees, risk_limits=risk_limits)
        for lag in params.lags
    }
    tracker = WhaleTracker(params)
    copies: list[CopyRow] = []
    signals: list[TapePrint] = []
    signal_reasons: dict[str, int] = {}
    copy_reasons: dict[int, dict[str, int]] = {lag: {} for lag in params.lags}
    last_copy: dict[tuple[int, str, str, Outcome], datetime] = {}
    close_prices: dict[str, Decimal | None] = {}
    for market_id, market in tape.markets.items():
        last = tape.last_print_before(market_id, market.closed_at) if market.closed else None
        close_prices[market_id] = last.yes_equivalent_price if last is not None else None

    for tape_print in tape.prints:
        decision = tracker.evaluate(tape_print)
        signal_reasons[decision.reason] = signal_reasons.get(decision.reason, 0) + 1
        if decision.signal:
            signals.append(tape_print)
            market = tape.markets[tape_print.market_id]
            for lag, runtime in runtimes.items():
                runtime.summary.candidates += 1
                reference = tape.first_print_at_or_after(tape_print.market_id, tape_print.timestamp + timedelta(seconds=lag))
                key = (lag, tape_print.wallet, tape_print.market_id, tape_print.long_outcome)
                plan: CopyDecision = copy_decision(
                    tape_print, lag_seconds=lag, reference=reference, params=params, tick_size=market.tick_size,
                    market_closed_at=market.closed_at, last_copy_at=last_copy.get(key),
                )
                if not plan.copy or plan.order is None or plan.reference is None:
                    runtime.summary.refuse(plan.reason)
                    copy_reasons[lag][plan.reason] = copy_reasons[lag].get(plan.reason, 0) + 1
                    continue
                runtime.summary.admitted += 1
                runtime.summary.proposed_orders += 1
                client = runtime.clients[Venue.POLYMARKET]
                assert isinstance(client, TapeReplayClient)
                client.clock = plan.reference.timestamp
                fills_before = runtime.summary.paper_fills
                report = await runtime.submit(plan.order, edge=None)
                if report is None or runtime.summary.paper_fills == fills_before:
                    continue  # risk refusal already counted by TrackRuntime.submit
                last_copy[key] = tape_print.timestamp
                copy_reasons[lag]["copy"] = copy_reasons[lag].get("copy", 0) + 1
                fill = report.fills[0]
                copies.append(
                    CopyRow(
                        track=runtime.name, lag_seconds=lag, wallet=tape_print.wallet, market_id=tape_print.market_id,
                        title=market.question, outcome=plan.order.outcome, signal_at=tape_print.timestamp,
                        signal_yes_price=tape_print.yes_equivalent_price, signal_notional=tape_print.notional,
                        copy_at=plan.reference.timestamp, copy_price=fill.price, yes_equivalent_price=fill.yes_equivalent_price,
                        quantity=fill.quantity, fee=fill.fee, stake=fill.quantity * fill.price, market_type=market.market_type,
                    )
                )
        tracker.observe(tape_print)

    for row in copies:
        row.score(tape.markets[row.market_id], close_prices[row.market_id])
    whale_own = [_whale_own_row(s, tape.markets[s.market_id], close_prices[s.market_id]) for s in signals]

    label = cycle_label or f"tennis_whale:{tape.source}:{_now().isoformat()}"
    for lag, runtime in runtimes.items():
        for position in list(runtime.ledger.open_positions):
            market = tape.markets.get(position.market_id)
            if market is None:
                continue
            if market.resolved_outcome is not None:
                runtime.ledger.settle(Venue.POLYMARKET, position.market_id, market.resolved_outcome)
                continue
            last = tape.last_print_before(position.market_id, None)
            if last is not None:
                runtime.ledger.mark(Venue.POLYMARKET, position.market_id, last.yes_equivalent_price)
        runtime.finalize(label=label)
        finalize_copy_metrics(runtime, lag, copies, copy_reasons[lag], tape)
    return ReplayResult(params, tracker, runtimes, copies, signals, signal_reasons, copy_reasons, whale_own, close_prices)


def finalize_copy_metrics(runtime: TrackRuntime, lag: int, copies: list[CopyRow], reasons: dict[str, int], tape: TennisTape) -> None:
    mine = [c for c in copies if c.lag_seconds == lag]
    summary = runtime.summary
    summary.metrics["family"] = TRACK_FAMILY
    summary.metrics["lag_seconds"] = lag
    summary.metrics["lag"] = lag_label(lag)
    summary.metrics["copies"] = len(mine)
    summary.metrics["copy_reasons"] = dict(sorted(reasons.items()))
    summary.metrics["copied_markets"] = len({c.market_id for c in mine})
    summary.metrics["copied_wallets"] = len({c.wallet for c in mine})
    summary.metrics["resolved_copies"] = sum(1 for c in mine if c.settlement_roi is not None)
    summary.metrics["closed_copies"] = sum(1 for c in mine if c.clv_roi is not None)
    summary.metrics["stake_usdc"] = sum((c.stake for c in mine), ZERO).quantize(Q2)
    summary.metrics["snapshot"] = {
        "polymarket": {"source": tape.source, "markets": len(tape.markets), "prints": len(tape.prints), "errors": list(tape.meta.get("errors") or [])}
    }
    summary.settlement_risk_flag = False
    summary.notes = (
        f"Paper copy of qualified tennis whales' taker prints {lag_label(lag)} after the print, at the first executed "
        "print on the market plus slippage, venue taker fee, $10 stake per copy capped by the print size; "
        "$25/order, 100 contracts/market, $75 daily-loss rails. Resolved markets settled in the ledger, open ones marked at the last print."
    )
    # Per-fill paper_pnl in the scoreboard rows: settlement or last-print mark, whichever the ledger used.
    for row in summary.fills:
        mark = runtime.ledger.marks.get((Venue.POLYMARKET, row["market"]))
        if mark is None:
            continue
        signed = row["qty"] if (row["side"] == "buy") == (row["outcome"] == "yes") else -row["qty"]
        row["mark"] = mark
        row["paper_pnl"] = (signed * (mark - row["yes_equivalent_price"]) - row["fee"]).quantize(Q4)


# --------------------------------------------------------------------------
# Statistics: market-clustered bootstrap
# --------------------------------------------------------------------------
def cluster_bootstrap(
    values_by_cluster: dict[str, list[Decimal]],
    *,
    resamples: int = 2000,
    alpha: float = 0.05,
    seed: int = 20260914,
) -> dict[str, Any]:
    """Percentile CI of the equal-weighted mean, resampling clusters (markets) with replacement."""
    clusters = [(k, [float(v) for v in vals]) for k, vals in sorted(values_by_cluster.items()) if vals]
    n = sum(len(vals) for _, vals in clusters)
    out: dict[str, Any] = {
        "n": n, "n_clusters": len(clusters), "resamples": resamples, "alpha": alpha, "seed": seed,
        "mean": None, "ci_low": None, "ci_high": None, "share_positive": None, "effective_n_clusters": None,
    }
    if n == 0:
        return out
    flat = [v for _, vals in clusters for v in vals]
    out["mean"] = round(sum(flat) / n, 6)
    out["share_positive"] = round(sum(1 for v in flat if v > 0) / n, 4)
    sizes = [len(vals) for _, vals in clusters]
    out["effective_n_clusters"] = round((sum(sizes) ** 2) / sum(s * s for s in sizes), 2)
    if len(clusters) < 2:
        return out
    rng = random.Random(seed)
    means: list[float] = []
    count = len(clusters)
    for _ in range(resamples):
        total = 0.0
        size = 0
        for _ in range(count):
            _, vals = clusters[rng.randrange(count)]
            total += sum(vals)
            size += len(vals)
        if size:
            means.append(total / size)
    means.sort()
    lower = means[max(0, min(len(means) - 1, int((alpha / 2) * len(means))))]
    upper = means[max(0, min(len(means) - 1, int((1 - alpha / 2) * len(means)) - 1))]
    out["ci_low"], out["ci_high"] = round(lower, 6), round(upper, 6)
    return out


def _stat_verdict(stat: dict[str, Any], *, min_copies: int, min_markets: int) -> str:
    if stat["n"] < min_copies or stat["n_clusters"] < min_markets or stat["ci_low"] is None:
        return VERDICT_INSUFFICIENT
    return VERDICT_PASS if stat["ci_low"] > 0 else VERDICT_FAIL


def evaluate_lag(
    copies: list[CopyRow],
    *,
    lag: int,
    alpha: float,
    min_copies: int,
    min_markets: int,
    resamples: int,
    seed: int,
) -> dict[str, Any]:
    mine = [c for c in copies if c.lag_seconds == lag]
    settlement: dict[str, list[Decimal]] = {}
    clv: dict[str, list[Decimal]] = {}
    for c in mine:
        if c.settlement_roi is not None:
            settlement.setdefault(c.market_id, []).append(c.settlement_roi)
        if c.clv_roi is not None:
            clv.setdefault(c.market_id, []).append(c.clv_roi)
    settlement_stat = cluster_bootstrap(settlement, resamples=resamples, alpha=alpha, seed=seed)
    clv_stat = cluster_bootstrap(clv, resamples=resamples, alpha=alpha, seed=seed + 1)
    settlement_stat["verdict"] = _stat_verdict(settlement_stat, min_copies=min_copies, min_markets=min_markets)
    clv_stat["verdict"] = _stat_verdict(clv_stat, min_copies=min_copies, min_markets=min_markets)
    settlement_stat["total_pnl"] = sum((c.settlement_pnl for c in mine if c.settlement_pnl is not None), ZERO).quantize(Q4)
    clv_stat["total_pnl"] = sum((c.clv_pnl for c in mine if c.clv_pnl is not None), ZERO).quantize(Q4)
    by_type: dict[str, dict[str, Any]] = {}
    for c in mine:
        cell = by_type.setdefault(c.market_type or "unknown", {"copies": 0, "markets": set(), "settlement_roi": [], "clv_roi": []})
        cell["copies"] += 1
        cell["markets"].add(c.market_id)
        if c.settlement_roi is not None:
            cell["settlement_roi"].append(c.settlement_roi)
        if c.clv_roi is not None:
            cell["clv_roi"].append(c.clv_roi)
    by_market_type = {
        name: {
            "copies": cell["copies"],
            "markets": len(cell["markets"]),
            "n_settled": len(cell["settlement_roi"]),
            "settlement_roi_mean": (sum(cell["settlement_roi"], ZERO) / len(cell["settlement_roi"])).quantize(Q4) if cell["settlement_roi"] else None,
            "clv_roi_mean": (sum(cell["clv_roi"], ZERO) / len(cell["clv_roi"])).quantize(Q4) if cell["clv_roi"] else None,
        }
        for name, cell in sorted(by_type.items())
    }
    verdicts = {settlement_stat["verdict"], clv_stat["verdict"]}
    if VERDICT_PASS in verdicts:
        verdict, reason = VERDICT_PASS, "ci_lower_bound_above_zero"
    elif verdicts == {VERDICT_INSUFFICIENT}:
        verdict, reason = VERDICT_INSUFFICIENT, f"fewer than {min_copies} copies or {min_markets} markets with outcomes"
    else:
        verdict, reason = VERDICT_FAIL, "no statistic with ci_low > 0"
    significantly_negative = all(
        s["verdict"] == VERDICT_FAIL and s["ci_high"] is not None and s["ci_high"] < 0 for s in (settlement_stat, clv_stat)
    )
    return {
        "lag_seconds": lag,
        "lag": lag_label(lag),
        "track": track_for_lag(lag),
        "copies": len(mine),
        "settlement_roi": settlement_stat,
        "clv_roi": clv_stat,
        "by_market_type": by_market_type,
        "verdict": verdict,
        "reason": reason,
        "significantly_negative_both": significantly_negative,
    }


def kill_rule(lag_results: list[dict[str, Any]], whale_own_settlement: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pre-registered kill rule, evaluated from the same bootstrap outputs the pass rule uses.

    Two independent triggers:

    * ``copy_negative_all_lags``: every lag has sufficient data and both settlement
      ROI and CLV ROI have CI upper bound < 0.
    * ``whale_own_negative``: the whales' own fills (lag 0, taker fee) have a
      settlement-ROI CI upper bound < 0 with sufficient data. If the signal itself
      loses after fees, no lag can recover it.
    """
    evaluable = [r for r in lag_results if r["verdict"] != VERDICT_INSUFFICIENT]
    own = whale_own_settlement or {}
    own_evaluable = own.get("ci_high") is not None and own.get("sufficient", False)
    components = {
        "copy_negative_all_lags": bool(evaluable) and len(evaluable) == len(lag_results) and all(r["significantly_negative_both"] for r in evaluable),
        "whale_own_negative": bool(own_evaluable and own["ci_high"] < 0),
        "lags_evaluable": len(evaluable),
        "whale_own_evaluable": bool(own_evaluable),
    }
    if not evaluable and not own_evaluable:
        return {"triggered": False, "status": "not_evaluable", "reason": "no lag (and no whale-own benchmark) has enough copies with outcomes", "components": components}
    if components["copy_negative_all_lags"]:
        return {"triggered": True, "status": "kill", "reason": "settlement and CLV ROI CI upper bounds below zero at every lag", "components": components}
    if components["whale_own_negative"]:
        return {"triggered": True, "status": "kill", "reason": "whale-own settlement ROI CI upper bound below zero: the signal itself loses after fees", "components": components}
    return {"triggered": False, "status": "continue", "reason": "no trigger: some lag or the whale-own benchmark is not significantly negative", "components": components}


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
FAIL_RISKS = [
    "exit_liquidity: a copy fills at a print, not a resting book; the tape shows no depth, so real fills at size may be worse than the print+slippage assumption and exits before settlement are not modelled.",
    "farmers_and_makers: volume farmers, hedgers and market makers take both sides; the two_sided_share filter removes the obvious ones only, and a whale can be a farmer on other sports we do not see.",
    "selection_on_notional: whales are chosen by taker size, not skill; large takers can be informed, or simply rich and wrong. The whale-own benchmark shows whether the signal itself had edge.",
    "wallet_fragmentation: one actor may use several proxy wallets, so repeat-size detection undercounts (false negatives) and clustering by market is the only dependence structure modelled.",
    "resolved_universe_bias: resolved markets are listed by close time; short in-play markets dominate and long-dated tournament futures are underrepresented.",
    "tape_truncation: per-market trade caps keep the newest prints (endgame flow) when a market is truncated; markets_truncated is reported.",
    "fee_model: venue taker fee C*rate*p*(1-p) by Gamma feeType; rebates and maker programmes are not modelled.",
    "multiple_lags: three lags are three tests; CIs are Bonferroni-corrected but the PASS-at-any-lag rule is still a disjunction.",
]


def _status(tape: TennisTape, result: ReplayResult) -> str:
    if not tape.markets:
        return "no_tennis_markets"
    if not tape.prints:
        return "no_tennis_prints"
    if not result.tracker.whales():
        return "no_tennis_whales_found"
    if not result.signals:
        return "whales_found_no_signals"
    if tape.source == "fixture":
        return "fixture_synthetic"
    return "measured_from_public_tape"


def _headline(status: str, lag_results: list[dict[str, Any]], kill: dict[str, Any], whales: int, copies: int) -> str:
    if status in ("no_tennis_markets", "no_tennis_prints"):
        return f"No tennis tape ({status}); nothing to copy. INSUFFICIENT_DATA at every lag."
    if status == "no_tennis_whales_found":
        return "No wallet met the pre-registered whale definition on this tape; zero copies. INSUFFICIENT_DATA at every lag."
    parts = []
    for r in lag_results:
        s, c = r["settlement_roi"], r["clv_roi"]
        parts.append(
            f"{r['lag']}: {r['verdict']} (n={r['copies']}, settlement ROI {s['mean']} [{s['ci_low']}, {s['ci_high']}], "
            f"CLV ROI {c['mean']} [{c['ci_low']}, {c['ci_high']}])"
        )
    overall = VERDICT_PASS if any(r["verdict"] == VERDICT_PASS for r in lag_results) else (
        VERDICT_FAIL if any(r["verdict"] == VERDICT_FAIL for r in lag_results) else VERDICT_INSUFFICIENT
    )
    prefix = "Synthetic fixture (not evidence): " if status == "fixture_synthetic" else ""
    return f"{prefix}{whales} whales, {copies} paper copies. Overall {overall}; kill rule {kill['status']}. " + "; ".join(parts)


def build_report(
    tape: TennisTape,
    result: ReplayResult,
    *,
    mode: str,
    run_id: str,
    measured_at: str,
    alpha: float = 0.05,
    min_copies: int = 30,
    min_markets: int = 10,
    resamples: int = 2000,
    seed: int = 20260914,
    top_whales: int = 25,
) -> dict[str, Any]:
    params = result.params
    corrected_alpha = alpha / len(params.lags)
    lag_results = [
        evaluate_lag(result.copies, lag=lag, alpha=corrected_alpha, min_copies=min_copies, min_markets=min_markets, resamples=resamples, seed=seed)
        for lag in params.lags
    ]
    whales = result.tracker.whales()
    status = _status(tape, result)
    overall = VERDICT_PASS if any(r["verdict"] == VERDICT_PASS for r in lag_results) else (
        VERDICT_FAIL if any(r["verdict"] == VERDICT_FAIL for r in lag_results) else VERDICT_INSUFFICIENT
    )

    own_settlement: dict[str, list[Decimal]] = {}
    own_clv: dict[str, list[Decimal]] = {}
    for row in result.whale_own:
        if row["settlement_roi"] is not None:
            own_settlement.setdefault(row["market"], []).append(row["settlement_roi"])
        if row["clv_roi"] is not None:
            own_clv.setdefault(row["market"], []).append(row["clv_roi"])
    own_settlement_stat = cluster_bootstrap(own_settlement, resamples=resamples, alpha=corrected_alpha, seed=seed + 7)
    own_settlement_stat["sufficient"] = own_settlement_stat["n"] >= min_copies and own_settlement_stat["n_clusters"] >= min_markets
    own_clv_stat = cluster_bootstrap(own_clv, resamples=resamples, alpha=corrected_alpha, seed=seed + 8)
    own_clv_stat["sufficient"] = own_clv_stat["n"] >= min_copies and own_clv_stat["n_clusters"] >= min_markets
    whale_own = {
        "note": "The whales' own signal fills scored like a copy at lag 0 (their price, taker fee). If this is not positive, no lag can be.",
        "settlement_roi": own_settlement_stat,
        "clv_roi": own_clv_stat,
    }
    kill = kill_rule(lag_results, own_settlement_stat)

    copies_by_market: dict[str, dict[str, Any]] = {}
    for c in result.copies:
        cell = copies_by_market.setdefault(
            c.market_id,
            {"market": c.market_id, "title": c.title, "market_type": c.market_type, "resolved_outcome": c.resolved_outcome.value if c.resolved_outcome else None,
             "copies_by_lag": {}, "wallets": set(), "settlement_pnl": ZERO, "clv_pnl": ZERO, "stake_usdc": ZERO},
        )
        cell["copies_by_lag"][lag_label(c.lag_seconds)] = cell["copies_by_lag"].get(lag_label(c.lag_seconds), 0) + 1
        cell["wallets"].add(c.wallet)
        cell["settlement_pnl"] += c.settlement_pnl or ZERO
        cell["clv_pnl"] += c.clv_pnl or ZERO
        cell["stake_usdc"] += c.stake
    market_rows = [
        {**cell, "wallets": len(cell["wallets"]), "settlement_pnl": cell["settlement_pnl"].quantize(Q4), "clv_pnl": cell["clv_pnl"].quantize(Q4), "stake_usdc": cell["stake_usdc"].quantize(Q2)}
        for cell in sorted(copies_by_market.values(), key=lambda x: x["stake_usdc"], reverse=True)
    ]

    signals_by_wallet: dict[str, int] = {}
    for s in result.signals:
        signals_by_wallet[s.wallet] = signals_by_wallet.get(s.wallet, 0) + 1
    copies_by_wallet: dict[str, int] = {}
    for c in result.copies:
        copies_by_wallet[c.wallet] = copies_by_wallet.get(c.wallet, 0) + 1
    whale_rows = []
    for stats in whales[:top_whales]:
        own = [r for r in result.whale_own if r["wallet"] == stats.wallet and r["settlement_roi"] is not None]
        whale_rows.append(
            {
                **stats.as_dict(),
                "signals": signals_by_wallet.get(stats.wallet, 0),
                "copies_all_lags": copies_by_wallet.get(stats.wallet, 0),
                "refused_two_sided": stats.two_sided_share > params.max_two_sided_share,
                "own_settlement_roi_mean": (sum((r["settlement_roi"] for r in own), ZERO) / len(own)).quantize(Q4) if own else None,
                "own_scored_signals": len(own),
            }
        )

    tracks = {}
    for lag, runtime in result.runtimes.items():
        s = runtime.summary
        tracks[s.track] = {
            "label": s.label,
            "lag": lag_label(lag),
            "candidates": s.candidates,
            "admitted": s.admitted,
            "paper_fills": s.paper_fills,
            "refused_by_reason": dict(sorted(s.refused_by_reason.items())),
            "copied_markets": s.metrics.get("copied_markets", 0),
            "copied_wallets": s.metrics.get("copied_wallets", 0),
            "ledger": {k: s.ledger.get(k) for k in ("starting_cash", "cash", "equity", "realized_pnl", "unrealized_pnl", "total_pnl", "fees_paid", "gross_notional", "max_drawdown", "open_positions", "unmarked_positions", "fills", "settlement_fills", "mark_method")},
        }

    resolved = sum(1 for m in tape.markets.values() if m.resolved_outcome is not None)
    closed = sum(1 for m in tape.markets.values() if m.closed)
    market_types: dict[str, int] = {}
    for m in tape.markets.values():
        market_types[m.market_type or "unknown"] = market_types.get(m.market_type or "unknown", 0) + 1
    kalshi = dict(tape.meta.get("kalshi") or {"checked": False, "whale_identifiable": False})
    kalshi.setdefault("verdict", VERDICT_NOT_IDENTIFIABLE)
    kalshi.setdefault("reason", "Kalshi's public tape has no account identity; whale tracking is not identifiable from public data.")

    verdict_table = [
        {"scope": "lag", "check": f"copy_ev_or_clv_positive_{r['lag']}", "verdict": r["verdict"], "detail": r["reason"]} for r in lag_results
    ]
    verdict_table.append({"scope": "overall", "check": "copy_portfolio_positive_at_any_lag", "verdict": overall, "detail": "pre-registered pass rule"})
    verdict_table.append({"scope": "overall", "check": "kill_rule", "verdict": "TRIGGERED" if kill["triggered"] else kill["status"].upper(), "detail": kill["reason"]})
    verdict_table.append({"scope": "kalshi", "check": "kalshi_whales_identifiable", "verdict": VERDICT_NOT_IDENTIFIABLE, "detail": kalshi["reason"]})

    return {
        "schema_version": REPORT_SCHEMA,
        "kind": REPORT_KIND,
        "paper_only": True,
        "run_id": run_id,
        "mode": mode,
        "status": status,
        "measured_at": measured_at,
        "generated_at": _now().isoformat(),
        "pnl_source": "core.ledger.PaperLedger",
        "track_family": TRACK_FAMILY,
        "headline": _headline(status, lag_results, kill, len(whales), len(result.copies)),
        "overall_verdict": overall,
        "verdict_table": verdict_table,
        "pre_registration": {
            "hypothesis": "Wallets that repeatedly take size on Polymarket tennis carry information; copying their next print at a lag earns positive settlement EV or CLV after fees.",
            "pass_rule": f"At one or more lags in {list(params.lags)} s, the market-clustered bootstrap CI lower bound of equal-weighted per-copy ROI is > 0 for settlement ROI or CLV ROI after fees and slippage.",
            "kill_rule": "Kill when (a) every lag has sufficient data and both settlement ROI and CLV ROI have CI upper bound < 0, or (b) the whale-own benchmark (the whales' own fills at lag 0, taker fee) has settlement-ROI CI upper bound < 0 with sufficient data. Either trigger ends the track; there is no re-tuning of thresholds after seeing results.",
            "sufficiency": {"min_copies_with_outcome": min_copies, "min_markets_with_outcome": min_markets},
            "inference": {"statistic": "equal-weighted mean ROI per copy", "cluster": "market (conditionId)", "resamples": resamples, "alpha_family": alpha, "alpha_per_lag_bonferroni": corrected_alpha, "seed": seed},
            "parameters": params.as_dict(),
            "risk_limits": {"max_notional_per_order": COPY_RISK_LIMITS.max_notional_per_order, "max_position_per_market": COPY_RISK_LIMITS.max_position_per_market, "max_daily_loss": COPY_RISK_LIMITS.max_daily_loss},
        },
        "universe": {
            "source": tape.source,
            "harvested_at": tape.meta.get("harvested_at"),
            "endpoints": tape.meta.get("endpoints"),
            "requests": tape.meta.get("requests"),
            "errors": tape.meta.get("errors", []),
            "markets": len(tape.markets),
            "resolved_markets": resolved,
            "closed_markets": closed,
            "open_markets": len(tape.markets) - closed,
            "market_types": dict(sorted(market_types.items())),
            "prints": len(tape.prints),
            "wallets": len(tape.wallets()),
            "markets_truncated": tape.meta.get("markets_truncated", 0),
            "first_print_at": tape.prints[0].timestamp.isoformat() if tape.prints else None,
            "last_print_at": tape.prints[-1].timestamp.isoformat() if tape.prints else None,
        },
        "whales": {
            "qualified": len(whales),
            "refused_two_sided": sum(1 for w in whales if w.two_sided_share > params.max_two_sided_share),
            "signals": len(result.signals),
            "signal_reasons": dict(sorted(result.signal_reasons.items())),
            "top": whale_rows,
        },
        "whale_own_benchmark": whale_own,
        "lags": {r["lag"]: r for r in lag_results},
        "kill_rule": kill,
        "kalshi": kalshi,
        "tracks": tracks,
        "copies_total": len(result.copies),
        "copies_by_market": market_rows,
        "copies_file": COPIES_FILE,
        "fail_risks": FAIL_RISKS,
        "assumptions": {
            "copy_price": "First executed print on the same market at or after signal_time + lag, plus slippage_ticks against us. A print is evidence liquidity existed; it is not a resting quote.",
            "copy_size": "stake_per_copy / price, whole contracts, capped by the reference print's size (no depth on the tape). Risk rails: $25/order, 100 contracts/market, $75 daily loss.",
            "fees": "Polymarket taker fee C*rate*p*(1-p) with rate from Gamma feeType (sports 5%); makers pay nothing; rebates ignored.",
            "settlement": "Resolved markets (umaResolutionStatus=resolved, outcomePrices 1/0) settle in the ledger at 1/0; open positions marked at the last print's YES-equivalent price and counted as unrealized.",
            "clv": "Closing line = YES-equivalent price of the last print strictly before Gamma closedTime (or the last print when closedTime is missing). CLV ROI is (signed*(close - entry)*qty - fee)/stake.",
            "whale_definition": "Walk-forward: min_large_fills taker prints >= large_fill_notional USDC across >= min_markets tennis markets, all strictly before the print being evaluated.",
            "farmer_filter": "two_sided_share = markets where the wallet ended up long both outcomes / markets traded; above max_two_sided_share the wallet's prints are refused two_sided_flow.",
            "kalshi": "Not identifiable: public /markets/trades carries no account identity. Whether tennis series are listed is recorded but changes nothing.",
            "fixture": "research/fixtures/tennis_whale_tape.json is synthetic and exercises the pipeline; its numbers are not evidence about Polymarket." if tape.source == "fixture" else "Network tape harvested from public endpoints at harvested_at; see universe.",
        },
    }


def not_measured_report(reason: str, *, mode: str, run_id: str, measured_at: str, lags: tuple[int, ...] = DEFAULT_LAGS) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA,
        "kind": REPORT_KIND,
        "paper_only": True,
        "run_id": run_id,
        "mode": mode,
        "status": "not_measured",
        "measured_at": measured_at,
        "generated_at": _now().isoformat(),
        "pnl_source": "core.ledger.PaperLedger",
        "track_family": TRACK_FAMILY,
        "headline": f"Not measured: {reason}",
        "overall_verdict": VERDICT_INSUFFICIENT,
        "verdict_table": [{"scope": "lag", "check": f"copy_ev_or_clv_positive_{lag_label(l)}", "verdict": VERDICT_INSUFFICIENT, "detail": reason} for l in lags],
        "lags": {},
        "kill_rule": {"triggered": False, "status": "not_evaluable", "reason": reason},
        "fail_risks": FAIL_RISKS,
    }


__all__ = [
    "CopyRow",
    "ReplayResult",
    "TapeReplayClient",
    "TennisMarket",
    "TennisTape",
    "build_report",
    "cluster_bootstrap",
    "evaluate_lag",
    "harvest_tennis_tape",
    "kalshi_tennis_listing",
    "kill_rule",
    "load_tape",
    "not_measured_report",
    "replay_copy_tracks",
    "tape_from_payload",
]
