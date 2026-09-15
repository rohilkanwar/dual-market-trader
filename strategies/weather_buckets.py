"""Polymarket weather temperature-bucket edge: decision logic (no I/O).

Pre-registered experiment (see ``docs/WEATHER_BUCKETS.md``)::

    city-day        one Polymarket NegRisk event "Highest|Lowest temperature in <city> on <date>"
    station         the exact settlement station parsed from the rules text (NOAA
                    timeseries ``site=`` or a Weather Underground ICAO history page);
                    anything ambiguous or missing is refused before pricing
    p_model(b)      share of free multi-model ensemble members whose daily max / min,
                    rounded to the settlement precision (whole degrees), lands in
                    bucket b, with additive smoothing alpha over the K buckets
    edge(b)         buy YES: p_model - yes_ask - fee(yes_ask)
                    buy NO : (1 - p_model) - no_ask - fee(no_ask)
                    fee = rate * price * (1 - price)  (Polymarket taker fee, weather rate)
    enter           when the better side clears ``min_net_edge`` (0.03) at a price
                    inside [min_entry_price, max_entry_price], whole contracts, caps
                    $25 / order, $50 / bucket market, $100 / city-day, RiskManager rails
    settle          venue resolution (authoritative) closes positions at 1 / 0;
                    a public METAR-derived high / low is a provisional check only
    PASS            n_settled_city_days >= 30 and mean net PnL per contract across
                    city-days >= 0.02 with the 95 % lower bound above 0
    FAIL            n >= 30 and the rule is not met; fewer -> insufficient_sample

Everything here is arithmetic over inputs the research layer fetched; nothing
in this module reads a feed, and every threshold is a field of
:class:`WeatherEdgeParameters` echoed into the artifacts.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Sequence

from core.portfolio import Portfolio
from core.risk import RiskLimits, RiskManager
from core.types import ONE, ZERO, Market, Order, OrderBook, Outcome, Position, Side

TRACK = "weather_bucket_edge"
WEATHER_RISK_LIMITS = RiskLimits(
    max_notional_per_order=Decimal("10"),
    max_position_per_market=Decimal("50"),
    max_daily_loss=Decimal("250"),
)
Q4 = Decimal("0.0001")
_WHOLE = Decimal("1")
UNITS = ("F", "C")
KINDS = ("high", "low")
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4, rounding=ROUND_HALF_UP) if value is not None else None


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def whole_contracts(quantity: Decimal) -> Decimal:
    return quantity.quantize(_WHOLE, rounding=ROUND_DOWN)


def round_half_up(value: float | Decimal) -> int:
    """Whole-degree rounding used for the settlement precision (21.5 -> 22, -0.5 -> 0)."""
    return int(math.floor(float(value) + 0.5))


# --------------------------------------------------------------------------
# Parameters (pre-registered defaults)
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WeatherEdgeParameters:
    min_net_edge: Decimal = Decimal("0.03")
    pass_net_ev_per_contract: Decimal = Decimal("0.02")
    strong_net_ev_per_contract: Decimal = Decimal("0.03")
    min_settled_city_days: int = 30
    max_order_notional: Decimal = WEATHER_RISK_LIMITS.max_notional_per_order
    max_market_notional: Decimal = Decimal("20")
    max_city_day_notional: Decimal = Decimal("40")
    # Total cost basis the track may tie up; equals the paper starting cash so the
    # ledger never simulates borrowing (Polymarket requires full collateral).
    max_total_cash_at_risk: Decimal = Decimal("1000")
    min_entry_price: Decimal = Decimal("0.02")
    max_entry_price: Decimal = Decimal("0.95")
    max_spread: Decimal = Decimal("0.10")
    minimum_touch_size: Decimal = Decimal("5")  # Polymarket orderMinSize on these markets
    min_members: int = 30
    min_models: int = 2
    max_lead_days: int = 1
    max_forecast_age_hours: Decimal = Decimal("12")
    smoothing_alpha: Decimal = Decimal("0.5")
    dispersion_multiplier: Decimal = Decimal("1")
    bias_degrees: Decimal = ZERO
    min_complete_day_observations: int = 18

    def __post_init__(self) -> None:
        if not ZERO <= self.min_net_edge < ONE:
            raise ValueError("min_net_edge must be within [0, 1)")
        if self.min_settled_city_days < 1:
            raise ValueError("min_settled_city_days must be at least 1")
        if not ZERO <= self.min_entry_price < self.max_entry_price <= ONE:
            raise ValueError("entry price bounds must satisfy 0 <= min < max <= 1")
        if min(self.max_order_notional, self.max_market_notional, self.max_city_day_notional, self.max_total_cash_at_risk) <= ZERO:
            raise ValueError("notional caps must be positive")
        if not ZERO < self.max_spread <= ONE:
            raise ValueError("max_spread must be within (0, 1]")
        if self.minimum_touch_size <= ZERO or self.min_members < 1 or self.min_models < 1:
            raise ValueError("touch size, members and models minima must be positive")
        if self.max_lead_days < 0 or self.max_forecast_age_hours <= ZERO:
            raise ValueError("lead days must be non-negative and forecast age positive")
        if self.smoothing_alpha < ZERO or self.dispersion_multiplier <= ZERO:
            raise ValueError("smoothing_alpha must be >= 0 and dispersion_multiplier > 0")

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_net_edge": self.min_net_edge,
            "pass_net_ev_per_contract": self.pass_net_ev_per_contract,
            "strong_net_ev_per_contract": self.strong_net_ev_per_contract,
            "min_settled_city_days": self.min_settled_city_days,
            "max_order_notional": self.max_order_notional,
            "max_market_notional": self.max_market_notional,
            "max_city_day_notional": self.max_city_day_notional,
            "max_total_cash_at_risk": self.max_total_cash_at_risk,
            "min_entry_price": self.min_entry_price,
            "max_entry_price": self.max_entry_price,
            "max_spread": self.max_spread,
            "minimum_touch_size": self.minimum_touch_size,
            "min_members": self.min_members,
            "min_models": self.min_models,
            "max_lead_days": self.max_lead_days,
            "max_forecast_age_hours": self.max_forecast_age_hours,
            "smoothing_alpha": self.smoothing_alpha,
            "dispersion_multiplier": self.dispersion_multiplier,
            "bias_degrees": self.bias_degrees,
            "min_complete_day_observations": self.min_complete_day_observations,
            "risk_limits": {
                "max_notional_per_order": WEATHER_RISK_LIMITS.max_notional_per_order,
                "max_position_per_market": WEATHER_RISK_LIMITS.max_position_per_market,
                "max_daily_loss": WEATHER_RISK_LIMITS.max_daily_loss,
            },
        }


# --------------------------------------------------------------------------
# Buckets
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TemperatureBucket:
    """Closed whole-degree interval; ``None`` bounds are open ("or below" / "or higher")."""

    label: str
    lower: int | None
    upper: int | None
    unit: str

    def __post_init__(self) -> None:
        if self.unit not in UNITS:
            raise ValueError(f"unit must be one of {UNITS}")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError(f"bucket {self.label!r} has lower > upper")

    def contains(self, value: int) -> bool:
        if self.lower is not None and value < self.lower:
            return False
        if self.upper is not None and value > self.upper:
            return False
        return True

    @property
    def sort_key(self) -> int:
        if self.lower is not None:
            return self.lower
        return (self.upper if self.upper is not None else 0) - 10_000

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "lower": self.lower, "upper": self.upper, "unit": self.unit}


_RE_RANGE = re.compile(r"^(?:between\s+)?(-?\d+)\s*(?:°\s*([FC])\s*)?(?:-|–|to)\s*(-?\d+)\s*°\s*([FC])$", re.I)
_RE_OPEN_LOW = re.compile(r"^(-?\d+)\s*°\s*([FC])\s+or\s+(?:below|lower|less|colder)$", re.I)
_RE_OPEN_HIGH = re.compile(r"^(-?\d+)\s*°\s*([FC])\s+or\s+(?:higher|above|more|warmer)$", re.I)
_RE_POINT = re.compile(r"^(-?\d+)\s*°\s*([FC])$", re.I)


def _normalise_label(label: str) -> str:
    text = label.strip().replace("º", "°").replace("˚", "°").replace("℉", "°F").replace("℃", "°C")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?i)^will the (?:highest|lowest) temperature in .+? be ", "", text)
    text = re.sub(r"(?i) on [a-z]+ \d{1,2}\??$", "", text)
    return text.strip().rstrip("?").strip()


def parse_bucket_label(label: str) -> TemperatureBucket | None:
    """``"66-67°F"`` / ``"65°F or below"`` / ``"82°F or higher"`` / ``"27°C"`` -> bucket, else ``None``."""
    text = _normalise_label(label)
    if (m := _RE_RANGE.match(text)) is not None:
        lo, unit_a, hi, unit_b = m.groups()
        if unit_a and unit_a.upper() != unit_b.upper():
            return None
        lower, upper = int(lo), int(hi)
        if lower > upper:
            return None
        return TemperatureBucket(label.strip(), lower, upper, unit_b.upper())
    if (m := _RE_OPEN_LOW.match(text)) is not None:
        return TemperatureBucket(label.strip(), None, int(m.group(1)), m.group(2).upper())
    if (m := _RE_OPEN_HIGH.match(text)) is not None:
        return TemperatureBucket(label.strip(), int(m.group(1)), None, m.group(2).upper())
    if (m := _RE_POINT.match(text)) is not None:
        value = int(m.group(1))
        return TemperatureBucket(label.strip(), value, value, m.group(2).upper())
    return None


def buckets_partition_reason(buckets: Sequence[TemperatureBucket]) -> str | None:
    """``None`` when the buckets tile the whole line exactly once, else the reason."""
    if len(buckets) < 2:
        return "too_few_buckets"
    if len({b.unit for b in buckets}) != 1:
        return "mixed_units"
    ordered = sorted(buckets, key=lambda b: b.sort_key)
    if ordered[0].lower is not None:
        return "no_open_lower_bucket"
    if ordered[-1].upper is not None:
        return "no_open_upper_bucket"
    for previous, current in zip(ordered, ordered[1:]):
        if previous.upper is None or current.lower is None:
            return "open_bucket_in_the_middle"
        if current.lower != previous.upper + 1:
            return "gap_or_overlap"
    return None


def bucket_for_value(buckets: Sequence[TemperatureBucket], value: int) -> TemperatureBucket | None:
    for bucket in buckets:
        if bucket.contains(value):
            return bucket
    return None


# --------------------------------------------------------------------------
# Settlement rules: station, unit, kind, date (fail-closed)
# --------------------------------------------------------------------------
_RE_NOAA_SITE = re.compile(r"weather\.gov/wrh/timeseries\?site=([A-Za-z0-9]{3,6})")
_RE_WUNDERGROUND = re.compile(r"wunderground\.com/history/daily/([A-Za-z0-9_\-./]+)")


def _wunderground_stations(text: str) -> list[str]:
    """Station segment of ``.../history/daily/<country>/<city>/<STATION>[/date/...]`` URLs."""
    out: list[str] = []
    for match in _RE_WUNDERGROUND.finditer(text):
        segments = [s for s in match.group(1).rstrip(".,)").split("/") if s]
        if "date" in segments:
            segments = segments[: segments.index("date")]
        if len(segments) >= 3:
            out.append(segments[-1].upper())
    return out
_RE_UNIT = re.compile(r"in degrees (Fahrenheit|Celsius)", re.I)
_RE_KIND = re.compile(r"\b(highest|lowest) temperature", re.I)
_RE_DATE = re.compile(r"\bon (\d{1,2}) ([A-Za-z]{3})\.? '(\d{2})\b")
_RE_STATION_NAME = re.compile(r"recorded (?:by [A-Za-z ]+? )?at the (.+?)(?: Station)? in degrees", re.I)
_RE_ICAO = re.compile(r"^[A-Z][A-Z0-9]{3}$")


@dataclass(frozen=True, slots=True)
class SettlementRules:
    station: str | None
    source: str | None  # noaa_timeseries | wunderground
    unit: str | None
    kind: str | None
    observation_date: date | None
    station_name: str | None
    reason: str  # parsed | no_station | station_ambiguous | station_not_icao | no_unit | no_kind | no_date
    stations_seen: tuple[str, ...] = ()

    @property
    def admitted(self) -> bool:
        return self.reason == "parsed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "source": self.source,
            "unit": self.unit,
            "kind": self.kind,
            "observation_date": self.observation_date.isoformat() if self.observation_date else None,
            "station_name": self.station_name,
            "reason": self.reason,
            "stations_seen": list(self.stations_seen),
        }


def parse_settlement_rules(text: str) -> SettlementRules:
    """Read the station, unit, high/low and date out of a Polymarket weather description.

    Fail-closed: no station URL, two different station ids, a non-ICAO id, or a
    missing unit / kind / date all refuse. The Hong Kong Observatory markets
    (no station id, a climatological table) are refused as ``no_station``.
    """
    text = text or ""
    found: list[tuple[str, str]] = []
    for match in _RE_NOAA_SITE.finditer(text):
        found.append((match.group(1).upper(), "noaa_timeseries"))
    for station_id in _wunderground_stations(text):
        found.append((station_id, "wunderground"))
    stations = tuple(sorted({s for s, _ in found}))
    unit_match = _RE_UNIT.search(text)
    unit = {"fahrenheit": "F", "celsius": "C"}.get(unit_match.group(1).lower()) if unit_match else None
    kind_match = _RE_KIND.search(text)
    kind = {"highest": "high", "lowest": "low"}.get(kind_match.group(1).lower()) if kind_match else None
    observation_date: date | None = None
    if (date_match := _RE_DATE.search(text)) is not None:
        day, month, year = date_match.groups()
        month_index = _MONTHS.get(month.lower())
        if month_index is not None:
            try:
                observation_date = date(2000 + int(year), month_index, int(day))
            except ValueError:
                observation_date = None
    name_match = _RE_STATION_NAME.search(text)
    station_name = name_match.group(1).strip() if name_match else None

    if not stations:
        reason = "no_station"
        station = source = None
    elif len(stations) > 1:
        reason = "station_ambiguous"
        station = source = None
    else:
        station = stations[0]
        sources = {src for s, src in found if s == station}
        source = "noaa_timeseries" if "noaa_timeseries" in sources else next(iter(sources))
        if not _RE_ICAO.match(station):
            reason = "station_not_icao"
        elif unit is None:
            reason = "no_unit"
        elif kind is None:
            reason = "no_kind"
        elif observation_date is None:
            reason = "no_date"
        else:
            reason = "parsed"
    return SettlementRules(station, source, unit, kind, observation_date, station_name, reason, stations)


# --------------------------------------------------------------------------
# Ensemble -> bucket probabilities
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BucketProbabilities:
    probabilities: dict[str, Decimal]  # bucket label -> p_model
    counts: dict[str, int]
    n_members: int
    mean: Decimal | None
    std: Decimal | None
    unassigned: int
    rounded_values: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "probabilities": {k: _q(v) for k, v in self.probabilities.items()},
            "counts": dict(self.counts),
            "n_members": self.n_members,
            "mean": _q(self.mean),
            "std": _q(self.std),
            "unassigned": self.unassigned,
        }


def ensemble_bucket_probabilities(
    members: Sequence[float | Decimal],
    buckets: Sequence[TemperatureBucket],
    *,
    parameters: WeatherEdgeParameters | None = None,
) -> BucketProbabilities:
    """Empirical member frequency per bucket after bias / dispersion adjustment and rounding.

    ``p(b) = (count_b + alpha) / (N + alpha * K)``; with the pre-registered
    ``alpha = 0.5`` a bucket no member hit still carries a small probability, so
    a bucket priced at 0.1 c is never called a certain zero.
    """
    params = parameters or WeatherEdgeParameters()
    values = [float(v) for v in members]
    n = len(values)
    if n == 0:
        return BucketProbabilities({b.label: ZERO for b in buckets}, {b.label: 0 for b in buckets}, 0, None, None, 0, ())
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    multiplier = float(params.dispersion_multiplier)
    bias = float(params.bias_degrees)
    rounded = tuple(round_half_up(mean + (v - mean) * multiplier + bias) for v in values)
    counts = {b.label: 0 for b in buckets}
    unassigned = 0
    for value in rounded:
        bucket = bucket_for_value(buckets, value)
        if bucket is None:
            unassigned += 1
        else:
            counts[bucket.label] += 1
    alpha = params.smoothing_alpha
    denominator = Decimal(n) + alpha * Decimal(len(buckets))
    probabilities = {label: (Decimal(count) + alpha) / denominator for label, count in counts.items()}
    return BucketProbabilities(
        probabilities=probabilities,
        counts=counts,
        n_members=n,
        mean=Decimal(str(round(mean, 4))),
        std=Decimal(str(round(math.sqrt(variance), 4))),
        unassigned=unassigned,
        rounded_values=rounded,
    )


# --------------------------------------------------------------------------
# Fees, edges, sizing
# --------------------------------------------------------------------------
def polymarket_fee_per_contract(price: Decimal, rate: Decimal) -> Decimal:
    """Published taker fee ``rate * p * (1 - p)`` per contract at the price paid."""
    if rate <= ZERO:
        return ZERO
    return rate * price * (ONE - price)


def position_cash_at_risk(position: Position | None) -> Decimal:
    """Cost basis of a position in the outcome it holds (YES at avg, NO at 1 - avg)."""
    if position is None or position.quantity == ZERO:
        return ZERO
    if position.quantity > ZERO:
        return position.quantity * position.average_price
    return -position.quantity * (ONE - position.average_price)


def portfolio_cash_at_risk(portfolio: Portfolio | None) -> Decimal:
    if portfolio is None:
        return ZERO
    return sum((position_cash_at_risk(p) for p in portfolio.positions()), ZERO)


def no_ask_from_books(yes_book: OrderBook, no_book: OrderBook | None) -> tuple[Decimal, Decimal] | None:
    """Best price and size to *buy NO*: the NO ladder when present, else ``1 - yes_bid``."""
    if no_book is not None and no_book.best_ask is not None:
        return no_book.best_ask.price, no_book.best_ask.size
    if yes_book.best_bid is not None:
        return ONE - yes_book.best_bid.price, yes_book.best_bid.size
    return None


@dataclass(frozen=True, slots=True)
class WeatherEvaluation:
    reason: str
    bucket: str
    p_model: Decimal
    mid: Decimal | None = None
    yes_ask: Decimal | None = None
    no_ask: Decimal | None = None
    side: str | None = None  # buy_yes | buy_no
    price: Decimal | None = None
    gross_edge: Decimal | None = None
    fee_per_contract: Decimal | None = None
    net_edge: Decimal | None = None
    quantity: Decimal = ZERO
    orders: tuple[Order, ...] = ()

    @property
    def traded(self) -> bool:
        return bool(self.orders)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "bucket": self.bucket,
            "p_model": _q(self.p_model),
            "mid": _q(self.mid),
            "yes_ask": _q(self.yes_ask),
            "no_ask": _q(self.no_ask),
            "side": self.side,
            "price": _q(self.price),
            "gross_edge": _q(self.gross_edge),
            "fee_per_contract": _q(self.fee_per_contract),
            "net_edge": _q(self.net_edge),
            "quantity": self.quantity,
            "orders": len(self.orders),
        }


class WeatherBucketEdgeStrategy:
    """Per-bucket taker decision: buy the side whose model edge clears fees plus the threshold."""

    name = TRACK

    def __init__(
        self,
        parameters: WeatherEdgeParameters | None = None,
        *,
        portfolio: Portfolio | None = None,
        risk: RiskManager | None = None,
    ) -> None:
        self.parameters = parameters or WeatherEdgeParameters()
        self.portfolio = portfolio
        self.risk = risk

    def evaluate(
        self,
        market: Market,
        bucket: TemperatureBucket,
        p_model: Decimal,
        yes_book: OrderBook,
        no_book: OrderBook | None = None,
        *,
        fee_rate: Decimal = ZERO,
        city_day_notional_used: Decimal = ZERO,
        context: dict[str, str] | None = None,
    ) -> WeatherEvaluation:
        params = self.parameters
        base = {"bucket": bucket.label, "p_model": p_model, "mid": yes_book.mid_price}
        if not market.active:
            return WeatherEvaluation("market_inactive", **base)
        yes_ask = yes_book.best_ask
        no_side = no_ask_from_books(yes_book, no_book)
        base["yes_ask"] = yes_ask.price if yes_ask is not None else None
        base["no_ask"] = no_side[0] if no_side is not None else None
        if yes_ask is None and no_side is None:
            return WeatherEvaluation("empty_book", **base)
        if yes_book.mid_price is None:
            # The edge is measured against a two-sided book; a one-sided touch is not a price.
            return WeatherEvaluation("one_sided_book", **base)
        if (yes_book.spread or ZERO) > params.max_spread:
            return WeatherEvaluation("wide_spread", **base)

        candidates: list[tuple[Decimal, str, Decimal, Decimal, Decimal, Decimal]] = []
        if yes_ask is not None:
            gross = p_model - yes_ask.price
            fee = polymarket_fee_per_contract(yes_ask.price, fee_rate)
            candidates.append((gross - fee, "buy_yes", yes_ask.price, yes_ask.size, gross, fee))
        if no_side is not None:
            price, size = no_side
            gross = (ONE - p_model) - price
            fee = polymarket_fee_per_contract(price, fee_rate)
            candidates.append((gross - fee, "buy_no", price, size, gross, fee))
        net, side, price, size, gross, fee = max(candidates, key=lambda c: c[0])
        common = {**base, "side": side, "price": price, "gross_edge": gross, "fee_per_contract": fee, "net_edge": net}
        if gross <= ZERO:
            return WeatherEvaluation("no_edge", **common)
        if net < params.min_net_edge:
            return WeatherEvaluation("below_edge_threshold", **common)
        if price < params.min_entry_price:
            return WeatherEvaluation("price_below_floor", **common)
        if price > params.max_entry_price:
            return WeatherEvaluation("price_above_cap", **common)
        if not ZERO < price < ONE:
            return WeatherEvaluation("touch_at_bound", **common)
        if size < params.minimum_touch_size:
            return WeatherEvaluation("insufficient_touch_depth", **common)

        outcome = Outcome.YES if side == "buy_yes" else Outcome.NO
        position = self.portfolio.get(market.venue, market.market_id) if self.portfolio else None
        if position is not None and position.quantity != ZERO:
            holds_yes = position.quantity > ZERO
            if holds_yes != (outcome is Outcome.YES):
                return WeatherEvaluation("opposite_position_held", **common)
        market_headroom = params.max_market_notional - position_cash_at_risk(position)
        city_headroom = params.max_city_day_notional - city_day_notional_used
        total_headroom = params.max_total_cash_at_risk - portfolio_cash_at_risk(self.portfolio)
        if market_headroom <= ZERO:
            return WeatherEvaluation("market_notional_cap_reached", **common)
        if city_headroom <= ZERO:
            return WeatherEvaluation("city_day_notional_cap_reached", **common)
        if total_headroom <= ZERO:
            return WeatherEvaluation("total_cash_at_risk_cap_reached", **common)
        quantity = min(size, params.max_order_notional / price, market_headroom / price, city_headroom / price, total_headroom / price)
        probe = Order(
            venue=market.venue,
            market_id=market.market_id,
            side=Side.BUY,
            outcome=outcome,
            quantity=max(whole_contracts(quantity), _WHOLE),
            price=price,
        )
        if self.risk is not None:
            quantity = min(quantity, self.risk.remaining_order_capacity(probe, position))
        quantity = whole_contracts(quantity)
        if quantity <= ZERO:
            return WeatherEvaluation("no_position_headroom", **common)
        if quantity < params.minimum_touch_size:
            return WeatherEvaluation("below_min_order_size", quantity=quantity, **common)
        order = Order(
            venue=market.venue,
            market_id=market.market_id,
            side=Side.BUY,
            outcome=outcome,
            quantity=quantity,
            price=price,
            metadata={
                "strategy": self.name,
                "bucket": bucket.label,
                "p_model": str(_q(p_model)),
                "mid": str(_q(yes_book.mid_price)) if yes_book.mid_price is not None else "",
                "gross_edge": str(_q(gross)),
                "fee_per_contract": str(_q(fee)),
                "net_edge": str(_q(net)),
                "fee_rate": str(fee_rate),
                "price_signal_status": "free_multi_model_ensemble",
                **(context or {}),
            },
        )
        return WeatherEvaluation("trade", quantity=quantity, orders=(order,), **common)


# --------------------------------------------------------------------------
# Verdict on settled city-days
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CityDayResult:
    record_id: str
    contracts: Decimal
    net_pnl: Decimal  # after fees, in USDC
    settled_by: str  # venue | metar_provisional

    @property
    def per_contract(self) -> Decimal | None:
        return (self.net_pnl / self.contracts) if self.contracts > ZERO else None


@dataclass(frozen=True, slots=True)
class Verdict:
    n: int
    contracts: Decimal
    net_pnl: Decimal
    mean_per_contract: Decimal | None
    pooled_per_contract: Decimal | None
    std_per_contract: Decimal | None
    lower_95: Decimal | None
    upper_95: Decimal | None
    status: str  # PASS | FAIL | insufficient_sample
    strong: bool
    pre_registered: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "contracts": self.contracts,
            "net_pnl": _q(self.net_pnl),
            "mean_per_contract": _q(self.mean_per_contract),
            "pooled_per_contract": _q(self.pooled_per_contract),
            "std_per_contract": _q(self.std_per_contract),
            "lower_95": _q(self.lower_95),
            "upper_95": _q(self.upper_95),
            "status": self.status,
            "strong": self.strong,
            "pre_registered": self.pre_registered,
        }


def verdict(results: Sequence[CityDayResult], *, parameters: WeatherEdgeParameters | None = None) -> Verdict:
    """Pre-registered rule over settled city-days that carried at least one contract.

    Each city-day is one observation (its buckets share a single outcome, so
    per-bucket rows are not independent). The mean of per-city-day net PnL per
    contract must clear ``pass_net_ev_per_contract`` with its normal-approximation
    95 % lower bound above zero, at ``n >= min_settled_city_days``.
    """
    params = parameters or WeatherEdgeParameters()
    rows = [r for r in results if r.contracts > ZERO]
    n = len(rows)
    contracts = sum((r.contracts for r in rows), ZERO)
    net = sum((r.net_pnl for r in rows), ZERO)
    per = [r.per_contract for r in rows if r.per_contract is not None]
    mean = (sum(per, ZERO) / n) if n else None
    pooled = (net / contracts) if contracts > ZERO else None
    std = lower = upper = None
    if n >= 2 and mean is not None:
        variance = sum(((p - mean) ** 2 for p in per), ZERO) / (n - 1)
        std = Decimal(str(round(math.sqrt(float(variance)), 6)))
        half = Decimal("1.96") * std / Decimal(str(round(math.sqrt(n), 6)))
        lower, upper = mean - half, mean + half
    if n < params.min_settled_city_days:
        status = "insufficient_sample"
    elif mean is not None and lower is not None and mean >= params.pass_net_ev_per_contract and lower > ZERO:
        status = "PASS"
    else:
        status = "FAIL"
    return Verdict(
        n=n,
        contracts=contracts,
        net_pnl=net,
        mean_per_contract=mean,
        pooled_per_contract=pooled,
        std_per_contract=std,
        lower_95=lower,
        upper_95=upper,
        status=status,
        strong=bool(status == "PASS" and mean is not None and mean >= params.strong_net_ev_per_contract),
        pre_registered={
            "rule": (
                "PASS when n_settled_city_days >= min_settled_city_days and the city-day mean of net PnL per "
                "contract (after fees) >= pass_net_ev_per_contract with its 95% lower bound > 0; FAIL when n is "
                "reached and the rule fails; insufficient_sample otherwise"
            ),
            "unit_of_inference": "one settled city-day (all its bucket fills share one outcome)",
            "pass_net_ev_per_contract": params.pass_net_ev_per_contract,
            "strong_net_ev_per_contract": params.strong_net_ev_per_contract,
            "min_settled_city_days": params.min_settled_city_days,
            "entry_threshold_min_net_edge": params.min_net_edge,
            "settlement": "venue resolution only (METAR-derived outcomes are a provisional cross-check, reported separately)",
            "fees": "Polymarket taker fee rate * p * (1 - p) per contract, deducted at fill time",
        },
    )


def brier(probability: Decimal, outcome: bool) -> Decimal:
    target = ONE if outcome else ZERO
    return (probability - target) ** 2


__all__ = [
    "BucketProbabilities",
    "CityDayResult",
    "KINDS",
    "SettlementRules",
    "TRACK",
    "TemperatureBucket",
    "UNITS",
    "Verdict",
    "WEATHER_RISK_LIMITS",
    "WeatherBucketEdgeStrategy",
    "WeatherEdgeParameters",
    "WeatherEvaluation",
    "brier",
    "bucket_for_value",
    "buckets_partition_reason",
    "ensemble_bucket_probabilities",
    "no_ask_from_books",
    "parse_bucket_label",
    "parse_settlement_rules",
    "polymarket_fee_per_contract",
    "portfolio_cash_at_risk",
    "position_cash_at_risk",
    "round_half_up",
    "verdict",
    "whole_contracts",
]
