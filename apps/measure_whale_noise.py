"""Make-on-whale, take-on-noise: Kalshi tennis paper measurement.

Replays one event stream (polled L2 books + public trade prints) through three
paper legs — ``kalshi_whale_noise_combined``, ``kalshi_whale_maker_leg``,
``kalshi_noise_taker_leg`` — and reports whether the combined book beat either
leg alone, with fill rate and toxicity for both legs. Outputs (relative to
``--artifact-dir``, default ``artifacts/``):

* ``whale_noise_report_latest.json``   verdicts, fill model, toxicity, quotes, ex-post, assumptions
* ``scoreboard_whale_noise.json``      scoreboard artifact for the three tracks (source=measured)
* ``paper/ledger_<track>.json``        ledgers (carried across runs unless ``--no-persist``)
* ``paper/equity_curve_<track>.jsonl``
* ``paper/runs/<run_id>.json``         run record picked up by the dashboard experiments index

Data sources (pick one):

* default: the committed synthetic fixture (deterministic, no network)
* ``--archive DIR``: replay the self-logged ``apps.book_logger`` archive — the
  only source on which maker fills are *measured*
* ``--network``: one public snapshot + recent tape per open tennis market;
  maker fills are not simulated (quotes are recorded as resting)

Paper-only: refuses to start if the environment requests live trading. Reads
only unauthenticated Kalshi endpoints; no keys are read or needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from apps.measure_all import (
    append_jsonl,
    equity_curve_path,
    ledger_path,
    load_ledgers,
    to_jsonable,
    write_json,
)
from core.config import require_paper_only
from core.ledger import PaperLedger
from research.flb import KalshiFeeModel
from research.flb_expost import harvest_settled_trades, load_settled_trades
from research.scoreboard import TrackSummary
from research.scoreboard_artifact import build_scoreboard_artifact
from research.whale_noise import (
    COMBINED,
    EXPOST_FIXTURE_PATH,
    HARVEST_FILE,
    MAKER_LEG,
    TAKER_LEG,
    WHALE_NOISE_PRIMARY,
    WHALE_NOISE_TRACKS,
    MarketTimeline,
    load_fixture_timelines,
    not_measured_expost,
    parse_ts,
    run_whale_noise_tracks,
    timelines_from_archive,
    timelines_from_network,
    whale_flow_expost,
)
from strategies.whale_noise import DEFAULT_TENNIS_SERIES, WhaleNoiseParameters, WhaleRule, load_whale_registry

REPORT_NAME = "whale_noise_report_latest.json"
SCOREBOARD_NAME = "scoreboard_whale_noise.json"
REPORT_SCHEMA = "1.0.0"
RUN_KIND = "kalshi_whale_noise"
EXPERIMENT = {
    "name": "make-on-whale, take-on-noise",
    "hypothesis": (
        "When a whale-class print lifts one side of a Kalshi tennis market, resting a same-direction bid one "
        "tick behind the touch (inventory-capped) earns the whale's information at maker cost; when only retail "
        "longshot flow is hitting, taking the fade of the favourite earns the favorite-longshot bias. Running "
        "both in one book should beat either alone."
    ),
    "pass_criterion": "combined paper PnL > paper PnL of either leg alone on the same event stream, with >= min_fills_for_verdict combined fills; fill rate and toxicity logged for both legs",
    "fail_risks": [
        "queue priority: the resting quote sits behind every displayed contract at its level and is filled only by later public prints (conservative queue model); low fill rate is a FAIL mode, not a modelling choice",
        "whale cancel / reversal: a whale lift that reverts leaves the maker leg long at a worse price (measured as negative post-fill markouts = toxicity, and as whale_follow_through.reversal_rate)",
        "identity: the public tape is anonymous; 'known +EV whale' is a size class whose EV is verified ex post from settled tennis markets or asserted by an operator registry, never inferred from a name",
    ],
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _verdict_table(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {"scope": "replay", "check": "combined_beats_each_leg", "verdict": report["combined_vs_legs"]["verdict"], "detail": report["combined_vs_legs"]["reason"]},
    ]
    tox = report["legs"][COMBINED].get("maker_toxicity_verdict")
    if tox:
        rows.append({"scope": "replay", "check": "maker_fills_not_adversely_selected", "verdict": tox["verdict"], "detail": tox["reason"]})
    for key, section in report["ex_post"]["verdicts"].items():
        rows.append({"scope": "ex_post", "check": key, "verdict": section["verdict"], "detail": section.get("reason")})
    early = report["ex_post"].get("excluding_final_minutes")
    if early:
        for key, section in early["verdicts"].items():
            rows.append({"scope": "ex_post", "check": f"{key}_excluding_final_minutes", "verdict": section["verdict"], "detail": section.get("reason")})
    return rows


def _headline(report: dict[str, Any]) -> str:
    cv = report["combined_vs_legs"]
    maker = report["legs"][COMBINED]["maker"]
    horizon = str(max(report["parameters"]["markout_horizons_seconds"]))
    tox = maker["toxicity"].get(horizon, {})
    rate = maker.get("fill_rate_contracts")
    ev = report["ex_post"]["verdicts"]["whale_flow_ev_positive"]
    return (
        f"Combined vs legs: {cv['verdict']} (combined {float(cv['combined_pnl']):+.2f} vs maker {float(cv['maker_leg_pnl']):+.2f} / "
        f"taker {float(cv['taker_leg_pnl']):+.2f}, {cv['combined_fills']} combined fills). "
        f"Maker fill rate {('%.0f%%' % (float(rate) * 100)) if rate is not None else 'n/a'} of quoted contracts "
        f"({maker['quotes_filled']}/{maker['quotes_placed']} quotes filled), {horizon}s toxicity "
        f"{('%.0f%%' % (float(tox['toxicity_rate']) * 100)) if tox.get('toxicity_rate') is not None else 'n/a'}. "
        f"Whale flow ex post: {ev['verdict']} ({ev.get('ev_status', 'unverified')})."
    )


def _leg_section(summary: TrackSummary) -> dict[str, Any]:
    metrics = summary.metrics
    maker = {k: v for k, v in metrics["maker"].items()}
    return {
        "label": summary.label,
        "candidates": summary.candidates,
        "admitted": summary.admitted,
        "proposed_orders": summary.proposed_orders,
        "paper_fills": summary.paper_fills,
        "fill_rate": summary.fill_rate,
        "refused_by_reason": dict(sorted(summary.refused_by_reason.items())),
        "edge_bps": summary.edge_bps,
        "ledger": {k: summary.ledger.get(k) for k in ("starting_cash", "cash", "equity", "realized_pnl", "unrealized_pnl", "total_pnl", "fees_paid", "gross_notional", "max_drawdown", "open_positions", "unmarked_positions", "fills", "mark_method")},
        "maker": maker,
        "taker": metrics["taker"],
        "whale_follow_through": metrics["whale_follow_through"],
        "maker_toxicity_verdict": metrics.get("maker_toxicity_verdict"),
        "settlement_preview": metrics.get("settlement_preview"),
        "cash_at_risk": metrics.get("cash_at_risk"),
        "notes": summary.notes,
    }


def build_report(
    *,
    timelines: list[MarketTimeline],
    data_meta: dict[str, Any],
    summaries: list[TrackSummary],
    replay: Any,
    ex_post: dict[str, Any],
    params: WhaleNoiseParameters,
    mode: str,
    kalshi_env: str | None,
    measured_at: str,
    run_id: str,
    model_fees: bool,
) -> dict[str, Any]:
    by_track = {s.track: s for s in summaries}
    fee_model = KalshiFeeModel() if model_fees else KalshiFeeModel.zero()
    combined = by_track[COMBINED]
    quotes = {track: by_track[track].metrics.get("quotes", []) for track in (COMBINED, MAKER_LEG)}
    max_rows = 200
    evaluations = {
        "note": f"first {max_rows} rows per leg; counts are complete",
        "quote_evaluations": {
            track: {"count": len(by_track[track].metrics.get("quote_evaluations", [])), "rows": by_track[track].metrics.get("quote_evaluations", [])[:max_rows]}
            for track in (COMBINED, MAKER_LEG)
        },
        "fade_evaluations": {
            track: {"count": len(by_track[track].metrics.get("fade_evaluations", [])), "rows": by_track[track].metrics.get("fade_evaluations", [])[:max_rows]}
            for track in (COMBINED, TAKER_LEG)
        },
    }
    report = {
        "schema_version": REPORT_SCHEMA,
        "kind": "kalshi_whale_noise_report",
        "paper_only": True,
        "run_id": run_id,
        "mode": mode,
        "kalshi_env": kalshi_env,
        "measured_at": measured_at,
        "generated_at": _now(),
        "pnl_source": "core.ledger.PaperLedger",
        "experiment": EXPERIMENT,
        "headline": "",
        "verdict_table": [],
        "data": data_meta,
        "event_stream": {
            "markets": len(timelines),
            "prints": replay.prints_seen,
            "print_classes": dict(sorted(replay.print_classes.items())),
            "whale_events": len(replay.whale_events),
            "books": sum(len(t.books) for t in timelines),
            "alignment": combined.metrics.get("alignment"),
            "span_seconds_max": max((t.span_seconds for t in timelines), default=0.0),
            "markets_with_whale_events": len({e.market_id for e in replay.whale_events}),
            "per_market": [
                {
                    "market": t.market.market_id,
                    "title": t.market.title,
                    "series": t.market.metadata.get("series_ticker"),
                    "books": len(t.books),
                    "prints": len(t.prints),
                    "whale_events": sum(1 for e in replay.whale_events if e.market_id == t.market.market_id),
                    "final_mid": t.final_book.mid_price,
                    "settlement": t.settlement.value if t.settlement else None,
                }
                for t in timelines
            ],
        },
        "parameters": params.as_dict(),
        "fee_model": {
            "taker_rate": fee_model.taker_rate,
            "maker_rate": fee_model.maker_rate,
            "formula": "round_up(M * rate * C * P * (1-P)); M = series fee_multiplier; maker rate only on quadratic_with_maker_fees series (assumed when unknown)",
        },
        "combined_vs_legs": combined.metrics["combined_vs_legs"],
        "legs": {s.track: _leg_section(s) for s in summaries},
        "quotes": quotes,
        "evaluations": evaluations,
        "ex_post": ex_post,
        "assumptions": {
            "whale_definition": (
                f"A non-block print with size >= {params.default_rule.min_contracts} contracts, notional >= ${params.default_rule.min_notional}, and "
                f"size >= {params.default_rule.size_multiple} x the median of the market's previous {params.recent_prints_window} prints once "
                f"{params.default_rule.min_history} prints exist. The public tape is anonymous: this is a size class, not a trader. ev_status={params.default_rule.ev_status}."
            ),
            "one_tick_behind": (
                f"After a whale buys outcome X, rest a bid for X {params.ticks_behind} tick(s) behind X's best bid in the latest book *before* the print "
                "(pre-impact touch). The quote never joins or improves the touch and never sits at the whale's price."
            ),
            "fill_model": (
                "Conservative queue: the quote sits behind every displayed contract at its level at placement; later snapshots can only lengthen "
                "the queue; fills come from public prints at the level after the queue is consumed, or from a print through the level for at most "
                f"its printed size; nothing fills inside {params.reaction_latency_seconds}s of the trigger or after {params.quote_ttl_seconds}s. "
                "Cancels ahead of us are never assumed. Without a forward tape (single network snapshot) no maker fill is simulated."
            ),
            "taker_leg": (
                f"A retail print (below whale size) buying a side priced under {params.longshot_threshold} triggers the FLB longshot fade (buy the favourite "
                f"at the touch, taker fee) unless a whale print hit the market within {params.whale_lookback_seconds}s; one fade per market per "
                f"{params.taker_cooldown_seconds}s. Fills walk the latest book before the print."
            ),
            "toxicity": (
                f"Markout = direction x (book mid at t+h - fill price) for h in {list(params.markout_horizons_seconds)} seconds, using the first book at or "
                "after t+h; a fill is toxic when its markout is negative. whale_follow_through measures the same drift after each whale print."
            ),
            "marks": "All three ledgers mark conservatively (longs at bid, shorts at ask) from the last book of each market; positions without a two-sided book are valued at cost and counted as unmarked.",
            "sizing": (
                f"Whole contracts; ${params.max_order_notional}/order, ${params.max_market_notional} cash at risk per market, ${params.max_total_cash_at_risk} total "
                "collateral including resting quotes (strategy), plus 75 contracts/market and $75 daily loss (RiskManager). Same caps on all three legs."
            ),
            "comparison": "The three legs replay the identical event stream with identical caps, fees and marks; interaction_pnl = combined - (maker + taker) isolates what sharing one inventory book changes.",
            "ex_post": "whale_flow_ev_positive classifies settled-market prints with the same size rule and asks whether that class earned a positive taker return after fees (market-clustered t >= 2). Only this can turn ev_status from unverified into verified_expost.",
        },
    }
    report["headline"] = _headline(report)
    report["verdict_table"] = _verdict_table(report)
    return report


def persist_run(
    *,
    artifact_dir: Path,
    report: dict[str, Any],
    summaries: list[TrackSummary],
    ledgers: dict[str, PaperLedger],
    mode: str,
    kalshi_env: str | None,
    limit: int,
    measured_at: str,
    run_id: str,
) -> dict[str, Any]:
    for track, ledger in ledgers.items():
        ledger.save(ledger_path(artifact_dir, track))
        if ledger.equity_curve:
            append_jsonl(equity_curve_path(artifact_dir, track), {"run_id": run_id, "track": track, "mode": mode, **asdict(ledger.equity_curve[-1])})
    # Per-quote / per-print detail lives in the report; the scoreboard and run record keep counts.
    for summary in summaries:
        for key in ("quotes", "quote_evaluations", "fade_evaluations"):
            if key in summary.metrics:
                summary.metrics[key] = {"count": len(summary.metrics[key]), "detail": REPORT_NAME}
    findings = {
        "kalshi_whale_noise": {
            "headline": report["headline"],
            "verdicts": {row["check"]: row["verdict"] for row in report["verdict_table"]},
            "combined_vs_legs": {k: report["combined_vs_legs"][k] for k in ("verdict", "combined_pnl", "maker_leg_pnl", "taker_leg_pnl", "interaction_pnl", "combined_fills")},
            "report": REPORT_NAME,
        },
        "divergence_findings_status": "not_applicable_whale_noise_run",
        "arbai_summary": report["headline"],
    }
    scoreboard_mode = "network" if mode in ("network", "archive") else "fixtures"
    artifact = build_scoreboard_artifact(
        summaries,
        mode=scoreboard_mode,
        measured_at=measured_at,
        limit=limit,
        kalshi_env=kalshi_env,
        primary_track=WHALE_NOISE_PRIMARY,
        findings=findings,
        venues=("kalshi",),
        venue_focus="kalshi",
        label_suffix="KALSHI WHALE/NOISE" + (" / ARCHIVE REPLAY" if mode == "archive" else ""),
        track_family="kalshi_flb",
    )
    artifact["meta"]["run_id"] = run_id
    artifact["meta"]["kind"] = RUN_KIND
    artifact["meta"]["data_mode"] = mode
    artifact["meta"]["refresh"] = "python -m apps.measure_whale_noise [--archive artifacts/books | --network --kalshi-env prod] && cd dashboard && npm run sync-artifacts"
    write_json(artifact_dir / SCOREBOARD_NAME, artifact)
    write_json(artifact_dir / REPORT_NAME, report)
    write_json(
        artifact_dir / "paper" / "runs" / f"{run_id}.json",
        {
            "run_id": run_id,
            "paper_only": True,
            "kind": RUN_KIND,
            "mode": scoreboard_mode,
            "data_mode": mode,
            "measured_at": measured_at,
            "venues": ["kalshi"],
            "venue_focus": "kalshi",
            "kalshi_env": kalshi_env,
            "primary_track": WHALE_NOISE_PRIMARY,
            "track_family": "kalshi_flb",
            "label": artifact["meta"]["label"],
            "whale_noise": findings["kalshi_whale_noise"],
            "tracks": [summary.as_dict() for summary in summaries],
        },
    )
    return artifact


def _print_report(report: dict[str, Any]) -> None:
    print(f"\nmake-on-whale / take-on-noise paper measurement ({report['mode']}, run {report['run_id']})")
    print(report["headline"])
    print(f"\n{'scope':<8}{'check':<58}{'verdict':<20}detail")
    print("-" * 140)
    for row in report["verdict_table"]:
        print(f"{row['scope']:<8}{row['check']:<58}{row['verdict']:<20}{(row['detail'] or '')[:60]}")
    es = report["event_stream"]
    print(f"\nevent stream: {es['markets']} markets, {es['books']} books, {es['prints']} prints, classes={es['print_classes']}, whale events={es['whale_events']}")
    print(f"\n{'leg':<32}{'cand':>6}{'adm':>6}{'ord':>6}{'fill':>6}{'real':>10}{'unreal':>10}{'fees':>8}  maker fill rate / tox   taker fills")
    horizon = str(max(report["parameters"]["markout_horizons_seconds"]))
    for name, leg in report["legs"].items():
        ledger = leg["ledger"]
        maker = leg["maker"]
        rate = maker.get("fill_rate_contracts")
        tox = maker["toxicity"].get(horizon, {}).get("toxicity_rate")
        print(
            f"{name:<32}{leg['candidates']:>6}{leg['admitted']:>6}{leg['proposed_orders']:>6}{leg['paper_fills']:>6}"
            f"{float(ledger.get('realized_pnl') or 0):>10.4f}{float(ledger.get('unrealized_pnl') or 0):>10.4f}{float(ledger.get('fees_paid') or 0):>8.4f}"
            f"  {(('%.0f%%' % (float(rate) * 100)) if rate is not None else '  n/a'):>6} / {(('%.0f%%' % (float(tox) * 100)) if tox is not None else 'n/a'):>4}"
            f"   {leg['taker']['fill_events']}"
        )
        if leg["refused_by_reason"]:
            print(f"{'':<32}refused: {', '.join(f'{k}={v}' for k, v in leg['refused_by_reason'].items())}")
    ex_post = report["ex_post"]
    if ex_post.get("status") == "measured_from_settled_trades":
        whale = ex_post["classes"]["whale"]
        retail = ex_post["classes"]["retail_longshot"]
        print(f"\nex post ({ex_post['markets']} settled markets, {ex_post['trades']} trades): whale class n={whale.get('n_markets')} taker net/ct={whale.get('taker_net_per_contract')} t(taker)={whale.get('t_stat_taker_gross')}; retail longshot n={retail.get('n_markets')} maker net/ct={retail.get('maker_net_per_contract')} t(taker)={retail.get('t_stat_taker_gross')}")
        early = ex_post.get("excluding_final_minutes", {})
        if early:
            ew, er = early["classes"]["whale"], early["classes"]["retail_longshot"]
            print(f"  excluding final {early['minutes']} min: whale taker net/ct={ew.get('taker_net_per_contract')} t={ew.get('t_stat_taker_gross')} -> {early['verdicts']['whale_flow_ev_positive']['verdict']}; retail longshot maker net/ct={er.get('maker_net_per_contract')} t={er.get('t_stat_taker_gross')} -> {early['verdicts']['retail_longshot_fade_ev_positive']['verdict']}")
    else:
        print(f"\nex post: {ex_post.get('status')} - {ex_post.get('reason', '')}")


def parameters_from_args(args: argparse.Namespace) -> WhaleNoiseParameters:
    rule = WhaleRule(
        min_contracts=args.whale_min_contracts,
        size_multiple=args.whale_size_multiple,
        min_notional=args.whale_min_notional,
        ev_status="operator_asserted" if args.assume_whale_ev else "unverified",
        note="operator asserted via --assume-whale-ev" if args.assume_whale_ev else "",
    )
    series_rules: dict[str, WhaleRule] = {}
    if args.whale_registry is not None:
        rule, series_rules = load_whale_registry(json.loads(Path(args.whale_registry).read_text(encoding="utf-8")), rule)
    return WhaleNoiseParameters(
        series=tuple(args.series),
        default_rule=rule,
        series_rules=series_rules,
        ticks_behind=args.ticks_behind,
        reaction_latency_seconds=args.reaction_latency,
        quote_ttl_seconds=args.quote_ttl,
        later_arrivals_ahead=not args.assume_later_arrivals_behind,
        require_ev_verified=args.require_ev_verified,
        longshot_threshold=args.longshot_threshold,
        whale_lookback_seconds=args.whale_lookback,
        taker_cooldown_seconds=args.taker_cooldown,
        max_total_cash_at_risk=args.max_total_cash_at_risk,
        markout_horizons_seconds=tuple(sorted(set(args.markout_horizons))),
        min_fills_for_verdict=args.min_fills,
    )


async def run(
    *,
    use_network: bool,
    archive: Path | None,
    limit: int,
    artifact_dir: Path,
    harvest_dir: Path,
    harvest_trades: bool,
    settled_per_series: int,
    max_trades_per_market: int,
    kalshi_env: str | None,
    persist_ledgers: bool,
    reset_ledgers: bool,
    model_fees: bool,
    params: WhaleNoiseParameters,
    tickers: tuple[str, ...] = (),
    since: datetime | None = None,
    until: datetime | None = None,
    tape_window_seconds: int | None = 1800,
    min_markets: int = 10,
    min_contracts: float = 1000.0,
    exclude_final_minutes: int = 60,
) -> dict[str, Any]:
    require_paper_only("measure_whale_noise")
    if use_network and archive is not None:
        raise ValueError("choose one of --network or --archive")
    mode = "network" if use_network else "archive" if archive is not None else "fixtures"
    measured_at = _now()
    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-whale-{uuid.uuid4().hex[:8]}"
    if mode == "network":
        timelines, data_meta = await timelines_from_network(kalshi_env=kalshi_env, series=params.series, limit=limit, tape_window_seconds=tape_window_seconds)
    elif mode == "archive":
        assert archive is not None
        timelines, data_meta = timelines_from_archive(archive, series=params.series if not tickers else None, tickers=tickers or None, since=since, until=until)
        timelines = timelines[:limit] if limit else timelines
        data_meta["markets_after_limit"] = len(timelines)
    else:
        timelines, data_meta = load_fixture_timelines()
    ledgers = {} if reset_ledgers or not persist_ledgers else load_ledgers(artifact_dir, WHALE_NOISE_TRACKS)
    summaries, ledgers_by_track, replay = await run_whale_noise_tracks(
        timelines, params=params, ledgers=ledgers, model_fees=model_fees, cycle_label=f"whale_noise:{mode}:{run_id}", source=data_meta["source"],
    )

    fee_model = KalshiFeeModel() if model_fees else KalshiFeeModel.zero()
    if mode == "fixtures":
        markets, meta = load_settled_trades(EXPOST_FIXTURE_PATH)
        ex_post = {**whale_flow_expost(markets, params, fee_model=fee_model, min_markets=min_markets, min_contracts=min_contracts, exclude_final_minutes=exclude_final_minutes), "source": str(EXPOST_FIXTURE_PATH), "harvest_meta": meta, "note": "Synthetic fixture: exercises the pipeline, not evidence about Kalshi."}
    else:
        target = harvest_dir / HARVEST_FILE
        if harvest_trades and use_network:
            payload = await harvest_settled_trades(kalshi_env=kalshi_env or "prod", series=params.series, settled_per_series=settled_per_series, max_trades_per_market=max_trades_per_market)
            write_json(target, payload)
        if target.exists():
            markets, meta = load_settled_trades(target)
            ex_post = {**whale_flow_expost(markets, params, fee_model=fee_model, min_markets=min_markets, min_contracts=min_contracts, exclude_final_minutes=exclude_final_minutes), "source": str(target), "harvest_meta": meta}
        else:
            ex_post = not_measured_expost(f"no settled tennis-trade harvest at {target}; run with --network --harvest-trades")
    if ex_post["verdicts"]["whale_flow_ev_positive"].get("ev_status") in ("verified_expost", "refuted_expost") and params.default_rule.ev_status == "unverified":
        # The rule the replay used stays as it was; the report shows what ex post would relabel it to.
        ex_post["ev_status_note"] = f"replay ran with ev_status=unverified; ex post says {ex_post['verdicts']['whale_flow_ev_positive']['ev_status']}"

    report = build_report(
        timelines=timelines, data_meta=data_meta, summaries=summaries, replay=replay, ex_post=ex_post, params=params,
        mode=mode, kalshi_env=kalshi_env if mode == "network" else None, measured_at=measured_at, run_id=run_id, model_fees=model_fees,
    )
    report = to_jsonable(report)
    persist_run(
        artifact_dir=artifact_dir, report=report, summaries=summaries,
        ledgers=ledgers_by_track if persist_ledgers else {}, mode=mode, kalshi_env=kalshi_env if mode == "network" else None,
        limit=limit, measured_at=measured_at, run_id=run_id,
    )
    _print_report(report)
    print(f"\nartifacts: {artifact_dir / REPORT_NAME}  {artifact_dir / SCOREBOARD_NAME}  {artifact_dir / 'paper'}")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_argument_group("data source")
    src.add_argument("--network", action="store_true", help="one public Kalshi snapshot + recent tape per open tennis market (maker fills not simulated)")
    src.add_argument("--archive", type=Path, default=None, metavar="DIR", help="replay an apps.book_logger archive root (e.g. artifacts/books)")
    src.add_argument("--kalshi-env", choices=("demo", "prod"), default=None, help="Kalshi public API host (default: KALSHI_ENV or demo)")
    src.add_argument("--series", nargs="*", default=list(DEFAULT_TENNIS_SERIES), help="Kalshi tennis series (network universe / archive filter)")
    src.add_argument("--tickers", nargs="*", default=[], help="explicit tickers (archive filter; overrides --series)")
    src.add_argument("--since", type=str, default=None, help="archive replay window start (ISO-8601)")
    src.add_argument("--until", type=str, default=None, help="archive replay window end (ISO-8601)")
    src.add_argument("--tape-window", type=int, default=1800, help="network: only prints within N seconds before the snapshot trigger (default 1800)")
    src.add_argument("--limit", type=int, default=200, help="max markets")
    out = parser.add_argument_group("output")
    out.add_argument("--artifact-dir", type=Path, default=Path("artifacts"))
    out.add_argument("--no-persist", action="store_true", help="do not carry ledgers across runs")
    out.add_argument("--reset-ledgers", action="store_true")
    out.add_argument("--no-fees", action="store_true", help="disable the Kalshi fee model")
    whale = parser.add_argument_group("whale definition (size class; the tape is anonymous)")
    whale.add_argument("--whale-min-contracts", type=Decimal, default=Decimal("1000"))
    whale.add_argument("--whale-size-multiple", type=Decimal, default=Decimal("10"), help="x median of the market's recent prints")
    whale.add_argument("--whale-min-notional", type=Decimal, default=Decimal("500"))
    whale.add_argument("--whale-registry", type=Path, default=None, help='JSON {"default": {...}, "series": {"KXWTAMATCH": {...}}} of per-series rules / ev_status')
    whale.add_argument("--assume-whale-ev", action="store_true", help="label the default rule ev_status=operator_asserted (documented assertion, not evidence)")
    whale.add_argument("--require-ev-verified", action="store_true", help="maker leg quotes only when the rule's ev_status is verified_expost / operator_asserted")
    maker = parser.add_argument_group("maker leg")
    maker.add_argument("--ticks-behind", type=int, default=1)
    maker.add_argument("--reaction-latency", type=Decimal, default=Decimal("2"), help="seconds after the whale print before the quote can fill")
    maker.add_argument("--quote-ttl", type=Decimal, default=Decimal("300"), help="seconds a quote rests before it is cancelled")
    maker.add_argument("--assume-later-arrivals-behind", action="store_true", help="LESS conservative: do not lengthen the queue when a later book shows more size at our level")
    taker = parser.add_argument_group("taker leg")
    taker.add_argument("--longshot-threshold", type=Decimal, default=Decimal("0.20"))
    taker.add_argument("--whale-lookback", type=Decimal, default=Decimal("120"), help="seconds after a whale print during which the fade is skipped")
    taker.add_argument("--taker-cooldown", type=Decimal, default=Decimal("60"))
    caps = parser.add_argument_group("caps / measurement")
    caps.add_argument("--max-total-cash-at-risk", type=Decimal, default=Decimal("1000"))
    caps.add_argument("--markout-horizons", type=int, nargs="*", default=[60, 300], help="seconds")
    caps.add_argument("--min-fills", type=int, default=5, help="combined fills needed for a PASS/FAIL verdict")
    ex = parser.add_argument_group("ex post (settled tennis trades)")
    ex.add_argument("--harvest-dir", type=Path, default=Path("data/harvests"), help=f"directory holding {HARVEST_FILE}")
    ex.add_argument("--harvest-trades", action="store_true", help="fetch settled tennis markets + public trades now (network only)")
    ex.add_argument("--settled-per-series", type=int, default=30)
    ex.add_argument("--max-trades-per-market", type=int, default=4000)
    ex.add_argument("--min-markets", type=int, default=10)
    ex.add_argument("--min-contracts", type=float, default=1000.0)
    ex.add_argument("--exclude-final-minutes", type=int, default=60, help="ex-post variant that drops prints this close to market close")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    require_paper_only("measure_whale_noise")
    params = parameters_from_args(args)
    asyncio.run(
        run(
            use_network=args.network,
            archive=args.archive,
            limit=max(0, args.limit),
            artifact_dir=args.artifact_dir,
            harvest_dir=args.harvest_dir,
            harvest_trades=args.harvest_trades,
            settled_per_series=max(1, args.settled_per_series),
            max_trades_per_market=max(1, args.max_trades_per_market),
            kalshi_env=args.kalshi_env,
            persist_ledgers=not args.no_persist,
            reset_ledgers=args.reset_ledgers,
            model_fees=not args.no_fees,
            params=params,
            tickers=tuple(args.tickers),
            since=parse_ts(args.since),
            until=parse_ts(args.until),
            tape_window_seconds=args.tape_window if args.tape_window > 0 else None,
            min_markets=args.min_markets,
            min_contracts=args.min_contracts,
            exclude_final_minutes=args.exclude_final_minutes,
        )
    )


if __name__ == "__main__":
    main()
