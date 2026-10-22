"""Per-tick control analysis (dry-run): demand -> congestion -> allocation
-> starvation -> fairness, using the Stage A modules.

Nothing here touches the system.  It consumes values the Runtime has already
measured and returns (results, events).  Metrics that are not measured stay
None; they are never filled with guesses.
"""

from __future__ import annotations

from collections import deque
from typing import Mapping

from .allocation import allocate, default_class_specs
from .capacity import CapacitySample, SaturationCapacityEstimator
from .congestion import CongestionDetector, CongestionSignals, CongestionState
from .demand import DemandEstimator
from .fairness import service_report
from .models import TrafficClass
from .stability import PolicyStabilizer
from .starvation import StarvationGuard

LOG_RATE_CHANGE_FRACTION = 0.10   # log a dry-run allocation change when any class moves >=10% of capacity


class ControlAnalysis:
    def __init__(self) -> None:
        self.demand = DemandEstimator()
        self.sat = SaturationCapacityEstimator()
        self.cong = CongestionDetector()
        self.starve = StarvationGuard()
        self.stabilizer = PolicyStabilizer()
        self._rates: dict[TrafficClass, float] | None = None
        self._logged_rates: dict[TrafficClass, float] | None = None
        self._rtt_baseline: float | None = None
        self._first = True
        self.history: deque[dict] = deque(maxlen=100)
        self.last: dict | None = None

    def step(self, now: float, classes: Mapping[str, dict], total_mbps: float,
             capacity_mbps: float | None, probe: dict | None,
             drops_per_s: float, wall: float | None = None,
             intent_class: TrafficClass | None = None,
             queue_delay_ms: float | None = None) -> tuple[dict, list[dict]]:
        if self._first:
            self.cong.since, self._first = now, False
        events: list[dict] = []
        probe = probe or {}

        # 1. demand (per class).  Class saturation is not measured without
        #    per-class tc stats, so saturated=False (documented in the UI).
        est = {}
        for cls in TrafficClass:
            c = classes.get(cls.value, {})
            est[cls] = self.demand.update(now, cls, c.get("mbps", 0.0), c.get("pps", 0.0),
                                          len(c.get("clients", [])), saturated=False)
        demands = {cls: e.demand_mbps for cls, e in est.items()}
        demand_total = round(sum(demands.values()), 6)

        # 2. saturation-based capacity (reported next to the placeholder, not instead of it)
        avg, mn, loss = probe.get("avg_ms"), probe.get("min_ms"), probe.get("loss_pct")
        if mn is not None:
            self._rtt_baseline = mn if self._rtt_baseline is None else min(self._rtt_baseline, mn)
        sat = self.sat.observe(CapacitySample(now, total_mbps, rtt_ms=avg, queue_delay_ms=None,
                                              loss_pct=loss, drops_delta=int(round(drops_per_s))))
        sat_ok = sat is not None and sat.method == "saturation_delivery_rate" and sat.confidence >= 0.5

        # 3. congestion state machine (only measured signals are passed)
        inflation = (avg - self._rtt_baseline) if (avg is not None and self._rtt_baseline is not None) else None
        signals = CongestionSignals(
            demand_ratio=(demand_total / sat.capacity_mbps) if sat_ok else None,
            queue_delay_ms=queue_delay_ms, loss_pct=loss, rtt_inflation_ms=inflation)
        change = self.cong.update(now, signals)
        if change:
            rec = {"ts": wall, "ts_mono": now, "previous": change.previous.value, "new": change.new.value,
                   "reason": change.reason, "signals": dict(change.signals),
                   "capacity_mbps": capacity_mbps, "demand_mbps": demand_total}
            self.history.append(rec)
            events.append({"kind": "CONGESTION_STATE_CHANGE",
                           "title": f"Congestion {change.previous.value} → {change.new.value}",
                           "level": {"CONGESTED": "danger", "WARNING": "warn"}.get(change.new.value, "info"),
                           "detail": {"previous": change.previous.value, "new": change.new.value,
                                      "reason": change.reason, "capacity_mbps": capacity_mbps,
                                      "demand_mbps": demand_total}})
        if sat_ok:
            util = {"value": round(total_mbps / sat.capacity_mbps, 4), "basis": "saturation_estimate"}
        elif capacity_mbps:
            util = {"value": round(total_mbps / capacity_mbps, 4), "basis": "placeholder_capacity"}
        else:
            util = {"value": None, "basis": None}

        # 4. allocation + stability (dry-run; nothing is enforced)
        allocation = None
        prev_rates = dict(self._rates) if self._rates else None
        if capacity_mbps:
            specs = self.starve.apply(
                default_class_specs(capacity_mbps, demands, intent_class=intent_class),
                capacity_mbps
            )
            res = allocate(capacity_mbps, specs)
            stab = self.stabilizer.stabilize(
                now, capacity_mbps, {c: a.allocated_mbps for c, a in res.allocations.items()},
                self._rates, congested=self.cong.state is CongestionState.CONGESTED)
            self._rates = dict(stab.rates_mbps)
            rows = {c.value: {"demand_mbps": a.demand_mbps, "guaranteed_mbps": a.guaranteed_mbps,
                              "target_mbps": a.allocated_mbps, "rate_mbps": self._rates.get(c),
                              "ceil_mbps": a.ceil_mbps, "reasons": list(a.reasons)}
                    for c, a in res.allocations.items()}
            allocation = {"capacity_mbps": capacity_mbps, "classes": rows,
                          "total_rate_mbps": round(sum(self._rates.values()), 6),
                          "infeasible_minimums": res.infeasible_minimums, "notes": list(res.notes),
                          "stability_notes": list(stab.notes), "enforced": False}
            ref = self._logged_rates
            if ref is None or max(abs(self._rates[c] - ref.get(c, 0.0)) for c in self._rates) \
                    >= LOG_RATE_CHANGE_FRACTION * capacity_mbps:
                self._logged_rates = dict(self._rates)
                events.append({"kind": "ALLOCATION_UPDATED",
                               "title": "Dry-run allocation updated (not enforced)", "level": "info",
                               "detail": {"capacity_mbps": capacity_mbps, "demand_mbps": demand_total,
                                          "allocation": ", ".join(f"{c.value}={v:.2f}" for c, v in self._rates.items()),
                                          "stability": "; ".join(stab.notes) or "none"}})

        # 5. starvation guard (served vs. what the class is entitled to)
        samples = {}
        for cls in TrafficClass:
            entitled = min(demands[cls], (prev_rates or {}).get(cls, demands[cls]))
            samples[cls] = (classes.get(cls.value, {}).get("mbps", 0.0), entitled)
        rep = self.starve.update(now, samples)
        for cls in rep.triggered:
            events.append({"kind": "STARVATION_GUARD_TRIGGERED", "level": "warn",
                           "title": f"Starvation guard triggered for {cls.value}",
                           "detail": {"class": cls.value, "starved_for_s": round(rep.starved.get(cls, 0), 1)}})
        for cls in rep.released:
            events.append({"kind": "STARVATION_GUARD_RELEASED", "level": "success",
                           "title": f"Starvation guard released for {cls.value}", "detail": {"class": cls.value}})

        # 6. fairness / service report
        fr = service_report({c: classes.get(c.value, {}).get("mbps", 0.0) for c in TrafficClass},
                            demands, self._rates)
        out = {
            "demand": {"total_mbps": demand_total,
                       "by_class": {c.value: {"current_mbps": e.current_mbps, "rolling_mbps": e.rolling_mbps,
                                              "peak_mbps": e.recent_peak_mbps, "demand_mbps": e.demand_mbps,
                                              "pps": e.packet_rate, "active_flows": e.active_flows,
                                              "saturated": e.saturated} for c, e in est.items()},
                       "note": "EWMA / recent-peak estimate per class. Class saturation is not measured "
                               "(no per-class tc stats), so demand cannot yet exceed observed rate."},
            "capacity_saturation": (None if sat is None else
                                    {"mbps": sat.capacity_mbps, "confidence": sat.confidence, "trend": sat.trend,
                                     "method": sat.method, "source_type": sat.source, "usable": sat_ok}),
            "congestion": {"state": self.cong.state.value, "duration_s": round(now - self.cong.since, 1),
                           "signals": signals.available(), "utilization": util,
                           "history": list(self.history)[-20:]},
            "allocation": allocation,
            "fairness": {"index": None if fr.fairness_index is None else round(fr.fairness_index, 4),
                         "population": list(fr.population),
                         "service_ratio": {k: round(v, 4) for k, v in fr.service_ratio.items()},
                         "min_service_ratio": None if fr.min_service_ratio is None else round(fr.min_service_ratio, 4),
                         "min_service_class": fr.min_service_class,
                         "allocation_utilization": None if fr.allocation_utilization is None
                         else round(fr.allocation_utilization, 4)},
            "starvation": {"starved_s": {c.value: round(s, 1) for c, s in rep.starved.items()},
                           "guard_active": [c.value for c in rep.active]},
        }
        self.last = out
        return out, events

    def thresholds(self) -> dict:
        cfg = self.cong.cfg
        return {"signals": {k: {"warn": w, "congested": c} for k, (w, c) in cfg.thresholds.items()},
                "exit_factor": cfg.exit_factor, "warn_ticks": cfg.warn_ticks,
                "congest_ticks": cfg.congest_ticks, "clear_ticks": cfg.clear_ticks,
                "recover_ticks": cfg.recover_ticks, "min_state_s": cfg.min_state_s,
                "congested_hold_s": cfg.congested_hold_s}


__all__ = ["ControlAnalysis"]
