"""``category_specialist`` paper track: score traders per category, promote, paper-follow, evaluate.

Pipeline for one run (fixture or public-network data, paper-only throughout):

1. fetch trader histories (``research/specialist_sources.py``);
2. settle previously followed bets whose market has resolved (fixture
   ``paper_settlement_outcome`` or the public Data API resolution feed) on the
   track's own :class:`core.ledger.PaperLedger`;
3. score every (trader, category) on its rolling in-category window and
   promote the top decile with positive ROI *and* positive directional Brier
   skill (``strategies/specialist.py``);
4. paper-follow each specialist's open **in-category** bets at the touch,
   recording the YES mid at follow time as the benchmark;
5. evaluate the pre-registered test on the accumulated follow log:
   ``underpowered`` until ``N`` follows have resolved, then ``pass`` / ``fail``.

State (the follow log) is carried across runs in
``artifacts/paper/specialist_follow_state.json``; the full scoreboard is
written to ``specialist_scoreboard_<mode>.json`` by ``apps.measure_all`` and by
this module's CLI::

    python -m research.specialist_scoreboard                  # fixtures (synthetic traders)
    python -m research.specialist_scoreboard --network --traders 10
    python -m research.specialist_scoreboard --json /tmp/specialists.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

from core.config import require_paper_only
from core.types import ONE, ZERO, Market, Order, OrderBook, Outcome, Side, Venue
from research.specialist_sources import (
    CATEGORIES,
    FixtureResolutionOracle,
    NullResolutionOracle,
    PolymarketResolutionOracle,
    ResolutionOracle,
    TraderHistoryBatch,
    TraderHistorySource,
    build_trader_source,
)
from strategies.specialist import (
    STATUS_UNDERPOWERED,
    CategoryScore,
    FollowedBet,
    SpecialistParameters,
    TraderBet,
    categories_of,
    evaluate_follows,
    preregistration,
    promote,
    score_traders,
)

LOGGER = logging.getLogger("specialist_scoreboard")

SPECIALIST_TRACK = "category_specialist"
STATE_SCHEMA_VERSION = "1.0.0"
REPORT_SCHEMA_VERSION = "1.0.0"
STATE_FILE = "specialist_follow_state.json"
Q4 = Decimal("0.0001")

BookFetcher = Callable[[list[TraderBet]], Awaitable[tuple[dict[str, Market], dict[str, OrderBook], list[str]]]]


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Follow state (carried across runs)
# --------------------------------------------------------------------------
@dataclass(slots=True)
class SpecialistState:
    followed: list[FollowedBet] = field(default_factory=list)
    promotions: list[dict[str, Any]] = field(default_factory=list)
    runs: int = 0
    created_at: str = field(default_factory=lambda: _now().isoformat())
    updated_at: str = field(default_factory=lambda: _now().isoformat())

    def already_followed(self, trader: str, market_id: str, direction: Outcome) -> bool:
        return any(
            f.trader == trader and f.market_id == market_id and f.direction is direction for f in self.followed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "paper_only": True,
            "track": SPECIALIST_TRACK,
            "runs": self.runs,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "followed": [f.as_dict() for f in self.followed],
            "promotions": list(self.promotions),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SpecialistState:
        if payload.get("paper_only") is not True:
            raise ValueError("refusing to load specialist state that is not marked paper_only")
        state = cls(
            followed=[FollowedBet.from_dict(item) for item in payload.get("followed", [])],
            promotions=list(payload.get("promotions", [])),
            runs=int(payload.get("runs", 0)),
        )
        state.created_at = str(payload.get("created_at") or state.created_at)
        state.updated_at = str(payload.get("updated_at") or state.updated_at)
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True, default=_json_default) + "\n")

    @classmethod
    def load(cls, path: Path) -> SpecialistState:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def state_path(artifact_dir: Path) -> Path:
    return artifact_dir / "paper" / STATE_FILE


def load_specialist_state(artifact_dir: Path) -> SpecialistState | None:
    path = state_path(artifact_dir)
    return SpecialistState.load(path) if path.exists() else None


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


# --------------------------------------------------------------------------
# Books for the markets a specialist is currently in
# --------------------------------------------------------------------------
def fixture_book_fetcher(markets: list[Market], books: dict[str, OrderBook]) -> BookFetcher:
    """Serve the follow leg from a frozen fixture snapshot (deterministic)."""
    by_id = {m.market_id: m for m in markets}

    async def fetch(bets: list[TraderBet]) -> tuple[dict[str, Market], dict[str, OrderBook], list[str]]:
        found = {b.market_id: by_id[b.market_id] for b in bets if b.market_id in by_id}
        return found, {mid: books.get(mid, OrderBook(market_id=mid)) for mid in found}, []

    return fetch


def market_from_bet(bet: TraderBet) -> Market:
    return Market(
        venue=bet.venue,
        market_id=bet.market_id,
        title=bet.title or bet.market_id,
        active=True,
        yes_token_id=bet.yes_token_id,
        no_token_id=bet.no_token_id,
        metadata={
            "source": "network",
            "category": bet.category,
            "slug": bet.metadata.get("slug"),
            "event_slug": bet.metadata.get("event_slug"),
            "end_date": bet.metadata.get("end_date"),
            "neg_risk": bet.metadata.get("neg_risk"),
            "discovered_by": "specialist_open_position",
        },
    )


def polymarket_book_fetcher() -> BookFetcher:
    """Public CLOB ``POST /books`` for the YES token of every candidate market."""

    async def fetch(bets: list[TraderBet]) -> tuple[dict[str, Market], dict[str, OrderBook], list[str]]:
        from venues.polymarket import PolymarketClient

        markets: dict[str, Market] = {}
        for bet in bets:
            if bet.venue is Venue.POLYMARKET and bet.yes_token_id and bet.market_id not in markets:
                markets[bet.market_id] = market_from_bet(bet)
        if not markets:
            return {}, {}, []
        client = PolymarketClient(paper=True, use_fixtures=False)
        errors: list[str] = []
        books: dict[str, OrderBook] = {}
        try:
            by_token = await client.get_books([m.yes_token_id for m in markets.values() if m.yes_token_id])
        except Exception as exc:  # a dead endpoint must not kill the track
            errors.append(f"books: {type(exc).__name__}: {exc}")
            by_token = {}
        finally:
            await client.close()
        for market in markets.values():
            raw = by_token.get(market.yes_token_id or "")
            if raw is None:
                errors.append(f"book_missing[{market.market_id[:12]}]")
                continue
            books[market.market_id] = OrderBook(
                market_id=market.market_id, bids=raw.bids, asks=raw.asks, timestamp=raw.timestamp
            )
        return markets, books, errors

    return fetch


# --------------------------------------------------------------------------
# Track runner
# --------------------------------------------------------------------------
def track_status(batch: TraderHistoryBatch, *, specialists_count: int, follows_this_run: int) -> str:
    if batch.source == "none":
        return "no_trader_source"
    if not batch.bets and batch.errors:
        return "source_errors"
    if not batch.bets:
        return "no_trader_history"
    if batch.source == "fixture":
        return "fixture_synthetic"
    if specialists_count == 0:
        return "no_specialists_promoted"
    if follows_this_run == 0:
        return "specialists_without_admissible_next_bets"
    return "following_specialists"


def _touch(book: OrderBook, direction: Outcome) -> Decimal | None:
    view = book.for_outcome(direction)
    return view.best_ask.price if view.best_ask else None


def _past_end_date(bet: TraderBet, as_of: datetime) -> bool:
    """Open positions on markets whose end date has passed sit on stale books."""
    raw = bet.metadata.get("end_date")
    if not raw:
        return False
    try:
        end = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    # A bare date means the event day; give it the whole day before calling it stale.
    if len(str(raw)) <= 10:
        end = end.replace(hour=23, minute=59, second=59)
    return end < as_of


async def run_category_specialist_track(
    runtime: Any,
    *,
    source: TraderHistorySource,
    parameters: SpecialistParameters | None = None,
    state: SpecialistState | None = None,
    oracle: ResolutionOracle | None = None,
    book_fetcher: BookFetcher | None = None,
    as_of: datetime | None = None,
    mode: str = "fixtures",
) -> Any:
    """Run the lane against ``runtime`` (a ``research.scoreboard.TrackRuntime``)."""
    summary = runtime.summary
    params = parameters or SpecialistParameters()
    state = state if state is not None else SpecialistState()
    oracle = oracle or NullResolutionOracle()
    as_of = as_of or _now()
    venue = Venue.POLYMARKET
    snapshot = runtime.snapshots.get(venue)
    client = runtime.clients.get(venue)

    # 1. trader histories
    try:
        batch = await source.fetch(as_of=as_of)
    except Exception as exc:  # a source must never take the scoreboard down
        batch = TraderHistoryBatch(source=getattr(source, "name", type(source).__name__))
        batch.errors.append(f"fetch: {type(exc).__name__}: {exc}")

    # 2. settle followed bets that have since resolved. Fixture and network
    #    follows share one state file but never one evaluation: synthetic
    #    fixture outcomes must not count toward the network hypothesis.
    settled: list[dict[str, Any]] = []
    mode_follows = [f for f in state.followed if f.mode == mode]
    pending = [f for f in mode_follows if not f.resolved]
    oracle_errors: list[str] = []
    if pending:
        try:
            outcomes = await oracle.resolve(sorted({f.market_id for f in pending}))
        except Exception as exc:
            outcomes = {}
            oracle_errors.append(f"resolve: {type(exc).__name__}: {exc}")
        oracle_errors.extend(getattr(oracle, "errors", []) or [])
        settled_markets: set[str] = set()
        for follow in pending:
            outcome = outcomes.get(follow.market_id)
            if outcome is None:
                continue
            follow.resolved = True
            follow.outcome = outcome
            follow.resolved_at = as_of
            if follow.market_id not in settled_markets:
                settled_markets.add(follow.market_id)
                fill = runtime.ledger.settle(follow.venue, follow.market_id, outcome)
                settled.append(
                    {
                        "market_id": follow.market_id,
                        "outcome": outcome.value,
                        "settlement_fill": fill.quantity if fill else ZERO,
                        "followers": [f.follow_id for f in pending if f.market_id == follow.market_id],
                    }
                )

    # 3. score and promote
    scores = promote(score_traders(batch.bets, params), params)
    promoted: dict[tuple[str, str], CategoryScore] = {(s.trader, s.category): s for s in scores if s.promoted}
    promoted_traders = {trader for trader, _ in promoted}
    for score in promoted.values():
        state.promotions.append(
            {
                "run": state.runs + 1,
                "promoted_at": as_of.isoformat(),
                "trader": score.trader,
                "category": score.category,
                "roi": score.roi.quantize(Q4) if score.roi is not None else None,
                "brier_skill": score.brier_skill.quantize(Q4) if score.brier_skill is not None else None,
                "resolved_bets": score.resolved_bets,
                "rank": score.rank,
                "scored_in_category": score.scored_in_category,
            }
        )

    # 4. follow each specialist's open in-category bets
    open_bets = sorted((b for b in batch.bets if not b.resolved), key=lambda b: (b.trader, b.bet_id))
    summary.candidates = len(open_bets)
    wanted = [
        b
        for b in open_bets
        if (b.trader, b.category) in promoted and not state.already_followed(b.trader, b.market_id, b.direction)
    ]
    markets: dict[str, Market] = {}
    books: dict[str, OrderBook] = {}
    book_errors: list[str] = []
    if wanted and book_fetcher is not None:
        try:
            markets, books, book_errors = await book_fetcher(wanted)
        except Exception as exc:
            book_errors.append(f"book_fetcher: {type(exc).__name__}: {exc}")
    if client is not None and snapshot is not None:
        known = {m.market_id for m in snapshot.markets}
        for market_id, market in markets.items():
            client._market_cache[market_id] = market
            if market_id not in known:
                snapshot.markets.append(market)
            snapshot.books[market_id] = books.get(market_id, OrderBook(market_id=market_id))

    follows: list[FollowedBet] = []
    attempts: list[dict[str, Any]] = []
    seen_this_run: set[tuple[str, Outcome]] = set()
    for bet in open_bets:
        row = {
            "bet_id": bet.bet_id,
            "trader": bet.trader,
            "category": bet.category,
            "market_id": bet.market_id,
            "title": bet.title,
            "direction": bet.direction.value,
            "trader_entry_price": bet.entry_price,
            "trader_size": bet.size,
        }
        reason: str | None = None
        mid: Decimal | None = None
        touch: Decimal | None = None
        if (bet.trader, bet.category) not in promoted:
            reason = "out_of_category" if bet.trader in promoted_traders else "trader_not_promoted"
        elif state.already_followed(bet.trader, bet.market_id, bet.direction):
            reason = "already_followed"
        elif (bet.market_id, bet.direction) in seen_this_run:
            reason = "duplicate_market_direction"
        elif bet.market_id not in markets:
            reason = "market_not_in_snapshot"
        elif not markets[bet.market_id].active:
            reason = "market_inactive"
        elif _past_end_date(bet, as_of):
            reason = "market_past_end_date"
        else:
            book = books.get(bet.market_id, OrderBook(market_id=bet.market_id))
            mid = book.mid_price
            touch = _touch(book, bet.direction)
            position = runtime.ledger.portfolio.get(bet.venue, bet.market_id)
            if mid is None:
                reason = "no_two_sided_book"
            elif (book.spread or ZERO) > params.max_spread:
                reason = "spread_too_wide"
            elif touch is None:
                reason = "no_ask_for_direction"
            elif not ZERO < touch < ONE:
                reason = "touch_at_bound"
            elif position is not None and position.quantity != ZERO:
                reason = "position_already_held"
        if reason is not None or mid is None or touch is None:
            summary.refuse(reason or "no_two_sided_book")
            row["reason"] = reason or "no_two_sided_book"
            attempts.append(row)
            continue
        seen_this_run.add((bet.market_id, bet.direction))
        order = Order(
            venue=bet.venue,
            market_id=bet.market_id,
            side=Side.BUY,
            outcome=bet.direction,
            quantity=params.follow_quantity,
            price=touch,
            metadata={
                "strategy": SPECIALIST_TRACK,
                "price_signal_status": "specialist_direction_at_touch",
                "trader": bet.trader,
                "category": bet.category,
                "source_bet_id": bet.bet_id,
                "mid_at_follow": str(mid.quantize(Q4)),
                "trader_entry_price": str(bet.entry_price),
            },
        )
        summary.proposed_orders += 1
        report = await runtime.submit(order, edge=None)
        if report is None:
            row["reason"] = "risk_refused"
            attempts.append(row)
            continue
        if not report.fills:
            summary.refuse("no_fill")
            row["reason"] = "no_fill"
            attempts.append(row)
            continue
        filled = sum((f.quantity for f in report.fills), ZERO)
        avg_price = sum((f.quantity * f.price for f in report.fills), ZERO) / filled
        follow = FollowedBet(
            follow_id=f"follow-{state.runs + 1}-{len(state.followed) + 1}",
            trader=bet.trader,
            category=bet.category,
            venue=bet.venue,
            market_id=bet.market_id,
            direction=bet.direction,
            title=bet.title,
            followed_at=as_of,
            mid_at_follow=mid.quantize(Q4),
            quantity=filled,
            fill_price=avg_price.quantize(Q4),
            fee=sum((f.fee for f in report.fills), ZERO),
            source_bet_id=bet.bet_id,
            trader_entry_price=bet.entry_price,
            mode=mode,
        )
        state.followed.append(follow)
        mode_follows.append(follow)
        follows.append(follow)
        summary.admitted += 1
        row.update({"reason": "followed", "follow_id": follow.follow_id, "fill_price": follow.fill_price,
                    "mid_at_follow": follow.mid_at_follow, "quantity": filled})
        attempts.append(row)

    state.runs += 1
    state.updated_at = as_of.isoformat()

    # 5. pre-registered evaluation over this mode's whole follow log
    pooled = evaluate_follows(mode_follows, params)
    by_category = {c: evaluate_follows(mode_follows, params, category=c) for c in categories_of(mode_follows)}

    # 6. metrics
    category_stats: dict[str, dict[str, Any]] = {}
    for score in scores:
        stats = category_stats.setdefault(
            score.category, {"traders": 0, "scored": 0, "promoted": 0, "resolved_bets": 0, "open_bets": 0}
        )
        stats["traders"] += 1
        stats["scored"] += int(score.rank is not None)
        stats["promoted"] += int(score.promoted)
        stats["resolved_bets"] += score.total_resolved
        stats["open_bets"] += score.open_bets
    coverage = {c: 0 for c in CATEGORIES}
    for bet in batch.bets:
        coverage[bet.category] = coverage.get(bet.category, 0) + 1
    specialists_rows = [s.as_dict() for s in scores if s.promoted]
    summary.metrics.update(
        {
            "status": track_status(batch, specialists_count=len(promoted), follows_this_run=len(follows)),
            "venue_scope": "polymarket_only (Kalshi publishes no per-trader data)",
            "source": {
                "name": batch.source,
                "traders": len(batch.traders),
                "bets": len(batch.bets),
                "resolved_bets": sum(1 for b in batch.bets if b.resolved),
                "open_bets": len(open_bets),
                "requests": batch.requests,
                "errors": batch.errors,
                "note": batch.note,
                "fetched_at": batch.fetched_at,
                "as_of": as_of.isoformat(),
            },
            "parameters": params.as_dict(),
            "preregistration": preregistration(params),
            "traders": [t.as_dict() for t in batch.traders],
            "categories": dict(sorted(category_stats.items())),
            "taxonomy_coverage": {k: v for k, v in coverage.items() if v},
            "scoreboard": [s.as_dict() for s in sorted(scores, key=lambda s: (s.category, s.rank or 10**6, s.trader))],
            "specialists": specialists_rows,
            "follow_attempts": attempts,
            "follows_this_run": [f.as_dict() for f in follows],
            "settled_this_run": settled,
            "oracle": {"name": getattr(oracle, "name", type(oracle).__name__), "errors": oracle_errors},
            "book_errors": book_errors,
            "follow_log": {
                "mode": mode,
                "total": len(mode_follows),
                "resolved": sum(1 for f in mode_follows if f.resolved),
                "pending": sum(1 for f in mode_follows if not f.resolved),
                "other_modes": len(state.followed) - len(mode_follows),
                "runs": state.runs,
            },
            "evaluation": {
                "pooled": pooled.as_dict(),
                "by_category": {c: ev.as_dict() for c, ev in sorted(by_category.items())},
            },
            "follow_state": state.to_dict(),
            "not_validated": not_validated(mode),
        }
    )
    summary.settlement_risk_flag = False
    return summary


def not_validated(mode: str) -> list[str]:
    items = [
        "persistence of in-category skill (the hypothesis itself): pooled sample is underpowered until N resolved follows",
        "category taxonomy: keyword / slug-prefix heuristic; 'other' absorbs unmatched markets",
        "market mid at the trader's entry is unavailable from the Data API; entry price is the Brier benchmark proxy on network",
        "open-position placement time is unknown on network (positions endpoint has no timestamp); 'next bet' means currently open",
        "closed-position size uses totalBought and venue-reported realizedPnl; partial exits before resolution are excluded; resolved-but-unredeemed positions count as resolved",
        "leaderboard selection by monthly volume; the top-50 cap and one-month window are operator choices, not calibrated",
        "taker fees on followed network markets are not modelled (Data API rows carry no feeType); follow PnL is pre-fee",
    ]
    if mode == "fixtures":
        items.insert(0, "fixture traders and outcomes are SYNTHETIC; fixture results prove the arithmetic and the rails only")
    return items


# --------------------------------------------------------------------------
# Standalone report + CLI
# --------------------------------------------------------------------------
def build_specialist_report(
    summary: Any, *, mode: str, measured_at: str, run_id: str | None = None, source: str = "measured"
) -> dict[str, Any]:
    """The full specialist scoreboard artifact (``specialist_scoreboard_<mode>.json``)."""
    if source == "sample":
        raise ValueError("generated specialist reports are never samples")
    m = summary.metrics
    pooled = m.get("evaluation", {}).get("pooled", {})
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "specialist_scoreboard",
        "meta": {
            "source": source,
            "paper_only": True,
            "mode": mode,
            "measured_at": measured_at,
            "generated_at": _now().isoformat(),
            "run_id": run_id,
            "track": SPECIALIST_TRACK,
            "status": m.get("status"),
            "venue_scope": m.get("venue_scope"),
            "pnl_source": "core.ledger.PaperLedger",
            "data_source": m.get("source", {}),
            "oracle": m.get("oracle", {}),
        },
        "preregistration": m.get("preregistration", {}),
        "parameters": m.get("parameters", {}),
        "totals": {
            "traders": m.get("source", {}).get("traders", 0),
            "bets": m.get("source", {}).get("bets", 0),
            "resolved_bets": m.get("source", {}).get("resolved_bets", 0),
            "open_bets": summary.candidates,
            "specialists": len(m.get("specialists", [])),
            "follows_this_run": summary.admitted,
            "paper_fills": summary.paper_fills,
            "follow_log_total": m.get("follow_log", {}).get("total", 0),
            "follow_log_resolved": m.get("follow_log", {}).get("resolved", 0),
            "follow_log_pending": m.get("follow_log", {}).get("pending", 0),
            "evaluation_status": pooled.get("status"),
            "powered": pooled.get("powered", False),
            "refused_by_reason": dict(sorted(summary.refused_by_reason.items())),
            "ledger_total_pnl": summary.ledger.get("total_pnl") if summary.ledger else None,
        },
        "categories": m.get("categories", {}),
        "taxonomy_coverage": m.get("taxonomy_coverage", {}),
        "scoreboard": m.get("scoreboard", []),
        "specialists": m.get("specialists", []),
        # Every open bet of a non-promoted trader is one 'trader_not_promoted' row; on
        # network that is hundreds of rows carrying no information beyond the count.
        "follow_attempts": [a for a in m.get("follow_attempts", []) if a.get("reason") != "trader_not_promoted"],
        "follow_attempts_by_reason": _count_by_reason(m.get("follow_attempts", [])),
        "follows_this_run": m.get("follows_this_run", []),
        "settled_this_run": m.get("settled_this_run", []),
        "follow_log": [f for f in m.get("follow_state", {}).get("followed", []) if f.get("mode") == mode],
        "follow_log_other_modes": m.get("follow_log", {}).get("other_modes", 0),
        "evaluation": m.get("evaluation", {}),
        "ledger": summary.ledger,
        "not_validated": m.get("not_validated", []),
    }


def _count_by_reason(attempts: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for attempt in attempts:
        reason = str(attempt.get("reason"))
        out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items()))


def specialist_finding(summary: Any) -> dict[str, Any]:
    """Compact headline for ``findings.category_specialist`` on the main scoreboard."""
    m = summary.metrics
    pooled = m.get("evaluation", {}).get("pooled", {})
    return {
        "status": m.get("status", "not_run"),
        "source": m.get("source", {}).get("name"),
        "traders": m.get("source", {}).get("traders", 0),
        "resolved_bets": m.get("source", {}).get("resolved_bets", 0),
        "open_bets": summary.candidates,
        "specialists": len(m.get("specialists", [])),
        "follows_this_run": summary.admitted,
        "follow_log_resolved": m.get("follow_log", {}).get("resolved", 0),
        "follow_log_pending": m.get("follow_log", {}).get("pending", 0),
        "preregistered_n": m.get("parameters", {}).get("preregistered_n"),
        "evaluation_status": pooled.get("status", "no_follows"),
        "hit_rate": pooled.get("hit_rate"),
        "mean_excess_vs_mid": pooled.get("mean_excess_vs_mid"),
        "sign_test_p": pooled.get("sign_test_p"),
        "hypothesis_validated": pooled.get("status") == "pass",
        "note": (
            "Pass/fail is only declared once the pre-registered N resolved follows exist; "
            "see docs/SPECIALIST_SCOREBOARD.md."
        ),
    }


def default_oracle_and_books(
    *, use_fixtures: bool, fixture_markets: list[Market], fixture_books: dict[str, OrderBook]
) -> tuple[ResolutionOracle, BookFetcher]:
    """Fixture runs settle and fill from the frozen fixture; network runs read public endpoints."""
    if use_fixtures:
        return FixtureResolutionOracle(fixture_settlements(fixture_markets)), fixture_book_fetcher(
            fixture_markets, fixture_books
        )
    return PolymarketResolutionOracle(), polymarket_book_fetcher()


async def measure_specialist_lane(
    *,
    use_fixtures: bool = True,
    traders: int | None = None,
    window: str = "month",
    parameters: SpecialistParameters | None = None,
    state: SpecialistState | None = None,
    source: TraderHistorySource | None = None,
    ledger: Any | None = None,
    model_fees: bool = True,
) -> tuple[dict[str, Any], SpecialistState, Any]:
    """Run only this lane and return ``(report, state, ledger)``."""
    from research.scoreboard import DEFAULT_RISK_LIMITS, DEFAULT_STARTING_CASH, TrackRuntime, VenueSnapshot
    from venues.polymarket import PolymarketClient

    require_paper_only(SPECIALIST_TRACK)
    mode = "fixtures" if use_fixtures else "network"
    source = source or build_trader_source(use_fixtures=use_fixtures, traders=traders, window=window)
    state = state or SpecialistState()
    markets: list[Market] = []
    books: dict[str, OrderBook] = {}
    if use_fixtures:
        poly = PolymarketClient(paper=True, use_fixtures=True)
        markets = await poly.list_markets(limit=50)
        books = {m.market_id: await poly.get_order_book(m) for m in markets}
        await poly.close()
    oracle, fetcher = default_oracle_and_books(use_fixtures=use_fixtures, fixture_markets=markets, fixture_books=books)
    runtime = TrackRuntime.create(
        SPECIALIST_TRACK,
        {Venue.POLYMARKET: VenueSnapshot(venue=Venue.POLYMARKET, source="fixture" if use_fixtures else "network")},
        ledger=ledger,
        risk_limits=DEFAULT_RISK_LIMITS,
        starting_cash=DEFAULT_STARTING_CASH,
        model_fees=model_fees,
    )
    summary = await run_category_specialist_track(
        runtime, source=source, parameters=parameters, state=state, oracle=oracle, book_fetcher=fetcher, mode=mode
    )
    runtime.finalize(label=f"specialist:{mode}")
    report = build_specialist_report(summary, mode=mode, measured_at=_now().isoformat())
    return report, state, runtime.ledger


def fixture_settlements(markets: list[Market]) -> dict[str, Outcome]:
    out: dict[str, Outcome] = {}
    for market in markets:
        raw = market.metadata.get("paper_settlement_outcome")
        if raw in ("yes", "no"):
            out[market.market_id] = Outcome(raw)
    return out


def _fmt(value: Any, width: int = 8) -> str:
    if value is None:
        return f"{'-':>{width}}"
    if isinstance(value, Decimal):
        return f"{float(value):>{width}.4f}"
    return f"{str(value):>{width}}"


def print_report(report: dict[str, Any]) -> None:
    meta, totals = report["meta"], report["totals"]
    print(f"\ncategory_specialist ({meta['mode']}) status={meta['status']} source={meta['data_source'].get('name')}")
    for error in meta["data_source"].get("errors", [])[:5]:
        print(f"  source error: {error}")
    header = f"{'trader':<22}{'category':<18}{'n':>4}{'notional':>14}{'roi':>9}{'brier':>9}{'hit':>8}{'rank':>6}  verdict"
    print(header)
    print("-" * len(header))
    for row in report["scoreboard"]:
        verdict = "PROMOTED" if row["promoted"] else ",".join(row["reasons"]) or "-"
        notional = f"{float(row['notional']):>14,.0f}" if row["notional"] is not None else f"{'-':>14}"
        print(
            f"{row['trader'][:21]:<22}{row['category']:<18}{row['resolved_bets']:>4}{notional}"
            f"{_fmt(row['roi'], 9)}{_fmt(row['brier_skill'], 9)}{_fmt(row['hit_rate'])}{_fmt(row['rank'], 6)}  {verdict}"
        )
    print(f"\nfollow attempts this run: {sum(report['follow_attempts_by_reason'].values())}  refused={totals['refused_by_reason']}")
    for row in report["follows_this_run"]:
        print(
            f"  followed {row['trader'][:18]:<18} {row['category']:<14} {row['direction']:<3} {row['quantity']:>6} @ "
            f"{row['fill_price']} (mid {row['mid_at_follow']})  {str(row['title'])[:48]}"
        )
    pooled = report["evaluation"].get("pooled", {})
    print(
        f"\nevaluation (pooled): status={pooled.get('status')} resolved={pooled.get('n_resolved')} pending={pooled.get('n_pending')} "
        f"wins={pooled.get('wins')} hit_rate={pooled.get('hit_rate')} mean_excess_vs_mid={pooled.get('mean_excess_vs_mid')} "
        f"sign_test_p={pooled.get('sign_test_p')} N={pooled.get('preregistered_n')}"
    )
    if pooled.get("note"):
        print(f"  {pooled['note']}")
    ledger = report.get("ledger") or {}
    print(
        f"ledger: realized={ledger.get('realized_pnl')} unrealized={ledger.get('unrealized_pnl')} "
        f"fees={ledger.get('fees_paid')} equity={ledger.get('equity')}"
    )
    print("NOT validated: " + "; ".join(report["not_validated"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", action="store_true", help="read the public Polymarket Data API + CLOB books (paper fills stay local)")
    parser.add_argument("--traders", type=int, default=25, help="wallets from the volume leaderboard on network runs (0 disables)")
    parser.add_argument("--window", default="month", help="leaderboard window: day, week, month, all")
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts"),
                        help="follow state + ledger live in <dir>/paper/, the report in <dir>/specialist_scoreboard_<mode>.json")
    parser.add_argument("--no-persist", action="store_true", help="fresh follow state and ledger; write nothing under paper/")
    parser.add_argument("--json", type=Path, default=None, help="also write the full report here")
    parser.add_argument("--no-fees", action="store_true")
    parser.add_argument("--preregistered-n", type=int, default=None, help="override the pre-registered N (documented as a deviation)")
    args = parser.parse_args()
    require_paper_only(SPECIALIST_TRACK)
    from core.ledger import PaperLedger

    mode = "network" if args.network else "fixtures"
    params = SpecialistParameters(preregistered_n=args.preregistered_n) if args.preregistered_n else None
    ledger_file = args.artifact_dir / "paper" / f"ledger_{SPECIALIST_TRACK}.json"
    state = None if args.no_persist else load_specialist_state(args.artifact_dir)
    ledger = None if args.no_persist or not ledger_file.exists() else PaperLedger.load(ledger_file)
    report, state, ledger = asyncio.run(
        measure_specialist_lane(
            use_fixtures=not args.network,
            traders=args.traders,
            window=args.window,
            parameters=params,
            state=state,
            ledger=ledger,
            model_fees=not args.no_fees,
        )
    )
    print_report(report)
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    report_file = args.artifact_dir / f"specialist_scoreboard_{mode}.json"
    report_file.write_text(json.dumps(report, default=_json_default, indent=2, sort_keys=True) + "\n")
    print(f"report: {report_file}")
    if not args.no_persist:
        state.save(state_path(args.artifact_dir))
        ledger.save(ledger_file)
        print(f"state: {state_path(args.artifact_dir)}  ledger: {ledger_file}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, default=_json_default, indent=2, sort_keys=True) + "\n")
        print(f"report: {args.json}")


if __name__ == "__main__":
    main()


__all__ = [
    "SPECIALIST_TRACK",
    "STATUS_UNDERPOWERED",
    "SpecialistState",
    "build_specialist_report",
    "fixture_book_fetcher",
    "fixture_settlements",
    "load_specialist_state",
    "measure_specialist_lane",
    "polymarket_book_fetcher",
    "run_category_specialist_track",
    "specialist_finding",
    "state_path",
]
