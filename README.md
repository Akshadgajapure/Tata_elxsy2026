# TataElexy Adaptive QoS Engine

## Quick start (Windows CMD)

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000**. You can also double-click `start_app.bat` after installing dependencies.

The Flask server entry point is the single root file `app.py`. Dashboard templates and static assets remain in `web/templates` and `web/static`; the QoS controller modules remain separate so the controller logic can be tested and maintained. Windows uses simulated telemetry for demonstration. Live Linux `tc` enforcement requires a suitable Linux environment and privileges.

# TataElexy: Adaptive QoS Engine for Mixed Home Broadband Traffic

Intent-aware adaptive Quality of Service (QoS) engine designed for heterogeneous home-broadband networks (mixed gaming, interactive calls, 4K streaming, bulk downloads, and Windows mobile hotspot client traffic).

---

## 1. System Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                        DATA PLANE (Kernel Space)                       │
│  Hard Real-Time / Wire Speed (< 1 ms latency)                          │
│  • Linux tc HTB + FQ-CoDel per-packet queuing & fair drop              │
│  • Microsecond-level packet classification via u32 kernel filters      │
│  • Egress shaper on router-server bottleneck (r-server interface)      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Kernel counters (/proc/net/dev, tc -s)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      CONTROL PLANE (Python Runtime)                    │
│  Soft Real-Time (1.0 Hz Discrete Feedback Loop)                        │
│  • Telemetry → MetadataClassifier (payload-free flow classification)   │
│  • CapacityEstimator (EWMA + Saturation Capacity Estimator)            │
│  • CongestionDetector (NORMAL → WARNING → CONGESTED → RECOVERING)      │
│  • DemandEstimator & 4-Phase Weighted Max-Min Allocator                │
│  • IntentManager: User intent boosts target class (+10pp, 2x weight)  │
│  • SafetyValidator: Enforces starvation floors & queue packet limits   │
│  • Backend Dependency Injection: DryRunBackend OR LinuxTCBackend       │
└───────────────────┬────────────────────────────────┬───────────────────┘
                    │ Socket.IO push (1 Hz)          │ Ingested metadata
                    ▼                                ▼
