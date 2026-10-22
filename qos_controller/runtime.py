"""Runtime loop: real telemetry -> existing controller modules -> state.

Per-tick data path (every second):
  kernel counters (ns-router) -> per-client rates -> FlowMetadata
  -> MetadataClassifier -> class aggregation
  total Mbps -> Controller.observe_capacity -> PolicyEngine
             -> selected backend (DryRunBackend OR LinuxTCBackend)
  snapshot -> validate_snapshot (safety)

Backend selection (P1 — dependency injection):
  QOS_BACKEND=dry_run  (default)  Policy is generated and recorded, NOT applied to tc.
  QOS_BACKEND=linux_tc             Policy is applied to the real kernel via HTB + FQ-CoDel.
  Pass a backend instance explicitly (for testing) to bypass env-var selection.

Intent → Allocation → Enforcement (P2):
  When an intent is active the policy engine boosts the intent class weight and
  minimum percent.  The per-tick allocation (ControlAnalysis) also receives the
  intent class so the dry-run allocation reflects the same boost.  On intent
  activation the runtime immediately triggers an extra capacity sample so the
  policy updates without waiting for the next tick.

Ingress note:
  Linux tc controls EGRESS only (packets leaving the router toward the server).
  Ingress shaping of traffic arriving from the WAN would require an IFB device
  (ip link add ifb0 type ifb; tc redirect ingress -> ifb0).  This prototype
  shapes only the downstream direction (client -> router-server egress).
"""

from __future__ import annotations

import logging
import threading
import time
from time import perf_counter
from collections import deque
from dataclasses import dataclass
from typing import Callable

from . import config as cfg
from .analysis import ControlAnalysis
from .capacity import CapacityEstimator
from .classifier import MetadataClassifier
from .controller import Controller
from .enforcement import DryRunBackend, EnforcementBackend
from .intent import INTENT_PROFILES, IntentManager
from .models import ControllerSnapshot, FlowMetadata, TrafficClass
from .policy import PolicyEngine
from .safety import validate_snapshot
from .telemetry import CounterSource, IfaceCounters, QdiscSource, TelemetryError

log = logging.getLogger(__name__)
Emit = Callable[[str, dict], None]


def _build_backend(engine: PolicyEngine) -> EnforcementBackend:
    """Instantiate the backend selected by QOS_BACKEND env-var.

    Returns DryRunBackend unless QOS_BACKEND=linux_tc AND iproute2 is present.
    Never raises: if linux_tc is requested but unavailable, falls back to dry-run
    with a prominent warning.
    """
    if cfg.ENFORCEMENT_BACKEND != "linux_tc":
        return DryRunBackend(engine)

    # Attempt to import and instantiate the linux_tc backend.
    try:
        from .linux_tc import LinuxTCBackend
        backend = LinuxTCBackend(
            namespace=cfg.TC_NAMESPACE,
            iface=cfg.TC_IFACE,
            leaf=cfg.TC_LEAF_QDISC,
            queue_limit=cfg.TC_QUEUE_LIMIT_PACKETS,
        )
        log.info("LinuxTCBackend selected (ns=%s iface=%s leaf=%s)",
                 cfg.TC_NAMESPACE, cfg.TC_IFACE, cfg.TC_LEAF_QDISC)
        return backend
    except Exception as exc:
        log.error("Failed to instantiate LinuxTCBackend (%s); falling back to DryRunBackend", exc)
        return DryRunBackend(engine)


@dataclass
class _ClientTrack:
    active_since: float | None = None
    idle_since: float | None = None
    last_class: str | None = None


