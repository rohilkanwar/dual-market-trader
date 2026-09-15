import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from core.types import Market, OrderBook, PriceLevel, Venue
from research.scoreboard import DEFAULT_RISK_LIMITS, TrackRuntime, VenueSnapshot
from research.scoreboard_artifact import build_scoreboard_artifact
from research.weather_calibration_track import (
    AdmissionRecord,
    CityDayRecord,
    WeatherRegister,
    backfill_calibration,
    create_runtimes,
    load_replay,
    replay_fixture,
    run_weather_cycle,
    settle_pending,
    snapshot_from_events,
)
from research.weather_calibration_sources import (
    FixtureUniverse,
    OpenMeteoSource,
    StaticForecastSource,
    _daily_max,
    event_from_fixture,
    event_from_gamma,
)
from strategies.weather_calibration import (
    CALIBRATED_TRACK,
    NAIVE_TRACK,
    CalibrationSample,
    CalibrationStore,
    SettledRecord,
    WeatherLaneStrategy,
    WeatherParameters,
    ab_verdict,
    bucket_probability,
    calibrated_ensemble,
    ladder_probabilities,
    naive_ensemble,
    normal_cdf,
    walk_forward_skill,
)
from strategies.weather_types import (
    CityRegistry,
    WeatherEvent,
    WeatherLeg,
    ladder_is_contiguous,
    parse_bucket_label,
    parse_target_date,
    parse_weather_event_title,
)
from venues.polymarket.client import PolymarketClient

D = Decimal
REGISTRY = CityRegistry.load()
PARAMS = WeatherParameters()


# --------------------------------------------------------------------------
# Shared types
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "label,lo,hi,unit",
    [
        ("65°F or below", None, 65, "F"),
        ("66-67°F", 66, 67, "F"),
        ("between 74-75°F", 74, 75, "F"),
        ("84°F or higher", 84, None, "F"),
        ("27°C", 27, 27, "C"),
        ("26°C or below", None, 26, "C"),
        ("36°C or higher", 36, None, "C"),
        ("-3°C", -3, -3, "C"),
    ],
)
def test_bucket_labels_parse(label: str, lo: int | None, hi: int | None, unit: str) -> None:
    bucket = parse_bucket_label(label)
    assert bucket is not None and (bucket.lo, bucket.hi, bucket.unit) == (lo, hi, unit)


def test_bucket_edges_follow_the_source_precision() -> None:
    two_f = parse_bucket_label("74-75°F")
    assert two_f is not None and two_f.edges("whole") == (73.5, 75.5)
    assert two_f.contains(73.5) and two_f.contains(75.49) and not two_f.contains(75.5)
    assert two_f.midpoint("whole") == 74.5
    one_c = parse_bucket_label("31°C")
    assert one_c is not None and one_c.edges("whole") == (30.5, 31.5) and one_c.edges("tenth") == (31.0, 32.0)
    assert one_c.midpoint("tenth") == 31.5 and one_c.midpoint("whole") == 31.0
    low = parse_bucket_label("65°F or below")
    high = parse_bucket_label("84°F or higher")
    assert low is not None and high is not None
    assert low.edges("whole")[0] == float("-inf") and high.edges("whole")[1] == float("inf")
    assert low.midpoint("whole", ladder_width=2) == 64.5 and high.midpoint("whole", ladder_width=2) == 84.5
    assert parse_bucket_label("nonsense") is None and parse_bucket_label("74-75°F", unit="C") is None


def test_ladder_contiguity_requires_both_open_ends_and_touching_neighbours() -> None:
    labels = ["65°F or below", "66-67°F", "68-69°F", "70°F or higher"]
    buckets = [parse_bucket_label(l) for l in labels]
    assert ladder_is_contiguous([b for b in buckets if b is not None])
    gap = [parse_bucket_label(l) for l in ["65°F or below", "68-69°F", "70°F or higher"]]
    assert not ladder_is_contiguous([b for b in gap if b is not None])
    no_top = [parse_bucket_label(l) for l in ["65°F or below", "66-67°F"]]
    assert not ladder_is_contiguous([b for b in no_top if b is not None])


