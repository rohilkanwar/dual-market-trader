"""End-to-end: the gated_cross_venue track cannot place a paper order for a
clause-mismatched pair, no matter how wide the price gap is, and it still emits
a measured gate report when nothing is admitted."""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

from apps.measure_all import json_default, persist_run
from core.types import Market, OrderBook, PriceLevel, Venue
from research.scoreboard import (
    CONTROL_TRACK,
    GATED_TRACK,
    VenueSnapshot,
    measure_all_with_ledgers,
)
from research.scoreboard_artifact import build_gate_report, build_scoreboard_artifact
from strategies.matching import CuratedPair
from tests.synthetic_pairs import FED_TEXT, base_metadata

D = Decimal


def book(market_id: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        bids=tuple(PriceLevel(D(p), D(s)) for p, s in bids),
        asks=tuple(PriceLevel(D(p), D(s)) for p, s in asks),
    )


def snapshots(
    kalshi: list[tuple[Market, OrderBook]], poly: list[tuple[Market, OrderBook]]
) -> dict[Venue, VenueSnapshot]:
    return {
        Venue.KALSHI: VenueSnapshot(
            Venue.KALSHI, "synthetic", [m for m, _ in kalshi], {m.market_id: b for m, b in kalshi}
        ),
        Venue.POLYMARKET: VenueSnapshot(
            Venue.POLYMARKET, "synthetic", [m for m, _ in poly], {m.market_id: b for m, b in poly}
        ),
    }


def macro_market(venue: Venue, market_id: str, title: str, metadata: dict[str, Any]) -> Market:
    return Market(venue=venue, market_id=market_id, title=title, metadata={**metadata, "category": "macro"})


# Books with a 20-cent gap: any price-only strategy would trade this.
WIDE_KALSHI = [("0.70", "500"), ("0.69", "500")], [("0.72", "500"), ("0.73", "500")]
WIDE_POLY = [("0.48", "500"), ("0.47", "500")], [("0.50", "500"), ("0.51", "500")]

MISMATCHED_PAIRS: list[tuple[str, dict[str, Any]]] = []
for label, mutate in (
    ("fallback", {"resolution_text": FED_TEXT.replace(
        "It will not fall back to a prior period.",
        "If publication is delayed, it falls back to the last available month.")}),
    ("tie-lower", {"resolution_text": FED_TEXT.replace("ties use the higher bracket", "ties use the lower bracket")}),
    ("no-fingerprint", {"fingerprint": None}),
    ("threshold", {"fingerprint": {**deepcopy(base_metadata()["fingerprint"]), "threshold": "0.50"}}),
    ("media-host", {"source_url": "https://www.reuters.com/markets/"}),
    ("fed-union", {"fed_bucket": ["C25", "C26"]}),
    ("expiry", {"close_time": "2026-12-10T19:00:00Z"}),
):
    poly_meta = base_metadata()
    for key, value in mutate.items():
        if value is None:
            poly_meta.pop(key, None)
        else:
            poly_meta[key] = value
    MISMATCHED_PAIRS.append((label, poly_meta))


def mismatched_universe() -> tuple[dict[Venue, VenueSnapshot], tuple[CuratedPair, ...]]:
    kalshi, poly, curated = [], [], []
    for index, (label, poly_meta) in enumerate(MISMATCHED_PAIRS):
        k_id, p_id = f"K-{label}", f"P-{label}"
        title = f"Will the Fed cut rates at the September 2026 meeting? ({index})"
        kalshi.append((macro_market(Venue.KALSHI, k_id, title, base_metadata()), book(k_id, *WIDE_KALSHI)))
        poly.append((macro_market(Venue.POLYMARKET, p_id, title, poly_meta), book(p_id, *WIDE_POLY)))
        curated.append(CuratedPair(pair_id=f"mismatch-{label}", kalshi_market_id=k_id, polymarket_market_id=p_id))
    return snapshots(kalshi, poly), tuple(curated)


