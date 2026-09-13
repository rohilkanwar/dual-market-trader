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
