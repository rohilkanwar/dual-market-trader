"""Track-id contract and scoreboard hooks for the Polymarket weather paper tracks.

The weather strategies live on sibling branches and may not be merged yet. This
module carries no strategy code; it exists so the scoreboard, ``measure_all`` and
the dashboard agree on **which ids are weather tracks** and **what headline they
publish**, whether or not a strategy module is importable.

Contract for a weather strategy branch:

* emit one :class:`~research.scoreboard.TrackSummary` per track in
  :data:`WEATHER_TRACKS` (or a subset), with the id spelled exactly as listed;
* put the measured counts under ``summary.metrics`` using the
  :data:`METRIC_KEYS` names below (all optional; missing keys are reported as 0
  / ``None``, never invented);
* optionally expose ``research.weather_scoreboard.run_weather_tracks`` with the
  :class:`WeatherRunner` signature so ``measure_all`` picks the tracks up
  automatically, and ``research.weather_scoreboard.build_weather_report`` so
  ``persist_run`` writes ``weather_report_<mode>.json`` next to the board;
* a dedicated CLI should write ``scoreboard_weather.json`` with
  ``meta.track_family = "weather"`` (``persist_run(..., scoreboard_name=
  "scoreboard_weather.json", artifact_kwargs={"track_family": WEATHER_FAMILY,
  ...})``) so ``npm run sync-artifacts`` publishes it as its own run.

Paper only. Nothing here books, marks or reports PnL; that stays with the
per-track :class:`~core.ledger.PaperLedger`.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable, Iterable
from decimal import Decimal
from typing import Any, Protocol

WEATHER_FAMILY = "weather"
WEATHER_REPORT_KIND = "weather_report"
WEATHER_REPORT_FILE = "weather_report_<mode>.json"
WEATHER_SCOREBOARD_NAME = "scoreboard_weather.json"

#: Reserved ids, in display order. Mirrored in dashboard/scripts/track-families.mjs.
WEATHER_TRACKS: tuple[str, ...] = (
    "weather_bucket_edge",
    "weather_dead_bucket",
    "weather_calibrated_ensemble",
)
WEATHER_PRIMARY_TRACK = "weather_bucket_edge"
WEATHER_TRACK_LABELS: dict[str, str] = {
    "weather_bucket_edge": "Weather bucket edge (ensemble vs. mid)",
    "weather_dead_bucket": "Weather dead bucket (late-day METAR)",
    "weather_calibrated_ensemble": "Weather calibrated ensemble",
}

#: ``summary.metrics`` keys the headline is reduced from. Strategy branches
#: should use these names; anything else is carried on the board untouched.
METRIC_KEYS: dict[str, str] = {
    "status": "one word for why the lane looks the way it does (e.g. no_weather_markets, fixture_synthetic, measured)",
    "source": "{name, ...}: forecast / observation feed used",
    "markets": "weather markets seen in the snapshot",
    "buckets": "temperature buckets (outcomes) evaluated",
    "cities": "list[str] of city / station names covered",
    "stations": "METAR / observation stations requested",
    "stations_parsed": "stations whose observation parsed",
    "ensemble_edge": "{n, mean_bps}: ensemble-vs-mid edge samples",
    "dead_bucket": "{candidates, kills}: buckets ruled out by a late-day observation",
    "calibration": "{n, status}: calibration samples behind the ensemble",
    "evaluation": "{status, preregistered_n, ...}: pre-registered test state",
}

#: Runner module / attribute ``measure_all`` looks for. Absent -> no weather tracks.
WEATHER_RUNNER_MODULE = "research.weather_scoreboard"
WEATHER_RUNNER_ATTR = "run_weather_tracks"
WEATHER_REPORT_BUILDER_ATTR = "build_weather_report"

EVALUATION_STATUSES = ("not_run", "no_candidates", "pending_resolutions", "underpowered", "pass", "fail")


class WeatherRunner(Protocol):
    """``run_weather_tracks(snapshots, *, ledgers, starting_cash, model_fees, use_fixtures, cycle_label)``.

    Returns ``(summaries, ledgers_by_track)`` like :func:`research.scoreboard.run_flb_tracks`.
    """

    def __call__(
        self,
        snapshots: dict[Any, Any],
        *,
        ledgers: dict[str, Any] | None,
        starting_cash: Decimal,
        model_fees: bool,
        use_fixtures: bool,
        cycle_label: str,
    ) -> Awaitable[tuple[list[Any], dict[str, Any]]]: ...


def is_weather_track(track: str | None) -> bool:
    """Reserved id, or any id a human would read as weather (``weather_*``, ``*_metar_*``)."""
    if not track:
        return False
    if track in WEATHER_TRACKS:
        return True
    tokens = set(track.lower().replace("-", "_").split("_"))
    return bool(tokens & {"weather", "metar", "temperature", "ensemble", "nws", "noaa"})


def weather_summaries(summaries: Iterable[Any]) -> list[Any]:
    return [s for s in summaries if is_weather_track(getattr(s, "track", None))]


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _num(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - anything unparsable is "not measured"
        return None


def _first(metrics_list: list[dict[str, Any]], *path: str) -> Any:
    """First non-``None`` value found at ``path`` across the tracks' metrics."""
    for metrics in metrics_list:
        node: Any = metrics
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if node is not None:
            return node
    return None


