#!/usr/bin/env python3
"""
TataElexy Adaptive QoS Engine — Scripted Demo & Evidence Collection Script
===========================================================================

This script implements the PS3 requirement for an automated scripted demo:
    reset → condition injection → traffic generation → evidence collection → report

Usage:
    python scripts/demo_run.py [--scenario SCENARIO] [--output-dir DIR]

Scenarios (matches experiment runner IDs):
    iso_video_call       Bulk download crowding out a VoIP call
    capacity_drop        100 → 20 Mbps link capacity drop
    streams_gaming       4K stream + gaming + bulk contention
    fairness_all_classes All 6 classes competing simultaneously

On Windows (no Linux tc), runs the Algorithmic Queueing Simulation model.
On Linux with root, runs actual kernel namespace experiments.

Output:
    <output-dir>/demo_<scenario>_<timestamp>.{json,md,html,csv}
    <output-dir>/demo_report_summary.md

Exit codes:
    0   All scenarios passed
    1   One or more scenarios failed
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import pathlib
from dataclasses import asdict

# Allow running from any directory
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _check_environment() -> dict:
    """Check what is available in the current environment."""
    is_linux = platform.system() == "Linux"
    is_root = (hasattr(os, "geteuid") and os.geteuid() == 0) if is_linux else False
    has_tc = False
    has_ip = False
    has_iperf = False

    if is_linux:
        import subprocess
        for binary, flag in [("tc", has_tc), ("ip", has_ip), ("iperf3", has_iperf)]:
            try:
                r = subprocess.run(["which", binary], capture_output=True, text=True, timeout=2)
                if binary == "tc":
                    has_tc = r.returncode == 0
                elif binary == "ip":
                    has_ip = r.returncode == 0
                elif binary == "iperf3":
                    has_iperf = r.returncode == 0
            except Exception:
                pass

    env = {
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "is_linux": is_linux,
        "is_root": is_root,
        "has_tc": has_tc,
        "has_ip": has_ip,
        "has_iperf3": has_iperf,
        "mode": (
            "LIVE_KERNEL" if (is_linux and is_root and has_tc and has_ip)
            else "ALGORITHMIC_SIMULATION"
        ),
        "emulation_label": (
            "Linux Kernel Namespace Experiment (real tc enforcement)"
            if (is_linux and is_root and has_tc)
            else "Algorithmic Queueing Simulation (deterministic discrete-event model)"
        ),
    }
    return env


def _print_header(env: dict) -> None:
    print("=" * 70)
    print("  TataElexy Adaptive QoS Engine — Scripted Demo")
    print("=" * 70)
    print(f"  Mode:     {env['mode']}")
    print(f"  Platform: {env['platform']} / Python {env['python_version']}")
    if env["mode"] == "ALGORITHMIC_SIMULATION":
        print()
        print("  NOTE: Running in Algorithmic Queueing Simulation mode.")
        print("  Results are computed via discrete-event queueing theory.")
        print("  For live kernel measurements, run on Linux as root with")
        print("  iproute2, iperf3, and the network namespace testbed.")
    print("=" * 70)
    print()


def _run_scenario(runner, scenario_id: str, env: dict) -> dict:
    """Run a single scenario and collect evidence."""
    print(f"  [→] Running scenario: {scenario_id!r} ...")
    start = time.time()

    job_id = runner.start_scenario(scenario_id)
    # Wait for completion (max 120 seconds)
    for _ in range(240):
        job = runner.get_job(job_id)
        if job and job.state in ("completed", "failed"):
            break
        time.sleep(0.5)

    elapsed = time.time() - start
    job = runner.get_job(job_id)

    if job is None:
        return {"scenario_id": scenario_id, "status": "NOT_FOUND", "elapsed_s": elapsed}
    if job.state == "failed":
        return {"scenario_id": scenario_id, "status": "FAILED", "error": job.error,
                "elapsed_s": elapsed}

    result = job.result
    print(f"  [✓] Completed in {elapsed:.1f}s  ({env['emulation_label']})")

    # Extract key metrics
    comparisons = result.comparisons if result else {}
    no_qos = comparisons.get("no_qos", {})
    adaptive = comparisons.get("adaptive_qos", {})

    rtt_no = no_qos.get("call_rtt_ms", no_qos.get("game_rtt_ms", no_qos.get("rtt_ms")))
    rtt_ad = adaptive.get("call_rtt_ms", adaptive.get("game_rtt_ms", adaptive.get("rtt_ms")))
    improvement = None
    if rtt_no and rtt_ad and rtt_no > 0:
        improvement = round((rtt_no - rtt_ad) / rtt_no * 100, 1)

    row = {
        "scenario_id": scenario_id,
        "scenario_name": result.scenario_name if result else scenario_id,
        "status": "PASSED" if job.state == "completed" else "FAILED",
        "elapsed_s": round(elapsed, 2),
        "mode": env["mode"],
        "emulation_label": env["emulation_label"],
        "no_qos_rtt_ms": rtt_no,
        "adaptive_rtt_ms": rtt_ad,
        "rtt_improvement_pct": improvement,
        "no_qos_loss_pct": no_qos.get("call_packet_loss_pct"),
        "adaptive_loss_pct": adaptive.get("call_packet_loss_pct"),
        "raw_measurement_trials": len(result.raw_measurements) if result else 0,
        "comparisons": comparisons,
        "summary": result.summary_text if result else "",
    }

    if improvement is not None:
        print(f"       RTT:   {rtt_no:.1f} ms (No-QoS) → {rtt_ad:.1f} ms (Adaptive)  "
              f"[{improvement:+.0f}% improvement]")
    return row


def _generate_summary_report(results: list[dict], env: dict, output_dir: pathlib.Path) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    passed = sum(1 for r in results if r.get("status") == "PASSED")
    failed = len(results) - passed

    lines = [
        "# TataElexy Adaptive QoS Engine — Demo Run Report",
        "",
        f"**Generated:** {ts}  ",
        f"**Mode:** {env['emulation_label']}  ",
        f"**Platform:** {env['platform']} / Python {env['python_version']}  ",
        f"**Scenarios:** {passed} passed, {failed} failed  ",
        "",
        "> **Emulation Notice:**",
        f"> This report was generated in **{env['mode']}** mode.",
        "> " + (
            "Results reflect measurements from live Linux kernel namespaces."
            if env["mode"] == "LIVE_KERNEL"
            else "Results are computed by a deterministic discrete-event queueing simulation. "
                 "Metrics are NOT measurements from real traffic. "
                 "For measured results, run on Linux as root with iproute2 and iperf3."
        ),
        "",
        "## Evaluation Summary",
        "",
        "| Scenario | Status | No-QoS RTT | Adaptive RTT | Improvement | Trials |",
        "|---|---|---|---|---|---|",
    ]

    for r in results:
        rtt_no = f"{r['no_qos_rtt_ms']:.1f} ms" if r.get("no_qos_rtt_ms") else "N/A"
        rtt_ad = f"{r['adaptive_rtt_ms']:.1f} ms" if r.get("adaptive_rtt_ms") else "N/A"
        impr = f"{r['rtt_improvement_pct']:.0f}%" if r.get("rtt_improvement_pct") else "N/A"
        trials = r.get("raw_measurement_trials", 0)
        status_icon = "✅" if r.get("status") == "PASSED" else "❌"
        lines.append(
            f"| {r['scenario_id']} | {status_icon} {r.get('status', 'UNKNOWN')} | "
            f"{rtt_no} | {rtt_ad} | {impr} | {trials} |"
        )

    lines += [
        "",
        "## Evaluation Dimensions (PS3)",
        "",
        "### 1. Classification & Capacity Accuracy",
        "- Classifier uses Layer-3/4 metadata only (zero payload inspection).",
        "- Confidence scores (0.0–1.0) reported per flow; UNKNOWN class for ambiguous flows.",
        "- Capacity estimated via EWMA + saturation-aware estimator.",
        "- Idle link holds previous capacity floor (no collapse during low demand).",
        "",
        "### 2. Interactive Performance",
        "- RTT, jitter, and packet loss compared across No-QoS, Static-QoS, Adaptive-QoS.",
        "- 10 discrete trials per scenario; results reported as mean ± trial sample data.",
        "",
        "### 3. Fairness & Starvation Prevention",
        "- 4-phase weighted max-min allocation guarantees non-zero floor for every class.",
        "- Anti-starvation floor: minimum 2% (= ≥8 kbit at any capacity ≥0.4 Mbps).",
        "- Jain's fairness index computed per scenario.",
        "",
        "### 4. Adaptation Speed & Policy Stability",
        "- Policy recalculates at 1 Hz (each telemetry tick).",
        "- Intent activation triggers an immediate off-tick policy update.",
        "- Congestion state machine: NORMAL → WARNING → CONGESTED → RECOVERING.",
        "- Anti-oscillation hysteresis: 5s min dwell, 10s congested hold, 0.7 exit factor.",
        "",
        "### 5. CPU Overhead & Reproducibility",
        f"- Tick interval: 1.0 s (configurable via QOS_TICK_SECONDS)",
        f"- All scenario configs: `experiments/scenario_configs.yaml`",
        f"- Deterministic seeds: embedded in each scenario config.",
        f"- Raw trial data preserved in each job's `raw_measurements` field.",
        "",
        "## Known Limitations",
        "",
        "- **Ingress shaping**: Not implemented. Linux `tc` shapes egress only.",
        "  Full bidirectional shaping requires an IFB device (`ifb0`).",
        "- **WSL boundary**: Windows hotspot traffic is observed via pktmon on Windows.",
        "  Linux tc enforces policy on the Linux gateway — separate operating modes.",
        "- **IPv6 data path**: IPv6 filter commands are generated but the lab testbed",
        "  uses IPv4-only addressing. No IPv6 traffic has been measured.",
        "- **Per-client RTT**: ICMP probe is router→server only (not per-client).",
        "- **Simulation vs. measurement**: On Windows, scenarios use algorithmic",
        "  queueing simulation, not measured kernel traffic.",
        "",
        "## Reproduce This Run",
        "```bash",
        "# On Linux with root, iproute2, and iperf3:",
        "python scripts/demo_run.py --scenario iso_video_call --output-dir results/",
        "",
        "# On Windows (simulation mode):",
        "python scripts/demo_run.py --scenario iso_video_call --output-dir results/",
        "```",
    ]

    report = "\n".join(lines)
    out_path = output_dir / "demo_report_summary.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\n  [✓] Summary report: {out_path}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TataElexy scripted demo runner")
    parser.add_argument("--scenario", default=None,
                        help="Single scenario ID to run (default: all)")
    parser.add_argument("--output-dir", default="demo_results",
                        help="Output directory for reports")
    args = parser.parse_args(argv)

    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    env = _check_environment()
    _print_header(env)

    from qos_controller.experiments.runner import ExperimentRunner
    runner = ExperimentRunner(output_dir=output_dir)

    scenarios = runner.list_scenarios()
    if args.scenario:
        matching = [s for s in scenarios if s["id"] == args.scenario]
        if not matching:
            print(f"ERROR: Unknown scenario {args.scenario!r}")
            print(f"Available: {[s['id'] for s in scenarios]}")
            return 1
        scenarios = matching

    print(f"Running {len(scenarios)} scenario(s)...\n")
    results = []
    for s in scenarios:
        result = _run_scenario(runner, s["id"], env)
        results.append(result)

    _generate_summary_report(results, env, output_dir)

    # Save full JSON
    full_json = output_dir / f"demo_results_{int(time.time())}.json"
    full_json.write_text(json.dumps({"env": env, "results": results}, indent=2), encoding="utf-8")
    print(f"  [✓] Full JSON: {full_json}")

    failed = [r for r in results if r.get("status") != "PASSED"]
    if failed:
        print(f"\n  FAILED: {[r['scenario_id'] for r in failed]}")
        return 1
    print(f"\n  All {len(results)} scenario(s) passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
