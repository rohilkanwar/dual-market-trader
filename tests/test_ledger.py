from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.ledger import SETTLEMENT_ORDER_ID, PaperLedger
from core.types import Fill, OrderBook, Outcome, PriceLevel, Side, Venue

D = Decimal
K = Venue.KALSHI


def fill(
    qty: str,
    price: str,
    *,
    side: Side = Side.BUY,
    outcome: Outcome = Outcome.YES,
    market: str = "M",
    fee: str = "0",
    venue: Venue = K,
) -> Fill:
    return Fill(
        venue=venue,
        market_id=market,
        order_id="o",
        side=side,
        outcome=outcome,
        quantity=D(qty),
        price=D(price),
        fee=D(fee),
    )


def book(bid: str, ask: str, market: str = "M") -> OrderBook:
    return OrderBook(market, bids=(PriceLevel(D(bid), D("10")),), asks=(PriceLevel(D(ask), D("10")),))


def assert_identity(ledger: PaperLedger) -> None:
    assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl


def test_zero_fill_run_is_flat_and_explicitly_zero() -> None:
    ledger = PaperLedger(starting_cash=D("1000"))
    point = ledger.snapshot(label="empty")

    assert ledger.cash == D("1000")
    assert ledger.realized_pnl == 0 and ledger.unrealized_pnl == 0
    assert point.equity == D("1000") and point.drawdown == 0
    summary = ledger.summary()
    assert summary["open_positions"] == 0 and summary["fills"] == 0
    assert summary["total_pnl"] == D("0.0000")
    assert_identity(ledger)


def test_long_yes_marks_to_market_and_realizes_on_close() -> None:
    ledger = PaperLedger(starting_cash=D("100"))
    ledger.record_fill(fill("10", "0.40"))
    assert ledger.cash == D("96")
    # Fill price is the default mark: zero unrealized until a book arrives.
    assert ledger.unrealized_pnl == 0

    ledger.mark(K, "M", D("0.50"))
    assert ledger.unrealized_pnl == D("1.0")
    assert ledger.equity == D("101")
    assert_identity(ledger)

    ledger.record_fill(fill("10", "0.55", side=Side.SELL))
    assert ledger.cash == D("101.5")
    assert ledger.realized_pnl == D("1.5")
    assert ledger.open_positions == []
    assert ledger.unrealized_pnl == 0
    assert_identity(ledger)


def test_partial_close_realizes_only_closed_portion() -> None:
    ledger = PaperLedger(starting_cash=D("100"))
    ledger.record_fill(fill("10", "0.40"))
    ledger.record_fill(fill("4", "0.50", side=Side.SELL))
    ledger.mark(K, "M", D("0.50"))

    position = ledger.open_positions[0]
    assert position.quantity == D("6")
    assert position.average_price == D("0.40")
    assert ledger.realized_pnl == D("0.4")       # 4 * (0.50 - 0.40)
    assert ledger.unrealized_pnl == D("0.6")     # 6 * (0.50 - 0.40)
    assert ledger.cash == D("98")                # 100 - 4 + 2
    assert_identity(ledger)


def test_adverse_mark_produces_negative_unrealized_and_drawdown() -> None:
    ledger = PaperLedger(starting_cash=D("100"))
    ledger.record_fill(fill("10", "0.60"))
    ledger.snapshot(label="entry")
    ledger.mark(K, "M", D("0.45"))
    point = ledger.snapshot(label="adverse")

    assert ledger.unrealized_pnl == D("-1.5")
    assert point.equity == D("98.5")
    assert point.drawdown == D("1.5")
    assert ledger.max_drawdown == D("1.5")

    ledger.mark(K, "M", D("0.70"))
    ledger.snapshot(label="recovery")
    assert ledger.max_drawdown == D("1.5")  # drawdown never shrinks retroactively
    assert ledger.peak_equity == D("101")
    assert_identity(ledger)


def test_buying_no_is_a_short_yes_liability() -> None:
    ledger = PaperLedger(starting_cash=D("100"))
    ledger.record_fill(fill("10", "0.40", outcome=Outcome.NO))  # YES-equivalent 0.60

    position = ledger.open_positions[0]
    assert position.quantity == D("-10")
    assert position.average_price == D("0.60")
    assert ledger.cash == D("106")                 # receives 10 * 0.60 under the liability model
    assert ledger.position_value(position) == D("-6.0")
    assert ledger.equity == D("100")

    ledger.mark(K, "M", D("0.50"))                 # YES got cheaper: NO holder gains
    assert ledger.unrealized_pnl == D("1.0")
    assert_identity(ledger)

    ledger.settle(K, "M", Outcome.NO)
    assert ledger.realized_pnl == D("6.0")         # paid 10 * 0.40, received 10 * 1.00
    assert ledger.equity == D("106")
    assert ledger.cash == D("106")                 # short YES liability settles at zero
    assert ledger.open_positions == []
    assert ledger.fills[-1].order_id == SETTLEMENT_ORDER_ID
    assert_identity(ledger)


