"""Data contracts shared by the QoS controller modules."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class TrafficClass(str, Enum):
    REAL_TIME = "real_time"
    INTERACTIVE = "interactive"
    ADAPTIVE_VIDEO = "adaptive_video"
    BULK = "bulk"
    BACKGROUND = "background"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FlowMetadata:
    """Payload-free observations about one flow."""

    protocol: str
    packet_rate: float
    mean_packet_size: float
    size_stddev: float
    duration_seconds: float
    bytes_per_second: float
    direction: str = "downlink"
    dscp: int | None = None
    device_id: str | None = None


@dataclass(frozen=True)
class Classification:
    category: TrafficClass
    confidence: float
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapacityEstimate:
    megabits_per_second: float
    confidence: float
    trend: str
    # Added in Stage A (all optional, so existing callers/tests are unaffected).
    method: str = "ewma_throughput_placeholder"
    source: str = "ESTIMATED_CAPACITY"  # or KNOWN_EMULATED_CAPACITY
    timestamp: float | None = None

    @property
    def capacity_mbps(self) -> float:
        return self.megabits_per_second


@dataclass(frozen=True)
class ClassPolicy:
    minimum_percent: float
    maximum_percent: float
    weight: int


@dataclass(frozen=True)
class Policy:
    capacity_mbps: float
    classes: Mapping[TrafficClass, ClassPolicy]
    intent_device: str | None = None
    # The traffic class that was boosted for the active intent (None = no intent).
    # Carried in the policy so dashboards and tests can inspect the boost without
    # re-deriving it.
    intent_class: "TrafficClass | None" = None


@dataclass(frozen=True)
class Intent:
    device_id: str
    traffic_class: TrafficClass
    expires_at: float


@dataclass
class ControllerSnapshot:
    """Small observable state used by safety checks and dashboards."""

    queue_depth_packets: int = 0
    class_throughput_mbps: dict[TrafficClass, float] = field(default_factory=dict)
    last_policy_error: str | None = None
