"""Tests for capacity estimation correctness: idle-hold, saturation detection,
capacity drop response, recovery, and integration with the enforcement path.

These tests cover the correctness issues identified in the PS3 audit:
  - Idle-capacity regression (held floor must not collapse)
  - Saturation detection accuracy
  - Capacity drop response speed
  - Confidence decay when idle
"""

from __future__ import annotations

import time

import pytest

from qos_controller.capacity import (
    CapacityEstimator,
    CapacitySample,
    SaturationCapacityEstimator,
    SaturationConfig,
)


# ---------------------------------------------------------------- CapacityEstimator (EWMA)

def test_ewma_initial_sample_sets_estimate() -> None:
    est = CapacityEstimator(alpha=0.3, safety_margin=1.0)
    result = est.update(100.0)
    assert result.megabits_per_second == 100.0
    assert result.trend == "initial"


def test_ewma_converges_toward_new_value() -> None:
    est = CapacityEstimator(alpha=0.5, safety_margin=1.0)
    est.update(100.0)
    r = est.update(50.0)
    assert r.megabits_per_second == 75.0  # 0.5*50 + 0.5*100


def test_ewma_safety_margin_applied() -> None:
    est = CapacityEstimator(alpha=1.0, safety_margin=0.90)
    r = est.update(100.0)
    assert abs(r.megabits_per_second - 90.0) < 0.01


def test_ewma_trend_labels() -> None:
    est = CapacityEstimator(alpha=0.5, safety_margin=1.0)
    est.update(50.0)
    r_up = est.update(100.0)
    assert r_up.trend == "increasing"
    r_down = est.update(10.0)
    assert r_down.trend == "decreasing"


def test_ewma_rejects_non_positive() -> None:
    est = CapacityEstimator()
    with pytest.raises(ValueError):
        est.update(0.0)
    with pytest.raises(ValueError):
        est.update(-5.0)


# ---------------------------------------------------------------- SaturationCapacityEstimator

def _sample(ts: float, mbps: float, rtt_ms: float | None = None,
            loss_pct: float | None = None, drops: int = 0,
            queue_ms: float | None = None) -> CapacitySample:
    return CapacitySample(ts, mbps, rtt_ms=rtt_ms, loss_pct=loss_pct,
                          drops_delta=drops, queue_delay_ms=queue_ms)


def test_idle_link_holds_capacity_floor() -> None:
    """Critical regression: idle link must NOT collapse capacity estimate."""
    cfg = SaturationConfig(ewma_alpha=0.5, safety_margin=1.0, drop_ticks=3)
    est = SaturationCapacityEstimator(cfg)

    # Saturate link at 50 Mbps
    for i in range(5):
        est.observe(_sample(float(i), 48.0, rtt_ms=50.0, loss_pct=1.0))

    saturated_cap = est._est
    assert saturated_cap is not None and saturated_cap >= 40.0

    # Now link goes idle (0.1 Mbps)
    r_idle = est.observe(_sample(10.0, 0.1))
    # Estimate must be HELD, not lowered to 0.1
    assert est._est >= saturated_cap * 0.5, (
        f"Idle link collapsed capacity from {saturated_cap:.1f} to {est._est:.1f}")
    # Should be non-None (held estimate)
    assert r_idle is not None


def test_saturation_detected_by_loss() -> None:
    cfg = SaturationConfig(saturation_loss_pct=0.5, ewma_alpha=0.5, safety_margin=1.0)
    est = SaturationCapacityEstimator(cfg)
    s = _sample(0.0, 40.0, loss_pct=1.0)  # 1% > 0.5% threshold
    assert est._saturated(s) is True


def test_saturation_detected_by_queue_delay() -> None:
    cfg = SaturationConfig(saturation_queue_ms=20.0, safety_margin=1.0)
    est = SaturationCapacityEstimator(cfg)
    s = _sample(0.0, 40.0, queue_ms=25.0)
    assert est._saturated(s) is True


def test_saturation_detected_by_rtt_inflation() -> None:
    cfg = SaturationConfig(saturation_rtt_inflation_ms=15.0, safety_margin=1.0)
    est = SaturationCapacityEstimator(cfg)
    # Establish baseline RTT
    est.observe(_sample(0.0, 30.0, rtt_ms=10.0))  # baseline=10ms
    s = _sample(1.0, 45.0, rtt_ms=30.0)  # inflation=20ms > 15ms threshold
    assert est._saturated(s) is True


