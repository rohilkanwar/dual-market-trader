import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from apps.measure_all import json_default, persist_run
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.types import Fill, Market, OrderBook, Outcome, PriceLevel, Side, Venue
from research.scoreboard import VenueSnapshot
from research.weather_buckets import (
    DAILY_TEMPERATURE_TAG_ID,
    WEATHER_TRACK,
    CityDayRegister,
    classify_venue_resolution,
    load_replay,
    measure_weather_buckets,
    parse_city_day,
    parse_slug,
    replay_fixture,
    snapshot_from_events,
)
from research.weather_sources import (
    FixtureWeatherFeed,
    NullWeatherFeed,
    PublicWeatherFeed,
    Station,
    StationRegistry,
    metar_temperature_c,
    observed_extreme,
    paid_source_policy,
    parse_ensemble_payload,
    parse_metar_payload,
    parse_station_info,
    zone_for,
)
from strategies.weather_buckets import (
    WEATHER_RISK_LIMITS,
    CityDayResult,
    TemperatureBucket,
    WeatherBucketEdgeStrategy,
    WeatherEdgeParameters,
    buckets_partition_reason,
    ensemble_bucket_probabilities,
    parse_bucket_label,
    parse_settlement_rules,
    polymarket_fee_per_contract,
    round_half_up,
    verdict,
)
from venues.polymarket import PolymarketClient

D = Decimal
AS_OF = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)

# Verbatim Polymarket descriptions read from Gamma on 2026-09-15.
NYC_RULES = (
    "This market will resolve to the temperature range that contains the highest temperature recorded by NOAA at the "
    "LaGuardia Airport Station in degrees Fahrenheit on 15 Sep '26.\n\nThe resolution source for this market will be "
    "information from NOAA, specifically the highest reading under the \"Temp\" column for all times on this day, available "
    "here: https://www.weather.gov/wrh/timeseries?site=klga\n\nThis market will resolve off of the Hourly Data provided using "
    "the \"Show Hourly Data\" button.\n\nIf NOAA data for the observation date is unavailable by 11:59 PM ET on the day "
    "following the observation date, the Weather Underground Daily Observations table will be used as the resolution source."
    "\n\nTo toggle between Fahrenheit and Celsius, click the \"Switch to US Units w/ kts\" button until the relevant table "
    "displays °F.\n\nThe resolution source for this market measures temperatures to whole degrees Fahrenheit (eg, 21°F)."
)
TEL_AVIV_RULES = (
    "This market will resolve to the temperature range that contains the highest temperature recorded by NOAA at the Ben "
    "Gurion International Airport in degrees Celsius on 15 Sep '26.\n\nThe resolution source for this market will be "
    "information from NOAA, specifically the highest reading under the \"Temp\" column for all times on this day, available "
    "here: https://www.weather.gov/wrh/timeseries?site=LLBG"
)
TAIPEI_RULES = (
    "This market will resolve to the temperature range that contains the highest temperature recorded at the Taipei Songshan "
    "Airport Station in degrees Celsius on 15 Sep '26.\n\nThe resolution source for this market will be information from "
    "Wunderground, specifically the highest temperature recorded for all times on this day for the Taipei Songshan Airport "
    "Station, available here: https://www.wunderground.com/history/daily/tw/taipei/RCSS.\n\nIn the event that there is no data"
)
HONG_KONG_RULES = (
    "This market will resolve to the temperature range that contains the highest temperature recorded by the Hong Kong "
    "Observatory in degrees Celsius on 14 Sep '26.  The resolution source for this market will be information from the Hong "
    "Kong Observatory, specifically the \"Absolute Daily Max (deg. C)\" ... available here: https://www.weather.gov.hk/en/cis/climat.htm"
)
LONDON_LOW_RULES = (
    "This market will resolve to the temperature range that contains the lowest temperature recorded by NOAA at the London "
    "City Airport Station in degrees Celsius on 15 Sep '26.\n\nThe resolution source for this market will be information from "
    "NOAA, specifically the lowest reading under the \"Temp\" column for all times on this day, available here: "
    "https://www.weather.gov/wrh/timeseries?site=eglc"
)
NYC_LABELS = ["63°F or below", "64-65°F", "66-67°F", "68-69°F", "70-71°F", "72-73°F", "74-75°F", "76-77°F", "78-79°F", "80-81°F", "82°F or higher"]


def book(bid: str | None, ask: str | None, size: str = "300", market_id: str = "M") -> OrderBook:
    bids = (PriceLevel(D(bid), D(size)),) if bid is not None else ()
    asks = (PriceLevel(D(ask), D(size)),) if ask is not None else ()
    return OrderBook(market_id=market_id, bids=bids, asks=asks)


def market(market_id: str = "M", *, fee_rate: str = "0.05", active: bool = True) -> Market:
    return Market(venue=Venue.POLYMARKET, market_id=market_id, title=market_id, active=active, metadata={"taker_fee_rate": fee_rate})


def bucket(label: str) -> TemperatureBucket:
    parsed = parse_bucket_label(label)
    assert parsed is not None
    return parsed


# --------------------------------------------------------------------------
# Buckets
# --------------------------------------------------------------------------
def test_bucket_labels_parse_every_live_shape_and_reject_garbage() -> None:
    assert parse_bucket_label("66-67°F") == TemperatureBucket("66-67°F", 66, 67, "F")
    assert parse_bucket_label("65°F or below") == TemperatureBucket("65°F or below", None, 65, "F")
    assert parse_bucket_label("82°F or higher") == TemperatureBucket("82°F or higher", 82, None, "F")
    assert parse_bucket_label("27°C") == TemperatureBucket("27°C", 27, 27, "C")
    assert parse_bucket_label("between 66-67°F").lower == 66
    assert parse_bucket_label("Will the highest temperature in New York City be between 66-67°F on September 14?").upper == 67
    assert parse_bucket_label("Will the highest temperature in Hong Kong be 26°C or below on September 14?").upper == 26
    assert parse_bucket_label("-3--2°C") == TemperatureBucket("-3--2°C", -3, -2, "C")
    assert parse_bucket_label("Other") is None
    assert parse_bucket_label("67-66°F") is None
    assert parse_bucket_label("66°C-67°F") is None
    b = bucket("66-67°F")
    assert b.contains(66) and b.contains(67) and not b.contains(65) and not b.contains(68)
    assert bucket("65°F or below").contains(-40) and bucket("82°F or higher").contains(120)