async def test_gated_track_admits_nothing_when_every_pair_is_mismatched() -> None:
    snaps, curated = mismatched_universe()
    summaries, ledgers = await measure_all_with_ledgers(snapshots=snaps, curated_pairs=curated)
    by_track = {s.track: s for s in summaries}
    gated, control = by_track[GATED_TRACK], by_track[CONTROL_TRACK]

    assert gated.candidates == len(MISMATCHED_PAIRS)
    assert gated.admitted == 0
    assert gated.proposed_orders == 0
    assert gated.paper_fills == 0
    assert ledgers[GATED_TRACK].total_pnl == 0
    assert ledgers[GATED_TRACK].fees_paid == 0
    assert gated.rejects == len(MISMATCHED_PAIRS)
    assert set(gated.refused_by_reason) == {
        "clause_refuse_mismatch",
        "fingerprint_indeterminate",
        "fingerprint_not_equivalent",
        "host_tier_not_allowed",
        "fed_bucket_union",
        "expiry_mismatch",
    }
    assert sum(gated.refused_by_reason.values()) == len(MISMATCHED_PAIRS)
    # Every pair carries the full stage report, and none reached pricing.
    for pair_id, result in gated.metrics["gate_results"].items():
        assert result["admitted"] is False, pair_id
        assert result["reasons"], pair_id
        assert "edge" not in result, pair_id
    assert gated.metrics["priced_but_no_edge"] == {}
    assert not gated.settlement_risk_flag

    # The control shows what price-only logic would have done with the same books.
    assert control.candidates == len(MISMATCHED_PAIRS)
    assert control.admitted == len(MISMATCHED_PAIRS)
    assert control.paper_fills > 0
    assert control.settlement_risk_flag
    assert control.metrics["gate_would_have_refused_traded_pairs"] == len(MISMATCHED_PAIRS)


async def test_zero_admit_run_still_emits_a_measured_gate_report(tmp_path: Path) -> None:
    snaps, curated = mismatched_universe()
    summaries, ledgers = await measure_all_with_ledgers(snapshots=snaps, curated_pairs=curated)

    report = build_gate_report(summaries, mode="fixtures", measured_at="t", run_id="r1")
    assert report["kind"] == "gate_report"
    assert report["meta"]["source"] == "measured"
    assert report["meta"]["policy"]["name"] == "strict"
    assert report["totals"]["candidates"] == len(MISMATCHED_PAIRS)
    assert report["totals"]["gate_admitted"] == 0
    assert report["totals"]["traded"] == 0
    assert report["totals"]["status"] == "zero_admits_expected"
    assert report["totals"]["expected_locked_pnl_if_settlement_equivalent"] == 0
    assert len(report["pairs"]) == len(MISMATCHED_PAIRS)
    assert report["stage_failures"]["clauses"] == 2
    assert report["control"]["traded"] == len(MISMATCHED_PAIRS)
    assert report["control"]["settlement_risk_flag"] is True
    json.dumps(report, default=json_default)

    artifact = persist_run(
        summaries, ledgers, artifact_dir=tmp_path, mode="fixtures", measured_at="t", limit=7, kalshi_env=None
    )
    assert (tmp_path / "gate_report_fixtures.json").exists()
    assert (tmp_path / "gate_report_latest.json").exists()
    on_disk = json.loads((tmp_path / "gate_report_latest.json").read_text())
    assert on_disk["meta"]["run_id"] == artifact["meta"]["run_id"]
    assert on_disk["totals"]["traded"] == 0
    assert artifact["gate_report"]["totals"]["gate_refused"] == len(MISMATCHED_PAIRS)
    assert artifact["findings"]["gated_cross_venue"]["traded"] == 0
    assert artifact["findings"]["live_network_cross_venue_candidates"] == len(MISMATCHED_PAIRS)


