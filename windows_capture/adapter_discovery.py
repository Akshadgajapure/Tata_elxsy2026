"""Discovery of network adapters and Windows pktmon components.

Identifies the proper capture point for Windows Mobile Hotspot without
hardcoding component IDs (such as component 67).
"""

from __future__ import annotations

import logging
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Sequence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PktmonComponent:
    component_id: int
    name: str
    description: str
    component_type: str  # e.g., "Adapter", "Filter", "Protocol"


@dataclass(frozen=True)
class HotspotTarget:
    component_id: int | None
    adapter_name: str | None
    description: str | None
    is_direct: bool
    confidence: str  # "high", "medium", "fallback", "none"
    message: str


def is_windows() -> bool:
    return platform.system() == "Windows"


def run_command_safe(argv: Sequence[str], timeout: float = 5.0) -> tuple[int, str, str]:
    """Execute a command without shell=True and return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"Executable '{argv[0]}' not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"Command '{' '.join(argv)}' timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def discover_pktmon_components() -> list[PktmonComponent]:
    """Run `pktmon component list` and parse the output."""
    if not is_windows():
        log.warning("pktmon is only available on Windows")
        return []

    rc, stdout, stderr = run_command_safe(["pktmon", "component", "list"])
    if rc != 0:
        log.debug("pktmon component list failed (rc=%d): %s", rc, stderr)
        return []

    components: list[PktmonComponent] = []
    # Match patterns like:
    # 67: Microsoft Wi-Fi Direct Virtual Adapter
    # or tabulated blocks with ID, Name, Description
    current_id: int | None = None
    current_name: str = ""
    current_desc: str = ""
    current_type: str = "Adapter"

    for line in stdout.splitlines():
        line_clean = line.strip()
        if not line_clean:
            continue

        # Pattern: "    ID: 67" or "67: <Name>"
        m_id = re.match(r"^(?:ID:\s*)?(\d+)\s*[:\-]\s*(.+)$", line_clean, re.IGNORECASE)
        if m_id:
            try:
                cid = int(m_id.group(1))
                name = m_id.group(2).strip()
                components.append(PktmonComponent(
                    component_id=cid,
                    name=name,
                    description=name,
                    component_type=current_type,
                ))
            except ValueError:
                pass
            continue

        # Tabular match: ID   Name   Description / Driver
        m_tab = re.match(r"^(\d+)\s+([A-Za-z0-9_\-\.\s]+?)\s{2,}(.+)$", line_clean)
        if m_tab:
            try:
                cid = int(m_tab.group(1))
                name = m_tab.group(2).strip()
                desc = m_tab.group(3).strip()
                components.append(PktmonComponent(
                    component_id=cid,
                    name=name,
                    description=desc,
                    component_type="Adapter" if "adapter" in (name + desc).lower() else "Filter",
                ))
            except ValueError:
                pass

    return components


def discover_adapters() -> list[dict]:
    """Discover network adapters on Windows via netsh or Get-NetAdapter."""
    if not is_windows():
        return []

    rc, stdout, _ = run_command_safe(["netsh", "interface", "show", "interface"])
    adapters = []
    if rc == 0:
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] in ("Enabled", "Disabled"):
                state = parts[0]
                status = parts[1]
                itype = parts[2]
                name = " ".join(parts[3:])
                adapters.append({
                    "name": name,
                    "state": state,
                    "status": status,
                    "type": itype,
                })
    return adapters


def find_hotspot_target(custom_id: int | None = None) -> HotspotTarget:
    """Find the best pktmon capture component for the Mobile Hotspot.

    If custom_id is provided, validates that it exists.
    Otherwise searches for Wi-Fi Direct / Hotspot Virtual Adapter components.
    Never relies solely on hardcoded ID 67.
    """
    if not is_windows():
        return HotspotTarget(
            component_id=None,
            adapter_name=None,
            description=None,
            is_direct=False,
            confidence="none",
            message="Operating system is not Windows. Windows pktmon capture unavailable.",
        )

    components = discover_pktmon_components()
    if not components:
        # Check if pktmon is runnable
        rc, _, err = run_command_safe(["pktmon", "--version"])
        if rc != 0:
            return HotspotTarget(
                component_id=None,
                adapter_name=None,
                description=None,
                is_direct=False,
                confidence="none",
                message=f"pktmon utility unavailable or requires Administrator privileges: {err.strip()}",
            )
        return HotspotTarget(
            component_id=None,
            adapter_name=None,
            description=None,
            is_direct=False,
            confidence="none",
            message="No pktmon components enumerated. Please run with Administrator privileges.",
        )

    # 1. If user provided a custom ID, verify it
    if custom_id is not None:
        matched = next((c for c in components if c.component_id == custom_id), None)
        if matched:
            return HotspotTarget(
                component_id=matched.component_id,
                adapter_name=matched.name,
                description=matched.description,
                is_direct="direct" in (matched.name + matched.description).lower(),
                confidence="high",
                message=f"User-specified component {custom_id} verified: {matched.name}",
            )
        else:
            return HotspotTarget(
                component_id=custom_id,
                adapter_name=None,
                description=None,
                is_direct=False,
                confidence="fallback",
                message=f"Component ID {custom_id} was requested but not found in pktmon component list.",
            )

    # 2. Search for Wi-Fi Direct Virtual Adapter
    keywords = ["wi-fi direct", "wifi direct", "direct virtual adapter", "hosted network", "mobile hotspot"]
    for c in components:
        text = (c.name + " " + c.description).lower()
        if any(kw in text for kw in keywords):
            return HotspotTarget(
                component_id=c.component_id,
                adapter_name=c.name,
                description=c.description,
                is_direct=True,
                confidence="high",
                message=f"Discovered Wi-Fi Direct Hotspot component {c.component_id}: {c.name}",
            )

    # 3. Check for general Wi-Fi / WLAN adapter
    wifi_keywords = ["wi-fi", "wireless", "wlan", "802.11"]
    for c in components:
        text = (c.name + " " + c.description).lower()
        if any(kw in text for kw in wifi_keywords) and "virtual" in text:
            return HotspotTarget(
                component_id=c.component_id,
                adapter_name=c.name,
                description=c.description,
                is_direct=True,
                confidence="medium",
                message=f"Discovered wireless virtual component {c.component_id}: {c.name}",
            )

    # 4. Check if component 67 happens to exist on this specific machine
    c67 = next((c for c in components if c.component_id == 67), None)
    if c67:
        return HotspotTarget(
            component_id=67,
            adapter_name=c67.name,
            description=c67.description,
            is_direct="direct" in (c67.name + c67.description).lower(),
            confidence="medium",
            message=f"Component 67 detected on this machine: {c67.name}",
        )

    # 5. Fallback: first available adapter
    first_adapter = next((c for c in components if c.component_type == "Adapter"), components[0])
    return HotspotTarget(
        component_id=first_adapter.component_id,
        adapter_name=first_adapter.name,
        description=first_adapter.description,
        is_direct=False,
        confidence="fallback",
        message=(
            f"No specific Wi-Fi Direct component found. Falling back to component "
            f"{first_adapter.component_id} ({first_adapter.name}). "
            f"Note: capturing on physical adapter may not expose forwarded hotspot client traffic."
        ),
    )
