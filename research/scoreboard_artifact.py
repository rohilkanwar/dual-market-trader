"""Build the versioned scoreboard JSON the dashboard renders.

Every PnL number in the output is read from a track's :class:`PaperLedger`
summary. The writer refuses to emit ``meta.source == "sample"``: sample files
are hand-written schema examples and can only be produced by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from research.scoreboard import (
    CONTROL_TRACK,
    CROSS_VENUE_TRACKS,
    GATED_TRACK,
    NEWS_TRACK,
    PRIMARY_TRACK,
    SPECIALIST_TRACK,
    TrackSummary,
)
from research.specialist_scoreboard import specialist_finding
from research.weather_tracks import WEATHER_FAMILY, is_weather_track, slim_weather_metrics, weather_finding

# 1.4.0: `findings.weather` (+ root `weather_report` pointer) on boards that carry
# a weather track; boards without one are unchanged, so 1.3.0 readers still work.
SCHEMA_VERSION = "1.4.0"
GATE_REPORT_SCHEMA_VERSION = "1.0.0"
ALLOWED_SOURCES = ("measured", "synced")
ZERO = Decimal("0")
Q = Decimal("0.0001")


class SampleSourceRefused(ValueError):
    """Raised when a writer is asked to label generated data as a sample."""


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _sum(values: list[Any]) -> Decimal:
    return sum((_dec(v) for v in values), ZERO).quantize(Q)


def _slim_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Per-pair detail lives in the gate report; the scoreboard keeps counts and pointers."""
    slim = dict(metrics)
    if "gate_results" in slim:
        slim["gate_results"] = {
            pair_id: {"admitted": r.get("admitted"), "reason": r.get("reason"), "reasons": r.get("reasons", [])}
            for pair_id, r in slim["gate_results"].items()
        }
        slim["gate_results_detail"] = "gate_report_<mode>.json"
    if "refused_pairs" in slim:
        slim["refused_pairs"] = {
            pair_id: {"reason": r.get("reason"), "reasons": r.get("reasons", [])}
            for pair_id, r in slim["refused_pairs"].items()
        }
    if isinstance(slim.get("vetoed_candidates"), list):
        vetoes = slim["vetoed_candidates"]
        by_reason: dict[str, int] = {}
        for veto in vetoes:
            by_reason[veto["reason"]] = by_reason.get(veto["reason"], 0) + 1
        slim["vetoed_candidates"] = {"count": len(vetoes), "by_reason": dict(sorted(by_reason.items()))}
    if isinstance(slim.get("records"), list):
        # tennis_basis gap register: per-record detail lives in tennis_basis_latest.json.
        slim["records"] = {"count": len(slim["records"]), "detail": "tennis_basis_latest.json"}
        if isinstance(slim.get("measurements"), list):
            slim["measurements"] = {"count": len(slim["measurements"]), "detail": "tennis_basis_latest.json"}
    if "follow_state" in slim:
        # The specialist lane's full scoreboard, follow log and per-attempt rows
        # live in specialist_scoreboard_<mode>.json; the board keeps counts.
        slim["follow_state"] = {"followed": len(slim["follow_state"].get("followed", [])), "detail": "specialist_scoreboard_<mode>.json"}
        slim["scoreboard"] = {"rows": len(slim.get("scoreboard", [])), "detail": "specialist_scoreboard_<mode>.json"}
        attempts = slim.get("follow_attempts", [])
        by_reason = {}
        for attempt in attempts:
            by_reason[attempt.get("reason")] = by_reason.get(attempt.get("reason"), 0) + 1
        slim["follow_attempts"] = {"count": len(attempts), "by_reason": dict(sorted(by_reason.items()))}
        slim.pop("traders", None)
    return slim


