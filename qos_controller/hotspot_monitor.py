"""Hotspot client traffic monitoring, device identification, and metadata classification.

Tracks real connected devices from captured Windows Mobile Hotspot packets,
derives traffic rates, classifies flows payload-free, and handles device lifecycle.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from . import config as cfg
from .classifier import MetadataClassifier
from .models import FlowMetadata, TrafficClass

log = logging.getLogger(__name__)

# Standard Windows Mobile Hotspot default subnet is 192.168.137.0/24
DEFAULT_HOTSPOT_SUBNET = ipaddress.ip_network("192.168.137.0/24")


def is_multicast_or_broadcast(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_multicast or ip.is_loopback or ip.is_unspecified or ip_str == "255.255.255.255"
    except ValueError:
        return False


def is_private_ip(ip_str: str) -> bool:
    try:
        return ipaddress.ip_address(ip_str).is_private
    except ValueError:
        return False


@dataclass
class FlowRecord:
    flow_key: str
    src_ip: str
    dst_ip: str
    protocol: str
    src_port: int | None
    dst_port: int | None
    start_time: float
    last_time: float
    packets: int = 0
    bytes: int = 0
    classified_class: str = "unknown"
    confidence: float = 0.5
    reasons: list[str] = field(default_factory=list)


@dataclass
class DeviceObservation:
    ip: str
    label: str
    mac: str | None = None
    first_seen: float = 0.0
    last_seen: float = 0.0
    rx_bytes: int = 0     # bytes sent to client (download)
    tx_bytes: int = 0     # bytes sent from client (upload)
    rx_packets: int = 0
    tx_packets: int = 0
    rx_rate_mbps: float = 0.0
    tx_rate_mbps: float = 0.0
    active_flows: int = 0
    traffic_classes: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # class -> bytes
    primary_class: str = "unknown"
    confidence: float = 0.5
    manual_class_override: str | None = None
    source_kind: str = "real_capture"  # "real_capture" | "imported_pcapng" | "synthetic_demo"

    @property
    def total_bytes(self) -> int:
        return self.rx_bytes + self.tx_bytes

    @property
    def total_packets(self) -> int:
        return self.rx_packets + self.tx_packets


class HotspotMonitor:
    """Manages real device discovery, flow classification, and traffic metrics from hotspot captures."""

    def __init__(self,
                 hotspot_subnet: str = "192.168.137.0/24",
                 device_ttl_seconds: float = cfg.HOTSPOT_DEVICE_TTL_S,
                 clock: Any = time.monotonic,
                 wall: Any = time.time,
                 enable_synthetic_demo: bool = False) -> None:
        self.device_ttl_seconds = device_ttl_seconds
        self._clock = clock
        self._wall = wall
        try:
            self.hotspot_subnet = ipaddress.ip_network(hotspot_subnet, strict=False)
        except ValueError:
            self.hotspot_subnet = DEFAULT_HOTSPOT_SUBNET

        self.classifier = MetadataClassifier()
        self.devices: dict[str, DeviceObservation] = {}
        self.flows: dict[str, FlowRecord] = {}
        self.user_labels: dict[str, str] = {}
        self.class_overrides: dict[str, str] = {}
        self._last_data_source: str = "none"  # "real_capture", "imported_pcapng", "synthetic_demo"
        self._last_ingest_time: float | None = None
        self._total_ingested_packets = 0
        self._total_ingested_bytes = 0
        self._synthetic_demo_enabled = enable_synthetic_demo
        if enable_synthetic_demo:
            self._seed_iperf_devices()

    def set_synthetic_demo(self, enabled: bool) -> bool:
        """Toggle synthetic demo mode on or off cleanly."""
        self._synthetic_demo_enabled = bool(enabled)
        if self._synthetic_demo_enabled:
            if not any(d.source_kind == "synthetic_demo" for d in self.devices.values()):
                self._seed_iperf_devices()
            else:
                self._last_data_source = "synthetic_demo"
        else:
            synthetic_ips = [ip for ip, d in self.devices.items() if d.source_kind == "synthetic_demo"]
            for ip in synthetic_ips:
                del self.devices[ip]
            if not self.devices:
                self._last_data_source = "none"
        return self._synthetic_demo_enabled

    def _seed_iperf_devices(self) -> None:
        """Initialize synthetic device profiles matching the testbed clients."""
        now = self._wall()
        preset_devices = [
            ("192.168.137.101", "Client-WorkCall (Phone)", "real_time", 0.95, 0.45, 0.12),
            ("192.168.137.102", "Client-Gaming (Laptop)", "interactive", 0.92, 0.85, 0.40),
            ("192.168.137.103", "Client-Video4K (SmartTV)", "adaptive_video", 0.88, 12.50, 0.35),
            ("192.168.137.104", "Client-BulkDL (Tablet)", "bulk", 0.96, 24.80, 1.20),
        ]
        for ip, label, cls_name, conf, rx, tx in preset_devices:
            dev = DeviceObservation(
                ip=ip,
                label=self.user_labels.get(ip, label),
                first_seen=now - 300,
                last_seen=now,
                rx_rate_mbps=rx,
                tx_rate_mbps=tx,
                primary_class=self.class_overrides.get(ip, cls_name),
                confidence=conf,
                active_flows=2,
                source_kind="synthetic_demo",
                traffic_classes={cls_name: 10_000_000},
            )
            self.devices[ip] = dev
        self._last_data_source = "synthetic_demo"
        self._last_ingest_time = now

    def tick_synthetic_from_iperf(self, active_profiles: Sequence[str] = ()) -> None:
        """Periodically vary synthetic rates based on simulated or active iperf traffic."""
        now = self._wall()
        import random
        for ip, d in self.devices.items():
            d.last_seen = now
            jitter = random.uniform(0.92, 1.08)
            # If a specific profile is highlighted
            base_rx = {"real_time": 0.5, "interactive": 0.9, "adaptive_video": 14.0, "bulk": 25.0}.get(d.primary_class, 5.0)
            base_tx = {"real_time": 0.15, "interactive": 0.45, "adaptive_video": 0.4, "bulk": 1.5}.get(d.primary_class, 1.0)
            d.rx_rate_mbps = round(base_rx * jitter, 2)
            d.tx_rate_mbps = round(base_tx * jitter, 2)
            bytes_added = int((d.rx_rate_mbps + d.tx_rate_mbps) * 1e6 / 8)
            d.rx_bytes += int(d.rx_rate_mbps * 1e6 / 8)
            d.tx_bytes += int(d.tx_rate_mbps * 1e6 / 8)
            d.rx_packets += max(1, int(d.rx_rate_mbps * 100))
            d.tx_packets += max(1, int(d.tx_rate_mbps * 60))
            self._total_ingested_bytes += bytes_added
            self._total_ingested_packets += max(2, int((d.rx_rate_mbps + d.tx_rate_mbps) * 80))
        self._last_data_source = "synthetic_demo"
        self._last_ingest_time = now

    def set_device_label(self, ip: str, label: str) -> None:
        """Set a user-defined friendly name for a device."""
        if not ip or not label:
            raise ValueError("Both ip and label are required.")
        self.user_labels[ip] = label.strip()
        if ip in self.devices:
            self.devices[ip].label = label.strip()

    def set_device_class_override(self, ip: str, traffic_class: str) -> None:
        """Allow user to manually correct misclassification for a device."""
        valid_classes = {c.value for c in TrafficClass}
        if traffic_class not in valid_classes:
            raise ValueError(f"Invalid traffic class: {traffic_class}. Must be one of {valid_classes}")
        self.class_overrides[ip] = traffic_class
        if ip in self.devices:
            self.devices[ip].manual_class_override = traffic_class
            self.devices[ip].primary_class = traffic_class
            self.devices[ip].confidence = 1.0

    def is_client_ip(self, ip_str: str) -> bool:
        """Determine if an IP address belongs to a hotspot client."""
        if is_multicast_or_broadcast(ip_str):
            return False
        try:
            addr = ipaddress.ip_address(ip_str)
            # In hotspot subnet but not the gateway (.1)
            if addr in self.hotspot_subnet:
                # Gateway is typically host 1
                if addr == self.hotspot_subnet.network_address + 1:
                    return False
                return True
            # For IPv6, link-local or ULA client addresses
            if addr.version == 6 and not addr.is_loopback:
                return True
            return False
        except ValueError:
            return False

    def ingest_packet_records(self,
                              records: Sequence[Mapping[str, Any]],
                              source_kind: str = "real_capture") -> dict[str, Any]:
        """Ingest extracted packet metadata records into the monitor state."""
        if not records:
            return {"ingested_packets": 0, "active_devices": len(self.devices)}

        now_mono = self._clock()
        now_wall = self._wall()
        self._last_data_source = source_kind
        self._last_ingest_time = now_wall

        # Group records by client
        client_deltas: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"rx_b": 0, "tx_b": 0, "rx_p": 0, "tx_p": 0, "flows": set(), "classes": defaultdict(int)}
        )

        for rec in records:
            src = str(rec.get("src_ip", "")).strip()
            dst = str(rec.get("dst_ip", "")).strip()
            proto = str(rec.get("protocol", "unknown")).lower()
            length = int(rec.get("length", 0))
            ts = float(rec.get("timestamp", now_wall))
            src_port = rec.get("src_port")
            dst_port = rec.get("dst_port")

            if not src or not dst or is_multicast_or_broadcast(src) or is_multicast_or_broadcast(dst):
                continue

            self._total_ingested_packets += 1
            self._total_ingested_bytes += length

            src_is_client = self.is_client_ip(src)
            dst_is_client = self.is_client_ip(dst)

            # Determine client and direction
            if src_is_client:
                client_ip = src
                direction = "uplink"
                d = client_deltas[client_ip]
                d["tx_b"] += length
                d["tx_p"] += 1
            elif dst_is_client:
                client_ip = dst
                direction = "downlink"
                d = client_deltas[client_ip]
                d["rx_b"] += length
                d["rx_p"] += 1
            else:
                # If neither is in the exact hotspot subnet, check if one is private
                if is_private_ip(src) and not is_private_ip(dst):
                    client_ip = src
                    direction = "uplink"
                    d = client_deltas[client_ip]
                    d["tx_b"] += length
                    d["tx_p"] += 1
                elif is_private_ip(dst) and not is_private_ip(src):
                    client_ip = dst
                    direction = "downlink"
                    d = client_deltas[client_ip]
                    d["rx_b"] += length
                    d["rx_p"] += 1
                else:
                    continue

            flow_key = f"{src}:{src_port}->{dst}:{dst_port}/{proto}"
            d["flows"].add(flow_key)

            # Update or create flow record
            flow = self.flows.get(flow_key)
            if flow is None:
                flow = FlowRecord(
                    flow_key=flow_key,
                    src_ip=src,
                    dst_ip=dst,
                    protocol=proto,
                    src_port=src_port,
                    dst_port=dst_port,
                    start_time=ts,
                    last_time=ts,
                    packets=1,
                    bytes=length,
                )
                self.flows[flow_key] = flow
            else:
                flow.packets += 1
                flow.bytes += length
                flow.last_time = ts

            # Classify flow with MetadataClassifier
            duration = max(0.001, flow.last_time - flow.start_time)
            mean_pkt_size = flow.bytes / max(1, flow.packets)
            bps = (flow.bytes * 8.0) / duration if duration > 0 else 0.0
            pps = flow.packets / duration if duration > 0 else 0.0

            classification = self.classifier.classify(FlowMetadata(
                protocol=proto,
                packet_rate=pps,
                mean_packet_size=mean_pkt_size,
                size_stddev=0.0,
                duration_seconds=duration,
                bytes_per_second=bps / 8.0,
                direction=direction,
                device_id=client_ip,
            ))

            flow.classified_class = classification.category.value
            flow.confidence = classification.confidence
            flow.reasons = list(classification.reason_codes)

            d["classes"][flow.classified_class] += length

        # Update DeviceObservations
        for client_ip, delta in client_deltas.items():
            dev = self.devices.get(client_ip)
            if dev is None:
                default_label = self.user_labels.get(client_ip) or f"Device-{client_ip.split('.')[-1] if '.' in client_ip else client_ip[-4:]}"
                dev = DeviceObservation(
                    ip=client_ip,
                    label=default_label,
                    first_seen=now_wall,
                    last_seen=now_wall,
                    source_kind=source_kind,
                )
                self.devices[client_ip] = dev

            dev.last_seen = now_wall
            dev.rx_bytes += delta["rx_b"]
            dev.tx_bytes += delta["tx_b"]
            dev.rx_packets += delta["rx_p"]
            dev.tx_packets += delta["tx_p"]
            dev.active_flows = len(delta["flows"])

            # Compute burst rates (approximate over last 1s batch)
            dev.rx_rate_mbps = round((delta["rx_b"] * 8.0) / 1e6, 3)
            dev.tx_rate_mbps = round((delta["tx_b"] * 8.0) / 1e6, 3)

            for cls_name, b_count in delta["classes"].items():
                dev.traffic_classes[cls_name] += b_count

            # Determine primary class (highest byte volume)
            if dev.manual_class_override:
                dev.primary_class = dev.manual_class_override
                dev.confidence = 1.0
            elif dev.traffic_classes:
                best_cls = max(dev.traffic_classes.items(), key=lambda item: item[1])[0]
                dev.primary_class = best_cls

        self.expire_stale_devices(now_wall)
        return {
            "ingested_packets": len(records),
            "active_devices": len(self.devices),
            "source_kind": source_kind,
        }

    def expire_stale_devices(self, now: float | None = None) -> list[str]:
        """Mark or remove devices not seen within device_ttl_seconds."""
        now_ts = now if now is not None else self._wall()
        stale = []
        # Expire flows older than TTL
        stale_flow_keys = [k for k, f in self.flows.items() if (now_ts - f.last_time) > self.device_ttl_seconds]
        for k in stale_flow_keys:
            del self.flows[k]

        return stale

    def get_devices(self) -> list[dict[str, Any]]:
        """Return structured summary of observed devices."""
        now = self._wall()
        result = []
        for ip, d in self.devices.items():
            age_s = round(now - d.last_seen, 1)
            is_stale = age_s > self.device_ttl_seconds
            result.append({
                "ip": d.ip,
                "label": d.label,
                "mac": d.mac,
                "first_seen": d.first_seen,
                "last_seen": d.last_seen,
                "age_seconds": age_s,
                "is_stale": is_stale,
                "rx_bytes": d.rx_bytes,
                "tx_bytes": d.tx_bytes,
                "total_bytes": d.total_bytes,
                "rx_packets": d.rx_packets,
                "tx_packets": d.tx_packets,
                "total_packets": d.total_packets,
                "rx_rate_mbps": d.rx_rate_mbps,
                "tx_rate_mbps": d.tx_rate_mbps,
                "active_flows": d.active_flows,
                "primary_class": d.primary_class,
                "confidence": d.confidence,
                "manual_class_override": d.manual_class_override,
                "classes_breakdown": dict(d.traffic_classes),
                "source_kind": d.source_kind,
            })
        return sorted(result, key=lambda x: x["last_seen"], reverse=True)

    def get_summary(self) -> dict[str, Any]:
        """Summary for the Hotspot Dashboard page."""
        devs = self.get_devices()
        active_devs = [d for d in devs if not d["is_stale"]]

        total_rx_mbps = sum(d["rx_rate_mbps"] for d in active_devs)
        total_tx_mbps = sum(d["tx_rate_mbps"] for d in active_devs)

        classes_totals: dict[str, int] = defaultdict(int)
        for d in devs:
            for cls_name, b_count in d["classes_breakdown"].items():
                classes_totals[cls_name] += b_count

        return {
            "source_kind": self._last_data_source,
            "is_real_capture": self._last_data_source == "real_capture",
            "is_imported_pcapng": self._last_data_source == "imported_pcapng",
            "is_synthetic": self._last_data_source == "synthetic_demo",
            "last_ingest_time": self._last_ingest_time,
            "total_devices_seen": len(devs),
            "active_devices": len(active_devs),
            "total_rx_rate_mbps": round(total_rx_mbps, 3),
            "total_tx_rate_mbps": round(total_tx_mbps, 3),
            "total_packets": self._total_ingested_packets,
            "total_bytes": self._total_ingested_bytes,
            "classes_bytes": dict(classes_totals),
            "active_flows": len(self.flows),
            "enforcement_note": (
                "Windows Mobile Hotspot traffic is observed via Windows packet monitoring. "
                "Linux tc inside WSL cannot shape forwarded Windows hotspot traffic directly. "
                "To apply QoS, client devices must be routed through a Linux gateway."
            ),
        }
