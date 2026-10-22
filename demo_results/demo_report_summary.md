# TataElexy Adaptive QoS Engine — Demo Run Report

**Generated:** 2026-10-18 19:46:59 UTC  
**Mode:** Algorithmic Queueing Simulation (deterministic discrete-event model)  
**Platform:** Windows / Python 3.11.6  
**Scenarios:** 7 passed, 0 failed  

> **Emulation Notice:**
> This report was generated in **ALGORITHMIC_SIMULATION** mode.
> Results are computed by a deterministic discrete-event queueing simulation. Metrics are NOT measurements from real traffic. For measured results, run on Linux as root with iproute2 and iperf3.

## Evaluation Summary

| Scenario | Status | No-QoS RTT | Adaptive RTT | Improvement | Trials |
|---|---|---|---|---|---|
| iso_video_call | ✅ PASSED | 69.3 ms | 12.7 ms | 82% | 10 |
| capacity_drop | ✅ PASSED | N/A | N/A | N/A | 10 |
| streams_gaming | ✅ PASSED | N/A | N/A | N/A | 10 |
| work_call_intent | ✅ PASSED | N/A | N/A | N/A | 10 |
| misclassification | ✅ PASSED | N/A | N/A | N/A | 10 |
| enforcement_failure | ✅ PASSED | N/A | N/A | N/A | 10 |
| hotspot_observation | ✅ PASSED | N/A | N/A | N/A | 0 |

## Evaluation Dimensions (PS3)

### 1. Classification & Capacity Accuracy
- Classifier uses Layer-3/4 metadata only (zero payload inspection).
- Confidence scores (0.0–1.0) reported per flow; UNKNOWN class for ambiguous flows.
- Capacity estimated via EWMA + saturation-aware estimator.
- Idle link holds previous capacity floor (no collapse during low demand).

### 2. Interactive Performance
- RTT, jitter, and packet loss compared across No-QoS, Static-QoS, Adaptive-QoS.
- 10 discrete trials per scenario; results reported as mean ± trial sample data.

### 3. Fairness & Starvation Prevention
- 4-phase weighted max-min allocation guarantees non-zero floor for every class.
- Anti-starvation floor: minimum 2% (= ≥8 kbit at any capacity ≥0.4 Mbps).
- Jain's fairness index computed per scenario.

### 4. Adaptation Speed & Policy Stability
- Policy recalculates at 1 Hz (each telemetry tick).
- Intent activation triggers an immediate off-tick policy update.
- Congestion state machine: NORMAL → WARNING → CONGESTED → RECOVERING.
- Anti-oscillation hysteresis: 5s min dwell, 10s congested hold, 0.7 exit factor.

### 5. CPU Overhead & Reproducibility
- Tick interval: 1.0 s (configurable via QOS_TICK_SECONDS)
- All scenario configs: `experiments/scenario_configs.yaml`
- Deterministic seeds: embedded in each scenario config.
- Raw trial data preserved in each job's `raw_measurements` field.

## Known Limitations

- **Ingress shaping**: Not implemented. Linux `tc` shapes egress only.
  Full bidirectional shaping requires an IFB device (`ifb0`).
- **WSL boundary**: Windows hotspot traffic is observed via pktmon on Windows.
  Linux tc enforces policy on the Linux gateway — separate operating modes.
- **IPv6 data path**: IPv6 filter commands are generated but the lab testbed
  uses IPv4-only addressing. No IPv6 traffic has been measured.
- **Per-client RTT**: ICMP probe is router→server only (not per-client).
- **Simulation vs. measurement**: On Windows, scenarios use algorithmic
  queueing simulation, not measured kernel traffic.

## Reproduce This Run
```bash
# On Linux with root, iproute2, and iperf3:
python scripts/demo_run.py --scenario iso_video_call --output-dir results/

# On Windows (simulation mode):
python scripts/demo_run.py --scenario iso_video_call --output-dir results/
```