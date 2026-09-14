"""Book logger: mocked public API -> JSONL archive, incremental trades, rate limiting."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from apps.book_logger import build_parser, run
from research.book_log import (
    BookLogger,
    Fetcher,
    JsonlArchive,
    KalshiBookSource,
    LoggerConfig,
    PolymarketBookSource,
    RateLimited,
    RateLimiter,
    dec_str,
    export_parquet,
    iter_records,
)

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "research" / "fixtures" / "book_logger_public_api.json").read_text())


class PublicApi:
    """Stateful mock of the public endpoints. ``new_print`` reveals trade t4."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.new_print = False
        self.throttle_next = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(f"{request.method} {url}")
        path = request.url.path
        assert "demo-api" not in url and "/portfolio" not in path and "/orders" not in path  # public + read-only
        if self.throttle_next:
            self.throttle_next -= 1
            return httpx.Response(429, headers={"Retry-After": "0.01"}, json={"error": "rate limited"})
        k, p = FIXTURE["kalshi"], FIXTURE["polymarket"]
        if path.endswith("/trade-api/v2/markets"):
            assert request.url.params["status"] == "open"
            return httpx.Response(200, json=k["markets_page2"] if request.url.params.get("cursor") else k["markets_page1"])
        if path.endswith("/orderbook"):
            if "C25" in path:
                assert request.url.params.get("depth") in (None, "25")  # depth=0 omits the param
                return httpx.Response(200, json=k["orderbook_fp"])
            return httpx.Response(200, json=k["orderbook_legacy_cents"])
        if path.endswith("/markets/trades"):
            if request.url.params["ticker"] != "KXFEDDECISION-26SEP-C25":
                return httpx.Response(200, json=k["trades_empty"])
            if request.url.params.get("cursor"):
                return httpx.Response(200, json=k["trades_empty"])
            return httpx.Response(200, json=k["trades_page1_after_new_print"] if self.new_print else k["trades_page1"])
        if path.endswith("/public-search"):
            return httpx.Response(200, json=p["public_search"])
        if path.endswith("/books"):
            requested = {row["token_id"] for row in json.loads(request.content)}
            return httpx.Response(200, json=[b for b in p["books"] if b["asset_id"] in requested])
        if request.url.host.startswith("data-api") and path.endswith("/trades"):
            return httpx.Response(200, json=p["trades"])
        return httpx.Response(404, json={"path": path})


def _fetcher(api: PublicApi, **kwargs: Any) -> tuple[httpx.AsyncClient, Fetcher]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    return http, Fetcher(http, RateLimiter(1000.0), **kwargs)


def _lines(path: Path) -> list[dict[str, Any]]:
    return list(iter_records([path]))


async def test_kalshi_cycle_writes_books_markets_and_trades(tmp_path: Path) -> None:
    api = PublicApi()
    http, fetcher = _fetcher(api)
    source = KalshiBookSource(fetcher, environment="prod", series=("KXFEDDECISION",), depth=25)
    logger = BookLogger([source], JsonlArchive(tmp_path), config=LoggerConfig(interval=0), fetcher=fetcher, run_id="run1")
    try:
        (result,) = await logger.run_once()
    finally:
        await http.aclose()
    assert result.errors == [] and result.markets == 2 and result.books == 2 and result.trades == 2

    (books_file,) = list(tmp_path.glob("kalshi/*/books.jsonl"))
    books = {r["market_id"]: r for r in _lines(books_file)}
    fp = books["KXFEDDECISION-26SEP-C25"]
    # YES bids best-first, NO bids complemented into YES asks ascending, canonical decimal strings.
    assert fp["bids"] == [["0.62", "120"], ["0.61", "50"]]
    assert fp["asks"] == [["0.64", "110"], ["0.65", "180"]]
    assert fp["raw"]["no_bids"] == [["0.36", "110"], ["0.35", "180"]]
    assert fp["depth_requested"] == 25 and fp["cycle"] == 1 and fp["ts"].endswith("Z") and fp["ts_venue"] is None
    legacy = books["KXFEDDECISION-26SEP-H"]
    assert legacy["bids"] == [["0.3", "40"]] and legacy["asks"] == [["0.33", "25"]]  # integer cents parsed

    markets = {r["market_id"]: r for r in _lines(books_file.with_name("markets.jsonl"))}
    assert markets["KXFEDDECISION-26SEP-C25"]["yes_bid"] == "0.62" and markets["KXFEDDECISION-26SEP-C25"]["series_ticker"] == "KXFEDDECISION"
    assert markets["KXFEDDECISION-26SEP-H"]["yes_ask"] == "0.33" and markets["KXFEDDECISION-26SEP-H"]["volume"] == "5000"

    trades = _lines(books_file.with_name("trades.jsonl"))
    assert [t["trade_id"] for t in trades] == ["t3", "t2"]
    assert trades[1] == {**trades[1], "price": "0.62", "size": "5", "taker_side": "no", "ts_venue": "2026-09-14T21:00:02Z"}

    session = json.loads((tmp_path / "sessions" / "run1.json").read_text())
    assert session["paper_only"] is True and session["counts"] == {"books": 2, "trades": 2, "markets": 2, "unchanged_skipped": 0, "errors": 0}
    assert session["universe"] == {"kalshi": 2} and session["fetch"]["requests"] == len(api.calls)
    # First trade poll fetches one page only (bounded backfill); listing paginated twice.
    assert sum("/markets/trades" in c for c in api.calls) == 2
    assert sum(c.endswith("status=open&limit=200") or "cursor=page2" in c for c in api.calls) == 2


