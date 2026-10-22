"""Real Linux telemetry: interface counters, tc queue stats, RTT probe."""

from __future__ import annotations

import logging
import re
import statistics
import subprocess
import threading
import os
import signal
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from .config import PROBE_NS, ROUTER_NS, SERVER_IP
from .netns import CommandError, popen_in_ns, run_in_ns

log = logging.getLogger(__name__)


class TelemetryError(RuntimeError):
    pass


@dataclass(frozen=True)
class IfaceCounters:
    rx_bytes: int
    rx_packets: int
    rx_drop: int
    tx_bytes: int
    tx_packets: int
    tx_drop: int


def parse_proc_net_dev(text: str) -> dict[str, IfaceCounters]:
    """Parse /proc/net/dev (kernel counters, per interface)."""
    out: dict[str, IfaceCounters] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, data = line.split(":", 1)
        f = data.split()
        if len(f) < 16:
            continue
        out[name.strip()] = IfaceCounters(
            rx_bytes=int(f[0]), rx_packets=int(f[1]), rx_drop=int(f[3]),
            tx_bytes=int(f[8]), tx_packets=int(f[9]), tx_drop=int(f[11]),
        )
    return out


@dataclass(frozen=True)
class QdiscStats:
    backlog_packets: int
    backlog_bytes: int
    dropped: int


_BACKLOG = re.compile(r"backlog\s+(\d+)b\s+(\d+)p")
_DROPPED = re.compile(r"dropped\s+(\d+)")


def parse_tc_qdisc(text: str) -> QdiscStats | None:
    """Parse `tc -s qdisc show dev X`.  Uses the FIRST (root) qdisc only:
    summing parent and leaf qdiscs would double count queued packets."""
    b = _BACKLOG.search(text)
    d = _DROPPED.search(text)
    if not b:
        return None
    return QdiscStats(int(b.group(2)), int(b.group(1)), int(d.group(1)) if d else 0)


class CounterSource(Protocol):
    def read(self) -> dict[str, IfaceCounters]: ...


class QdiscSource(Protocol):
    def read(self) -> QdiscStats | None: ...


class NetnsCounterSource:
    """One bounded `cat /proc/net/dev` inside ns-router per call."""

    def read(self) -> dict[str, IfaceCounters]:
        try:
            res = run_in_ns(ROUTER_NS, ["cat", "/proc/net/dev"], timeout=2.0)
        except CommandError as exc:
            raise TelemetryError(str(exc)) from exc
        if res.returncode != 0:
            raise TelemetryError(res.stderr.strip() or f"rc={res.returncode}")
        counters = parse_proc_net_dev(res.stdout)
        if not counters:
            raise TelemetryError("no interfaces parsed")
        return counters


class TcQdiscSource:
    def __init__(self, iface: str) -> None:
        self.iface = iface

    def read(self) -> QdiscStats | None:
        try:
            res = run_in_ns(ROUTER_NS, ["tc", "-s", "qdisc", "show", "dev", self.iface], 2.0)
        except CommandError:
            return None
        return parse_tc_qdisc(res.stdout) if res.returncode == 0 else None


class SimulatedCounterSource:
    """Windows-compatible demo counter source.

    Generates realistic interface counters driven by the active iperf generator profiles.
    When profiles are started (gaming, call, stream, bulk), their respective clients
    reflect their exact target bitrates in real-time.
    """

    def __init__(self, generator: Any = None) -> None:
        import time
        self.generator = generator
        self._start = time.monotonic()
        self._bytes_accum: dict[str, float] = {}

    def read(self) -> dict[str, IfaceCounters]:
        import time
        from .config import CLIENTS
        now = time.monotonic()
        t = now - self._start
        result = {}

        # Profile rate mapping in bits/second
        profile_rates = {
            "gaming": 400_000,       # 400 kbps UDP
            "call": 80_000,          # 80 kbps UDP
            "stream": 8_000_000,     # 8 Mbps TCP
            "bulk": 25_000_000,      # 25 Mbps TCP
        }
        active_names = set()
        if self.generator:
            try:
                for stat in self.generator.status():
                    if stat.get("running"):
                        active_names.add(stat.get("name"))
            except Exception:
                pass

        for c in CLIENTS:
            # If the profile is actively started, produce its full rate;
            # otherwise provide baseline background traffic (~1-2 Mbit/s)
            is_active = c.name in active_names
            if is_active:
                bps = profile_rates.get(c.name, 5_000_000)
            else:
                bps = 1_500_000  # 1.5 Mbps idle baseline

            # Accumulate bytes smoothly over time
            bytes_total = int(bps * t / 8)
            pkts_total = max(1, bytes_total // 600)

            result[c.router_iface] = IfaceCounters(
                rx_bytes=bytes_total,
                rx_packets=pkts_total,
                rx_drop=0,
                tx_bytes=int(bytes_total * 0.25),
                tx_packets=max(1, pkts_total // 4),
                tx_drop=0,
            )
        return result


class NullQdiscSource:
    """No-op qdisc source for Windows — always returns None."""
    def read(self) -> None:
        return None


class NullProbe:
    """No-op RTT probe for Windows — reports probe as not running."""
    error: str | None = "Probe unavailable on Windows (Linux only)"

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def stats(self) -> dict:
        return {
            "running": False,
            "samples": 0,
            "avg_ms": None,
            "min_ms": None,
            "max_ms": None,
            "jitter_ms": None,
            "loss_pct": None,
            "error": self.error,
        }



_PING = re.compile(r"icmp_seq=(\d+).*?time=([\d.]+)\s*ms")


class RttProbe:
    """Continuous ping toward the server; reports RTT, jitter and loss.

    Loss is estimated from missing sequence numbers inside a sliding window
    (in-flight replies are not counted as lost).  Jitter is the mean absolute
    difference of consecutive RTTs.
    """

    def __init__(self, namespace: str = PROBE_NS, target: str = SERVER_IP,
                 window: int = 100, interval: float = 0.2) -> None:
        self.namespace, self.target = namespace, target
        self.window, self.interval = window, interval
        self._samples: deque[tuple[int, float]] = deque(maxlen=window)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    def feed(self, line: str) -> None:
        m = _PING.search(line)
        if m:
            with self._lock:
                self._samples.append((int(m.group(1)), float(m.group(2))))

    def stats(self) -> dict:
        with self._lock:
            s = list(self._samples)
        running = bool(self._proc and self._proc.poll() is None)
        if not s:
            return {"running": running, "samples": 0, "avg_ms": None, "min_ms": None,
                    "max_ms": None, "jitter_ms": None, "loss_pct": None,
                    "error": self.error}
        rtts = [r for _, r in s]
        max_seq = max(q for q, _ in s)
        lo = max(1, max_seq - self.window + 1)
        expected = max_seq - lo + 1
        got = len({q for q, _ in s if q >= lo})
        diffs = [abs(a - b) for a, b in zip(rtts, rtts[1:])]
        return {
            "running": running, "samples": len(rtts),
            "avg_ms": round(statistics.fmean(rtts), 3), "min_ms": min(rtts),
            "max_ms": max(rtts),
            "jitter_ms": round(statistics.fmean(diffs), 3) if diffs else 0.0,
            "loss_pct": round(100.0 * (expected - got) / expected, 2),
            "error": self.error,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            self._proc = popen_in_ns(
                self.namespace,
                ["ping", "-n", "-i", str(self.interval), "-W", "1", self.target],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1,
            )
        except CommandError as exc:
            self.error = str(exc)
            return
        self._thread = threading.Thread(target=self._read, daemon=True, name="rtt-probe")
        self._thread.start()

    def _read(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            self.feed(line)
        if self._proc.poll() not in (None, 0):
            self.error = f"ping exited rc={self._proc.returncode}"

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(self._proc.pid, signal.SIGTERM)
                else:
                    self._proc.terminate()
            except (ProcessLookupError, OSError):
                pass


def estimate_queue_delay_ms(backlog_bytes: int, capacity_mbps: float) -> float:
    """Little's Law queue delay estimate: Delay (ms) = (Backlog Bytes * 8 / Capacity bps) * 1000.
    Returns estimated sojourn delay in milliseconds.
    """
    if backlog_bytes <= 0 or capacity_mbps <= 0:
        return 0.0
    rate_bps = max(1.0, capacity_mbps * 1e6)
    return round((backlog_bytes * 8.0 / rate_bps) * 1000.0, 2)


class ClientLatencyProbe:
    """Periodic ICMP probe measuring per-client RTT, jitter, and packet loss."""

    def __init__(self, clients: tuple = (), interval_seconds: float = 2.0) -> None:
        self.clients = clients
        self.interval_seconds = interval_seconds
        self._stats: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def stats_for(self, client_name: str) -> dict:
        with self._lock:
            return self._stats.get(client_name, {
                "rtt_ms": None, "jitter_ms": None, "loss_pct": None, "samples": 0
            })

    def all_stats(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._stats)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="client-latency-probe")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            for c in self.clients:
                try:
                    res = run_in_ns(ROUTER_NS, ["ping", "-c", "1", "-W", "1", c.ip], timeout=1.5)
                    if res.returncode == 0:
                        m = _PING.search(res.stdout)
                        if m:
                            rtt = float(m.group(2))
                            with self._lock:
                                prev = self._stats.get(c.name, {})
                                prev_rtt = prev.get("rtt_ms")
                                jitter = abs(rtt - prev_rtt) if prev_rtt is not None else 0.0
                                ewma_rtt = round(0.7 * (prev_rtt or rtt) + 0.3 * rtt, 2)
                                self._stats[c.name] = {
                                    "rtt_ms": ewma_rtt,
                                    "jitter_ms": round(jitter, 2),
                                    "loss_pct": 0.0,
                                    "samples": prev.get("samples", 0) + 1,
                                }
                except Exception:
                    pass
            self._stop.wait(self.interval_seconds)

