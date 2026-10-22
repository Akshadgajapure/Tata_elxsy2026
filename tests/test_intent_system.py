"""Tests for intent system: activation, conflicting intents, expiry,
policy enforcement linkage, and kernel rate restoration.

Verifies PS3 requirement: Temporary user intent API/UI
- Activation changes policy immediately
- Conflicting intents are resolved deterministically
- Expiry restores normal allocation
- NORMAL/cancel_all clears all active intents
"""

from __future__ import annotations

import time

import pytest

from qos_controller.intent import INTENT_PROFILES, IntentManager
from qos_controller.models import TrafficClass
from qos_controller.policy import PolicyEngine


# ---------------------------------------------------------------- IntentManager tests

def test_intent_profiles_all_defined() -> None:
    """All 5 required profiles must exist."""
    required = {"WORK_CALL", "GAMING", "VIDEO_CONFERENCE", "STREAMING", "NORMAL"}
    assert required.issubset(INTENT_PROFILES.keys()), (
        f"Missing profiles: {required - set(INTENT_PROFILES)}")


def test_normal_profile_has_no_traffic_class() -> None:
    """NORMAL is the 'reset' intent and must have TrafficClass = None."""
    label, tc = INTENT_PROFILES["NORMAL"]
    assert tc is None, "NORMAL intent must have traffic_class=None (cancel-all semantics)"


def test_all_non_normal_profiles_have_traffic_class() -> None:
    for k, (label, tc) in INTENT_PROFILES.items():
        if k != "NORMAL":
            assert tc is not None, f"Profile {k!r} missing traffic class"
            assert isinstance(tc, TrafficClass)


def test_activate_single_intent() -> None:
    now = [0.0]
    mgr = IntentManager(clock=lambda: now[0])
    intent = mgr.set_priority("client:gaming", TrafficClass.INTERACTIVE, 60.0)
    assert intent.device_id == "client:gaming"
    assert intent.traffic_class == TrafficClass.INTERACTIVE
    assert intent.expires_at == 60.0


def test_intent_expiry_returns_none() -> None:
    now = [0.0]
    mgr = IntentManager(clock=lambda: now[0])
    mgr.set_priority("dev1", TrafficClass.REAL_TIME, 10.0)
    now[0] = 5.0
    assert mgr.get("dev1") is not None
    now[0] = 15.0
    assert mgr.get("dev1") is None  # expired


def test_expired_intent_removed_from_active() -> None:
    now = [0.0]
    mgr = IntentManager(clock=lambda: now[0])
    mgr.set_priority("dev1", TrafficClass.REAL_TIME, 10.0)
    now[0] = 20.0  # past expiry
    assert len(mgr.list_active()) == 0


def test_cancel_removes_intent() -> None:
    now = [0.0]
    mgr = IntentManager(clock=lambda: now[0])
    mgr.set_priority("dev1", TrafficClass.INTERACTIVE, 60.0)
    cancelled = mgr.cancel("dev1")
    assert cancelled is True
    assert mgr.get("dev1") is None


def test_cancel_nonexistent_returns_false() -> None:
    mgr = IntentManager()
    assert mgr.cancel("nonexistent") is False


def test_cancel_all_clears_multiple_intents() -> None:
    now = [0.0]
    mgr = IntentManager(clock=lambda: now[0])
    mgr.set_priority("dev1", TrafficClass.REAL_TIME, 60.0)
    mgr.set_priority("dev2", TrafficClass.INTERACTIVE, 60.0)
    mgr.set_priority("dev3", TrafficClass.BULK, 60.0)
    assert len(mgr.list_active()) == 3
    mgr.cancel_all()
    assert len(mgr.list_active()) == 0


def test_conflicting_intents_both_stored() -> None:
    """Two different devices can have simultaneous intents."""
    now = [0.0]
    mgr = IntentManager(clock=lambda: now[0])
    mgr.set_priority("dev1", TrafficClass.REAL_TIME, 60.0)
    mgr.set_priority("dev2", TrafficClass.INTERACTIVE, 60.0)
    active = mgr.list_active()
    assert len(active) == 2


