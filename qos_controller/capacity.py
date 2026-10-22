"""Capacity estimation with a bounded exponentially weighted moving average."""

from .models import CapacityEstimate


class CapacityEstimator:
    def __init__(self, alpha: float = 0.2, safety_margin: float = 0.92) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be greater than 0 and at most 1")
        if not 0 < safety_margin <= 1:
            raise ValueError("safety_margin must be greater than 0 and at most 1")
        self.alpha = alpha
        self.safety_margin = safety_margin
        self._estimate: float | None = None
        self._previous_sample: float | None = None

    def update(self, measured_mbps: float) -> CapacityEstimate:
        if measured_mbps <= 0:
            raise ValueError("measured_mbps must be positive")

        if self._estimate is None:
            self._estimate = measured_mbps
            trend = "initial"
        else:
            previous = self._estimate
            self._estimate = (
                self.alpha * measured_mbps + (1 - self.alpha) * self._estimate
            )
            if self._estimate > previous * 1.02:
                trend = "increasing"
            elif self._estimate < previous * 0.98:
                trend = "decreasing"
            else:
                trend = "stable"

        self._previous_sample = measured_mbps
        confidence = self._confidence()
        return CapacityEstimate(
            megabits_per_second=round(self._estimate * self.safety_margin, 3),
            confidence=confidence,
            trend=trend,
        )

    def _confidence(self) -> float:
        if self._estimate is None or self._previous_sample is None:
            return 0.0
        deviation = abs(self._previous_sample - self._estimate) / self._estimate
        return round(max(0.2, min(0.99, 0.95 - deviation)), 3)


# ---------------------------------------------------------------------------
# Stage A: capacity model that does not confuse throughput with capacity.
#
#   KNOWN_EMULATED_CAPACITY  configured by us in the testbed (e.g. tc rate)
#   ESTIMATED_CAPACITY       inferred from measurements; never "physical truth"
#
# The old CapacityEstimator above is kept unchanged (placeholder, still tested).
# ---------------------------------------------------------------------------
from collections import deque  # noqa: E402
from dataclasses import dataclass  # noqa: E402

KNOWN_EMULATED = "KNOWN_EMULATED_CAPACITY"
ESTIMATED = "ESTIMATED_CAPACITY"


class KnownCapacity:
    """Capacity of a bottleneck that WE configured in the testbed."""

    def __init__(self, mbps: float, method: str = "configured_bottleneck") -> None:
        if mbps <= 0:
            raise ValueError("mbps must be positive")
        self.mbps, self.method = float(mbps), method

    def set(self, mbps: float) -> None:
        if mbps <= 0:
            raise ValueError("mbps must be positive")
        self.mbps = float(mbps)

    def estimate(self, now: float) -> CapacityEstimate:
        return CapacityEstimate(self.mbps, 1.0, "configured", self.method, KNOWN_EMULATED, now)


@dataclass(frozen=True)
class CapacitySample:
    ts: float
    throughput_mbps: float
    rtt_ms: float | None = None
    queue_delay_ms: float | None = None
    loss_pct: float | None = None
    drops_delta: int = 0


@dataclass(frozen=True)
class SaturationConfig:
    window: int = 20                    # saturated samples kept
    saturation_queue_ms: float = 20.0
    saturation_rtt_inflation_ms: float = 15.0
    saturation_loss_pct: float = 0.5
    ewma_alpha: float = 0.3
    drop_ratio: float = 0.8             # saturated sample below 80% of estimate ...
    drop_ticks: int = 3                 # ... for this many ticks => fast downward revision
    safety_margin: float = 0.95
    stale_s: float = 30.0               # confidence decays after this long without saturation
    probe_headroom: float = 1.10
    probe_interval_s: float = 60.0
    min_sample_mbps: float = 0.5


