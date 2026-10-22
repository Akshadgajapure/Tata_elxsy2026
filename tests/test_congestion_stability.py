import pytest

from qos_controller.congestion import (CongestionConfig, CongestionDetector,
                                       CongestionSignals as Sig, CongestionState as S)
from qos_controller.models import TrafficClass as T
from qos_controller.stability import PolicyStabilizer, StabilityConfig

FAST = CongestionConfig(warn_ticks=2, congest_ticks=2, clear_ticks=2, recover_ticks=2,
                        min_state_s=1, congested_hold_s=3)


def run(det, seq, t0=0.0):
    changes, t = [], t0
    for sig in seq:
        t += 1.0
        ch = det.update(t, sig)
        if ch:
            changes.append(ch)
    return changes


def test_full_cycle_normal_to_congested_and_back() -> None:
    det = CongestionDetector(FAST)
    bad, worse, ok = Sig(queue_delay_ms=30), Sig(queue_delay_ms=90), Sig(queue_delay_ms=1)
    ch = run(det, [bad] * 3 + [worse] * 3 + [ok] * 12)
    assert [c.new for c in ch] == [S.WARNING, S.CONGESTED, S.RECOVERING, S.NORMAL]
    assert ch[0].reason == "queue_delay_ms_above_threshold"


def test_single_spike_does_not_change_state() -> None:
    det = CongestionDetector(FAST)
    assert run(det, [Sig(queue_delay_ms=500), Sig(queue_delay_ms=1)] * 5) == []


def test_hysteresis_value_between_exit_and_entry_holds_warning() -> None:
    det = CongestionDetector(FAST)
    run(det, [Sig(queue_delay_ms=30)] * 3)
    assert det.state is S.WARNING
    run(det, [Sig(queue_delay_ms=18)] * 10, t0=10)       # below warn(20) but above 0.7*20=14
    assert det.state is S.WARNING


def test_relapse_from_recovering_goes_back_to_congested() -> None:
    det = CongestionDetector(FAST)
    run(det, [Sig(queue_delay_ms=90)] * 6)
    run(det, [Sig(queue_delay_ms=1)] * 3, t0=6)
    assert det.state is S.RECOVERING
    run(det, [Sig(queue_delay_ms=90)] * 3, t0=9)
    assert det.state is S.CONGESTED


def test_no_signals_means_hold_not_guess() -> None:
    det = CongestionDetector(FAST)
    assert run(det, [Sig()] * 20) == [] and det.state is S.NORMAL


def test_min_dwell_blocks_fast_transition() -> None:
    det = CongestionDetector(CongestionConfig(warn_ticks=1, min_state_s=100))
    assert run(det, [Sig(loss_pct=50)] * 10) == []


# ---------------- stability ----------------
CUR = {T.BULK: 10.0, T.INTERACTIVE: 5.0}


def test_step_is_bounded_not_a_jump() -> None:
    s = PolicyStabilizer(StabilityConfig(smoothing=1.0, max_step_fraction=0.1))
    out = s.stabilize(100, 100, {T.BULK: 80.0, T.INTERACTIVE: 5.0}, CUR)
    assert out.rates_mbps[T.BULK] == pytest.approx(20.0)     # 10 + 10% of 100
    assert out.rates_mbps[T.INTERACTIVE] == 5.0


def test_deadband_ignores_small_changes() -> None:
    out = PolicyStabilizer().stabilize(0, 100, {T.BULK: 10.1}, {T.BULK: 10.0})
    assert out.rates_mbps[T.BULK] == 10.0 and out.changed == ()


def test_major_changes_are_rate_limited_in_time() -> None:
    s = PolicyStabilizer(StabilityConfig(smoothing=1.0, max_step_fraction=0.5,
                                         major_change_fraction=0.2, min_major_interval_s=10))
    a = s.stabilize(0, 100, {T.BULK: 50.0}, {T.BULK: 5.0})
    assert a.rates_mbps[T.BULK] == 50.0                       # first major allowed (45 > 20)
    b = s.stabilize(5, 100, {T.BULK: 5.0}, a.rates_mbps)
    assert b.rates_mbps[T.BULK] == pytest.approx(30.0)        # deferred: limited to 20
    assert any("deferred" in n for n in b.notes)


def test_oscillating_target_produces_damped_output() -> None:
    s = PolicyStabilizer()
    cur, seen = {T.BULK: 10.0}, []
    for i in range(20):
        out = s.stabilize(i, 100, {T.BULK: 40.0 if i % 2 else 5.0}, cur)
        cur = dict(out.rates_mbps); seen.append(cur[T.BULK])
    assert max(abs(a - b) for a, b in zip(seen, seen[1:])) < 20   # never the raw 35 Mbps swing


def test_capacity_drop_is_clamped_immediately() -> None:
    out = PolicyStabilizer().stabilize(0, 20, {T.BULK: 60.0}, {T.BULK: 80.0})
    assert sum(out.rates_mbps.values()) <= 20 + 1e-9
    assert any("capacity_clamp" in n for n in out.notes)


def test_urgent_step_for_protected_class_when_congested() -> None:
    cfg = StabilityConfig(smoothing=1.0, max_step_fraction=0.1, urgent_multiplier=2.0,
                          major_change_fraction=1.0)
    normal = PolicyStabilizer(cfg).stabilize(0, 100, {T.INTERACTIVE: 50.0}, {T.INTERACTIVE: 5.0})
    urgent = PolicyStabilizer(cfg).stabilize(0, 100, {T.INTERACTIVE: 50.0}, {T.INTERACTIVE: 5.0}, congested=True)
    assert urgent.rates_mbps[T.INTERACTIVE] > normal.rates_mbps[T.INTERACTIVE]
