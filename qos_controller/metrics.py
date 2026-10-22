"""Metrics collection and acceptance criteria evaluation (Priority 5).

Aggregates operational and experiment metrics:
- Interactive latency, jitter, loss
- Throughput and demand satisfaction
- Jain's fairness index
- Policy stability & adaptation speed
- Starvation tracking & deficit accounting
- Classification accuracy and uncertainty
- Resource utilization
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .fairness import jains_index
from .models import TrafficClass

log = logging.getLogger(__name__)


@dataclass
class PolicyChangeEvent:
    timestamp: float
    previous_capacity: float | None
    new_capacity: float
    reason: str


@dataclass
class SystemResourceSample:
    timestamp: float
    cpu_percent: float | None = None
    memory_rss_mb: float | None = None


class MetricsCollector:
    """Collects and calculates performance and fairness metrics for the QoS controller."""

    def __init__(self, history_window: int = 120) -> None:
        self.history_window = history_window
        self.policy_changes: deque[PolicyChangeEvent] = deque(maxlen=200)
        self.resource_samples: deque[SystemResourceSample] = deque(maxlen=history_window)
        self.starvation_events: list[dict[str, Any]] = []
        self.enforcement_events: deque[dict[str, Any]] = deque(maxlen=100)

        # Ground truth tracking for classifier accuracy validation
        self._ground_truth_matches = 0
        self._ground_truth_total = 0

    def record_policy_change(self, new_capacity: float,
                             previous_capacity: float | None = None,
                             reason: str = "") -> None:
        self.policy_changes.append(
            PolicyChangeEvent(
                timestamp=time.time(),
                previous_capacity=previous_capacity,
                new_capacity=new_capacity,
                reason=reason,
            )
        )

    def record_enforcement(self, action: str, success: bool, detail: str = "") -> None:
        self.enforcement_events.append({
            "timestamp": time.time(),
            "action": action,
            "success": success,
            "detail": detail,
        })

    def record_ground_truth(self, predicted_class: str, true_class: str) -> None:
        self._ground_truth_total += 1
        if predicted_class == true_class:
            self._ground_truth_matches += 1

    def sample_system_resources(self) -> SystemResourceSample:
        """Sample CPU and memory of the current process."""
        cpu = None
        rss_mb = None
        try:
            import psutil
            proc = psutil.Process()
            cpu = proc.cpu_percent(interval=None)
            rss_mb = round(proc.memory_info().rss / (1024 * 1024), 2)
        except Exception:
            pass

        sample = SystemResourceSample(timestamp=time.time(), cpu_percent=cpu, memory_rss_mb=rss_mb)
        self.resource_samples.append(sample)
        return sample

    def compute_policy_oscillation_rate(self, window_seconds: float = 60.0) -> float:
        """Calculate policy changes per minute."""
        now = time.time()
        recent = [e for e in self.policy_changes if (now - e.timestamp) <= window_seconds]
        if not recent:
            return 0.0
        return round(len(recent) * (60.0 / window_seconds), 2)

    def compute_demand_satisfaction(self,
                                   served_mbps: Mapping[TrafficClass | str, float],
                                   demanded_mbps: Mapping[TrafficClass | str, float]) -> dict[str, float]:
        """Per-class demand satisfaction ratio (served / demanded)."""
        ratios = {}
        for k, demand in demanded_mbps.items():
            key = k.value if isinstance(k, TrafficClass) else str(k)
            served = served_mbps.get(k, 0.0)
            if demand > 1e-6:
                ratios[key] = round(min(1.0, served / demand), 4)
            else:
                ratios[key] = 1.0  # no demand = 100% satisfied
        return ratios

    def compute_fairness(self, allocations: Sequence[float]) -> float | None:
        """Jain's Fairness Index for given allocation list."""
        return jains_index(allocations)

    def get_summary(self) -> dict[str, Any]:
        """Aggregate metrics overview."""
        accuracy = None
        if self._ground_truth_total > 0:
            accuracy = round(self._ground_truth_matches / self._ground_truth_total, 4)

        recent_res = self.resource_samples[-1] if self.resource_samples else None
        enf_total = len(self.enforcement_events)
        enf_success = sum(1 for e in self.enforcement_events if e["success"])

        return {
            "policy_changes_per_minute": self.compute_policy_oscillation_rate(),
            "total_policy_changes": len(self.policy_changes),
            "enforcement_success_rate": round(enf_success / max(1, enf_total), 4) if enf_total else 1.0,
            "classifier_accuracy": accuracy,
            "classifier_evaluated_samples": self._ground_truth_total,
            "cpu_percent": recent_res.cpu_percent if recent_res else None,
            "memory_rss_mb": recent_res.memory_rss_mb if recent_res else None,
        }