class SaturationCapacityEstimator:
    """Infer capacity from *saturated* delivery, with honest lower-bound handling.

    * Saturated sample (queue delay / RTT inflation / loss / drops): delivered
      throughput ~ bottleneck capacity -> EWMA toward it.
    * Unsaturated sample: throughput is only a LOWER BOUND.  It may raise the
      estimate but never lowers it, and confidence decays with staleness.
    * Persistent saturated samples well below the estimate -> fast downward
      revision (adaptation to a capacity drop).
    * propose_probe(): controlled upward probe, because shaping to our own
      estimate would otherwise hide extra capacity (self-confirming loop).
      The caller raises the shaper by `probe_headroom`, then passes
      probing=True; the estimate rises only if no congestion signal appears.
    """

    def __init__(self, config: SaturationConfig | None = None) -> None:
        self.cfg = config or SaturationConfig()
        self._est: float | None = None
        self._last_saturated: float | None = None
        self._low_ticks = 0
        self._low_samples: list[float] = []
        self._baseline_rtt: float | None = None
        self._last_probe: float | None = None
        self._recent = deque(maxlen=self.cfg.window)

    def _saturated(self, s: CapacitySample) -> bool:
        c = self.cfg
        if s.queue_delay_ms is not None and s.queue_delay_ms >= c.saturation_queue_ms:
            return True
        if s.drops_delta > 0 or (s.loss_pct is not None and s.loss_pct >= c.saturation_loss_pct):
            return True
        if s.rtt_ms is not None and self._baseline_rtt is not None:
            return s.rtt_ms - self._baseline_rtt >= c.saturation_rtt_inflation_ms
        return False

    def observe(self, s: CapacitySample, probing: bool = False) -> CapacityEstimate | None:
        c = self.cfg
        if s.rtt_ms is not None:
            self._baseline_rtt = s.rtt_ms if self._baseline_rtt is None else min(self._baseline_rtt, s.rtt_ms)
        if s.throughput_mbps < c.min_sample_mbps:
            return self._current(s.ts)           # idle: no information, hold
        prev = self._est
        if self._saturated(s):
            self._last_saturated = s.ts
            self._recent.append(s.throughput_mbps)
            if self._est is None:
                self._est = s.throughput_mbps
            elif s.throughput_mbps < c.drop_ratio * self._est:
                self._low_ticks += 1
                self._low_samples.append(s.throughput_mbps)
                if self._low_ticks >= c.drop_ticks:
                    low = sorted(self._low_samples[-c.drop_ticks:])
                    self._est = low[len(low) // 2]       # median of the post-drop samples
                    self._recent.clear()
                    self._recent.extend(self._low_samples)
                    self._low_ticks, self._low_samples = 0, []
            else:
                self._low_ticks, self._low_samples = 0, []
                self._est = c.ewma_alpha * s.throughput_mbps + (1 - c.ewma_alpha) * self._est
        else:
            self._low_ticks, self._low_samples = 0, []
            if self._est is None or s.throughput_mbps > self._est or (
                    probing and s.throughput_mbps >= 0.95 * self._est):
                self._est = max(self._est or 0.0, s.throughput_mbps)  # lower bound only raises
        return self._current(s.ts, prev)

    def propose_probe(self, now: float) -> float | None:
        """Multiplier to apply to the shaper for a short probe, or None."""
        if self._est is None:
            return None
        if self._last_probe is None or now - self._last_probe >= self.cfg.probe_interval_s:
            self._last_probe = now
            return self.cfg.probe_headroom
        return None

    def _current(self, now: float, prev: float | None = None) -> CapacityEstimate | None:
        if self._est is None:
            return None
        c = self.cfg
        if prev is None or abs(self._est - prev) <= 0.02 * prev:
            trend = "stable"
        else:
            trend = "increasing" if self._est > prev else "decreasing"
        if self._last_saturated is None:
            conf, method = 0.3, "lower_bound_from_throughput"
        else:
            age = now - self._last_saturated
            conf = 0.9 if age <= c.stale_s else max(0.3, 0.9 - 0.01 * (age - c.stale_s))
            method = "saturation_delivery_rate"
        return CapacityEstimate(round(self._est * c.safety_margin, 3), round(conf, 3),
                                trend, method, ESTIMATED, now)
