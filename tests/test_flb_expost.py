import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from research.flb import KalshiFeeModel
from research.flb_expost import (
    FIXTURE_PATH,
    SettledMarket,
    TradeRecord,
    expost_band_table,
    expost_report,
    flb_verdict,
    harvest_settled_trades,
    load_settled_trades,
    maker_fade_verdict,
    not_measured_report,
)

D = Decimal
T0 = datetime(2026, 6, 1, tzinfo=UTC)


def _market(ticker: str, result: str, trades: list[tuple[str, str, str]], *, series: str = "S", category: str = "economics", fee_type: str = "quadratic_with_maker_fees", multiplier: str = "1", hours_before_close: float = 48) -> SettledMarket:
    return SettledMarket(
        ticker=ticker,
        series_ticker=series,
        result=result,
        close_time=T0,
        category=category,
        fee_type=fee_type,
        fee_multiplier=D(multiplier),
        trades=[TradeRecord(T0 - timedelta(hours=hours_before_close), side, D(price), D(count)) for side, price, count in trades],
    )


def test_fixture_exhibits_flb_and_positive_maker_edge() -> None:
    markets, meta = load_settled_trades(FIXTURE_PATH)
    assert meta["source"] == "fixture" and len(markets) == 28
    report = expost_report(markets)
    assert report["status"] == "measured_from_settled_trades"
    verdicts = report["verdicts"]
    assert verdicts["ex_post_flb"]["verdict"] == "PASS"
    assert verdicts["ex_post_flb_excluding_final_minutes"]["verdict"] == "PASS"
    assert verdicts["maker_fade_edge_after_fees"]["verdict"] == "PASS"
    longshot = verdicts["ex_post_flb"]["longshot"]
    assert longshot["n_markets"] >= 10 and longshot["taker_gross_per_contract"] < 0
    assert longshot["maker_gross_per_contract"] == -longshot["taker_gross_per_contract"]
    assert longshot["maker_net_per_contract"] < longshot["maker_gross_per_contract"]  # maker fee deducted
    assert longshot["taker_fee_per_contract"] > longshot["maker_fee_per_contract"]  # 0.07 vs 0.0175 (x0.5 on MLB)
    assert report["by_category"]["sports"]["flb"]["verdict"] == "INSUFFICIENT_DATA"  # 4 MLB markets only
    assert report["by_category"]["economics"]["flb"]["verdict"] == "PASS"
    # End-game trades sit within the final 30 minutes; excluding them removes trades but not markets.
    all_t = report["band_table"]
    early = report["band_table_excluding_final_minutes"]
    assert sum(c["n_trades"] for c in early.values()) < sum(c["n_trades"] for c in all_t.values())
    json.dumps(report, default=str)


def test_taker_and_maker_returns_are_computed_per_trade() -> None:
    # Taker buys YES at 0.05 x 100 in a market that settles NO: taker loses 5 per 100, maker gains 5.
    lose = _market("L", "no", [("yes", "0.05", "100")])
    # Taker buys NO at 0.95 (= YES price 0.05) x 100 in a market that settles NO: taker wins 5.
    win = _market("W", "no", [("no", "0.05", "100")])
    table = expost_band_table([lose, win])
    assert table["bands"]["<10c"]["taker_gross_per_contract"] == -0.05
    assert table["bands"]["<10c"]["taker_win_rate"] == 0.0
    assert table["bands"][">=90c"]["taker_gross_per_contract"] == 0.05
    assert table["bands"][">=90c"]["taker_win_rate"] == 1.0
    fee = float(KalshiFeeModel().per_contract(D("0.05"), maker=False))  # 0.003325
    assert table["bands"]["<10c"]["taker_net_per_contract"] == round(-0.05 - fee, 5)
    maker_fee = float(KalshiFeeModel().per_contract(D("0.05"), maker=True))  # 0.00083125
    assert table["bands"]["<10c"]["maker_net_per_contract"] == round(0.05 - maker_fee, 5)
    # Series without maker fees: the maker keeps the whole gross.
    plain = expost_band_table([_market("P", "no", [("yes", "0.05", "100")], fee_type="quadratic")])
    assert plain["bands"]["<10c"]["maker_fee_per_contract"] == 0.0
    assert plain["bands"]["<10c"]["maker_net_per_contract"] == 0.05