def test_capacity_drop_detected_within_3_samples() -> None:
    """Capacity drop: persistent low samples trigger fast downward revision."""
    cfg = SaturationConfig(ewma_alpha=0.5, safety_margin=1.0, drop_ticks=3, drop_ratio=0.8)
    est = SaturationCapacityEstimator(cfg)

    # Establish 50 Mbps capacity
    for i in range(5):
        est.observe(_sample(float(i), 48.0, loss_pct=2.0))

    old_cap = est._est
    assert old_cap >= 40.0

    # Now capacity drops to 20 Mbps (simulated by persistent saturated low samples)
    for i in range(3):
        est.observe(_sample(10.0 + i, 19.0, loss_pct=2.0))  # 19 < 0.8 * 50 = 40

    # After 3 consecutive low samples, estimate should drop
    assert est._est is not None
    assert est._est < old_cap * 0.7, (
        f"Capacity drop not detected: {old_cap:.1f} -> {est._est:.1f}")


def test_capacity_recovery_after_drop() -> None:
    """After capacity recovers, estimate should rise again."""
    cfg = SaturationConfig(ewma_alpha=0.5, safety_margin=1.0, drop_ticks=3, drop_ratio=0.8)
    est = SaturationCapacityEstimator(cfg)

    # Establish 50 Mbps
    for i in range(5):
        est.observe(_sample(float(i), 48.0, loss_pct=2.0))

    # Drop capacity
    for i in range(3):
        est.observe(_sample(10.0 + i, 19.0, loss_pct=2.0))

    dropped_cap = est._est

    # Recovery: push saturated high samples
    for i in range(5):
        est.observe(_sample(20.0 + i, 46.0, loss_pct=2.0))

    assert est._est > dropped_cap, (
        f"Capacity failed to recover: {dropped_cap:.1f} -> {est._est:.1f}")


def test_confidence_decays_when_not_saturated() -> None:
    """Confidence must decay when the estimator hasn't seen a saturated sample recently."""
    cfg = SaturationConfig(stale_s=5.0, safety_margin=1.0)
    est = SaturationCapacityEstimator(cfg)

    # First saturate
    est.observe(_sample(0.0, 40.0, loss_pct=2.0))
    r_fresh = est._current(0.0)
    assert r_fresh is not None

    # Now get confidence at t=100 (well past stale_s=5)
    r_stale = est._current(100.0)
    assert r_stale is not None
    assert r_stale.confidence < 0.7, f"Stale confidence too high: {r_stale.confidence}"


def test_safety_margin_applied_to_output() -> None:
    cfg = SaturationConfig(safety_margin=0.90, ewma_alpha=1.0)
    est = SaturationCapacityEstimator(cfg)
    est.observe(_sample(0.0, 50.0, loss_pct=2.0))
    r = est._current(0.0)
    assert r is not None
    # With safety_margin=0.90 and estimate=50, output should be 45
    assert abs(r.capacity_mbps - 45.0) < 0.5


def test_below_min_sample_returns_held_estimate() -> None:
    """Throughput below min_sample_mbps (idle) must return held estimate, not None."""
    cfg = SaturationConfig(min_sample_mbps=1.0, safety_margin=1.0)
    est = SaturationCapacityEstimator(cfg)
    # Prime with a saturated sample
    est.observe(_sample(0.0, 40.0, loss_pct=2.0))
    # Send idle sample
    r = est.observe(_sample(1.0, 0.05))  # 0.05 < min_sample_mbps=1.0
    assert r is not None, "Idle sample must return held estimate, not None"


def test_unsaturated_sample_only_raises_estimate() -> None:
    """Unsaturated throughput is a lower bound: it can raise but never lower estimate."""
    cfg = SaturationConfig(ewma_alpha=0.5, safety_margin=1.0)
    est = SaturationCapacityEstimator(cfg)

    # Establish at 50 Mbps via saturation
    est.observe(_sample(0.0, 50.0, loss_pct=2.0))
    high = est._est

    # Unsaturated sample at 20 Mbps (lower than current estimate)
    est.observe(_sample(1.0, 20.0))  # no loss, no queue delay = not saturated
    # Estimate must NOT decrease
    assert est._est >= high * 0.99, (
        f"Unsaturated sample lowered estimate: {high:.1f} -> {est._est:.1f}")
