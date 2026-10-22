"""Tests for adaptive allocation and intent management (Priority 2)."""

import pytest
import time

from qos_controller import config as cfg
from qos_controller.allocation import allocate, default_class_specs
from qos_controller.enforcement import DryRunBackend
from qos_controller.intent import INTENT_PROFILES, IntentManager
from qos_controller.models import TrafficClass
from qos_controller.policy import PolicyEngine
from qos_controller.runtime import Runtime
from qos_controller.telemetry import IfaceCounters


class FakeCounters:
    def read(self):
        return {c.router_iface: IfaceCounters(1000, 10, 0, 1000, 10, 0) for c in cfg.CLIENTS}


class FakeGen:
    def declared_protocol(self, client):
        return "udp"


class FakeProbe:
    def stats(self):
        return {"running": True, "samples": 1, "avg_ms": 5.0}


def test_intent_boosts_policy_weights_and_minimums():
    engine = PolicyEngine()
    normal_policy = engine.build(100.0)
    rt_normal = normal_policy.classes[TrafficClass.REAL_TIME]

    boosted_policy = engine.build(
        100.0,
        intent_device="client:call",
        intent_class=TrafficClass.REAL_TIME,
        intent_weight_boost=2.0,
        intent_min_boost_pp=10.0,
    )
    rt_boosted = boosted_policy.classes[TrafficClass.REAL_TIME]

    assert boosted_policy.intent_device == "client:call"
    assert boosted_policy.intent_class == TrafficClass.REAL_TIME
    assert rt_boosted.weight > rt_normal.weight
    assert rt_boosted.minimum_percent > rt_normal.minimum_percent
    assert rt_boosted.minimum_percent <= rt_boosted.maximum_percent

    # Verify other classes still have valid positive floors (no starvation)
    for cls, cp in boosted_policy.classes.items():
        assert cp.minimum_percent >= engine.minimum_floor_percent
        assert cp.maximum_percent >= cp.minimum_percent
        assert cp.weight >= 1


def test_intent_boosts_allocation_under_competition():
    capacity = 50.0  # Mbps
    demands = {
        TrafficClass.REAL_TIME: 25.0,
        TrafficClass.INTERACTIVE: 20.0,
        TrafficClass.ADAPTIVE_VIDEO: 25.0,
        TrafficClass.BULK: 30.0,
        TrafficClass.BACKGROUND: 10.0,
        TrafficClass.UNKNOWN: 5.0,
    }

    # Normal allocation without intent
    normal_specs = default_class_specs(capacity, demands)
    res_normal = allocate(capacity, normal_specs)
    norm_rt_guar = res_normal.allocations[TrafficClass.REAL_TIME].guaranteed_mbps

    # Boosted allocation with WORK_CALL (REAL_TIME)
    boosted_specs = default_class_specs(capacity, demands, intent_class=TrafficClass.REAL_TIME)
    res_boosted = allocate(capacity, boosted_specs)
    boost_rt_guar = res_boosted.allocations[TrafficClass.REAL_TIME].guaranteed_mbps

    # Boosted allocation raises assured/guaranteed rate for real_time (HTB floor)
    assert boost_rt_guar > norm_rt_guar
    assert res_boosted.total_allocated_mbps <= capacity + 1e-6
    for cls, alloc in res_boosted.allocations.items():
        assert alloc.allocated_mbps > 0.0

    # Also test elastic competing classes where weight boost increases allocated Mbps
    v_demands = {
        TrafficClass.REAL_TIME: 5.0,
        TrafficClass.INTERACTIVE: 5.0,
        TrafficClass.ADAPTIVE_VIDEO: 30.0,
        TrafficClass.BULK: 30.0,
        TrafficClass.BACKGROUND: 5.0,
        TrafficClass.UNKNOWN: 5.0,
    }
    v_norm = allocate(capacity, default_class_specs(capacity, v_demands))
    v_boost = allocate(capacity, default_class_specs(capacity, v_demands, intent_class=TrafficClass.ADAPTIVE_VIDEO))
    assert v_boost.allocations[TrafficClass.ADAPTIVE_VIDEO].allocated_mbps > v_norm.allocations[TrafficClass.ADAPTIVE_VIDEO].allocated_mbps


def test_runtime_intent_activation_and_cancellation():
    clock_val = [100.0]
    rt = Runtime(
        FakeCounters(), None, FakeProbe(), FakeGen(),
        clock=lambda: clock_val[0], wall=lambda: 1000.0 + clock_val[0]
    )
    # Set capacity
    rt._capacity = rt.controller.observe_capacity(50.0)

    # Activate WORK_CALL intent on client:call
    view = rt.activate_intent("WORK_CALL", "client:call", 60)
    assert len(view["active"]) == 1
    act = view["active"][0]
    assert act["intent"] == "WORK_CALL"
    assert act["traffic_class"] == "real_time"
    assert act["target"] == "client call"

    # Verify event logged
    events = rt.decision_list()
    assert any(e["kind"] == "INTENT_ACTIVATED" for e in events)

    # Cancel intent
    cancelled = rt.cancel_intent("client:call")
    assert cancelled is True
    view_after = rt.intent_view()
    assert len(view_after["active"]) == 0
    assert any(e["kind"] == "INTENT_CANCELLED" for e in rt.decision_list())


def test_runtime_intent_automatic_expiry():
    clock_val = [100.0]
    rt = Runtime(
        FakeCounters(), None, FakeProbe(), FakeGen(),
        clock=lambda: clock_val[0], wall=lambda: 1000.0 + clock_val[0]
    )
    rt.activate_intent("GAMING", "client:gaming", 30)
    assert len(rt.intent_view()["active"]) == 1

    # Advance clock past expiry
    clock_val[0] += 35.0
    view_expired = rt.intent_view()
    assert len(view_expired["active"]) == 0
    assert any(e["kind"] == "INTENT_EXPIRED" for e in rt.decision_list())


def test_deterministic_intent_conflict_resolution():
    im = IntentManager(clock=lambda: 100.0)
    # Device A sets WORK_CALL (REAL_TIME)
    im.set_priority("devA", TrafficClass.REAL_TIME, 60)
    # Device B sets GAMING (INTERACTIVE)
    im.set_priority("devB", TrafficClass.INTERACTIVE, 120)

    assert set(im.active_devices()) == {"devA", "devB"}

    from qos_controller.controller import Controller
    from qos_controller.capacity import CapacityEstimator
    ctrl = Controller(CapacityEstimator(), PolicyEngine(), im, DryRunBackend(PolicyEngine()))
    dev, active_class = ctrl._active_intent()
    # Real-time has higher priority than interactive
    assert active_class == TrafficClass.REAL_TIME
