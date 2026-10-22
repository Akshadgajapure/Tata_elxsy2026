"""Experiment scenario definitions and descriptions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..models import TrafficClass


@dataclass(frozen=True)
class ScenarioDefinition:
    id: str
    name: str
    description: str
    duration_seconds: int
    primary_metric: str
    traffic_profile: Mapping[str, Any]
    requires_root: bool = False
    notes: str = ""


SCENARIOS: dict[str, ScenarioDefinition] = {
    "iso_video_call": ScenarioDefinition(
        id="iso_video_call",
        name="ISO Download + Interactive Call",
        description="A large ISO download starts while an interactive video call is active. "
                    "Evaluates whether QoS protects call latency and jitter from bufferbloat.",
        duration_seconds=30,
        primary_metric="Interactive RTT & Jitter (ms)",
        traffic_profile={
            "interactive_rate_mbps": 5.0,
            "bulk_rate_mbps": 80.0,
            "bottleneck_mbps": 50.0,
        },
        requires_root=False,
        notes="Evaluates bufferbloat mitigation when bulk queue saturates bottleneck.",
    ),
    "capacity_drop": ScenarioDefinition(
        id="capacity_drop",
        name="Capacity Drop (100 -> 20 Mbit/s)",
        description="Bottleneck capacity suddenly drops from 100 Mbps to 20 Mbps. "
                    "Measures adaptation latency and policy stabilization speed.",
        duration_seconds=30,
        primary_metric="Time to adapt (s) & Packet drops",
        traffic_profile={
            "initial_capacity_mbps": 100.0,
            "dropped_capacity_mbps": 20.0,
            "total_demand_mbps": 45.0,
            "drop_at_second": 10,
        },
        requires_root=False,
        notes="Evaluates hysteresis and rapid oscillation avoidance.",
    ),
    "streams_gaming": ScenarioDefinition(
        id="streams_gaming",
        name="Multiple Streams + Low-Latency Gaming",
        description="Three adaptive video streams compete with a latency-sensitive gaming flow. "
                    "Measures Jain's fairness index and gaming packet delay.",
        duration_seconds=30,
        primary_metric="Jain's Fairness Index & Gaming Latency",
        traffic_profile={
            "gaming_demand_mbps": 3.0,
            "streams_demand_mbps": [12.0, 10.0, 8.0],
            "bottleneck_mbps": 25.0,
        },
        requires_root=False,
        notes="Evaluates fair sharing across identical elastic flows while isolating interactive flows.",
    ),
    "work_call_intent": ScenarioDefinition(
        id="work_call_intent",
        name="Temporary Work-Call Intent",
        description="User activates a WORK_CALL intent, boosting real_time allocation. "
                    "Verifies allocation boost, bounded priorities, and clean restoration after expiry.",
        duration_seconds=25,
        primary_metric="Real-Time Allocation Boost (Mbps)",
        traffic_profile={
            "intent": "WORK_CALL",
            "target": "client:call",
            "activate_at_second": 5,
            "duration_s": 15,
            "bottleneck_mbps": 40.0,
        },
        requires_root=False,
        notes="Verifies intent-driven policy reallocation and starvation avoidance for background traffic.",
    ),
    "misclassification": ScenarioDefinition(
        id="misclassification",
        name="Misclassification & Manual Correction",
        description="Injects uncertain flow metadata, observes confidence score, "
                    "applies manual user override, and checks policy adaptation.",
        duration_seconds=20,
        primary_metric="Classification Confidence & Override Response",
        traffic_profile={
            "device_id": "192.168.137.45",
            "injected_protocol": "unknown_udp",
            "override_class": "real_time",
        },
        requires_root=False,
        notes="Tests explainable reason codes and user-in-the-loop correction.",
    ),
    "enforcement_failure": ScenarioDefinition(
        id="enforcement_failure",
        name="Enforcement Failure & Safe Rollback",
        description="Simulates a kernel enforcement failure, tests automatic restoration "
                    "of the last-known-good plan, and checks unshaped safe fallback.",
        duration_seconds=15,
        primary_metric="Rollback Success & Safe State Verification",
        traffic_profile={
            "fail_after_step": 3,
            "expected_rollback": True,
        },
        requires_root=False,
        notes="Verifies that no failed policy is left partially applied in the kernel.",
    ),
    "hotspot_observation": ScenarioDefinition(
        id="hotspot_observation",
        name="Hotspot Client Observation",
        description="Evaluates real captured or ingested hotspot traffic metadata, "
                    "measuring device discovery, per-flow classification, and upload/download split.",
        duration_seconds=20,
        primary_metric="Devices Discovered & Flow Counts",
        traffic_profile={
            "source_kind": "batch_capture",
        },
        requires_root=False,
        notes="Observation-only mode for Windows Mobile Hotspot client devices.",
    ),
}


def get_scenario(scenario_id: str) -> ScenarioDefinition:
    if scenario_id not in SCENARIOS:
        raise KeyError(f"Unknown scenario ID: {scenario_id}. Choose from: {list(SCENARIOS)}")
    return SCENARIOS[scenario_id]
