"""Tests for extended web endpoints: Hotspot Monitor, Experiment Runner, and Backend status."""

import pytest
import time
from app import create_app
from qos_controller.runtime import Runtime
from qos_controller.hotspot_monitor import HotspotMonitor
from qos_controller.experiments.runner import ExperimentRunner
from tests.test_runtime import FakeCounters, FakeGen


class FakeProbe:
    def stats(self): return {"running": True, "samples": 0, "avg_ms": None}


@pytest.fixture()
def client(tmp_path):
    now = [0.0]
    src, gen, probe = FakeCounters(), FakeGen(), FakeProbe()
    rt = Runtime(src, None, probe, gen, clock=lambda: now[0], wall=lambda: 1000 + now[0])
    h_mon = HotspotMonitor(clock=lambda: now[0], wall=lambda: now[0])
    e_run = ExperimentRunner(output_dir=tmp_path)
    app, sio, _ = create_app(runtime=rt, hotspot_monitor=h_mon, experiment_runner=e_run, autostart=False)
    return app.test_client(), h_mon, e_run


def test_hotspot_page_renders(client):
    c, _, _ = client
    res = c.get("/hotspot")
    assert res.status_code == 200
    assert b"Hotspot / Real Devices" in res.data
    assert b"Boundary Notice" in res.data


def test_hotspot_api_endpoints(client):
    c, h_mon, _ = client

    # Status
    st = c.get("/api/hotspot/status").get_json()
    assert "summary" in st
    assert "capture_status" in st

    # Ingest records
    records = [
        {"timestamp": 1000.0, "src_ip": "192.168.137.15", "dst_ip": "8.8.8.8",
         "protocol": "udp", "length": 128, "src_port": 5353, "dst_port": 53}
    ]
    ingest_res = c.post("/api/hotspot/ingest", json={"records": records, "source_kind": "real_capture"})
    assert ingest_res.status_code == 200
    assert ingest_res.get_json()["ingested_packets"] == 1

    # Devices
    devs = c.get("/api/hotspot/devices").get_json()
    assert len(devs) == 1
    assert devs[0]["ip"] == "192.168.137.15"

    # Label device
    lbl_res = c.post("/api/hotspot/label", json={"ip": "192.168.137.15", "label": "Test-Pixel"})
    assert lbl_res.status_code == 200
    assert c.get("/api/hotspot/devices").get_json()[0]["label"] == "Test-Pixel"

    # Classify override
    cls_res = c.post("/api/hotspot/classify", json={"ip": "192.168.137.15", "traffic_class": "real_time"})
    assert cls_res.status_code == 200
    assert c.get("/api/hotspot/devices").get_json()[0]["primary_class"] == "real_time"


def test_experiment_runner_api_workflow(client):
    c, _, e_run = client

    # List scenarios
    scenarios = c.get("/api/experiments/scenarios").get_json()
    assert len(scenarios) >= 6
    assert any(s["id"] == "iso_video_call" for s in scenarios)

    # Run scenario
    run_res = c.post("/api/experiments/run", json={"scenario_id": "iso_video_call"})
    assert run_res.status_code == 200
    job_id = run_res.get_json()["job_id"]

    # Wait for completion
    for _ in range(50):
        j = c.get(f"/api/experiments/job/{job_id}").get_json()
        if j.get("state") in ("completed", "failed"):
            break
        time.sleep(0.05)

    assert j["state"] == "completed"
    assert j["result"] is not None

    # Fetch reports in all formats
    r_html = c.get(f"/api/experiments/report/{job_id}/html")
    assert r_html.status_code == 200
    assert r_html.mimetype == "text/html"

    r_csv = c.get(f"/api/experiments/report/{job_id}/csv")
    assert r_csv.status_code == 200
    assert r_csv.mimetype == "text/csv"

    r_md = c.get(f"/api/experiments/report/{job_id}/md")
    assert r_md.status_code == 200
    assert r_md.mimetype == "text/markdown"

    r_json = c.get(f"/api/experiments/report/{job_id}/json")
    assert r_json.status_code == 200
    assert r_json.mimetype == "application/json"


def test_backend_api(client):
    c, _, _ = client
    b = c.get("/api/backend").get_json()
    assert "configured_backend" in b
    assert "active_backend" in b
    assert "details" in b