def test_bucket_partition_check_is_fail_closed() -> None:
    buckets = [bucket(l) for l in NYC_LABELS]
    assert buckets_partition_reason(buckets) is None
    assert buckets_partition_reason(buckets[:-1]) == "no_open_upper_bucket"
    assert buckets_partition_reason(buckets[1:]) == "no_open_lower_bucket"
    assert buckets_partition_reason(buckets[:3] + buckets[4:]) == "gap_or_overlap"
    assert buckets_partition_reason(buckets[:1]) == "too_few_buckets"
    mixed = buckets[:-1] + [bucket("28°C or higher")]
    assert buckets_partition_reason(mixed) == "mixed_units"
    celsius = [bucket("11°C or below")] + [bucket(f"{v}°C") for v in range(12, 21)] + [bucket("21°C or higher")]
    assert buckets_partition_reason(celsius) is None


# --------------------------------------------------------------------------
# Settlement rules (verbatim live texts)
# --------------------------------------------------------------------------
def test_settlement_rules_parse_live_texts_and_fail_closed() -> None:
    nyc = parse_settlement_rules(NYC_RULES)
    assert nyc.admitted and nyc.station == "KLGA" and nyc.source == "noaa_timeseries"
    assert nyc.unit == "F" and nyc.kind == "high" and nyc.observation_date == date(2026, 9, 15)
    assert nyc.station_name == "LaGuardia Airport"

    tlv = parse_settlement_rules(TEL_AVIV_RULES)
    assert tlv.admitted and tlv.station == "LLBG" and tlv.unit == "C"

    tpe = parse_settlement_rules(TAIPEI_RULES)
    assert tpe.admitted and tpe.station == "RCSS" and tpe.source == "wunderground" and tpe.unit == "C"

    london = parse_settlement_rules(LONDON_LOW_RULES)
    assert london.admitted and london.kind == "low" and london.station == "EGLC"

    hk = parse_settlement_rules(HONG_KONG_RULES)
    assert not hk.admitted and hk.reason == "no_station" and hk.station is None

    two = parse_settlement_rules(NYC_RULES + " see also https://www.weather.gov/wrh/timeseries?site=kjfk")
    assert two.reason == "station_ambiguous" and two.stations_seen == ("KJFK", "KLGA")

    assert parse_settlement_rules(NYC_RULES.replace("in degrees Fahrenheit", "")).reason == "no_unit"
    assert parse_settlement_rules(NYC_RULES.replace("on 15 Sep '26", "on the 15th")).reason == "no_date"
    assert parse_settlement_rules("").reason == "no_station"
    wu_date = parse_settlement_rules("highest temperature in degrees Fahrenheit on 15 Sep '26 https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA/date/2026-9-15")
    assert wu_date.station == "KLGA" and wu_date.source == "wunderground"


def test_slug_parsing() -> None:
    assert parse_slug("highest-temperature-in-nyc-on-september-15-2026") == ("high", "nyc", date(2026, 9, 15))
    assert parse_slug("lowest-temperature-in-buenos-aires-on-september-16-2026") == ("low", "buenos aires", date(2026, 9, 16))
    assert parse_slug("fed-decision-in-september-2026") == (None, None, None)


# --------------------------------------------------------------------------
# Ensemble -> probabilities
# --------------------------------------------------------------------------
def test_ensemble_bucket_probabilities_match_hand_calculation() -> None:
    buckets = [bucket(l) for l in NYC_LABELS]
    members = [68.9, 70.6, 71.4, 72.5, 72.5, 73.4, 73.49, 74.6, 75.4, 71.0]
    probs = ensemble_bucket_probabilities(members, buckets)
    # rounding half up: 68.9->69, 70.6->71, 71.4->71, 72.5->73, 72.5->73, 73.4->73, 73.49->73, 74.6->75, 75.4->75, 71.0->71
    assert probs.counts["68-69°F"] == 1 and probs.counts["70-71°F"] == 3 and probs.counts["72-73°F"] == 4 and probs.counts["74-75°F"] == 2
    assert probs.unassigned == 0 and probs.n_members == 10
    denominator = D(10) + D("0.5") * 11
    assert probs.probabilities["72-73°F"] == (D(4) + D("0.5")) / denominator
    assert probs.probabilities["63°F or below"] == D("0.5") / denominator
    assert sum(probs.probabilities.values()).quantize(D("0.000001")) == D(1)
    assert round_half_up(72.5) == 73 and round_half_up(-0.5) == 0 and round_half_up(21.49) == 21

    inflated = ensemble_bucket_probabilities(members, buckets, parameters=WeatherEdgeParameters(dispersion_multiplier=D(3)))
    assert inflated.counts["72-73°F"] < probs.counts["72-73°F"]
    shifted = ensemble_bucket_probabilities(members, buckets, parameters=WeatherEdgeParameters(bias_degrees=D(14)))
    assert shifted.counts["82°F or higher"] == 10  # 68.9 + 14 = 82.9 -> 83, the coolest member lands in the top bucket
    empty = ensemble_bucket_probabilities([], buckets)
    assert empty.n_members == 0 and all(p == 0 for p in empty.probabilities.values())