def test_event_title_date_and_city_registry() -> None:
    assert parse_weather_event_title("Highest temperature in NYC on September 14?") == ("NYC", 9, 14)
    assert parse_weather_event_title("Highest temperature in Seoul (Incheon) on March 3?") == ("Seoul (Incheon)", 3, 3)
    assert parse_weather_event_title("Will it rain in NYC?") is None
    assert parse_target_date("Highest temperature in NYC on September 14?", slug="highest-temperature-in-nyc-on-september-14-2026") == date(2026, 9, 14)
    assert parse_target_date("Highest temperature in NYC on September 14?", end_date="2026-09-14T12:00:00Z") == date(2026, 9, 14)
    assert parse_target_date("Highest temperature in NYC on September 14?") is None
    nyc = REGISTRY.resolve("NYC")
    assert nyc is not None and nyc.station == "KLGA" and nyc.unit == "F" and REGISTRY.resolve("New York City") is nyc
    assert REGISTRY.resolve("São Paulo") is REGISTRY.resolve("Sao Paulo")
    assert REGISTRY.resolve("Seoul (Incheon)") is not None and REGISTRY.resolve("Atlantis") is None
    hk = REGISTRY.resolve("Hong Kong")
    assert hk is not None and hk.precision == "tenth" and hk.station is None
    assert len(REGISTRY) >= 50
    as_of = datetime(2026, 9, 15, 0, 30, tzinfo=UTC)  # 20:30 EDT Sep 14 in New York, 08:30 Sep 15 in Hong Kong
    assert nyc.local_date(as_of) == date(2026, 9, 14) and hk.local_date(as_of) == date(2026, 9, 15)


# --------------------------------------------------------------------------
# Probabilities and calibration store
# --------------------------------------------------------------------------
def test_ladder_probabilities_tile_the_line_and_move_with_the_mean() -> None:
    assert abs(normal_cdf(0.0) - 0.5) < 1e-12 and abs(normal_cdf(1.96) - 0.975) < 1e-3
    labels = ["65°F or below"] + [f"{lo}-{lo + 1}°F" for lo in range(66, 84, 2)] + ["84°F or higher"]
    buckets = [b for b in (parse_bucket_label(l) for l in labels) if b is not None]
    assert ladder_is_contiguous(buckets)
    probs = ladder_probabilities(77.2, 1.5, buckets, "whole")
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    assert max(probs, key=probs.get) == "76-77°F"
    assert bucket_probability(77.2, 1.5, buckets[6], "whole") > bucket_probability(80.0, 1.5, buckets[6], "whole")
    with pytest.raises(ValueError):
        bucket_probability(77.0, 0.0, buckets[0])


def _sample(city: str, day: str, truth_lo: int, forecasts: dict[str, float], *, unit: str = "F", source: str = "open_meteo_previous_runs_day1") -> CalibrationSample:
    return CalibrationSample(
        city=city, date=day, unit=unit, precision="whole", truth_lo=truth_lo, truth_hi=truth_lo + 1, truth_value=truth_lo + 0.5,
        forecasts=forecasts, forecast_source=source, truth_source="polymarket_resolution",
    )


def _biased_store(n: int = 30) -> CalibrationStore:
    store = CalibrationStore()
    for i in range(n):
        truth_lo = 70 + 2 * (i % 5)
        truth = truth_lo + 0.5
        store.add_sample(
            _sample("nyc", (date(2026, 7, 1) + timedelta(days=i)).isoformat(), truth_lo, {
                "gfs_seamless": truth + 3.0 + (0.3 if i % 2 else -0.3),
                "ecmwf_ifs025": truth + (0.4 if i % 3 else -0.4),
                "gem_seamless": truth + (5.0 if i % 2 else -5.0),
            })
        )
    return store


def test_calibration_store_learns_bias_variance_hit_rate_and_weights() -> None:
    store = _biased_store()
    cal = store.city_calibration("nyc", unit="F", precision="whole", parameters=PARAMS, before=date(2026, 9, 1))
    assert cal.n_days == 30 and cal.adequate
    gfs, ecmwf, gem = cal.models["gfs_seamless"], cal.models["ecmwf_ifs025"], cal.models["gem_seamless"]
    assert abs(gfs.bias - 3.0) < 0.05 and abs(ecmwf.bias) < 0.2 and abs(gem.bias) < 0.5
    assert gfs.raw_hit_rate == 0.0 and gfs.hit_rate == 1.0  # biased but sharp: only the de-biased forecast lands in the bucket
    assert gem.variance > gfs.variance and gem.hit_rate < 0.5
    assert cal.weights["gem_seamless"] < cal.weights["gfs_seamless"] and cal.weights["gem_seamless"] < cal.weights["ecmwf_ifs025"]
    assert abs(sum(cal.weights.values()) - 1.0) < 1e-9
    assert cal.best_model == "ecmwf_ifs025"
    estimate = calibrated_ensemble(cal, {"gfs_seamless": 80.0, "ecmwf_ifs025": 77.1, "gem_seamless": 72.0})
    assert abs(estimate.mean - 77.0) < 0.6 and estimate.adequate and estimate.n_calibration_days == 30
    naive = naive_ensemble({"gfs_seamless": 80.0, "ecmwf_ifs025": 77.1, "gem_seamless": 72.0}, sigma=3.0, sigma_source="test")
    assert abs(naive.mean - 76.3667) < 1e-3 and naive.sigma == 3.0


