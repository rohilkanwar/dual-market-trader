"""Build the versioned scoreboard JSON the dashboard renders.

Every PnL number in the output is read from a track's :class:`PaperLedger`
summary. The writer refuses to emit ``meta.source == "sample"``: sample files
are hand-written schema examples and can only be produced by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from research.scoreboard import CROSS_VENUE_TRACKS, NEWS_TRACK, PRIMARY_TRACK, TrackSummary

SCHEMA_VERSION = "1.2.0"
ALLOWED_SOURCES = ("measured", "synced")
ZERO = Decimal("0")
Q = Decimal("0.0001")


class SampleSourceRefused(ValueError):
    """Raised when a writer is asked to label generated data as a sample."""


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _sum(values: list[Any]) -> Decimal:
    return sum((_dec(v) for v in values), ZERO).quantize(Q)


def build_scoreboard_artifact(
    summaries: list[TrackSummary],
    *,
    mode: str,
    measured_at: str,
    limit: int,
    source: str = "measured",
    generated_at: str | None = None,
    kalshi_env: str | None = None,
    primary_track: str = PRIMARY_TRACK,
    findings: dict[str, Any] | None = None,
    cycle: int | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    if source not in ALLOWED_SOURCES:
        raise SampleSourceRefused(
            f"meta.source must be one of {ALLOWED_SOURCES}; refusing {source!r}. "
            "Generated artifacts are never samples."
        )
    if mode not in ("network", "fixtures"):
        raise ValueError("mode must be 'network' or 'fixtures'")

    by_track = {summary.track: summary for summary in summaries}
    ledgers = {summary.track: summary.ledger for summary in summaries if summary.ledger}
    primary_ledger = ledgers.get(primary_track, {})

    total_candidates = sum(s.candidates for s in summaries)
    total_admitted = sum(s.admitted for s in summaries)
    total_fills = sum(s.paper_fills for s in summaries)
    total_orders = sum(s.proposed_orders for s in summaries)
    settlement_risk_pairs = sum(
        int(s.metrics.get("gate_would_have_refused_traded_pairs", 0)) + int(s.metrics.get("host_conflicts", 0))
        for s in summaries
        if s.track in CROSS_VENUE_TRACKS and s.settlement_risk_flag
    )
    admitted_edges = [s.edge_bps for s in summaries if s.edge_bps is not None and s.admitted]
    paper_pnl = _sum([l.get("total_pnl", 0) for l in ledgers.values()])

    positions_all: list[dict[str, Any]] = []
    for track, ledger in ledgers.items():
        for position in ledger.get("positions", []):
            positions_all.append({**position, "track": track})
    gross_by_venue: dict[str, Decimal] = {}
    for position in positions_all:
        gross_by_venue[position["venue"]] = gross_by_venue.get(position["venue"], ZERO) + abs(_dec(position["value"]))
    gross_total = sum(gross_by_venue.values(), ZERO)

    risk_flags: list[str] = []
    if total_fills == 0:
        risk_flags.append("zero_paper_fills_this_run")
    if any(int(l.get("unmarked_positions", 0)) for l in ledgers.values()):
        risk_flags.append("unmarked_positions_valued_at_cost")
    if all(by_track[t].candidates == 0 for t in CROSS_VENUE_TRACKS if t in by_track):
        risk_flags.append("cross_venue_tracks_empty")
    if any(s.settlement_risk_flag for s in summaries):
        risk_flags.append("settlement_risk_flagged_on_ungated_track")
    if any(s.metrics.get("snapshot", {}).get(v, {}).get("errors") for s in summaries[:1] for v in ("kalshi", "polymarket")):
        risk_flags.append("snapshot_errors_present")
    news = by_track.get(NEWS_TRACK)
    if news is not None and (news.paper_fills > 0 or int(news.ledger.get("fills", 0)) > 0):
        # Any paper PnL on this lane rests on an unvalidated signal->probability mapping,
        # including positions carried from earlier cycles.
        risk_flags.append("news_signal_mapping_unvalidated")
    risk_flags.append("pnl_from_ledger_not_placeholder")

    fills = [dict(row) for s in summaries for row in s.fills]
    fills.sort(key=lambda r: (abs(_dec(r["paper_pnl"])) if r.get("paper_pnl") is not None else ZERO, _dec(r["qty"]) * _dec(r["price"])), reverse=True)
    edges = [dict(row) for s in summaries for row in s.edges]
    edges.sort(key=lambda r: (r.get("edge_bps") or -10**9), reverse=True)

    def _label() -> str:
        base = "MEASURED / NETWORK" if mode == "network" else "MEASURED / FIXTURES"
        return f"{base}{f' / CYCLE {cycle}' if cycle is not None else ''}"

    findings_out: dict[str, Any] = {
        "live_network_cross_venue_candidates": sum(
            by_track[t].candidates for t in ("gated_cross_venue_macro", "sports_cross_venue") if t in by_track
        ),
        "arbai_summary": (
            "Cross-venue macro settlement rules frequently diverge (clause, fingerprint or host "
            "mismatch). The gated track refuses those pairs; single-venue fair value is the primary "
            "paper track and carries no cross-venue settlement exposure."
        ),
        "divergence_findings_status": "not_measured_in_this_run",
    }
    if news is not None:
        mapping = news.metrics.get("mapping", {}).get("counts", {})
        ratio = news.metrics.get("reaction_ratio", {})
        findings_out["news_underreaction"] = {
            "status": news.metrics.get("status", "not_run"),
            "signal_source": news.metrics.get("signal_source", {}).get("name"),
            "signals": news.candidates,
            "mapped": sum(v for k, v in mapping.items() if k != "unmapped"),
            "unmapped": mapping.get("unmapped", 0),
            "paper_fills": news.paper_fills,
            "reaction_ratio_observed_mean": ratio.get("observed_mean"),
            "reaction_ratio_literature": ratio.get("literature"),
            "literature_reference": news.metrics.get("literature", {}).get("reference"),
            "mapping_validated": False,
            "note": (
                "Signal-to-probability mapping is not validated; fixture signals are synthetic. "
                "See docs/NEWS_UNDERREACTION.md."
            ),
        }
    if findings:
        findings_out.update(findings)

    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "source": source,
            "label": _label(),
            "paper_only": True,
            "mode": mode,
            "measured_at": measured_at,
            "generated_at": generated_at or datetime.now(UTC).isoformat(),
            "venues": ["kalshi", "polymarket"],
            "markets_per_venue": limit,
            "primary_track": primary_track,
            "venue_focus": "kalshi",
            "kalshi_env": kalshi_env,
            "cycle": cycle,
            "pnl_source": "core.ledger.PaperLedger",
            "refresh": "python -m apps.measure_all [--network] && cd dashboard && npm run sync-artifacts",
        },
        "findings": findings_out,
        "totals": {
            "candidates": total_candidates,
            "admitted": total_admitted,
            "rejects": max(0, total_candidates - total_admitted),
            "proposed_orders": total_orders,
            "paper_fills": total_fills,
            "fill_rate": (Decimal(total_fills) / Decimal(total_orders)).quantize(Q) if total_orders else None,
            "settlement_risk_pairs": settlement_risk_pairs,
            "paper_pnl": paper_pnl,
            "realized_pnl": _sum([l.get("realized_pnl", 0) for l in ledgers.values()]),
            "unrealized_pnl": _sum([l.get("unrealized_pnl", 0) for l in ledgers.values()]),
            "fees_paid": _sum([l.get("fees_paid", 0) for l in ledgers.values()]),
            "avg_edge_bps": int(sum(admitted_edges) / len(admitted_edges)) if admitted_edges else None,
        },
        "tracks": [
            {k: v for k, v in s.as_dict().items() if k not in ("fills", "edges")}
            for s in summaries
        ],
        "top_fills": [{"rank": i + 1, **row} for i, row in enumerate(fills[:top_n])],
        "top_edges": [{"rank": i + 1, **row} for i, row in enumerate(edges[:top_n])],
        "portfolio": {
            "paper_only": True,
            "source": "ledger_aggregate",
            "open_positions": len([p for p in positions_all if _dec(p["quantity"]) != ZERO]),
            "starting_cash": _sum([l.get("starting_cash", 0) for l in ledgers.values()]),
            "cash": _sum([l.get("cash", 0) for l in ledgers.values()]),
            "equity": _sum([l.get("equity", 0) for l in ledgers.values()]),
            "gross_notional": _sum([l.get("gross_notional", 0) for l in ledgers.values()]),
            "net_exposure": _sum([l.get("net_exposure", 0) for l in ledgers.values()]),
            "realized_pnl": _sum([l.get("realized_pnl", 0) for l in ledgers.values()]),
            "unrealized_pnl": _sum([l.get("unrealized_pnl", 0) for l in ledgers.values()]),
            "total_pnl": paper_pnl,
            "fees_paid": _sum([l.get("fees_paid", 0) for l in ledgers.values()]),
            "max_drawdown": max((_dec(l.get("max_drawdown", 0)) for l in ledgers.values()), default=ZERO),
            "settlement_risk_pairs": settlement_risk_pairs,
            "concentration": [
                {"venue": venue, "weight": (value / gross_total).quantize(Q) if gross_total else ZERO}
                for venue, value in sorted(gross_by_venue.items())
            ],
            "risk_flags": risk_flags,
            "primary_track": primary_track,
            "primary": primary_ledger,
            "by_track": {
                track: {
                    k: ledger.get(k)
                    for k in (
                        "starting_cash", "cash", "equity", "realized_pnl", "unrealized_pnl",
                        "total_pnl", "fees_paid", "gross_notional", "max_drawdown",
                        "open_positions", "fills", "equity_points",
                    )
                }
                for track, ledger in ledgers.items()
            },
        },
        "charts": {
            "candidates_by_track": [{"track": s.track, "value": s.candidates} for s in summaries],
            "fills_by_track": [{"track": s.track, "value": s.paper_fills} for s in summaries],
            "venue_fills": [
                {"venue": venue, "value": sum(1 for f in fills if f["venue"] == venue)}
                for venue in ("kalshi", "polymarket")
            ],
            "pnl_by_track": [{"track": t, "value": l.get("total_pnl", ZERO)} for t, l in ledgers.items()],
        },
    }
