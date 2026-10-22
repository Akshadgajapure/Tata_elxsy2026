"""Dashboard + Stage A wiring.  Scripted kernel counters stand in for the
kernel ONLY to drive the arithmetic; no value here is invented by the code
under test (unmeasured metrics must stay None / 'N/A')."""

import pytest

from qos_controller.analysis import ControlAnalysis
from qos_controller.runtime import Runtime
from tests.test_runtime import FakeCounters, FakeGen
from app import create_app


class Probe:
    def __init__(self, **kw): self.s = {"running": True, "samples": 0, "avg_ms": None, "min_ms": None,
                                        "jitter_ms": None, "loss_pct": None, "error": None, **kw}
    def stats(self): return dict(self.s)


@pytest.fixture()
def env():
    now = [0.0]
    src, gen, probe = FakeCounters(), FakeGen(), Probe()
    rt = Runtime(src, None, probe, gen, clock=lambda: now[0], wall=lambda: 1000 + now[0])
    app, sio, rt = create_app(runtime=rt)
    return app.test_client(), rt, src, now, probe


def step(rt, now, src, bulk=(12_500_000, 8_000), **kw):
    now[0] += 1.0
    if bulk:
        src.add("r-bulk", *bulk)
    for k, v in kw.items():
        src.add(k.replace("_", "-"), *v)
    return rt.tick()


NEW_PAGES = ["/intent", "/congestion", "/safety", "/rollback", "/experiments",
             "/controller", "/telemetry", "/tc"]
ALL_PAGES = ["/", "/traffic", "/clients", "/policies", "/decisions", *NEW_PAGES]


def test_every_sidebar_page_renders_and_nothing_is_disabled(env) -> None:
    c = env[0]
    for url in ALL_PAGES:
        r = c.get(url)
        assert r.status_code == 200, url
        assert b"nav-item disabled" not in r.data.replace(b"nav-item.disabled", b"")
        assert b"Enforcement: dry-run" in r.data


def test_new_api_endpoints_respond(env) -> None:
    c = env[0]
    for url in ["/api/intent", "/api/congestion", "/api/rollback", "/api/safety/detail", "/api/experiments",
                "/api/system/controller", "/api/system/telemetry", "/api/system/tc"]:
        assert c.get(url).status_code == 200, url


def test_experiments_and_tc_do_not_pretend(env) -> None:
    c = env[0]
    assert b"Experiment runner not yet implemented" in c.get("/experiments").data
    j = c.get("/api/experiments").get_json()
    assert j["runner_implemented"] is False and all(p["status"] == "NOT IMPLEMENTED" for p in j["planned"])
    assert b"DRY-RUN \xe2\x80\x94 Linux tc backend not implemented yet" in c.get("/tc").data
    t = c.get("/api/system/tc").get_json()
    assert t["tc_backend_implemented"] is False and t["enforcement_applied"] is False


def test_frames_expose_stage_a_values_and_label_placeholder(env) -> None:
    _, rt, src, now, _ = env
    rt.tick()
    for _ in range(3):
        fr = step(rt, now, src)
    assert fr["capacity"]["kind"] == "PLACEHOLDER" and fr["capacity"]["source_type"] == "ESTIMATED_CAPACITY"
    assert set(fr["demand"]["by_class"]) == {"real_time", "interactive", "adaptive_video", "bulk", "background", "unknown"}
    assert fr["demand"]["total_mbps"] > 0
    a = fr["allocation"]
    assert a["enforced"] is False and a["total_rate_mbps"] <= a["capacity_mbps"] + 1e-6
    assert fr["congestion"]["state"] == "NORMAL" and fr["enforcement"]["tc_backend_implemented"] is False
    assert fr["queue"] is None                                   # not measured -> None, not 0
    assert fr["rtt"]["avg_ms"] is None
    bulk = next(c for c in fr["clients"] if c["name"] == "bulk")
    assert bulk["class_demand_mbps"] is not None
    assert "rtt_ms" not in bulk and "jitter_ms" not in bulk       # per-client latency is not invented


def test_telemetry_failure_frame_has_no_stage_a_values(env) -> None:
    _, rt, src, now, _ = env
    rt.tick(); src.fail = True
    fr = step(rt, now, src, bulk=None)
    assert fr["telemetry_ok"] is False and "demand" not in fr and "allocation" not in fr


