# Third-Party Components & Attributions

This project uses only open-source, permissive-licensed dependencies and native operating system facilities. No paid services, cloud APIs, or proprietary AI models are required.

| Component | Version | License | Use in Project |
|---|---|---|---|
| **Flask** | >=3.0 | BSD-3-Clause | Web application server and REST APIs |
| **Flask-SocketIO** | >=5.3 | MIT | Real-time WebSocket event streaming |
| **simple-websocket** | >=1.0 | MIT | WebSocket protocol transport |
| **Chart.js** | 4.4.7 | MIT | Frontend interactive telemetry charts (vendored) |
| **socket.io-client** | 4.8.1 | MIT | Browser WebSocket client library (vendored) |
| **pytest** | >=8.0 | MIT | Test framework for unit, integration, and property tests |
| **psutil** | >=5.9 | BSD-3-Clause | Optional system CPU/memory telemetry sampling |
| **Windows pktmon** | Built-in | Microsoft OS EULA | Windows Mobile Hotspot packet monitoring (external tool) |
| **iproute2 / tc** | Built-in | GPL-2.0 (external) | Linux network namespaces and traffic control (external tool) |
| **iperf3** | Built-in | BSD-3-Clause (external) | Synthetic lab traffic generation (external tool) |

### Notes
* Fonts (Inter, JetBrains Mono) are referenced by CSS name and fall back cleanly to local system sans-serif and monospace fonts.
* Windows `pktmon` is invoked via bounded subprocesses (`capture.ps1` / `companion.py`) and is not redistributed.
* Raw packet payloads, personal credentials, authentication cookies, and sensitive communications are strictly excluded from storage. Only permitted layer-3/4 header metadata (IP addresses, ports, protocol, length, timestamp) is extracted.
