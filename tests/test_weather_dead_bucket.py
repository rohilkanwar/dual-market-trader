import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from apps.measure_all import json_default, persist_run
from core.ledger import PaperLedger
from core.risk import RiskLimits, RiskManager
from core.types import Market, OrderBook, Outcome, PriceLevel, Venue
from research.scoreboard import TRACKS, VenueSnapshot
from research.weather_dead_bucket import (
    WEATHER_TRACK,
    WEATHER_TRACKS,
    DeadBucketRegister,
    capture_weather_snapshot,
    classify_venue_resolution,
    load_replay,
    measure_weather_dead_bucket,
    replay_fixture,
    weather_events,
)
from research.weather_obs import (
    AviationWeatherMetarSource,
    ChainedObservationSource,
    JsonObservationSource,
    NullObservationSource,
    NwsObservationSource,
    StaticObservationSource,
    build_observation_source,
    hours_back,
    parse_aviationweather_payload,
    parse_nws_payload,
)
from strategies.weather_dead_bucket import (
    STATION_TIMEZONES,
    DeadBucketParameters,
    DeadBucketStrategy,
    KillRule,
    MarketKind,
    ResolutionSourceKind,
    SettledRecord,
    StationObservation,
    TemperatureBucket,
    TemperatureUnit,
    WeatherMarketSpec,
    classify_bucket,
    net_edge_per_contract,
    parse_bucket_label,
    parse_metar_observation,
    parse_metar_temperature_tenths_c,
    parse_weather_market,
    running_high,
    tenths_c_to_unit,
    verdict,
)
from venues.polymarket.client import PolymarketClient

D = Decimal

# Verbatim Polymarket description (Gamma, 2026-09-15) for "Highest temperature in NYC on September 14?".
NYC_DESC = (
    "This market will resolve to the temperature range that contains the highest temperature recorded by NOAA at the "
    "LaGuardia Airport Station in degrees Fahrenheit on 14 Sep '26.\n\nThe resolution source for this market will be "
    "information from NOAA, specifically the highest reading under the \"Temp\" column for all times on this day, available "
    "here: https://www.weather.gov/wrh/timeseries?site=klga\n\nThis market will resolve off of the Hourly Data provided using "
    "the \"Show Hourly Data\" button.\n\nIf NOAA data for the observation date is unavailable by 11:59 PM ET on the day "
    "following the observation date, the Weather Underground Daily Observations table will be used as the resolution source."
    "\n\nThe resolution source for this market measures temperatures to whole degrees Fahrenheit (eg, 21\u00b0F)."
)
LAX_DESC = (
    "This market will resolve to the temperature range that contains the highest temperature recorded at the Los Angeles "
    "International Airport Station in degrees Fahrenheit on 9 Jul '26. The resolution source for this market will be "
    "information from Wunderground, specifically the highest temperature recorded for all times on this day for the Los "
    "Angeles International Airport Station, available here: https://www.wunderground.com/history/daily/us/ca/los-angeles/KLAX."
)
HKO_DESC = (
    "This market will resolve to the temperature range that contains the highest temperature recorded by the Hong Kong "
    "Observatory in degrees Celsius on 14 Sep '26.\n\nThe resolution source for this market will be information from the "
    "Hong Kong Observatory, specifically the \"Absolute Daily Max (deg. C)\"."
)
KLGA_SPEC = WeatherMarketSpec("KLGA", "NYC", date(2026, 9, 15), TemperatureUnit.F, MarketKind.HIGHEST, ResolutionSourceKind.NOAA_WRH_TIMESERIES, "https://www.weather.gov/wrh/timeseries?site=klga", "America/New_York")
SAEZ_SPEC = WeatherMarketSpec("SAEZ", "Buenos Aires", date(2026, 9, 15), TemperatureUnit.C, MarketKind.HIGHEST, ResolutionSourceKind.NOAA_WRH_TIMESERIES, None, "America/Argentina/Buenos_Aires")


def obs(station: str, when: str, tenths: int, kind: str = "METAR") -> StationObservation:
    return StationObservation(station, datetime.fromisoformat(when.replace("Z", "+00:00")), tenths, kind, f"{kind} {station}")


def hourly(station: str, start: str, tenths: list[int]) -> list[StationObservation]:
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return [StationObservation(station, t0 + timedelta(hours=i), t, "METAR", "") for i, t in enumerate(tenths)]


def book(bids: list[tuple[str, str]], asks: list[tuple[str, str]], market_id: str = "M") -> OrderBook:
    return OrderBook(market_id=market_id, bids=tuple(PriceLevel(D(p), D(s)) for p, s in bids), asks=tuple(PriceLevel(D(p), D(s)) for p, s in asks))


def market(market_id: str = "M", *, fee: str | None = "0.05") -> Market:
    meta = {"group_item_title": "66-67°F", "slug": f"slug-{market_id}"}
    if fee is not None:
        meta["taker_fee_rate"] = fee
    return Market(Venue.POLYMARKET, market_id, "Will the highest temperature in NYC be 66-67°F on September 15?", yes_token_id="y", no_token_id="n", metadata=meta)


# --------------------------------------------------------------------------
# Shared types: buckets, specs
# --------------------------------------------------------------------------
def test_bucket_labels_parse_ranges_open_ends_and_refuse_unit_mismatch() -> None:
    assert parse_bucket_label("66-67°F", TemperatureUnit.F) == TemperatureBucket(66, 67, "66-67°F")
    assert parse_bucket_label("65°F or below", TemperatureUnit.F) == TemperatureBucket(None, 65, "65°F or below")
    assert parse_bucket_label("84°F or higher", TemperatureUnit.F) == TemperatureBucket(84, None, "84°F or higher")
    assert parse_bucket_label("20-21°C", TemperatureUnit.C) == TemperatureBucket(20, 21, "20-21°C")
    assert parse_bucket_label("-3--2°C", TemperatureUnit.C) == TemperatureBucket(-3, -2, "-3--2°C")
    assert parse_bucket_label("-3 to -2°C", TemperatureUnit.C) == TemperatureBucket(-3, -2, "-3 to -2°C")
    assert parse_bucket_label("-2--3°C", TemperatureUnit.C) is None  # inverted range is refused, not repaired
    assert parse_bucket_label("66-67°F", TemperatureUnit.C) is None  # unit mismatch
    assert parse_bucket_label("Yes") is None
    b = TemperatureBucket(66, 67)
    assert b.contains(66) and b.contains(67) and not b.contains(68)
    assert b.entirely_below(68) and not b.entirely_below(67) and b.entirely_above(65) and not b.entirely_above(66)
    with pytest.raises(ValueError):
        TemperatureBucket(None, None)
    with pytest.raises(ValueError):
        TemperatureBucket(70, 69)


