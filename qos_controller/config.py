"""Topology and runtime configuration for the namespace prototype.

Names mirror the already-built topology (ns-gaming/ns-call/ns-stream/ns-bulk
-> ns-router -> ns-server).  Override with environment variables if needed.
Nothing here is a secret; nothing here is accepted from the browser.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Client:
    name: str          # short id used by the API ("gaming")
    namespace: str     # ns-gaming
    router_iface: str  # interface on ns-router facing this client
    ip: str


ROUTER_NS = os.environ.get("QOS_ROUTER_NS", "ns-router")
SERVER_NS = os.environ.get("QOS_SERVER_NS", "ns-server")
SERVER_IP = os.environ.get("QOS_SERVER_IP", "10.10.9.10")
ROUTER_SERVER_IFACE = os.environ.get("QOS_SERVER_IFACE", "r-server")

CLIENTS: tuple[Client, ...] = (
    Client("gaming", "ns-gaming", "r-gaming", "10.10.1.10"),
    Client("call", "ns-call", "r-call", "10.10.2.10"),
    Client("stream", "ns-stream", "r-stream", "10.10.3.10"),
    Client("bulk", "ns-bulk", "r-bulk", "10.10.4.10"),
)

# Namespaces the backend is ever allowed to enter.
ALLOWED_NAMESPACES = frozenset({ROUTER_NS, SERVER_NS, *(c.namespace for c in CLIENTS)})

# RTT probe runs in the router namespace toward the server, so it crosses the
# server-facing egress queue (the expected bottleneck).  It does NOT run in a
# client namespace because that would pollute that client's flow statistics.
PROBE_NS = os.environ.get("QOS_PROBE_NS", ROUTER_NS)

TICK_SECONDS = 1.0
HISTORY_SECONDS = 120
IDLE_RESET_SECONDS = 3          # a client idle this long starts a new "flow"
ACTIVE_PPS = 1.0                # packets/s above which a client counts as active
MIN_CAPACITY_SAMPLE_MBPS = 1.0  # idle samples are not fed to the EWMA
MAX_QUEUE_PACKETS = 1000
STARVATION_SECONDS = 15.0

# ---- Linux tc enforcement (opt-in; see qos_controller/linux_tc.py) ----
# Shaping point: egress of the server-facing router interface (client -> server direction).
TC_IFACE = os.environ.get("QOS_TC_IFACE", ROUTER_SERVER_IFACE)
TC_LEAF_QDISC = os.environ.get("QOS_TC_LEAF", "fq_codel")   # falls back to pfifo if the kernel lacks it
TC_QUEUE_LIMIT_PACKETS = int(os.environ.get("QOS_TC_QUEUE_LIMIT", "200"))
TC_NAMESPACE = os.environ.get("QOS_TC_NS", ROUTER_NS)  # namespace where tc enforcement runs

# ---- Backend selection -----------------------------------------------
# QOS_BACKEND=dry_run   (default) Policy is calculated but NOT applied to the kernel.
# QOS_BACKEND=linux_tc  Real kernel HTB enforcement via 'tc'. Requires root + iproute2.
#
# Switching requires a restart; live switching is not supported because a partial
# tc state could be left if the process is interrupted mid-apply.
ENFORCEMENT_BACKEND = os.environ.get("QOS_BACKEND", "dry_run")  # "dry_run" | "linux_tc"

# ---- Intent allocation boost -----------------------------------------
# When a user intent is active the prioritised class gets its weight multiplied
# by INTENT_WEIGHT_BOOST and its minimum_percent raised by INTENT_MIN_BOOST_PP,
# both bounded by that class's maximum_percent to prevent starvation of others.
INTENT_WEIGHT_BOOST = float(os.environ.get("QOS_INTENT_WEIGHT_BOOST", "2.0"))
INTENT_MIN_BOOST_PP = float(os.environ.get("QOS_INTENT_MIN_BOOST_PP", "10.0"))

# ---- Hotspot / Windows companion ------------------------------------
HOTSPOT_INGEST_MAX_BYTES = int(os.environ.get("QOS_HOTSPOT_MAX_BYTES", str(10 * 1024 * 1024)))  # 10 MB
HOTSPOT_DEVICE_TTL_S = float(os.environ.get("QOS_HOTSPOT_DEVICE_TTL", "60.0"))   # stale-device expiry
HOTSPOT_PCAPNG_RETAIN = int(os.environ.get("QOS_HOTSPOT_PCAPNG_RETAIN", "5"))    # ring-file count
HOTSPOT_CAPTURE_DIR = os.environ.get("QOS_HOTSPOT_CAPTURE_DIR", "captures")      # local directory

# ---- Experiment runner -----------------------------------------------
EXPERIMENT_OUTPUT_DIR = os.environ.get("QOS_EXPERIMENT_DIR", "experiment_results")
