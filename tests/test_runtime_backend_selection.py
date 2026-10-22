"""Tests for runtime backend selection and dependency injection (Priority 1)."""

import pytest
from unittest.mock import MagicMock

from qos_controller import config as cfg
from qos_controller.enforcement import DryRunBackend
from qos_controller.linux_tc import LinuxTCBackend, TcPlan
from qos_controller.policy import PolicyEngine
from qos_controller.runtime import Runtime, _build_backend
from qos_controller.telemetry import IfaceCounters


class DummyCounters:
    def read(self):
        return {c.router_iface: IfaceCounters(1000, 10, 0, 1000, 10, 0) for c in cfg.CLIENTS}


class DummyGen:
    def declared_protocol(self, client):
        return "udp"


class DummyProbe:
    def stats(self):
        return {"running": True, "samples": 1, "avg_ms": 5.0}


def test_build_backend_default_is_dry_run(monkeypatch):
    monkeypatch.setattr(cfg, "ENFORCEMENT_BACKEND", "dry_run")
    engine = PolicyEngine()
    backend = _build_backend(engine)
    assert isinstance(backend, DryRunBackend)


def test_build_backend_selects_linux_tc_when_configured(monkeypatch):
    monkeypatch.setattr(cfg, "ENFORCEMENT_BACKEND", "linux_tc")
    monkeypatch.setattr(cfg, "TC_NAMESPACE", "test-ns")
    monkeypatch.setattr(cfg, "TC_IFACE", "eth0")
    engine = PolicyEngine()
    backend = _build_backend(engine)
    assert isinstance(backend, LinuxTCBackend)
    assert backend.namespace == "test-ns"
    assert backend.iface == "eth0"


def test_runtime_accepts_injected_backend():
    engine = PolicyEngine()
    custom_backend = DryRunBackend(engine)
    rt = Runtime(
        DummyCounters(), None, DummyProbe(), DummyGen(),
        backend=custom_backend
    )
    assert rt.backend is custom_backend
    assert rt._backend_name == "dry_run"
    assert rt._enforcement()["backend"] == "dry_run"
    assert rt._enforcement()["applies_to_tc"] is False


def test_runtime_with_mocked_linux_tc_backend():
    fake_tc = MagicMock(spec=LinuxTCBackend)
    fake_tc.namespace = "ns-router"
    fake_tc.iface = "r-server"
    fake_tc.leaf = "fq_codel"
    fake_tc.leaf_fallback = False
    fake_tc.active_plan = None
    fake_tc.known_good = None
    fake_tc.status.return_value = {
        "backend": "linux-tc", "namespace": "ns-router", "iface": "r-server",
        "leaf": "fq_codel", "leaf_fallback": False,
        "installed": True, "capacity_kbit": 50000,
        "shaping_direction": "egress (client->server)",
        "ingress_note": "Linux tc controls egress only.",
        "events": [],
    }
    fake_tc.stats.return_value = {}

    rt = Runtime(
        DummyCounters(), None, DummyProbe(), DummyGen(),
        backend=fake_tc
    )
    # Backend detection recognizes LinuxTCBackend
    assert rt._backend_name == "linux_tc"
    enf = rt._enforcement()
    assert enf["backend"] == "linux_tc"
    assert enf["applies_to_tc"] is True
    assert "REAL ENFORCEMENT" in enf["mode"]
    assert "ingress_note" in enf


def test_runtime_enforcement_reports_status_dynamically():
    engine = PolicyEngine()
    backend = DryRunBackend(engine)
    rt = Runtime(DummyCounters(), None, DummyProbe(), DummyGen(), backend=backend)
    assert rt._enforcement()["policy_active"] is False
    # Apply a policy
    policy = engine.build(100.0)
    backend.apply(policy)
    assert rt._enforcement()["policy_active"] is True
