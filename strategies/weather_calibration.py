"""Per-city NWP model calibration for the Polymarket daily-temperature markets.

Pre-registered experiment (see ``docs/WEATHER_CALIBRATION.md``)::

    sample            one settled city-day: the free per-model day-ahead maximum-
                      temperature forecasts and the bucket Polymarket settled on
    truth             midpoint of the settled bucket (open ends: one width past
                      the edge), in the resolution source's precision
    naive ensemble    equal-weight mean of the raw forecasts, sigma pooled over
                      every city (a constant when there is no history)
    calibrated        per city x model: bias = mean(forecast - truth), variance
                      of the de-biased error, de-biased bucket hit rate; weights
                      w_m ∝ (hit_m + 0.1) / var_m shrunk toward equal weights by
                      k pseudo-samples; mean = Σ w_m (f_m - bias_m); sigma = the
                      city's residual std (floored)
    p(bucket)         Φ((upper - μ) / σ) - Φ((lower - μ) / σ) over the ladder
    admission         |p - market mid| >= edge_threshold (0.05) *and* the fair-
                      value engine finds a positive cost-adjusted touch edge;
                      the calibrated lane also needs >= min_calibration_samples
                      settled days for the city
    PASS              both lanes have >= min_settled_per_lane settled admissions,
                      calibrated net EV/contract - naive net EV/contract >=
                      margin_vs_naive (0.02) and calibrated net EV/contract >=
                      absolute_min_ev (0.025); otherwise FAIL, or UNDERPOWERED
                      while either lane is below the sample floor

All probabilities are YES probabilities of the leg's bucket. Nothing here
reads a paid API; the model list is Open-Meteo's free public set.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from core.portfolio import Portfolio
from core.risk import RiskManager
from core.types import ONE, ZERO, Market, Order, OrderBook, Venue
from strategies.edge import CalibratedFairValueStrategy, FairValueEvaluation, FairValueParameters
from strategies.weather_types import Precision, TemperatureBucket, TemperatureUnit, WeatherLeg

NAIVE_TRACK = "weather_naive_ensemble"
CALIBRATED_TRACK = "weather_calibrated_ensemble"
WEATHER_TRACKS: tuple[str, ...] = (NAIVE_TRACK, CALIBRATED_TRACK)
LANES = {"naive": NAIVE_TRACK, "calibrated": CALIBRATED_TRACK}
# Open-Meteo's free global models; the "seamless" variants splice in regional
# high-resolution runs where the provider has them. KNMI/DMI are omitted: they
# fall back to ECMWF outside Europe and would double-count it in the naive mean.
DEFAULT_MODELS: tuple[str, ...] = (
    "gfs_seamless",
    "ecmwf_ifs025",
    "icon_seamless",
    "gem_seamless",
    "meteofrance_seamless",
    "ukmo_seamless",
    "jma_seamless",
)
STORE_SCHEMA = "1.0.0"
Q4 = Decimal("0.0001")
F_PER_C = 1.8


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4, rounding=ROUND_HALF_EVEN) if value is not None else None


def _now() -> datetime:
    return datetime.now(UTC)


def _r(value: float | None, places: int = 3) -> float | None:
    return round(value, places) if value is not None else None


# --------------------------------------------------------------------------
# Parameters (pre-registered defaults)
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WeatherParameters:
    edge_threshold: Decimal = Decimal("0.05")
    minimum_edge: Decimal = Decimal("0.02")
    fee_buffer_per_contract: Decimal = Decimal("0.01")
    maximum_order_size: Decimal = Decimal("10")
    min_calibration_samples: int = 20
    min_settled_per_lane: int = 50
    margin_vs_naive: Decimal = Decimal("0.02")
    absolute_min_ev: Decimal = Decimal("0.025")
    admissible_lead_days: tuple[int, ...] = (1,)
    models: tuple[str, ...] = DEFAULT_MODELS
    naive_sigma_default_f: float = 3.0
    sigma_floor_f: float = 1.0
    shrinkage_pseudo_samples: int = 10
    hit_rate_offset: float = 0.1
    max_calibration_age_days: int = 120
    min_models_per_sample: int = 3

    def __post_init__(self) -> None:
        if not ZERO < self.edge_threshold < ONE:
            raise ValueError("edge_threshold must be within (0, 1)")
        if self.minimum_edge < ZERO or self.fee_buffer_per_contract < ZERO:
            raise ValueError("edge thresholds must not be negative")
        if self.maximum_order_size <= ZERO:
            raise ValueError("maximum_order_size must be positive")
        if self.min_calibration_samples < 1 or self.min_settled_per_lane < 1:
            raise ValueError("sample floors must be at least 1")
        if self.margin_vs_naive < ZERO or self.absolute_min_ev < ZERO:
            raise ValueError("pass margins must not be negative")
        if not self.admissible_lead_days or not self.models:
            raise ValueError("need at least one admissible lead day and one model")
        if self.naive_sigma_default_f <= 0 or self.sigma_floor_f <= 0:
            raise ValueError("sigmas must be positive")
        if self.shrinkage_pseudo_samples < 0 or self.hit_rate_offset < 0:
            raise ValueError("shrinkage and hit-rate offset must not be negative")
        if self.max_calibration_age_days < 1 or self.min_models_per_sample < 1:
            raise ValueError("window and model floor must be at least 1")

    def naive_sigma_default(self, unit: TemperatureUnit) -> float:
        return self.naive_sigma_default_f if unit == "F" else self.naive_sigma_default_f / F_PER_C

    def sigma_floor(self, unit: TemperatureUnit) -> float:
        return self.sigma_floor_f if unit == "F" else self.sigma_floor_f / F_PER_C

    def as_dict(self) -> dict[str, Any]:
        return {
            "edge_threshold": self.edge_threshold,
            "minimum_edge": self.minimum_edge,
            "fee_buffer_per_contract": self.fee_buffer_per_contract,
            "maximum_order_size": self.maximum_order_size,
            "min_calibration_samples": self.min_calibration_samples,
            "min_settled_per_lane": self.min_settled_per_lane,
            "margin_vs_naive": self.margin_vs_naive,
            "absolute_min_ev": self.absolute_min_ev,
            "admissible_lead_days": list(self.admissible_lead_days),
            "models": list(self.models),
            "naive_sigma_default_f": self.naive_sigma_default_f,
            "sigma_floor_f": self.sigma_floor_f,
            "shrinkage_pseudo_samples": self.shrinkage_pseudo_samples,
            "hit_rate_offset": self.hit_rate_offset,
            "max_calibration_age_days": self.max_calibration_age_days,
            "min_models_per_sample": self.min_models_per_sample,
        }


# --------------------------------------------------------------------------
# Normal bucket probabilities
# --------------------------------------------------------------------------
def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_probability(mean: float, sigma: float, bucket: TemperatureBucket, precision: Precision = "whole") -> float:
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    lower, upper = bucket.edges(precision)
    hi = 1.0 if math.isinf(upper) else normal_cdf((upper - mean) / sigma)
    lo = 0.0 if math.isinf(lower) else normal_cdf((lower - mean) / sigma)
    return max(0.0, min(1.0, hi - lo))


def ladder_probabilities(mean: float, sigma: float, buckets: list[TemperatureBucket], precision: Precision = "whole") -> dict[str, float]:
    return {b.label: bucket_probability(mean, sigma, b, precision) for b in buckets}


# --------------------------------------------------------------------------
# Calibration samples and store
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CalibrationSample:
    city: str
    date: str  # ISO date of the observation day (city-local)
    unit: TemperatureUnit
    precision: Precision
    truth_lo: int | None
    truth_hi: int | None
    truth_value: float
    forecasts: dict[str, float]
    forecast_source: str  # open_meteo_previous_runs_day1 | open_meteo_forecast_live | fixture
    truth_source: str  # polymarket_resolution | fixture
    event_slug: str | None = None
    recorded_at: str = field(default_factory=lambda: _now().isoformat())

    @property
    def key(self) -> tuple[str, str]:
        return self.city, self.date

    @property
    def day(self) -> date:
        return date.fromisoformat(self.date)

    @property
    def truth_bucket(self) -> TemperatureBucket:
        return TemperatureBucket(self.truth_lo, self.truth_hi, self.unit)

    @property
    def is_live(self) -> bool:
        return "live" in self.forecast_source

    def as_dict(self) -> dict[str, Any]:
        return {
            "city": self.city,
            "date": self.date,
            "unit": self.unit,
            "precision": self.precision,
            "truth_lo": self.truth_lo,
            "truth_hi": self.truth_hi,
            "truth_value": self.truth_value,
            "forecasts": dict(sorted(self.forecasts.items())),
            "forecast_source": self.forecast_source,
            "truth_source": self.truth_source,
            "event_slug": self.event_slug,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> CalibrationSample:
        return cls(
            city=str(item["city"]),
            date=str(item["date"]),
            unit="F" if str(item.get("unit", "C")).upper() == "F" else "C",
            precision="tenth" if item.get("precision") == "tenth" else "whole",
            truth_lo=item.get("truth_lo"),
            truth_hi=item.get("truth_hi"),
            truth_value=float(item["truth_value"]),
            forecasts={str(k): float(v) for k, v in (item.get("forecasts") or {}).items() if v is not None},
            forecast_source=str(item.get("forecast_source", "unknown")),
            truth_source=str(item.get("truth_source", "unknown")),
            event_slug=item.get("event_slug"),
            recorded_at=str(item.get("recorded_at") or _now().isoformat()),
        )


@dataclass(frozen=True, slots=True)
class ModelStats:
    model: str
    n: int
    bias: float
    mae: float
    rmse: float
    variance: float  # of the de-biased error
    hit_rate: float  # de-biased forecast lands in the settled bucket
    raw_hit_rate: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "n": self.n,
            "bias": _r(self.bias),
            "mae": _r(self.mae),
            "rmse": _r(self.rmse),
            "variance": _r(self.variance),
            "hit_rate": _r(self.hit_rate, 4),
            "raw_hit_rate": _r(self.raw_hit_rate, 4),
        }


@dataclass(frozen=True, slots=True)
class CityCalibration:
    city: str
    unit: TemperatureUnit
    precision: Precision
    n_days: int
    models: dict[str, ModelStats]
    weights: dict[str, float]
    sigma: float
    sigma_raw: float | None
    adequate: bool
    window: tuple[str, str] | None

    @property
    def best_model(self) -> str | None:
        if not self.models:
            return None
        return min(self.models.values(), key=lambda s: s.mae).model

    def as_dict(self) -> dict[str, Any]:
        return {
            "city": self.city,
            "unit": self.unit,
            "n_days": self.n_days,
            "adequate": self.adequate,
            "window": list(self.window) if self.window else None,
            "sigma": _r(self.sigma),
            "sigma_raw": _r(self.sigma_raw),
            "best_model": self.best_model,
            "weights": {k: _r(v, 4) for k, v in sorted(self.weights.items())},
            "models": {k: v.as_dict() for k, v in sorted(self.models.items())},
        }


@dataclass(frozen=True, slots=True)
class EnsembleEstimate:
    method: str  # naive_equal_weight | calibrated_weighted
    mean: float
    sigma: float
    models_used: tuple[str, ...]
    n_calibration_days: int
    sigma_source: str
    adequate: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "mean": _r(self.mean),
            "sigma": _r(self.sigma),
            "models_used": list(self.models_used),
            "n_calibration_days": self.n_calibration_days,
            "sigma_source": self.sigma_source,
            "adequate": self.adequate,
        }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _pstd(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def naive_ensemble(forecasts: dict[str, float], *, sigma: float, sigma_source: str) -> EnsembleEstimate:
    if not forecasts:
        raise ValueError("no forecasts")
    return EnsembleEstimate(
        method="naive_equal_weight",
        mean=_mean(list(forecasts.values())),
        sigma=sigma,
        models_used=tuple(sorted(forecasts)),
        n_calibration_days=0,
        sigma_source=sigma_source,
    )


def calibrated_ensemble(calibration: CityCalibration, forecasts: dict[str, float]) -> EnsembleEstimate:
    usable = {m: v for m, v in forecasts.items() if m in calibration.weights and m in calibration.models}
    if not usable:
        raise ValueError("no forecasts from calibrated models")
    total = sum(calibration.weights[m] for m in usable)
    mean = sum(calibration.weights[m] / total * (v - calibration.models[m].bias) for m, v in usable.items())
    return EnsembleEstimate(
        method="calibrated_weighted",
        mean=mean,
        sigma=calibration.sigma,
        models_used=tuple(sorted(usable)),
        n_calibration_days=calibration.n_days,
        sigma_source="city_residual_std" if calibration.sigma_raw is not None else "floor",
        adequate=calibration.adequate,
    )


class CalibrationStore:
    """Settled city-day samples plus the per-city x model statistics derived from them.

    Statistics are computed on demand from samples strictly *before* a cutoff
    date, so a caller pricing day ``D`` never sees ``D``'s own settlement.
    """

    def __init__(self, samples: list[CalibrationSample] | None = None, *, created_at: str | None = None) -> None:
        self.samples: dict[tuple[str, str], CalibrationSample] = {}
        self.created_at = created_at or _now().isoformat()
        self.updated_at = self.created_at
        for sample in samples or []:
            self.add_sample(sample)

    # ------------------------------------------------------------- samples
    def add_sample(self, sample: CalibrationSample) -> bool:
        """Insert; a live-lead sample is never replaced by an archive backfill."""
        existing = self.samples.get(sample.key)
        if existing is not None and existing.is_live and not sample.is_live:
            return False
        self.samples[sample.key] = sample
        self.updated_at = _now().isoformat()
        return True

    def has(self, city: str, day: date | str) -> bool:
        return (city, day if isinstance(day, str) else day.isoformat()) in self.samples

    def city_samples(self, city: str, *, before: date | None = None, max_age_days: int | None = None, min_models: int = 1) -> list[CalibrationSample]:
        out = []
        for sample in self.samples.values():
            if sample.city != city or len(sample.forecasts) < min_models:
                continue
            if before is not None:
                if sample.day >= before:
                    continue
                if max_age_days is not None and (before - sample.day).days > max_age_days:
                    continue
            out.append(sample)
        return sorted(out, key=lambda s: s.date)

    def cities(self) -> list[str]:
        return sorted({s.city for s in self.samples.values()})

    # ------------------------------------------------------------- statistics
    @staticmethod
    def model_stats(samples: list[CalibrationSample], model: str) -> ModelStats | None:
        rows = [(s.forecasts[model], s) for s in samples if model in s.forecasts]
        if not rows:
            return None
        errors = [f - s.truth_value for f, s in rows]
        bias = _mean(errors)
        debiased = [e - bias for e in errors]
        raw_hits = sum(1 for f, s in rows if s.truth_bucket.contains(f, s.precision))
        hits = sum(1 for f, s in rows if s.truth_bucket.contains(f - bias, s.precision))
        return ModelStats(
            model=model,
            n=len(rows),
            bias=bias,
            mae=_mean([abs(e) for e in errors]),
            rmse=math.sqrt(_mean([e * e for e in errors])),
            variance=_mean([d * d for d in debiased]),
            hit_rate=hits / len(rows),
            raw_hit_rate=raw_hits / len(rows),
        )

    def city_calibration(self, city: str, *, unit: TemperatureUnit, precision: Precision, parameters: WeatherParameters, before: date | None = None) -> CityCalibration:
        params = parameters
        samples = self.city_samples(city, before=before, max_age_days=params.max_calibration_age_days, min_models=params.min_models_per_sample)
        stats: dict[str, ModelStats] = {}
        for model in params.models:
            s = self.model_stats(samples, model)
            if s is not None and s.n >= 2:
                stats[model] = s
        n_days = len(samples)
        weights = _weights(stats, params)
        sigma_raw: float | None = None
        if stats and samples:
            residuals = []
            for sample in samples:
                usable = {m: v for m, v in sample.forecasts.items() if m in weights}
                if not usable:
                    continue
                total = sum(weights[m] for m in usable)
                mean = sum(weights[m] / total * (v - stats[m].bias) for m, v in usable.items())
                residuals.append(sample.truth_value - mean)
            if len(residuals) >= 2:
                sigma_raw = _pstd(residuals)
        floor = params.sigma_floor(unit)
        sigma = max(floor, sigma_raw) if sigma_raw is not None else max(floor, params.naive_sigma_default(unit))
        return CityCalibration(
            city=city,
            unit=unit,
            precision=precision,
            n_days=n_days,
            models=stats,
            weights=weights,
            sigma=sigma,
            sigma_raw=sigma_raw,
            adequate=n_days >= params.min_calibration_samples and bool(stats),
            window=(samples[0].date, samples[-1].date) if samples else None,
        )

    def pooled_naive_sigma(self, *, unit: TemperatureUnit, parameters: WeatherParameters, before: date | None = None) -> tuple[float, str]:
        """Std of ``truth - equal-weight mean`` over every city (pooled in °F), in ``unit``."""
        residuals_f: list[float] = []
        for city in self.cities():
            for sample in self.city_samples(city, before=before, max_age_days=parameters.max_calibration_age_days, min_models=parameters.min_models_per_sample):
                usable = [v for m, v in sample.forecasts.items() if m in parameters.models]
                if not usable:
                    continue
                r = sample.truth_value - _mean(usable)
                residuals_f.append(r * (F_PER_C if sample.unit == "C" else 1.0))
        if len(residuals_f) < 2:
            return parameters.naive_sigma_default(unit), "pre_registered_default"
        sigma_f = max(parameters.sigma_floor_f, _pstd(residuals_f))
        return (sigma_f if unit == "F" else sigma_f / F_PER_C), f"pooled_residual_std(n={len(residuals_f)})"

    # ------------------------------------------------------------- reporting
    def summary(self, *, parameters: WeatherParameters, cities: dict[str, tuple[TemperatureUnit, Precision]] | None = None, before: date | None = None) -> dict[str, Any]:
        per_city: list[dict[str, Any]] = []
        units = cities or {}
        for city in self.cities():
            samples = self.city_samples(city)
            unit, precision = units.get(city, (samples[0].unit, samples[0].precision))
            cal = self.city_calibration(city, unit=unit, precision=precision, parameters=parameters, before=before)
            per_city.append(cal.as_dict())
        adequate = [c for c in per_city if c["adequate"]]
        best_models: dict[str, int] = {}
        for c in adequate:
            if c["best_model"]:
                best_models[c["best_model"]] = best_models.get(c["best_model"], 0) + 1
        return {
            "samples": len(self.samples),
            "cities": len(per_city),
            "cities_adequate": len(adequate),
            "min_calibration_samples": parameters.min_calibration_samples,
            "samples_by_forecast_source": _count(s.forecast_source for s in self.samples.values()),
            "samples_by_truth_source": _count(s.truth_source for s in self.samples.values()),
            "date_range": [min(s.date for s in self.samples.values()), max(s.date for s in self.samples.values())] if self.samples else None,
            "best_model_by_city_count": dict(sorted(best_models.items())),
            "distinct_best_models_among_adequate_cities": len(best_models),
            "per_city": per_city,
        }

    # ------------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STORE_SCHEMA,
            "paper_only": True,
            "kind": "weather_calibration_store",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "samples": [self.samples[k].as_dict() for k in sorted(self.samples)],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CalibrationStore:
        if payload.get("paper_only") is not True:
            raise ValueError("refusing to load a calibration store that is not marked paper_only")
        store = cls(created_at=payload.get("created_at"))
        for item in payload.get("samples", []):
            store.samples[(str(item["city"]), str(item["date"]))] = CalibrationSample.from_dict(item)
        store.updated_at = payload.get("updated_at", store.updated_at)
        return store

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    def load_or_create(cls, path: Path) -> CalibrationStore:
        if path.exists():
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls()


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items()))


def _weights(stats: dict[str, ModelStats], params: WeatherParameters) -> dict[str, float]:
    """Inverse de-biased variance x hit rate, shrunk toward equal weights."""
    if not stats:
        return {}
    eps = 1e-6
    raw = {m: (s.hit_rate + params.hit_rate_offset) / max(s.variance, eps) for m, s in stats.items()}
    total = sum(raw.values())
    perf = {m: v / total for m, v in raw.items()} if total > 0 else {m: 1.0 / len(stats) for m in stats}
    equal = 1.0 / len(stats)
    n = min(s.n for s in stats.values())
    k = params.shrinkage_pseudo_samples
    lam = n / (n + k) if (n + k) > 0 else 1.0
    return {m: lam * perf[m] + (1.0 - lam) * equal for m in stats}


# --------------------------------------------------------------------------
# Lane strategy: p vs. mid through the fair-value engine
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LaneEvaluation:
    lane: str
    reason: str
    probability: Decimal
    mid: Decimal | None
    gap: Decimal | None
    side: str | None
    fair_value: FairValueEvaluation | None = None
    orders: tuple[Order, ...] = ()

    @property
    def traded(self) -> bool:
        return bool(self.orders)

    @property
    def cost_adjusted_edge(self) -> Decimal | None:
        return self.fair_value.cost_adjusted_edge if self.fair_value else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "reason": self.reason,
            "p": _q(self.probability),
            "mid": _q(self.mid),
            "gap": _q(self.gap),
            "side": self.side,
            "touch_price": _q(self.fair_value.touch.price) if self.fair_value and self.fair_value.touch else None,
            "raw_edge": _q(self.fair_value.raw_edge) if self.fair_value else None,
            "cost_adjusted_edge": _q(self.cost_adjusted_edge),
            "quantity": self.fair_value.quantity if self.fair_value else ZERO,
            "orders": len(self.orders),
        }


class WeatherLaneStrategy:
    """One lane (naive or calibrated): compares its bucket probability with the mid."""

    def __init__(self, lane: str, *, parameters: WeatherParameters | None = None, portfolio: Portfolio | None = None, risk: RiskManager | None = None) -> None:
        if lane not in LANES:
            raise ValueError(f"unknown lane {lane!r}")
        self.lane = lane
        self.name = LANES[lane]
        self.parameters = parameters or WeatherParameters()
        self.venue_parameters = {
            Venue.POLYMARKET: FairValueParameters(
                minimum_edge=self.parameters.minimum_edge,
                fee_buffer_per_contract=self.parameters.fee_buffer_per_contract,
                maximum_order_size=self.parameters.maximum_order_size,
            )
        }
        self.portfolio = portfolio
        self.risk = risk

    def evaluate(self, leg: WeatherLeg, book: OrderBook, probability: float, *, estimate: EnsembleEstimate, context: dict[str, str]) -> LaneEvaluation:
        market: Market = leg.market
        p = Decimal(str(round(probability, 6)))
        p = min(max(p, ZERO), ONE)
        mid = book.mid_price
        if mid is None:
            return LaneEvaluation(self.lane, "no_two_sided_mid", p, None, None, None)
        gap = p - mid
        side = "buy" if gap > ZERO else ("sell" if gap < ZERO else None)
        if abs(gap) < self.parameters.edge_threshold:
            return LaneEvaluation(self.lane, "gap_below_threshold", p, mid, gap, side)
        engine = CalibratedFairValueStrategy(
            {market.market_id: p}, venue_parameters=self.venue_parameters, portfolio=self.portfolio, risk=self.risk
        )
        fair = engine.evaluate(market, book)
        if fair.side is not None and fair.side.value != side:
            return LaneEvaluation(self.lane, "touch_disagrees_with_mid", p, mid, gap, side, fair)
        orders = tuple(
            replace(
                order,
                metadata={
                    **order.metadata,
                    "strategy": self.name,
                    "lane": self.lane,
                    "price_signal_status": "free_nwp_ensemble_" + estimate.method,
                    "ensemble_mean": str(_r(estimate.mean)),
                    "ensemble_sigma": str(_r(estimate.sigma)),
                    "n_calibration_days": str(estimate.n_calibration_days),
                    **context,
                },
            )
            for order in fair.orders
        )
        return LaneEvaluation(self.lane, fair.reason, p, mid, gap, side, fair, orders)


# --------------------------------------------------------------------------
# Pre-registered A/B verdict on settled paper admissions
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SettledRecord:
    lane: str
    cluster: str  # city:date, the bootstrap resampling unit
    contracts: Decimal
    net_ev: Decimal  # signed_qty * (settle - entry) - fees, in USD


@dataclass(frozen=True, slots=True)
class LaneOutcome:
    lane: str
    settled: int
    contracts: Decimal
    net_ev: Decimal
    ev_per_contract: Decimal | None
    hit_rate: Decimal | None
    ci95_ev_per_contract: tuple[Decimal, Decimal] | None
    clusters: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "settled": self.settled,
            "contracts": self.contracts,
            "net_ev": _q(self.net_ev),
            "ev_per_contract": _q(self.ev_per_contract),
            "hit_rate": _q(self.hit_rate),
            "ci95_ev_per_contract": [_q(self.ci95_ev_per_contract[0]), _q(self.ci95_ev_per_contract[1])] if self.ci95_ev_per_contract else None,
            "city_days": self.clusters,
        }


def lane_outcome(lane: str, records: list[SettledRecord], *, resamples: int = 2000, seed: int = 0) -> LaneOutcome:
    rows = [r for r in records if r.lane == lane and r.contracts > ZERO]
    contracts = sum((r.contracts for r in rows), ZERO)
    net = sum((r.net_ev for r in rows), ZERO)
    clusters: dict[str, list[SettledRecord]] = {}
    for r in rows:
        clusters.setdefault(r.cluster, []).append(r)
    ci: tuple[Decimal, Decimal] | None = None
    if len(clusters) >= 2 and contracts > ZERO:
        rng = random.Random(seed)
        keys = list(clusters)
        draws: list[float] = []
        for _ in range(resamples):
            picked = [clusters[rng.choice(keys)] for _ in keys]
            c = sum(float(r.contracts) for group in picked for r in group)
            e = sum(float(r.net_ev) for group in picked for r in group)
            if c > 0:
                draws.append(e / c)
        if draws:
            draws.sort()
            lo = draws[int(0.025 * (len(draws) - 1))]
            hi = draws[int(0.975 * (len(draws) - 1))]
            ci = (Decimal(str(round(lo, 4))), Decimal(str(round(hi, 4))))
    return LaneOutcome(
        lane=lane,
        settled=len(rows),
        contracts=contracts,
        net_ev=net,
        ev_per_contract=(net / contracts) if contracts > ZERO else None,
        hit_rate=(Decimal(sum(1 for r in rows if r.net_ev > ZERO)) / Decimal(len(rows))) if rows else None,
        ci95_ev_per_contract=ci,
        clusters=len(clusters),
    )


@dataclass(frozen=True, slots=True)
class ABVerdict:
    status: str  # PASS | FAIL | UNDERPOWERED | no_settled
    naive: LaneOutcome
    calibrated: LaneOutcome
    difference_per_contract: Decimal | None
    pre_registered: dict[str, Any]
    sample_callout: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "naive": self.naive.as_dict(),
            "calibrated": self.calibrated.as_dict(),
            "difference_per_contract": _q(self.difference_per_contract),
            "pre_registered": self.pre_registered,
            "sample_callout": self.sample_callout,
        }


def ab_verdict(records: list[SettledRecord], *, parameters: WeatherParameters | None = None) -> ABVerdict:
    params = parameters or WeatherParameters()
    naive = lane_outcome("naive", records)
    cal = lane_outcome("calibrated", records)
    diff = (cal.ev_per_contract - naive.ev_per_contract) if (cal.ev_per_contract is not None and naive.ev_per_contract is not None) else None
    floor = params.min_settled_per_lane
    if naive.settled == 0 and cal.settled == 0:
        status = "no_settled"
    elif naive.settled < floor or cal.settled < floor:
        status = "UNDERPOWERED"
    elif diff is not None and diff >= params.margin_vs_naive and cal.ev_per_contract is not None and cal.ev_per_contract >= params.absolute_min_ev:
        status = "PASS"
    else:
        status = "FAIL"
    callout = (
        f"settled admissions: naive={naive.settled} ({naive.clusters} city-days), calibrated={cal.settled} "
        f"({cal.clusters} city-days); pre-registered floor {floor} per lane"
    )
    return ABVerdict(
        status=status,
        naive=naive,
        calibrated=cal,
        difference_per_contract=diff,
        pre_registered={
            "rule": (
                "PASS when both lanes have >= min_settled_per_lane settled admissions, calibrated net EV per "
                "contract - naive net EV per contract >= margin_vs_naive, and calibrated net EV per contract "
                ">= absolute_min_ev; UNDERPOWERED below the floor; otherwise FAIL"
            ),
            "min_settled_per_lane": floor,
            "margin_vs_naive": params.margin_vs_naive,
            "absolute_min_ev": params.absolute_min_ev,
            "edge_threshold": params.edge_threshold,
            "min_calibration_samples": params.min_calibration_samples,
            "net_ev_definition": "signed contracts x (settlement price - YES-equivalent entry) - venue taker fee, per settled admission",
            "ci": "95% percentile bootstrap over city-day clusters (2000 resamples, seed 0); reported, not part of the rule",
        },
        sample_callout=callout,
    )


__all__ = [
    "ABVerdict",
    "CALIBRATED_TRACK",
    "CalibrationSample",
    "CalibrationStore",
    "CityCalibration",
    "DEFAULT_MODELS",
    "EnsembleEstimate",
    "LANES",
    "LaneEvaluation",
    "LaneOutcome",
    "ModelStats",
    "NAIVE_TRACK",
    "SettledRecord",
    "WEATHER_TRACKS",
    "WeatherLaneStrategy",
    "WeatherParameters",
    "ab_verdict",
    "bucket_probability",
    "calibrated_ensemble",
    "ladder_probabilities",
    "lane_outcome",
    "naive_ensemble",
    "normal_cdf",
]