def test_weather_market_spec_is_parsed_from_the_resolution_text_only() -> None:
    parsed = parse_weather_market(event_title="Highest temperature in NYC on September 14?", description=NYC_DESC, resolution_source_url="https://www.weather.gov/wrh/timeseries?site=klga")
    assert parsed.ok and parsed.spec is not None
    spec = parsed.spec
    assert spec.station_icao == "KLGA" and spec.city == "NYC" and spec.local_date == date(2026, 9, 14)
    assert spec.unit is TemperatureUnit.F and spec.resolution_source is ResolutionSourceKind.NOAA_WRH_TIMESERIES
    assert spec.timezone == "America/New_York" and spec.hourly_only is True
    # Station only from the URL in the text when Gamma's resolutionSource is empty (Tel Aviv / Moscow shape).
    from_text = parse_weather_market(event_title="Highest temperature in NYC on September 14?", description=NYC_DESC)
    assert from_text.ok and from_text.spec.station_icao == "KLGA"
    lax = parse_weather_market(event_title="Highest temperature in Los Angeles on July 9?", description=LAX_DESC)
    assert lax.ok and lax.spec.station_icao == "KLAX" and lax.spec.resolution_source is ResolutionSourceKind.WUNDERGROUND_HISTORY
    assert lax.spec.local_date == date(2026, 7, 9) and lax.spec.hourly_only is False


@pytest.mark.parametrize(
    ("title", "description", "reason"),
    [
        ("Highest temperature in Hong Kong on September 14?", HKO_DESC, "station_unparsed"),
        ("Lowest temperature in NYC on September 14?", NYC_DESC.replace("highest", "lowest"), "market_kind_unsupported"),
        ("Will the Fed cut rates?", "Resolves per the FOMC statement.", "market_kind_unsupported"),
        ("Highest temperature in NYC on September 14?", NYC_DESC.replace(" on 14 Sep '26", ""), "date_unparsed"),
        ("Highest temperature in NYC on September 14?", NYC_DESC.replace("degrees Fahrenheit", "degrees"), "unit_unparsed"),
        ("Highest temperature in Reykjavik on September 14?", NYC_DESC.replace("site=klga", "site=bikf"), "station_timezone_unknown"),
    ],
)
def test_spec_parsing_fails_closed(title: str, description: str, reason: str) -> None:
    parsed = parse_weather_market(event_title=title, description=description)
    assert not parsed.ok and parsed.reason == reason


def test_station_timezone_registry_covers_the_live_station_set_and_is_installed() -> None:
    from zoneinfo import ZoneInfo

    live = ["KLGA", "KDAL", "KATL", "KMIA", "KORD", "KAUS", "KBKF", "KHOU", "KSEA", "KLAX", "KSFO", "SBGR", "SAEZ", "CYYZ", "MMMX", "MPMG", "EGLC", "LFPB", "RKSI", "LTAC", "NZWN", "VILK", "EDDM", "RJTT", "ZSPD", "WSSS", "LIMC", "LEMD", "EPWA", "RCSS", "ZUCK", "ZBAA", "ZHHH", "ZUUU", "ZGSZ", "RKPK", "EHAM", "EFHK", "WMKK", "ZSJN", "ZHCC"]
    assert all(s in STATION_TIMEZONES for s in live)
    for zone in set(STATION_TIMEZONES.values()):
        ZoneInfo(zone)


# --------------------------------------------------------------------------
# METAR parsing, rounding, running high
# --------------------------------------------------------------------------
def test_metar_temperature_prefers_the_t_group_and_falls_back_to_the_body() -> None:
    raw = "METAR KLGA 142351Z 35013G19KT 10SM FEW060 22/08 A3015 RMK AO2 SLP209 T02170078 10239 20217 51022"
    assert parse_metar_temperature_tenths_c(raw) == 217
    assert parse_metar_temperature_tenths_c("METAR EGLC 142350Z 27005KT 9999 FEW030 M02/M05 Q1021") == -20
    assert parse_metar_temperature_tenths_c("METAR EGLC 142350Z 27005KT 9999 FEW030 RMK T11051120") == -105
    assert parse_metar_temperature_tenths_c("METAR EGLC 142350Z 27005KT 9999 FEW030 Q1021") is None
    o = parse_metar_observation("SPECI KLGA 142312Z 35013KT 10SM FEW060 22/08 RMK AO2 T02170078", month_anchor=datetime(2026, 9, 1, tzinfo=UTC))
    assert o is not None and o.report_type == "SPECI" and o.observed_at == datetime(2026, 9, 14, 23, 12, tzinfo=UTC) and o.temp_c == D("21.7")
    # The station named in the report wins over the hint (a mismatched source stays visible).
    assert parse_metar_observation("METAR KFLL 142353Z 10SM 25/20", station="KMIA", observed_at=datetime(2026, 9, 14, 23, 53, tzinfo=UTC)).station_icao == "KFLL"
    assert parse_metar_observation("no time here", station="KLGA") is None


def test_unit_conversion_carries_both_rounding_candidates_at_exact_halves() -> None:
    assert tenths_c_to_unit(233, TemperatureUnit.F) == (74, 74)  # 73.94
    assert tenths_c_to_unit(239, TemperatureUnit.F) == (75, 75)  # 75.02
    assert tenths_c_to_unit(225, TemperatureUnit.F) == (72, 73)  # 72.5 exactly
    assert tenths_c_to_unit(195, TemperatureUnit.C) == (19, 20)
    assert tenths_c_to_unit(200, TemperatureUnit.C) == (20, 20)
    assert tenths_c_to_unit(-5, TemperatureUnit.C) == (-1, 0)
    assert tenths_c_to_unit(-178, TemperatureUnit.F) == (0, 0)  # -0.04 F


def test_running_high_uses_hourly_reports_specials_only_widen_and_next_day_completes() -> None:
    rows = hourly("KLGA", "2026-09-15T04:51:00Z", [189, 200, 222, 233, 239, 233, 228, 222, 211, 200])
    rows.append(obs("KLGA", "2026-09-15T09:30:00Z", 242, "SPECI"))  # 76 F special, warmer than any hourly
    rows.append(obs("KDAL", "2026-09-15T12:53:00Z", 300))  # another station, ignored
    high = running_high(rows, KLGA_SPEC)
    assert high.hourly_observations == 10 and high.observations == 11
    assert (high.high_low, high.high_high) == (75, 75) and high.all_high_high == 76
    assert high.latest_temp_high == 68 and high.trend == "falling" and high.consecutive_nonrising == 5
    assert not high.day_complete and high.peak_observed_at == datetime(2026, 9, 15, 8, 51, tzinfo=UTC)
    complete = running_high(rows + [obs("KLGA", "2026-09-16T04:51:00Z", 167)], KLGA_SPEC)
    assert complete.day_complete and complete.first_next_day_observation == datetime(2026, 9, 16, 4, 51, tzinfo=UTC)
    # A next-day SPECI does not complete an hourly-only market.
    assert not running_high(rows + [obs("KLGA", "2026-09-16T04:10:00Z", 167, "SPECI")], KLGA_SPEC).day_complete
    ambiguous = running_high(hourly("KLGA", "2026-09-15T04:51:00Z", [200, 225, 220]), KLGA_SPEC)
    assert ambiguous.high_ambiguous and (ambiguous.high_low, ambiguous.high_high) == (72, 73)
    assert ambiguous.as_dict("America/New_York")["running_high"] is None
    assert running_high([], KLGA_SPEC).has_high is False


