"""Structured event logging and dependency-free metrics hooks."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol


class EventSink(Protocol):
    def emit(self, event: str, **fields: Any) -> None: ...


class Metrics(Protocol):
    def increment(self, name: str, value: int = 1, **tags: str) -> None: ...

    def gauge(self, name: str, value: float, **tags: str) -> None: ...


def _json_default(value: Any) -> Any:
    if isinstance(value, (Decimal, datetime)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


class LoggingEventSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("trading.events")

    def emit(self, event: str, **fields: Any) -> None:
        payload = {"event": event, **fields}
        self.logger.info(json.dumps(payload, default=_json_default, sort_keys=True))


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})


class NullMetrics:
    def increment(self, name: str, value: int = 1, **tags: str) -> None:
        del name, value, tags

    def gauge(self, name: str, value: float, **tags: str) -> None:
        del name, value, tags