def test_walk_forward_skill_scores_only_days_with_prior_history_and_rewards_the_calibrated_lane() -> None:
    store = _biased_store(40)
    skill = walk_forward_skill(store, parameters=PARAMS)
    assert skill["n_city_days"] == 20 and skill["skipped_thin_history"] == 20 and skill["skipped_open_ended_truth"] == 0
    assert skill["mae_f"]["calibrated"] < skill["mae_f"]["naive"]
    assert skill["log_score"]["calibrated"] > skill["log_score"]["naive"] and skill["brier"]["calibrated"] < skill["brier"]["naive"]
    assert 0.0 <= skill["top_bucket_hit"]["calibrated"] <= 1.0 and "not the pre-registered test" in skill["note"].lower()
    assert walk_forward_skill(CalibrationStore(), parameters=PARAMS)["n_city_days"] == 0


def test_calibration_cutoff_is_strictly_before_and_shrinks_to_equal_weights_on_thin_history() -> None:
    store = _biased_store(3)
    thin = store.city_calibration("nyc", unit="F", precision="whole", parameters=PARAMS, before=date(2026, 9, 1))
    assert not thin.adequate and thin.n_days == 3
    spread = max(thin.weights.values()) - min(thin.weights.values())
    full = _biased_store(30).city_calibration("nyc", unit="F", precision="whole", parameters=PARAMS, before=date(2026, 9, 1))
    assert spread < max(full.weights.values()) - min(full.weights.values())
    assert store.city_calibration("nyc", unit="F", precision="whole", parameters=PARAMS, before=date(2026, 7, 2)).n_days == 1
    assert store.city_calibration("nyc", unit="F", precision="whole", parameters=PARAMS, before=date(2026, 7, 1)).n_days == 0


def test_store_keeps_live_samples_over_backfill_and_round_trips(tmp_path: Path) -> None:
    store = CalibrationStore()
    assert store.add_sample(_sample("nyc", "2026-09-01", 74, {"gfs_seamless": 76.0, "ecmwf_ifs025": 74.0, "gem_seamless": 75.0}))
    assert store.add_sample(_sample("nyc", "2026-09-01", 74, {"gfs_seamless": 77.0, "ecmwf_ifs025": 74.5, "gem_seamless": 75.0}, source="open_meteo_forecast_live"))
    assert not store.add_sample(_sample("nyc", "2026-09-01", 74, {"gfs_seamless": 70.0, "ecmwf_ifs025": 70.0, "gem_seamless": 70.0}))
    assert store.samples[("nyc", "2026-09-01")].forecasts["gfs_seamless"] == 77.0
    store.add_sample(_sample("london", "2026-09-02", 20, {"gfs_seamless": 21.0, "ecmwf_ifs025": 20.0, "gem_seamless": 22.0}, unit="C"))
    sigma_f, source = store.pooled_naive_sigma(unit="F", parameters=PARAMS, before=date(2026, 9, 10))
    sigma_c, _ = store.pooled_naive_sigma(unit="C", parameters=PARAMS, before=date(2026, 9, 10))
    assert source.startswith("pooled_residual_std") and abs(sigma_f - sigma_c * 1.8) < 1e-9
    assert CalibrationStore().pooled_naive_sigma(unit="C", parameters=PARAMS) == (PARAMS.naive_sigma_default_f / 1.8, "pre_registered_default")
    path = tmp_path / "store.json"
    store.save(path)
    loaded = CalibrationStore.load_or_create(path)
    assert loaded.cities() == ["london", "nyc"] and loaded.samples[("nyc", "2026-09-01")].is_live
    summary = loaded.summary(parameters=PARAMS)
    assert summary["samples"] == 2 and summary["cities_adequate"] == 0 and summary["samples_by_forecast_source"]["open_meteo_forecast_live"] == 1
    with pytest.raises(ValueError):
        CalibrationStore.from_dict({"paper_only": False, "samples": []})


def test_parameters_are_validated() -> None:
    with pytest.raises(ValueError):
        WeatherParameters(edge_threshold=D("0"))
    with pytest.raises(ValueError):
        WeatherParameters(min_settled_per_lane=0)
    with pytest.raises(ValueError):
        WeatherParameters(models=())
    assert WeatherParameters().as_dict()["min_settled_per_lane"] == 50


# --------------------------------------------------------------------------
# Lane strategy and verdict
# --------------------------------------------------------------------------
def _leg(market_id: str = "m1", label: str = "76-77°F") -> WeatherLeg:
    bucket = parse_bucket_label(label)
    assert bucket is not None
    market = Market(venue=Venue.POLYMARKET, market_id=market_id, title=f"Will it be {label}?", metadata={"taker_fee_rate": "0.05", "group_item_title": label})
    return WeatherLeg(market=market, bucket=bucket)


def _book(bid: str, ask: str, size: str = "50") -> OrderBook:
    return OrderBook(market_id="m1", bids=(PriceLevel(D(bid), D(size)),), asks=(PriceLevel(D(ask), D(size)),))


def test_lane_strategy_admits_only_gaps_above_threshold_and_trades_toward_p() -> None:
    naive = naive_ensemble({"a": 77.0, "b": 77.4}, sigma=1.5, sigma_source="test")
    lane = WeatherLaneStrategy("naive", parameters=PARAMS)
    buy = lane.evaluate(_leg(), _book("0.29", "0.31"), 0.45, estimate=naive, context={"city": "nyc"})
    assert buy.traded and buy.side == "buy" and buy.orders[0].price == D("0.31") and buy.orders[0].metadata["lane"] == "naive"
    assert buy.orders[0].metadata["strategy"] == NAIVE_TRACK and buy.orders[0].quantity == D("10")
    sell = lane.evaluate(_leg(), _book("0.29", "0.31"), 0.20, estimate=naive, context={})
    assert sell.traded and sell.side == "sell" and sell.orders[0].price == D("0.29")
    small = lane.evaluate(_leg(), _book("0.29", "0.31"), 0.33, estimate=naive, context={})
    assert not small.traded and small.reason == "gap_below_threshold"
    one_sided = lane.evaluate(_leg(), OrderBook(market_id="m1", asks=(PriceLevel(D("0.31"), D("5")),)), 0.6, estimate=naive, context={})
    assert one_sided.reason == "no_two_sided_mid"
    wide = lane.evaluate(_leg(), _book("0.10", "0.50"), 0.40, estimate=naive, context={})
    assert not wide.traded and wide.reason in ("no_edge", "below_edge_threshold")  # gap 0.10 vs mid, but the touch is at 0.50
    with pytest.raises(ValueError):
        WeatherLaneStrategy("other")


def _settled(lane: str, n: int, ev: str, *, contracts: str = "10") -> list[SettledRecord]:
    return [SettledRecord(lane=lane, cluster=f"c{i % 7}:2026-09-{1 + i % 9:02d}", contracts=D(contracts), net_ev=D(ev) * D(contracts)) for i in range(n)]


def test_ab_verdict_applies_the_pre_registered_rule() -> None:
    assert ab_verdict([]).status == "no_settled"
    under = ab_verdict(_settled("naive", 60, "0.00") + _settled("calibrated", 20, "0.10"))
    assert under.status == "UNDERPOWERED" and "calibrated=20" in under.sample_callout
    passing = ab_verdict(_settled("naive", 50, "0.00") + _settled("calibrated", 50, "0.03"))
    assert passing.status == "PASS" and passing.difference_per_contract == D("0.03")
    assert passing.calibrated.ev_per_contract == D("0.03") and passing.calibrated.hit_rate == D("1")
    assert passing.calibrated.ci95_ev_per_contract is not None
    thin_margin = ab_verdict(_settled("naive", 50, "0.02") + _settled("calibrated", 50, "0.03"))
    assert thin_margin.status == "FAIL"
    below_absolute = ab_verdict(_settled("naive", 50, "-0.05") + _settled("calibrated", 50, "0.01"))
    assert below_absolute.status == "FAIL"
    assert passing.as_dict()["pre_registered"]["min_settled_per_lane"] == 50


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
GAMMA_EVENT = {
    "id": "1009108",
    "slug": "highest-temperature-in-nyc-on-september-14-2026",
    "title": "Highest temperature in NYC on September 14?",
    "endDate": "2026-09-14T12:00:00Z",
    "closed": True,
    "negRisk": True,
    "markets": [
        {
            "id": str(4492068 + i), "conditionId": f"0xcond{i}", "question": f"Will the highest temperature in New York City be {label} on September 14?",
            "groupItemTitle": label, "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]' if label == "76-77°F" else '["0", "1"]',
            "clobTokenIds": f'["yes{i}", "no{i}"]', "closed": True, "active": False, "feeType": "weather_fees", "feesEnabled": True,
            "orderPriceMinTickSize": 0.001, "description": "NOAA LaGuardia",
        }
        for i, label in enumerate(["65°F or below", "66-67°F", "68-69°F", "70-71°F", "72-73°F", "74-75°F", "76-77°F", "78-79°F", "80-81°F", "82-83°F", "84°F or higher"])
    ],
}


def test_event_from_gamma_reads_buckets_fees_and_the_settled_leg() -> None:
    client = PolymarketClient(paper=True, use_fixtures=True)
    event = event_from_gamma(GAMMA_EVENT, registry=REGISTRY, client=client)
    assert event is not None and event.city is not None and event.city.key == "nyc" and event.target_date == date(2026, 9, 14)
    assert len(event.legs) == 11 and event.contiguous and event.closed
    assert event.resolved_bucket is not None and event.resolved_bucket.label == "76-77°F" and event.truth_value() == 76.5
    leg = event.legs[6]
    assert leg.market.metadata["taker_fee_rate"] == "0.05" and leg.market.metadata["category"] == "weather" and leg.market.yes_token_id == "yes6"
    assert event_from_gamma({"title": "Will BTC hit 100k?", "slug": "x", "markets": []}, registry=REGISTRY, client=client) is None