async def test_second_cycle_is_incremental_and_skips_unchanged_books(tmp_path: Path) -> None:
    api = PublicApi()
    http, fetcher = _fetcher(api)
    source = KalshiBookSource(fetcher, series=("KXFEDDECISION",))
    logger = BookLogger([source], JsonlArchive(tmp_path), config=LoggerConfig(interval=0, skip_unchanged=True), fetcher=fetcher)
    try:
        await logger.run_once()
        api.new_print = True
        (second,) = await logger.run_once()
        (third,) = await logger.run_once()
    finally:
        await http.aclose()
    assert second.books == 0 and second.markets == 0  # unchanged books skipped; universe not re-listed until refresh
    assert second.trades == 1  # only t4 is new; t3/t2 already archived, pagination stopped at the first seen id
    assert third.trades == 0
    trades = list(iter_records(tmp_path.glob("kalshi/*/trades.jsonl")))
    assert [t["trade_id"] for t in trades] == ["t3", "t2", "t4"]
    assert len({t["trade_id"] for t in trades}) == 3
    assert logger.session["counts"]["unchanged_skipped"] == 4
    # Cycle 2 must not have paged past the first trades page (it saw t3 there).
    assert not any("/markets/trades" in c and "cursor=older" in c for c in api.calls)


async def test_polymarket_books_and_trades(tmp_path: Path) -> None:
    api = PublicApi()
    http, fetcher = _fetcher(api)
    source = PolymarketBookSource(fetcher, search_terms=("Fed decision",), trades=True)
    logger = BookLogger([source], JsonlArchive(tmp_path, compress=True), config=LoggerConfig(interval=0), fetcher=fetcher)
    try:
        (result,) = await logger.run_once()
    finally:
        await http.aclose()
    assert result.errors == [] and result.markets == 1 and result.books == 2 and result.trades == 2
    (books_file,) = list(tmp_path.glob("polymarket/*/books.jsonl.gz"))
    books = {r["token_id"]: r for r in _lines(books_file)}
    yes = books["111"]
    assert yes["market_id"] == "0xabc" and yes["outcome"] == "yes" and yes["ts_venue"] == "2026-09-14T21:00:00.123Z"
    assert yes["bids"] == [["0.62", "150.5"], ["0.61", "300"]] and yes["asks"] == [["0.64", "90"], ["0.65", "200"]]
    assert yes["hash"] == "h1" and yes["tick_size"] == "0.01" and yes["neg_risk"] is True
    assert books["222"]["outcome"] == "no"
    (market,) = _lines(books_file.with_name("markets.jsonl.gz"))
    assert market["token_ids"] == ["111", "222"] and market["neg_risk"] is True and market["event_slug"] == "fed-decision-september-2026"
    trades = _lines(books_file.with_name("trades.jsonl.gz"))
    assert {(t["token_id"], t["taker_side"], t["price"], t["size"]) for t in trades} == {("111", "buy", "0.64", "20"), ("222", "sell", "0.36", "7.5")}
    assert all(t["trade_id"] is None and t["tx"] for t in trades)
    # One batched POST /books for both tokens; the closed leg never reached the CLOB.
    assert sum(c.startswith("POST") and c.endswith("/books") for c in api.calls) == 1
    assert "333" not in json.dumps(api.calls)


