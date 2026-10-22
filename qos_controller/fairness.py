"""Fairness and service metrics with an explicit comparison population.

Two different questions, kept separate on purpose:

* within_class_fairness - Jain's index over rates of *comparable* flows
  (same class).  Raw rates are comparable here.
* demand_satisfaction_fairness - Jain's index over served/demand ratios of
  classes that actually have demand.  Normalising by demand is what makes
  unlike classes comparable.  NOTE: strict priority protection (real-time
  before bulk) lowers this index BY DESIGN; read it together with
  min_service_ratio and the starvation list, not as a standalone score.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .models import TrafficClass


def jain_index(values: Iterable[float]) -> float | None:
    """(sum x)^2 / (n * sum x^2); 1.0 = perfectly equal.  None for empty input."""
    xs = list(values)
    if not xs:
        return None
    if any(x < 0 for x in xs):
        raise ValueError("values must be non-negative")
    sq = sum(x * x for x in xs)
    if sq == 0:
        return 1.0  # everyone equally got nothing
    return sum(xs) ** 2 / (len(xs) * sq)


jains_index = jain_index


def within_class_fairness(client_rates_mbps: Mapping[str, float]) -> float | None:
    """Jain over flows/clients of ONE class.  None when fewer than 2 flows."""
    if len(client_rates_mbps) < 2:
        return None
    return jain_index(client_rates_mbps.values())


@dataclass(frozen=True)
class ServiceReport:
    fairness_index: float | None            # demand-satisfaction Jain
    population: tuple[str, ...]             # classes included in the index
    service_ratio: Mapping[str, float]      # served/demand, capped at 1
    min_service_ratio: float | None
    min_service_class: str | None
    allocation_utilization: float | None    # served / allocated


def service_report(served_mbps: Mapping[TrafficClass, float],
                   demand_mbps: Mapping[TrafficClass, float],
                   allocated_mbps: Mapping[TrafficClass, float] | None = None,
                   min_demand_mbps: float = 0.05) -> ServiceReport:
    ratios = {c.value: min(1.0, served_mbps.get(c, 0.0) / d)
              for c, d in demand_mbps.items() if d >= min_demand_mbps}
    index = jain_index(ratios.values()) if ratios else None
    worst = min(ratios.items(), key=lambda kv: (kv[1], kv[0])) if ratios else (None, None)
    util = None
    if allocated_mbps:
        total_alloc = sum(allocated_mbps.values())
        if total_alloc > 0:
            util = min(1.0, sum(served_mbps.values()) / total_alloc)
    return ServiceReport(index, tuple(sorted(ratios)), ratios, worst[1], worst[0], util)
