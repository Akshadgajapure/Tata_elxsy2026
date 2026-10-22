"""REAL-kernel test.  Opt-in because it replaces the qdisc on the router's
server-facing interface.  Needs root, the namespace topology and iperf3:

    sudo -E env "PATH=$PATH" QOS_TC_INTEGRATION=1 python -m pytest tests/test_linux_tc_integration.py -q
"""
import json
import os
import subprocess
import time

import pytest

from qos_controller import config as cfg
from qos_controller.linux_tc import LinuxTCBackend, plan_from_policy
from qos_controller.models import TrafficClass as T
from qos_controller.netns import popen_in_ns
from qos_controller.policy import PolicyEngine

pytestmark = pytest.mark.skipif(os.environ.get("QOS_TC_INTEGRATION") != "1",
                                reason="set QOS_TC_INTEGRATION=1 (needs root + namespaces)")


def test_real_tc_shapes_and_classifies_bulk_traffic() -> None:
    b = LinuxTCBackend()
    plan = plan_from_policy(PolicyEngine().build(20.0), [("10.10.4.10", T.BULK)])
    srv = popen_in_ns(cfg.SERVER_NS, ["iperf3", "-s", "-p", "5304"], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL)
    try:
        assert b.apply_plan(plan).ok
        time.sleep(0.5)
        before = b.stats()[T.BULK].sent_bytes
        out = popen_in_ns("ns-bulk", ["iperf3", "-c", cfg.SERVER_IP, "-p", "5304", "-t", "4", "-P", "2", "-J"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).communicate(timeout=20)[0]
        mbps = json.loads(out)["end"]["sum_received"]["bits_per_second"] / 1e6
        after = b.stats()[T.BULK].sent_bytes
    finally:
        srv.terminate()
        b.clear()
    ceil_mbps = plan.classes[T.BULK].ceil_kbit / 1000
    assert mbps <= ceil_mbps * 1.10, f"bulk {mbps:.1f} Mbit/s exceeded its {ceil_mbps} Mbit/s ceiling"
    assert mbps >= ceil_mbps * 0.5, "bulk did not get anywhere near its ceiling"
    assert after - before > 1_000_000, "bulk packets did not hit the bulk class (filter not working)"