# --------------------------------------------------------------------------
# Edge, fees, sizing
# --------------------------------------------------------------------------
def test_fee_formula_and_edge_math_with_caps() -> None:
    assert polymarket_fee_per_contract(D("0.52"), D("0.05")) == D("0.05") * D("0.52") * D("0.48")
    assert polymarket_fee_per_contract(D("0.52"), D(0)) == 0
    strategy = WeatherBucketEdgeStrategy(portfolio=Portfolio(), risk=RiskManager(WEATHER_RISK_LIMITS))
    yes = book("0.50", "0.52")
    no = book("0.48", "0.50")
    ev = strategy.evaluate(market(), bucket("72-73°F"), D("0.6145"), yes, no, fee_rate=D("0.05"))
    assert ev.traded and ev.side == "buy_yes" and ev.price == D("0.52")
    assert ev.gross_edge == D("0.6145") - D("0.52")
    assert ev.fee_per_contract == D("0.05") * D("0.52") * D("0.48")
    assert ev.net_edge == ev.gross_edge - ev.fee_per_contract
    assert ev.quantity == D(19)  # floor(10 / 0.52): the $10 per-order cap binds
    order = ev.orders[0]
    assert order.side is Side.BUY and order.outcome is Outcome.YES and order.quantity * order.price <= D(10)
    assert order.metadata["strategy"] == WEATHER_TRACK and order.metadata["price_signal_status"] == "free_multi_model_ensemble"

    # NO side: model 0.1566 on a bucket asked at 0.27 -> buy NO at 1 - 0.24 (no NO ladder) or the NO ask when present.
    ev_no = strategy.evaluate(market("N"), bucket("70-71°F"), D("0.1566"), book("0.24", "0.27", market_id="N"), None, fee_rate=D("0.05"))
    assert ev_no.traded and ev_no.side == "buy_no" and ev_no.price == D("0.76") and ev_no.orders[0].outcome is Outcome.NO
    assert ev_no.quantity == D(13)
    ev_no_ladder = strategy.evaluate(market("N2"), bucket("70-71°F"), D("0.1566"), book("0.24", "0.27", market_id="N2"), book("0.72", "0.75", market_id="N2"), fee_rate=D("0.05"))
    assert ev_no_ladder.price == D("0.75")


def test_evaluate_refusal_reasons_are_pre_registered_gates() -> None:
    strategy = WeatherBucketEdgeStrategy(portfolio=Portfolio(), risk=RiskManager(WEATHER_RISK_LIMITS))
    b = bucket("72-73°F")
    assert strategy.evaluate(market(), b, D("0.6"), book(None, None)).reason == "empty_book"
    assert strategy.evaluate(market(), b, D("0.6"), book(None, "0.52")).reason == "one_sided_book"
    assert strategy.evaluate(market(), b, D("0.6"), book("0.30", "0.52")).reason == "wide_spread"
    assert strategy.evaluate(market(), b, D("0.53"), book("0.50", "0.52"), fee_rate=D("0.05")).reason == "below_edge_threshold"
    assert strategy.evaluate(market(), b, D("0.50"), book("0.50", "0.52"), fee_rate=D("0.05")).reason == "no_edge"
    assert strategy.evaluate(market(), b, D("0.20"), book("0.005", "0.010")).reason == "price_below_floor"
    assert strategy.evaluate(market(), b, D("0.999"), book("0.95", "0.96")).reason == "price_above_cap"
    assert strategy.evaluate(market(), b, D("0.60"), book("0.50", "0.52", size="4")).reason == "insufficient_touch_depth"
    assert strategy.evaluate(market(active=False), b, D("0.60"), book("0.50", "0.52")).reason == "market_inactive"
    # A $0.90 price with a $10 cap yields 11 contracts; at $0.95 with a 5-contract minimum it is 10 -> fine; force below min:
    tiny = WeatherBucketEdgeStrategy(WeatherEdgeParameters(max_order_notional=D("3")), portfolio=Portfolio())
    assert tiny.evaluate(market(), b, D("0.99"), book("0.90", "0.91")).reason == "below_min_order_size"


def test_caps_per_market_city_day_and_total_cash_at_risk_bind() -> None:
    portfolio = Portfolio()
    strategy = WeatherBucketEdgeStrategy(portfolio=portfolio, risk=RiskManager(WEATHER_RISK_LIMITS))
    b = bucket("72-73°F")
    # Existing long YES worth $19.76 leaves $0.24 of the $20 market cap -> no whole contract fits.
    portfolio.apply_fill(Fill(venue=Venue.POLYMARKET, market_id="M", order_id="x", side=Side.BUY, quantity=D(38), price=D("0.52")))
    ev = strategy.evaluate(market(), b, D("0.9"), book("0.50", "0.52"), fee_rate=D("0.05"))
    assert ev.reason == "no_position_headroom"
    # Opposite direction on a held market is refused outright.
    assert strategy.evaluate(market(), b, D("0.05"), book("0.50", "0.52"), fee_rate=D("0.05")).reason == "opposite_position_held"
    # City-day cap: $40 used -> refused before sizing.
    assert strategy.evaluate(market("M2"), b, D("0.9"), book("0.50", "0.52", market_id="M2"), city_day_notional_used=D(40)).reason == "city_day_notional_cap_reached"
    # Total cash-at-risk cap equal to the paper cash: never borrow.
    capped = WeatherBucketEdgeStrategy(WeatherEdgeParameters(max_total_cash_at_risk=D("19.76")), portfolio=portfolio)
    assert capped.evaluate(market("M3"), b, D("0.9"), book("0.50", "0.52", market_id="M3")).reason == "total_cash_at_risk_cap_reached"
    nearly = WeatherBucketEdgeStrategy(WeatherEdgeParameters(max_total_cash_at_risk=D("25")), portfolio=portfolio)
    ev = nearly.evaluate(market("M3"), b, D("0.9"), book("0.50", "0.52", market_id="M3"))
    assert ev.traded and ev.quantity == D(10)  # floor((25 - 19.76) / 0.52) = 10
    # Position cap in contracts (RiskManager): 50 contracts at 0.10 would be $5 < $10 order cap.
    rail = WeatherBucketEdgeStrategy(portfolio=Portfolio(), risk=RiskManager(WEATHER_RISK_LIMITS))
    ev = rail.evaluate(market("M4"), b, D("0.9"), book("0.09", "0.10", market_id="M4"))
    assert ev.quantity == D(50)


def test_parameters_validate() -> None:
    with pytest.raises(ValueError):
        WeatherEdgeParameters(min_net_edge=D("1.5"))
    with pytest.raises(ValueError):
        WeatherEdgeParameters(min_entry_price=D("0.5"), max_entry_price=D("0.4"))
    with pytest.raises(ValueError):
        WeatherEdgeParameters(dispersion_multiplier=D(0))
    with pytest.raises(ValueError):
        WeatherEdgeParameters(max_spread=D(0))
    params = WeatherEdgeParameters()
    assert params.as_dict()["risk_limits"]["max_notional_per_order"] == D(10)
    assert params.max_lead_days == 1 and params.min_net_edge == D("0.03") and params.pass_net_ev_per_contract == D("0.02")


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------
def _results(values: list[str], contracts: str = "20") -> list[CityDayResult]:
    return [CityDayResult(f"r{i}", D(contracts), D(v) * D(contracts), "venue") for i, v in enumerate(values)]