def test_settlement_against_a_long_that_loses() -> None:
    ledger = PaperLedger(starting_cash=D("50"))
    ledger.record_fill(fill("10", "0.70"))
    ledger.settle(K, "M", Outcome.NO)

    assert ledger.realized_pnl == D("-7.0")
    assert ledger.equity == D("43")
    assert ledger.summary()["settlement_fills"] == 1
    assert_identity(ledger)


def test_fees_reduce_cash_and_realized_pnl_immediately() -> None:
    ledger = PaperLedger(starting_cash=D("100"))
    ledger.record_fill(fill("10", "0.50", fee="0.18"))

    assert ledger.cash == D("94.82")
    assert ledger.realized_pnl == D("-0.18")
    assert ledger.fees_paid == D("0.18")
    assert ledger.equity == D("99.82")
    assert_identity(ledger)


def test_mark_from_book_mid_and_conservative() -> None:
    ledger = PaperLedger()
    ledger.record_fill(fill("10", "0.50"))
    assert ledger.mark_from_book(K, "M", book("0.48", "0.52")) == D("0.50")
    assert ledger.mark_from_book(K, "M", book("0.48", "0.52"), method="conservative") == D("0.48")
    ledger.record_fill(fill("20", "0.50", side=Side.SELL))  # now short 10
    assert ledger.mark_from_book(K, "M", book("0.48", "0.52"), method="conservative") == D("0.52")
    assert ledger.mark_from_book(K, "M", OrderBook("M")) is None
    assert ledger.mark_from_book(K, "M", OrderBook("M", bids=(PriceLevel(D("0.3"), D("1")),))) == D("0.3")


def test_unmarked_positions_are_counted_not_hidden() -> None:
    ledger = PaperLedger()
    ledger.record_fill(fill("5", "0.30"))
    del ledger.marks[(K, "M")]
    summary = ledger.summary()
    assert summary["unmarked_positions"] == 1
    assert summary["unrealized_pnl"] == D("0.0000")


def test_multi_market_multi_venue_aggregation() -> None:
    ledger = PaperLedger(starting_cash=D("1000"))
    ledger.record_fill(fill("10", "0.40", market="A"))
    ledger.record_fill(fill("20", "0.30", market="B", venue=Venue.POLYMARKET, outcome=Outcome.NO))
    ledger.mark(K, "A", D("0.45"))
    ledger.mark(Venue.POLYMARKET, "B", D("0.75"))  # YES-equiv entry 0.70 -> loss for the NO holder

    assert ledger.unrealized_pnl == D("0.5") + D("-1.0")
    assert ledger.gross_notional == D("4.5") + D("15.0")
    assert ledger.net_exposure == D("4.5") - D("15.0")
    summary = ledger.summary()
    assert {c["venue"] for c in summary["concentration"]} == {"kalshi", "polymarket"}
    assert sum(c["weight"] for c in summary["concentration"]) == pytest.approx(1, abs=1e-3)
    assert_identity(ledger)


def test_round_trip_persistence(tmp_path: Path) -> None:
    ledger = PaperLedger(starting_cash=D("500"), ledger_id="t")
    ledger.record_fill(fill("10", "0.40", fee="0.05"))
    ledger.record_fill(fill("3", "0.45", side=Side.SELL))
    ledger.mark(K, "M", D("0.50"))
    ledger.snapshot(label="one")
    ledger.mark(K, "M", D("0.35"))
    ledger.snapshot(label="two")
    path = tmp_path / "ledger.json"
    ledger.save(path)

    loaded = PaperLedger.load(path)
    assert loaded.ledger_id == "t"
    assert loaded.cash == ledger.cash
    assert loaded.realized_pnl == ledger.realized_pnl
    assert loaded.unrealized_pnl == ledger.unrealized_pnl
    assert loaded.max_drawdown == ledger.max_drawdown
    assert [p.equity for p in loaded.equity_curve] == [p.equity for p in ledger.equity_curve]
    assert len(loaded.fills) == 2 and loaded.fills[0].fee == D("0.05")
    assert loaded.summary()["positions"] == ledger.summary()["positions"]
    assert_identity(loaded)


def test_refuses_to_load_non_paper_ledger() -> None:
    with pytest.raises(ValueError, match="paper_only"):
        PaperLedger.from_dict({"starting_cash": "1", "cash": "1"})


def test_load_or_create(tmp_path: Path) -> None:
    ledger = PaperLedger.load_or_create(tmp_path / "missing.json", starting_cash=D("7"))
    assert ledger.starting_cash == D("7")


def test_snapshot_timestamp_is_explicit_when_given() -> None:
    ledger = PaperLedger()
    when = datetime(2026, 9, 14, 12, tzinfo=UTC)
    assert ledger.snapshot(timestamp=when).timestamp == when.isoformat()