def test_idle_system_has_no_policy_no_allocation_na_fairness(env) -> None:
    c, rt, src, now, _ = env
    rt.tick(); fr = step(rt, now, src, bulk=None)
    assert c.get("/api/policy").get_json() is None
    assert fr["allocation"] is None and fr["fairness"]["index"] is None


def test_decisions_carry_context(env) -> None:
    c, rt, src, now, _ = env
    rt.tick()
    for _ in range(3):
        step(rt, now, src)
    ev = [e for e in c.get("/api/decisions").get_json() if e["kind"] == "POLICY_APPLIED_DRY_RUN"][0]
    ctx = ev["context"]
    assert ctx["enforcement_mode"] == "dry-run" and ctx["capacity_kind"] == "PLACEHOLDER"
    assert set(ctx) >= {"capacity_mbps", "demand_mbps", "congestion_state", "intents", "allocation", "safety_ok"}


# ---------------- intent ----------------
def test_intent_api_validation(env) -> None:
    c = env[0]
    ok = {"intent": "WORK_CALL", "target": "client:call", "duration_seconds": 60}
    assert c.post("/api/intent", data="x").status_code == 400
    for bad in [{**ok, "intent": "rm -rf /"}, {**ok, "target": "ns-evil"}, {**ok, "target": "client:call; id"},
                {**ok, "duration_seconds": "60"}, {**ok, "duration_seconds": True},
                {**ok, "duration_seconds": 5}, {**ok, "duration_seconds": 99999}, {**ok, "target": "class:nope"}]:
        assert c.post("/api/intent", json=bad).status_code == 400, bad
    assert c.get("/api/intent").get_json()["active"] == []


def test_intent_activate_policy_expiry_and_log(env) -> None:
    c, rt, src, now, _ = env
    rt.tick()
    r = c.post("/api/intent", json={"intent": "WORK_CALL", "target": "ns-call", "duration_seconds": 30})
    assert r.status_code == 200
    a = c.get("/api/intent").get_json()["active"][0]
    assert a["device_id"] == "client:call" and a["traffic_class"] == "real_time" and a["remaining_s"] == 30
    step(rt, now, src)
    assert c.get("/api/policy").get_json()["intent_device"] == "client:call"
    now[0] += 31                                               # lease runs out on its own
    step(rt, now, src)
    assert c.get("/api/intent").get_json()["active"] == []
    assert c.get("/api/policy").get_json()["intent_device"] is None   # normal policy restored
    kinds = [e["kind"] for e in c.get("/api/decisions").get_json()]
    assert "INTENT_ACTIVATED" in kinds and "INTENT_EXPIRED" in kinds
    hist = c.get("/api/intent").get_json()["history"]
    assert [h["status"] for h in hist] == ["expired"]            # one record per lease, updated in place


def test_intent_cancel_and_class_target(env) -> None:
    c = env[0]
    c.post("/api/intent", json={"intent": "GAMING", "target": "class:interactive", "duration_seconds": 60})
    assert c.get("/api/intent").get_json()["active"][0]["device_id"] == "class:interactive"
    j = c.post("/api/intent/cancel", json={"target": "class:interactive"}).get_json()
    assert j["cancelled"] is True and j["active"] == []
    assert c.post("/api/intent/cancel", json={"target": "bogus"}).status_code == 400


# ---------------- rollback / safety ----------------
def test_rollback_view_reflects_real_rollback(env) -> None:
    c, rt, src, now, _ = env
    rt.tick()
    assert c.get("/api/rollback").get_json()["available"] is False
    step(rt, now, src); step(rt, now, src)
    assert c.get("/api/rollback").get_json()["available"] is True
    rt.backend.fail_next_apply = True
    step(rt, now, src)
    v = c.get("/api/rollback").get_json()
    assert v["last_rollback"]["reason"] == "simulated enforcement failure"
    assert "restored" in v["status"] and v["current"] is not None
    kinds = [e["kind"] for e in c.get("/api/decisions").get_json()]
    assert "POLICY_ROLLED_BACK" in kinds
    s = c.get("/api/safety/detail").get_json()
    assert any(e["kind"] == "POLICY_ROLLED_BACK" for e in s["recent_failures"])