# --------------------------------------------------------------------------
# Kill rules
# --------------------------------------------------------------------------
def _high(tenths: list[int], *, start: str = "2026-09-15T04:51:00Z", extra: list[StationObservation] | None = None, spec: WeatherMarketSpec = KLGA_SPEC):
    return running_high(hourly(spec.station_icao, start, tenths) + (extra or []), spec)


def test_bucket_below_the_running_high_is_dead_at_any_hour_and_the_rest_waits() -> None:
    params = DeadBucketParameters()
    morning = _high([189, 200, 222])  # high 72 at 06:51 EDT, rising
    at = datetime(2026, 9, 15, 11, 0, tzinfo=UTC)
    assert classify_bucket(TemperatureBucket(66, 67), morning, KLGA_SPEC, params, as_of=at).rule is KillRule.DEAD_BELOW_RUNNING_HIGH
    assert classify_bucket(TemperatureBucket(None, 71), morning, KLGA_SPEC, params, as_of=at).outcome is Outcome.NO
    v = classify_bucket(TemperatureBucket(72, 73), morning, KLGA_SPEC, params, as_of=at)
    assert v.status == "too_early_in_day" and v.outcome is None
    assert classify_bucket(TemperatureBucket(84, None), morning, KLGA_SPEC, params, as_of=at).status == "too_early_in_day"
    two = _high([189, 200])
    assert classify_bucket(TemperatureBucket(60, 61), two, KLGA_SPEC, params, as_of=at).status == "insufficient_observations"


def test_late_day_falling_kills_above_and_names_the_certain_bucket() -> None:
    params = DeadBucketParameters()
    rows = hourly("KLGA", "2026-09-15T04:51:00Z", [189, 200, 222, 233, 239, 233, 228, 222, 211, 200, 194, 189, 183, 178, 172, 167, 161, 156])
    high = running_high(rows, KLGA_SPEC)  # high 75, latest 61 at 17:51 EDT
    at = datetime(2026, 9, 15, 22, 30, tzinfo=UTC)  # 18:30 EDT
    assert classify_bucket(TemperatureBucket(78, 79), high, KLGA_SPEC, params, as_of=at).rule is KillRule.DEAD_ABOVE_LATE_DAY
    assert classify_bucket(TemperatureBucket(77, None), high, KLGA_SPEC, params, as_of=at).rule is KillRule.DEAD_ABOVE_LATE_DAY
    live = classify_bucket(TemperatureBucket(76, 77), high, KLGA_SPEC, params, as_of=at)
    assert live.status == "live"  # lo 76 <= ceiling 76: could still print 76
    certain = classify_bucket(TemperatureBucket(75, 76), high, KLGA_SPEC, params, as_of=at)
    assert certain.rule is KillRule.CERTAIN_YES_LATE_DAY and certain.outcome is Outcome.YES
    assert classify_bucket(TemperatureBucket(74, 75), high, KLGA_SPEC, params, as_of=at).status == "live"  # does not cover 76
    # Before 17:00 local the same picture is too early; a flat tail is not falling.
    early = datetime(2026, 9, 15, 20, 0, tzinfo=UTC)
    assert classify_bucket(TemperatureBucket(78, 79), high, KLGA_SPEC, params, as_of=early).status == "too_early_in_day"
    flat = running_high(hourly("KLGA", "2026-09-15T04:51:00Z", [189, 200, 222, 233, 239, 239, 239]), KLGA_SPEC)
    assert classify_bucket(TemperatureBucket(80, 81), flat, KLGA_SPEC, params, as_of=at).status == "not_falling"
    # A warmer SPECI widens the ceiling: 76 F special -> ceiling 77 -> 76-77 is no longer dead-above candidate territory.
    with_speci = running_high(rows + [obs("KLGA", "2026-09-15T09:30:00Z", 242, "SPECI")], KLGA_SPEC)
    assert classify_bucket(TemperatureBucket(78, 79), with_speci, KLGA_SPEC, params, as_of=at).rule is KillRule.DEAD_ABOVE_LATE_DAY
    assert classify_bucket(TemperatureBucket(75, 76), with_speci, KLGA_SPEC, params, as_of=at).status == "live"


def test_day_complete_decides_every_bucket_and_halves_refuse() -> None:
    params = DeadBucketParameters()
    rows = hourly("KLGA", "2026-09-15T04:51:00Z", [189, 200, 222, 233, 239, 233, 200]) + [obs("KLGA", "2026-09-16T04:51:00Z", 160)]
    high = running_high(rows, KLGA_SPEC)
    at = datetime(2026, 9, 16, 5, 30, tzinfo=UTC)
    assert classify_bucket(TemperatureBucket(74, 75), high, KLGA_SPEC, params, as_of=at).rule is KillRule.CERTAIN_YES_DAY_COMPLETE
    assert classify_bucket(TemperatureBucket(76, 77), high, KLGA_SPEC, params, as_of=at).rule is KillRule.DEAD_DAY_COMPLETE
    assert classify_bucket(TemperatureBucket(None, 73), high, KLGA_SPEC, params, as_of=at).rule is KillRule.DEAD_DAY_COMPLETE
    half = running_high(hourly("KLGA", "2026-09-15T04:51:00Z", [189, 200, 225, 200]) + [obs("KLGA", "2026-09-16T04:51:00Z", 160)], KLGA_SPEC)
    assert (half.high_low, half.high_high) == (72, 73)
    assert classify_bucket(TemperatureBucket(71, 72), half, KLGA_SPEC, params, as_of=at).status == "bucket_edge_ambiguous"
    assert classify_bucket(TemperatureBucket(73, 74), half, KLGA_SPEC, params, as_of=at).status == "bucket_edge_ambiguous"
    assert classify_bucket(TemperatureBucket(72, 73), half, KLGA_SPEC, params, as_of=at).rule is KillRule.CERTAIN_YES_DAY_COMPLETE
    assert classify_bucket(TemperatureBucket(None, 70), half, KLGA_SPEC, params, as_of=at).rule is KillRule.DEAD_DAY_COMPLETE
    # Running (not final) high on a half: the straddling bucket is refused, lower ones still die.
    running_half = running_high(hourly("KLGA", "2026-09-15T04:51:00Z", [189, 200, 225]), KLGA_SPEC)
    at_morning = datetime(2026, 9, 15, 11, 0, tzinfo=UTC)
    assert classify_bucket(TemperatureBucket(71, 72), running_half, KLGA_SPEC, params, as_of=at_morning).status == "bucket_edge_ambiguous"
    assert classify_bucket(TemperatureBucket(69, 70), running_half, KLGA_SPEC, params, as_of=at_morning).rule is KillRule.DEAD_BELOW_RUNNING_HIGH