def test_verdict_applies_the_pre_registered_rule() -> None:
    good = verdict(_results(["0.05"] * 15 + ["0.03"] * 15))
    assert good.n == 30 and good.status == "PASS" and good.strong and good.mean_per_contract == D("0.04")
    assert good.lower_95 is not None and good.lower_95 > 0
    weak = verdict(_results(["0.01"] * 30))
    assert weak.status == "FAIL" and weak.strong is False
    noisy = verdict(_results(["0.60"] * 15 + ["-0.50"] * 15))
    assert noisy.status == "FAIL" and noisy.mean_per_contract == D("0.05") and noisy.lower_95 < 0
    short = verdict(_results(["0.10"] * 29))
    assert short.status == "insufficient_sample" and short.n == 29
    empty = verdict([])
    assert empty.n == 0 and empty.mean_per_contract is None and empty.status == "insufficient_sample"
    zero_contract = verdict([CityDayResult("z", D(0), D(0), "venue")] + _results(["0.05"] * 30))
    assert zero_contract.n == 30  # city-days without contracts never enter the sample
    assert good.pre_registered["min_settled_city_days"] == 30 and good.pre_registered["pass_net_ev_per_contract"] == D("0.02")
    # Pooled (contract-weighted) per-contract figure is reported next to the city-day mean, never as the verdict.
    mixed = verdict([CityDayResult("a", D(100), D(10), "venue"), CityDayResult("b", D(10), D("-2"), "venue")])
    assert mixed.pooled_per_contract == D("8") / D(110) and mixed.mean_per_contract == (D("0.1") + D("-0.2")) / 2


# --------------------------------------------------------------------------
# Feeds: documented shapes
# --------------------------------------------------------------------------
def test_open_meteo_ensemble_payload_parses_per_model_members() -> None:
    payload = {
        "timezone": "America/New_York",
        "utc_offset_seconds": -14400,
        "daily_units": {"temperature_2m_max_ncep_gefs_seamless": "°F"},
        "daily": {
            "time": ["2026-09-15"],
            "temperature_2m_max_ncep_gefs_seamless": [72.9],
            "temperature_2m_max_member01_ncep_gefs_seamless": [71.1],
            "temperature_2m_max_member02_ncep_gefs_seamless": [None],
            "temperature_2m_max_ecmwf_ifs025_ensemble": [73.4],
            "temperature_2m_max_member01_ecmwf_ifs025_ensemble": [74.0],
            "temperature_2m_min_member01_ecmwf_ifs025_ensemble": [60.0],
        },
    }
    forecast = parse_ensemble_payload(payload, station="KLGA", target_date=date(2026, 9, 15), kind="high", unit="F")
    assert forecast.available and forecast.n_members == 4 and forecast.n_models == 2
    assert forecast.members_by_model == {"ncep_gefs_seamless": [72.9, 71.1], "ecmwf_ifs025_ensemble": [73.4, 74.0]}
    assert forecast.timezone == "America/New_York" and forecast.utc_offset_seconds == -14400
    missing = parse_ensemble_payload(payload, station="KLGA", target_date=date(2026, 9, 16), kind="high", unit="F")
    assert not missing.available and "not in daily.time" in missing.errors[0]
    error = parse_ensemble_payload({"error": True, "reason": "Latitude must be in range"}, station="X", target_date=date(2026, 9, 15), kind="high", unit="F")
    assert error.errors == ["Latitude must be in range"]


def test_metar_temperature_prefers_the_tenths_group_and_day_extreme_uses_the_local_day() -> None:
    row = {"icaoId": "KLGA", "obsTime": 1789429860, "temp": 22, "rawOb": "METAR KLGA 142351Z 35013G19KT 10SM FEW060 22/08 A3015 RMK AO2 SLP209 T02170078 10239 20217 51022"}
    assert metar_temperature_c(row) == (21.7, True)
    assert metar_temperature_c({"temp": 5}) == (5.0, False)
    assert metar_temperature_c({"rawOb": "METAR EFHK 150550Z RMK T10231045"}) == (-2.3, True)
    observations = parse_metar_payload([row, {"icaoId": "KJFK", "obsTime": 1789429860, "temp": 30}], station="KLGA")
    assert len(observations) == 1 and observations[0].temp_c == 21.7 and observations[0].precise

    zone = ZoneInfo("America/New_York")
    day = date(2026, 9, 15)
    start = datetime(2026, 9, 15, 4, 51, tzinfo=UTC)  # 00:51 EDT
    rows = []
    for h in range(24):
        temp = 15.0 + h * 0.5 if h <= 15 else 22.5 - (h - 15) * 0.7
        rows.append({"icaoId": "KLGA", "time": (start + timedelta(hours=h)).isoformat(), "temp_c": round(temp, 1)})
    rows.append({"icaoId": "KLGA", "time": datetime(2026, 9, 16, 4, 51, tzinfo=UTC).isoformat(), "temp_c": 30.0})  # next local day: ignored
    obs = parse_metar_payload(rows, station="KLGA")
    done = observed_extreme(obs, local_day=day, zone=zone, unit="F", kind="high", as_of=datetime(2026, 9, 16, 6, 0, tzinfo=UTC))
    assert done.value == 73 and done.complete and done.reason == "observed" and done.n_observations == 24  # 22.5C -> 72.5F -> 73
    low = observed_extreme(obs, local_day=day, zone=zone, unit="F", kind="low", as_of=datetime(2026, 9, 16, 6, 0, tzinfo=UTC))
    assert low.value == 59  # 15.0C -> 59F
    celsius = observed_extreme(obs, local_day=day, zone=zone, unit="C", kind="high", as_of=datetime(2026, 9, 16, 6, 0, tzinfo=UTC))
    assert celsius.value == 23  # 22.5 rounds half up
    early = observed_extreme(obs, local_day=day, zone=zone, unit="F", kind="high", as_of=datetime(2026, 9, 15, 20, 0, tzinfo=UTC))
    assert early.reason == "day_not_over" and not early.complete and early.value is not None
    partial = observed_extreme(obs[:10], local_day=day, zone=zone, unit="F", kind="high", as_of=datetime(2026, 9, 16, 6, 0, tzinfo=UTC))
    assert partial.reason == "partial_day" and not partial.complete
    none = observed_extreme([], local_day=day, zone=zone, unit="F", kind="high", as_of=datetime(2026, 9, 16, 6, 0, tzinfo=UTC))
    assert none.reason == "no_observations" and none.value is None
    assert zone_for("Europe/London", 3600).utcoffset(datetime(2026, 7, 1, tzinfo=UTC)) == timedelta(hours=1)
    assert zone_for("Not/AZone", -14400).utcoffset(None) == timedelta(hours=-4)
    assert zone_for(None, None) is UTC