async def test_fetcher_retries_on_429_and_gives_up_cleanly() -> None:
    api = PublicApi()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    http, fetcher = _fetcher(api, retries=2, sleep=fake_sleep)
    try:
        api.throttle_next = 2
        payload = await fetcher.json("GET", "https://api.elections.kalshi.com/trade-api/v2/markets", params={"status": "open", "limit": 200})
        assert payload["cursor"] == "page2"
        assert fetcher.stats.rate_limited == 2 and fetcher.stats.retries == 2 and sleeps == [0.01, 0.01]  # Retry-After honoured

        api.throttle_next = 5
        with pytest.raises(RateLimited):
            await fetcher.json("GET", "https://api.elections.kalshi.com/trade-api/v2/markets", params={"status": "open"})
        assert fetcher.stats.errors == 1 and fetcher.stats.retries == 4  # 1 + 2 retries, then gave up

        api.throttle_next = 0
        with pytest.raises(httpx.HTTPStatusError):  # a plain 404 is not retried
            await fetcher.json("GET", "https://api.elections.kalshi.com/trade-api/v2/nope")
        assert fetcher.stats.retries == 4 and fetcher.stats.errors == 2
    finally:
        await http.aclose()


async def test_rate_limiter_spaces_requests() -> None:
    now = [100.0]
    slept: list[float] = []
    limiter = RateLimiter(4.0, clock=lambda: now[0])

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds
        await real_sleep(0)

    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        for _ in range(3):
            await limiter.acquire()
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]
    assert slept == [0.25, 0.25] and limiter.waits == 2
    limiter.penalise(5.0)
    assert limiter._next_allowed >= now[0] + 5.0


async def test_source_errors_are_recorded_not_fatal(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/trade-api/v2/markets"):
            return httpx.Response(200, json={"markets": [{"ticker": "A"}, {"ticker": "B"}]})
        if request.url.path.endswith("/A/orderbook"):
            return httpx.Response(200, json={"orderbook_fp": {"yes_dollars": [["0.5", "1"]], "no_dollars": []}})
        return httpx.Response(404, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(http, RateLimiter(1000.0), retries=0)
    logger = BookLogger([KalshiBookSource(fetcher, series=("S",))], JsonlArchive(tmp_path), config=LoggerConfig(interval=0, trades=False), fetcher=fetcher)
    try:
        (result,) = await logger.run_once()
    finally:
        await http.aclose()
    assert result.books == 1 and len(result.errors) == 1 and result.errors[0].startswith("book[B]: HTTPStatusError")
    assert logger.session["last_errors"] == result.errors


async def test_run_loop_honours_cycles_and_overrun(tmp_path: Path) -> None:
    api = PublicApi()
    http, fetcher = _fetcher(api)
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    def clock() -> float:
        now[0] += 7.0  # every clock read advances 7 s -> each cycle "takes" 7 s
        return now[0]

    logger = BookLogger([KalshiBookSource(fetcher, series=("KXFEDDECISION",))], JsonlArchive(tmp_path), config=LoggerConfig(interval=30, trades=False), fetcher=fetcher, log=lambda _: None)
    try:
        session = await logger.run(max_cycles=3, clock=clock, sleep=fake_sleep)
    finally:
        await http.aclose()
    assert session["cycles"] == 3 and len(sleeps) == 2 and all(0 < s < 30 for s in sleeps)
    assert session["finished_at"] is not None and session["overruns"] == 0


async def test_cli_once_writes_archive_and_refuses_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = PublicApi()
    http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    args = build_parser().parse_args(["--once", "--root", str(tmp_path), "--series", "KXFEDDECISION", "--polymarket", "--max-rps", "1000"])
    try:
        session = await run(args, http=http)
    finally:
        await http.aclose()
    assert session["cycles"] == 1 and session["counts"]["books"] == 4 and session["counts"]["trades"] == 2  # Polymarket trades are opt-in
    assert session["config"]["venues"] == ["kalshi", "polymarket"] and session["config"]["kalshi_env"] == "prod"
    assert (tmp_path / "sessions" / f"{session['run_id']}.json").exists()

    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    with pytest.raises(ValueError, match="paper-only"):
        await run(build_parser().parse_args(["--once", "--root", str(tmp_path / "never")]))
    assert not (tmp_path / "never").exists()


def test_parquet_export_flattens_ladders(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    archive = JsonlArchive(tmp_path)
    archive.append("kalshi", "books", [{"kind": "book", "venue": "kalshi", "market_id": "A", "ts": "2026-09-14T21:00:00.000Z", "bids": [["0.5", "1"]], "asks": []}])
    counts = export_parquet(tmp_path, tmp_path / "pq")
    assert counts == {"kalshi/books": 1}
    import pyarrow.parquet as pq

    table = pq.read_table(tmp_path / "pq" / "kalshi_books.parquet")
    assert table.column("bids").to_pylist() == ['[["0.5", "1"]]']


def test_dec_str_canonical_forms() -> None:
    assert dec_str("0.6200") == "0.62" and dec_str("120.00") == "120" and dec_str(62, cents=True) == "0.62"
    assert dec_str("1E+2") == "100" and dec_str("") is None and dec_str("abc") is None and dec_str("NaN") is None
