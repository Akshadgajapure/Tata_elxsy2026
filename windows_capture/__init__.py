"""Windows Mobile Hotspot packet capture companion module.

Provides discovery of network adapters and pktmon components,
safe execution of Windows packet monitor (pktmon), batch capture
workflow, PCAPNG conversion, and ingestion into the TataElexy QoS controller.
"""

from .adapter_discovery import discover_adapters, discover_pktmon_components, find_hotspot_target
from .companion import WindowsCaptureCompanion

__all__ = [
    "discover_adapters",
    "discover_pktmon_components",
    "find_hotspot_target",
    "WindowsCaptureCompanion",
]
