import pytest

from qos_controller.netns import CommandError, build_argv
from qos_controller.telemetry import RttProbe, parse_proc_net_dev, parse_tc_qdisc

PROC = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo:       0       0    0    0    0     0          0         0        0       0    0    0    0     0       0          0
r-call: 1000 10 0 2 0 0 0 0 5000 50 0 3 0 0 0 0
"""
TC = """qdisc htb 1: root refcnt 2 r2q 10 default 0x10 direct_packets_stat 0
 Sent 123456 bytes 789 pkt (dropped 4, overlimits 12 requeues 0)
 backlog 3000b 2p requeues 0
qdisc fq_codel 10: parent 1:10 limit 10240p
 Sent 1 bytes 1 pkt (dropped 9, overlimits 0 requeues 0)
 backlog 9999b 9p requeues 0
"""


def test_parse_proc_net_dev() -> None:
    c = parse_proc_net_dev(PROC)["r-call"]
    assert (c.rx_bytes, c.rx_packets, c.rx_drop) == (1000, 10, 2)
    assert (c.tx_bytes, c.tx_packets, c.tx_drop) == (5000, 50, 3)
    assert "lo" in parse_proc_net_dev(PROC)


def test_parse_tc_uses_root_qdisc_only() -> None:
    q = parse_tc_qdisc(TC)
    assert (q.backlog_packets, q.backlog_bytes, q.dropped) == (2, 3000, 4)
    assert parse_tc_qdisc("garbage") is None


def test_command_allowlists() -> None:
    assert build_argv("ns-router", ["cat", "/proc/net/dev"])[:4] == ["ip", "netns", "exec", "ns-router"]
    with pytest.raises(CommandError):
        build_argv("ns-evil", ["cat", "/proc/net/dev"])
    with pytest.raises(CommandError):
        build_argv("ns-router", ["sh", "-c", "id"])


def test_rtt_probe_stats_jitter_and_loss() -> None:
    p = RttProbe()
    for seq, rtt in [(1, 1.0), (2, 3.0), (4, 2.0), (5, 2.0)]:  # seq 3 lost
        p.feed(f"64 bytes from 10.10.9.10: icmp_seq={seq} ttl=63 time={rtt} ms")
    s = p.stats()
    assert s["avg_ms"] == 2.0 and s["loss_pct"] == 20.0
    assert s["jitter_ms"] == pytest.approx((2 + 1 + 0) / 3, abs=0.01)
