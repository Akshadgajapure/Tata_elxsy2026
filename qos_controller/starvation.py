"""Starvation guard: no class with real demand is silently starved forever."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from .allocation import ClassSpec
from .models import TrafficClass


@dataclass(frozen=True)
class StarvationConfig:
    threshold_s: float = 15.0          # starving this long -> guard triggers
    service_ratio_floor: float = 0.10  # served < 10% of entitled == starving
    min_entitled_mbps: float = 0.05    # ignore classes with negligible demand
    release_s: float = 5.0             # healthy this long -> guard releases
    recovery_mbps: float = 1.0         # bounded floor granted while guarded
    budget_fraction: float = 0.10      # all guarded floors together <= 10% of capacity


@dataclass(frozen=True)
class StarvationReport:
    starved: Mapping[TrafficClass, float]       # class -> seconds starving
    triggered: tuple[TrafficClass, ...]         # newly activated this tick
    released: tuple[TrafficClass, ...]
    active: tuple[TrafficClass, ...]             # guard currently active


@dataclass
class StarvationGuard:
    cfg: StarvationConfig = field(default_factory=StarvationConfig)

    def __post_init__(self) -> None:
        self._since: dict[TrafficClass, float] = {}
        self._healthy_since: dict[TrafficClass, float] = {}
        self._active: set[TrafficClass] = set()

    def update(self, now: float,
               samples: Mapping[TrafficClass, tuple[float, float]]) -> StarvationReport:
        """samples: class -> (served_mbps, entitled_mbps).  entitled is what the
        class should be getting: min(demand, allocation)."""
        c = self.cfg
        starved: dict[TrafficClass, float] = {}
        triggered, released = [], []
        for cls, (served, entitled) in samples.items():
            is_starving = entitled >= c.min_entitled_mbps and served < c.service_ratio_floor * entitled
            if is_starving:
                self._healthy_since.pop(cls, None)
                since = self._since.setdefault(cls, now)
                starved[cls] = now - since
                if now - since >= c.threshold_s and cls not in self._active:
                    self._active.add(cls)
                    triggered.append(cls)
            else:
                self._since.pop(cls, None)
                if cls in self._active:
                    h = self._healthy_since.setdefault(cls, now)
                    if now - h >= c.release_s:
                        self._active.discard(cls)
                        self._healthy_since.pop(cls, None)
                        released.append(cls)
        return StarvationReport(starved, tuple(triggered), tuple(released),
                                tuple(sorted(self._active, key=lambda k: k.value)))

    def apply(self, specs: list[ClassSpec], capacity_mbps: float) -> list[ClassSpec]:
        """Raise min/hard-floor of guarded classes, bounded by the budget, so
        higher-priority guarantees are still honoured afterwards."""
        guarded = [s for s in specs if s.cls in self._active]
        if not guarded:
            return specs
        per = min(self.cfg.recovery_mbps,
                  self.cfg.budget_fraction * capacity_mbps / len(guarded))
        out = []
        for s in specs:
            if s.cls in self._active:
                f = min(per, s.max_mbps)
                s = replace(s, hard_floor_mbps=max(s.hard_floor_mbps, f),
                            min_mbps=max(s.min_mbps, f))
            out.append(s)
        return out