def test_station_registry_and_stationinfo_parsing() -> None:
    registry = StationRegistry.load()
    assert len(registry) >= 40 and registry.get("klga") is not None and registry.get("KBKF").name == "Buckley SFB"
    parsed = parse_station_info([{"icaoId": "EGLC", "site": "London City Arpt", "lat": 51.505, "lon": 0.055, "elev": 10, "country": "GB"}, {"icaoId": "BAD"}])
    assert set(parsed) == {"EGLC"} and parsed["EGLC"].source == "aviationweather_stationinfo"


def test_paid_sources_are_reported_and_never_enabled() -> None:
    off = paid_source_policy({})
    assert off == {**off, "enabled": False, "implemented": False, "keys_present": [], "requested": False}
    on = paid_source_policy({"VISUAL_CROSSING_KEY": "secret", "WEATHER_ALLOW_PAID_SOURCES": "true"})
    assert on["enabled"] is False and on["implemented"] is False and on["keys_present"] == ["VISUAL_CROSSING_KEY"] and on["requested"] is True


async def test_public_feed_calls_the_documented_endpoints_and_survives_failures() -> None:
    calls: list[tuple[str, dict]] = []

    async def http_get(url: str, params: dict) -> object:
        calls.append((url, params))
        if url.endswith("/ensemble"):
            assert params["daily"] == "temperature_2m_min" and params["temperature_unit"] == "celsius" and params["timezone"] == "auto"
            assert params["start_date"] == params["end_date"] == "2026-09-16" and params["models"] == "gfs_seamless,ecmwf_ifs025"
            return {"timezone": "Europe/London", "utc_offset_seconds": 3600, "daily": {"time": ["2026-09-16"], "temperature_2m_min_ncep_gefs_seamless": [13.5], "temperature_2m_min_member01_ecmwf_ifs025_ensemble": [12.5]}}
        if url.endswith("/metar"):
            assert params == {"ids": "EGLC", "format": "json", "hours": 48}
            return [{"icaoId": "EGLC", "obsTime": 1789429860, "temp": 14, "rawOb": "METAR EGLC 142350Z RMK T01380090"}]
        if url.endswith("/stationinfo"):
            return [{"icaoId": "ZZZZ", "site": "Nowhere", "lat": 1.0, "lon": 2.0}]
        raise AssertionError(url)

    feed = PublicWeatherFeed(registry=StationRegistry.load(), models=("gfs_seamless", "ecmwf_ifs025"), http_get=http_get, max_requests=3)
    station = await feed.station("EGLC")
    assert station is not None and station.source == "registry" and feed.requests_made == 0
    forecast = await feed.forecast(station, target_date=date(2026, 9, 16), kind="low", unit="C", as_of=AS_OF)
    assert forecast.available and forecast.n_members == 2 and forecast.timezone == "Europe/London"
    observations = await feed.observations(station, as_of=datetime(2026, 9, 16, tzinfo=UTC))
    assert observations[0].temp_c == 13.8 and observations[0].precise
    assert (await feed.station("zzzz")).name == "Nowhere"
    assert feed.requests_made == 3
    # Budget exhausted: the forecast reports an error, the station lookup returns None, nothing raises.
    broken = await feed.forecast(station, target_date=date(2026, 9, 16), kind="low", unit="C", as_of=AS_OF)
    assert not broken.available and "request budget" in broken.errors[0]
    assert await feed.station("QQQQ") is None and feed.errors
    assert feed.as_dict()["sources"]["forecast"]["provider"].startswith("Open-Meteo")


# --------------------------------------------------------------------------
# City-day parsing from Gamma-shaped events
# --------------------------------------------------------------------------
def test_city_day_parsing_from_replay_events_is_fail_closed() -> None:
    step = load_replay()[0]
    by_slug = {parse_city_day(g).slug: parse_city_day(g) for g in step.snapshot.groups}
    nyc = by_slug["highest-temperature-in-nyc-on-september-15-2026"]
    assert nyc.admitted and nyc.station == "KLGA" and nyc.unit == "F" and nyc.kind == "high" and nyc.record_id == "KLGA:2026-09-15:high"
    assert [bm.bucket.label for bm in nyc.buckets] == NYC_LABELS
    london = by_slug["lowest-temperature-in-london-on-september-16-2026"]
    assert london.admitted and london.kind == "low" and london.unit == "C" and london.observation_date == date(2026, 9, 16)
    assert by_slug["highest-temperature-in-hong-kong-on-september-15-2026"].reason == "no_station"
    assert by_slug["highest-temperature-in-denver-on-september-15-2026"].reason == "bucket_parse_failed"
    assert by_slug["lowest-temperature-in-chicago-on-september-15-2026"].reason == "station_ambiguous_across_legs"
    assert by_slug["fed-decision-in-september-2026"].reason == "not_a_temperature_event"

    payload = json.loads(Path("research/fixtures/weather_buckets_replay.json").read_text())
    event = next(e for e in payload["steps"][0]["events"] if e["slug"] == "highest-temperature-in-nyc-on-september-15-2026")
    augmented = snapshot_from_events([{**event, "negRiskAugmented": True}], {})
    assert parse_city_day(augmented.groups[0]).reason == "augmented_group"
    not_exclusive = snapshot_from_events([{**event, "negRisk": False, "markets": [{**m, "negRisk": False} for m in event["markets"]]}], {})
    assert parse_city_day(not_exclusive.groups[0]).reason == "not_exclusive_group"
    swapped = snapshot_from_events([{**event, "slug": "lowest-temperature-in-nyc-on-september-15-2026"}], {})
    assert parse_city_day(swapped.groups[0]).reason == "kind_mismatch_slug_vs_rules"