def test_celsius_markets_use_the_one_degree_margin() -> None:
    params = DeadBucketParameters()
    rows = hourly("SAEZ", "2026-09-15T03:00:00Z", [90, 100, 150, 180, 200, 200, 190])  # high 20, latest 19 at 18:00 ART
    high = running_high(rows, SAEZ_SPEC)
    at = datetime(2026, 9, 15, 21, 30, tzinfo=UTC)
    assert classify_bucket(TemperatureBucket(22, 23), high, SAEZ_SPEC, params, as_of=at).rule is KillRule.DEAD_ABOVE_LATE_DAY
    assert classify_bucket(TemperatureBucket(20, 21), high, SAEZ_SPEC, params, as_of=at).rule is KillRule.CERTAIN_YES_LATE_DAY
    not_yet = running_high(rows[:-1] + [obs("SAEZ", "2026-09-15T21:00:00Z", 200)], SAEZ_SPEC)
    assert classify_bucket(TemperatureBucket(22, 23), not_yet, SAEZ_SPEC, params, as_of=at).status == "not_falling"


def test_parameters_are_validated() -> None:
    with pytest.raises(ValueError):
        DeadBucketParameters(min_net_edge=D("1"))
    with pytest.raises(ValueError):
        DeadBucketParameters(late_day_local_hour=24)
    with pytest.raises(ValueError):
        DeadBucketParameters(upper_headroom_degrees=-1)
    assert DeadBucketParameters().falling_margin(TemperatureUnit.C) == 1


# --------------------------------------------------------------------------
# Edge after fees and order construction
# --------------------------------------------------------------------------
def test_net_edge_deducts_the_polymarket_taker_fee() -> None:
    gross, fee, net = net_edge_per_contract(D("0.97"), D("0.05"))
    assert gross == D("0.03") and fee == D("0.05") * D("0.97") * D("0.03") and net == gross - fee
    assert net_edge_per_contract(D("0.98"), D("0.05"))[2] < D("0.02")  # 2c gross does not clear the threshold after fees


def _dead_verdict(bucket: TemperatureBucket = TemperatureBucket(66, 67, "66-67°F")):
    high = _high([189, 200, 222, 233])
    return classify_bucket(bucket, high, KLGA_SPEC, DeadBucketParameters(), as_of=datetime(2026, 9, 15, 12, 0, tzinfo=UTC)), high


def test_strategy_buys_no_at_the_no_ask_sized_by_caps_and_depth() -> None:
    v, high = _dead_verdict()
    m = market()
    yes = book([("0.030", "100")], [("0.040", "100")])
    no = book([("0.960", "100")], [("0.970", "100")])
    risk = RiskManager(RiskLimits(D("25"), D("75"), D("75")))
    ev = DeadBucketStrategy(portfolio=None, risk=risk).evaluate(m, yes, no, v, spec=KLGA_SPEC, high=high)
    assert ev.traded and ev.reason == "trade" and ev.quantity == D("25")  # $25 / 0.97 -> 25 whole contracts
    (order,) = ev.orders
    assert order.outcome is Outcome.NO and order.price == D("0.970") and order.side.value == "buy"
    assert order.metadata["strategy"] == WEATHER_TRACK and order.metadata["kill_rule"] == "dead_below_running_high"
    assert ev.net_edge == D("0.03") - D("0.05") * D("0.970") * D("0.03") and ev.bid == D("0.960")
    # Depth caps the size; the venue minimum refuses tiny prints; a thin ask is refused.
    ev_depth = DeadBucketStrategy(risk=risk).evaluate(m, yes, book([("0.960", "100")], [("0.970", "8")]), v, spec=KLGA_SPEC, high=high)
    assert ev_depth.quantity == D("8")
    assert DeadBucketStrategy(risk=risk).evaluate(m, yes, book([], [("0.970", "3")]), v, spec=KLGA_SPEC, high=high).reason == "insufficient_depth"
    assert DeadBucketStrategy(risk=risk).evaluate(m, yes, book([], [("0.985", "100")]), v, spec=KLGA_SPEC, high=high).reason == "edge_below_threshold"
    # Empty NO ladder: fall back to the complement of the YES book (a YES bid is a NO ask); none -> no_ask with the bid reported.
    from_yes = DeadBucketStrategy(risk=risk).evaluate(m, book([("0.030", "50")], [("0.040", "50")]), OrderBook(market_id="M"), v, spec=KLGA_SPEC, high=high)
    assert from_yes.traded and from_yes.ask == D("0.970") and from_yes.quantity == D("25")
    none = DeadBucketStrategy(risk=risk).evaluate(m, book([], [("0.003", "800")]), book([("0.997", "800")], []), v, spec=KLGA_SPEC, high=high)
    assert none.reason == "no_ask" and none.bid == D("0.997") and none.bid_size == D("800")
    # Position cap: 75 contracts per market.
    from core.portfolio import Portfolio
    from core.types import Fill, Side

    portfolio = Portfolio()
    portfolio.apply_fill(Fill(Venue.POLYMARKET, "M", "x", Side.BUY, D("70"), D("0.97"), Outcome.NO))
    capped = DeadBucketStrategy(portfolio=portfolio, risk=risk).evaluate(m, yes, no, v, spec=KLGA_SPEC, high=high)
    assert capped.quantity == D("5")
    portfolio.apply_fill(Fill(Venue.POLYMARKET, "M", "x", Side.BUY, D("5"), D("0.97"), Outcome.NO))
    assert DeadBucketStrategy(portfolio=portfolio, risk=risk).evaluate(m, yes, no, v, spec=KLGA_SPEC, high=high).reason == "no_position_headroom"
    # Unknown fee metadata assumes the 5 % weather rate.
    assert DeadBucketStrategy(risk=risk).evaluate(market(fee=None), yes, no, v, spec=KLGA_SPEC, high=high).fee_rate == D("0.05")
    inactive = Market(Venue.POLYMARKET, "M", "closed", active=False, metadata={"taker_fee_rate": "0.05"})
    assert DeadBucketStrategy(risk=risk).evaluate(inactive, yes, no, v, spec=KLGA_SPEC, high=high).reason == "market_inactive"


