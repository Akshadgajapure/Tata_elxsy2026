"""Temporary user intent leases and multi-class prioritization profiles."""

from __future__ import annotations

import time
from typing import Callable

from .models import Intent, TrafficClass

# User-facing intents -> (Label, TrafficClass).
# Priority boost: prioritized class gets weight x2.0 and minimum floor +10pp.
INTENT_PROFILES: dict[str, tuple[str, TrafficClass | None]] = {
    "WORK_CALL": ("Work Call (Real-Time Priority)", TrafficClass.REAL_TIME),
    "GAMING": ("Competitive Gaming (Low Latency)", TrafficClass.INTERACTIVE),
    "VIDEO_CONFERENCE": ("Video Conference (Protected A/V)", TrafficClass.REAL_TIME),
    "STREAMING": ("4K Ultra HD Streaming (High Throughput)", TrafficClass.ADAPTIVE_VIDEO),
    "NORMAL": ("Fair Mode (Balanced / Reset)", None),
}


class IntentManager:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._intents: dict[str, Intent] = {}

    def set_priority(
        self, device_id: str, traffic_class: TrafficClass, duration_seconds: float
    ) -> Intent:
        if not device_id.strip():
            raise ValueError("device_id must not be empty")
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        intent = Intent(
            device_id=device_id,
            traffic_class=traffic_class,
            expires_at=self._clock() + duration_seconds,
        )
        self._intents[device_id] = intent
        return intent

    def get(self, device_id: str) -> Intent | None:
        intent = self._intents.get(device_id)
        if intent is None:
            return None
        if intent.expires_at <= self._clock():
            del self._intents[device_id]
            return None
        return intent

    def list_active(self) -> tuple[Intent, ...]:
        return tuple(i for i in (self.get(d) for d in tuple(self._intents)) if i is not None)

    def cancel(self, device_id: str) -> bool:
        return self._intents.pop(device_id, None) is not None

    def cancel_all(self) -> int:
        count = len(self._intents)
        self._intents.clear()
        return count

    def active_devices(self) -> tuple[str, ...]:
        return tuple(
            device_id
            for device_id in tuple(self._intents)
            if self.get(device_id) is not None
        )
