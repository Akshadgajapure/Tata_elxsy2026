"""Policy generation with fairness and starvation guards.

Intent-aware weight boosting (P2):
  When a user intent is active, the prioritised traffic class receives:
    * minimum_percent raised by INTENT_MIN_BOOST_PP (bounded by maximum_percent)
    * weight multiplied by INTENT_WEIGHT_BOOST
  All other classes keep their starvation floors and are NOT starved below
  minimum_floor_percent.  Safety validation still runs on the boosted policy.
"""

from __future__ import annotations

from .models import ClassPolicy, Policy, TrafficClass


class PolicyEngine:
    def __init__(self, minimum_floor_percent: float = 2.0) -> None:
        if minimum_floor_percent <= 0:
            raise ValueError("minimum_floor_percent must be positive")
        self.minimum_floor_percent = minimum_floor_percent

    # Static base parameters (weight, min%, max%) — mirrors allocation.DEFAULT_CLASS_PARAMS
    _BASE: dict[TrafficClass, tuple[int, float, float]] = {
        TrafficClass.REAL_TIME:      (5, 15, 40),
        TrafficClass.INTERACTIVE:    (4, 15, 35),
        TrafficClass.ADAPTIVE_VIDEO: (3, 30, 70),
        TrafficClass.BULK:           (2, 10, 60),
        TrafficClass.BACKGROUND:     (1,  5, 30),
        TrafficClass.UNKNOWN:        (1,  5, 25),
    }

    def build(self, capacity_mbps: float, intent_device: str | None = None,
              intent_class: TrafficClass | None = None,
              intent_weight_boost: float = 2.0,
              intent_min_boost_pp: float = 10.0) -> Policy:
        """Build a policy, optionally boosting one class for an active intent.

        Parameters
        ----------
        capacity_mbps:      estimated link capacity
        intent_device:      opaque device ID recorded in the policy (may be None)
        intent_class:       the TrafficClass to boost when an intent is active
        intent_weight_boost: multiplier applied to the intent class weight
        intent_min_boost_pp: percentage points added to the intent class minimum
        """
        if capacity_mbps <= 0:
            raise ValueError("capacity_mbps must be positive")

        classes: dict[TrafficClass, ClassPolicy] = {}
        for tc, (weight, min_pct, max_pct) in self._BASE.items():
            # Apply anti-starvation floor first
            eff_min = max(min_pct, self.minimum_floor_percent)
            eff_weight = weight

            # Boost the intent class
            if intent_class is not None and tc is intent_class:
                boosted_min = eff_min + intent_min_boost_pp
                # Never let boosted minimum exceed maximum_percent
                eff_min = min(boosted_min, max_pct)
                eff_weight = max(1, round(weight * intent_weight_boost))

            classes[tc] = ClassPolicy(
                minimum_percent=eff_min,
                maximum_percent=max_pct,
                weight=eff_weight,
            )

        return Policy(capacity_mbps=capacity_mbps, classes=classes,
                      intent_device=intent_device, intent_class=intent_class)

    def validate(self, policy: Policy) -> None:
        if policy.capacity_mbps <= 0:
            raise ValueError("policy capacity must be positive")
        for category, class_policy in policy.classes.items():
            if class_policy.minimum_percent <= 0:
                raise ValueError(f"{category.value} has no starvation floor")
            if class_policy.maximum_percent < class_policy.minimum_percent:
                raise ValueError(f"{category.value} maximum is below its minimum")
            if class_policy.weight <= 0:
                raise ValueError(f"{category.value} has an invalid weight")