def test_certain_yes_buys_yes_at_the_yes_ask() -> None:
    rows = hourly("KLGA", "2026-09-15T04:51:00Z", [189, 200, 222, 233, 239, 233, 200]) + [obs("KLGA", "2026-09-16T04:51:00Z", 160)]
    high = running_high(rows, KLGA_SPEC)
    v = classify_bucket(TemperatureBucket(74, 75, "74-75°F"), high, KLGA_SPEC, DeadBucketParameters(), as_of=datetime(2026, 9, 16, 5, 30, tzinfo=UTC))
    ev = DeadBucketStrategy().evaluate(market(), book([("0.950", "500")], [("0.960", "500")]), book([("0.040", "500")], [("0.050", "500")]), v, spec=KLGA_SPEC, high=high)
    assert ev.traded and ev.orders[0].outcome is Outcome.YES and ev.orders[0].price == D("0.960") and ev.quantity == D("26")


# --------------------------------------------------------------------------
# Pre-registered verdict
# --------------------------------------------------------------------------
def _settled(n: int, *, station_days: int, net: str = "0.75", won: bool = True, rule: KillRule = KillRule.DEAD_BELOW_RUNNING_HIGH, side: Outcome = Outcome.NO) -> list[SettledRecord]:
    return [SettledRecord(f"K{i % station_days:03d}:2026-09-15", rule, side, D("25"), D(net), won) for i in range(n)]


def test_verdict_applies_the_pre_registered_rule_and_kill_switch() -> None:
    passing = verdict(_settled(30, station_days=10))
    assert passing.status == "PASS" and passing.n == 30 and passing.station_days == 10 and passing.mean_net_per_contract == D("0.03")
    assert verdict(_settled(30, station_days=9)).status == "INSUFFICIENT_DATA"
    assert verdict(_settled(29, station_days=10)).status == "INSUFFICIENT_DATA"
    thin = verdict(_settled(30, station_days=10, net="0.40"))  # 1.6c per contract
    assert thin.status == "FAIL" and not thin.kill_rule_triggered
    one_loss = verdict(_settled(29, station_days=10) + _settled(1, station_days=1, net="-24.5", won=False))
    assert one_loss.status == "FAIL" and one_loss.kill_rule_triggered and one_loss.losses == 1
    small_loss = verdict(_settled(5, station_days=5) + _settled(1, station_days=1, net="-24.5", won=False))
    assert small_loss.status == "INSUFFICIENT_DATA" and small_loss.kill_rule_triggered
    yes_side = verdict(_settled(30, station_days=10, side=Outcome.YES, rule=KillRule.CERTAIN_YES_DAY_COMPLETE), side=Outcome.YES)
    assert yes_side.status == "PASS" and "certain_yes_day_complete" in yes_side.by_rule
    assert verdict(_settled(30, station_days=10, side=Outcome.YES), side=Outcome.NO).n == 0
    d = passing.as_dict()
    assert d["pre_registered"]["min_settled_positions"] == 30 and d["pre_registered"]["min_station_days"] == 10


# --------------------------------------------------------------------------
# Observation sources
# --------------------------------------------------------------------------
AWC_ROW = {
    "icaoId": "KLGA", "receiptTime": "2026-09-14T23:54:08.569Z", "obsTime": 1789429860, "reportTime": "2026-09-15T00:00:00.000Z",
    "temp": 21.7, "dewp": 7.8, "metarType": "METAR",
    "rawOb": "METAR KLGA 142351Z 35013G19KT 10SM FEW060 22/08 A3015 RMK AO2 SLP209 T02170078 10239 20217 51022",
}


def test_aviationweather_and_nws_payloads_normalise_to_the_same_observation() -> None:
    (awc,) = parse_aviationweather_payload([AWC_ROW, {"icaoId": "KLGA", "obsTime": None}, "junk"])
    assert awc.station_icao == "KLGA" and awc.temp_tenths_c == 217 and awc.observed_at == datetime.fromtimestamp(1789429860, tz=UTC) and awc.report_type == "METAR"
    (no_raw,) = parse_aviationweather_payload([{"icaoId": "KLGA", "obsTime": 1789429860, "temp": 21.66, "metarType": "SPECI"}])
    assert no_raw.temp_tenths_c == 217 and no_raw.report_type == "SPECI"
    nws = {"features": [{"properties": {"station": "https://api.weather.gov/stations/KLGA", "timestamp": "2026-09-14T23:51:00+00:00", "temperature": {"value": 21.7, "unitCode": "wmoUnit:degC"}, "rawMessage": AWC_ROW["rawOb"]}}, {"properties": {"timestamp": "2026-09-14T22:51:00+00:00", "temperature": {"value": None}}}]}
    (row,) = parse_nws_payload(nws, station="KLGA")
    assert (row.station_icao, row.temp_tenths_c, row.observed_at) == (awc.station_icao, awc.temp_tenths_c, awc.observed_at)
    assert hours_back(datetime(2026, 9, 15, 4, tzinfo=UTC), datetime(2026, 9, 15, 22, 30, tzinfo=UTC)) == 20
    assert hours_back(datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 9, 15, tzinfo=UTC)) == 48


async def test_chained_source_falls_back_per_station_and_reports_errors() -> None:
    calls: list[tuple[str, dict]] = []

    async def awc(url: str, params: dict) -> object:
        calls.append(("awc", params))
        if params["ids"] == "KLGA":
            return [AWC_ROW]
        if params["ids"] == "KORD":
            raise ConnectionError("awc down")
        return []

    async def nws(url: str, params: dict) -> object:
        calls.append(("nws", {"url": url, **params}))
        return {"features": [{"properties": {"station": url.rsplit("/observations", 1)[0], "timestamp": "2026-09-14T23:51:00+00:00", "temperature": {"value": 20.0}, "rawMessage": ""}}]}

    source = ChainedObservationSource(AviationWeatherMetarSource(http_get=awc), NwsObservationSource(http_get=nws))
    start, end = datetime(2026, 9, 14, 4, tzinfo=UTC), datetime(2026, 9, 15, 1, tzinfo=UTC)
    batch = await source.fetch({"KLGA", "KORD", "KSEA"}, start=start, end=end)
    assert batch.source == "aviationweather+nws" and set(batch.observations) == {"KLGA", "KORD", "KSEA"}
    assert batch.for_station("KLGA")[0].temp_tenths_c == 217 and batch.for_station("KORD")[0].temp_tenths_c == 200
    assert any("awc down" in e for e in batch.errors) and batch.requests == 4  # 2 successful AWC calls + 2 fallback calls
    assert [p["ids"] for kind, p in calls if kind == "awc"] == ["KLGA", "KORD", "KSEA"]
    assert {p["url"].rsplit("/", 2)[1] for kind, p in calls if kind == "nws"} == {"KORD", "KSEA"}
    assert all(p["format"] == "json" and p["hours"] == 22 for kind, p in calls if kind == "awc")

    async def dead(url: str, params: dict) -> object:
        raise ConnectionError("offline")

    offline = await ChainedObservationSource(AviationWeatherMetarSource(http_get=dead), NwsObservationSource(http_get=dead)).fetch({"KLGA"}, start=start, end=end)
    assert offline.observations == {} and len(offline.errors) == 2