def test_daily_max_takes_the_hourly_maximum_per_local_day_and_model() -> None:
    times = [f"2026-09-15T{h:02d}:00" for h in range(24)] + [f"2026-09-16T{h:02d}:00" for h in range(24)]
    hourly = {
        "time": times,
        "temperature_2m_gfs_seamless": [60 + (h % 24) * 0.5 for h in range(48)],
        "temperature_2m_ecmwf_ifs025": [None] * 24 + [70.0] * 23 + [75.5],
    }
    out = _daily_max(hourly, "temperature_2m", ("gfs_seamless", "ecmwf_ifs025"))
    assert out[date(2026, 9, 15)] == {"gfs_seamless": 71.5}
    assert out[date(2026, 9, 16)] == {"gfs_seamless": 71.5, "ecmwf_ifs025": 75.5}


async def test_open_meteo_source_keeps_paid_keys_off_unless_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPEN_METEO_API_KEY", "secret")
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"host": request.url.host, "apikey": request.url.params.get("apikey", ""), "models": request.url.params.get("models", "")})
        hourly = {"time": [f"2026-09-16T{h:02d}:00" for h in range(24)], "temperature_2m_gfs_seamless": [70.0] * 23 + [78.0], "temperature_2m_ecmwf_ifs025": [72.0] * 24}
        return httpx.Response(200, json={"hourly": hourly})

    city = REGISTRY.resolve("NYC")
    assert city is not None
    free = OpenMeteoSource(models=("gfs_seamless", "ecmwf_ifs025"), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert not free.paid_key_used
    assert await free.day_max(city, date(2026, 9, 16)) == {"gfs_seamless": 78.0, "ecmwf_ifs025": 72.0}
    assert seen[-1]["host"] == "api.open-meteo.com" and seen[-1]["apikey"] == "" and seen[-1]["models"] == "gfs_seamless,ecmwf_ifs025"
    paid = OpenMeteoSource(models=("gfs_seamless",), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), allow_paid_keys=True)
    assert paid.paid_key_used and paid.forecast_host.startswith("https://customer-")
    await paid.previous_day1_history(city, past_days=500)
    assert seen[-1]["host"] == "customer-previous-runs-api.open-meteo.com" and seen[-1]["apikey"] == "secret"
    assert paid.as_dict()["paid_key_used"] is True


# --------------------------------------------------------------------------
# Track: replay, settlement, persistence, backfill
# --------------------------------------------------------------------------
async def test_replay_measures_every_scripted_branch_and_keeps_the_ledgers_consistent() -> None:
    summaries, ledgers, register, store, steps = await replay_fixture()
    naive, cal = summaries["naive"], summaries["calibrated"]
    assert naive.track == NAIVE_TRACK and cal.track == CALIBRATED_TRACK
    assert naive.candidates == cal.candidates == 87
    first = steps[0]["naive"].metrics["event_universe"]["by_reason"]
    assert first == {"city_unknown": 1, "forecast_unavailable": 1, "ladder_not_contiguous": 1, "lead_not_admissible": 1, "priced": 3}
    assert cal.refused_by_reason["calibration_underpowered"] == 11  # London: 5 settled days < 20
    assert naive.refused_by_reason["no_two_sided_mid"] == 1 and "calibration_underpowered" not in naive.refused_by_reason
    assert naive.admitted == naive.paper_fills == 13 and cal.admitted == cal.paper_fills == 9
    # Settlements: NYC (t1), London + Hong Kong (t2); NYC Sep 17 stays pending.
    assert steps[1]["naive"].metrics["settlement"]["resolved"] == 1 and steps[2]["naive"].metrics["settlement"]["resolved"] == 2
    assert cal.metrics["register"]["city_days_pending"] == 1 and cal.metrics["register"]["city_days_resolved"] == 3
    assert store.summary(parameters=PARAMS)["samples_by_forecast_source"] == {"fixture": 70, "fixture_live": 3}
    verdict = cal.metrics["verdict"]
    assert verdict["status"] == "UNDERPOWERED" and verdict["calibrated"]["settled"] == 7 and verdict["naive"]["settled"] == 9
    # Net EV bookkeeping ties to the ledgers: realized = settled net EV - fees still held on open admissions.
    for lane, track in (("naive", NAIVE_TRACK), ("calibrated", CALIBRATED_TRACK)):
        ledger = ledgers[track]
        assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl
        settled_ev = sum(D(a.net_ev or "0") for a in register.settled_admissions(lane))
        open_fees = sum(D(a.fees) for a in register.open_admissions(lane))
        assert abs(ledger.realized_pnl - (settled_ev - open_fees)) < D("0.0005"), lane
        assert ledger.summary()["settlement_fills"] == len(register.settled_admissions(lane))
    # The calibrated lane bought the bucket that settled YES and sold two that settled NO on NYC Sep 15.
    nyc_cal = {a.bucket["label"]: (a.side, a.outcome, D(a.net_ev or "0") > 0) for a in register.settled_admissions("calibrated") if a.city == "nyc"}
    assert nyc_cal["76-77°F"] == ("buy", "yes", True) and nyc_cal["80-81°F"] == ("sell", "no", True)
    assert cal.metrics["status"] == "fixture_synthetic" and "no evidence" in cal.metrics["network_status"]
    assert cal.notes and naive.notes and cal.ledger["paper_only"] is True