def test_classify_venue_resolution() -> None:
    ids = ["a", "b", "c"]
    resolved = {"a": {"closed": True, "outcome_prices": ["0", "1"]}, "b": {"closed": True, "outcome_prices": ["1", "0"]}, "c": {"closed": True, "outcome_prices": ["0", "1"]}}
    assert classify_venue_resolution(resolved, ids) == ("resolved", "b", "venue_resolved")
    assert classify_venue_resolution({**resolved, "c": {"closed": False, "outcome_prices": ["0.2", "0.8"]}}, ids)[0] == "pending"
    assert classify_venue_resolution({**resolved, "c": {"closed": True, "outcome_prices": ["0.5", "0.5"]}}, ids)[0] == "excluded"
    two = {**resolved, "a": {"closed": True, "outcome_prices": ["1", "0"]}}
    assert classify_venue_resolution(two, ids) == ("excluded", None, "2_winning_buckets")
    assert classify_venue_resolution(None, ids) == ("pending", None, "no_status")


# --------------------------------------------------------------------------
# Replay: every scripted branch, hand-checked numbers
# --------------------------------------------------------------------------
async def test_replay_measures_every_scripted_branch() -> None:
    aggregate, ledger, register, steps = await replay_fixture()
    t0, t1, t2, t3 = steps
    assert t0.candidates == 10 and t0.admitted == 2 and t0.proposed_orders == 5 and t0.paper_fills == 5
    assert t0.refused_by_reason == {
        "bucket_parse_failed": 1, "forecast_unavailable": 1, "insufficient_members": 1, "no_station": 1,
        "not_a_temperature_event": 1, "priced_no_edge": 1, "station_ambiguous_across_legs": 1, "too_far_ahead": 1,
    }
    assert t0.metrics["station_parse"]["counts"] == {"no_station": 2, "parsed": 8} and t0.metrics["station_parse"]["success_rate"] == D("0.8000")
    assert t0.metrics["bucket_refusals"]["no_edge"] == 4 and t0.metrics["bucket_refusals"]["below_edge_threshold"] > 20

    nyc = register.records["KLGA:2026-09-15:high"]
    filled = {b["label"]: b for b in nyc.buckets if b["fills"]}
    assert set(filled) == {"70-71°F", "72-73°F"}
    yes = filled["72-73°F"]
    assert yes["side"] == "buy_yes" and yes["price"] == "0.5200" and yes["filled_quantity"] == "19" and yes["fees"] == "0.23712"
    assert D(yes["net_edge"]) == (D("25.5") / D("41.5") - D("0.52") - D("0.05") * D("0.52") * D("0.48")).quantize(D("0.0001"))
    no = filled["70-71°F"]
    assert no["side"] == "buy_no" and no["price"] == "0.7600" and no["filled_quantity"] == "13" and no["fees"] == "0.11856"
    assert nyc.paper["lead_days"] == 0 and nyc.timezone == "America/New_York"

    # t1: NYC's local day is over, the venue has not resolved, METAR gives a provisional 73F -> 72-73F.
    assert t1.refused_by_reason == {"already_entered": 2} and t1.paper_fills == 0
    assert t1.metrics["register"]["settlement_checks_this_run"]["metar_provisional"] == 1
    assert t1.metrics["verdict"]["n"] == 0 and t1.metrics["verdict_provisional_including_metar"]["n"] == 1
    # t2: venue resolves NYC to 72-73F (agrees with METAR); ledger settles at 1/0; NYC 17 entered.
    assert nyc.status == "settled" and nyc.settlement["method"] == "venue" and nyc.settlement["winning_label"] == "72-73°F"
    assert nyc.metar["value"] == 73 and nyc.metar["bucket"] == "72-73°F" and nyc.metar["agrees_with_venue"] is True
    assert D(nyc.paper["realized_pnl"]) == (D("19") * D("0.48") - D("0.23712") + D("13") * D("0.24") - D("0.11856")).quantize(D("0.0001"))
    assert D(nyc.metar["provisional_pnl"]).quantize(D("0.01")) == D(nyc.paper["realized_pnl"]).quantize(D("0.01"))
    assert nyc.brier["model"] < nyc.brier["market"]
    assert t2.admitted == 1 and t2.paper_fills == 2 and t2.metrics["register"]["settlement_checks_this_run"]["venue_resolved"] == 1
    # t3: London resolves to 14C while METAR said 13C -> disagreement recorded; our 13C YES and 14C NO lose, 15C NO wins.
    london = register.records["EGLC:2026-09-16:low"]
    assert london.status == "settled" and london.settlement["winning_label"] == "14°C" and london.metar["bucket"] == "13°C"
    assert london.metar["agrees_with_venue"] is False and D(london.paper["realized_pnl"]) < 0
    assert {b["label"] for b in london.buckets if b["fills"]} == {"13°C", "14°C", "15°C"}
    assert t3.metrics["metar_venue_agreement"] == {**t3.metrics["metar_venue_agreement"], "checked": 2, "agree": 1, "disagree": 1}
    open_records = {r.record_id for r in register.open_records()}
    assert open_records == {"KORD:2026-09-15:high", "KLGA:2026-09-17:high"}
    assert register.records["KORD:2026-09-15:high"].contracts == 0  # priced, no edge, still tracked for Brier
    nyc17 = register.records["KLGA:2026-09-17:high"]
    assert nyc17.settlement["method"] == "metar_provisional" and nyc17.metar["bucket"] == "70-71°F"

    v = t3.metrics["verdict"]
    assert v["n"] == 2 and v["status"] == "insufficient_sample" and v["pre_registered"]["min_settled_city_days"] == 30
    assert t3.metrics["verdict_provisional_including_metar"]["n"] == 3
    assert aggregate.candidates == 15 and aggregate.admitted == 3 and aggregate.paper_fills == 7 and aggregate.metrics["replayed_steps"] == 4
    assert aggregate.metrics["station_parse"]["parsed"] == 13 and aggregate.metrics["station_parse"]["total"] == 15
    assert aggregate.metrics["city_days"]["priced"] == 4 and aggregate.metrics["settlement_fills"] == 2
    assert aggregate.metrics["status"] == "fixture_synthetic" and aggregate.metrics["network_status"] == "fixture_synthetic"
    assert aggregate.metrics["paid_sources"]["enabled"] is False

    # Ledger: identity holds, every fill respects the caps, settled markets are flat.
    assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl
    for fill in ledger.fills:
        if fill.order_id != "settlement":
            assert fill.quantity * fill.price <= D(10)
    for position in ledger.open_positions:
        assert abs(position.quantity) <= 50
    assert ledger.portfolio.get(Venue.POLYMARKET, yes["market_id"]).quantity == 0
    assert len(ledger.open_positions) == 2  # NYC 17 only
    assert sum(1 for f in ledger.fills if f.order_id == "settlement") == 5