def test_safety_checks_report_na_instead_of_guessing(env) -> None:
    c, rt, src, now, _ = env
    checks = {x["key"]: x for x in c.get("/api/safety/detail").get_json()["checks"]}
    assert checks["policy_validation"]["ok"] is None and checks["valid_interface"]["ok"] is None
    assert checks["valid_namespace"]["ok"] is True and checks["tc_state"]["ok"] is None
    rt.tick(); step(rt, now, src); step(rt, now, src)
    checks = {x["key"]: x for x in c.get("/api/safety/detail").get_json()["checks"]}
    assert checks["policy_validation"]["ok"] is True and checks["valid_interface"]["ok"] is True
    assert checks["rate_bounds"]["ok"] is True and checks["starvation_protection"]["ok"] is True


# ---------------- congestion ----------------
def test_congestion_state_change_from_measured_probe_signals(env) -> None:
    c, rt, src, now, probe = env
    probe.s.update(avg_ms=2.0, min_ms=2.0, loss_pct=0.0)
    rt.tick()
    for _ in range(3):
        step(rt, now, src)
    assert c.get("/api/congestion").get_json()["state"] == "NORMAL"
    probe.s.update(avg_ms=120.0, loss_pct=8.0)                 # RTT inflation + loss from the probe
    for _ in range(25):
        step(rt, now, src)
    v = c.get("/api/congestion").get_json()
    assert v["state"] == "CONGESTED"
    assert [h["new"] for h in v["history"][::-1]][:2] == ["WARNING", "CONGESTED"]
    assert "queue_delay_ms" not in v["signals"]                  # unmeasured signal is absent
    ev = [e for e in c.get("/api/decisions").get_json() if e["kind"] == "CONGESTION_STATE_CHANGE"]
    assert ev and ev[0]["detail"]["reason"].endswith("_above_threshold")


def test_allocation_reacts_to_bulk_demand_and_never_exceeds_capacity(env) -> None:
    _, rt, src, now, _ = env
    rt.tick()
    for _ in range(6):
        fr = step(rt, now, src, r_call=(62 * 202, 62))
    a = fr["allocation"]
    assert a["total_rate_mbps"] <= a["capacity_mbps"] + 1e-6
    assert all(v["rate_mbps"] > 0 for v in a["classes"].values())   # nobody at zero


# ---------------- system views ----------------
def test_system_views_state_what_is_not_measured(env) -> None:
    c, rt, src, now, _ = env
    rt.tick(); step(rt, now, src)
    t = c.get("/api/system/telemetry").get_json()
    m = {x["metric"]: x["measured"] for x in t["metrics"]}
    assert m["Per-client RTT / jitter / loss"] is False and m["Per-class tc statistics"] is False
    assert m["Per-client throughput / packet rate"] is True
    k = c.get("/api/system/controller").get_json()
    assert k["tick_seconds"] == 1.0 and k["ticks"] >= 2 and k["last_tick_ms"] is not None


def test_control_analysis_without_capacity_has_no_allocation() -> None:
    a = ControlAnalysis()
    out, events = a.step(1.0, {}, 0.0, None, {}, 0.0)
    assert out["allocation"] is None and out["congestion"]["state"] == "NORMAL"
    assert out["fairness"]["index"] is None and events == []


def test_reactivating_same_target_marks_old_lease_replaced(env) -> None:
    c = env[0]
    body = {"intent": "WORK_CALL", "target": "client:call", "duration_seconds": 60}
    c.post("/api/intent", json=body); c.post("/api/intent", json={**body, "intent": "GAMING"})
    hist = c.get("/api/intent").get_json()["history"]
    assert [h["status"] for h in hist] == ["active", "replaced"] and len(c.get("/api/intent").get_json()["active"]) == 1


def test_congestion_history_has_wall_clock_time(env) -> None:
    c, rt, src, now, probe = env
    probe.s.update(avg_ms=2.0, min_ms=2.0, loss_pct=0.0)
    rt.tick(); step(rt, now, src)
    probe.s.update(avg_ms=120.0, loss_pct=8.0)
    for _ in range(12):
        step(rt, now, src)
    h = c.get("/api/congestion").get_json()["history"]
    assert h and all(x["ts"] >= 1000 for x in h)                 # wall clock, not monotonic seconds
