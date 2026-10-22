"""Tests for 1-day improvements: Little's Law queue delay, streaming intent, fair mode reset, and continuous capture."""

import pytest
from qos_controller.allocation import allocate, default_class_specs
from qos_controller.intent import INTENT_PROFILES, IntentManager
from qos_controller.models import TrafficClass
from qos_controller.runtime import Runtime
from qos_controller.telemetry import ClientLatencyProbe, IfaceCounters, estimate_queue_delay_ms
from tests.test_runtime import FakeCounters, FakeGen


class FakeProbe:
    def stats(self): return {"running": True, "samples": 0, "avg_ms": None}


def test_estimate_queue_delay_littles_law():
    # 25,000 bytes backlog on a 20 Mbps link
    # Delay = (25000 * 8) / 20,000,000 = 0.010 s = 10.0 ms
    delay = estimate_queue_delay_ms(25000, 20.0)
    assert delay == 10.0

    # Zero backlog -> 0.0 ms delay
    assert estimate_queue_delay_ms(0, 20.0) == 0.0
    assert estimate_queue_delay_ms(100, 0.0) == 0.0


def test_intent_profiles_contain_streaming_and_normal():
    assert "STREAMING" in INTENT_PROFILES
    assert "NORMAL" in INTENT_PROFILES
    assert INTENT_PROFILES["STREAMING"][1] == TrafficClass.ADAPTIVE_VIDEO
    assert INTENT_PROFILES["NORMAL"][1] is None


def test_streaming_intent_boosts_video_allocation():
    capacity = 50.0
    demands = {
        TrafficClass.REAL_TIME: 5.0,
        TrafficClass.INTERACTIVE: 5.0,
        TrafficClass.ADAPTIVE_VIDEO: 30.0,
        TrafficClass.BULK: 30.0,
        TrafficClass.BACKGROUND: 5.0,
        TrafficClass.UNKNOWN: 5.0,
    }
    norm = allocate(capacity, default_class_specs(capacity, demands))
    boosted = allocate(capacity, default_class_specs(capacity, demands, intent_class=TrafficClass.ADAPTIVE_VIDEO))

    # Adaptive video allocation must be strictly higher with STREAMING intent
    assert boosted.allocations[TrafficClass.ADAPTIVE_VIDEO].allocated_mbps > norm.allocations[TrafficClass.ADAPTIVE_VIDEO].allocated_mbps
    assert boosted.total_allocated_mbps <= capacity + 1e-6


def test_runtime_normal_intent_resets_all_leases():
    now = [100.0]
    rt = Runtime(FakeCounters(), None, FakeProbe(), FakeGen(), clock=lambda: now[0], wall=lambda: now[0])
    rt._capacity = rt.controller.observe_capacity(50.0)

    # Activate a work call intent
    rt.activate_intent("WORK_CALL", "client:call", 300)
    assert len(rt.intent_view()["active"]) == 1

    # Activate NORMAL (Fair mode reset)
    res = rt.activate_intent("NORMAL", "client:bulk", 60)
    assert len(res["active"]) == 0
    assert len(rt.intent_view()["active"]) == 0
    assert any(e["kind"] == "FAIR_MODE_RESTORED" for e in rt.decision_list())


def test_client_latency_probe_interface():
    probe = ClientLatencyProbe(clients=(), interval_seconds=1.0)
    stats = probe.all_stats()
    assert isinstance(stats, dict)
    single = probe.stats_for("gaming")
    assert single["rtt_ms"] is None
    assert single["samples"] == 0