async def test_register_round_trips_and_a_resumed_replay_matches_a_straight_one(tmp_path: Path) -> None:
    steps = load_replay()
    straight, ledger_a, register_a, _ = await replay_fixture(steps)
    _, ledger_b, register_b, _ = await replay_fixture(steps[:2])
    path = tmp_path / "register.json"
    register_b.save(path)
    ledger_path = tmp_path / "ledger.json"
    ledger_b.save(ledger_path)
    reloaded_register = CityDayRegister.load_or_create(path)
    from core.ledger import PaperLedger

    resumed, ledger_c, register_c, _ = await replay_fixture(steps[2:], ledger=PaperLedger.load(ledger_path), register=reloaded_register)
    assert {k: (r.status, r.paper.get("realized_pnl")) for k, r in register_c.records.items()} == {k: (r.status, r.paper.get("realized_pnl")) for k, r in register_a.records.items()}
    assert ledger_c.equity == ledger_a.equity and ledger_c.realized_pnl == ledger_a.realized_pnl
    with pytest.raises(ValueError):
        CityDayRegister.from_dict({"paper_only": False, "records": []})
    again, ledger_d, register_d, _ = await replay_fixture(steps, ledger=ledger_a, register=register_a)
    assert again.paper_fills == 0 and again.admitted == 0 and ledger_d.equity == ledger_a.equity and len(register_d.records) == 4


async def test_network_path_without_a_feed_is_an_honest_empty() -> None:
    step = load_replay()[0]

    async def no_lookup(record):
        return None

    summary, ledger, register = await measure_weather_buckets(
        use_fixtures=False, snapshot=step.snapshot, feed=NullWeatherFeed(StationRegistry.load()), as_of=step.as_of, resolution_lookup=no_lookup,
    )
    assert summary.metrics["status"] == "no_weather_feed" and summary.metrics["network_status"].startswith("UNKNOWN")
    assert summary.admitted == 0 and summary.paper_fills == 0 and ledger.equity == ledger.starting_cash
    # Seoul is refused on the coarse date gate before any feed call; the five remaining parsed city-days all need a forecast.
    assert summary.refused_by_reason["forecast_unavailable"] == 5 and summary.refused_by_reason["too_far_ahead"] == 1
    assert register.records == {}
    assert summary.metrics["station_parse"]["success_rate"] == D("0.8000")


async def test_exploding_feed_and_lookup_do_not_take_the_run_down() -> None:
    step = load_replay()[0]

    class Exploding:
        name = "exploding"

        async def station(self, icao):
            return Station(icao, None, 0.0, 0.0)

        async def forecast(self, station, *, target_date, kind, unit, as_of):
            raise RuntimeError("boom")

        async def observations(self, station, *, as_of, hours=48):
            raise RuntimeError("boom")

    async def bad_lookup(record):
        raise RuntimeError("gamma down")

    summary, _, register = await measure_weather_buckets(use_fixtures=False, snapshot=step.snapshot, feed=Exploding(), as_of=step.as_of, resolution_lookup=bad_lookup)
    assert summary.refused_by_reason["forecast_unavailable"] == 5 and summary.paper_fills == 0
    assert any("RuntimeError" in e for e in summary.metrics["feed"]["stats"]["forecast_errors"])

    # A record past its day with a broken lookup and broken observations stays open with the error recorded.
    steps = load_replay()
    _, ledger, register, _ = await replay_fixture(steps[:1])
    later, _, register = await measure_weather_buckets(
        use_fixtures=False, snapshot=steps[1].snapshot, feed=Exploding(), as_of=steps[1].as_of, ledger=ledger, register=register, resolution_lookup=bad_lookup,
    )
    nyc = register.records["KLGA:2026-09-15:high"]
    assert nyc.status == "open" and "gamma down" in nyc.settlement["lookup_error"] and "RuntimeError" in nyc.metar["error"]
    # Both Sept-15 records (NYC with fills, Chicago priced-no-edge) are past their day and were checked.
    assert later.metrics["register"]["settlement_checks_this_run"]["lookup_errors"] == 2


async def test_empty_snapshot_reports_no_weather_markets() -> None:
    snapshot = VenueSnapshot(venue=Venue.POLYMARKET, source="network")

    async def no_lookup(record):
        return None

    summary, _, _ = await measure_weather_buckets(use_fixtures=False, snapshot=snapshot, feed=FixtureWeatherFeed(), as_of=AS_OF, resolution_lookup=no_lookup)
    assert summary.metrics["status"] == "fixture_synthetic"  # a fixture feed labels itself honestly
    public = PublicWeatherFeed(http_get=None, max_requests=0)
    summary, _, _ = await measure_weather_buckets(use_fixtures=False, snapshot=snapshot, feed=public, as_of=AS_OF, resolution_lookup=no_lookup)
    assert summary.metrics["status"] == "no_weather_markets" and summary.candidates == 0
    snapshot.errors.append("list_events_by_tag: boom")
    summary, _, _ = await measure_weather_buckets(use_fixtures=False, snapshot=snapshot, feed=public, as_of=AS_OF, resolution_lookup=no_lookup)
    assert summary.metrics["status"] == "venue_snapshot_errors"