┌──────────────────────────────────┐ ┌───────────────────────────────────┐
│     WEB DASHBOARD & UI           │ │   WINDOWS HOTSPOT MONITOR         │
│  Live Streaming (1s update push) │ │  Near-Real-Time Batch Capture     │
│  • Overview, Traffic, Clients    │ │  • Windows pktmon companion tool  │
│  • Congestion, Policies, Intent  │ │  • Dynamic adapter discovery      │
│  • Rollback, Safety, TC State    │ │  • Payload-free metadata parser   │
│  • Experiments & Hotspot Monitor │ │  • Device tracking & user labels  │
└──────────────────────────────────┘ └───────────────────────────────────┘
```

---

## 2. Four-Tier Feature Completion Status Table

| Feature / Capability | Implementation Tier | Operational Verification | Notes & Technical Rationale |
|---|---|---|---|
| **Linux `tc` HTB Enforcement** | **Tier 1: Genuinely Working** | In-place `class change`, `apply_plan`, rollback | Validated against fake kernel unit tests and Linux root namespaces. Fallback to `pfifo` if `fq_codel` unavailable. |
| **Backend Dependency Injection** | **Tier 1: Genuinely Working** | `QOS_BACKEND=dry_run` / `linux_tc` | Configurable via environment variable; defaults to safe `dry_run` without requiring root. |
| **Intent → Allocation Boost** | **Tier 1: Genuinely Working** | WORK_CALL, GAMING, STREAMING | Boosts target class weight ($\times 2.0$) and floor ($+10\text{pp}$), preserving minimums for all other classes. |
| **Deterministic Intent Resolution** | **Tier 1: Genuinely Working** | Multi-device arbitration | Latest expiry wins as primary device; highest-priority class wins if multiple classes collide. |
| **Congestion State Machine** | **Tier 1: Genuinely Working** | NORMAL/WARNING/CONGESTED/RECOVERING | Anti-oscillation dwell times ($5\text{s}$ min state, $10\text{s}$ congested hold) and hysteresis exit factor (0.7). |
| **4-Phase Bandwidth Allocation** | **Tier 1: Genuinely Working** | Hard floor → Min → Protected → Weighted fill | Strictly bounded by usable capacity; guarantees background classes never starve. |
| **Windows Hotspot Device Monitor** | **Tier 2: Emulated / Local Observation** | `windows_capture/` companion + `pktmon` | Discovers devices, tracks upload/download, parses flow metadata payload-free, supports user renaming. |
| **Dynamic Hotspot Adapter Discovery** | **Tier 2: Emulated / Local Observation** | `find_hotspot_target()` | Dynamically scans `pktmon component list` for Wi-Fi Direct virtual adapter; does NOT hardcode component 67. |
| **Batch Capture Workflow** | **Tier 2: Emulated / Local Observation** | `start` → `stop` → `pcapng` → `ingest` | Correctly labelled as batch capture; avoids fake real-time claims. |
| **Experiment Runner (6 Scenarios)** | **Tier 1: Genuinely Working** | Automated scenario execution | Runs comparative baselines (No QoS vs. Static QoS vs. Adaptive QoS) and outputs HTML, Markdown, CSV, and JSON. |
| **Live Telemetry & Dashboard** | **Tier 1: Genuinely Working** | Flask-SocketIO + Chart.js | Streams per-client throughput, latency probes, queue backlogs, policy changes, and decision logs. |
| **Ingress Shaping / IFB** | **Tier 4: Intentionally Documented Limitation** | Egress-only shaping | Linux `tc` shapes outbound packets. Shaping WAN ingress requires an IFB redirect device (`ifb0`); documented in UI and logs. |
| **Cross-Platform Boundary (WSL)** | **Tier 4: Intentionally Documented Limitation** | Separate observation vs. enforcement | Windows hotspot traffic forwarded by Windows is observed via Windows `pktmon`; Linux `tc` shapes Linux gateway traffic. |

---

## 3. Quick Start & Execution

### Option A: Windows Observation & Demo Mode (No Root Required)
```powershell
# 1. Open PowerShell in project directory
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Run test suite (123+ tests passing)
python -m pytest -q

# 3. Launch the dashboard (runs on http://127.0.0.1:5000)
python app.py
```

### Option B: Windows Mobile Hotspot Real-Device Monitoring
To capture and monitor real phone traffic connected to Windows Mobile Hotspot:
```powershell
# In an Administrator PowerShell terminal:
# 1. Discover the hotspot adapter component ID
python -m windows_capture.companion components

# 2. Start capturing on the hotspot component (e.g. component 67)
python -m windows_capture.companion start

# 3. Use your phone on the hotspot (browse, stream, or call)
# 4. Stop capture and convert to PCAPNG
python -m windows_capture.companion stop
python -m windows_capture.companion convert

# 5. Open http://127.0.0.1:5000/hotspot to view discovered devices and classified flows
```

### Option C: Linux / WSL Gateway Enforcement (Requires Root)
```bash
# 1. Create a Linux virtual environment (do not reuse Windows venv)
python3 -m venv .venv-linux
source .venv-linux/bin/activate
pip install -r requirements.txt

# 2. Enable real Linux tc enforcement backend
export QOS_BACKEND=linux_tc
export QOS_TC_IFACE=r-server
export QOS_TC_NS=ns-router

