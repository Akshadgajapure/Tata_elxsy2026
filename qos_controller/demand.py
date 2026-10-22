"""Per-class demand estimation.  Demand != current throughput.

A class that is being throttled shows throughput == its allocation even if it
wants far more.  So:  demand = max(EWMA rate, discounted recent peak), and when
the class is *saturated* (backlog/drops at its shaper, or running at its ceil)
the estimate grows multiplicatively each tick until the saturation clears.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .models import TrafficClass


@dataclass(frozen=True)
class DemandConfig:
    alpha: float = 0.4               # EWMA weight of the newest sample
    peak_window_s: float = 30.0
    peak_discount: float = 0.7       # a recent peak keeps demand >= 0.7 * peak
    saturation_growth: float = 1.3   # per-tick growth while saturated
    max_demand_mbps: float = 10_000.0


@dataclass(frozen=True)
class DemandEstimate:
    cls: TrafficClass
    current_mbps: float
    rolling_mbps: float
    recent_peak_mbps: float
    demand_mbps: float
    packet_rate: float
    active_flows: int
    saturated: bool


@dataclass
class _State:
    ewma: float | None = None
    demand: float = 0.0
    samples: deque = field(default_factory=deque)  # (ts, mbps)


class DemandEstimator:
    def __init__(self, config: DemandConfig | None = None) -> None:
        self.cfg = config or DemandConfig()
        if not 0 < self.cfg.alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        self._state: dict[TrafficClass, _State] = {}

    def update(self, now: float, cls: TrafficClass, rate_mbps: float, pps: float = 0.0,
               active_flows: int = 0, saturated: bool = False,
               cap_mbps: float | None = None) -> DemandEstimate:
        if rate_mbps < 0:
            raise ValueError("rate_mbps must be >= 0")
        c = self.cfg
        st = self._state.setdefault(cls, _State())
        st.ewma = rate_mbps if st.ewma is None else c.alpha * rate_mbps + (1 - c.alpha) * st.ewma
        st.samples.append((now, rate_mbps))
        while st.samples and now - st.samples[0][0] > c.peak_window_s:
            st.samples.popleft()
        peak = max(r for _, r in st.samples)
        demand = max(st.ewma, c.peak_discount * peak)
        if saturated:
            demand = max(demand, st.demand * c.saturation_growth, rate_mbps * c.saturation_growth)
        limit = min(c.max_demand_mbps, cap_mbps) if cap_mbps is not None else c.max_demand_mbps
        demand = min(demand, limit)
        st.demand = demand
        return DemandEstimate(cls, round(rate_mbps, 6), round(st.ewma, 6), round(peak, 6),
                              round(demand, 6), round(pps, 3), active_flows, saturated)

    def demands(self) -> dict[TrafficClass, float]:
        return {k: v.demand for k, v in self._state.items()}
