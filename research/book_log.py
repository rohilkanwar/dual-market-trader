"""Self-logged order-book archive: poll public venue endpoints, append JSONL.

The $0 path to a forward L2 archive. Nothing here authenticates, nothing
trades. Only unauthenticated, read-only endpoints are polled:

* Kalshi ``GET /markets`` (universe), ``GET /markets/{ticker}/orderbook``
  (YES/NO bid ladders), ``GET /markets/trades`` (public tape, newest first).
* Polymarket Gamma ``GET /public-search`` and ``GET /markets`` (universe),
  CLOB ``POST /books`` (batched L2 per token), Data-API ``GET /trades``
  (public fills; best effort, no stable trade id).

Every record is a point-in-time snapshot taken by *this* process. The archive
therefore holds what a poller can see: aggregated size per price level at the
capture instant plus the public trade prints between polls. It does **not**
hold order ids, queue position, cancels or intra-poll churn (true L3 / FIFO);
see ``docs/BOOK_LOGGER.md`` for the full list of what is not captured.

Layout under the archive root (default ``artifacts/books/``, git-ignored)::

    <venue>/<YYYY-MM-DD>/books.jsonl     one line per (market, token) snapshot
    <venue>/<YYYY-MM-DD>/trades.jsonl    one line per new public trade
    <venue>/<YYYY-MM-DD>/markets.jsonl   universe listing at each refresh
    sessions/<run_id>.json               config + running counters for the run

Files are opened in append mode per write so a crash loses at most the cycle
in flight and files roll over by UTC day without a restart.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import random
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

import httpx

from venues.kalshi.client import DEFAULT_MACRO_SERIES, HOSTS
from venues.polymarket.client import CLOB_URL, DEFAULT_MACRO_SEARCH, GAMMA_URL, _token_ids

SCHEMA_VERSION = 1
DATA_API_URL = "https://data-api.polymarket.com"
DEFAULT_ROOT = Path("artifacts") / "books"
KALSHI_TRADES_PAGE = 1000
SEEN_TRADES_CAP = 5000  # trade ids remembered per market for de-duplication
POLYMARKET_BOOKS_BATCH = 200
POLYMARKET_TRADES_PAGE = 100
CENTS = Decimal("100")
ONE = Decimal("1")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dec_str(value: Any, *, cents: bool = False) -> str | None:
    """Canonical decimal string (``"0.52"``, ``"120"``) or ``None`` if unparsable."""
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    if cents:
        number = number / CENTS
    return format(number.normalize(), "f")


def _complement(price: str) -> str:
    return format((ONE - Decimal(price)).normalize(), "f")


def _levels(raw: Any, *, cents: bool = False) -> list[list[str]]:
    """Normalise ``[[price, size], ...]`` or ``[{"price":..,"size":..}, ...]``."""
    out: list[list[str]] = []
    for level in raw or []:
        if isinstance(level, dict):
            price, size = level.get("price"), level.get("size")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, size = level[0], level[1]
        else:
            continue
        p, s = dec_str(price, cents=cents), dec_str(size)
        if p is not None and s is not None:
            out.append([p, s])
    return out


# --------------------------------------------------------------------------
# Rate limiting + retrying fetch
# --------------------------------------------------------------------------
class RateLimiter:
    """Serialises request starts to at most ``max_rps`` per second, process-wide.

    ``penalise`` pushes the next allowed start out (used after a 429/5xx) so a
    venue that is already throttling us is not hit again by the other tasks.
    """

    def __init__(self, max_rps: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._clock = clock
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()
        self.waits = 0

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next_allowed - now
            if wait > 0:
                self.waits += 1
                await asyncio.sleep(wait)
                now = self._clock()
            self._next_allowed = max(now, self._next_allowed) + self.min_interval

    def penalise(self, seconds: float) -> None:
        self._next_allowed = max(self._next_allowed, self._clock() + seconds)


class RateLimited(RuntimeError):
    """Raised when retries are exhausted on 429/5xx/transport errors."""


@dataclass(slots=True)
class FetchStats:
    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    errors: int = 0


class Fetcher:
    """httpx wrapper: rate-limited, retrying on 429/5xx/transport errors."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        limiter: RateLimiter,
        *,
        retries: int = 4,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.http = http
        self.limiter = limiter
        self.retries = retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self.stats = FetchStats()

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            header = response.headers.get("Retry-After")
            if header:
                try:
                    return min(float(header), self.backoff_cap)
                except ValueError:
                    pass
        base = min(self.backoff_base * (2**attempt), self.backoff_cap)
        return base * (0.75 + random.random() * 0.5)

    async def json(self, method: str, url: str, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            await self.limiter.acquire()
            self.stats.requests += 1
            response: httpx.Response | None = None
            try:
                response = await self.http.request(method, url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    self.stats.rate_limited += int(response.status_code == 429)
                    raise httpx.HTTPStatusError(
                        f"{response.status_code} from {url}", request=response.request, response=response
                    )
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if status is not None and status < 500 and status != 429:
                    self.stats.errors += 1
                    raise  # a 4xx other than 429 will not get better by retrying
                if attempt >= self.retries:
                    break
                delay = self._retry_delay(response, attempt)
                self.limiter.penalise(delay)
                self.stats.retries += 1
                await self._sleep(delay)
        self.stats.errors += 1
        raise RateLimited(f"{method} {url}: gave up after {self.retries + 1} attempts: {last_error}")


# --------------------------------------------------------------------------
# Archive
# --------------------------------------------------------------------------
class JsonlArchive:
    """Append-only JSONL files partitioned by venue and UTC day."""

    def __init__(self, root: Path = DEFAULT_ROOT, *, compress: bool = False) -> None:
        self.root = Path(root)
        self.compress = compress
        self.lines_written = 0
        self.bytes_written = 0

    def path_for(self, venue: str, kind: str, ts: datetime) -> Path:
        suffix = ".jsonl.gz" if self.compress else ".jsonl"
        return self.root / venue / ts.astimezone(UTC).strftime("%Y-%m-%d") / f"{kind}{suffix}"

    def append(self, venue: str, kind: str, records: Iterable[dict[str, Any]]) -> int:
        by_path: dict[Path, list[str]] = {}
        for record in records:
            ts = datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
            line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
            by_path.setdefault(self.path_for(venue, kind, ts), []).append(line)
        written = 0
        for path, lines in by_path.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = ("\n".join(lines) + "\n").encode("utf-8")
            opener = gzip.open if self.compress else open
            with opener(path, "ab") as handle:  # type: ignore[operator]
                handle.write(payload)
            written += len(lines)
            self.bytes_written += len(payload)
        self.lines_written += written
        return written

    def write_session(self, session: dict[str, Any]) -> Path:
        path = self.root / "sessions" / f"{session['run_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(session, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
        return path

    def files(self, venue: str | None = None, kind: str | None = None) -> list[Path]:
        pattern = f"{venue or '*'}/*/{kind or '*'}.jsonl*"
        return sorted(p for p in self.root.glob(pattern) if p.is_file())


def iter_records(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    """Yield records from JSONL / JSONL.gz files in order."""
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)


def export_parquet(root: Path, out_dir: Path) -> dict[str, int]:
    """Convert the JSONL archive to one Parquet file per (venue, kind).

    Requires ``pyarrow`` (``uv sync --extra books``). Nested ladders are kept
    as JSON strings so the schema stays flat and stable across venues.
    """
    try:
        import pyarrow as pa  # type: ignore[import-not-found]
        import pyarrow.parquet as pq  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise SystemExit("pyarrow is required for --to-parquet: uv sync --extra books") from exc
    archive = JsonlArchive(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for venue_dir in sorted(p for p in archive.root.iterdir() if p.is_dir() and p.name != "sessions"):
        for kind in ("books", "trades", "markets"):
            paths = archive.files(venue_dir.name, kind)
            if not paths:
                continue
            rows = []
            for record in iter_records(paths):
                flat = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in record.items()}
                rows.append(flat)
            if not rows:
                continue
            table = pa.Table.from_pylist(rows)
            target = out_dir / f"{venue_dir.name}_{kind}.parquet"
            pq.write_table(table, target)
            counts[f"{venue_dir.name}/{kind}"] = table.num_rows
    return counts


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
@dataclass(slots=True)
class CycleResult:
    venue: str
    books: int = 0
    trades: int = 0
    markets: int = 0
    errors: list[str] = field(default_factory=list)


class BookSource(Protocol):
    venue: str

    async def refresh_universe(self) -> list[dict[str, Any]]: ...

    async def poll_books(self, cycle: int) -> list[dict[str, Any]]: ...

    async def poll_trades(self, cycle: int) -> list[dict[str, Any]]: ...

    def universe_ids(self) -> list[str]: ...


class SidecarSource(Protocol):
    """Hook for later free context feeds (official RSS, public weather).

    Deliberately not implemented: a sidecar polls a free public feed and
    returns ``{"kind": "<name>", "ts": iso, ...}`` records that
    :class:`BookLogger` appends under ``<name>/<day>/<name>.jsonl``. Nothing
    is mapped to a probability here; that is the (unvalidated) news lane's
    job. See ``docs/BOOK_LOGGER.md``.
    """

    name: str

    async def poll(self) -> list[dict[str, Any]]: ...


class KalshiBookSource:
    venue = "kalshi"

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        environment: str = "prod",
        series: tuple[str, ...] = DEFAULT_MACRO_SERIES,
        tickers: tuple[str, ...] = (),
        limit: int = 200,
        depth: int = 0,
        trade_pages_first: int = 1,
        trade_pages_max: int = 3,
        concurrency: int = 2,
    ) -> None:
        if environment not in HOSTS:
            raise ValueError(f"kalshi environment must be one of {sorted(HOSTS)}")
        self.fetch = fetcher
        self.base = HOSTS[environment]
        self.environment = environment
        self.series = tuple(series)
        self.tickers = tuple(tickers)
        self.limit = max(1, limit)
        self.depth = max(0, depth)
        self.trade_pages_first = max(1, trade_pages_first)
        self.trade_pages_max = max(1, trade_pages_max)
        self._gate = asyncio.Semaphore(max(1, concurrency))
        self._universe: dict[str, dict[str, Any]] = {}
        self._seen_trade_ids: dict[str, set[str]] = {}

    def universe_ids(self) -> list[str]:
        return list(self._universe)

    async def refresh_universe(self) -> list[dict[str, Any]]:
        ts = iso(utc_now())
        items: dict[str, dict[str, Any]] = {}
        if self.tickers:
            for ticker in self.tickers:
                items[ticker] = {"ticker": ticker, "source": "explicit"}
        for series in self.series:
            cursor: str | None = None
            while True:
                params: dict[str, Any] = {"series_ticker": series, "status": "open", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                payload = await self.fetch.json("GET", f"{self.base}/markets", params=params)
                for item in payload.get("markets", []) or []:
                    if isinstance(item, dict) and item.get("ticker"):
                        items[str(item["ticker"])] = {**item, "series_ticker": series}
                cursor = payload.get("cursor") or None
                if not cursor or len(items) >= self.limit:
                    break
        self._universe = dict(list(items.items())[: self.limit])
        records = []
        for ticker, item in self._universe.items():
            records.append(
                {
                    "kind": "market",
                    "v": SCHEMA_VERSION,
                    "venue": self.venue,
                    "market_id": ticker,
                    "ts": ts,
                    "series_ticker": item.get("series_ticker"),
                    "event_ticker": item.get("event_ticker"),
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "open_time": item.get("open_time"),
                    "close_time": item.get("close_time"),
                    "yes_bid": dec_str(item.get("yes_bid_dollars")) or dec_str(item.get("yes_bid"), cents=True),
                    "yes_ask": dec_str(item.get("yes_ask_dollars")) or dec_str(item.get("yes_ask"), cents=True),
                    "last_price": dec_str(item.get("last_price_dollars")) or dec_str(item.get("last_price"), cents=True),
                    "volume": dec_str(item.get("volume_fp")) or dec_str(item.get("volume")),
                    "open_interest": dec_str(item.get("open_interest_fp")) or dec_str(item.get("open_interest")),
                }
            )
        return records

    def _book_record(self, ticker: str, payload: dict[str, Any], *, cycle: int, requested: datetime) -> dict[str, Any]:
        if isinstance(payload.get("orderbook_fp"), dict):
            book, yes_key, no_key, cents = payload["orderbook_fp"], "yes_dollars", "no_dollars", False
        else:
            book, yes_key, no_key, cents = payload.get("orderbook") or {}, "yes", "no", True
        yes_bids = _levels(book.get(yes_key), cents=cents)
        no_bids = _levels(book.get(no_key), cents=cents)
        # Kalshi publishes two bid ladders; a NO bid at p is a YES ask at 1 - p.
        bids = sorted(yes_bids, key=lambda lv: Decimal(lv[0]), reverse=True)
        asks = sorted(([_complement(p), s] for p, s in no_bids), key=lambda lv: Decimal(lv[0]))
        captured = utc_now()
        return {
            "kind": "book",
            "v": SCHEMA_VERSION,
            "venue": self.venue,
            "market_id": ticker,
            "ts": iso(captured),
            "ts_venue": None,  # the public orderbook endpoint carries no server timestamp
            "cycle": cycle,
            "latency_ms": int((captured - requested).total_seconds() * 1000),
            "depth_requested": self.depth or None,
            "bids": bids,
            "asks": asks,
            "raw": {"yes_bids": yes_bids, "no_bids": no_bids},
        }

    async def poll_books(self, cycle: int) -> list[dict[str, Any]]:
        async def one(ticker: str) -> dict[str, Any] | Exception:
            async with self._gate:
                requested = utc_now()
                params = {"depth": self.depth} if self.depth else None
                try:
                    payload = await self.fetch.json("GET", f"{self.base}/markets/{ticker}/orderbook", params=params)
                except Exception as exc:  # one dead market must not kill the cycle
                    return exc
                return self._book_record(ticker, payload, cycle=cycle, requested=requested)

        results = await asyncio.gather(*(one(t) for t in self.universe_ids()))
        return [r if isinstance(r, dict) else {"error": f"book[{t}]: {type(r).__name__}: {r}"} for t, r in zip(self.universe_ids(), results)]

    def _trade_record(self, raw: dict[str, Any], *, cycle: int, captured: str) -> dict[str, Any] | None:
        trade_id = str(raw.get("trade_id") or "")
        price = dec_str(raw.get("yes_price_dollars")) or dec_str(raw.get("yes_price"), cents=True)
        size = dec_str(raw.get("count_fp")) or dec_str(raw.get("count"))
        if not trade_id or price is None or size is None:
            return None
        return {
            "kind": "trade",
            "v": SCHEMA_VERSION,
            "venue": self.venue,
            "market_id": str(raw.get("ticker") or ""),
            "trade_id": trade_id,
            "ts": captured,
            "ts_venue": raw.get("created_time"),
            "cycle": cycle,
            "price": price,
            "size": size,
            "taker_side": str(raw.get("taker_side") or "").lower() or None,
        }

    async def poll_trades(self, cycle: int) -> list[dict[str, Any]]:
        async def one(ticker: str) -> list[dict[str, Any]]:
            async with self._gate:
                captured = iso(utc_now())
                seen = self._seen_trade_ids.get(ticker)
                first_poll = seen is None
                pages_allowed = self.trade_pages_first if first_poll else self.trade_pages_max
                new: list[dict[str, Any]] = []
                new_ids: set[str] = set()
                cursor: str | None = None
                stop = False
                for _ in range(pages_allowed):
                    params: dict[str, Any] = {"ticker": ticker, "limit": KALSHI_TRADES_PAGE}
                    if cursor:
                        params["cursor"] = cursor
                    try:
                        payload = await self.fetch.json("GET", f"{self.base}/markets/trades", params=params)
                    except Exception as exc:
                        return [{"error": f"trades[{ticker}]: {type(exc).__name__}: {exc}"}]
                    for raw in payload.get("trades", []) or []:
                        if not isinstance(raw, dict):
                            continue
                        record = self._trade_record({**raw, "ticker": raw.get("ticker") or ticker}, cycle=cycle, captured=captured)
                        if record is None:
                            continue
                        if seen is not None and record["trade_id"] in seen:
                            stop = True  # newest-first: everything older is already archived
                            break
                        new.append(record)
                        new_ids.add(record["trade_id"])
                    cursor = payload.get("cursor") or None
                    if stop or not cursor:
                        break
                # Remember this poll's ids plus (a bounded slice of) the previous
                # frontier so a print that straddles two polls is never written twice.
                frontier = set(new_ids)
                for trade_id in seen or ():
                    if len(frontier) >= SEEN_TRADES_CAP:
                        break
                    frontier.add(trade_id)
                self._seen_trade_ids[ticker] = frontier
                return new

        chunks = await asyncio.gather(*(one(t) for t in self.universe_ids()))
        return [record for chunk in chunks for record in chunk]


class PolymarketBookSource:
    venue = "polymarket"

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        search_terms: tuple[str, ...] = DEFAULT_MACRO_SEARCH,
        condition_ids: tuple[str, ...] = (),
        limit: int = 100,
        trades: bool = False,
        concurrency: int = 2,
    ) -> None:
        self.fetch = fetcher
        self.search_terms = tuple(search_terms)
        self.condition_ids = tuple(condition_ids)
        self.limit = max(1, limit)
        self.trades_enabled = trades
        self._gate = asyncio.Semaphore(max(1, concurrency))
        self._universe: dict[str, dict[str, Any]] = {}  # condition id -> market item (+ event)
        self._token_outcome: dict[str, tuple[str, str]] = {}  # token id -> (condition id, outcome)
        self._seen_trade_keys: dict[str, set[str]] = {}

    def universe_ids(self) -> list[str]:
        return list(self._universe)

    @staticmethod
    def _outcomes(item: dict[str, Any]) -> list[str]:
        raw = item.get("outcomes") or '["Yes","No"]'
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = ["Yes", "No"]
        return [str(o) for o in raw] or ["Yes", "No"]

    def _register(self, item: dict[str, Any], event: dict[str, Any]) -> None:
        condition_id = str(item.get("conditionId") or item.get("condition_id") or "")
        tokens = _token_ids(item)
        if not condition_id or not tokens:
            return
        if condition_id not in self._universe and len(self._universe) >= self.limit:
            return
        if item.get("closed") or item.get("active") is False or item.get("enableOrderBook") is False:
            return
        self._universe[condition_id] = {**item, "_event": {k: event.get(k) for k in ("id", "slug", "title", "negRisk")}}
        for token, outcome in zip(tokens, self._outcomes(item)):
            self._token_outcome[token] = (condition_id, outcome.lower())

    async def refresh_universe(self) -> list[dict[str, Any]]:
        ts = iso(utc_now())
        self._universe.clear()
        self._token_outcome.clear()
        for condition_id in self.condition_ids:
            payload = await self.fetch.json("GET", f"{GAMMA_URL}/markets", params={"condition_ids": condition_id})
            for item in payload if isinstance(payload, list) else []:
                if isinstance(item, dict):
                    events = item.get("events") or []
                    self._register(item, events[0] if events and isinstance(events[0], dict) else {})
        for term in self.search_terms:
            try:
                payload = await self.fetch.json("GET", f"{GAMMA_URL}/public-search", params={"q": term, "limit_per_type": 10})
            except (httpx.HTTPError, RateLimited):
                continue  # one failed search must not empty the universe
            events = payload.get("events") if isinstance(payload, dict) else None
            for event in events or []:
                if not isinstance(event, dict) or event.get("closed"):
                    continue
                for item in event.get("markets") or []:
                    if isinstance(item, dict):
                        self._register(item, event)
        records = []
        for condition_id, item in self._universe.items():
            records.append(
                {
                    "kind": "market",
                    "v": SCHEMA_VERSION,
                    "venue": self.venue,
                    "market_id": condition_id,
                    "ts": ts,
                    "slug": item.get("slug"),
                    "question": item.get("question"),
                    "event_slug": item["_event"].get("slug"),
                    "event_title": item["_event"].get("title"),
                    "neg_risk": item.get("negRisk", item["_event"].get("negRisk")),
                    "token_ids": _token_ids(item),
                    "outcomes": self._outcomes(item),
                    "tick_size": dec_str(item.get("orderPriceMinTickSize")),
                    "min_order_size": dec_str(item.get("orderMinSize")),
                    "end_date": item.get("endDate") or item.get("endDateIso"),
                    "liquidity": dec_str(item.get("liquidityNum") or item.get("liquidity")),
                    "volume": dec_str(item.get("volumeNum") or item.get("volume")),
                    "fees_enabled": item.get("feesEnabled"),
                }
            )
        return records

    async def poll_books(self, cycle: int) -> list[dict[str, Any]]:
        tokens = list(self._token_outcome)
        records: list[dict[str, Any]] = []
        for start in range(0, len(tokens), POLYMARKET_BOOKS_BATCH):
            chunk = tokens[start : start + POLYMARKET_BOOKS_BATCH]
            requested = utc_now()
            try:
                payload = await self.fetch.json("POST", f"{CLOB_URL}/books", json=[{"token_id": t} for t in chunk])
            except Exception as exc:
                records.append({"error": f"books[{start}:{start + len(chunk)}]: {type(exc).__name__}: {exc}"})
                continue
            captured = utc_now()
            returned: set[str] = set()
            for book in payload if isinstance(payload, list) else []:
                if not isinstance(book, dict):
                    continue
                token = str(book.get("asset_id") or "")
                if token not in self._token_outcome:
                    continue
                returned.add(token)
                condition_id, outcome = self._token_outcome[token]
                raw_ts = book.get("timestamp")
                ts_venue = None
                if raw_ts not in (None, ""):
                    try:
                        ts_venue = iso(datetime.fromtimestamp(int(str(raw_ts)) / 1000, tz=UTC))
                    except (ValueError, OverflowError, OSError):
                        ts_venue = None
                bids = sorted(_levels(book.get("bids")), key=lambda lv: Decimal(lv[0]), reverse=True)
                asks = sorted(_levels(book.get("asks")), key=lambda lv: Decimal(lv[0]))
                records.append(
                    {
                        "kind": "book",
                        "v": SCHEMA_VERSION,
                        "venue": self.venue,
                        "market_id": condition_id,
                        "token_id": token,
                        "outcome": outcome,
                        "ts": iso(captured),
                        "ts_venue": ts_venue,
                        "cycle": cycle,
                        "latency_ms": int((captured - requested).total_seconds() * 1000),
                        "depth_requested": None,  # CLOB /books returns the full aggregated ladder
                        "bids": bids,
                        "asks": asks,
                        "hash": book.get("hash"),
                        "tick_size": dec_str(book.get("tick_size")),
                        "min_order_size": dec_str(book.get("min_order_size")),
                        "neg_risk": book.get("neg_risk"),
                    }
                )
            for token in chunk:
                if token not in returned:
                    records.append({"error": f"book_missing[{token}]"})
        return records

    async def poll_trades(self, cycle: int) -> list[dict[str, Any]]:
        if not self.trades_enabled:
            return []

        async def one(condition_id: str) -> list[dict[str, Any]]:
            async with self._gate:
                captured = iso(utc_now())
                try:
                    payload = await self.fetch.json(
                        "GET", f"{DATA_API_URL}/trades", params={"market": condition_id, "limit": POLYMARKET_TRADES_PAGE}
                    )
                except Exception as exc:
                    return [{"error": f"trades[{condition_id}]: {type(exc).__name__}: {exc}"}]
                seen = self._seen_trade_keys.setdefault(condition_id, set())
                new: list[dict[str, Any]] = []
                keys: set[str] = set()
                for raw in payload if isinstance(payload, list) else []:
                    if not isinstance(raw, dict):
                        continue
                    # The Data-API exposes no trade id; the tuple below is the closest stable key.
                    key = "|".join(str(raw.get(k) or "") for k in ("transactionHash", "asset", "proxyWallet", "price", "size", "timestamp"))
                    keys.add(key)
                    if key in seen:
                        continue
                    price, size = dec_str(raw.get("price")), dec_str(raw.get("size"))
                    if price is None or size is None:
                        continue
                    raw_ts = raw.get("timestamp")
                    try:
                        ts_venue = iso(datetime.fromtimestamp(int(str(raw_ts)), tz=UTC)) if raw_ts not in (None, "") else None
                    except (ValueError, OverflowError, OSError):
                        ts_venue = None
                    new.append(
                        {
                            "kind": "trade",
                            "v": SCHEMA_VERSION,
                            "venue": self.venue,
                            "market_id": condition_id,
                            "token_id": str(raw.get("asset") or ""),
                            "outcome": str(raw.get("outcome") or "").lower() or None,
                            "trade_id": None,
                            "tx": raw.get("transactionHash"),
                            "ts": captured,
                            "ts_venue": ts_venue,
                            "cycle": cycle,
                            "price": price,
                            "size": size,
                            "taker_side": str(raw.get("side") or "").lower() or None,
                        }
                    )
                self._seen_trade_keys[condition_id] = keys
                return new

        chunks = await asyncio.gather(*(one(c) for c in self.universe_ids()))
        return [record for chunk in chunks for record in chunk]


# --------------------------------------------------------------------------
# Logger loop
# --------------------------------------------------------------------------
@dataclass(slots=True)
class LoggerConfig:
    interval: float = 30.0
    universe_refresh_every: int = 20  # cycles
    trades: bool = True
    skip_unchanged: bool = False


class BookLogger:
    def __init__(
        self,
        sources: list[BookSource],
        archive: JsonlArchive,
        *,
        config: LoggerConfig | None = None,
        fetcher: Fetcher | None = None,
        sidecars: list[SidecarSource] | None = None,
        run_id: str | None = None,
        cli_args: dict[str, Any] | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.sources = sources
        self.archive = archive
        self.config = config or LoggerConfig()
        self.fetcher = fetcher
        self.sidecars = sidecars or []
        self.run_id = run_id or f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}-books-{uuid.uuid4().hex[:8]}"
        self.log = log
        self.cycle = 0
        self._last_book_key: dict[tuple[str, str, str | None], str] = {}
        self.session: dict[str, Any] = {
            "run_id": self.run_id,
            "schema_version": SCHEMA_VERSION,
            "paper_only": True,
            "started_at": iso(utc_now()),
            "finished_at": None,
            "config": {
                "interval_s": self.config.interval,
                "universe_refresh_every": self.config.universe_refresh_every,
                "trades": self.config.trades,
                "skip_unchanged": self.config.skip_unchanged,
                "venues": [s.venue for s in sources],
                **(cli_args or {}),
            },
            "cycles": 0,
            "overruns": 0,
            "counts": {"books": 0, "trades": 0, "markets": 0, "unchanged_skipped": 0, "errors": 0},
            "universe": {},
            "last_errors": [],
            "fetch": {},
        }

    def _dedupe(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.config.skip_unchanged:
            return records
        kept = []
        for record in records:
            key = (record["venue"], record["market_id"], record.get("token_id"))
            digest = json.dumps([record["bids"], record["asks"]], separators=(",", ":"))
            if self._last_book_key.get(key) == digest:
                self.session["counts"]["unchanged_skipped"] += 1
                continue
            self._last_book_key[key] = digest
            kept.append(record)
        return kept

    async def run_once(self) -> list[CycleResult]:
        self.cycle += 1
        results: list[CycleResult] = []
        for source in self.sources:
            result = CycleResult(venue=source.venue)
            due = (self.cycle - 1) % max(1, self.config.universe_refresh_every) == 0
            if due or not source.universe_ids():
                try:
                    markets = await source.refresh_universe()
                    result.markets = self.archive.append(source.venue, "markets", markets)
                except Exception as exc:
                    result.errors.append(f"universe: {type(exc).__name__}: {exc}")
            books = await source.poll_books(self.cycle)
            good = [r for r in books if "error" not in r]
            result.errors.extend(r["error"] for r in books if "error" in r)
            result.books = self.archive.append(source.venue, "books", self._dedupe(good))
            if self.config.trades:
                trades = await source.poll_trades(self.cycle)
                result.errors.extend(r["error"] for r in trades if "error" in r)
                result.trades = self.archive.append(source.venue, "trades", [r for r in trades if "error" not in r])
            self.session["universe"][source.venue] = len(source.universe_ids())
            results.append(result)
        for sidecar in self.sidecars:
            try:
                records = await sidecar.poll()
                for record in records:
                    record.setdefault("ts", iso(utc_now()))
                self.archive.append(sidecar.name, sidecar.name, records)
            except Exception as exc:
                results.append(CycleResult(venue=sidecar.name, errors=[f"sidecar: {type(exc).__name__}: {exc}"]))
        self._account(results)
        return results

    def _account(self, results: list[CycleResult]) -> None:
        counts = self.session["counts"]
        errors: list[str] = []
        for r in results:
            counts["books"] += r.books
            counts["trades"] += r.trades
            counts["markets"] += r.markets
            counts["errors"] += len(r.errors)
            errors.extend(r.errors)
        self.session["cycles"] = self.cycle
        self.session["last_errors"] = errors[-20:]
        if self.fetcher is not None:
            self.session["fetch"] = {
                "requests": self.fetcher.stats.requests,
                "retries": self.fetcher.stats.retries,
                "rate_limited": self.fetcher.stats.rate_limited,
                "errors": self.fetcher.stats.errors,
                "limiter_waits": self.fetcher.limiter.waits,
            }
        self.session["archive"] = {"lines": self.archive.lines_written, "bytes": self.archive.bytes_written, "root": str(self.archive.root)}
        self.archive.write_session(self.session)
        summary = {
            "cycle": self.cycle,
            "ts": iso(utc_now()),
            **{f"{r.venue}": {"books": r.books, "trades": r.trades, "markets": r.markets, "errors": len(r.errors)} for r in results},
        }
        self.log(json.dumps(summary, separators=(",", ":")))
        for error in errors[:5]:
            self.log(f"  ! {error}")

    async def run(
        self,
        *,
        max_cycles: int | None = None,
        duration: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> dict[str, Any]:
        started = clock()
        try:
            while True:
                cycle_started = clock()
                await self.run_once()
                if max_cycles is not None and self.cycle >= max_cycles:
                    break
                elapsed = clock() - cycle_started
                if duration is not None and clock() - started + self.config.interval > duration:
                    break
                remaining = self.config.interval - elapsed
                if remaining < 0:
                    self.session["overruns"] += 1
                    self.log(f"  ! cycle {self.cycle} took {elapsed:.1f}s > interval {self.config.interval:.1f}s; starting next cycle now")
                    continue
                await sleep(remaining)
        finally:
            self.session["finished_at"] = iso(utc_now())
            self.archive.write_session(self.session)
        return self.session