def _track_row(summary: TrackSummary) -> dict[str, Any]:
    row = {k: v for k, v in summary.as_dict().items() if k not in ("fills", "edges")}
    row["metrics"] = _slim_metrics(row["metrics"])
    if is_weather_track(summary.track):
        # Per-station / per-bucket rows belong in weather_report_<mode>.json; the
        # board keeps counts. The family stamp lets the dashboard place the row
        # even if the id is a weather-like variant the registry has not seen.
        row["metrics"] = slim_weather_metrics(row["metrics"])
        row.setdefault("family", WEATHER_FAMILY)
    return row


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
    venues: tuple[str, ...] = ("kalshi", "polymarket"),
    venue_focus: str = "kalshi",
    label_suffix: str | None = None,
    track_family: str | None = None,
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
    if any(t in by_track for t in CROSS_VENUE_TRACKS) and all(
        by_track[t].candidates == 0 for t in CROSS_VENUE_TRACKS if t in by_track
    ):
        risk_flags.append("cross_venue_tracks_empty")
    if any(s.settlement_risk_flag for s in summaries):
        risk_flags.append("settlement_risk_flagged_on_ungated_track")
    if any(
        s.metrics.get("snapshot", {}).get(v, {}).get("errors")
        for s in summaries
        for v in ("kalshi", "polymarket")
    ):
        risk_flags.append("snapshot_errors_present")
    news = by_track.get(NEWS_TRACK)
    if news is not None and (news.paper_fills > 0 or int(news.ledger.get("fills", 0)) > 0):
        # Any paper PnL on this lane rests on an unvalidated signal->probability mapping,
        # including positions carried from earlier cycles.
        risk_flags.append("news_signal_mapping_unvalidated")
    if any(_dec(l.get("total_pnl", 0)) != ZERO for t, l in ledgers.items() if t == "polymarket_combinatorial_arb"):
        risk_flags.append("combinatorial_positions_marked_at_mid_not_resolution")
    specialist = by_track.get(SPECIALIST_TRACK)
    if specialist is not None:
        pooled_status = specialist.metrics.get("evaluation", {}).get("pooled", {}).get("status")
        if int(specialist.metrics.get("follow_log", {}).get("total", 0)) > 0 and pooled_status != "pass":
            # Follow PnL exists but the pre-registered test has not passed (usually: underpowered).
            risk_flags.append("specialist_hypothesis_not_validated")
    weather = weather_finding(summaries)
    if weather is not None and not weather["hypothesis_validated"]:
        weather_fills = weather["paper_fills"] + sum(
            int(ledgers.get(t, {}).get("fills", 0)) for t in weather["tracks"]
        )
        if weather_fills > 0:
            # Paper PnL on the weather lane exists, but its pre-registered test has not passed.
            risk_flags.append("weather_hypothesis_not_validated")
    risk_flags.append("pnl_from_ledger_not_placeholder")

    fills = [dict(row) for s in summaries for row in s.fills]
    fills.sort(key=lambda r: (abs(_dec(r["paper_pnl"])) if r.get("paper_pnl") is not None else ZERO, _dec(r["qty"]) * _dec(r["price"])), reverse=True)
    edges = [dict(row) for s in summaries for row in s.edges]
    edges.sort(key=lambda r: (r.get("edge_bps") or -10**9), reverse=True)

    def _label() -> str:
        base = "MEASURED / NETWORK" if mode == "network" else "MEASURED / FIXTURES"
        suffix = f" / {label_suffix}" if label_suffix else ""
        return f"{base}{suffix}{f' / CYCLE {cycle}' if cycle is not None else ''}"

    gated = by_track.get(GATED_TRACK)
    findings_out: dict[str, Any] = {
        "live_network_cross_venue_candidates": (
            gated.candidates
            if gated is not None
            else sum(by_track[t].candidates for t in ("gated_cross_venue_macro", "sports_cross_venue") if t in by_track)
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
    if gated is not None:
        findings_out["gated_cross_venue"] = gate_summary(gated)
    if specialist is not None:
        findings_out["category_specialist"] = specialist_finding(specialist)
    if weather is not None:
        findings_out["weather"] = weather
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
            "venues": list(venues),
            "markets_per_venue": limit,
            "primary_track": primary_track,
            "venue_focus": venue_focus,
            "kalshi_env": kalshi_env,
            "cycle": cycle,
            "pnl_source": "core.ledger.PaperLedger",
            "track_family": track_family,
            "refresh": (
                "python -m apps.measure_polymarket_arb [--network] && cd dashboard && npm run sync-artifacts"
                if track_family == "polymarket_arb"
                else "python -m apps.measure_weather [--network] && cd dashboard && npm run sync-artifacts"
                if track_family == WEATHER_FAMILY
                else "python -m apps.measure_all [--network] && cd dashboard && npm run sync-artifacts"
            ),
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
        "tracks": [_track_row(s) for s in summaries],
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
                for venue in venues
            ],
            "pnl_by_track": [{"track": t, "value": l.get("total_pnl", ZERO)} for t, l in ledgers.items()],
        },
    }


