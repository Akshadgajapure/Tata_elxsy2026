import pytest

from qos_controller.allocation import default_class_specs
from qos_controller.fairness import jain_index, service_report, within_class_fairness
from qos_controller.models import TrafficClass as T
from qos_controller.starvation import StarvationConfig, StarvationGuard


def test_jain_known_values() -> None:
    assert jain_index([5, 5, 5, 5]) == pytest.approx(1.0)
    assert jain_index([10, 0, 0, 0]) == pytest.approx(0.25)
    assert jain_index([]) is None and jain_index([0, 0]) == 1.0
    with pytest.raises(ValueError):
        jain_index([-1])


def test_within_class_needs_two_comparable_flows() -> None:
    assert within_class_fairness({"a": 3}) is None
    assert within_class_fairness({"a": 3, "b": 3}) == pytest.approx(1.0)


def test_service_report_population_and_min_service() -> None:
    served = {T.REAL_TIME: 4, T.BULK: 2}
    demand = {T.REAL_TIME: 4, T.BULK: 10, T.BACKGROUND: 0.0}   # background: no demand -> excluded
    r = service_report(served, demand, {T.REAL_TIME: 4, T.BULK: 4})
    assert r.population == ("bulk", "real_time")
    assert r.min_service_class == "bulk" and r.min_service_ratio == pytest.approx(0.2)
    assert r.fairness_index == pytest.approx((1.2 ** 2) / (2 * (1 + 0.04)))
    assert r.allocation_utilization == pytest.approx(6 / 8)


def test_service_report_with_no_demand_is_none_not_fake() -> None:
    r = service_report({}, {})
    assert r.fairness_index is None and r.min_service_ratio is None


def test_guard_triggers_after_threshold_and_releases() -> None:
    g = StarvationGuard(StarvationConfig(threshold_s=15, release_s=5))
    starving = {T.BULK: (0.0, 2.0)}
    assert g.update(0, starving).triggered == ()
    assert g.update(14, starving).triggered == ()
    rep = g.update(15, starving)
    assert rep.triggered == (T.BULK,) and rep.starved[T.BULK] == 15
    assert g.update(16, starving).triggered == ()          # fires once, stays active
    healthy = {T.BULK: (2.0, 2.0)}
    assert g.update(17, healthy).active == (T.BULK,)
    assert g.update(23, healthy).released == (T.BULK,)


def test_guard_ignores_negligible_demand() -> None:
    g = StarvationGuard()
    assert g.update(0, {T.BULK: (0, 0.0)}).starved == {}


def test_guard_apply_is_bounded_and_keeps_interactive_protected() -> None:
    g = StarvationGuard(StarvationConfig(threshold_s=1, recovery_mbps=5, budget_fraction=0.1))
    g.update(0, {T.BULK: (0, 2)}); g.update(2, {T.BULK: (0, 2)})
    specs = default_class_specs(20, {T.BULK: 50})
    out = {s.cls: s for s in g.apply(specs, 20)}
    assert out[T.BULK].hard_floor_mbps == pytest.approx(2.0)   # min(5, 10% of 20)
    assert out[T.REAL_TIME] == next(s for s in specs if s.cls is T.REAL_TIME)