async def test_register_round_trips_and_a_resumed_replay_re_admits_nothing(tmp_path: Path) -> None:
    summaries, ledgers, register, store, _ = await replay_fixture()
    register.save(tmp_path / "register.json")
    store.save(tmp_path / "store.json")
    loaded = WeatherRegister.load_or_create(tmp_path / "register.json")
    assert loaded.to_dict()["admissions"] == register.to_dict()["admissions"]
    again, ledgers2, register2, store2, steps = await replay_fixture(ledgers=ledgers, register=loaded, store=CalibrationStore.load_or_create(tmp_path / "store.json"))
    assert again["naive"].admitted == 0 and again["calibrated"].admitted == 0
    assert steps[0]["naive"].metrics["event_universe"]["by_reason"]["city_day_already_resolved"] == 3
    assert again["naive"].refused_by_reason["target_position_reached"] == 4  # the still-open NYC Sep 17 legs
    assert len(register2.settled_admissions()) == len(register.settled_admissions())
    assert ledgers2[NAIVE_TRACK].realized_pnl == ledgers[NAIVE_TRACK].realized_pnl
    with pytest.raises(ValueError):
        WeatherRegister.from_dict({"paper_only": False})


def _event_and_books(label_mid: dict[str, str], *, closed: bool = False, resolved: str | None = None, slug: str = "highest-temperature-in-nyc-on-september-16-2026") -> tuple[WeatherEvent, dict[str, OrderBook]]:
    labels = ["65°F or below"] + [f"{lo}-{lo + 1}°F" for lo in range(66, 84, 2)] + ["84°F or higher"]
    item = {
        "slug": slug, "event_id": "e1", "title": "Highest temperature in NYC on September 16?", "closed": closed,
        "legs": [
            {"market_id": f"m{i}", "label": label, "bids": [[str(D(label_mid.get(label, "0.02")) - D("0.01")), "50"]], "asks": [[str(D(label_mid.get(label, "0.02")) + D("0.01")), "50"]],
             **({"resolved": label == resolved} if closed else {})}
            for i, label in enumerate(labels)
        ],
    }
    return event_from_fixture(item, registry=REGISTRY)


async def test_settlement_handles_ambiguous_and_missing_events_without_touching_positions() -> None:
    event, books = _event_and_books({"76-77°F": "0.30"})
    snapshot = snapshot_from_events([event], books, source="fixture")
    runtimes = create_runtimes(snapshot, ledgers=None, risk_limits=DEFAULT_RISK_LIMITS, starting_cash=D("1000"), model_fees=True)
    register = WeatherRegister()
    store = CalibrationStore()
    register.city_days["nyc:2026-09-16"] = CityDayRecord(
        key="nyc:2026-09-16", event_slug=event.slug, event_id="e1", title=event.title, city="nyc", city_name="NYC", date="2026-09-16", unit="F", precision="whole",
        forecasts={"gfs_seamless": 78.0, "ecmwf_ifs025": 77.0, "icon_seamless": 76.0}, forecast_source="fixture", forecast_fetched_at="x", lead_days=1, legs=[l.market_id for l in event.legs],
    )
    register.city_days["nyc:2026-09-10"] = CityDayRecord(
        key="nyc:2026-09-10", event_slug="gone", event_id="e0", title="t", city="nyc", city_name="NYC", date="2026-09-10", unit="F", precision="whole",
        forecasts={}, forecast_source="fixture", forecast_fetched_at="x", lead_days=1, legs=[],
    )
    ambiguous_item = {"slug": event.slug, "event_id": "e1", "title": event.title, "closed": True, "legs": [{"market_id": f"m{i}", "label": leg.bucket.label, "resolved": False} for i, leg in enumerate(event.legs)]}
    universe = FixtureUniverse(registry=REGISTRY, settlement_items={event.slug: ambiguous_item})
    register.admissions["naive:m6"] = AdmissionRecord(record_id="naive:m6", lane="naive", track=NAIVE_TRACK, market_id="m6", event_slug=event.slug, city="nyc", date="2026-09-16", title="t", bucket={"label": "76-77°F"}, p="0.5", mid="0.3", gap="0.2", side="buy", opened_at="x", quantity="10", entry_yes_price="0.31", fees="0.1", fills=1)
    result = await settle_pending(register, store, runtimes, universe=universe, as_of=datetime(2026, 9, 18, tzinfo=UTC))
    assert result["counts"]["ambiguous"] == 1 and result["counts"]["missing"] == 1 and result["counts"]["admissions_settled"] == 0
    assert register.admissions["naive:m6"].status == "open" and register.city_days["nyc:2026-09-16"].status == "ambiguous"
    assert len(store.samples) == 0
    # A day that has not ended (UTC) is not even looked up.
    register.city_days["nyc:2026-09-16"].status = "pending"
    early = await settle_pending(register, store, runtimes, universe=universe, as_of=datetime(2026, 9, 16, 12, tzinfo=UTC))
    assert early["counts"]["checked"] == 0 and early["counts"]["still_pending"] == 1