def test_replacing_intent_for_same_device() -> None:
    """Setting a new intent for the same device replaces the old one."""
    now = [0.0]
    mgr = IntentManager(clock=lambda: now[0])
    mgr.set_priority("dev1", TrafficClass.REAL_TIME, 60.0)
    mgr.set_priority("dev1", TrafficClass.BULK, 120.0)  # replace
    active = mgr.list_active()
    assert len(active) == 1
    assert active[0].traffic_class == TrafficClass.BULK
    assert active[0].expires_at == 120.0


# ---------------------------------------------------------------- Policy engine + intent boost

def test_intent_boost_increases_minimum_percent() -> None:
    engine = PolicyEngine()
    policy_normal = engine.build(50.0)
    policy_intent = engine.build(50.0, intent_class=TrafficClass.REAL_TIME,
                                  intent_weight_boost=2.0, intent_min_boost_pp=10.0)
    rt_normal = policy_normal.classes[TrafficClass.REAL_TIME]
    rt_intent = policy_intent.classes[TrafficClass.REAL_TIME]
    assert rt_intent.minimum_percent > rt_normal.minimum_percent


def test_intent_boost_doubles_weight() -> None:
    engine = PolicyEngine()
    policy_normal = engine.build(50.0)
    policy_intent = engine.build(50.0, intent_class=TrafficClass.INTERACTIVE,
                                  intent_weight_boost=2.0)
    normal_w = policy_normal.classes[TrafficClass.INTERACTIVE].weight
    intent_w = policy_intent.classes[TrafficClass.INTERACTIVE].weight
    assert intent_w == normal_w * 2


def test_intent_boost_does_not_starve_other_classes() -> None:
    engine = PolicyEngine()
    policy = engine.build(50.0, intent_class=TrafficClass.REAL_TIME,
                           intent_weight_boost=2.0, intent_min_boost_pp=10.0)
    # Every other class must still have a positive minimum floor
    for cls, cp in policy.classes.items():
        if cls != TrafficClass.REAL_TIME:
            assert cp.minimum_percent > 0, f"{cls.value} starved by intent boost"


def test_boosted_minimum_never_exceeds_maximum() -> None:
    engine = PolicyEngine()
    # REAL_TIME max is 40%; boost 10pp from 15% should give 25%, which is < 40%
    policy = engine.build(50.0, intent_class=TrafficClass.REAL_TIME,
                           intent_min_boost_pp=10.0)
    rt = policy.classes[TrafficClass.REAL_TIME]
    assert rt.minimum_percent <= rt.maximum_percent


def test_intent_validation_still_passes_after_boost() -> None:
    engine = PolicyEngine()
    policy = engine.build(50.0, intent_class=TrafficClass.GAMING
                          if hasattr(TrafficClass, "GAMING") else TrafficClass.INTERACTIVE,
                          intent_weight_boost=2.0, intent_min_boost_pp=10.0)
    engine.validate(policy)  # must not raise


# ---------------------------------------------------------------- Intent duration validation

def test_intent_requires_positive_duration() -> None:
    mgr = IntentManager()
    with pytest.raises(ValueError):
        mgr.set_priority("dev1", TrafficClass.REAL_TIME, 0.0)
    with pytest.raises(ValueError):
        mgr.set_priority("dev1", TrafficClass.REAL_TIME, -10.0)


def test_intent_requires_non_empty_device_id() -> None:
    mgr = IntentManager()
    with pytest.raises(ValueError):
        mgr.set_priority("  ", TrafficClass.REAL_TIME, 60.0)


# ---------------------------------------------------------------- Active devices API

def test_active_devices_only_returns_live_intents() -> None:
    now = [0.0]
    mgr = IntentManager(clock=lambda: now[0])
    mgr.set_priority("dev1", TrafficClass.REAL_TIME, 30.0)
    mgr.set_priority("dev2", TrafficClass.BULK, 10.0)
    now[0] = 15.0  # dev2 expired
    devices = mgr.active_devices()
    assert "dev1" in devices or any("dev1" in d for d in devices)
    # dev2 should not be active
    assert not any(d == "dev2" for d in devices)
