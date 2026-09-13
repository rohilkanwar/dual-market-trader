
import asyncio
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
    return json.loads(path.read_text())


def _allowed_origins() -> list[str]:
    return [
        origin.strip().rstrip("/")
        for origin in os.getenv("DASHBOARD_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]


def _read_latest(artifact_dir: Path) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for mode, filename in (
    api.state.dashboard_service = service
    api.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=False,
        allow_methods=["GET", "POST"],