# --------------------------------------------------------------------------
# Polymarket client: tag discovery and event status (mocked Gamma)
# --------------------------------------------------------------------------
async def test_polymarket_client_lists_weather_events_by_tag_and_reads_event_status() -> None:
    payload = json.loads(Path("research/fixtures/weather_buckets_replay.json").read_text())
    events = payload["steps"][0]["events"][:2]
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gamma-api.polymarket.com" and request.url.path == "/events"
        params = dict(request.url.params)
        seen.append(params)
        if "slug" in params:
            resolved = [{**m, "closed": True, "outcomePrices": json.dumps(["1", "0"] if i == 5 else ["0", "1"])} for i, m in enumerate(events[0]["markets"])]
            return httpx.Response(200, json=[{**events[0], "closed": True, "markets": resolved}])
        assert params["tag_id"] == str(DAILY_TEMPERATURE_TAG_ID) and params["active"] == "true" and params["closed"] == "false"
        return httpx.Response(200, json=events if params.get("offset", "0") == "0" else [])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = PolymarketClient(paper=True, use_fixtures=False, http=http, search_terms=None)
    groups = await client.list_events_by_tag(DAILY_TEMPERATURE_TAG_ID, limit=10)
    assert [g.metadata["slug"] for g in groups] == [e["slug"] for e in events]
    assert all(g.exclusive and g.convertible and not g.augmented for g in groups)
    assert groups[0].markets[0].metadata["taker_fee_rate"] == "0.05" and groups[0].markets[0].metadata["group_item_title"] == "63°F or below"
    status = await client.get_event_status(events[0]["slug"])
    winners = [mid for mid, s in status.items() if s["outcome_prices"] == ["1", "0"]]
    assert len(status) == 11 and winners == [events[0]["markets"][5]["conditionId"]] and all(s["closed"] for s in status.values())
    assert classify_venue_resolution(status, [m["conditionId"] for m in events[0]["markets"]])[0] == "resolved"
    await client.close()


# --------------------------------------------------------------------------
# Artifacts and CLI
# --------------------------------------------------------------------------
async def test_scoreboard_artifact_is_measured_and_ledger_backed(tmp_path: Path) -> None:
    summary, ledger, _ = await measure_weather_buckets()
    artifact = persist_run(
        [summary], {WEATHER_TRACK: ledger}, artifact_dir=tmp_path, mode="fixtures", measured_at="t", limit=120, kalshi_env=None,
        scoreboard_name="scoreboard_weather_buckets.json", write_latest=False,
        artifact_kwargs={"primary_track": WEATHER_TRACK, "venues": ("polymarket",), "venue_focus": "polymarket", "label_suffix": "WEATHER BUCKETS", "track_family": "weather"},
    )
    assert artifact["meta"]["source"] == "measured" and artifact["meta"]["track_family"] == "weather"
    assert artifact["meta"]["pnl_source"] == "core.ledger.PaperLedger" and artifact["meta"]["venues"] == ["polymarket"]
    assert artifact["totals"]["paper_pnl"] == ledger.total_pnl.quantize(D("0.0001"))
    assert artifact["totals"]["paper_fills"] == 7 and artifact["totals"]["fill_rate"] == D("1.0000")
    row = artifact["tracks"][0]
    assert row["track"] == WEATHER_TRACK and row["metrics"]["verdict"]["status"] == "insufficient_sample"
    assert row["metrics"]["records"] == {"count": 4, "detail": "weather_buckets_latest.json"}
    assert row["metrics"]["measurements"] == {"count": 1, "detail": "weather_buckets_latest.json"}
    assert (tmp_path / "paper" / f"ledger_{WEATHER_TRACK}.json").exists() and not (tmp_path / "scoreboard_latest.json").exists()
    json.dumps(artifact, default=json_default)


def test_cli_fixture_run_writes_report_register_and_ledger(tmp_path: Path) -> None:
    env = {**os.environ, "TRADING_MODE": "paper", "ENABLE_LIVE_TRADING": "false"}
    result = subprocess.run([sys.executable, "-m", "apps.measure_weather_buckets", "--artifact-dir", str(tmp_path)], check=False, capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert "status=fixture_synthetic" in result.stdout and "insufficient_sample" in result.stdout
    report = json.loads((tmp_path / "weather_buckets_latest.json").read_text())
    assert report["paper_only"] is True and report["kind"] == "weather_buckets_report" and report["source"] == "measured"
    assert report["verdict"]["n"] == 2 and report["register"]["settled"] == 2 and report["register"]["open"] == 2
    assert report["experiment"]["pre_registered"]["min_settled_city_days"] == 30
    assert report["station_parse"]["parsed"] == 13 and report["metar_venue_agreement"]["disagree"] == 1
    assert report["experiment"]["paid_sources"]["enabled"] is False and report["not_validated"]
    assert (tmp_path / "weather_buckets" / "register.json").exists() and (tmp_path / "paper" / "ledger_weather_bucket_edge.json").exists()
    board = json.loads((tmp_path / "scoreboard_weather_buckets.json").read_text())
    assert board["meta"]["track_family"] == "weather" and board["meta"]["source"] == "measured"
    assert not (tmp_path / "scoreboard_latest.json").exists()
    # A second run over the persisted register opens nothing new and leaves the ledger unchanged.
    again = subprocess.run([sys.executable, "-m", "apps.measure_weather_buckets", "--artifact-dir", str(tmp_path)], check=False, capture_output=True, text=True, env=env)
    assert again.returncode == 0, again.stderr
    report2 = json.loads((tmp_path / "weather_buckets_latest.json").read_text())
    assert report2["paper_fills"] == 0 and report2["ledger"]["equity"] == report["ledger"]["equity"]


def test_cli_refuses_live_flags(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apps.measure_weather_buckets", "--artifact-dir", str(tmp_path)],
        check=False, capture_output=True, text=True, env={**os.environ, "TRADING_MODE": "live", "ENABLE_LIVE_TRADING": "true"},
    )
    assert result.returncode != 0 and not (tmp_path / "weather_buckets_latest.json").exists()
