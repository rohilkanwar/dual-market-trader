"""Ex-post Kalshi maker-vs-taker returns by price band from settled markets.

This is the only part of the FLB track that *measures* favorite–longshot bias
rather than describing book structure. It follows the Bürgi/Deng/Whelan design:
every public trade carries ``taker_side``, so for a settled market with a known
``result`` each trade splits into

* taker: bought side ``s`` at price ``p`` -> gross return ``1[s wins] - p``,
  fee ``M·0.07·p·(1-p)`` per contract;
* maker: the counterparty -> gross return ``-(taker gross)``, fee
  ``M·0.0175·p·(1-p)`` only on ``quadratic_with_maker_fees`` series.

Trades are bucketed by the price the taker paid for the side it bought. Because
every trade in a market shares one outcome, markets are the unit of inference:
band statistics are contract-weighted means across markets with a
market-clustered standard error. Data comes from two unauthenticated endpoints
(``GET /markets?status=settled`` and ``GET /markets/trades``); no keys are used.

Known limitations, all reported in the output rather than hidden:

* ``/markets/trades`` pages newest-first, so a per-market trade cap keeps the
  trades closest to settlement (``trades_truncated`` per market); the
  ``excluding_final_minutes`` table removes end-game trades near 0/100.
* Fee rounding is per order at a centicent; per-contract fees here are unrounded.
* Series fee parameters are read at harvest time; historical changes are ignored.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from core.types import ONE, ZERO, Market, Venue
from research.flb import (
    BAND_ORDER,
    FAVORITE_BANDS,
    LONGSHOT_BANDS,
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT,
    VERDICT_PASS,
    KalshiFeeModel,
    band_for,
)
from venues.kalshi.client import DEFAULT_MACRO_SERIES, HOSTS

SCHEMA_VERSION = "1.0.0"
FIXTURE_PATH = Path(__file__).resolve().parents[1] / "venues" / "kalshi" / "fixtures" / "flb_settled_trades.json"
HARVEST_FILE = "kalshi_settled_trades.json"
DEFAULT_SPORTS_SERIES: tuple[str, ...] = ("KXMLBGAME", "KXNFLGAME", "KXNCAAFGAME")
DEFAULT_EXPOST_SERIES: tuple[str, ...] = tuple(DEFAULT_MACRO_SERIES) + DEFAULT_SPORTS_SERIES
Q4 = Decimal("0.0001")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TradeRecord:
    created_time: datetime
    taker_side: str  # "yes" | "no"
    yes_price: Decimal
    count: Decimal

    @property
    def taker_price(self) -> Decimal:
        return self.yes_price if self.taker_side == "yes" else ONE - self.yes_price


@dataclass(slots=True)
class SettledMarket:
    ticker: str
    series_ticker: str
    result: str  # "yes" | "no"
    close_time: datetime | None
    category: str
    fee_type: str | None
    fee_multiplier: Decimal
    trades: list[TradeRecord] = field(default_factory=list)
    trades_truncated: bool = False
    volume: Decimal = ZERO

    def as_market(self) -> Market:
        return Market(
            venue=Venue.KALSHI,
            market_id=self.ticker,
            title=self.ticker,
            metadata={"fee_type": self.fee_type, "fee_multiplier": self.fee_multiplier, "series_ticker": self.series_ticker, "category": self.category},
        )


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _dec(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return default


def load_settled_trades(path: Path) -> tuple[list[SettledMarket], dict[str, Any]]:
    """Parse a harvest/fixture payload. Returns (markets, meta)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    series_info: dict[str, dict[str, Any]] = payload.get("series") or {}
    markets: list[SettledMarket] = []
    for item in payload.get("markets", []):
        result = str(item.get("result") or "").lower()
        if result not in ("yes", "no"):
            continue
        series = str(item.get("series_ticker") or "")
        info = series_info.get(series, {})
        trades: list[TradeRecord] = []
        for raw in item.get("trades", []):
            created = _dt(raw.get("t") or raw.get("created_time"))
            side = str(raw.get("s") or raw.get("taker_side") or "").lower()
            price = _dec(raw.get("p") if "p" in raw else raw.get("yes_price_dollars"), Decimal("-1"))
            count = _dec(raw.get("c") if "c" in raw else raw.get("count_fp"))
            if created is None or side not in ("yes", "no") or not ZERO <= price <= ONE or count <= ZERO:
                continue
            trades.append(TradeRecord(created, side, price, count))
        markets.append(
            SettledMarket(
                ticker=str(item.get("ticker")),
                series_ticker=series,
                result=result,
                close_time=_dt(item.get("close_time")),
                category=str(item.get("category") or info.get("category") or "").lower(),
                fee_type=item.get("fee_type") or info.get("fee_type"),
                fee_multiplier=_dec(item.get("fee_multiplier", info.get("fee_multiplier")), ONE),
                trades=trades,
                trades_truncated=bool(item.get("trades_truncated", False)),
                volume=_dec(item.get("volume_fp") or item.get("volume")),
            )
        )
    meta = {k: payload.get(k) for k in ("schema_version", "source", "kalshi_env", "harvested_at", "series_requested", "settled_per_series", "max_trades_per_market")}
    meta["series"] = series_info
    return markets, meta