def test_fair_prices_fail_the_flb_test_and_small_samples_are_insufficient() -> None:
    # Longshots at 0.10 that win exactly 2 of 20 markets: zero expected taker return.
    markets = [_market(f"F{i}", "yes" if i < 2 else "no", [("yes", "0.10", "200")]) for i in range(20)]
    table = expost_band_table(markets)
    verdict = flb_verdict(table)
    assert verdict["verdict"] == "FAIL"
    assert abs(table["longshot"]["taker_gross_per_contract"]) < 1e-9
    assert maker_fade_verdict(table)["verdict"] == "FAIL"

    few = expost_band_table(markets[:5])
    assert flb_verdict(few)["verdict"] == "INSUFFICIENT_DATA"
    assert maker_fade_verdict(few)["verdict"] == "INSUFFICIENT_DATA"
    assert flb_verdict(expost_band_table([]))["verdict"] == "INSUFFICIENT_DATA"


def test_market_clustering_prevents_one_market_from_looking_like_many_trades() -> None:
    # 1 market with 500 identical losing longshot trades: enough contracts, but one cluster -> no SE.
    one = _market("ONE", "no", [("yes", "0.05", "10")] * 500)
    table = expost_band_table([one])
    assert table["longshot"]["n_markets"] == 1 and table["longshot"]["clustered_se"] is None
    assert flb_verdict(table)["verdict"] == "INSUFFICIENT_DATA"


def test_exclude_final_minutes_drops_endgame_trades() -> None:
    market = _market("E", "no", [("yes", "0.01", "100")], hours_before_close=0.1)
    market.trades.append(TradeRecord(T0 - timedelta(hours=30), "yes", D("0.08"), D("100")))
    full = expost_band_table([market])
    early = expost_band_table([market], exclude_final_minutes=60)
    assert full["bands"]["<10c"]["n_trades"] == 2
    assert early["bands"]["<10c"]["n_trades"] == 1
    assert early["bands"]["<10c"]["taker_gross_per_contract"] == -0.08


def test_not_measured_report_is_explicit() -> None:
    report = not_measured_report("no harvest")
    assert report["status"] == "not_measured"
    assert report["verdicts"]["ex_post_flb"]["verdict"] == "INSUFFICIENT_DATA"
    assert "--harvest-trades" in report["how_to_measure"]


async def test_harvest_reads_public_endpoints_with_pagination_and_truncation() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        path = request.url.path
        if path.endswith("/series/KXFEDDECISION"):
            return httpx.Response(200, json={"series": {"ticker": "KXFEDDECISION", "category": "Economics", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1, "title": "Fed", "frequency": "custom"}})
        if path.endswith("/markets"):
            assert request.url.params["status"] == "settled"
            return httpx.Response(200, json={"markets": [
                {"ticker": "KXFEDDECISION-26JUL-H26", "event_ticker": "KXFEDDECISION-26JUL", "result": "no", "close_time": "2026-07-29T17:59:00Z", "volume_fp": "100.00"},
                {"ticker": "KXFEDDECISION-26JUL-OPEN", "result": "", "status": "open"},
            ]})
        if path.endswith("/markets/trades"):
            if request.url.params.get("cursor"):
                return httpx.Response(200, json={"cursor": "more", "trades": [{"created_time": "2026-07-29T17:00:00Z", "taker_side": "no", "yes_price_dollars": "0.0200", "count_fp": "5.00"}]})
            return httpx.Response(200, json={"cursor": "page2", "trades": [
                {"created_time": "2026-07-29T17:58:04Z", "taker_side": "yes", "yes_price_dollars": "0.0100", "count_fp": "2244.45"},
                {"created_time": "2026-07-29T17:57:42Z", "taker_side": "yes", "yes_price": 3, "count": 10},
            ]})
        return httpx.Response(404, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        payload = await harvest_settled_trades(kalshi_env="prod", series=("KXFEDDECISION",), settled_per_series=5, max_trades_per_market=3, http=http)
    finally:
        await http.aclose()
    assert payload["series"]["KXFEDDECISION"]["fee_multiplier"] == 1
    (market,) = payload["markets"]  # the open market without a result is dropped
    assert market["ticker"] == "KXFEDDECISION-26JUL-H26"
    assert market["trades_truncated"] is True and len(market["trades"]) == 3
    assert market["trades"][1] == {"t": "2026-07-29T17:57:42Z", "s": "yes", "p": "0.03", "c": "10"}  # legacy cents parsed
    assert all("demo" not in url for url in calls)
    assert not any("/orders" in url or "/portfolio" in url for url in calls)  # read-only, public only
