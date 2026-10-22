import pytest

from tests.test_runtime import FakeCounters, FakeGen, FakeProbe
from qos_controller.runtime import Runtime
from app import create_app


class StubGen(FakeGen):
    def __init__(self): super().__init__(); self.calls = []
    def start(self, name, duration): self.calls.append(("start", name, duration)); return {"name": name, "running": True}
    def stop(self, name): self.calls.append(("stop", name)); return True
    def status_one(self, name): return {"name": name, "running": False}
    def status(self): return []


@pytest.fixture()
def env():
    now = [0.0]
    src, gen = FakeCounters(), StubGen()
    rt = Runtime(src, None, FakeProbe(), gen, clock=lambda: now[0], wall=lambda: 1000 + now[0])
    app, sio, rt = create_app(runtime=rt)
    return app, sio, rt, src, now, gen


def test_pages_and_api_render(env) -> None:
    app, *_ = env
    c = app.test_client()
    for url in ["/", "/traffic", "/clients", "/policies", "/decisions"]:
        assert c.get(url).status_code == 200
    for url in ["/api/status", "/api/metrics", "/api/clients", "/api/policy", "/api/safety", "/api/decisions"]:
        assert c.get(url).status_code == 200
    assert b"Enforcement: dry-run" in c.get("/").data


def test_traffic_api_is_validated(env) -> None:
    app, _, _, _, _, gen = env
    c = app.test_client()
    assert c.post("/api/traffic", data="x").status_code == 400                     # not JSON
    assert c.post("/api/traffic", json={"profile": "rm -rf /", "action": "start"}).status_code == 400
    assert c.post("/api/traffic", json={"profile": "bulk", "action": "exec"}).status_code == 400
    assert c.post("/api/traffic", json={"profile": "bulk", "action": "start", "duration": "9"}).status_code == 400
    assert gen.calls == []
    assert c.post("/api/traffic", json={"profile": "bulk", "action": "start", "duration": 30}).status_code == 200
    assert gen.calls == [("start", "bulk", 30)]


def test_socketio_streams_history_and_metrics(env) -> None:
    app, sio, rt, src, now, _ = env
    rt.tick()
    now[0] += 1; src.add("r-bulk", 1_250_000, 800); rt.tick()
    client = sio.test_client(app)
    got = {m["name"]: m["args"][0] for m in client.get_received()}
    assert got["history"] and got["history"][-1]["total"]["mbps"] == 10.0
    now[0] += 1; src.add("r-bulk", 2_500_000, 1600); rt.tick()
    names = [m["name"] for m in client.get_received()]
    assert "metrics_update" in names