async def test_no_candidates_still_emits_a_gate_report() -> None:
    kalshi = [(
        macro_market(Venue.KALSHI, "K1", "Will August CPI be above 3.0%?", base_metadata()),
        book("K1", *WIDE_KALSHI),
    )]
    poly = [(
        macro_market(Venue.POLYMARKET, "P1", "Will New York beat Boston?", base_metadata()),
        book("P1", *WIDE_POLY),
    )]
    summaries, _ = await measure_all_with_ledgers(snapshots=snapshots(kalshi, poly), curated_pairs=())

    report = build_gate_report(summaries, mode="network", measured_at="t")
    assert report["totals"]["candidates"] == 0
    assert report["pairs"] == []
    assert report["totals"]["status"] == "zero_admits_expected"
    scoreboard_doc = build_scoreboard_artifact(summaries, mode="network", measured_at="t", limit=1)
    assert "cross_venue_tracks_empty" in scoreboard_doc["portfolio"]["risk_flags"]
    assert scoreboard_doc["findings"]["gated_cross_venue"]["candidates"] == 0


async def test_matching_pair_is_admitted_priced_through_depth_and_ledgered() -> None:
    title = "Will the Fed cut rates at the September 2026 meeting?"
    kalshi = [(
        macro_market(Venue.KALSHI, "K-OK", title, base_metadata()),
        book("K-OK", [("0.52", "120"), ("0.51", "200")], [("0.54", "110")]),
    )]
    poly = [(
        macro_market(Venue.POLYMARKET, "P-OK", title, base_metadata()),
        book("P-OK", [("0.41", "20")], [("0.43", "16"), ("0.44", "440")]),
    )]
    summaries, ledgers = await measure_all_with_ledgers(
        snapshots=snapshots(kalshi, poly), curated_pairs=(CuratedPair("ok", "K-OK", "P-OK"),)
    )
    gated = next(s for s in summaries if s.track == GATED_TRACK)

    assert gated.candidates == 1 and gated.admitted == 1
    assert gated.proposed_orders == 2
    assert gated.paper_fills == 3  # Polymarket leg walked two levels
    assert gated.metrics["gate_results"]["ok"]["admitted"] is True
    assert gated.metrics["gate_results"]["ok"]["edge"]["reason"] == "trade"
    assert gated.metrics["admitted_pairs"]["ok"]["net_edge_per_contract"] == D("0.0688")
    assert gated.metrics["expected_locked_pnl_if_settlement_equivalent"] == D("1.7200")
    assert gated.edge_bps == 688
    ledger = ledgers[GATED_TRACK]
    assert ledger.fees_paid == D("0.44")  # Kalshi fee on the hedge leg only
    assert len(ledger.open_positions) == 2
    # Mid-marked, a locked YES+NO pair shows the spread + fees as a loss until settlement.
    assert ledger.total_pnl < 0
    assert ledger.equity == ledger.starting_cash + ledger.realized_pnl + ledger.unrealized_pnl


async def test_admitted_pair_with_no_price_edge_is_counted_separately() -> None:
    title = "Will the Fed cut rates at the September 2026 meeting?"
    # Identical books on both venues: settlement-equivalent but nothing to lock.
    kalshi = [(macro_market(Venue.KALSHI, "K-FLAT", title, base_metadata()), book("K-FLAT", [("0.52", "50")], [("0.54", "50")]))]
    poly = [(macro_market(Venue.POLYMARKET, "P-FLAT", title, base_metadata()), book("P-FLAT", [("0.52", "50")], [("0.54", "50")]))]
    summaries, _ = await measure_all_with_ledgers(
        snapshots=snapshots(kalshi, poly), curated_pairs=(CuratedPair("flat", "K-FLAT", "P-FLAT"),)
    )
    gated = next(s for s in summaries if s.track == GATED_TRACK)

    assert gated.metrics["gate_admitted"] == 1
    assert gated.admitted == 0
    assert gated.refused_by_reason == {"edge_no_positive_touch_edge": 1}
    assert gated.metrics["priced_but_no_edge"] == {"flat": "no_positive_touch_edge"}