async def test_cycle_refuses_unknown_city_and_bad_lead_and_prices_tomorrow() -> None:
    event, books = _event_and_books({"76-77°F": "0.30", "78-79°F": "0.30"})
    snapshot = snapshot_from_events([event], books, source="fixture")
    runtimes = create_runtimes(snapshot, ledgers=None, risk_limits=DEFAULT_RISK_LIMITS, starting_cash=D("1000"), model_fees=True)
    register, store = WeatherRegister(), CalibrationStore()
    forecasts = StaticForecastSource({"nyc:2026-09-16": {"gfs_seamless": 80.0, "ecmwf_ifs025": 77.2, "icon_seamless": 75.0}})
    universe = FixtureUniverse(registry=REGISTRY)
    # 20:00 EDT on Sep 15 -> tomorrow is Sep 16 -> admissible; at Sep 16 10:00 EDT the same event is lead 0.
    summaries = await run_weather_cycle(runtimes, register, store, events=[event], universe=universe, forecasts=forecasts, as_of=datetime(2026, 9, 16, 0, 0, tzinfo=UTC), mode="network", settle=False)
    assert summaries["naive"].metrics["event_universe"]["by_reason"] == {"priced": 1}
    assert summaries["calibrated"].refused_by_reason == {"calibration_underpowered": 11}
    assert summaries["naive"].admitted >= 1 and "nyc:2026-09-16" in register.city_days
    assert register.city_days["nyc:2026-09-16"].forecasts == {"gfs_seamless": 80.0, "ecmwf_ifs025": 77.2, "icon_seamless": 75.0}
    assert summaries["naive"].metrics["status"] == "measured_no_settled"
    runtimes2 = create_runtimes(snapshot, ledgers=None, risk_limits=DEFAULT_RISK_LIMITS, starting_cash=D("1000"), model_fees=True)
    late = await run_weather_cycle(runtimes2, WeatherRegister(), store, events=[event], universe=universe, forecasts=forecasts, as_of=datetime(2026, 9, 16, 14, 0, tzinfo=UTC), mode="network", settle=False)
    assert late["naive"].metrics["event_universe"]["by_reason"] == {"lead_not_admissible": 1} and late["naive"].admitted == 0


async def test_backfill_joins_settled_buckets_with_archived_day1_forecasts() -> None:
    closed, _ = _event_and_books({}, closed=True, resolved="76-77°F")
    universe = FixtureUniverse(registry=REGISTRY, closed_items=[
        {"slug": closed.slug, "event_id": "e1", "title": closed.title, "closed": True, "legs": [{"market_id": f"m{i}", "label": l.bucket.label, "resolved": l.bucket.label == "76-77°F"} for i, l in enumerate(closed.legs)]},
        {"slug": "highest-temperature-in-atlantis-on-september-16-2026", "event_id": "e2", "title": "Highest temperature in Atlantis on September 16?", "closed": True, "legs": [{"market_id": "a1", "label": "20°C or below", "resolved": True}, {"market_id": "a2", "label": "21°C or higher", "resolved": False}]},
    ])
    forecasts = StaticForecastSource({}, history={"nyc:2026-09-16": {"gfs_seamless": 79.0, "ecmwf_ifs025": 77.0, "icon_seamless": 76.0}})
    store = CalibrationStore()
    result = await backfill_calibration(store, universe=universe, forecasts=forecasts, parameters=PARAMS, start=date(2026, 9, 10), end=date(2026, 9, 16), forecast_source_name="fixture")
    assert result["counts"]["closed_events"] == 2 and result["counts"]["city_unknown"] == 1 and result["counts"]["samples_added"] == 1
    sample = store.samples[("nyc", "2026-09-16")]
    assert sample.truth_value == 76.5 and sample.truth_lo == 76 and sample.forecasts["gfs_seamless"] == 79.0
    again = await backfill_calibration(store, universe=universe, forecasts=forecasts, parameters=PARAMS, start=date(2026, 9, 10), end=date(2026, 9, 16), forecast_source_name="fixture")
    assert again["counts"]["samples_added"] == 0 and again["counts"]["already_present"] == 1