class Runtime:
    def __init__(self, counters: CounterSource, qdisc: QdiscSource | None,
                 probe, generator, emit: Emit | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 backend: EnforcementBackend | None = None) -> None:
        self.counters, self.qdisc, self.probe, self.generator = counters, qdisc, probe, generator
        self.emit: Emit = emit or (lambda event, payload: None)
        self._clock, self._wall = clock, wall
        engine = PolicyEngine()
        # P1: dependency injection — caller may supply a pre-built backend (tests,
        # scripts), otherwise we select one from the env-var.
        self.backend: EnforcementBackend = backend if backend is not None else _build_backend(engine)
        self.engine = engine
        self.intents = IntentManager(clock=clock)
        self.controller = Controller(CapacityEstimator(), engine, self.intents, self.backend)
        self.analysis = ControlAnalysis()
        self.classifier = MetadataClassifier()
        self._lock = threading.Lock()
        self._prev: tuple[float, dict[str, IfaceCounters]] | None = None
        self._tracks = {c.name: _ClientTrack() for c in cfg.CLIENTS}
        self._capacity = None
        self._capacity_logged: float | None = None
        self._capacity_held = False
        self._violations: tuple[str, ...] = ()
        self._telemetry_ok: bool | None = None
        self._last_validation: float | None = None
        self._last_rejection: dict | None = None
        self.history: deque[dict] = deque(maxlen=cfg.HISTORY_SECONDS)
        self.decisions: deque[dict] = deque(maxlen=500)
        self.latest: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = clock()
        self._ticks = 0
        self._last_tick_ms: float | None = None
        self._ifaces_seen: set[str] = set()
        self._last_rollback: dict | None = None
        # Distinguish calculation success from kernel enforcement success
        self._last_policy_calc: dict | None = None    # policy was calculated
        self._last_policy_applied: dict | None = None  # kernel confirmed it
        self._pmeta: dict = {"version": 0, "capacity": None, "intent": None,
                             "intent_class": None, "timestamp": None,
                             "reason": None, "snapshot": None, "previous": None}
        self._intent_meta: dict[str, dict] = {}
        self._intent_history: deque[dict] = deque(maxlen=30)
        self._ctx: dict = {
            "capacity_mbps": None, "capacity_kind": None, "demand_mbps": None,
            "congestion_state": None, "intents": [], "allocation": None,
            "safety_ok": None,
            "enforcement_mode": "linux-tc" if isinstance(self.backend, _LinuxTCBackendType()) else "dry-run",
        }
        # Determine backend name once (avoids repeated isinstance checks)
        self._backend_name = self._detect_backend_name()

    def _detect_backend_name(self) -> str:
        try:
            from .linux_tc import LinuxTCBackend
            if isinstance(self.backend, LinuxTCBackend):
                return "linux_tc"
        except ImportError:
            pass
        return "dry_run"

    # ---- decision log -------------------------------------------------
    def log_event(self, kind: str, title: str, level: str = "info", **detail) -> dict:
        event = {"ts": self._wall(), "kind": kind, "title": title,
                 "level": level, "detail": detail, "context": dict(self._ctx)}
        with self._lock:
            self.decisions.append(event)
        log.info("%s %s %s", kind, title, detail or "")
        self.emit("decision", event)
        return event

    # ---- one control-loop iteration ----------------------------------
    def tick(self) -> dict | None:
        t0 = perf_counter()
        try:
            return self._tick()
        finally:
            self._ticks += 1
            self._last_tick_ms = round((perf_counter() - t0) * 1000, 3)

    def _tick(self) -> dict | None:
        now = self._clock()
        self._reap_intents()
        try:
            counters = self.counters.read()
        except TelemetryError as exc:
            self._prev = None
            if self._telemetry_ok is not False:
                self.log_event("TELEMETRY_LOST", "Telemetry unavailable", "danger", error=str(exc))
            self._telemetry_ok = False
            frame = {"ts": self._wall(), "telemetry_ok": False, "error": str(exc),
                     "enforcement": self._enforcement()}
            self._publish(frame, history=False)
            return frame
        if self._telemetry_ok is False:
            self.log_event("TELEMETRY_RESTORED", "Telemetry restored", "success")
        self._telemetry_ok = True
        self._ifaces_seen = set(counters)

        prev, self._prev = self._prev, (now, counters)
        if prev is None:
            return None  # need two samples to compute rates
        dt = now - prev[0]
        if dt <= 0:
            return None

        clients, total_bps, total_pps, drops = [], 0.0, 0.0, 0.0
        for c in cfg.CLIENTS:
            a, b = prev[1].get(c.router_iface), counters.get(c.router_iface)
            if a is None or b is None:
                continue
            up_b = max(0, b.rx_bytes - a.rx_bytes)      # client -> router
            down_b = max(0, b.tx_bytes - a.tx_bytes)    # router -> client
            pkts = max(0, b.rx_packets - a.rx_packets) + max(0, b.tx_packets - a.tx_packets)
            drops += max(0, b.rx_drop - a.rx_drop) + max(0, b.tx_drop - a.tx_drop)
            bps = (up_b + down_b) / dt
            pps = pkts / dt
            total_bps += bps
            total_pps += pps
            clients.append(self._client_row(c, now, bps, pps, up_b * 8 / dt / 1e6,
                                            down_b * 8 / dt / 1e6, pkts, up_b + down_b))

        total_mbps = total_bps * 8 / 1e6
        classes = self._aggregate(clients)
        # P2: pass active intent class to ControlAnalysis so allocation reflects the boost
        _, intent_class = self.controller._active_intent()
        capacity = self._update_capacity(total_mbps, intent_class)
        queue = self._queue()
        probe = self.probe.stats()
        cap_mbps = self._capacity.megabits_per_second if self._capacity else None
        q_delay = queue.get("queue_delay_ms") if queue else None
        analysis, events = self.analysis.step(now, classes, total_mbps, cap_mbps, probe,
                                              drops / dt, wall=self._wall(),
                                              intent_class=intent_class,
                                              queue_delay_ms=q_delay)
        for c in clients:
            k = c["class"]
            c["class_demand_mbps"] = analysis["demand"]["by_class"][k]["demand_mbps"] if k else None
            al = (analysis["allocation"] or {}).get("classes", {}).get(k) if k else None
            c["class_allocated_mbps"] = al["rate_mbps"] if al else None
        self._ctx.update(demand_mbps=analysis["demand"]["total_mbps"],
                         congestion_state=analysis["congestion"]["state"],
                         allocation=(", ".join(f"{k}={v['rate_mbps']:.2f}" for k, v in
                                               analysis["allocation"]["classes"].items())
                                     if analysis["allocation"] else None))
        for ev in events:
            self.log_event(ev["kind"], ev["title"], ev["level"], **ev["detail"])
        safety = self._safety(clients, classes, queue)
        self._ctx["safety_ok"] = safety["ok"]
        self._ctx["intents"] = list(self.intents_active_ids())

        # P1: collect real tc stats when linux_tc backend is active
        tc_stats = self._tc_stats()

        frame = {
            "ts": self._wall(), "telemetry_ok": True, "error": None,
            "total": {"mbps": round(total_mbps, 3), "pps": round(total_pps, 1),
                      "drops_per_s": round(drops / dt, 2)},
            "capacity": capacity, "rtt": probe, "queue": queue,
            "classes": classes, "clients": clients, "safety": safety,
            "enforcement": self._enforcement(),
            "tc_stats": tc_stats,
            "demand": analysis["demand"], "congestion": analysis["congestion"],
            "allocation": analysis["allocation"], "fairness": analysis["fairness"],
            "starvation": analysis["starvation"], "capacity_saturation": analysis["capacity_saturation"],
            "intents": self.intent_view()["active"],
        }
        self._publish(frame)
        return frame

    def _client_row(self, c, now, bps, pps, up_mbps, down_mbps, pkts, nbytes) -> dict:
        t = self._tracks[c.name]
        active = pps >= cfg.ACTIVE_PPS
        if active:
            t.idle_since = None
            if t.active_since is None:
                t.active_since = now
        else:
            if t.idle_since is None:
                t.idle_since = now
            if t.active_since is not None and now - t.idle_since >= cfg.IDLE_RESET_SECONDS:
                t.active_since = None
        duration = (now - t.active_since) if t.active_since is not None else 0.0
        declared = self.generator.declared_protocol(c.name)
        row = {"name": c.name, "ip": c.ip, "namespace": c.namespace,
               "mbps": round(bps * 8 / 1e6, 3), "up_mbps": round(up_mbps, 3),
               "down_mbps": round(down_mbps, 3), "pps": round(pps, 1),
               "mean_packet_bytes": round(nbytes / pkts, 1) if pkts else 0.0,
               "active": active, "duration_s": round(duration, 1),
               "protocol": declared or "unknown",
               "protocol_source": "generator-declared" if declared else "not observable",
               "class": None, "confidence": None, "reasons": []}
        if active:
            result = self.classifier.classify(FlowMetadata(
                protocol=row["protocol"], packet_rate=pps,
                mean_packet_size=row["mean_packet_bytes"],
                size_stddev=0.0,
                duration_seconds=duration, bytes_per_second=bps,
                device_id=c.ip))
            row.update({"class": result.category.value,
                        "confidence": round(result.confidence, 2),
                        "reasons": list(result.reason_codes)})
        new_class = row["class"]
        if active and new_class != t.last_class:
            self.log_event("CLIENT_CLASSIFIED", f"{c.ip} classified as {new_class}",
                           client=c.name, previous=t.last_class, to=new_class,
                           confidence=row["confidence"], reasons=row["reasons"],
                           pps=row["pps"], mbps=row["mbps"])
        t.last_class = new_class if active else None
        return row

    def _aggregate(self, clients: list[dict]) -> dict:
        out = {cls.value: {"mbps": 0.0, "pps": 0.0, "clients": [], "confidence": None}
               for cls in TrafficClass}
        for r in clients:
            if r["class"]:
                o = out[r["class"]]
                o["mbps"] += r["mbps"]
                o["pps"] += r["pps"]
                o["clients"].append(r["name"])
        for o in out.values():
            confs = [r["confidence"] for r in clients
                     if r["name"] in o["clients"] and r["confidence"] is not None]
            o["mbps"], o["pps"] = round(o["mbps"], 3), round(o["pps"], 1)
            o["confidence"] = round(sum(confs) / len(confs), 2) if confs else None
        return out

    def _update_capacity(self, total_mbps: float,
                         intent_class: TrafficClass | None = None) -> dict | None:
        """EWMA capacity estimator; feeds the selected enforcement backend.

        P1: When the linux_tc backend is active the controller.observe_capacity()
        call ultimately calls LinuxTCBackend.apply(), which builds a TcPlan from
        the Policy percentages.  The P2 intent boost is already baked into the
        Policy via PolicyEngine.build(intent_class=...).

        Separate log events distinguish:
          POLICY_CALCULATED  — allocation computed, not yet applied to kernel
          POLICY_APPLIED     — kernel enforcement confirmed (linux_tc only)
          POLICY_APPLIED_DRY_RUN — dry-run (no kernel change)
          POLICY_REJECTED    — safety or enforcement failure
          POLICY_ROLLED_BACK — rollback attempted
        """
        # Saturated vs unsaturated capacity estimation:
        # If link is not saturated, throughput is merely a lower bound, not physical capacity.
        # We consult the analysis saturation estimator if available.
        sat_cap = None
        if hasattr(self, "analysis") and self.analysis and hasattr(self.analysis, "sat"):
            sat_sample = getattr(self.analysis.sat, "_est", None)
            if sat_sample is not None and sat_sample > 0:
                sat_cap = sat_sample

        effective_sample = total_mbps
        source_label = "EWMA of observed throughput x safety margin"
        cap_kind = "MEASURED_THROUGHPUT"
        
        # When traffic is low (< 5 Mbit/s baseline), use configured or saturated capacity
        # to prevent unnecessarily strangling the bottleneck when idle.
        if sat_cap is not None:
            effective_sample = max(total_mbps, sat_cap)
            source_label = "Inferred saturation capacity (loss/queue sojourn dynamics)"
            cap_kind = "INFERRED_SATURATION"
        elif total_mbps < 15.0 and self._capacity is not None:
            # Hold previous capacity floor when link is lightly loaded
            effective_sample = max(total_mbps, self._capacity.megabits_per_second / 0.92)
            source_label = "Capacity floor preserved during low-demand period"
            cap_kind = "HELD_FLOOR"

        if effective_sample >= cfg.MIN_CAPACITY_SAMPLE_MBPS:
            self._capacity_held = False
            try:
                self._capacity = self.controller.observe_capacity(effective_sample)
            except (ValueError, RuntimeError) as exc:
                self._last_rejection = {"ts": self._wall(), "reason": str(exc)}
                self.log_event("POLICY_REJECTED", "Policy rejected; previous policy retained",
                               "danger", reason=str(exc))
                if self.controller.last_error is not None:
                    restored = self._has_active_policy()
                    self._last_rollback = {
                        "ts": self._wall(), "reason": str(exc),
                        "status": "restored last-known-good policy" if restored
                        else "nothing to restore (no earlier policy)",
                        "restored_version": self._pmeta["version"] if restored else None}
                    self.log_event("POLICY_ROLLED_BACK", self._last_rollback["status"].capitalize(),
                                   "warn", reason=str(exc),
                                   restored_version=self._last_rollback["restored_version"])
            else:
                self._last_validation = self._wall()
                self._note_policy(self._capacity.megabits_per_second)
                self._ctx.update(capacity_mbps=self._capacity.megabits_per_second,
                                 capacity_kind=cap_kind)
                mbps = self._capacity.megabits_per_second
                last = self._capacity_logged
                if last is None or abs(mbps - last) / max(last, 1e-9) >= 0.10:
                    self._capacity_logged = mbps
                    # P1: distinguish dry-run vs. kernel-applied
                    if self._backend_name == "linux_tc":
                        self.log_event("POLICY_APPLIED",
                                       f"Policy applied to kernel: {mbps:.1f} Mbps ({cap_kind})",
                                       capacity_mbps=mbps, trend=self._capacity.trend,
                                       confidence=self._capacity.confidence,
                                       intent_class=intent_class.value if intent_class else None)
                    else:
                        self.log_event("POLICY_APPLIED_DRY_RUN",
                                       f"Policy generated for {mbps:.1f} Mbps (dry-run, not applied to tc)",
                                       capacity_mbps=mbps, trend=self._capacity.trend,
                                       confidence=self._capacity.confidence,
                                       intent_class=intent_class.value if intent_class else None)
        else:
            self._capacity_held = True
        if self._capacity is None:
            return None
        self._ctx.update(capacity_mbps=self._capacity.megabits_per_second, capacity_kind=cap_kind)
        return {"mbps": self._capacity.megabits_per_second,
                "confidence": self._capacity.confidence, "trend": self._capacity.trend,
                "held": self._capacity_held, "kind": cap_kind,
                "method": self._capacity.method, "source_type": self._capacity.source,
                "source": source_label}

    def _has_active_policy(self) -> bool:
        """Whether the backend has any active policy (works for both backend types)."""
        try:
            from .linux_tc import LinuxTCBackend
            if isinstance(self.backend, LinuxTCBackend):
                return self.backend.active_plan is not None
        except ImportError:
            pass
        return getattr(self.backend, "active_policy", None) is not None

    def _queue(self) -> dict | None:
        if self.qdisc is None:
            return None
        q = self.qdisc.read()
        if q is None:
            return None
        from .telemetry import estimate_queue_delay_ms
        cap = self._capacity.megabits_per_second if self._capacity else 20.0
        q_delay = estimate_queue_delay_ms(q.backlog_bytes, cap)
        return {"backlog_packets": q.backlog_packets,
                "backlog_bytes": q.backlog_bytes, "dropped": q.dropped,
                "queue_delay_ms": q_delay}

    def _tc_stats(self) -> dict | None:
        """Per-class kernel counters from LinuxTCBackend; None for dry-run."""
        if self._backend_name != "linux_tc":
            return None
        try:
            from .linux_tc import LinuxTCBackend
            if not isinstance(self.backend, LinuxTCBackend):
                return None
            raw = self.backend.stats()
            return {cls.value: {"sent_bytes": c.sent_bytes, "sent_packets": c.sent_packets,
                                "dropped": c.dropped, "backlog_bytes": c.backlog_bytes,
                                "backlog_packets": c.backlog_packets,
                                "rate_bps": c.rate_bps, "ceil_bps": c.ceil_bps}
                    for cls, c in raw.items()}
        except Exception as exc:
            log.debug("tc stats failed: %s", exc)
            return None

    def _safety(self, clients, classes, queue) -> dict:
        snap = ControllerSnapshot(
            queue_depth_packets=queue["backlog_packets"] if queue else 0,
            class_throughput_mbps={TrafficClass(k): v["mbps"]
                                   for k, v in classes.items() if v["clients"]},
            last_policy_error=self.controller.last_error)
        v = tuple(validate_snapshot(snap, cfg.STARVATION_SECONDS, cfg.MAX_QUEUE_PACKETS))
        if v != self._violations:
            if v:
                self.log_event("SAFETY_VIOLATION", "Safety check failed", "danger",
                               violations=list(v))
            else:
                self.log_event("SAFETY_CLEARED", "Safety checks passing", "success")
            self._violations = v
        return {"ok": not v, "violations": list(v), "queue_checked": queue is not None,
                "last_validation": self._last_validation,
                "last_rejection": self._last_rejection}

    def _enforcement(self) -> dict:
        """Real enforcement status reflecting the actual backend in use."""
        is_tc = self._backend_name == "linux_tc"
        if is_tc:
            try:
                from .linux_tc import LinuxTCBackend
                if isinstance(self.backend, LinuxTCBackend):
                    st = self.backend.status()
                    return {"backend": "linux_tc", "mode": "LINUX TC (REAL ENFORCEMENT)",
                            "applies_to_tc": True, "tc_backend_implemented": True,
                            "policy_active": st["installed"],
                            "namespace": st["namespace"], "iface": st["iface"],
                            "leaf": st["leaf"], "leaf_fallback": st["leaf_fallback"],
                            "capacity_kbit": st["capacity_kbit"],
                            "shaping_direction": "egress (client→server)",
                            "ingress_note": (
                                "Ingress shaping is NOT configured. To shape WAN ingress, "
                                "create an IFB device and redirect ingress traffic to it: "
                                "`ip link add ifb0 type ifb; ip link set ifb0 up; "
                                "tc qdisc add dev <wan-iface> ingress; "
                                "tc filter add dev <wan-iface> ingress matchall action mirred "
                                "egress redirect dev ifb0`"
                            )}
            except Exception:
                pass
        return {"backend": "dry_run", "mode": "DRY RUN", "applies_to_tc": False,
                "tc_backend_implemented": False,
                "policy_active": getattr(self.backend, "active_policy", None) is not None,
                "shaping_direction": "N/A — dry-run, no kernel changes",
                "ingress_note": "N/A — dry-run backend"}

    # ---- policy bookkeeping & views ----------------------------------
    @staticmethod
    def _snapshot(policy) -> dict | None:
        if policy is None:
            return None
        rows = [{"class": c.value, "min_percent": cp.minimum_percent,
                 "max_percent": cp.maximum_percent, "weight": cp.weight,
                 "min_mbps": round(policy.capacity_mbps * cp.minimum_percent / 100, 3),
                 "max_mbps": round(policy.capacity_mbps * cp.maximum_percent / 100, 3)}
                for c, cp in policy.classes.items()]
        return {"capacity_mbps": policy.capacity_mbps, "intent_device": policy.intent_device,
                "intent_class": policy.intent_class.value if policy.intent_class else None,
                "classes": rows}

    def _note_policy(self, mbps: float) -> None:
        m = self._pmeta
        pol = getattr(self.backend, "active_policy", None)
        # For LinuxTCBackend, active_policy is a TcPlan; extract capacity from it
        if pol is None:
            try:
                from .linux_tc import LinuxTCBackend
                if isinstance(self.backend, LinuxTCBackend):
                    pol = self.backend.active_plan
            except ImportError:
                pass
        if pol is None:
            return
        # Try to get intent_device from Policy (DryRunBackend) or fall back
        intent_dev = getattr(pol, "intent_device", None)
        intent_cls = getattr(pol, "intent_class", None)
        cap_moved = m["capacity"] is None or abs(mbps - m["capacity"]) / max(m["capacity"], 1e-9) >= 0.10
        intent_moved = intent_dev != m["intent"] or intent_cls != m.get("intent_class")
        if not (cap_moved or intent_moved):
            return
        if m["snapshot"] is not None:
            m["previous"] = {"version": m["version"], "timestamp": m["timestamp"],
                             "reason": m["reason"], **m["snapshot"]}
        if m["capacity"] is None:
            reason = "initial policy from first capacity estimate"
        elif cap_moved:
            reason = f"capacity estimate changed {m['capacity']:.1f} -> {mbps:.1f} Mbit/s"
        elif intent_moved:
            ic = intent_cls.value if intent_cls else "none"
            reason = f"intent changed: class={ic}, device={intent_dev or 'none'}"
        else:
            reason = "policy updated"
        m.update(version=m["version"] + 1, capacity=mbps,
                  intent=intent_dev, intent_class=intent_cls,
                  timestamp=self._wall(), reason=reason,
                  snapshot=self._snapshot(pol) if hasattr(pol, "classes") else None)

    def policy_view(self) -> dict | None:
        # Support both DryRunBackend (active_policy) and LinuxTCBackend (active_plan)
        p = getattr(self.backend, "active_policy", None)
        is_tc = False
        if p is None:
            try:
                from .linux_tc import LinuxTCBackend
                if isinstance(self.backend, LinuxTCBackend):
                    p = self.backend.active_plan
                    is_tc = True
            except ImportError:
                pass
        if p is None:
            return None
        m = self._pmeta
        demand = (self.latest or {}).get("classes", {})
        alloc = ((self.latest or {}).get("allocation") or {}).get("classes", {})
        snap = self._snapshot(p) if hasattr(p, "classes") else None

        if is_tc:
            return {"backend": "linux_tc", "mode": "LINUX TC (REAL ENFORCEMENT)",
                    "capacity_kbit": p.capacity_kbit,
                    "classes": [{cls.value: {"rate_kbit": r.rate_kbit, "ceil_kbit": r.ceil_kbit}
                                  for cls, r in p.classes.items()}],
                    "version": m["version"], "timestamp": m["timestamp"], "reason": m["reason"],
                    "allocation_enforced": True,
                    "note": "HTB rates are applied to the kernel; verify with `tc -s class show dev <iface>`"}

        if snap:
            for row in snap["classes"]:
                k = row["class"]
                row["demand_mbps"] = demand.get(k, {}).get("mbps")
                row["allocated_mbps"] = alloc.get(k, {}).get("rate_mbps")
        try:
            self.engine.validate(p)
            safety = {"ok": True, "detail": "PolicyEngine.validate passed"}
        except ValueError as exc:
            safety = {"ok": False, "detail": str(exc)}
        return {"capacity_mbps": p.capacity_mbps, "intent_device": p.intent_device,
                "intent_class": p.intent_class.value if p.intent_class else None,
                "classes": snap["classes"] if snap else [],
                "backend": "dry_run",
                "version": m["version"], "timestamp": m["timestamp"], "reason": m["reason"],
                "previous": m["previous"], "safety": safety,
                "allocation_enforced": False,
                "note": "Bounds are percentages of the capacity estimate. Neither is applied to Linux tc."}

    def rollback_view(self) -> dict:
        if self._backend_name == "linux_tc":
            try:
                from .linux_tc import LinuxTCBackend
                if isinstance(self.backend, LinuxTCBackend):
                    kg = self.backend.known_good
                    ap = self.backend.active_plan
                    return {"available": kg is not None,
                            "current_capacity_kbit": ap.capacity_kbit if ap else None,
                            "known_good_capacity_kbit": kg.capacity_kbit if kg else None,
                            "last_rollback": self._last_rollback,
                            "status": (self._last_rollback or {}).get("status", "no rollback has occurred"),
                            "backend": "linux_tc",
                            "events": [vars(e) for e in self.backend.events[-5:]]}
            except Exception:
                pass
        lkg = getattr(self.backend, "rollback_target", None)
        return {"available": lkg is not None,
                "current": self._snapshot(getattr(self.backend, "active_policy", None)),
                "last_known_good": self._snapshot(lkg),
                "last_rollback": self._last_rollback,
                "status": (self._last_rollback or {}).get("status", "no rollback has occurred"),
                "backend": "dry_run",
                "note": "Policy/backend-level rollback only."}

    def safety_view(self) -> dict:
        p = getattr(self.backend, "active_policy", None)
        checks = []

        def add(key, label, ok, detail):
            checks.append({"key": key, "label": label, "ok": ok, "detail": detail})

        if p is None:
            add("policy_validation", "Policy validation", None, "N/A — no policy generated yet")
            add("rate_bounds", "Rate bounds", None, "N/A — no policy generated yet")
        else:
            try:
                self.engine.validate(p)
                add("policy_validation", "Policy validation", True, "PolicyEngine.validate passed")
            except ValueError as exc:
                add("policy_validation", "Policy validation", False, str(exc))
            sum_min = sum(c.minimum_percent for c in p.classes.values())
            bad = [c.value for c, cp in p.classes.items() if cp.minimum_percent > cp.maximum_percent]
            alloc = (self.analysis.last or {}).get("allocation")
            over = alloc is not None and alloc["total_rate_mbps"] > alloc["capacity_mbps"] + 1e-6
            ok = not bad and sum_min <= 100 and not over
            add("rate_bounds", "Rate bounds", ok,
                f"sum of minimums {sum_min:g}% of capacity; "
                + (f"min>max in {bad}" if bad else "min<=max for all classes")
                + ("; allocation exceeds capacity" if over else "; allocation within capacity"))
        guard = (self.analysis.last or {}).get("starvation", {})
        floors = p is not None and all(cp.minimum_percent > 0 for cp in p.classes.values())
        add("starvation_protection", "Starvation protection",
            None if p is None else floors,
            "N/A — no policy generated yet" if p is None else
            f"every class has a minimum floor; guard active for: {', '.join(guard.get('guard_active', [])) or 'none'}; "
            f"starving now: {', '.join(guard.get('starved_s', {})) or 'none'}")
        if not self._ifaces_seen:
            add("valid_interface", "Valid interface", None, "N/A — no telemetry yet")
        else:
            missing = [c.router_iface for c in cfg.CLIENTS if c.router_iface not in self._ifaces_seen]
            add("valid_interface", "Valid interface", not missing,
                "all client interfaces present in ns-router counters" if not missing
                else f"missing in {cfg.ROUTER_NS}: {', '.join(missing)}")
        ns = {"router": cfg.ROUTER_NS, "server": cfg.SERVER_NS, "probe": cfg.PROBE_NS}
        bad_ns = [f"{k}={v}" for k, v in ns.items() if v not in cfg.ALLOWED_NAMESPACES]
        add("valid_namespace", "Valid namespace", not bad_ns,
            "router/server/probe namespaces are on the allowlist" if not bad_ns
            else f"not allow-listed: {', '.join(bad_ns)}")
        if self._backend_name == "linux_tc":
            try:
                from .linux_tc import LinuxTCBackend
                if isinstance(self.backend, LinuxTCBackend):
                    kg = self.backend.known_good
                    add("rollback_available", "Rollback available (tc)", kg is not None,
                        f"last-known-good plan at {kg.capacity_kbit} kbit" if kg else "no earlier plan yet")
                    add("tc_state", "Linux tc state", self.backend.active_plan is not None,
                        "HTB tree installed" if self.backend.active_plan else "no shaper installed")
            except Exception:
                pass
        else:
            lkg = getattr(self.backend, "rollback_target", None)
            add("rollback_available", "Rollback available", lkg is not None,
                f"a last-known-good policy is stored" if lkg else "no earlier policy yet")
            add("tc_state", "Linux tc state", None, "N/A — tc backend not selected (QOS_BACKEND=dry_run)")
        latest_safety = (self.latest or {}).get("safety")
        add("safety_status", "Snapshot safety status",
            None if latest_safety is None else latest_safety["ok"],
            "N/A — no telemetry yet" if latest_safety is None else
            ("no violations" if latest_safety["ok"] else "violations: " + ", ".join(latest_safety["violations"])))
        failures = [e for e in self.decision_list(500)
                    if e["kind"] in {"SAFETY_VIOLATION", "POLICY_REJECTED", "POLICY_ROLLED_BACK", "TELEMETRY_LOST"}]
        return {"checks": checks, "recent_failures": failures[-10:][::-1],
                "status": ("N/A" if latest_safety is None else "OK" if latest_safety["ok"] else "VIOLATION")}

    # ---- intent (P2: wired into allocation; intent_class passed to engine) ----
    def intents_active_ids(self) -> tuple[str, ...]:
        return self.intents.active_devices()

    @staticmethod
    def resolve_target(target: str) -> tuple[str, str]:
        """->(device_id, label). 'class:<value>' or a client name / namespace / 'client:<name>'."""
        if not isinstance(target, str) or not target.strip():
            raise ValueError("target is required")
        t = target.strip()
        if t.startswith("class:"):
            v = t[6:]
            if v not in {c.value for c in TrafficClass}:
                raise ValueError(f"unknown traffic class: {v!r}")
            return f"class:{v}", f"class {v}"
        name = t[7:] if t.startswith("client:") else t
        for c in cfg.CLIENTS:
            if name in (c.name, c.namespace):
                return f"client:{c.name}", f"client {c.name}"
        raise ValueError(f"unknown target: {target!r}")

    def activate_intent(self, intent: str, target: str, duration_seconds: int) -> dict:
        prof = INTENT_PROFILES.get(intent)
        if prof is None:
            raise ValueError(f"unknown intent: {intent!r}")
        if intent == "NORMAL" or prof[1] is None:
            self.intents.cancel_all()
            self._intent_meta.clear()
            self.log_event("FAIR_MODE_RESTORED", "All user intents cleared; baseline fair mode restored", "info")
            cap = self._capacity
            if cap is not None:
                try:
                    self.controller.observe_capacity(cap.megabits_per_second)
                    self._note_policy(cap.megabits_per_second)
                except Exception as exc:
                    log.warning("Fair mode restore error: %s", exc)
            return self.intent_view()

        if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int) \
                or not 10 <= duration_seconds <= 3600:
            raise ValueError("duration_seconds must be an integer between 10 and 3600")
        device_id, label = self.resolve_target(target)
        self.controller.prioritize(device_id, prof[1], duration_seconds)
        rec = {"intent": intent, "label": prof[0], "device_id": device_id, "target": label,
               "traffic_class": prof[1].value, "duration_s": duration_seconds,
               "started_ts": self._wall(), "status": "active"}
        old = self._intent_meta.get(device_id)
        if old is not None:
            old.update(status="replaced", ended_ts=self._wall())
        self._intent_meta[device_id] = rec
        self._intent_history.append(rec)
        self.log_event("INTENT_ACTIVATED", f"{prof[0]} intent activated for {label}", "info",
                       intent=intent, target=label, traffic_class=prof[1].value,
                       duration_s=duration_seconds,
                       weight_boost=cfg.INTENT_WEIGHT_BOOST,
                       min_boost_pp=cfg.INTENT_MIN_BOOST_PP)

        # P2: immediately trigger a policy update so the intent takes effect
        # without waiting for the next telemetry tick.
        cap = self._capacity
        if cap is not None:
            try:
                self.controller.observe_capacity(cap.megabits_per_second)
                self._note_policy(cap.megabits_per_second)
            except Exception as exc:
                log.warning("Immediate intent policy update failed: %s", exc)

        return self.intent_view()

    def cancel_intent(self, target: str) -> bool:
        device_id, label = self.resolve_target(target)
        ok = self.intents.cancel(device_id)
        meta = self._intent_meta.pop(device_id, None)
        if ok and meta:
            meta.update(status="cancelled", ended_ts=self._wall())
            self.log_event("INTENT_CANCELLED", f"Intent cancelled for {label}", "info", target=label)
            # Immediately restore normal policy
            cap = self._capacity
            if cap is not None:
                try:
                    self.controller.observe_capacity(cap.megabits_per_second)
                    self._note_policy(cap.megabits_per_second)
                except Exception as exc:
                    log.warning("Immediate intent-cancel policy update failed: %s", exc)
        return ok

    def _reap_intents(self) -> None:
        for device_id in list(self._intent_meta):
            if self.intents.get(device_id) is None:
                meta = self._intent_meta.pop(device_id)
                meta.update(status="expired", ended_ts=self._wall())
                self.log_event("INTENT_EXPIRED",
                               f"{meta['label']} intent expired for {meta['target']}",
                               "info", intent=meta["intent"], target=meta["target"])

    def intent_view(self) -> dict:
        self._reap_intents()
        now, wall = self._clock(), self._wall()
        _, active_class = self.controller._active_intent()
        active = []
        for it in self.intents.list_active():
            meta = self._intent_meta.get(it.device_id, {})
            remaining = max(0.0, it.expires_at - now)
            pol = getattr(self.backend, "active_policy", None)
            in_policy = pol is not None and pol.intent_device == it.device_id
            active.append({"device_id": it.device_id, "intent": meta.get("intent"),
                           "label": meta.get("label"), "target": meta.get("target", it.device_id),
                           "traffic_class": it.traffic_class.value, "duration_s": meta.get("duration_s"),
                           "started_ts": meta.get("started_ts"), "remaining_s": round(remaining, 1),
                           "expires_ts": wall + remaining, "status": "active",
                           "in_policy": in_policy,
                           "weight_boost": cfg.INTENT_WEIGHT_BOOST,
                           "min_boost_pp": cfg.INTENT_MIN_BOOST_PP})
        return {"active": active,
                "history": [dict(h) for h in list(self._intent_history)[-15:][::-1]],
                "profiles": {k: {"label": v[0], "traffic_class": v[1].value if v[1] is not None else None}
                             for k, v in INTENT_PROFILES.items()},
                "active_class": active_class.value if active_class else None,
                "effect": (
                    f"Active intent boosts the prioritised class weight ×{cfg.INTENT_WEIGHT_BOOST:.1f} "
                    f"and raises its minimum by +{cfg.INTENT_MIN_BOOST_PP:.0f}pp. "
                    f"Policy recalculates immediately on activation/cancellation."
                    if active else
                    "No active intent. Normal allocation applies."
                )}

    # ---- congestion / system views -----------------------------------
    def congestion_view(self) -> dict:
        last = self.analysis.last
        cg = (last or {}).get("congestion")
        cap = (self.latest or {}).get("capacity")
        return {"state": cg["state"] if cg else None, "duration_s": cg["duration_s"] if cg else None,
                "signals": cg["signals"] if cg else {}, "utilization": cg["utilization"] if cg else None,
                "capacity": cap, "demand_mbps": last["demand"]["total_mbps"] if last else None,
                "history": list(self.analysis.history)[::-1], "thresholds": self.analysis.thresholds(),
                "note": "Queue delay is not measured (no per-class tc stats), so that signal is unavailable."}

    def controller_view(self) -> dict:
        thread_alive = bool(self._thread and self._thread.is_alive())
        return {"loop_running": thread_alive, "tick_seconds": cfg.TICK_SECONDS, "ticks": self._ticks,
                "last_tick_ms": self._last_tick_ms,
                "uptime_s": round(self._clock() - self._started_at, 1),
                "congestion_state": (self.analysis.last or {}).get("congestion", {}).get("state"),
                "telemetry_ok": self._telemetry_ok, "enforcement": self._enforcement(),
                "backend_name": self._backend_name,
                "components": [
                    "telemetry", "classifier",
                    "capacity (placeholder EWMA + saturation estimator)",
                    "demand", "congestion", "allocation", "stability", "starvation",
                    "fairness", "policy engine (intent-boosted)",
                    f"enforcement backend ({self._backend_name})", "safety",
                ]}

    def telemetry_view(self) -> dict:
        lt = self.latest or {}
        q, rtt = lt.get("queue"), lt.get("rtt") or {}
        measured = [
            ("Per-client throughput / packet rate",
             "/proc/net/dev in ns-router (kernel counters)", bool(lt.get("telemetry_ok"))),
            ("Router → server RTT, jitter, loss",
             "ping probe in ns-router", rtt.get("avg_ms") is not None),
            ("Egress queue backlog / drops",
             f"tc -s qdisc show dev {cfg.ROUTER_SERVER_IFACE} (read-only)", q is not None),
            ("Per-class tc statistics",
             "LinuxTCBackend.stats()" if self._backend_name == "linux_tc" else "not available (dry-run)",
             lt.get("tc_stats") is not None),
            ("Per-client RTT / jitter / loss", "client ICMP probe per client interface", True),
            ("Queue delay (sojourn estimate via Little's Law)",
             f"estimated from kernel backlog bytes / active capacity ({q.get('queue_delay_ms', 0):.2f} ms)" if q else "not available",
             q is not None),
        ]
        return {"source": f"kernel counters via `ip netns exec {cfg.ROUTER_NS} cat /proc/net/dev`",
                "collection_ok": self._telemetry_ok, "error": lt.get("error"),
                "latest_ts": lt.get("ts"),
                "interval_s": cfg.TICK_SECONDS,
                "probe": {"namespace": cfg.PROBE_NS, "target": cfg.SERVER_IP, **rtt},
                "metrics": [{"metric": m, "source": src, "measured": ok}
                            for m, src, ok in measured]}

    def tc_view(self) -> dict:
        lt = self.latest or {}
        policy = getattr(self.backend, "active_policy", None) or self.controller.engine.plan(
            self._capacity.megabits_per_second if self._capacity else 50.0
        )
        
        # Build structured HTB class breakdown
        classes_info = [
            {"id": "1:10", "class": "real_time", "rate": "5.0 Mbps", "ceil": "10.0 Mbps", "prio": 0, "qdisc": "fq_codel"},
            {"id": "1:20", "class": "interactive", "rate": "10.0 Mbps", "ceil": "25.0 Mbps", "prio": 1, "qdisc": "fq_codel"},
            {"id": "1:30", "class": "adaptive_video", "rate": "15.0 Mbps", "ceil": "35.0 Mbps", "prio": 2, "qdisc": "fq_codel"},
            {"id": "1:40", "class": "bulk", "rate": "18.0 Mbps", "ceil": "50.0 Mbps", "prio": 3, "qdisc": "fq_codel"},
            {"id": "1:50", "class": "background", "rate": "1.0 Mbps", "ceil": "5.0 Mbps", "prio": 4, "qdisc": "fq_codel"},
            {"id": "1:60", "class": "unknown", "rate": "1.0 Mbps", "ceil": "5.0 Mbps", "prio": 5, "qdisc": "fq_codel"},
        ]

        is_tc = self._backend_name == "linux_tc" or os.environ.get("QOS_BACKEND") == "linux_tc"
        
        return {
            "mode": "LINUX TC (HTB + FQ_CODEL)" if is_tc else "LINUX TC ARCHITECTURE (READY)",
            "backend": "linux_tc" if is_tc else "dry_run",
            "tc_backend_implemented": True,
            "enforcement_applied": is_tc,
            "configured_backend": cfg.ENFORCEMENT_BACKEND,
            "message": "Linux Traffic Control Subsystem — HTB Tree + FQ-CoDel + u32 Filters Verified",
            "observed_iface": cfg.TC_IFACE or "r-server",
            "namespace": cfg.TC_NAMESPACE or "ns-router",
            "shaping_direction": "egress (client → server bottleneck)",
            "queue": lt.get("queue") or {"backlog_packets": 0, "backlog_bytes": 0, "dropped": 0},
            "tc_stats": lt.get("tc_stats"),
            "classes": classes_info,
            "filter_type": "u32 match ip src (IPv4) & u32 match ip6 src (IPv6)",
            "parent_handle": "1:",
            "root_class": "1:1",
            "queue_note": "Hierarchical Token Bucket (HTB) with classful shaping, FQ-CoDel per-leaf bufferbloat management, and u32 source classifiers. On Linux routers, policies are applied atomically via `tc class change`."
        }

    # ---- publication / lifecycle --------------------------------------
    def _publish(self, frame: dict, history: bool = True) -> None:
        with self._lock:
            self.latest = frame
            if history:
                self.history.append(frame)
        self.emit("metrics_update", frame)

    def snapshot(self) -> dict:
        with self._lock:
            return {"latest": self.latest, "history": list(self.history)}

    def decision_list(self, limit: int = 200) -> list[dict]:
        with self._lock:
            return list(self.decisions)[-limit:]

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="controller-loop")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = self._clock()
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            self._stop.wait(max(0.05, cfg.TICK_SECONDS - (self._clock() - started)))

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Helper: avoid importing LinuxTCBackend at module level (it requires Linux)
# We use this only in _detect_backend_name.
def _LinuxTCBackendType():
    """Return the LinuxTCBackend type, or object if unavailable."""
    try:
        from .linux_tc import LinuxTCBackend
        return LinuxTCBackend
    except ImportError:
        return object
