"""Safety checks for bounded, observable controller behavior."""

from .models import ControllerSnapshot, TrafficClass


def validate_snapshot(
    snapshot: ControllerSnapshot,
    starvation_seconds: float,
    max_queue_depth_packets: int,
) -> list[str]:
    violations: list[str] = []
    if snapshot.queue_depth_packets > max_queue_depth_packets:
        violations.append("queue_depth_limit_exceeded")
    if starvation_seconds > 0:
        for category, throughput in snapshot.class_throughput_mbps.items():
            if category is not TrafficClass.UNKNOWN and throughput <= 0:
                violations.append(f"{category.value}_may_be_starved")
    if snapshot.last_policy_error:
        violations.append("policy_application_failed")
    return violations
