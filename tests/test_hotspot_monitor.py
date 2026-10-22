"""Tests for HotspotMonitor, device discovery, flow classification, and adapter discovery (Priority 3)."""

import pytest
import time

from qos_controller.hotspot_monitor import HotspotMonitor, is_multicast_or_broadcast
from windows_capture.adapter_discovery import PktmonComponent, find_hotspot_target


def test_is_multicast_or_broadcast():
    assert is_multicast_or_broadcast("255.255.255.255") is True
    assert is_multicast_or_broadcast("224.0.0.1") is True
    assert is_multicast_or_broadcast("127.0.0.1") is True
    assert is_multicast_or_broadcast("192.168.137.5") is False
    assert is_multicast_or_broadcast("10.0.0.1") is False


def test_hotspot_monitor_device_discovery_and_rates():
    now = [1000.0]
    mon = HotspotMonitor(clock=lambda: now[0], wall=lambda: now[0])

    # Ingest packets from a phone at 192.168.137.25 uploading to an internet server
    records = [
        {"timestamp": 1000.0, "src_ip": "192.168.137.25", "dst_ip": "142.250.190.46",
         "protocol": "udp", "length": 200, "src_port": 50000, "dst_port": 443},
        {"timestamp": 1000.1, "src_ip": "142.250.190.46", "dst_ip": "192.168.137.25",
         "protocol": "udp", "length": 200, "src_port": 443, "dst_port": 50000},
    ]

    res = mon.ingest_packet_records(records, source_kind="real_capture")
    assert res["ingested_packets"] == 2
    assert res["active_devices"] == 1

    devs = mon.get_devices()
    assert len(devs) == 1
    d = devs[0]
    assert d["ip"] == "192.168.137.25"
    assert d["tx_bytes"] == 200
    assert d["rx_bytes"] == 200
    assert d["total_bytes"] == 400
    assert d["total_packets"] == 2
    assert d["source_kind"] == "real_capture"

    # User friendly label
    mon.set_device_label("192.168.137.25", "My Android Phone")
    assert mon.get_devices()[0]["label"] == "My Android Phone"


def test_hotspot_monitor_classification_and_override():
    now = [1000.0]
    mon = HotspotMonitor(clock=lambda: now[0], wall=lambda: now[0])

    # Bulk traffic: large TCP packets
    bulk_records = [
        {"timestamp": 1000.0 + i * 0.01, "src_ip": "192.168.137.88", "dst_ip": "93.184.216.34",
         "protocol": "tcp", "length": 1460, "src_port": 45000, "dst_port": 80}
        for i in range(10)
    ]
    mon.ingest_packet_records(bulk_records, source_kind="real_capture")
    dev = mon.get_devices()[0]
    assert dev["total_bytes"] == 14600
    assert dev["primary_class"] in ("bulk", "adaptive_video", "unknown")

    # User override
    mon.set_device_class_override("192.168.137.88", "real_time")
    dev_after = mon.get_devices()[0]
    assert dev_after["primary_class"] == "real_time"
    assert dev_after["confidence"] == 1.0


def test_hotspot_monitor_stale_expiry():
    now = [1000.0]
    mon = HotspotMonitor(device_ttl_seconds=10.0, clock=lambda: now[0], wall=lambda: now[0])

    records = [
        {"timestamp": 1000.0, "src_ip": "192.168.137.99", "dst_ip": "1.1.1.1",
         "protocol": "udp", "length": 64, "src_port": 53, "dst_port": 53}
    ]
    mon.ingest_packet_records(records, source_kind="imported_pcapng")
    assert mon.get_devices()[0]["is_stale"] is False

    # Advance clock past TTL
    now[0] += 15.0
    mon.expire_stale_devices(now[0])
    devs = mon.get_devices()
    assert devs[0]["is_stale"] is True


def test_hotspot_monitor_summary_honesty():
    mon = HotspotMonitor()
    summary = mon.get_summary()
    assert summary["source_kind"] == "none"
    assert summary["is_real_capture"] is False
    assert "enforcement_note" in summary
    assert "Linux tc inside WSL cannot shape" in summary["enforcement_note"]


def test_find_hotspot_target_discovery_logic(monkeypatch):
    import windows_capture.adapter_discovery as ad

    # Mock discover_pktmon_components with simulated Windows output
    fake_components = [
        PktmonComponent(1, "Ethernet", "Intel Gigabit", "Adapter"),
        PktmonComponent(14, "Wi-Fi", "Intel Wi-Fi 6 AX201", "Adapter"),
        PktmonComponent(67, "Local Area Connection* 2", "Microsoft Wi-Fi Direct Virtual Adapter #2", "Adapter"),
    ]
    monkeypatch.setattr(ad, "is_windows", lambda: True)
    monkeypatch.setattr(ad, "discover_pktmon_components", lambda: fake_components)

    # Auto-discovery finds the Wi-Fi Direct Virtual Adapter (ID 67 here)
    target = find_hotspot_target()
    assert target.component_id == 67
    assert target.is_direct is True
    assert target.confidence == "high"

    # User specified ID override
    target_custom = find_hotspot_target(custom_id=14)
    assert target_custom.component_id == 14
    assert target_custom.confidence == "high"