# --------------------------------------------------------------------------
# Harvest (public, read-only)
# --------------------------------------------------------------------------
async def harvest_settled_trades(
    *,
    kalshi_env: str = "prod",
    series: tuple[str, ...] = DEFAULT_EXPOST_SERIES,
    settled_per_series: int = 15,
    max_trades_per_market: int = 2000,
    concurrency: int = 4,
    timeout: float = 30.0,
    http: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch settled markets and their public trades. Returns a JSON-ready payload."""
    base = HOSTS[kalshi_env]
    owns = http is None
    client = http or httpx.AsyncClient(timeout=timeout)
    gate = asyncio.Semaphore(max(1, concurrency))
    errors: list[str] = []

    async def get(path: str, **params: Any) -> dict[str, Any]:
        async with gate:
            response = await client.get(f"{base}{path}", params=params)
            response.raise_for_status()
            return response.json()

    async def series_info(ticker: str) -> dict[str, Any]:
        try:
            info = (await get(f"/series/{ticker}")).get("series") or {}
        except httpx.HTTPError as exc:
            errors.append(f"series[{ticker}]: {type(exc).__name__}: {exc}")
            return {}
        return {k: info.get(k) for k in ("category", "fee_type", "fee_multiplier", "title", "frequency")}

    async def settled(ticker: str) -> list[dict[str, Any]]:
        try:
            payload = await get("/markets", series_ticker=ticker, status="settled", limit=min(max(settled_per_series, 1), 200))
        except httpx.HTTPError as exc:
            errors.append(f"settled[{ticker}]: {type(exc).__name__}: {exc}")
            return []
        return [m for m in payload.get("markets", []) if isinstance(m, dict) and str(m.get("result", "")).lower() in ("yes", "no")][:settled_per_series]

    async def trades(ticker: str) -> tuple[list[dict[str, Any]], bool]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        truncated = False
        while True:
            params: dict[str, Any] = {"ticker": ticker, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                payload = await get("/markets/trades", **params)
            except httpx.HTTPError as exc:
                errors.append(f"trades[{ticker}]: {type(exc).__name__}: {exc}")
                break
            for raw in payload.get("trades", []):
                if not isinstance(raw, dict):
                    continue
                out.append({
                    "t": raw.get("created_time"),
                    "s": str(raw.get("taker_side") or "").lower(),
                    "p": raw.get("yes_price_dollars") if raw.get("yes_price_dollars") not in (None, "") else str(_dec(raw.get("yes_price")) / Decimal("100")),
                    "c": raw.get("count_fp") if raw.get("count_fp") not in (None, "") else str(_dec(raw.get("count"))),
                })
            cursor = payload.get("cursor") or None
            if len(out) >= max_trades_per_market:
                truncated = bool(cursor)
                out = out[:max_trades_per_market]
                break
            if not cursor:
                break
        return out, truncated

    try:
        infos = dict(zip(series, await asyncio.gather(*(series_info(t) for t in series))))
        settled_lists = await asyncio.gather(*(settled(t) for t in series))
        jobs: list[tuple[str, dict[str, Any]]] = [(t, m) for t, ms in zip(series, settled_lists) for m in ms]
        trade_results = await asyncio.gather(*(trades(str(m["ticker"])) for _, m in jobs))
    finally:
        if owns:
            await client.aclose()

    markets_out = []
    for (ticker, m), (rows, truncated) in zip(jobs, trade_results):
        markets_out.append({
            "ticker": m.get("ticker"),
            "series_ticker": ticker,
            "event_ticker": m.get("event_ticker"),
            "title": m.get("title"),
            "result": str(m.get("result")).lower(),
            "close_time": m.get("close_time"),
            "open_time": m.get("open_time"),
            "settlement_ts": m.get("settlement_ts"),
            "volume_fp": m.get("volume_fp") or m.get("volume"),
            "trades_truncated": truncated,
            "trades": rows,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "network",
        "paper_only": True,
        "kalshi_env": kalshi_env,
        "harvested_at": datetime.now(UTC).isoformat(),
        "series_requested": list(series),
        "settled_per_series": settled_per_series,
        "max_trades_per_market": max_trades_per_market,
        "series": infos,
        "errors": errors,
        "markets": markets_out,
    }


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
@dataclass(slots=True)
class _Cluster:
    contracts: float = 0.0
    taker_gross: float = 0.0
    taker_fee: float = 0.0
    maker_fee: float = 0.0
    trades: int = 0
    wins: float = 0.0  # contracts on which the taker won


def _weighted_stats(clusters: list[_Cluster]) -> dict[str, Any]:
    """Contract-weighted mean of per-market taker gross return with a clustered SE."""
    weights = [c.contracts for c in clusters if c.contracts > 0]
    if not weights:
        return {"n_markets": 0, "n_trades": 0, "contracts": 0.0}
    total_w = sum(weights)
    means = [c.taker_gross / c.contracts for c in clusters if c.contracts > 0]
    mu = sum(w * m for w, m in zip(weights, means)) / total_w
    var = sum(w * (m - mu) ** 2 for w, m in zip(weights, means)) / total_w
    n_eff = total_w**2 / sum(w * w for w in weights)
    se = math.sqrt(var / (n_eff - 1)) if n_eff > 1 and var > 0 else None
    taker_fee = sum(c.taker_fee for c in clusters) / total_w
    maker_fee = sum(c.maker_fee for c in clusters) / total_w
    contracts = sum(c.contracts for c in clusters)
    return {
        "n_markets": len(weights),
        "n_trades": sum(c.trades for c in clusters),
        "contracts": round(contracts, 2),
        "taker_win_rate": round(sum(c.wins for c in clusters) / contracts, 4),
        "taker_gross_per_contract": round(mu, 5),
        "taker_fee_per_contract": round(taker_fee, 5),
        "taker_net_per_contract": round(mu - taker_fee, 5),
        "maker_gross_per_contract": round(-mu, 5),
        "maker_fee_per_contract": round(maker_fee, 5),
        "maker_net_per_contract": round(-mu - maker_fee, 5),
        "clustered_se": round(se, 5) if se is not None else None,
        "t_stat_taker_gross": round(mu / se, 2) if se else None,
        "effective_n_markets": round(n_eff, 2),
    }


def _accumulate(markets: list[SettledMarket], fee_model: KalshiFeeModel, *, exclude_final_minutes: int | None) -> dict[str, dict[str, _Cluster]]:
    by_band: dict[str, dict[str, _Cluster]] = {}
    for market in markets:
        proxy = market.as_market()
        for trade in market.trades:
            if exclude_final_minutes is not None:
                if market.close_time is None:
                    continue
                minutes = (market.close_time - trade.created_time).total_seconds() / 60
                if minutes < exclude_final_minutes:
                    continue
            price = trade.taker_price
            if not ZERO < price < ONE:
                continue
            band = band_for(price)
            cluster = by_band.setdefault(band, {}).setdefault(market.ticker, _Cluster())
            won = trade.taker_side == market.result
            count = float(trade.count)
            cluster.contracts += count
            cluster.trades += 1
            cluster.wins += count if won else 0.0
            cluster.taker_gross += count * ((1.0 - float(price)) if won else -float(price))
            cluster.taker_fee += count * float(fee_model.per_contract(price, maker=False, market=proxy))
            cluster.maker_fee += count * float(fee_model.per_contract(price, maker=True, market=proxy))
    return by_band


def _merge_clusters(by_band: dict[str, dict[str, _Cluster]], bands: tuple[str, ...]) -> list[_Cluster]:
    merged: dict[str, _Cluster] = {}
    for band in bands:
        for ticker, cluster in by_band.get(band, {}).items():
            target = merged.setdefault(ticker, _Cluster())
            target.contracts += cluster.contracts
            target.taker_gross += cluster.taker_gross
            target.taker_fee += cluster.taker_fee
            target.maker_fee += cluster.maker_fee
            target.trades += cluster.trades
            target.wins += cluster.wins
    return list(merged.values())


def expost_band_table(markets: list[SettledMarket], *, fee_model: KalshiFeeModel | None = None, exclude_final_minutes: int | None = None) -> dict[str, Any]:
    fee_model = fee_model or KalshiFeeModel()
    by_band = _accumulate(markets, fee_model, exclude_final_minutes=exclude_final_minutes)
    bands = {band: _weighted_stats(list(by_band[band].values())) for band in BAND_ORDER if band in by_band}
    return {
        "exclude_final_minutes": exclude_final_minutes,
        "bands": bands,
        "longshot": _weighted_stats(_merge_clusters(by_band, LONGSHOT_BANDS)),
        "favorite": _weighted_stats(_merge_clusters(by_band, FAVORITE_BANDS)),
        "all": _weighted_stats(_merge_clusters(by_band, BAND_ORDER)),
    }


def flb_verdict(table: dict[str, Any], *, min_markets: int = 10, min_contracts: float = 1000.0, t_threshold: float = 2.0) -> dict[str, Any]:
    """PASS = longshot takers lose significantly and do worse than favourite takers."""
    longshot, favorite = table["longshot"], table["favorite"]
    base = {
        "question": "Do takers who buy longshots (<20c) earn significantly negative gross returns, below favourite (>=80c) takers?",
        "longshot": longshot,
        "favorite": favorite,
        "thresholds": {"min_markets": min_markets, "min_contracts": min_contracts, "t_stat": -t_threshold},
    }
    if longshot.get("n_markets", 0) < min_markets or longshot.get("contracts", 0.0) < min_contracts:
        return {"verdict": VERDICT_INSUFFICIENT, "reason": f"longshot bands need >= {min_markets} markets and >= {min_contracts:g} contracts", **base}
    t = longshot.get("t_stat_taker_gross")
    gross = longshot["taker_gross_per_contract"]
    worse_than_favorites = favorite.get("n_markets", 0) == 0 or gross < favorite["taker_gross_per_contract"]
    if gross < 0 and t is not None and t <= -t_threshold and worse_than_favorites:
        return {"verdict": VERDICT_PASS, "reason": "longshot taker gross return negative, |t| >= threshold, below favourite band", **base}
    if gross >= 0:
        reason = "longshot takers did not lose on average"
    elif t is None or t > -t_threshold:
        reason = "longshot taker losses not significant at the market-cluster level"
    else:
        reason = "longshot takers lost, but not more than favourite takers"
    return {"verdict": VERDICT_FAIL, "reason": reason, **base}


def maker_fade_verdict(table: dict[str, Any], *, min_markets: int = 10, min_contracts: float = 1000.0, t_threshold: float = 2.0) -> dict[str, Any]:
    """PASS = being the maker against longshot buyers earned a positive return after maker fees."""
    longshot = table["longshot"]
    base = {
        "question": "Did the maker side of longshot (<20c) trades earn a positive return per contract after maker fees?",
        "longshot": longshot,
        "thresholds": {"min_markets": min_markets, "min_contracts": min_contracts, "t_stat": t_threshold},
    }
    if longshot.get("n_markets", 0) < min_markets or longshot.get("contracts", 0.0) < min_contracts:
        return {"verdict": VERDICT_INSUFFICIENT, "reason": "insufficient longshot sample", **base}
    net, se = longshot["maker_net_per_contract"], longshot.get("clustered_se")
    t = net / se if se else None
    if net > 0 and t is not None and t >= t_threshold:
        return {"verdict": VERDICT_PASS, "reason": "maker net return positive and significant", "t_stat_maker_net": round(t, 2), **base}
    return {"verdict": VERDICT_FAIL, "reason": "maker net return not significantly positive after fees", "t_stat_maker_net": round(t, 2) if t is not None else None, **base}


def expost_report(
    markets: list[SettledMarket],
    *,
    fee_model: KalshiFeeModel | None = None,
    min_markets: int = 10,
    min_contracts: float = 1000.0,
    exclude_final_minutes: int = 60,
) -> dict[str, Any]:
    fee_model = fee_model or KalshiFeeModel()
    all_trades = expost_band_table(markets, fee_model=fee_model)
    early = expost_band_table(markets, fee_model=fee_model, exclude_final_minutes=exclude_final_minutes)
    categories: dict[str, Any] = {}
    for category in sorted({m.category or "unknown" for m in markets}):
        subset = [m for m in markets if (m.category or "unknown") == category]
        table = expost_band_table(subset, fee_model=fee_model)
        categories[category] = {
            "markets": len(subset),
            "longshot": table["longshot"],
            "favorite": table["favorite"],
            "flb": {k: v for k, v in flb_verdict(table, min_markets=min_markets, min_contracts=min_contracts).items() if k in ("verdict", "reason")},
        }
    return {
        "status": "measured_from_settled_trades" if markets else "no_settled_trades",
        "markets": len(markets),
        "markets_with_trades": sum(1 for m in markets if m.trades),
        "trades": sum(len(m.trades) for m in markets),
        "contracts": round(sum(float(t.count) for m in markets for t in m.trades), 2),
        "markets_with_truncated_trades": sum(1 for m in markets if m.trades_truncated),
        "series": sorted({m.series_ticker for m in markets}),
        "fee_model": {"taker_rate": fee_model.taker_rate, "maker_rate": fee_model.maker_rate, "note": "per-contract, unrounded; M = series fee_multiplier"},
        "verdicts": {
            "ex_post_flb": flb_verdict(all_trades, min_markets=min_markets, min_contracts=min_contracts),
            "ex_post_flb_excluding_final_minutes": {**flb_verdict(early, min_markets=min_markets, min_contracts=min_contracts), "exclude_final_minutes": exclude_final_minutes},
            "maker_fade_edge_after_fees": maker_fade_verdict(all_trades, min_markets=min_markets, min_contracts=min_contracts),
        },
        "band_table": all_trades["bands"],
        "band_table_excluding_final_minutes": early["bands"],
        "by_category": categories,
    }


def not_measured_report(reason: str) -> dict[str, Any]:
    return {
        "status": "not_measured",
        "reason": reason,
        "verdicts": {
            "ex_post_flb": {"verdict": VERDICT_INSUFFICIENT, "reason": reason},
            "maker_fade_edge_after_fees": {"verdict": VERDICT_INSUFFICIENT, "reason": reason},
        },
        "how_to_measure": "python -m apps.measure_flb --network --kalshi-env prod --harvest-trades",
    }
