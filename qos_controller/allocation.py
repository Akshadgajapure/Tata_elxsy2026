"""Deterministic, explainable, demand-based bandwidth allocation.

Pure function: no I/O, no clock, no randomness.  Same inputs -> same output.

Phases (each recorded in the per-class ``reasons``):
  0. hard floors      every class gets a tiny never-starve floor (scaled down
                      proportionally only if even the floors exceed capacity)
  1. min guarantees   up to min(min_mbps, demand), served in priority order, so
                      impossible minimums degrade the LOWEST priority first
  1b. protected demand REAL_TIME/INTERACTIVE demand (bounded by their max caps)
                      is served in priority order before elastic classes share
  2. demand sharing   remaining capacity is split by weight (progressive
                      filling / weighted max-min) up to each class's demand
  3. headroom         capacity nobody demanded is spread by weight up to each
                      class's max so a class that ramps up has room (optional)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .models import TrafficClass

EPS = 1e-9


@dataclass(frozen=True)
class ClassSpec:
    cls: TrafficClass
    priority: int            # lower number = more important
    weight: float            # share of contested capacity
    min_mbps: float          # guarantee (only up to actual demand)
    max_mbps: float          # ceiling
    hard_floor_mbps: float   # anti-starvation floor, reserved even when idle
    demand_mbps: float       # estimated demand (NOT current throughput)


@dataclass(frozen=True)
class ClassAllocation:
    cls: TrafficClass
    demand_mbps: float
    guaranteed_mbps: float   # after phases 0-1 (HTB "rate" candidate)
    allocated_mbps: float    # after phases 0-3
    ceil_mbps: float         # min(max_mbps, capacity) (HTB "ceil" candidate)
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AllocationResult:
    capacity_mbps: float
    allocations: Mapping[TrafficClass, ClassAllocation]
    total_allocated_mbps: float
    unallocated_mbps: float
    infeasible_minimums: bool
    notes: tuple[str, ...]


# priority, weight, min %, max %, hard floor %  (min/max/weight mirror PolicyEngine)
DEFAULT_CLASS_PARAMS: dict[TrafficClass, tuple[int, float, float, float, float]] = {
    TrafficClass.REAL_TIME:      (0, 5, 15, 40, 2),
    TrafficClass.INTERACTIVE:    (1, 4, 15, 35, 2),
    TrafficClass.ADAPTIVE_VIDEO: (2, 3, 30, 70, 2),
    TrafficClass.BULK:           (3, 2, 10, 60, 2),
    TrafficClass.BACKGROUND:     (4, 1, 5, 30, 2),
    TrafficClass.UNKNOWN:        (5, 1, 5, 25, 2),
}


def default_class_specs(capacity_mbps: float,
                        demand_mbps: Mapping[TrafficClass, float],
                        intent_class: TrafficClass | None = None,
                        intent_weight_boost: float = 2.0,
                        intent_min_boost_pp: float = 10.0) -> list[ClassSpec]:
    specs = []
    for cls, (prio, w, mn, mx, fl) in DEFAULT_CLASS_PARAMS.items():
        eff_w = float(w)
        eff_mn = mn
        if intent_class is not None and cls is intent_class:
            eff_w = max(1.0, w * intent_weight_boost)
            eff_mn = min(mx, mn + intent_min_boost_pp)
        specs.append(
            ClassSpec(cls, prio, eff_w, capacity_mbps * eff_mn / 100, capacity_mbps * mx / 100,
                      capacity_mbps * fl / 100, float(demand_mbps.get(cls, 0.0)))
        )
    return specs


def _validate(capacity_mbps: float, specs: list[ClassSpec]) -> None:
    if not capacity_mbps > 0 or math.isinf(capacity_mbps):
        raise ValueError("capacity_mbps must be positive and finite")
    if len({s.cls for s in specs}) != len(specs):
        raise ValueError("duplicate traffic class in specs")
    for s in specs:
        if s.weight <= 0:
            raise ValueError(f"{s.cls.value}: weight must be positive")
        if min(s.min_mbps, s.max_mbps, s.hard_floor_mbps, s.demand_mbps) < 0:
            raise ValueError(f"{s.cls.value}: negative rate")
        if s.max_mbps < s.min_mbps:
            raise ValueError(f"{s.cls.value}: max below min")


def _fill(order: list[ClassSpec], alloc: dict, remaining: float,
          target: dict, tag: str, reasons: dict) -> float:
    """Weighted progressive filling toward per-class targets."""
    def need(s: ClassSpec) -> float:
        return target[s.cls] - alloc[s.cls]

    active = [s for s in order if need(s) > EPS]
    while remaining > EPS and active:
        wsum = sum(s.weight for s in active)
        share = {s.cls: remaining * s.weight / wsum for s in active}
        satisfied = [s for s in active if need(s) <= share[s.cls] + EPS]
        if satisfied:
            for s in satisfied:
                n = need(s)
                alloc[s.cls] += n
                remaining -= n
                reasons[s.cls].append(f"{tag}_satisfied")
            active = [s for s in active if s not in satisfied]
        else:
            for s in active:
                alloc[s.cls] += share[s.cls]
                reasons[s.cls].append(f"{tag}_weighted_share")
            remaining = 0.0
    return max(0.0, remaining)


PROTECTED_CLASSES = frozenset({TrafficClass.REAL_TIME, TrafficClass.INTERACTIVE})


def allocate(capacity_mbps: float, specs: list[ClassSpec], headroom: bool = True,
             protected: frozenset = PROTECTED_CLASSES) -> AllocationResult:
    _validate(capacity_mbps, specs)
    order = sorted(specs, key=lambda s: (s.priority, s.cls.value))
    alloc = {s.cls: 0.0 for s in order}
    reasons: dict[TrafficClass, list[str]] = {s.cls: [] for s in order}
    notes: list[str] = []
    infeasible = False
    eff_demand = {s.cls: min(s.demand_mbps, s.max_mbps) for s in order}

    # Phase 0: hard floors.
    floors = {s.cls: min(s.hard_floor_mbps, s.max_mbps) for s in order}
    total_floor = sum(floors.values())
    if total_floor > capacity_mbps:
        scale = capacity_mbps / total_floor
        for c, f in floors.items():
            alloc[c] = f * scale
            reasons[c].append("hard_floor_scaled")
        remaining = 0.0
        infeasible = True
        notes.append("hard_floors_exceed_capacity: floors scaled proportionally")
    else:
        for c, f in floors.items():
            alloc[c] = f
            if f > 0:
                reasons[c].append("hard_floor")
        remaining = capacity_mbps - total_floor

    # Phase 1: minimum guarantees, strict priority order.
    for s in order:
        want = min(s.min_mbps, s.max_mbps, eff_demand[s.cls])
        extra = max(0.0, want - alloc[s.cls])
        give = min(extra, remaining)
        if give > EPS:
            reasons[s.cls].append("minimum_guarantee")
        if extra - give > EPS:
            infeasible = True
            notes.append(f"{s.cls.value}: minimum only partly met "
                         f"({alloc[s.cls] + give:.3f} of {want:.3f} Mbps)")
        alloc[s.cls] += give
        remaining -= give
    guaranteed = dict(alloc)

    # Phase 1b: protected (latency-sensitive) demand, bounded by max_mbps.
    for s in order:
        if s.cls in protected:
            give = min(max(0.0, eff_demand[s.cls] - alloc[s.cls]), remaining)
            if give > EPS:
                alloc[s.cls] += give
                remaining -= give
                reasons[s.cls].append("protected_demand")

    # Phase 2: share the rest according to demand and weight.
    remaining = _fill(order, alloc, remaining, eff_demand, "demand", reasons)

    # Phase 3: optional headroom up to each class's maximum.
    if headroom and remaining > EPS:
        caps = {s.cls: min(s.max_mbps, capacity_mbps) for s in order}
        remaining = _fill(order, alloc, remaining, caps, "headroom", reasons)

    out: dict[TrafficClass, ClassAllocation] = {}
    for s in order:
        a = math.floor(alloc[s.cls] * 1e6) / 1e6   # round down: sum never exceeds capacity
        if eff_demand[s.cls] < s.demand_mbps - EPS:
            reasons[s.cls].append("demand_capped_by_max")
        out[s.cls] = ClassAllocation(
            s.cls, round(s.demand_mbps, 6), round(math.floor(guaranteed[s.cls] * 1e6) / 1e6, 6),
            round(a, 6), round(min(s.max_mbps, capacity_mbps), 6),
            tuple(dict.fromkeys(reasons[s.cls])))
    total = round(sum(a.allocated_mbps for a in out.values()), 6)
    return AllocationResult(capacity_mbps, out, total, round(capacity_mbps - total, 6),
                            infeasible, tuple(notes))


def explain(result: AllocationResult) -> list[str]:
    """Human-readable decision text for the decision log / dashboard."""
    lines = [f"capacity={result.capacity_mbps:.3f} Mbps, allocated={result.total_allocated_mbps:.3f}, "
             f"unallocated={result.unallocated_mbps:.3f}"]
    for a in result.allocations.values():
        lines.append(f"{a.cls.value}: demand={a.demand_mbps:.3f} guaranteed={a.guaranteed_mbps:.3f} "
                     f"allocated={a.allocated_mbps:.3f} ceil={a.ceil_mbps:.3f} [{', '.join(a.reasons)}]")
    lines.extend(f"NOTE {n}" for n in result.notes)
    return lines


__all__ = ["ClassSpec", "ClassAllocation", "AllocationResult", "allocate",
           "default_class_specs", "explain", "DEFAULT_CLASS_PARAMS"]