def _sum(metrics_list: list[dict[str, Any]], *path: str) -> int:
    total = 0
    for metrics in metrics_list:
        node: Any = metrics
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        total += _int(node)
    return total


def weather_finding(summaries: Iterable[Any]) -> dict[str, Any] | None:
    """Compact ``findings.weather`` headline reduced from the weather tracks' metrics.

    Returns ``None`` when no weather track is present, so boards written before
    the weather branches merge are byte-for-byte unchanged. Every number is read
    from the metrics as stated; missing keys read as ``0`` / ``None``, and the
    hypothesis is validated only when the evaluation itself says ``pass``.
    """
    tracks = weather_summaries(summaries)
    if not tracks:
        return None
    metrics = [dict(getattr(s, "metrics", {}) or {}) for s in tracks]
    cities: list[str] = []
    for m in metrics:
        for city in m.get("cities") or []:
            if isinstance(city, str) and city not in cities:
                cities.append(city)
    stations = _sum(metrics, "stations")
    parsed = _sum(metrics, "stations_parsed")
    evaluation = _first(metrics, "evaluation") or {}
    evaluation_status = evaluation.get("status") if isinstance(evaluation, dict) else None
    if evaluation_status not in EVALUATION_STATUSES:
        evaluation_status = "not_run" if evaluation_status is None else str(evaluation_status)
    source = _first(metrics, "source")
    return {
        "status": str(_first(metrics, "status") or "not_run"),
        "source": source.get("name") if isinstance(source, dict) else (source if isinstance(source, str) else None),
        "tracks": [s.track for s in tracks],
        "markets": _sum(metrics, "markets"),
        "buckets": _sum(metrics, "buckets"),
        "cities": cities,
        "stations": stations,
        "stations_parsed": parsed,
        "station_parse_rate": (Decimal(parsed) / Decimal(stations)).quantize(Decimal("0.0001")) if stations else None,
        "ensemble_edge_n": _sum(metrics, "ensemble_edge", "n"),
        "ensemble_edge_mean_bps": _num(_first(metrics, "ensemble_edge", "mean_bps")),
        "dead_bucket_candidates": _sum(metrics, "dead_bucket", "candidates"),
        "dead_bucket_kills": _sum(metrics, "dead_bucket", "kills"),
        "calibration_n": _sum(metrics, "calibration", "n"),
        "preregistered_n": _num(evaluation.get("preregistered_n")) if isinstance(evaluation, dict) else None,
        "evaluation_status": evaluation_status,
        "hypothesis_validated": evaluation_status == "pass",
        "candidates": sum(_int(getattr(s, "candidates", 0)) for s in tracks),
        "admitted": sum(_int(getattr(s, "admitted", 0)) for s in tracks),
        "paper_fills": sum(_int(getattr(s, "paper_fills", 0)) for s in tracks),
        "note": (
            "Weather tracks are paper only; pass/fail is declared solely by the strategy's "
            "pre-registered evaluation. Per-market rows live in weather_report_<mode>.json."
        ),
    }


def slim_weather_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Keep the board small: any list of rows becomes ``{count, detail}``.

    The strategy branches choose their own row keys (observations, buckets,
    ensemble members, kills ...); rather than enumerate them, every list of
    dicts is reduced. Lists of scalars (``cities``) are kept.
    """
    slim = dict(metrics)
    for key, value in list(slim.items()):
        if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
            slim[key] = {"count": len(value), "detail": WEATHER_REPORT_FILE}
    return slim


def _load_attr(name: str) -> Callable[..., Any] | None:
    try:
        module = importlib.import_module(WEATHER_RUNNER_MODULE)
    except ImportError:
        return None
    attr = getattr(module, name, None)
    return attr if callable(attr) else None


def load_weather_runner() -> WeatherRunner | None:
    """``research.weather_scoreboard.run_weather_tracks`` when a strategy branch provides it, else ``None``."""
    return _load_attr(WEATHER_RUNNER_ATTR)  # type: ignore[return-value]


def load_weather_report_builder() -> Callable[..., dict[str, Any]] | None:
    """``research.weather_scoreboard.build_weather_report(summaries, *, mode, measured_at, run_id)`` or ``None``."""
    return _load_attr(WEATHER_REPORT_BUILDER_ATTR)
