"""FastAPI surface for the dashboard: read artifacts, start paper measurements.

Paper-only. ``create_app`` refuses to start a run if the process environment
requests live trading, and the app never constructs a non-paper venue client.
No authentication is implemented; see README before exposing publicly.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from apps.measure_all import load_ledgers, persist_run, to_jsonable, write_json
from core.config import live_environment_requested
from research.scoreboard import TRACKS, measure_all_with_ledgers
from research.specialist_scoreboard import load_specialist_state

ARTIFACT_FILES = (
    ("latest", "scoreboard_latest.json"),
    ("fixtures", "scoreboard_fixtures.json"),
    ("network", "scoreboard_network.json"),
    ("paper_loop", "paper_loop_latest.json"),
    ("paper_ledger_primary", "paper/ledger_single_venue_fair_value.json"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _allowed_origins() -> list[str]:
    return [
        origin.strip().rstrip("/")
        for origin in os.getenv("DASHBOARD_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]


def _read_latest(artifact_dir: Path) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for mode, filename in ARTIFACT_FILES:
        path = artifact_dir / filename
        if not path.exists():
            continue
        try:
            data = _read_json(path)
        except json.JSONDecodeError as exc:
            artifacts[mode] = {"path": str(path), "error": f"invalid JSON: {exc}"}
            continue
        artifacts[mode] = {
            "path": str(path),
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            "data": data,
        }
    return artifacts


@dataclass(slots=True)
class Job:
    job_id: str
    mode: str
    status: str = "queued"
    started_at: str = field(default_factory=_now)
    completed_at: str | None = None
    error: str | None = None
    artifact_path: str | None = None


class RunRequest(BaseModel):
    mode: str = "fixtures"
    limit: int = 25


class DashboardService:
    def __init__(self, artifact_dir: Path, harvest_dir: Path) -> None:
        self.artifact_dir = artifact_dir
        self.harvest_dir = harvest_dir
        self.jobs: dict[str, Job] = {}
        self._running: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._running is not None and not self._running.done()

    def latest(self) -> dict[str, Any]:
        return {"paper_only": True, "generated_at": _now(), "artifacts": _read_latest(self.artifact_dir)}

    def start(self, request: RunRequest) -> Job:
        if live_environment_requested():
            raise HTTPException(
                status_code=409,
                detail="dashboard API is paper-only; refusing while TRADING_MODE=live or "
                "ENABLE_LIVE_TRADING=true is set",
            )
        if request.mode not in ("fixtures", "network"):
            raise HTTPException(status_code=422, detail="mode must be 'fixtures' or 'network'")
        if self.running:
            raise HTTPException(status_code=409, detail="a paper measurement is already running")
        job = Job(job_id=uuid.uuid4().hex, mode=request.mode)
        self.jobs[job.job_id] = job
        self._running = asyncio.create_task(self._run(job, max(1, request.limit)))
        return job

    async def _run(self, job: Job, limit: int) -> None:
        job.status = "running"
        try:
            summaries, ledgers = await measure_all_with_ledgers(
                use_fixtures=job.mode == "fixtures",
                limit=limit,
                ledgers=load_ledgers(self.artifact_dir, TRACKS),
                specialist_state=load_specialist_state(self.artifact_dir),
            )
            artifact = await asyncio.to_thread(
                persist_run,
                summaries,
                ledgers,
                artifact_dir=self.artifact_dir,
                mode=job.mode,
                measured_at=_now(),
                limit=limit,
                kalshi_env=None,
            )
            job.artifact_path = str(self.artifact_dir / f"scoreboard_{job.mode}.json")
            job.status = "completed"
            del artifact
        except Exception as exc:  # surfaced through the job record
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.completed_at = _now()
            write_json(self.artifact_dir / "jobs" / f"{job.job_id}.json", asdict(job))


def create_app(
    *,
    artifact_dir: Path = Path("artifacts"),
    harvest_dir: Path = Path("data/harvests"),
) -> FastAPI:
    service = DashboardService(artifact_dir, harvest_dir)
    api = FastAPI(title="dual-market-trader paper API", version="0.2.0")
    api.state.dashboard_service = service
    api.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @api.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "paper_only": True,
            "live_environment_requested": live_environment_requested(),
            "running": service.running,
        }

    @api.get("/api/scoreboard/latest")
    async def latest() -> dict[str, Any]:
        return to_jsonable(service.latest())

    @api.post("/api/scoreboard/run", status_code=202)
    async def run(request: RunRequest) -> dict[str, Any]:
        return asdict(service.start(request))

    @api.get("/api/jobs/{job_id}")
    async def job(job_id: str) -> dict[str, Any]:
        record = service.jobs.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return asdict(record)

    return api


app = create_app()
