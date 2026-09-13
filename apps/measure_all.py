import argparse
import asyncio
import json
import uuid
from decimal import Decimal
from enum import Enum
from pathlib import Path

def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, default=json_default, indent=2, sort_keys=True) + "\n"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _print_open(summaries: list[TrackSummary], label: str) -> None:
