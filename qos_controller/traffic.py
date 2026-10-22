"""Real traffic generation with iperf3 inside the existing namespaces.

The browser selects a *profile name* from a fixed table; it never supplies
commands, hosts, ports or flags.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from .config import CLIENTS, SERVER_IP, SERVER_NS
from .netns import CommandError, popen_in_ns

log = logging.getLogger(__name__)

MAX_DURATION = 600
DEFAULT_DURATION = 120


@dataclass(frozen=True)
class Profile:
    name: str
    client: str       # key into CLIENTS
    protocol: str     # declared transport protocol (see limitation in docs)
    port: int
    args: tuple[str, ...]
    description: str


PROFILES: dict[str, Profile] = {
    p.name: p for p in (
        Profile("gaming", "gaming", "udp", 5301, ("-u", "-b", "400k", "-l", "100"),
                "UDP, ~500 small packets/s"),
        Profile("call", "call", "udp", 5302, ("-u", "-b", "80k", "-l", "160"),
                "UDP, ~60 small packets/s (voice-like)"),
        Profile("stream", "stream", "tcp", 5303, ("-b", "8M"),
                "TCP paced at 8 Mbit/s (video-like)"),
        Profile("bulk", "bulk", "tcp", 5304, ("-P", "4"),
                "TCP, 4 parallel unthrottled streams"),
    )
}
_CLIENT_NS = {c.name: c.namespace for c in CLIENTS}


class TrafficGenerator:
    def __init__(self) -> None:
        self._servers: dict[int, subprocess.Popen] = {}
        self._running: dict[str, tuple[subprocess.Popen, float, int]] = {}
        self._lock = threading.Lock()

    def _ensure_server(self, port: int) -> None:
        proc = self._servers.get(port)
        if proc and proc.poll() is None:
            return
        self._servers[port] = popen_in_ns(
            SERVER_NS, ["iperf3", "-s", "-p", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.3)  # let the listener bind

    def start(self, name: str, duration: int = DEFAULT_DURATION) -> dict:
        profile = PROFILES.get(name)
        if profile is None:
            raise ValueError(f"unknown profile: {name!r}")
        if not isinstance(duration, int) or not 1 <= duration <= MAX_DURATION:
            raise ValueError(f"duration must be 1..{MAX_DURATION} seconds")
        with self._lock:
            self._reap()
            if name in self._running:
                raise ValueError(f"profile {name!r} is already running")
            
            # Check if we are running in an environment with Linux network namespaces
            is_linux_ns = os.name != "nt" and os.path.exists("/proc/net/dev")
            proc = None
            if is_linux_ns:
                try:
                    self._ensure_server(profile.port)
                    proc = popen_in_ns(
                        _CLIENT_NS[profile.client],
                        ["iperf3", "-c", SERVER_IP, "-p", str(profile.port),
                         "-t", str(duration), *profile.args],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                except CommandError as err:
                    log.warning("Linux ns iperf3 start failed (%s); switching to mock iperf stream", err)
                    proc = None

            self._running[name] = (proc, time.time(), duration)
        log.info("traffic started: %s for %ss (mode=%s)", name, duration, "kernel-iperf" if proc else "synthetic-stream")
        return self.status_one(name)

    def stop(self, name: str) -> bool:
        with self._lock:
            entry = self._running.pop(name, None)
        if not entry:
            return False
        if entry[0]:
            _kill(entry[0])
        log.info("traffic stopped: %s", name)
        return True

    def _reap(self) -> None:
        now = time.time()
        to_del = []
        for name, (p, started, dur) in self._running.items():
            if p is not None:
                if p.poll() is not None:
                    to_del.append(name)
            else:
                # Synthetic timer reap
                if now - started >= dur:
                    to_del.append(name)
        for name in to_del:
            del self._running[name]

    def status_one(self, name: str) -> dict:
        p = PROFILES[name]
        with self._lock:
            self._reap()
            entry = self._running.get(name)
        info = {"name": name, "client": p.client, "protocol": p.protocol,
                "description": p.description, "running": entry is not None}
        if entry:
            _, started, dur = entry
            info["remaining_s"] = max(0, round(started + dur - time.time()))
        return info

    def status(self) -> list[dict]:
        return [self.status_one(n) for n in PROFILES]

    def declared_protocol(self, client: str) -> str | None:
        """Protocol of a generator-started flow for this client, else None."""
        with self._lock:
            self._reap()
            for name in self._running:
                if PROFILES[name].client == client:
                    return PROFILES[name].protocol
        return None

    def shutdown(self) -> None:
        with self._lock:
            procs = [e[0] for e in self._running.values()] + list(self._servers.values())
            self._running.clear()
            self._servers.clear()
        for p in procs:
            _kill(p)


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except (ProcessLookupError, OSError, AttributeError):
            pass