def test_replay_fixture_is_labelled_synthetic() -> None:
    replay = load_replay()
    assert "SYNTHETIC" in replay.note and "no evidence" in replay.note
    assert len(replay.history) == 70 and len(replay.steps) == 3


async def test_scoreboard_artifact_carries_both_lanes_and_slims_detail() -> None:
    summaries, _, _, _, _ = await replay_fixture()
    artifact = build_scoreboard_artifact(
        [summaries["naive"], summaries["calibrated"]], mode="fixtures", measured_at="t", limit=1, primary_track=CALIBRATED_TRACK,
        venues=("polymarket",), venue_focus="polymarket", track_family="weather_calibration",
    )
    assert artifact["meta"]["track_family"] == "weather_calibration" and artifact["meta"]["pnl_source"] == "core.ledger.PaperLedger"
    tracks = {t["track"]: t for t in artifact["tracks"]}
    assert set(tracks) == {NAIVE_TRACK, CALIBRATED_TRACK}
    metrics = tracks[CALIBRATED_TRACK]["metrics"]
    assert metrics["measurements"]["detail"] == "weather_calibration_latest.json" and isinstance(metrics["calibration_detail"], dict)
    assert metrics["verdict"]["status"] == "UNDERPOWERED"
    assert artifact["portfolio"]["by_track"][CALIBRATED_TRACK]["realized_pnl"] == summaries["calibrated"].ledger["realized_pnl"]


def test_cli_fixture_run_writes_report_register_store_and_ledgers(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apps.measure_weather_calibration", "--artifact-dir", str(tmp_path)],
        check=False, capture_output=True, text=True,
        env={**os.environ, "TRADING_MODE": "paper", "ENABLE_LIVE_TRADING": "false"},
    )
    assert result.returncode == 0, result.stderr
    assert "status=fixture_synthetic" in result.stdout and "verdict: UNDERPOWERED" in result.stdout and "paid_key_used=False" in result.stdout
    report = json.loads((tmp_path / "weather_calibration_latest.json").read_text())
    assert report["paper_only"] is True and report["kind"] == "weather_calibration_report"
    assert report["verdict"]["status"] == "UNDERPOWERED" and report["lanes"]["calibrated"]["settled_admissions"] == 7
    assert report["experiment"]["pre_registered"]["min_settled_per_lane"] == 50 and report["not_validated"]
    assert report["calibration"]["cities_adequate"] == 2 and len(report["calibration"]["per_city"]) == 3
    assert (tmp_path / "weather_calibration" / "register.json").exists() and (tmp_path / "weather_calibration" / "calibration_store.json").exists()
    assert (tmp_path / "paper" / f"ledger_{NAIVE_TRACK}.json").exists() and (tmp_path / "paper" / f"ledger_{CALIBRATED_TRACK}.json").exists()
    board = json.loads((tmp_path / "scoreboard_weather_calibration.json").read_text())
    assert board["meta"]["source"] == "measured" and board["meta"]["track_family"] == "weather_calibration"
    assert not (tmp_path / "scoreboard_latest.json").exists()


def test_cli_refuses_live_flags(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apps.measure_weather_calibration", "--artifact-dir", str(tmp_path)],
        check=False, capture_output=True, text=True,
        env={**os.environ, "TRADING_MODE": "live", "ENABLE_LIVE_TRADING": "true"},
    )
    assert result.returncode != 0
    assert not (tmp_path / "weather_calibration_latest.json").exists()


def test_snapshot_runtime_wiring_uses_the_weather_fee_rate() -> None:
    event, books = _event_and_books({"76-77°F": "0.30"})
    snapshot = snapshot_from_events([event], books, source="fixture")
    runtimes = create_runtimes(snapshot, ledgers=None, risk_limits=DEFAULT_RISK_LIMITS, starting_cash=D("1000"), model_fees=True)
    assert set(runtimes) == {"naive", "calibrated"} and all(isinstance(rt, TrackRuntime) for rt in runtimes.values())
    client = runtimes["naive"].clients[Venue.POLYMARKET]
    market = event.legs[6].market
    assert client._fee_schedule_for(market)(D("10"), D("0.31")) == D("0.10695")
    assert isinstance(snapshot, VenueSnapshot) and len(snapshot.markets) == 11