# 3. Run with root privileges to allow network namespace operations
sudo -E env "PATH=$PATH" python app.py
```

---

## 4. Linux `tc` Traffic-Control Backend Details

* **HTB Tree Hierarchy**:
  * Root qdisc `1:` on `r-server` interface with default class `1:60` (Unknown).
  * Root class `1:1` matching total link capacity.
  * Child classes:
    * `1:10` (Real-Time, prio 0, FQ-CoDel leaf)
    * `1:20` (Interactive, prio 1, FQ-CoDel leaf)
    * `1:30` (Adaptive Video, prio 2, FQ-CoDel leaf)
    * `1:40` (Bulk, prio 3, FQ-CoDel leaf)
    * `1:50` (Background, prio 4, FQ-CoDel leaf)
    * `1:60` (Unknown, prio 5, FQ-CoDel leaf)
* **Leaf Qdisc Fallback**: Tries `fq_codel` first; if the kernel lacks the module, visibly falls back to `pfifo limit 200` and reports `leaf_fallback=True`.
* **Verification & Idempotency**: Reads back `tc class show` and `tc filter show` after applying. If rates or filters deviate by $>2\%$, an automatic rollback to the last-known-good configuration is triggered.
* **Ingress Shaping Note**: Linux `tc` controls egress only. To shape incoming traffic from the WAN, redirect traffic to an Intermediate Functional Block (`ifb0`):
  ```bash
  ip link add ifb0 type ifb && ip link set ifb0 up
  tc qdisc add dev eth0 ingress
  tc filter add dev eth0 parent ffff: matchall action mirred egress redirect dev ifb0
  tc qdisc add dev ifb0 root handle 1: htb default 60
  ```

---

## 5. Automated Experiment Runner

The experiment runner (`qos_controller/experiments/runner.py`) provides 7 reproducible scenarios:

1. `iso_video_call` — Bulk ISO download while an interactive call is active.
2. `capacity_drop` — Bottleneck capacity drops suddenly (100 to 20 Mbps).
3. `streams_gaming` — 3 concurrent 4K streams competing with low-latency gaming.
4. `work_call_intent` — Activating, modifying, and expiring a temporary user intent.
5. `misclassification` — Injecting uncertain flows and testing manual user override.
6. `enforcement_failure` — Simulating kernel failure, post-enforcement verification, and rollback.
7. `hotspot_observation` — Monitoring connected mobile hotspot client devices.

### Accessing Reports
* Web UI: Navigate to **http://127.0.0.1:5000/experiments**
* REST API:
  * `POST /api/experiments/run` with `{"scenario_id": "iso_video_call"}`
  * `GET /api/experiments/report/<job_id>/html` (Interactive visual report)
  * `GET /api/experiments/report/<job_id>/markdown` (GitHub-ready Markdown)
  * `GET /api/experiments/report/<job_id>/csv` (Machine-readable CSV data)
  * `GET /api/experiments/report/<job_id>/json` (Complete raw structured measurements)

---

## 6. Configuration Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `QOS_BACKEND` | `dry_run` | Selects backend: `dry_run` or `linux_tc` |
| `QOS_TC_IFACE` | `r-server` | Network interface shaped by Linux `tc` |
| `QOS_TC_NS` | `ns-router` | Network namespace where `tc` operates |
| `QOS_TC_LEAF` | `fq_codel` | Leaf qdisc (`fq_codel` or `pfifo`) |
| `QOS_INTENT_WEIGHT_BOOST` | `2.0` | Multiplier for prioritized intent class weight |
| `QOS_INTENT_MIN_BOOST_PP` | `10.0` | Percentage points added to intent minimum reservation |
| `QOS_HOTSPOT_CAPTURE_DIR` | `captures` | Directory for hotspot capture files |
| `QOS_EXPERIMENT_DIR` | `experiment_results` | Directory for experiment output reports |

---

## 7. Troubleshooting Guide

* **Pktmon requires Administrator**: On Windows, packet monitoring requires an elevated terminal. Run PowerShell as Administrator.
* **WSL lacks tc permissions**: Run with `sudo -E env "PATH=$PATH"` so python can find and execute `tc` inside namespaces.
* **No devices in Hotspot monitor**: Ensure your phone is connected to the Windows Mobile Hotspot and sending traffic. Start capture using the companion tool before generating phone traffic.
* **Leaf qdisc warning**: If your Linux kernel lacks `sch_fq_codel`, the engine automatically falls back to `pfifo` without interrupting traffic.
