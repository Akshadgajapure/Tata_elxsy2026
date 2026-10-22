import pytest

from qos_controller.capacity import (ESTIMATED, KNOWN_EMULATED, CapacitySample as Cs,
                                     KnownCapacity, SaturationCapacityEstimator)
from qos_controller.demand import DemandEstimator
from qos_controller.models import TrafficClass as T


def test_known_capacity_is_labelled_emulated_and_updatable() -> None:
    k = KnownCapacity(100)
    e = k.estimate(1.0)
    assert e.source == KNOWN_EMULATED and e.confidence == 1.0 and e.capacity_mbps == 100
    k.set(20)
    assert k.estimate(2.0).capacity_mbps == 20
    with pytest.raises(ValueError):
        KnownCapacity(0)


def test_unsaturated_throughput_is_only_a_lower_bound_with_low_confidence() -> None:
    est = SaturationCapacityEstimator()
    e = est.observe(Cs(0, 10.0, rtt_ms=2, queue_delay_ms=0))
    assert e.source == ESTIMATED and e.method == "lower_bound_from_throughput" and e.confidence <= 0.3
    e = est.observe(Cs(1, 4.0, rtt_ms=2, queue_delay_ms=0))     # lower throughput must NOT shrink it
    assert e.capacity_mbps == pytest.approx(10 * 0.95)


def test_saturated_samples_define_capacity_and_idle_holds() -> None:
    est = SaturationCapacityEstimator()
    for t in range(10):
        e = est.observe(Cs(t, 20.0, rtt_ms=40, queue_delay_ms=50))
    assert e.method == "saturation_delivery_rate" and e.capacity_mbps == pytest.approx(19.0, abs=0.1)
    assert est.observe(Cs(11, 0.0)).capacity_mbps == e.capacity_mbps   # idle: hold


def test_capacity_drop_100_to_20_is_tracked_quickly() -> None:
    est = SaturationCapacityEstimator()
    for t in range(10):
        est.observe(Cs(t, 100.0, queue_delay_ms=50))
    e = None
    for t in range(10, 16):
        e = est.observe(Cs(t, 20.0, queue_delay_ms=80, drops_delta=5))
    assert e.capacity_mbps < 30 and e.trend in {"decreasing", "stable"}


def test_probe_is_periodic_and_only_after_an_estimate_exists() -> None:
    est = SaturationCapacityEstimator()
    assert est.propose_probe(0) is None
    est.observe(Cs(0, 10, queue_delay_ms=50))
    assert est.propose_probe(1) == 1.10 and est.propose_probe(2) is None
    assert est.propose_probe(100) == 1.10


def test_old_estimator_unchanged_and_new_fields_default() -> None:
    from qos_controller.capacity import CapacityEstimator
    e = CapacityEstimator().update(50)
    assert e.source == "ESTIMATED_CAPACITY" and "placeholder" in e.method


def test_demand_is_not_throughput_when_saturated() -> None:
    d = DemandEstimator()
    last = None
    for t in range(6):                         # class pinned at its 5 Mbps shaper, backlog present
        last = d.update(t, T.BULK, 5.0, saturated=True, cap_mbps=100)
    assert last.current_mbps == 5.0 and last.demand_mbps > 15
    last = d.update(7, T.BULK, 5.0, saturated=False, cap_mbps=100)
    assert last.demand_mbps >= last.rolling_mbps


def test_demand_tracks_peak_then_decays_after_window() -> None:
    d = DemandEstimator()
    for t, r in enumerate([1, 1, 20, 1, 1]):
        e = d.update(t, T.INTERACTIVE, r)
    assert e.recent_peak_mbps == 20 and e.demand_mbps >= 14       # 0.7 * peak
    e = d.update(100, T.INTERACTIVE, 1)                             # peak aged out
    assert e.recent_peak_mbps == 1 and e.demand_mbps < 3


def test_demand_capped_and_validated() -> None:
    d = DemandEstimator()
    for t in range(20):
        e = d.update(t, T.BULK, 5, saturated=True, cap_mbps=20)
    assert e.demand_mbps == 20
    with pytest.raises(ValueError):
        d.update(0, T.BULK, -1)
