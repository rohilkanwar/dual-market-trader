import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from apps import dashboard_api


@asynccontextmanager
async def client_for(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_run_is_refused_when_environment_requests_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    app = dashboard_api.create_app(artifact_dir=tmp_path / "artifacts", harvest_dir=tmp_path / "h")

    async with client_for(app) as client:
        response = await client.post("/api/scoreboard/run", json={"mode": "fixtures"})

    assert response.status_code == 409
    assert "paper-only" in response.json()["detail"]


async def test_latest_includes_paper_loop_artifact(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "paper_loop_latest.json").write_text(
        '{"paper_only":true,"mode":"fixtures","cycle":4,'
        '"completed_at":"2026-09-10T22:00:00+00:00"}'
    )
    app = dashboard_api.create_app(
        artifact_dir=artifact_dir,
        harvest_dir=tmp_path / "harvests",
    )

    async with client_for(app) as client:
        response = await client.get("/api/scoreboard/latest")

    assert response.status_code == 200
    assert response.json()["artifacts"]["paper_loop"]["data"]["cycle"] == 4


async def test_fixture_run_job_writes_ledger_backed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
    artifact_dir = tmp_path / "artifacts"
    app = dashboard_api.create_app(artifact_dir=artifact_dir, harvest_dir=tmp_path / "h")

    async with client_for(app) as client:
        started = await client.post("/api/scoreboard/run", json={"mode": "fixtures", "limit": 5})
        assert started.status_code == 202
        job_id = started.json()["job_id"]
        service = app.state.dashboard_service
        await service._running
        job = await client.get(f"/api/jobs/{job_id}")
        latest = await client.get("/api/scoreboard/latest")

    assert job.json()["status"] == "completed", job.json()
    artifact = json.loads((artifact_dir / "scoreboard_fixtures.json").read_text())
    assert artifact["meta"]["source"] == "measured"
    assert artifact["portfolio"]["source"] == "ledger_aggregate"
    assert latest.json()["artifacts"]["fixtures"]["data"]["totals"]["paper_fills"] > 0
