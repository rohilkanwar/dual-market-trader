"""Weather scoreboard hooks for measure_all (weather_tracks contract).

Re-exports runners from strategy modules that have landed. Currently only
``weather_dead_bucket`` is present; sibling tracks append when merged.
"""

from __future__ import annotations

from research.weather_dead_bucket import build_weather_report, run_weather_tracks

__all__ = ["build_weather_report", "run_weather_tracks"]
