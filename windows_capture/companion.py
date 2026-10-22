"""Windows Capture Companion for Mobile Hotspot monitoring.

Orchestrates pktmon capture, PCAPNG conversion, metadata extraction,
and ingestion into the TataElexy QoS controller.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import struct
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .adapter_discovery import (
    HotspotTarget,
    discover_adapters,
    discover_pktmon_components,
    find_hotspot_target,
    is_windows,
    run_command_safe,
)

log = logging.getLogger(__name__)


def is_admin() -> bool:
    """Check if the current process has Windows Administrator privileges."""
    if not is_windows():
        return False
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0  # type: ignore[attr-defined]
    except Exception:
        return False


@dataclass
class PacketMetadata:
    timestamp: float
    src_ip: str
    dst_ip: str
    protocol: str
    length: int
    src_port: int | None = None
    dst_port: int | None = None


@dataclass
class CaptureStatus:
    state: str  # "idle", "capturing", "converting", "error"
    is_admin: bool
    component_id: int | None
    target_description: str | None
    is_hotspot_direct: bool
    confidence: str
    output_dir: str
    current_file: str | None = None
    packets_captured: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    message: str = ""


class WindowsCaptureCompanion:
    """Manages the lifecycle of pktmon captures on Windows."""

    def __init__(self, output_dir: str | Path = "captures",
                 max_retained_files: int = 5) -> None:
        self.output_dir = Path(output_dir)
        self.max_retained_files = max_retained_files
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._target: HotspotTarget | None = None
        self._current_etl: Path | None = None
        self._current_pcapng: Path | None = None
        self._start_time: float | None = None
        self._state = "idle"
        self._last_error: str | None = None
        self._last_message: str = ""

    def get_status(self) -> CaptureStatus:
        target = self.get_target()
        dur = (time.monotonic() - self._start_time) if self._start_time and self._state == "capturing" else 0.0
        return CaptureStatus(
            state=self._state,
            is_admin=is_admin(),
            component_id=target.component_id,
            target_description=target.description or target.adapter_name,
            is_hotspot_direct=target.is_direct,
            confidence=target.confidence,
            output_dir=str(self.output_dir.resolve()),
            current_file=str(self._current_etl) if self._current_etl else None,
            duration_seconds=round(dur, 1),
            error=self._last_error,
            message=self._last_message or target.message,
        )

    def get_target(self, custom_component_id: int | None = None) -> HotspotTarget:
        if self._target is None or custom_component_id is not None:
            self._target = find_hotspot_target(custom_component_id)
        return self._target

    def start_capture(self, component_id: int | None = None,
                      packet_size_limit: int = 128) -> dict[str, Any]:
        """Start a pktmon capture batch."""
        if not is_windows():
            raise RuntimeError("Windows pktmon capture is only supported on Windows.")
        if not is_admin():
            raise PermissionError("Administrator privileges are required to run pktmon.")

        target = self.get_target(component_id)
        if target.component_id is None:
            raise RuntimeError(f"Cannot start capture: {target.message}")

        # Stop any existing capture first and clear filters
        self.stop_capture(ignore_errors=True)
        run_command_safe(["pktmon", "filter", "remove"])

        # Timestamped output path
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        etl_name = f"hotspot_{ts_str}.etl"
        self._current_etl = self.output_dir / etl_name
        self._current_pcapng = self.output_dir / f"hotspot_{ts_str}.pcapng"

        # Build command: pktmon start --capture --comp <id> --file-name <path> --pkt-size <size>
        cmd = [
            "pktmon", "start",
            "--capture",
            "--comp", str(target.component_id),
            "--file-name", str(self._current_etl.resolve()),
            "--pkt-size", str(packet_size_limit),
        ]

        rc, stdout, stderr = run_command_safe(cmd, timeout=10.0)
        if rc != 0:
            self._state = "error"
            self._last_error = stderr.strip() or stdout.strip() or f"rc={rc}"
            raise RuntimeError(f"pktmon start failed: {self._last_error}")

        self._state = "capturing"
        self._start_time = time.monotonic()
        self._last_error = None
        self._last_message = f"Capture started on component {target.component_id} ({target.description})"
        log.info(self._last_message)
        return asdict(self.get_status())

    def stop_capture(self, ignore_errors: bool = False) -> dict[str, Any]:
        """Stop the currently active pktmon capture."""
        if not is_windows():
            return asdict(self.get_status())

        rc, stdout, stderr = run_command_safe(["pktmon", "stop"], timeout=10.0)
        if rc != 0 and not ignore_errors:
            self._state = "error"
            self._last_error = stderr.strip() or stdout.strip()
            raise RuntimeError(f"pktmon stop failed: {self._last_error}")

        self._state = "idle"
        self._start_time = None
        self._last_message = "Capture stopped."
        log.info("pktmon capture stopped.")
        return asdict(self.get_status())

    def convert_to_pcapng(self, etl_path: Path | None = None,
                          pcapng_path: Path | None = None) -> Path:
        """Convert a captured .etl file to standard .pcapng using `pktmon pcapng`."""
        if not is_windows():
            raise RuntimeError("pktmon pcapng conversion is only supported on Windows.")

        source = etl_path or self._current_etl
        if not source or not source.exists():
            raise FileNotFoundError(f"ETL capture file not found: {source}")

        dest = pcapng_path or (source.with_suffix(".pcapng"))

        cmd = ["pktmon", "pcapng", str(source.resolve()), "-o", str(dest.resolve())]
        rc, stdout, stderr = run_command_safe(cmd, timeout=30.0)
        if rc != 0 or not dest.exists():
            raise RuntimeError(f"pktmon pcapng conversion failed (rc={rc}): {stderr.strip() or stdout.strip()}")

        self._rotate_files()
        return dest

    def extract_metadata(self, pcap_or_pcapng_path: Path) -> list[dict[str, Any]]:
        """Extract packet metadata (IP src, dst, protocol, length, timestamp)

        Reads pure metadata without inspecting payloads. Supports PCAP and PCAPNG blocks.
        """
        if not pcap_or_pcapng_path.exists():
            raise FileNotFoundError(f"Capture file not found: {pcap_or_pcapng_path}")

        records: list[dict[str, Any]] = []
        with open(pcap_or_pcapng_path, "rb") as f:
            data = f.read()

        if len(data) < 24:
            return records

        # Check file magic number
        # PCAP: 0xa1b2c3d4 or 0xd4c3b2a1
        # PCAPNG: 0x0a0d0d0a (Section Header Block)
        magic = data[:4]

        if magic in (b"\xa1\xb2\xc3\xd4", b"\xd4\xc3\xb2\xa1"):
            # Classic PCAP parser
            endian = "<" if magic == b"\xd4\xc3\xb2\xa1" else ">"
            offset = 24  # Skip file header
            while offset + 16 <= len(data):
                ts_sec, ts_usec, incl_len, orig_len = struct.unpack(
                    endian + "IIII", data[offset:offset + 16]
                )
                offset += 16
                pkt_data = data[offset:offset + incl_len]
                offset += incl_len
                meta = self._parse_ip_layer(pkt_data, ts_sec + ts_usec / 1e6)
                if meta:
                    records.append(meta)
        else:
            # PCAPNG or text fallback parsing
            records.extend(self._parse_pcapng_blocks(data))

        return records

    def _parse_pcapng_blocks(self, data: bytes) -> list[dict[str, Any]]:
        """Basic pure-Python parser for PCAPNG Enhanced Packet Blocks (Block Type 0x00000006)."""
        records: list[dict[str, Any]] = []
        offset = 0
        total_len = len(data)

        while offset + 8 <= total_len:
            block_type, block_len = struct.unpack("<II", data[offset:offset + 8])
            if block_len < 12 or offset + block_len > total_len:
                break

            # Enhanced Packet Block: Type 0x00000006
            if block_type == 6 and block_len >= 32:
                # EPB header: type(4), total_len(4), iface_id(4), ts_high(4), ts_low(4), cap_len(4), orig_len(4)
                _, _, _, ts_high, ts_low, cap_len, orig_len = struct.unpack(
                    "<IIIIIII", data[offset:offset + 28]
                )
                ts = ((ts_high << 32) | ts_low) / 1e6
                pkt_data = data[offset + 28:offset + 28 + cap_len]
                meta = self._parse_ip_layer(pkt_data, ts)
                if meta:
                    records.append(meta)

            # Simple Packet Block: Type 0x00000003
            elif block_type == 3 and block_len >= 16:
                orig_len = struct.unpack("<I", data[offset + 8:offset + 12])[0]
                cap_len = min(orig_len, block_len - 16)
                pkt_data = data[offset + 12:offset + 12 + cap_len]
                meta = self._parse_ip_layer(pkt_data, time.time())
                if meta:
                    records.append(meta)

            offset += block_len

        return records

    def _parse_ip_layer(self, pkt: bytes, timestamp: float) -> dict[str, Any] | None:
        """Parse Ethernet + IPv4/IPv6 headers to extract flow metadata without payloads."""
        if len(pkt) < 14:
            return None

        # Ethernet frame header (14 bytes): DstMAC(6), SrcMAC(6), EtherType(2)
        eth_type = struct.unpack("!H", pkt[12:14])[0]
        ip_offset = 14

        # Handle 802.1Q VLAN tag
        if eth_type == 0x8100 and len(pkt) >= 18:
            eth_type = struct.unpack("!H", pkt[16:18])[0]
            ip_offset = 18

        # IPv4
        if eth_type == 0x0800 and len(pkt) >= ip_offset + 20:
            ip_hdr = pkt[ip_offset:ip_offset + 20]
            ihl = (ip_hdr[0] & 0x0F) * 4
            total_len = struct.unpack("!H", ip_hdr[2:4])[0]
            proto_num = ip_hdr[9]
            src_ip = ".".join(str(b) for b in ip_hdr[12:16])
            dst_ip = ".".join(str(b) for b in ip_hdr[16:20])

            proto = "tcp" if proto_num == 6 else "udp" if proto_num == 17 else "icmp" if proto_num == 1 else f"proto_{proto_num}"
            src_port, dst_port = None, None
            l4_offset = ip_offset + ihl
            if proto in ("tcp", "udp") and len(pkt) >= l4_offset + 4:
                src_port, dst_port = struct.unpack("!HH", pkt[l4_offset:l4_offset + 4])

            return {
                "timestamp": timestamp,
                "src_ip": src_ip,
                "dst_ip": dst_ip,
                "protocol": proto,
                "length": total_len or len(pkt),
                "src_port": src_port,
                "dst_port": dst_port,
            }

        # IPv6
        elif eth_type == 0x86DD and len(pkt) >= ip_offset + 40:
            ip_hdr = pkt[ip_offset:ip_offset + 40]
            payload_len = struct.unpack("!H", ip_hdr[4:6])[0]
            next_hdr = ip_hdr[6]
            import ipaddress
            src_ip = str(ipaddress.IPv6Address(ip_hdr[8:24]))
            dst_ip = str(ipaddress.IPv6Address(ip_hdr[24:40]))

            proto = "tcp" if next_hdr == 6 else "udp" if next_hdr == 17 else f"ipv6_{next_hdr}"
            src_port, dst_port = None, None
            l4_offset = ip_offset + 40
            if proto in ("tcp", "udp") and len(pkt) >= l4_offset + 4:
                src_port, dst_port = struct.unpack("!HH", pkt[l4_offset:l4_offset + 4])

            return {
                "timestamp": timestamp,
                "src_ip": src_ip,
                "dst_ip": dst_ip,
                "protocol": proto,
                "length": payload_len + 40,
                "src_port": src_port,
                "dst_port": dst_port,
            }

        return None

    def start_continuous_loop(self, interval_seconds: int = 5, callback=None) -> bool:
        """Runs automated background batch capture bursts every interval_seconds."""
        if getattr(self, "_continuous_thread", None) and self._continuous_thread.is_alive():
            return False
        import threading
        self._stop_continuous = threading.Event()
        def _loop():
            while not self._stop_continuous.is_set():
                try:
                    self.start_capture()
                    self._stop_continuous.wait(interval_seconds)
                    if self._stop_continuous.is_set():
                        self.stop_capture(ignore_errors=True)
                        break
                    self.stop_capture(ignore_errors=True)
                    pcapng = self.convert_to_pcapng()
                    records = self.extract_metadata(pcapng)
                    if callback and records:
                        callback(records)
                except Exception as exc:
                    log.debug("Continuous loop error: %s", exc)
                    self._stop_continuous.wait(interval_seconds)
        self._continuous_thread = threading.Thread(target=_loop, daemon=True, name="hotspot-continuous")
        self._continuous_thread.start()
        return True

    def stop_continuous_loop(self) -> bool:
        if hasattr(self, "_stop_continuous"):
            self._stop_continuous.set()
        self.stop_capture(ignore_errors=True)
        return True

    @property
    def is_continuous(self) -> bool:
        return bool(getattr(self, "_continuous_thread", None) and self._continuous_thread.is_alive())

    def _rotate_files(self) -> None:
        """Keep only up to max_retained_files in the output directory."""
        try:
            files = sorted(self.output_dir.glob("hotspot_*.*"), key=lambda p: p.stat().st_mtime)
            if len(files) > self.max_retained_files * 2:
                for old in files[:-self.max_retained_files * 2]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        except Exception as exc:
            log.debug("Rotation error: %s", exc)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="TataElexy Windows Hotspot Capture Companion")
    parser.add_argument("action", choices=["status", "components", "adapters", "start", "stop", "convert"])
    parser.add_argument("--comp", type=int, help="Component ID to capture")
    parser.add_argument("--output", default="captures", help="Output directory")
    args = parser.parse_args()

    companion = WindowsCaptureCompanion(output_dir=args.output)
    if args.action == "status":
        print(json.dumps(asdict(companion.get_status()), indent=2))
    elif args.action == "components":
        comps = discover_pktmon_components()
        print(json.dumps([asdict(c) for c in comps], indent=2))
    elif args.action == "adapters":
        adapters = discover_adapters()
        print(json.dumps(adapters, indent=2))
    elif args.action == "start":
        print(json.dumps(companion.start_capture(args.comp), indent=2))
    elif args.action == "stop":
        print(json.dumps(companion.stop_capture(), indent=2))
    elif args.action == "convert":
        p = companion.convert_to_pcapng()
        print(f"Converted to {p}")


if __name__ == "__main__":
    main()