def gate_summary(gated: TrackSummary) -> dict[str, Any]:
    """Compact admissibility headline for the settlement-safe track."""
    metrics = gated.metrics
    gate_refused = int(metrics.get("gate_refused", 0))
    return {
        "track": gated.track,
        "policy": metrics.get("gate_policy", {}).get("name"),
        "candidates": gated.candidates,
        "gate_admitted": int(metrics.get("gate_admitted", gated.candidates - gate_refused)),
        "gate_refused": gate_refused,
        "priced_but_no_edge": len(metrics.get("priced_but_no_edge", {})),
        "traded": gated.admitted,
        "paper_fills": gated.paper_fills,
        "primary_reject_reasons": dict(sorted(gated.refused_by_reason.items())),
        "all_stage_reject_reasons": metrics.get("gate_reject_reasons_all", {}),
        "vetoed_candidates": len(metrics.get("vetoed_candidates", [])),
        "expected_locked_pnl_if_settlement_equivalent": metrics.get(
            "expected_locked_pnl_if_settlement_equivalent", ZERO
        ),
        "ledger_total_pnl": gated.ledger.get("total_pnl") if gated.ledger else None,
        "status": (
            "zero_admits_expected"
            if gated.admitted == 0
            else "admits_present_verify_fingerprints"
        ),
    }


def build_gate_report(
    summaries: list[TrackSummary],
    *,
    mode: str,
    measured_at: str,
    run_id: str | None = None,
    generated_at: str | None = None,
    source: str = "measured",
) -> dict[str, Any]:
    """Per-pair admissibility report for ``gated_cross_venue`` (+ the control).

    Emitted on every run, including runs with zero candidates or zero admits:
    an empty ``pairs`` list with a populated ``totals`` block *is* the finding.
    """
    if source not in ALLOWED_SOURCES:
        raise SampleSourceRefused(f"meta.source must be one of {ALLOWED_SOURCES}; refusing {source!r}.")
    by_track = {summary.track: summary for summary in summaries}
    gated = by_track.get(GATED_TRACK)
    if gated is None:
        raise ValueError(f"no {GATED_TRACK} summary supplied")
    control = by_track.get(CONTROL_TRACK)
    pairs = [
        {"pair_id": pair_id, **result}
        for pair_id, result in sorted(gated.metrics.get("gate_results", {}).items())
    ]
    stage_failures: dict[str, int] = {}
    for pair in pairs:
        for stage in pair.get("stages_failed", []):
            stage_failures[stage] = stage_failures.get(stage, 0) + 1
    control_block: dict[str, Any] | None = None
    if control is not None:
        control_block = {
            "track": control.track,
            "candidates": control.candidates,
            "traded": control.admitted,
            "paper_fills": control.paper_fills,
            "settlement_risk_flag": control.settlement_risk_flag,
            "gate_would_have_refused_traded_pairs": control.metrics.get("gate_would_have_refused_traded_pairs", 0),
            "ledger_total_pnl": control.ledger.get("total_pnl") if control.ledger else None,
            "note": "Control only. PnL here is not realisable: legs may settle on different events.",
        }
    return {
        "schema_version": GATE_REPORT_SCHEMA_VERSION,
        "kind": "gate_report",
        "meta": {
            "source": source,
            "paper_only": True,
            "mode": mode,
            "measured_at": measured_at,
            "generated_at": generated_at or datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "track": GATED_TRACK,
            "control_track": CONTROL_TRACK,
            "policy": gated.metrics.get("gate_policy", {}),
            "parameters": gated.metrics.get("parameters", {}),
            "fee_model": gated.metrics.get("fee_model", {}),
            "stage_order": gated.metrics.get("stage_order", []),
            "pnl_source": "core.ledger.PaperLedger",
        },
        "totals": gate_summary(gated),
        "stage_failures": dict(sorted(stage_failures.items())),
        "vetoed_candidates": gated.metrics.get("vetoed_candidates", []),
        "pairs": pairs,
        "control": control_block,
    }
