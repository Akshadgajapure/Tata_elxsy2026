import pytest

from qos_controller.allocation import ClassSpec, allocate, default_class_specs, explain
from qos_controller.models import TrafficClass as T


def spec(cls, prio, w, mn, mx, dem, floor=0.1):
    return ClassSpec(cls, prio, w, mn, mx, floor, dem)


def test_brief_example_20mbps_call_gaming_video_bulk() -> None:
    # CALL=4, GAMING=8, VIDEO=15, BULK=30 demand on 20 Mbps (from the case brief).
    specs = [spec(T.REAL_TIME, 0, 5, 2, 8, 4), spec(T.INTERACTIVE, 1, 4, 4, 12, 8),
             spec(T.ADAPTIVE_VIDEO, 2, 3, 4, 16, 15), spec(T.BULK, 3, 2, 2, 20, 30)]
    r = allocate(20, specs, headroom=False)
    a = {c: x.allocated_mbps for c, x in r.allocations.items()}
    assert a[T.REAL_TIME] == pytest.approx(4)            # demand fully met, protected
    assert a[T.INTERACTIVE] == pytest.approx(8)
    assert sum(a.values()) <= 20 + 1e-9
    assert a[T.BULK] >= 2 - 1e-6 and a[T.ADAPTIVE_VIDEO] >= 4 - 1e-6   # nobody starved
    assert r.total_allocated_mbps == pytest.approx(20, abs=1e-3)       # contested -> fully used
    assert not r.infeasible_minimums


def test_never_exceeds_capacity_or_max_and_is_deterministic() -> None:
    specs = default_class_specs(50, {T.BULK: 500, T.REAL_TIME: 1})
    r1, r2 = allocate(50, specs), allocate(50, list(reversed(specs)))
    assert r1.total_allocated_mbps <= 50
    assert r1.allocations == r2.allocations                # input order irrelevant
    for s in specs:
        assert r1.allocations[s.cls].allocated_mbps <= s.max_mbps + 1e-9


def test_uncontested_demand_is_fully_served() -> None:
    r = allocate(100, default_class_specs(100, {T.INTERACTIVE: 5, T.BULK: 10}), headroom=False)
    assert r.allocations[T.INTERACTIVE].allocated_mbps >= 5 - 1e-6
    assert r.allocations[T.BULK].allocated_mbps >= 10 - 1e-6
    assert r.unallocated_mbps > 0                           # no demand -> capacity left over


def test_bulk_is_not_starved_even_when_everything_else_wants_it_all() -> None:
    d = {T.REAL_TIME: 100, T.INTERACTIVE: 100, T.ADAPTIVE_VIDEO: 100, T.BULK: 100}
    r = allocate(20, default_class_specs(20, d))
    assert r.allocations[T.BULK].allocated_mbps > 0
    assert r.allocations[T.BACKGROUND].allocated_mbps > 0   # hard floor even with zero demand


def test_impossible_minimums_degrade_lowest_priority_first() -> None:
    specs = [spec(T.REAL_TIME, 0, 1, 8, 10, 10), spec(T.BULK, 3, 1, 8, 10, 10)]
    r = allocate(10, specs, headroom=False)
    assert r.infeasible_minimums and r.notes
    assert r.allocations[T.REAL_TIME].allocated_mbps > r.allocations[T.BULK].allocated_mbps
    assert r.allocations[T.BULK].allocated_mbps > 0         # still not zero


def test_floors_exceeding_capacity_are_scaled_not_rejected() -> None:
    specs = [spec(T.REAL_TIME, 0, 1, 0, 5, 5, floor=3), spec(T.BULK, 1, 1, 0, 5, 5, floor=3)]
    r = allocate(2, specs)
    assert r.infeasible_minimums
    assert r.total_allocated_mbps <= 2
    assert all(a.allocated_mbps > 0 for a in r.allocations.values())


def test_weights_split_contested_capacity() -> None:
    specs = [spec(T.BULK, 3, 3, 0, 100, 100, floor=0), spec(T.BACKGROUND, 4, 1, 0, 100, 100, floor=0)]
    r = allocate(40, specs)
    assert r.allocations[T.BULK].allocated_mbps == pytest.approx(30, abs=1e-3)
    assert r.allocations[T.BACKGROUND].allocated_mbps == pytest.approx(10, abs=1e-3)


def test_invalid_inputs_rejected() -> None:
    with pytest.raises(ValueError):
        allocate(0, [])
    with pytest.raises(ValueError):
        allocate(10, [spec(T.BULK, 1, 0, 0, 5, 1)])         # zero weight
    with pytest.raises(ValueError):
        allocate(10, [spec(T.BULK, 1, 1, 6, 5, 1)])         # max < min
    with pytest.raises(ValueError):
        allocate(10, [spec(T.BULK, 1, 1, 0, 5, 1), spec(T.BULK, 2, 1, 0, 5, 1)])


def test_explain_mentions_every_class() -> None:
    text = "\n".join(explain(allocate(30, default_class_specs(30, {T.BULK: 50}))))
    for c in T:
        assert c.value in text
