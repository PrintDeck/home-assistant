"""Validated, bounded PrintDeck event journals and independent source cursors."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

EVENT_TYPES = ("started", "paused", "resumed", "completed", "failed", "cancelled",
               "milestone", "attention", "attention_cleared")
CONDITIONS = ("unknown", "normal", "ready", "busy", "attention", "error")
STREAM = re.compile(r"[0-9a-f]{16}")
JOB = re.compile(r"[0-9a-f]{16}-[1-9][0-9]{0,9}")
MAX_EVENT_AGE_MS = 30_000

@dataclass(frozen=True)
class PrintEvent:
    sequence: int
    event_type: str
    job_id: str | None
    observed_at_ms: int
    job_kind: str
    progress_percent: float | None
    milestone: int
    condition: str

@dataclass(frozen=True)
class PrintEvents:
    stream_id: str
    sequence: int
    job_id: str | None
    observed_at_ms: int
    events: tuple[PrintEvent, ...]


def _uint(value: Any, limit: int = 0xFFFFFFFF) -> int:
    if type(value) is not int or not 0 <= value <= limit:
        raise ValueError("Invalid event counter")
    return value


def _job(value: Any, stream: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not JOB.fullmatch(value)
                              or not value.startswith(stream + "-")
                              or int(value[17:]) > 0xFFFFFFFF):
        raise ValueError("Invalid local print session")
    return value


def parse_print_events(value: Any) -> PrintEvents | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("stream_id"), str):
        raise ValueError("Invalid event journal")
    stream = value["stream_id"]
    if not STREAM.fullmatch(stream):
        raise ValueError("Invalid event stream")
    sequence = _uint(value.get("sequence"))
    now = _uint(value.get("observed_at_ms"), 0x1FFFFFFFFFFFFF)
    job_id = _job(value.get("job_id"), stream)
    records = value.get("events")
    if not isinstance(records, list) or len(records) > 8:
        raise ValueError("Invalid event history size")
    events = []
    previous = 0
    for item in records:
        if not isinstance(item, dict) or item.get("stream_id") != stream:
            raise ValueError("Invalid event source")
        seq = _uint(item.get("sequence"))
        timestamp = _uint(item.get("observed_at_ms"), 0x1FFFFFFFFFFFFF)
        if not previous < seq <= sequence or timestamp > now:
            raise ValueError("Invalid event order")
        event_type = item.get("event_type")
        kind = item.get("job_kind")
        condition = item.get("condition")
        if event_type not in EVENT_TYPES or kind not in ("print", "calibration") or condition not in CONDITIONS:
            raise ValueError("Invalid event type")
        progress = item.get("progress_percent")
        if progress is not None and (type(progress) not in (int, float) or
                                     not 0 <= progress <= 100 or not math.isfinite(progress)):
            raise ValueError("Invalid event progress")
        milestone = _uint(item.get("milestone"))
        if (event_type == "milestone" and milestone not in (25, 50, 75)) or (event_type != "milestone" and milestone):
            raise ValueError("Invalid event milestone")
        events.append(PrintEvent(seq, event_type, _job(item.get("job_id"), stream),
                                 timestamp, kind, progress, milestone, condition))
        previous = seq
    if (events and events[-1].sequence != sequence) or (not events and sequence):
        raise ValueError("Incomplete event journal")
    return PrintEvents(stream, sequence, job_id, now, tuple(events))


class PrintEventCursor:
    """One cursor per profile on one PrintDeck; never deduplicate different hubs."""
    def __init__(self) -> None:
        self._seen: dict[str, tuple[str, int]] = {}

    def reset(self) -> None:
        self._seen.clear()

    def prune(self, profiles: set[str]) -> None:
        self._seen = {key: value for key, value in self._seen.items() if key in profiles}

    def consume(self, printer_id: str, history: PrintEvents | None, stale: bool = False) -> tuple[PrintEvent, ...]:
        if history is None:
            self._seen.pop(printer_id, None)
            return ()
        previous = self._seen.get(printer_id)
        self._seen[printer_id] = (history.stream_id, history.sequence)
        if stale or previous is None or previous[0] != history.stream_id:
            return ()
        if history.sequence < previous[1]:
            self._seen[printer_id] = previous
            return ()
        return tuple(event for event in history.events if event.sequence > previous[1]
                     and history.observed_at_ms - event.observed_at_ms <= MAX_EVENT_AGE_MS)
