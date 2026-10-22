"""Congestion state machine: NORMAL -> WARNING -> CONGESTED -> RECOVERING -> NORMAL.

Anti-oscillation: sustained-tick requirements, a minimum dwell time per state,
an extra hold for CONGESTED, and hysteresis (exit thresholds are a fraction of
entry thresholds).  Signals that are unavailable (None) are ignored, never
invented; if NO signal is available the state holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class CongestionState(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CONGESTED = "CONGESTED"
    RECOVERING = "RECOVERING"


@dataclass(frozen=True)
class CongestionSignals:
    demand_ratio: float | None = None       # total demand / capacity
    queue_delay_ms: float | None = None
    loss_pct: float | None = None
    rtt_inflation_ms: float | None = None   # RTT minus idle baseline

    def available(self) -> dict[str, float]:
        return {k: v for k, v in vars(self).items() if v is not None}


@dataclass(frozen=True)
class CongestionConfig:
    # (warn, congested) entry thresholds per signal
    thresholds: Mapping[str, tuple[float, float]] = field(default_factory=lambda: {
        "demand_ratio": (0.95, 1.10),
        "queue_delay_ms": (20.0, 60.0),
        "loss_pct": (1.0, 3.0),
        "rtt_inflation_ms": (15.0, 50.0),
    })
    exit_factor: float = 0.7          # hysteresis: must fall below 70% of the entry threshold
    warn_ticks: int = 3               # consecutive ticks above warn before NORMAL->WARNING
    congest_ticks: int = 3
    clear_ticks: int = 5              # WARNING->NORMAL, RECOVERING->NORMAL
    recover_ticks: int = 3            # CONGESTED->RECOVERING
    min_state_s: float = 5.0
    congested_hold_s: float = 10.0


@dataclass(frozen=True)
class StateChange:
    previous: CongestionState
    new: CongestionState
    reason: str
    ts: float
    signals: Mapping[str, float]


class CongestionDetector:
    def __init__(self, config: CongestionConfig | None = None, start: float = 0.0) -> None:
        self.cfg = config or CongestionConfig()
        self.state = CongestionState.NORMAL
        self.since = start
        self._n = {"warn": 0, "severe": 0, "improving": 0, "clear": 0}

    def _classify(self, sig: Mapping[str, float]) -> tuple[bool, bool, bool, bool, str]:
        t, f = self.cfg.thresholds, self.cfg.exit_factor
        warn = severe = False
        improving = clear = True
        dominant, best = "", -1.0
        for name, v in sig.items():
            if name not in t:
                continue
            w, c = t[name]
            warn |= v >= w
            severe |= v >= c
            improving &= v < f * c
            clear &= v < f * w
            if v / c > best:
                best, dominant = v / c, name
        return warn, severe, improving, clear, dominant

    def _bump(self, **flags: bool) -> None:
        for k, on in flags.items():
            self._n[k] = self._n[k] + 1 if on else 0

    def update(self, now: float, signals: CongestionSignals) -> StateChange | None:
        sig = signals.available()
        if not any(k in self.cfg.thresholds for k in sig):
            return None  # nothing measured: hold state, do not guess
        warn, severe, improving, clear, dom = self._classify(sig)
        self._bump(warn=warn, severe=severe, improving=improving, clear=clear)
        c, dwell, S = self.cfg, now - self.since, CongestionState
        new: CongestionState | None = None
        reason = ""
        if dwell >= c.min_state_s:
            if self.state is S.NORMAL and self._n["warn"] >= c.warn_ticks:
                new, reason = S.WARNING, f"{dom}_above_threshold"
            elif self.state is S.WARNING:
                if self._n["severe"] >= c.congest_ticks:
                    new, reason = S.CONGESTED, f"{dom}_above_threshold"
                elif self._n["clear"] >= c.clear_ticks:
                    new, reason = S.NORMAL, "signals_below_exit_threshold"
            elif self.state is S.CONGESTED:
                if dwell >= c.congested_hold_s and self._n["improving"] >= c.recover_ticks:
                    new, reason = S.RECOVERING, "signals_improving"
            elif self.state is S.RECOVERING:
                if self._n["severe"] >= c.congest_ticks:
                    new, reason = S.CONGESTED, f"relapse_{dom}_above_threshold"
                elif self._n["clear"] >= c.clear_ticks:
                    new, reason = S.NORMAL, "signals_stable"
        if new is None:
            return None
        change = StateChange(self.state, new, reason, now, dict(sig))
        self.state, self.since = new, now
        self._n = dict.fromkeys(self._n, 0)
        return change
