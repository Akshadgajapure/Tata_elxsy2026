import pytest

from qos_controller.capacity import CapacityEstimator
from qos_controller.classifier import MetadataClassifier
from qos_controller.controller import Controller
from qos_controller.enforcement import DryRunBackend, EnforcementError
from qos_controller.intent import IntentManager
from qos_controller.models import ControllerSnapshot, FlowMetadata, TrafficClass
from qos_controller.policy import PolicyEngine
from qos_controller.safety import validate_snapshot


def test_classifier_returns_confidence_and_does_not_need_payload() -> None:
    result = MetadataClassifier().classify(
        FlowMetadata(
            protocol="udp",
            packet_rate=50,
            mean_packet_size=180,
            size_stddev=20,
            duration_seconds=30,
            bytes_per_second=9_000,
        )
    )
    assert result.category is TrafficClass.REAL_TIME
    assert 0 < result.confidence <= 1
    assert result.reason_codes


def test_capacity_estimator_adapts_downward_with_safety_margin() -> None:
    estimator = CapacityEstimator(alpha=0.5, safety_margin=0.9)
    estimator.update(100)
    estimate = estimator.update(20)
    assert estimate.trend == "decreasing"
    assert 20 < estimate.megabits_per_second < 100


def test_intent_expires_and_can_be_cancelled() -> None:
    now = [100.0]
    manager = IntentManager(clock=lambda: now[0])
    manager.set_priority("laptop", TrafficClass.REAL_TIME, 10)
    assert manager.get("laptop") is not None
    now[0] = 111.0
    assert manager.get("laptop") is None
    assert not manager.cancel("laptop")


def test_policy_has_a_floor_for_every_class() -> None:
    policy = PolicyEngine().build(50, intent_device="laptop")
    PolicyEngine().validate(policy)
    assert all(item.minimum_percent > 0 for item in policy.classes.values())
    assert policy.intent_device == "laptop"


def test_safety_detects_unbounded_queue_and_starvation() -> None:
    violations = validate_snapshot(
        ControllerSnapshot(
            queue_depth_packets=2_000,
            class_throughput_mbps={TrafficClass.BULK: 0},
        ),
        starvation_seconds=15,
        max_queue_depth_packets=1_000,
    )
    assert "queue_depth_limit_exceeded" in violations
    assert "bulk_may_be_starved" in violations


def test_invalid_measurement_is_rejected() -> None:
    with pytest.raises(ValueError):
        CapacityEstimator().update(0)


def test_controller_applies_policy_to_dry_run_backend() -> None:
    backend = DryRunBackend(PolicyEngine())
    controller = Controller(
        estimator=CapacityEstimator(),
        policy_engine=PolicyEngine(),
        intent_manager=IntentManager(),
        enforcement=backend,
    )
    controller.prioritize("work-laptop", TrafficClass.REAL_TIME, 60)
    controller.observe_capacity(100)
    assert backend.active_policy is not None
    assert backend.active_policy.intent_device == "work-laptop"


def test_controller_rolls_back_after_enforcement_failure() -> None:
    backend = DryRunBackend(PolicyEngine())
    controller = Controller(
        estimator=CapacityEstimator(),
        policy_engine=PolicyEngine(),
        intent_manager=IntentManager(),
        enforcement=backend,
    )
    controller.observe_capacity(100)
    previous = backend.active_policy
    backend.fail_next_apply = True
    with pytest.raises(EnforcementError):
        controller.observe_capacity(20)
    assert backend.active_policy is previous
