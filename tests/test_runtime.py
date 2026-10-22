"""Wiring tests: scripted counters stand in for the kernel ONLY to verify the
arithmetic and the classifier/controller path.  Real traffic is verified on Linux."""

from qos_controller import config as cfg
from qos_controller.runtime import Runtime
from qos_controller.telemetry import IfaceCounters, TelemetryError


class FakeCounters:
    def __init__(self):
        self.t = {c.router_iface: [0, 0, 0, 0] for c in cfg.CLIENTS}  # rxb, rxp, txb, txp
        self.fail = False

    def add(self, iface, rx_bytes, rx_pkts, tx_bytes=0, tx_pkts=0):
        v = self.t[iface]
        v[0] += rx_bytes; v[1] += rx_pkts; v[2] += tx_bytes; v[3] += tx_pkts

    def read(self):
        if self.fail:
            raise TelemetryError("boom")
        return {k: IfaceCounters(v[0], v[1], 0, v[2], v[3], 0) for k, v in self.t.items()}


class FakeGen:
    def __init__(self): self.proto = {}
    def declared_protocol(self, client): return self.proto.get(client)


class FakeProbe:
    def stats(self): return {"running": True, "samples": 0, "avg_ms": None}


def make():
    now = [0.0]
    src, gen = FakeCounters(), FakeGen()
    rt = Runtime(src, None, FakeProbe(), gen, clock=lambda: now[0], wall=lambda: 1000 + now[0])
    return rt, src, gen, now


def step(rt, now, src, **traffic):
    now[0] += 1.0
    for iface, args in traffic.items():
        src.add(iface.replace("_", "-"), *args)
    return rt.tick()


def test_first_tick_needs_baseline_then_computes_rates() -> None:
    rt, src, gen, now = make()
    assert rt.tick() is None
    fr = step(rt, now, src, r_bulk=(12_500_000, 8_000))  # 100 Mbit/s up
    assert fr["total"]["mbps"] == 100.0
    bulk = next(c for c in fr["clients"] if c["name"] == "bulk")
    assert bulk["up_mbps"] == 100.0 and bulk["down_mbps"] == 0.0 and bulk["pps"] == 8000.0


def test_classifier_receives_real_rates_and_class_changes_are_logged() -> None:
    rt, src, gen, now = make()
    gen.proto["call"] = "udp"
    rt.tick()
    fr = None
    for _ in range(4):  # ~62 pps x 202 B, sustained >2 s
        fr = step(rt, now, src, r_call=(62 * 202, 62))
    call = next(c for c in fr["clients"] if c["name"] == "call")
    assert call["class"] == "real_time" and call["confidence"] == 0.84
    assert fr["classes"]["real_time"]["clients"] == ["call"]
    assert any(e["kind"] == "CLIENT_CLASSIFIED" for e in rt.decision_list())


def test_idle_link_holds_capacity_and_does_not_crash() -> None:
    rt, src, gen, now = make()
    rt.tick()
    step(rt, now, src, r_bulk=(12_500_000, 8_000))
    fr = step(rt, now, src, r_bulk=(12_500_000, 8_000))
    cap = fr["capacity"]["mbps"]
    fr = step(rt, now, src)  # zero traffic
    assert fr["total"]["mbps"] == 0.0
    assert fr["capacity"]["mbps"] == cap and fr["capacity"]["held"] is True
    assert rt.policy_view()["capacity_mbps"] == cap


def test_telemetry_failure_is_reported_not_faked() -> None:
    rt, src, gen, now = make()
    rt.tick()
    src.fail = True
    fr = step(rt, now, src)
    assert fr["telemetry_ok"] is False and "boom" in fr["error"]
    assert any(e["kind"] == "TELEMETRY_LOST" for e in rt.decision_list())
    assert rt.snapshot()["history"] == []  # failure frames never enter the chart history


def test_counter_reset_does_not_produce_negative_rates() -> None:
    rt, src, gen, now = make()
    rt.tick()
    step(rt, now, src, r_bulk=(1_000_000, 1000))
    src.t["r-bulk"] = [0, 0, 0, 0]
    fr = step(rt, now, src)
    assert fr["total"]["mbps"] == 0.0