async def test_file_and_null_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    saved = tmp_path / "metar.json"
    saved.write_text(json.dumps([AWC_ROW]))
    batch = await JsonObservationSource(saved).fetch({"KLGA"}, start=datetime(2026, 9, 14, tzinfo=UTC), end=datetime(2026, 9, 15, tzinfo=UTC))
    assert len(batch.for_station("KLGA")) == 1
    broken = tmp_path / "broken.json"
    broken.write_text("{nope")
    assert (await JsonObservationSource(broken).fetch({"KLGA"}, start=datetime(2026, 9, 14, tzinfo=UTC), end=datetime(2026, 9, 15, tzinfo=UTC))).errors
    assert (await NullObservationSource().fetch({"KLGA"}, start=datetime(2026, 9, 14, tzinfo=UTC), end=datetime(2026, 9, 15, tzinfo=UTC))).source == "none"
    assert build_observation_source(use_fixtures=True) is None
    assert isinstance(build_observation_source(use_fixtures=False), ChainedObservationSource)
    assert isinstance(build_observation_source(use_fixtures=False, obs_file=saved), JsonObservationSource)


# --------------------------------------------------------------------------
# Polymarket discovery (mocked Gamma / CLOB) and venue resolution
# --------------------------------------------------------------------------
async def test_weather_event_discovery_reads_tagged_gamma_events_and_both_books() -> None:
    event = {
        "id": "1009108", "slug": "highest-temperature-in-nyc-on-september-14-2026", "title": "Highest temperature in NYC on September 14?",
        "description": NYC_DESC, "resolutionSource": "https://www.weather.gov/wrh/timeseries?site=klga", "negRisk": True, "closed": False, "active": True,
        "tags": [{"id": "84", "label": "Weather"}, {"id": "104596", "label": "Highest temperature"}],
        "markets": [
            {"conditionId": "0x6d95", "question": "Will the highest temperature in New York City be 65°F or below on September 14?", "slug": "nyc-65forbelow", "groupItemTitle": "65°F or below", "clobTokenIds": '["111", "222"]', "active": True, "closed": False, "acceptingOrders": True, "feesEnabled": True, "feeType": "weather_fees", "negRisk": True, "description": NYC_DESC},
            {"conditionId": "0xdead", "question": "closed leg", "groupItemTitle": "66-67°F", "clobTokenIds": '["333", "444"]', "active": True, "closed": True},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("gamma"):
            assert request.url.params["tag_id"] == "104596" and request.url.params["closed"] == "false"
            return httpx.Response(200, json=[event, "junk"])
        payload = json.loads(request.content)
        assert [p["token_id"] for p in payload] == ["111", "222"]
        return httpx.Response(200, json=[
            {"asset_id": "111", "market": "0x6d95", "bids": [], "asks": [{"price": "0.003", "size": "800"}]},
            {"asset_id": "222", "market": "0x6d95", "bids": [{"price": "0.997", "size": "800"}], "asks": []},
        ])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = PolymarketClient(paper=True, use_fixtures=False, http=http, search_terms=None)
    try:
        snapshot = await capture_weather_snapshot(limit=10, client=client)
    finally:
        await client.close()
        await http.aclose()
    assert snapshot.source == "network" and len(snapshot.groups) == 1 and snapshot.errors == []
    (group,) = snapshot.groups
    assert group.size == 1 and group.metadata["description"] == NYC_DESC and group.metadata["tags"] == ["Weather", "Highest temperature"]
    leg = group.markets[0]
    assert leg.metadata["taker_fee_rate"] == "0.05" and leg.metadata["source_url"].endswith("site=klga") and leg.metadata["group_item_title"] == "65°F or below"
    assert snapshot.book(leg).best_ask.price == D("0.003") and snapshot.no_book(leg).best_bid.price == D("0.997") and snapshot.no_book(leg).best_ask is None
    (parsed,) = weather_events(snapshot)
    assert parsed.parsed and parsed.spec.station_icao == "KLGA" and parsed.buckets["0x6d95"] == TemperatureBucket(None, 65, "65°F or below")


def test_venue_resolution_is_only_a_closed_binary_price() -> None:
    assert classify_venue_resolution({"closed": True, "outcome_prices": ["1", "0"]}) == (Outcome.YES, "polymarket_resolved_yes")
    assert classify_venue_resolution({"closed": True, "outcome_prices": ["0", "1"]}) == (Outcome.NO, "polymarket_resolved_no")
    assert classify_venue_resolution({"closed": True, "outcome_prices": ["0.5", "0.5"]})[0] is None
    assert classify_venue_resolution({"closed": False, "outcome_prices": ["0.9995", "0.0005"]}) == (None, None)
    assert classify_venue_resolution(None) == (None, None)


# --------------------------------------------------------------------------
# Fixture replay through the live code path
# --------------------------------------------------------------------------
async def test_replay_measures_every_scripted_branch() -> None:
    summary, ledger, register, steps = await replay_fixture()
    assert [s.metrics["step"] for s in steps] == ["t0_morning", "t1_late_day", "t2_day_complete", "t3_settlement"]
    assert summary.candidates == 140 and summary.admitted == 32 and summary.proposed_orders == 32 and summary.paper_fills == 32
    assert summary.metrics["positions_opened"] == 32 and summary.metrics["positions_settled_this_run"] == 32 and summary.metrics["settlement_fills"] == 32
    reasons = summary.refused_by_reason
    expected = {
        "already_positioned": 25, "bucket_edge_ambiguous": 1, "edge_below_threshold": 23, "insufficient_depth": 1, "live": 2,
        "market_kind_unsupported": 3, "no_ask": 1, "not_falling": 4, "stale_obs": 11, "station_mismatch": 3,
        "station_timezone_unknown": 2, "station_unparsed": 6, "too_early_in_day": 26,
    }
    assert reasons == expected, reasons
    assert summary.metrics["dead_buckets_without_taker_ask"] == 1 and summary.metrics["resting_only_opportunities"][0]["bucket"] == "80-81°F"

    morning, late, complete, settlement = steps
    assert morning.admitted == 6 and late.admitted == 24 and complete.admitted == 2 and settlement.admitted == 0
    highs = summary.metrics["running_highs"]
    klga, kdal, saez = highs["KLGA:2026-09-15"], highs["KDAL:2026-09-15"], highs["SAEZ:2026-09-15"]
    assert (klga["running_high_low"], klga["running_high_high"], klga["running_high_all_reports_high"], klga["day_complete"]) == (75, 75, 76, True)
    assert late.metrics["running_highs"]["KLGA:2026-09-15"]["latest_observed_at_local"] == "2026-09-15T17:51:00-04:00"
    assert (kdal["running_high_low"], kdal["running_high_high"]) == (81, 81) and morning.metrics["running_highs"]["KDAL:2026-09-15"]["running_high"] is None
    assert saez["day_complete"] and saez["running_high_low"] == 20

    by_bucket = {(r.city, r.bucket_label): r for r in register.records.values()}
    nyc_66 = by_bucket[("NYC", "66-67°F")]
    assert nyc_66.rule == "dead_below_running_high" and nyc_66.buy_outcome == "no" and nyc_66.quantity == "25" and nyc_66.entry_price == "0.9750"
    assert nyc_66.status == "settled" and nyc_66.venue_outcome == "no" and nyc_66.obs_implied_outcome == "no" and nyc_66.agreement == "agree"
    assert D(nyc_66.realized_pnl) == (D("25") * D("0.025") - D(nyc_66.entry_fee)).quantize(D("0.0001"))
    assert by_bucket[("NYC", "78-79°F")].rule == "dead_above_late_day" and by_bucket[("NYC", "76-77°F")].rule == "dead_day_complete"
    nyc_74 = by_bucket[("NYC", "74-75°F")]
    assert nyc_74.rule == "certain_yes_day_complete" and nyc_74.buy_outcome == "yes" and nyc_74.entry_price == "0.9600" and nyc_74.won
    chi_78 = by_bucket[("Chicago", "78-79°F")]
    assert chi_78.rule == "certain_yes_late_day" and chi_78.quantity == "45" and chi_78.won and chi_78.agreement == "unknown"
    assert by_bucket[("Dallas", "75-76°F")].quantity == "8"  # depth-capped, above the venue minimum
    assert ("Dallas", "71-72°F") in by_bucket and by_bucket[("Dallas", "71-72°F")].opened_at.startswith("2026-09-15T22:30")  # ambiguous in the morning, dead once the high moved on
    ba_18 = by_bucket[("Buenos Aires", "18-19°C")]
    assert ba_18.venue_outcome == "yes" and ba_18.obs_implied_outcome == "no" and ba_18.agreement == "disagree" and ba_18.won is False
    assert D(ba_18.realized_pnl) == (-(D("28") * D("0.88")) - D(ba_18.entry_fee)).quantize(D("0.0001"))
    assert by_bucket[("Buenos Aires", "20-21°C")].won is False and by_bucket[("Buenos Aires", "22-23°C")].won

    v = summary.metrics["verdict"]
    assert v["n"] == 29 and v["station_days"] == 5 and v["losses"] == 1 and v["kill_rule_triggered"] is True and v["status"] == "INSUFFICIENT_DATA"
    assert v["by_rule"]["dead_below_running_high"]["n"] == 24 and v["by_rule"]["dead_above_late_day"]["n"] == 4 and v["by_rule"]["dead_day_complete"]["n"] == 1
    y = summary.metrics["verdict_certain_yes"]
    assert y["n"] == 3 and y["losses"] == 1
    assert summary.metrics["register"]["agreement_venue_vs_observations"] == {"agree": 9, "disagree": 2, "unknown": 21}

    assert ledger.ledger_id == WEATHER_TRACK and len(ledger.fills) == 64 and ledger.open_positions == []
    assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl
    assert sum(1 for f in ledger.fills if f.order_id == "settlement") == 32
    fills_66 = [f for f in ledger.fills if f.market_id == nyc_66.record_id]
    assert [str(f.price) for f in fills_66] == ["0.975", "0"] and fills_66[0].fee == (D("25") * D("0.05") * D("0.975") * D("0.025")).quantize(D("0.00001"))
    assert all(row["strategy"] == WEATHER_TRACK for row in summary.fills)
    assert summary.metrics["status"] == "fixture_synthetic" and summary.metrics["network_status"] == "fixture_synthetic"
    assert summary.metrics["events_total"] == 17 and summary.metrics["events_parsed"] == 13


async def test_register_round_trips_and_a_resumed_replay_matches_a_straight_one(tmp_path: Path) -> None:
    steps = load_replay()
    _, _, straight, _ = await replay_fixture(steps)
    _, ledger, register, _ = await replay_fixture(steps[:2])
    assert len(register.open_records()) == 30
    path = tmp_path / "register.json"
    register.save(path)
    ledger.save(tmp_path / "ledger.json")
    reloaded = DeadBucketRegister.load_or_create(path)
    _, ledger2, register2, _ = await replay_fixture(steps[2:], ledger=PaperLedger.load(tmp_path / "ledger.json"), register=reloaded)
    assert {k: (r.status, r.realized_pnl, r.agreement) for k, r in register2.records.items()} == {k: (r.status, r.realized_pnl, r.agreement) for k, r in straight.records.items()}
    assert ledger2.equity == ledger2.starting_cash + ledger2.realized_pnl + ledger2.unrealized_pnl
    with pytest.raises(ValueError):
        DeadBucketRegister.from_dict({"paper_only": False, "records": []})


async def test_replaying_a_settled_register_again_opens_nothing_new() -> None:
    _, ledger, register, _ = await replay_fixture()
    again, ledger2, register2, _ = await replay_fixture(ledger=ledger, register=register)
    assert again.admitted == 0 and again.paper_fills == 0 and len(register2.records) == 32
    assert again.refused_by_reason["already_positioned"] >= 32
    assert ledger2.equity == ledger.equity


async def test_network_path_without_observations_is_an_honest_empty() -> None:
    step = load_replay()[1]

    async def no_lookup(record):
        return None

    summary, ledger, register = await measure_weather_dead_bucket(use_fixtures=False, snapshot=step.snapshot, observation_source=NullObservationSource(), as_of=step.as_of, settlement_lookup=no_lookup)
    assert summary.metrics["status"] == "no_observation_source" and summary.metrics["network_status"].startswith("UNKNOWN")
    assert summary.admitted == 0 and summary.paper_fills == 0 and register.records == {}
    assert set(summary.refused_by_reason) == {"no_obs"} and ledger.equity == ledger.starting_cash


async def test_exploding_observation_source_does_not_take_the_run_down() -> None:
    step = load_replay()[1]

    class Exploding:
        name = "exploding"

        async def fetch(self, stations, *, start, end):
            raise RuntimeError("boom")

    async def no_lookup(record):
        return None

    summary, _, _ = await measure_weather_dead_bucket(use_fixtures=False, snapshot=step.snapshot, observation_source=Exploding(), as_of=step.as_of, settlement_lookup=no_lookup)
    assert summary.metrics["status"] == "observation_source_errors" and "RuntimeError" in summary.metrics["observation_source"]["errors"][0]
    assert summary.refused_by_reason == {"no_obs": 52}


async def test_network_style_cycle_with_static_observations_trades_and_a_failing_lookup_leaves_records_open() -> None:
    step = load_replay()[1]

    async def broken_lookup(record):
        raise ConnectionError("gamma down")

    summary, ledger, register = await measure_weather_dead_bucket(
        use_fixtures=False, snapshot=step.snapshot, observation_source=StaticObservationSource(step.observations, source="aviationweather"),
        as_of=step.as_of, settlement_lookup=broken_lookup,
    )
    assert summary.metrics["status"] == "measured" and summary.metrics["network_status"] == "measured_against_free_source (aviationweather)"
    # Without the morning cycle's positions the 0.99 legs are below threshold; 24 late-day legs + NYC 70-71 trade.
    assert summary.admitted == 25 and len(register.open_records()) == 25
    assert all(r.settlement_detail.startswith("lookup_error") for r in register.open_records())
    assert ledger.open_positions and ledger.unmarked_positions == 0
    assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl


async def test_stale_observations_refuse_the_whole_event() -> None:
    step = load_replay()[0]
    seattle = {"KSEA": step.observations["KSEA"]}
    snapshot = VenueSnapshot(venue=Venue.POLYMARKET, source="network", groups=[g for g in step.snapshot.groups if "Seattle" in g.title])
    snapshot.markets = [m for g in snapshot.groups for m in g.markets]
    snapshot.books, snapshot.no_books = dict(step.snapshot.books), dict(step.snapshot.no_books)

    async def no_lookup(record):
        return None

    summary, _, _ = await measure_weather_dead_bucket(use_fixtures=False, snapshot=snapshot, observation_source=StaticObservationSource(seattle, source="aviationweather"), as_of=step.as_of, settlement_lookup=no_lookup)
    assert summary.refused_by_reason == {"stale_obs": 11}
    assert summary.metrics["weather_events"][0]["observation_status"] == "stale_obs"


# --------------------------------------------------------------------------
# Artifacts, scoreboard wiring and CLI
# --------------------------------------------------------------------------
def test_weather_tracks_are_additive_and_not_part_of_the_full_board() -> None:
    from research.weather_tracks import WEATHER_TRACKS as RESERVED_WEATHER_TRACKS

    assert WEATHER_TRACKS == (WEATHER_TRACK,) and WEATHER_TRACK not in TRACKS
    assert WEATHER_TRACK in RESERVED_WEATHER_TRACKS
    assert len(TRACKS) == 13


async def test_scoreboard_artifact_is_measured_and_ledger_backed(tmp_path: Path) -> None:
    summary, ledger, _ = await measure_weather_dead_bucket()
    artifact = persist_run(
        [summary], {WEATHER_TRACK: ledger}, artifact_dir=tmp_path, mode="fixtures", measured_at="t", limit=60, kalshi_env=None,
        scoreboard_name="scoreboard_weather_dead_bucket.json", write_latest=False,
        artifact_kwargs={"primary_track": WEATHER_TRACK, "venues": ("polymarket",), "venue_focus": "polymarket", "label_suffix": "WEATHER DEAD BUCKET", "track_family": "weather_dead_bucket"},
    )
    assert artifact["meta"]["source"] == "measured" and artifact["meta"]["track_family"] == "weather_dead_bucket" and artifact["meta"]["venues"] == ["polymarket"]
    assert artifact["meta"]["pnl_source"] == "core.ledger.PaperLedger"
    assert artifact["totals"]["paper_pnl"] == ledger.total_pnl.quantize(D("0.0001")) and artifact["totals"]["paper_fills"] == 32
    row = artifact["tracks"][0]
    assert row["track"] == WEATHER_TRACK and row["metrics"]["records"] == {"count": 32, "detail": "weather_dead_bucket_latest.json"}
    assert row["metrics"]["measurements"]["detail"] == "weather_dead_bucket_latest.json" and row["metrics"]["weather_events"]["count"] == 17
    assert row["metrics"]["verdict"]["status"] == "INSUFFICIENT_DATA" and row["metrics"]["verdict"]["kill_rule_triggered"] is True
    assert (tmp_path / "paper" / f"ledger_{WEATHER_TRACK}.json").exists() and not (tmp_path / "scoreboard_latest.json").exists()
    json.dumps(artifact, default=json_default)


def test_cli_fixture_run_writes_report_register_and_ledger(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apps.measure_weather", "--artifact-dir", str(tmp_path)],
        check=False, capture_output=True, text=True,
        env={**os.environ, "TRADING_MODE": "paper", "ENABLE_LIVE_TRADING": "false"},
    )
    assert result.returncode == 0, result.stderr
    assert "status=fixture_synthetic" in result.stdout and "INSUFFICIENT_DATA" in result.stdout and "kill=True" in result.stdout
    report = json.loads((tmp_path / "weather_dead_bucket_latest.json").read_text())
    assert report["paper_only"] is True and report["kind"] == "weather_dead_bucket_report" and report["source"] == "measured"
    assert report["verdict"]["n"] == 29 and report["verdict"]["kill_rule_triggered"] is True
    assert report["experiment"]["pre_registered"]["min_settled_positions"] == 30
    assert len(report["records"]) == 32 and report["not_validated"] and report["experiment"]["observation_sources"]["primary"]
    assert (tmp_path / "weather_dead_bucket" / "register.json").exists()
    assert (tmp_path / "paper" / "ledger_weather_dead_bucket.json").exists()
    assert (tmp_path / "scoreboard_weather_dead_bucket.json").exists() and not (tmp_path / "scoreboard_latest.json").exists()
    # A second run over the persisted register opens nothing new and keeps the ledger.
    again = subprocess.run([sys.executable, "-m", "apps.measure_weather", "--artifact-dir", str(tmp_path)], check=False, capture_output=True, text=True, env={**os.environ, "TRADING_MODE": "paper", "ENABLE_LIVE_TRADING": "false"})
    assert again.returncode == 0 and "opened=0" in again.stdout


def test_cli_refuses_live_flags(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apps.measure_weather", "--artifact-dir", str(tmp_path)],
        check=False, capture_output=True, text=True,
        env={**os.environ, "TRADING_MODE": "live", "ENABLE_LIVE_TRADING": "true"},
    )
    assert result.returncode != 0 and not (tmp_path / "weather_dead_bucket_latest.json").exists()
