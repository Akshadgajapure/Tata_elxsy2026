"""Small orchestration layer for the platform-independent prototype."""

from dataclasses import dataclass

from . import config as cfg
from .capacity import CapacityEstimator
from .enforcement import EnforcementBackend
from .intent import IntentManager
from .models import CapacityEstimate, Policy, TrafficClass
from .policy import PolicyEngine


@dataclass
class Controller:
    estimator: CapacityEstimator
    policy_engine: PolicyEngine
    intent_manager: IntentManager
    enforcement: EnforcementBackend
    current_policy: Policy | None = None
    last_error: str | None = None

    def observe_capacity(self, measured_mbps: float) -> CapacityEstimate:
        estimate = self.estimator.update(measured_mbps)
        intent_device, intent_class = self._active_intent()
        policy = self.policy_engine.build(
            estimate.megabits_per_second,
            intent_device=intent_device,
            intent_class=intent_class,
            intent_weight_boost=cfg.INTENT_WEIGHT_BOOST,
            intent_min_boost_pp=cfg.INTENT_MIN_BOOST_PP,
        )
        try:
            self.enforcement.apply(policy)
        except (ValueError, RuntimeError) as error:
            self.last_error = str(error)
            self.enforcement.rollback()
            raise
        self.current_policy = policy
        self.last_error = None
        return estimate

    def prioritize(
        self, device_id: str, traffic_class: TrafficClass, duration_seconds: float
    ) -> None:
        self.intent_manager.set_priority(
            device_id, traffic_class, duration_seconds
        )

    def _active_intent(self) -> tuple[str | None, TrafficClass | None]:
        """Return (device_id, traffic_class) for the most recent active intent.

        Multiple simultaneous intents are resolved by taking the one that expires
        latest (the user's most recent selection).  The policy records only one
        intent device and class; if different devices request different classes the
        higher-priority class wins (lower TrafficClass ordinal).
        """
        active = self.intent_manager.list_active()
        if not active:
            return None, None
        # Pick the intent that expires latest as the "primary"
        primary = max(active, key=lambda i: i.expires_at)
        # If there are multiple, choose the highest-priority (lowest ordinal)
        classes = [i.traffic_class for i in active]
        priority_order = list(TrafficClass)
        best_class = min(classes, key=lambda c: priority_order.index(c))
        return primary.device_id, best_class
