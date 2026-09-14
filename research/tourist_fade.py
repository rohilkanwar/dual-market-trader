"""Fade-the-tourist measurement: tape replay, adverse-selection markouts, live track.

Two measurements share :mod:`strategies.tourist_flow`:

* **Ex-post replay** over settled markets whose public trade tape and result
  are known. Every print is classified; when tourist prints cluster on one
  side the replay books a paper fade of the other side into a
  :class:`core.ledger.PaperLedger` (taker fee, assumed spread because the tape
  has no book), then settles it at the market result. The PASS criterion is
  *fade EV > 0 after fees* with events as the unit of inference.
* **Live track** (``fade_the_tourist``) that reads each open market's recent
  prints plus its current public book, fades a fresh cluster at the touch
  through the normal risk-gated engine, carries the ledger across runs and
  settles positions once the venue publishes a result.

Adverse selection is measured, not assumed: for every tourist-flagged print
the report carries the bought side's price change ``k`` prints later and at
settlement (a *markout*). Flow that looks recreational but earns a positive
markout is informed — the classic in-play tennis court-sider, or a crypto
taker reacting to spot before the book does — and fading it loses. The
``tourist_flow_loses`` verdict FAILs in that case and the report says so.

Data: two unauthenticated Kalshi endpoints (``GET /markets?status=settled``
and ``GET /markets/trades``, harvested by :func:`research.flb_expost.harvest_settled_trades`)
plus ``GET /markets/{ticker}`` for settlement of carried positions. No keys.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import httpx

from core.ledger import PaperLedger
from core.risk import RiskManager, RiskViolation
from core.types import ONE, ZERO, Fill, Market, Order, Outcome, Side, Venue
from research.flb import VERDICT_FAIL, VERDICT_INSUFFICIENT, VERDICT_PASS, KalshiFeeModel
from research.flb_expost import SettledMarket
from research.scoreboard import TrackRuntime, TrackSummary, VenueSnapshot, _bps
from strategies.tourist_flow import (
    TOURIST_RISK_LIMITS,
    ClusterDetector,
    ClusterSignal,
    FadeEvaluation,
    FadeTouristStrategy,
    TapeTrade,
    TouristFlags,
    TouristParameters,
    classify_trade,
    portfolio_cash_at_risk,
    regime_for,
    replay_fade_price,
    size_fade,
)

TOURIST_TRACK = "fade_the_tourist"
TOURIST_TRACK_LABEL = "Fade the tourist (Kalshi tennis)"
TOURIST_FAMILY = "tourist_fade"
SCHEMA_VERSION = "1.0.0"
HARVEST_FILE = "kalshi_tourist_settled_trades.json"
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "venues" / "kalshi" / "fixtures"
SETTLED_FIXTURE_PATH = FIXTURE_DIR / "tourist_settled_trades.json"
MARKETS_FIXTURE_PATH = FIXTURE_DIR / "tourist_markets.json"

TENNIS_SERIES: tuple[str, ...] = ("KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH")
CRYPTO_SERIES: tuple[str, ...] = ("KXBTC15M", "KXETH15M")
UNIVERSES: dict[str, tuple[str, ...]] = {
    "tennis": TENNIS_SERIES,
    "crypto": CRYPTO_SERIES,
    "both": TENNIS_SERIES + CRYPTO_SERIES,
}
MARKOUT_HORIZONS: tuple[int, ...] = (5, 20)
VERDICT_KEYS: tuple[str, ...] = (
    "fade_ev_positive_after_fees",
    "strong_regime_fade_ev_positive",
    "strong_regime_beats_weak",
    "fade_ev_positive_excluding_final_minutes",
    "strong_regime_fade_ev_positive_excluding_final_minutes",
    "tourist_flow_loses",
    "tourist_worse_than_other_takers",
)
Q4 = Decimal("0.0001")


def _now() -> datetime:
    return datetime.now(UTC)


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q4) if value is not None else None


# --------------------------------------------------------------------------
# Tape helpers
# --------------------------------------------------------------------------
def tape_from_settled(market: SettledMarket) -> list[TapeTrade]:
    """Chronological :class:`TapeTrade` list from a harvested settled market."""
    out: list[TapeTrade] = []
    for record in market.trades:
        if record.taker_side not in ("yes", "no") or not ZERO <= record.yes_price <= ONE or record.count <= ZERO:
            continue
        out.append(TapeTrade(record.created_time, Outcome(record.taker_side), record.yes_price, record.count))
    out.sort(key=lambda t: t.created_time)
    return out


def tape_from_rows(rows: Sequence[dict[str, Any]]) -> list[TapeTrade]:
    """Parse public ``/markets/trades`` rows (or the compact harvest form) into a chronological tape."""
    out: list[TapeTrade] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        created = _dt(raw.get("t") or raw.get("created_time"))
        side = str(raw.get("s") or raw.get("taker_side") or "").lower()
        price_raw = raw.get("p") if "p" in raw else raw.get("yes_price_dollars")
        if price_raw in (None, "") and raw.get("yes_price") not in (None, ""):
            price_raw = str(Decimal(str(raw["yes_price"])) / Decimal("100"))
        count_raw = raw.get("c") if "c" in raw else (raw.get("count_fp") if raw.get("count_fp") not in (None, "") else raw.get("count"))
        try:
            price = Decimal(str(price_raw))
            count = Decimal(str(count_raw))
        except (ArithmeticError, TypeError, ValueError):
            continue
        if created is None or side not in ("yes", "no") or not ZERO <= price <= ONE or count <= ZERO:
            continue
        out.append(TapeTrade(created, Outcome(side), price, count, trade_id=raw.get("trade_id")))
    out.sort(key=lambda t: t.created_time)
    return out


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# Clustered statistics (events are the unit of inference)
# --------------------------------------------------------------------------
@dataclass(slots=True)
class ClusterStat:
    """Contract-weighted mean with event-clustered SE plus an equal-weighted view."""

    weights: dict[str, float] = field(default_factory=dict)
    sums: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, cluster: str, weight: float, value_sum: float, n: int = 1) -> None:
        if weight <= 0:
            return
        self.weights[cluster] = self.weights.get(cluster, 0.0) + weight
        self.sums[cluster] = self.sums.get(cluster, 0.0) + value_sum
        self.counts[cluster] = self.counts.get(cluster, 0) + n

    @property
    def n_clusters(self) -> int:
        return len(self.weights)

    def summary(self) -> dict[str, Any]:
        keys = list(self.weights)
        if not keys:
            return {"n_events": 0, "n": 0, "contracts": 0.0, "mean": None, "clustered_se": None, "t_stat": None, "mean_equal_weight": None, "t_stat_equal_weight": None, "effective_n_events": 0.0}
        w = [self.weights[k] for k in keys]
        means = [self.sums[k] / self.weights[k] for k in keys]
        total_w = sum(w)
        mu = sum(wi * mi for wi, mi in zip(w, means)) / total_w
        var = sum(wi * (mi - mu) ** 2 for wi, mi in zip(w, means)) / total_w
        n_eff = total_w**2 / sum(wi * wi for wi in w)
        se = math.sqrt(var / (n_eff - 1)) if n_eff > 1 and var > 0 else None
        n = len(keys)
        mu_eq = sum(means) / n
        var_eq = sum((m - mu_eq) ** 2 for m in means) / (n - 1) if n > 1 else 0.0
        se_eq = math.sqrt(var_eq / n) if n > 1 and var_eq > 0 else None
        return {
            "n_events": n,
            "n": sum(self.counts.values()),
            "contracts": round(total_w, 2),
            "mean": round(mu, 5),
            "clustered_se": round(se, 5) if se is not None else None,
            "t_stat": round(mu / se, 2) if se else None,
            "effective_n_events": round(n_eff, 2),
            "mean_equal_weight": round(mu_eq, 5),
            "se_equal_weight": round(se_eq, 5) if se_eq is not None else None,
            "t_stat_equal_weight": round(mu_eq / se_eq, 2) if se_eq else None,
        }


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------
@dataclass(slots=True)
class FadeRecord:
    market: str
    event: str
    series: str
    category: str
    at: str
    faded_side: str
    fade_side: str
    price: Decimal
    regime: str
    quantity: Decimal
    fee: Decimal
    cluster_trades: int
    cluster_notional: Decimal
    combinations: dict[str, int]
    result: str
    minutes_to_close: float | None = None
    settlement_pnl: Decimal | None = None
    won: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "event": self.event,
            "series": self.series,
            "category": self.category,
            "at": self.at,
            "minutes_to_close": round(self.minutes_to_close, 2) if self.minutes_to_close is not None else None,
            "faded_side": self.faded_side,
            "fade_side": self.fade_side,
            "price": self.price,
            "regime": self.regime,
            "qty": self.quantity,
            "notional": _q(self.quantity * self.price),
            "fee": self.fee,
            "cluster_trades": self.cluster_trades,
            "cluster_notional": self.cluster_notional,
            "combinations": self.combinations,
            "result": self.result,
            "won": self.won,
            "settlement_pnl": self.settlement_pnl,
            "settlement_pnl_per_contract": _q(self.settlement_pnl / self.quantity) if self.settlement_pnl is not None and self.quantity else None,
        }


@dataclass(slots=True)
class MarketReplay:
    ticker: str
    event: str
    series: str
    category: str
    result: str
    tape_trades: int
    tourist_trades: int
    tourist_notional: Decimal
    total_notional: Decimal
    clusters: int
    fades: list[FadeRecord]
    refusals: dict[str, int]
    combinations: dict[str, int]
    # markout accumulators: key -> (contracts, sum(contracts * markout), n)
    markouts: dict[str, tuple[float, float, int]]


def _markout_key(group: str, horizon: str) -> str:
    return f"{group}|{horizon}"


def replay_market(
    market: SettledMarket,
    params: TouristParameters,
    fee_model: KalshiFeeModel,
    *,
    ledger: PaperLedger,
    risk: RiskManager | None = None,
) -> MarketReplay:
    """Replay one settled tape: classify, cluster, paper-fade, settle."""
    tape = tape_from_settled(market)
    proxy = market.as_market()
    detector = ClusterDetector(market.ticker, params)
    risk = risk or RiskManager(TOURIST_RISK_LIMITS)
    fades: list[FadeRecord] = []
    refusals: dict[str, int] = {}
    combinations: dict[str, int] = {}
    markouts: dict[str, tuple[float, float, int]] = {}
    flags_by_index: list[TouristFlags] = []
    tourist_notional = ZERO
    total_notional = ZERO
    result_outcome = Outcome(market.result)

    def bump(reason: str) -> None:
        refusals[reason] = refusals.get(reason, 0) + 1

    def add_markout(group: str, horizon: str, contracts: Decimal, value: Decimal) -> None:
        key = _markout_key(group, horizon)
        c, s, n = markouts.get(key, (0.0, 0.0, 0))
        markouts[key] = (c + float(contracts), s + float(contracts * value), n + 1)

    for index, trade in enumerate(tape):
        flags = classify_trade(trade, tape[:index], params, open_time=market.open_time, close_time=market.close_time)
        flags_by_index.append(flags)
        total_notional += trade.taker_notional
        tourist = flags.is_tourist(params)
        group = "tourist" if tourist else "other"
        if tourist:
            tourist_notional += trade.taker_notional
            combinations[flags.combination] = combinations.get(flags.combination, 0) + 1
        # Markouts: how the side the taker bought moved afterwards. Positive = the taker was right.
        for horizon in MARKOUT_HORIZONS:
            if index + horizon < len(tape):
                add_markout(group, f"+{horizon}", trade.count, tape[index + horizon].side_price(trade.taker_side) - trade.taker_price)
        settle_value = (ONE if trade.taker_side is result_outcome else ZERO) - trade.taker_price
        add_markout(group, "settlement", trade.count, settle_value)
        if tourist:
            add_markout(f"combo:{flags.combination}", "settlement", trade.count, settle_value)
        signal = detector.observe(trade, flags)
        if signal is None:
            continue
        price = replay_fade_price(signal, params)
        position = ledger.portfolio.get(Venue.KALSHI, market.ticker)
        probe = Order(venue=Venue.KALSHI, market_id=market.ticker, side=Side.BUY, quantity=ONE, outcome=signal.fade_side, price=price)
        quantity, refusal = size_fade(
            favorite_price=price,
            params=params,
            position=position,
            total_cash_at_risk=portfolio_cash_at_risk(ledger.portfolio),
            risk=risk,
            probe=probe,
        )
        if quantity <= ZERO:
            bump(refusal)
            continue
        order = Order(venue=Venue.KALSHI, market_id=market.ticker, side=Side.BUY, quantity=quantity, outcome=signal.fade_side, price=price)
        try:
            risk.validate_order(order, position)
        except RiskViolation as exc:
            bump("risk_" + str(exc).split(" ")[0].lower())
            continue
        fee = fee_model.fee(quantity, price, maker=False, market=proxy)
        fill = Fill(
            venue=Venue.KALSHI,
            market_id=market.ticker,
            order_id=f"replay-fade-{market.ticker}-{len(fades) + 1}",
            side=Side.BUY,
            quantity=quantity,
            price=price,
            outcome=signal.fade_side,
            timestamp=trade.created_time,
            fee=fee,
        )
        ledger.record_fill(fill)
        fades.append(
            FadeRecord(
                market=market.ticker,
                event=market.event_ticker or market.ticker,
                series=market.series_ticker,
                category=market.category or "unknown",
                at=trade.created_time.isoformat(),
                faded_side=signal.side.value,
                fade_side=signal.fade_side.value,
                price=price,
                regime=regime_for(price, params),
                quantity=quantity,
                fee=fee,
                cluster_trades=signal.trades,
                cluster_notional=signal.notional,
                combinations=signal.combinations,
                result=market.result,
                minutes_to_close=((market.close_time - trade.created_time).total_seconds() / 60) if market.close_time is not None else None,
            )
        )
    # Settle every fade at the published result. Realized PnL lands in the ledger.
    settle_price = ONE if result_outcome is Outcome.YES else ZERO
    for record in fades:
        yes_equivalent = record.price if record.fade_side == "yes" else ONE - record.price
        signed = record.quantity if record.fade_side == "yes" else -record.quantity
        record.settlement_pnl = (signed * (settle_price - yes_equivalent) - record.fee).quantize(Q4)
        record.won = record.fade_side == market.result
    if fades:
        ledger.settle(Venue.KALSHI, market.ticker, result_outcome)
        risk.record_realized_pnl(sum((r.settlement_pnl or ZERO for r in fades), ZERO))
    return MarketReplay(
        ticker=market.ticker,
        event=market.event_ticker or market.ticker,
        series=market.series_ticker,
        category=market.category or "unknown",
        result=market.result,
        tape_trades=len(tape),
        tourist_trades=detector.tourist_trades,
        tourist_notional=tourist_notional.quantize(Q4),
        total_notional=total_notional.quantize(Q4),
        clusters=len(detector.signals),
        fades=fades,
        refusals=refusals,
        combinations=combinations,
        markouts=markouts,
    )


def _fade_stats(fades: list[FadeRecord]) -> dict[str, Any]:
    net = ClusterStat()
    notional = ZERO
    pnl = ZERO
    fees = ZERO
    wins = 0
    for record in fades:
        if record.settlement_pnl is None:
            continue
        net.add(record.event, float(record.quantity), float(record.settlement_pnl))
        notional += record.quantity * record.price
        pnl += record.settlement_pnl
        fees += record.fee
        wins += int(bool(record.won))
    stats = net.summary()
    stats.update({
        "fades": len(fades),
        "markets": len({r.market for r in fades}),
        "win_rate": round(wins / len(fades), 4) if fades else None,
        "notional": _q(notional),
        "fees": _q(fees),
        "net_pnl": _q(pnl),
        "net_pnl_per_contract": stats["mean"],
        "return_on_stake": _q(pnl / notional) if notional else None,
        "return_on_stake_bps": _bps(pnl / notional) if notional else None,
    })
    return stats


def _markout_table(replays: list[MarketReplay], *, only_groups: tuple[str, ...] | None = None) -> dict[str, dict[str, Any]]:
    """Contract-weighted markouts of the side the taker bought, clustered by event."""
    stats: dict[str, ClusterStat] = {}
    for replay in replays:
        for key, (contracts, value_sum, n) in replay.markouts.items():
            if only_groups is not None and key.split("|", 1)[0] not in only_groups:
                continue
            stats.setdefault(key, ClusterStat()).add(replay.event, contracts, value_sum, n)
    table: dict[str, dict[str, Any]] = {}
    for key in sorted(stats):
        group, horizon = key.split("|", 1)
        table.setdefault(group, {})[horizon] = stats[key].summary()
    return table


def _markouts_by(replays: list[MarketReplay], key: Callable[[MarketReplay], str]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[MarketReplay]] = {}
    for replay in replays:
        groups.setdefault(key(replay), []).append(replay)
    return {name: _markout_table(rows, only_groups=("tourist", "other")) for name, rows in sorted(groups.items())}


def _table_by(fades: list[FadeRecord], key: Callable[[FadeRecord], str]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[FadeRecord]] = {}
    for record in fades:
        groups.setdefault(key(record), []).append(record)
    return {name: _fade_stats(rows) for name, rows in sorted(groups.items())}


def fade_price_band(price: Decimal) -> str:
    """Decile band of the price the fade paid for the favourite: ``0.7-0.8`` etc."""
    lower = (price * 10).to_integral_value(rounding="ROUND_DOWN") / 10
    lower = min(lower, Decimal("0.9"))
    return f"{lower:.1f}-{lower + Decimal('0.1'):.1f}"


def event_of_ticker(ticker: str) -> str:
    """Kalshi tickers are ``SERIES-EVENT-LEG``; both legs of a match share the event."""
    return ticker.rsplit("-", 1)[0] if ticker.count("-") >= 2 else ticker


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------
def _enough(stats: dict[str, Any], *, min_events: int, min_fades: int) -> bool:
    return stats.get("n_events", 0) >= min_events and stats.get("fades", stats.get("n", 0)) >= min_fades


def fade_ev_verdict(stats: dict[str, Any], *, scope: str, min_events: int, min_fades: int, t_threshold: float = 2.0) -> dict[str, Any]:
    base = {
        "question": f"Did paper-fading clustered tourist flow ({scope}) earn a positive settled return per contract after taker fees?",
        "weighting": "contracts, event-clustered SE",
        "stats": stats,
        "thresholds": {"min_events": min_events, "min_fades": min_fades, "t_stat": t_threshold},
    }
    if not _enough(stats, min_events=min_events, min_fades=min_fades):
        return {"verdict": VERDICT_INSUFFICIENT, "reason": f"need >= {min_events} events and >= {min_fades} fades; have {stats.get('n_events', 0)} / {stats.get('fades', 0)}", **base}
    mean, t = stats.get("mean"), stats.get("t_stat")
    if mean is not None and mean > 0 and t is not None and t >= t_threshold:
        return {"verdict": VERDICT_PASS, "reason": f"fade net {mean:+.4f}/contract after fees, event-clustered t={t}", **base}
    if mean is None or mean <= 0:
        reason = f"fade lost money after fees ({mean if mean is not None else 'n/a'}/contract)"
    else:
        reason = f"fade positive ({mean:+.4f}/contract) but not significant at the event level (t={t})"
    return {"verdict": VERDICT_FAIL, "reason": reason, **base}


def strong_beats_weak_verdict(by_regime: dict[str, dict[str, Any]], *, min_events: int) -> dict[str, Any]:
    strong, weak = by_regime.get("strong", {}), by_regime.get("weak", {})
    base = {"question": "Is the fade better when the favourite we buy is priced >= 70c (strong regime) than below it?", "strong": strong, "weak": weak, "thresholds": {"min_events_per_regime": min_events}}
    if strong.get("n_events", 0) < min_events or weak.get("n_events", 0) < min_events:
        return {"verdict": VERDICT_INSUFFICIENT, "reason": "both regimes need enough events", **base}
    s, w = strong.get("mean"), weak.get("mean")
    if s is not None and w is not None and s > w:
        return {"verdict": VERDICT_PASS, "reason": f"strong {s:+.4f} vs weak {w:+.4f} per contract", **base}
    return {"verdict": VERDICT_FAIL, "reason": f"strong {s} not above weak {w} per contract", **base}


def tourist_loses_verdict(markouts: dict[str, dict[str, Any]], *, min_events: int, t_threshold: float = 2.0) -> dict[str, Any]:
    """PASS = the flagged flow is uninformed (its settlement markout is significantly negative).

    FAIL with a positive markout is the adverse-selection failure mode: the
    flow that looked recreational was right, and fading it is the losing side.
    """
    tourist = markouts.get("tourist", {})
    settle = tourist.get("settlement", {})
    short = {h: tourist.get(f"+{h}", {}) for h in MARKOUT_HORIZONS}
    base = {
        "question": "Do tourist-flagged takers lose on the side they bought (settlement markout < 0), i.e. is the flow uninformed?",
        "settlement": settle,
        "short_horizon": short,
        "thresholds": {"min_events": min_events, "t_stat": -t_threshold},
    }
    if settle.get("n_events", 0) < min_events:
        return {"verdict": VERDICT_INSUFFICIENT, "reason": f"need >= {min_events} events with tourist prints", "adverse_selection_detected": None, **base}
    mean, t = settle.get("mean"), settle.get("t_stat")
    informed_short = any(s.get("mean") is not None and s["mean"] > 0 and (s.get("t_stat") or 0) >= t_threshold for s in short.values())
    if mean is not None and mean < 0 and t is not None and t <= -t_threshold:
        return {"verdict": VERDICT_PASS, "reason": f"tourist takers lost {mean:+.4f}/contract at settlement (t={t})", "adverse_selection_detected": informed_short, **base}
    if mean is not None and mean > 0:
        return {"verdict": VERDICT_FAIL, "reason": f"ADVERSE SELECTION: tourist-flagged takers earned {mean:+.4f}/contract at settlement (t={t}); the flow is informed and the fade is on the wrong side", "adverse_selection_detected": True, **base}
    return {"verdict": VERDICT_FAIL, "reason": f"tourist takers' settlement markout {mean} not significantly negative (t={t})", "adverse_selection_detected": informed_short, **base}


def tourist_worse_than_others_verdict(markouts: dict[str, dict[str, Any]], *, min_events: int) -> dict[str, Any]:
    tourist = markouts.get("tourist", {}).get("settlement", {})
    other = markouts.get("other", {}).get("settlement", {})
    base = {"question": "Does the classifier isolate flow that does worse than the rest of the taker tape (equal-weighted by event)?", "tourist": tourist, "other": other, "thresholds": {"min_events": min_events}}
    if tourist.get("n_events", 0) < min_events or other.get("n_events", 0) < min_events:
        return {"verdict": VERDICT_INSUFFICIENT, "reason": "both groups need enough events", **base}
    t_mu, o_mu = tourist.get("mean_equal_weight"), other.get("mean_equal_weight")
    if t_mu is not None and o_mu is not None and t_mu < o_mu:
        return {"verdict": VERDICT_PASS, "reason": f"tourist {t_mu:+.4f} vs other takers {o_mu:+.4f} per contract at settlement", **base}
    return {"verdict": VERDICT_FAIL, "reason": f"tourist {t_mu} not below other takers {o_mu}", **base}


# --------------------------------------------------------------------------
# Ex-post report
# --------------------------------------------------------------------------
def replay_universe(
    markets: list[SettledMarket],
    params: TouristParameters,
    *,
    fee_model: KalshiFeeModel | None = None,
    starting_cash: Decimal = Decimal("1000"),
) -> tuple[list[MarketReplay], PaperLedger]:
    """Replay every settled market in close-time order through one shared ledger.

    The per-order and per-market rails apply on every fade; the daily-loss rail
    is reset per market because the sample spans many days (documented).
    """
    fee_model = fee_model or KalshiFeeModel()
    ledger = PaperLedger(starting_cash=starting_cash, ledger_id=f"{TOURIST_TRACK}:replay")
    ordered = sorted(markets, key=lambda m: (m.close_time or datetime.max.replace(tzinfo=UTC), m.ticker))
    replays = [replay_market(m, params, fee_model, ledger=ledger, risk=RiskManager(TOURIST_RISK_LIMITS)) for m in ordered]
    ledger.snapshot(label="replay")
    return replays, ledger


def expost_report(
    markets: list[SettledMarket],
    params: TouristParameters,
    *,
    fee_model: KalshiFeeModel | None = None,
    min_events: int = 10,
    min_fades: int = 20,
    exclude_final_minutes: int = 30,
) -> dict[str, Any]:
    fee_model = fee_model or KalshiFeeModel()
    replays, ledger = replay_universe(markets, params, fee_model=fee_model)
    fades = [f for r in replays for f in r.fades]
    markouts = _markout_table(replays)
    by_regime = _table_by(fades, lambda f: f.regime)
    by_series = _table_by(fades, lambda f: f.series or "unknown")
    by_category = _table_by(fades, lambda f: f.category)
    all_stats = _fade_stats(fades)
    strong_stats = by_regime.get("strong", _fade_stats([]))
    # End-game prints dominate a newest-first tape; this variant drops fades whose cluster fired
    # inside the final minutes before close (1c longshots on a decided match).
    early = [f for f in fades if f.minutes_to_close is None or f.minutes_to_close >= exclude_final_minutes]
    early_stats = _fade_stats(early)
    early_by_regime = _table_by(early, lambda f: f.regime)
    combinations: dict[str, int] = {}
    refusals: dict[str, int] = {}
    for replay in replays:
        for k, v in replay.combinations.items():
            combinations[k] = combinations.get(k, 0) + v
        for k, v in replay.refusals.items():
            refusals[k] = refusals.get(k, 0) + v
    tourist_trades = sum(r.tourist_trades for r in replays)
    tape_trades = sum(r.tape_trades for r in replays)
    tourist_notional = sum((r.tourist_notional for r in replays), ZERO)
    total_notional = sum((r.total_notional for r in replays), ZERO)
    verdicts = {
        "fade_ev_positive_after_fees": fade_ev_verdict(all_stats, scope="all regimes", min_events=min_events, min_fades=min_fades),
        "strong_regime_fade_ev_positive": fade_ev_verdict(strong_stats, scope="favourite >= 70c", min_events=min_events, min_fades=min_fades),
        "strong_regime_beats_weak": strong_beats_weak_verdict(by_regime, min_events=max(3, min_events // 2)),
        "fade_ev_positive_excluding_final_minutes": {**fade_ev_verdict(early_stats, scope=f"all regimes, clusters >= {exclude_final_minutes} min before close", min_events=min_events, min_fades=min_fades), "exclude_final_minutes": exclude_final_minutes},
        "strong_regime_fade_ev_positive_excluding_final_minutes": {**fade_ev_verdict(early_by_regime.get("strong", _fade_stats([])), scope=f"favourite >= 70c, clusters >= {exclude_final_minutes} min before close", min_events=min_events, min_fades=min_fades), "exclude_final_minutes": exclude_final_minutes},
        "tourist_flow_loses": tourist_loses_verdict(markouts, min_events=min_events),
        "tourist_worse_than_other_takers": tourist_worse_than_others_verdict(markouts, min_events=min_events),
    }
    adverse = verdicts["tourist_flow_loses"].get("adverse_selection_detected")
    return {
        "status": "measured_from_settled_trades" if markets else "no_settled_trades",
        "markets": len(markets),
        "events": len({r.event for r in replays}),
        "markets_with_trades": sum(1 for r in replays if r.tape_trades),
        "markets_with_truncated_trades": sum(1 for m in markets if m.trades_truncated),
        "series": sorted({m.series_ticker for m in markets}),
        "tape": {
            "trades": tape_trades,
            "tourist_trades": tourist_trades,
            "tourist_share_of_trades": round(tourist_trades / tape_trades, 4) if tape_trades else None,
            "taker_notional": _q(total_notional),
            "tourist_notional": _q(tourist_notional),
            "tourist_share_of_notional": _q(tourist_notional / total_notional) if total_notional else None,
            "flag_combinations": dict(sorted(combinations.items())),
        },
        "clusters": sum(r.clusters for r in replays),
        "fades": all_stats,
        "fades_excluding_final_minutes": {**early_stats, "exclude_final_minutes": exclude_final_minutes, "by_regime": early_by_regime},
        "fade_refusals": dict(sorted(refusals.items())),
        "by_regime": by_regime,
        "by_fade_price_band": _table_by(fades, lambda f: fade_price_band(f.price)),
        "by_series": by_series,
        "by_category": by_category,
        "markouts": markouts,
        "adverse_selection": {
            "detected": adverse,
            "definition": (
                "A tourist-flagged taker's bought side is marked out +5 and +20 prints later and at settlement. "
                "Positive markouts mean the flow that looked recreational was informed; the fade is then its "
                "systematic counterparty and loses. Negative markouts are the hypothesis."
            ),
            "tourist": markouts.get("tourist", {}),
            "other_takers": markouts.get("other", {}),
            "by_flag_combination": {k.split(":", 1)[1]: v for k, v in markouts.items() if k.startswith("combo:")},
            "by_category": _markouts_by(replays, lambda r: r.category),
            "by_series": _markouts_by(replays, lambda r: r.series or "unknown"),
        },
        "ledger": ledger.summary(),
        "verdicts": verdicts,
        "fade_rows": [f.as_dict() for f in fades],
        "fee_model": {"taker_rate": fee_model.taker_rate, "note": "taker fee round_up(M*0.07*C*P*(1-P)) per fade; M = series fee_multiplier"},
        "parameters": params.as_dict(),
    }


def not_measured_report(reason: str) -> dict[str, Any]:
    keys = VERDICT_KEYS
    return {
        "status": "not_measured",
        "reason": reason,
        "verdicts": {k: {"verdict": VERDICT_INSUFFICIENT, "reason": reason} for k in keys},
        "adverse_selection": {"detected": None, "definition": "not measured"},
        "how_to_measure": "python -m apps.measure_tourist_fade --network --kalshi-env prod --harvest-trades",
    }


# --------------------------------------------------------------------------
# Live track (open books + recent prints)
# --------------------------------------------------------------------------
def tape_from_market_metadata(market: Market) -> list[TapeTrade]:
    """Fixture markets carry ``metadata.recent_trades``; network markets carry a fetched tape."""
    rows = market.metadata.get("recent_trades")
    return tape_from_rows(rows) if isinstance(rows, list) else []


def _edge_row(track: str, market: Market, ev: FadeEvaluation, *, filled: bool) -> dict[str, Any]:
    signal = ev.signal
    return {
        "track": track,
        "venue": market.venue.value,
        "market": market.market_id,
        "title": market.title,
        "edge_bps": None,
        "admitted": ev.traded,
        "filled": filled,
        "reason": ev.reason,
        "mid": ev.mid,
        "fair_value": None,
        "spread": ev.spread,
        "tape_trades": ev.tape_trades,
        "tourist_trades": ev.tourist_trades,
        "faded_side": signal.side.value if signal else None,
        "cluster_trades": signal.trades if signal else None,
        "cluster_notional": signal.notional if signal else None,
        "cluster_at": signal.at.isoformat() if signal else None,
        "favorite_price": ev.favorite_price,
        "regime": ev.regime,
    }


def settle_resolved_positions(ledger: PaperLedger, resolve: Callable[[str], Outcome | None]) -> list[dict[str, Any]]:
    """Close carried positions whose market now has a published result."""
    rows: list[dict[str, Any]] = []
    for position in list(ledger.open_positions):
        outcome = resolve(position.market_id)
        if outcome is None:
            continue
        before = ledger.realized_pnl
        fill = ledger.settle(position.venue, position.market_id, outcome)
        if fill is None:
            continue
        rows.append({
            "market": position.market_id,
            "outcome": outcome.value,
            "quantity": position.quantity,
            "average_price": position.average_price,
            "realized_pnl": (ledger.realized_pnl - before).quantize(Q4),
        })
    return rows


async def run_fade_the_tourist_track(
    runtime: TrackRuntime,
    *,
    params: TouristParameters | None = None,
    tapes: dict[str, list[TapeTrade]] | None = None,
    as_of: datetime | None = None,
    settled: list[dict[str, Any]] | None = None,
) -> TrackSummary:
    """Evaluate every Kalshi market in the snapshot against its recent prints."""
    params = params or TouristParameters()
    summary = runtime.summary
    snapshot = runtime.snapshots.get(Venue.KALSHI)
    if snapshot is None:
        summary.notes = "No Kalshi snapshot available."
        return summary
    strategy = FadeTouristStrategy(params, portfolio=runtime.ledger.portfolio, risk=runtime.risk)
    tapes = tapes or {}
    regimes: dict[str, dict[str, Any]] = {}
    clusters_seen = 0
    tourist_trades = 0
    tape_trades = 0
    # Both legs of a match see the same flow from opposite sides; one paper position per event.
    event_of = {m.market_id: str(m.metadata.get("event_ticker") or event_of_ticker(m.market_id)) for m in snapshot.markets}
    held_events = {event_of.get(p.market_id, event_of_ticker(p.market_id)) for p in runtime.ledger.open_positions}
    for market in snapshot.markets:
        book = snapshot.book(market)
        tape = tapes.get(market.market_id) or tape_from_market_metadata(market)
        summary.candidates += 1
        if event_of[market.market_id] in held_events and (runtime.ledger.portfolio.get(market.venue, market.market_id) is None or runtime.ledger.portfolio.get(market.venue, market.market_id).quantity == ZERO):
            summary.refuse("already_positioned_event")
            continue
        ev = strategy.evaluate(
            market, book, tape, as_of=as_of,
            open_time=_dt(market.metadata.get("open_time")), close_time=_dt(market.metadata.get("close_time")),
        )
        tape_trades += ev.tape_trades
        tourist_trades += ev.tourist_trades
        clusters_seen += int(ev.signal is not None)
        if not ev.traded:
            summary.refuse(ev.reason)
            if ev.reason not in ("no_tape", "no_tourist_cluster", "market_inactive"):
                summary.edges.append(_edge_row(runtime.name, market, ev, filled=False))
            continue
        summary.admitted += 1
        summary.proposed_orders += len(ev.orders)
        summary.admitted_edges.append(ZERO)
        fills_before = len(summary.fills)
        for order in ev.orders:
            await runtime.submit(order, edge=None)
        if len(summary.fills) > fills_before:
            held_events.add(event_of[market.market_id])
        regime_row = regimes.setdefault(ev.regime or "unknown", {"admitted": 0, "fills": 0, "contracts": ZERO, "notional": ZERO, "fees": ZERO})
        regime_row["admitted"] += 1
        for fill_row in summary.fills[fills_before:]:
            fill_row.update({
                "faded_side": ev.signal.side.value if ev.signal else None,
                "regime": ev.regime,
                "cluster_trades": ev.signal.trades if ev.signal else None,
                "cluster_notional": ev.signal.notional if ev.signal else None,
            })
            regime_row["fills"] += 1
            regime_row["contracts"] += fill_row["qty"]
            regime_row["notional"] += fill_row["qty"] * fill_row["price"]
            regime_row["fees"] += fill_row["fee"]
        summary.edges.append(_edge_row(runtime.name, market, ev, filled=len(summary.fills) > fills_before))
    for row in regimes.values():
        for k in ("contracts", "notional", "fees"):
            row[k] = _q(row[k])
    summary.metrics["parameters"] = params.as_dict()
    summary.metrics["risk_limits"] = {
        "max_notional_per_order": runtime.risk.limits.max_notional_per_order,
        "max_position_per_market": runtime.risk.limits.max_position_per_market,
        "max_daily_loss": runtime.risk.limits.max_daily_loss,
    }
    summary.metrics["tape"] = {"markets_with_tape": sum(1 for m in snapshot.markets if (tapes.get(m.market_id) or tape_from_market_metadata(m))), "trades": tape_trades, "tourist_trades": tourist_trades, "clusters": clusters_seen}
    summary.metrics["by_regime"] = regimes
    summary.metrics["settled_this_run"] = settled or []
    summary.metrics["cash_at_risk"] = {"positions": _q(portfolio_cash_at_risk(runtime.ledger.portfolio)), "cap": params.max_total_cash_at_risk}
    summary.metrics["family"] = TOURIST_FAMILY
    summary.settlement_risk_flag = False
    return summary


def finalize_tourist_metrics(runtime: TrackRuntime) -> None:
    """Per-regime paper PnL from marked fill rows (after :meth:`TrackRuntime.finalize`)."""
    table: dict[str, dict[str, Any]] = {}
    for row in runtime.summary.fills:
        regime = row.get("regime") or "unknown"
        cell = table.setdefault(regime, {"fills": 0, "contracts": ZERO, "notional": ZERO, "fees": ZERO, "paper_pnl": ZERO, "marked": 0})
        cell["fills"] += 1
        cell["contracts"] += row["qty"]
        cell["notional"] += row["qty"] * row["price"]
        cell["fees"] += row["fee"]
        if row.get("paper_pnl") is not None:
            cell["paper_pnl"] += row["paper_pnl"]
            cell["marked"] += 1
    for cell in table.values():
        cell["return_on_notional_bps"] = _bps(cell["paper_pnl"] / cell["notional"]) if cell["notional"] else None
        for k in ("contracts", "notional", "fees", "paper_pnl"):
            cell[k] = _q(cell[k])
    runtime.summary.metrics["paper_pnl_by_regime"] = table


def create_tourist_runtime(snapshots: dict[Venue, VenueSnapshot], *, ledger: PaperLedger | None, starting_cash: Decimal, model_fees: bool) -> TrackRuntime:
    from research.flb import flb_client_factory

    fee_model = KalshiFeeModel() if model_fees else KalshiFeeModel.zero()
    return TrackRuntime.create(
        TOURIST_TRACK,
        snapshots,
        ledger=ledger,
        risk_limits=TOURIST_RISK_LIMITS,
        starting_cash=starting_cash,
        model_fees=model_fees,
        client_factory=flb_client_factory(fee_model),
        mark_method="mid",
    )


# --------------------------------------------------------------------------
# Public reads for the live track
# --------------------------------------------------------------------------
async def fetch_recent_tape(http: httpx.AsyncClient, base_url: str, ticker: str, *, limit: int = 1000, max_pages: int = 2) -> list[TapeTrade]:
    """Newest prints for ``ticker`` from the public ``/markets/trades`` endpoint, returned chronologically."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max(1, max_pages)):
        params: dict[str, Any] = {"ticker": ticker, "limit": min(max(limit, 1), 1000)}
        if cursor:
            params["cursor"] = cursor
        response = await http.get(f"{base_url}/markets/trades", params=params)
        response.raise_for_status()
        payload = response.json()
        rows.extend(r for r in payload.get("trades", []) if isinstance(r, dict))
        cursor = payload.get("cursor") or None
        if not cursor or len(rows) >= limit:
            break
    return tape_from_rows(rows)


async def fetch_market_result(http: httpx.AsyncClient, base_url: str, ticker: str) -> Outcome | None:
    """``yes``/``no`` result of a market, or ``None`` while it is open or resolved otherwise."""
    response = await http.get(f"{base_url}/markets/{ticker}")
    response.raise_for_status()
    market = response.json().get("market") or {}
    result = str(market.get("result") or "").lower()
    return Outcome(result) if result in ("yes", "no") else None
