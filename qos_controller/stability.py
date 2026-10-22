"""Policy stability: bounded, smoothed, rate-limited changes (no oscillation).

Applied to the *target* per-class rates before they reach enforcement:
deadband -> smoothing -> per-cycle step limit -> major-change interval.
A drop in capacity is the one thing that is NOT delayed: if the stabilised
rates exceed the (new) capacity they are scaled down at once, because
exceeding capacity is a safety violation, not a stability preference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

from .models import TrafficClass


@dataclass(frozen=True)
class StabilityConfig:
    smoothing: float = 0.5               # fraction of the gap closed per cycle
    deadband_abs_mbps: float = 0.25      # ignore changes smaller than this ...
    deadband_rel: float = 0.05           # ... or 5% of the current rate
    max_step_fraction: float = 0.25      # max change per cycle, as fraction of capacity
    min_step_mbps: float = 0.5
    major_change_fraction: float = 0.15  # a change this large (of capacity) is "major"
    min_major_interval_s: float = 10.0
    urgent_multiplier: float = 2.0       # larger steps for protected classes when CONGESTED
    protected: tuple[TrafficClass, ...] = (TrafficClass.REAL_TIME, TrafficClass.INTERACTIVE)


@dataclass(frozen=True)
class StabilizedRates:
    rates_mbps: Mapping[TrafficClass, float]
    changed: tuple[TrafficClass, ...]
    notes: tuple[str, ...]


@dataclass
class PolicyStabilizer:
    cfg: StabilityConfig = field(default_factory=StabilityConfig)

    def __post_init__(self) -> None:
        self._last_major: float | None = None

    def stabilize(self, now: float, capacity_mbps: float,
                  target: Mapping[TrafficClass, float],
                  current: Mapping[TrafficClass, float] | None,
                  congested: bool = False) -> StabilizedRates:
        c = self.cfg
        if current is None:
            self._last_major = now
            return StabilizedRates(dict(target), tuple(target), ("initial_policy",))
        major_ok = (self._last_major is None
                    or now - self._last_major >= c.min_major_interval_s)
        major_thr = c.major_change_fraction * capacity_mbps
        out: dict[TrafficClass, float] = {}
        changed: list[TrafficClass] = []
        notes: list[str] = []
        had_major = False
        for cls, tgt in target.items():
            cur = current.get(cls, tgt)
            gap = tgt - cur
            if abs(gap) <= max(c.deadband_abs_mbps, c.deadband_rel * cur):
                out[cls] = cur
                continue
            step = c.smoothing * gap
            limit = max(c.min_step_mbps, c.max_step_fraction * capacity_mbps)
            if congested and cls in c.protected and gap > 0:
                limit *= c.urgent_multiplier
            if abs(step) > limit:
                step = limit if step > 0 else -limit
                notes.append(f"{cls.value}: step limited")
            if abs(step) > major_thr:
                if major_ok:
                    had_major = True
                else:
                    step = major_thr if step > 0 else -major_thr
                    notes.append(f"{cls.value}: major change deferred (interval)")
            out[cls] = max(0.0, cur + step)
            changed.append(cls)
        if had_major:
            self._last_major = now
        total = sum(out.values())
        if total > capacity_mbps > 0:
            scale = capacity_mbps / total
            out = {k: v * scale for k, v in out.items()}
            notes.append("capacity_clamp: rates scaled to fit capacity immediately")
            changed = list(out)
        return StabilizedRates({k: math.floor(v * 1e6) / 1e6 for k, v in out.items()},
                               tuple(changed), tuple(notes))
