#!/usr/bin/env python3
"""Real-traffic proof of tc enforcement (no controller, no dashboard).

Same bottleneck, same workload, only the tc plan differs:
  noqos   all traffic lands in ONE class behind one queue (bufferbloat)
  static  per-class HTB bounds + source-address filters (Static QoS)

Workload: bulk TCP (4 streams, ns-bulk) saturating the link, a UDP voice-like
flow and a ping, both from ns-call.  Measured: call RTT, call-UDP loss/jitter,
bulk throughput, and per-class kernel counters (proof packets hit the classes).

WARNING: replaces the qdisc on the router's server-facing interface and removes
it at the end (unless --keep).  Run as root:
    sudo -E env "PATH=$PATH" python scripts/tc_demo.py --capacity 20
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qos_controller import config as cfg                      # noqa: E402
from qos_controller.linux_tc import LinuxTCBackend, plan_from_policy, plan_from_rates, CLASS_IDS  # noqa: E402
from qos_controller.models import TrafficClass as T           # noqa: E402
from qos_controller.netns import popen_in_ns                  # noqa: E402
from qos_controller.policy import PolicyEngine                # noqa: E402

MAP = [(c.ip, cls) for c, cls in zip(cfg.CLIENTS, (T.INTERACTIVE, T.REAL_TIME, T.ADAPTIVE_VIDEO, T.BULK))]
PORTS = {"bulk": 5304, "call": 5302}


def plan_noqos(cap: float):
    floor = 0.008
    rates = {c: (floor, cap) for c in CLASS_IDS}
    rates[T.UNKNOWN] = (cap - floor * (len(CLASS_IDS) - 1) - 0.001, cap)
    return plan_from_rates(cap, rates, mappings=(), label="no-qos")


def run_phase(name: str, backend: LinuxTCBackend, plan, secs: int) -> dict:
    backend.apply_plan(plan)
    time.sleep(0.5)
    before = backend.stats()                           # counters are cumulative: report deltas
    srv = cfg.SERVER_IP
    bulk = popen_in_ns("ns-bulk", ["iperf3", "-c", srv, "-p", str(PORTS["bulk"]), "-t", str(secs), "-P", "4", "-J"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    time.sleep(2.5)                                    # let bulk fill the queue
    call = popen_in_ns("ns-call", ["iperf3", "-c", srv, "-p", str(PORTS["call"]), "-u", "-b", "80k", "-l", "160",
                                   "-t", str(secs - 3), "-J"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = int((secs - 4) / 0.2)
    ping = popen_in_ns("ns-call", ["ping", "-n", "-i", "0.2", "-c", str(n), srv],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pout = ping.communicate(timeout=secs + 10)[0]
    bj = json.loads(bulk.communicate(timeout=secs + 10)[0] or "{}")
    cj = json.loads(call.communicate(timeout=secs + 10)[0] or "{}")
    m = re.search(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", pout)
    loss = re.search(r"([\d.]+)% packet loss", pout)
    stats = backend.stats()
    return {
        "mode": name,
        "call_rtt_avg_ms": float(m.group(2)) if m else None,
        "call_rtt_max_ms": float(m.group(3)) if m else None,
        "call_ping_loss_pct": float(loss.group(1)) if loss else None,
        "call_udp_loss_pct": cj.get("end", {}).get("sum", {}).get("lost_percent"),
        "call_udp_jitter_ms": cj.get("end", {}).get("sum", {}).get("jitter_ms"),
        "bulk_mbps": round(bj.get("end", {}).get("sum_received", {}).get("bits_per_second", 0) / 1e6, 2),
        "class_sent_mbytes": {c.value: round((s.sent_bytes - before[c].sent_bytes) / 1e6, 2)
                              for c, s in stats.items() if s.sent_bytes > before[c].sent_bytes},
        "class_dropped_pkts": {c.value: s.dropped - before[c].dropped for c, s in stats.items()
                               if s.dropped > before[c].dropped},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capacity", type=float, default=20.0, help="emulated bottleneck, Mbit/s")
    ap.add_argument("--seconds", type=int, default=14)
    ap.add_argument("--keep", action="store_true", help="leave the last shaper installed")
    ap.add_argument("--json", help="write results to this file")
    a = ap.parse_args()
    backend = LinuxTCBackend()
    servers = [popen_in_ns(cfg.SERVER_NS, ["iperf3", "-s", "-p", str(p)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) for p in PORTS.values()]
    time.sleep(0.5)
    results = []
    try:
        results.append(run_phase("no_qos", backend, plan_noqos(a.capacity), a.seconds))
        static = plan_from_policy(PolicyEngine().build(a.capacity), MAP)
        results.append(run_phase("static_qos", backend, static, a.seconds))
    finally:
        for s in servers:
            s.terminate()
        if not a.keep:
            backend.clear()
    print(f"\nleaf qdisc: {backend.leaf}" + ("  (fq_codel unavailable -> pfifo fallback)" if backend.leaf_fallback else ""))
    for r in results:
        print(json.dumps(r, indent=2))
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
