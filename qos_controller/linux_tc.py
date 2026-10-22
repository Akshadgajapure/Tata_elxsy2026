"""Linux tc enforcement backend: HTB classes + leaf qdisc + u32 source filters.

Packet path (egress of the server-facing router interface):

    packet -> u32 filter (source address -> class) -> HTB class 1:xx
           -> leaf qdisc (fq_codel, or pfifo if the kernel lacks it) -> NIC

Design rules
* Pure functions (validate / render / parse) are separate from execution, so
  they are unit-tested without root.
* Every number placed in a tc command is an int we computed; every address is
  parsed with ``ipaddress``; the interface name must match a strict allowlist.
  Nothing from a browser or a model is ever interpolated into a command.
* A plan is VALIDATED before touching the kernel, VERIFIED by reading tc back
  afterwards, and on any failure the last known-good plan is restored (or the
  shaper is removed, which is the safe fallback: unshaped, never blackholed).
* Only IPv4 and IPv6 *source address* mapping is implemented.  The ``fw``
  classifier is not used (it is absent from some kernels, including the one
  this was developed on).
"""

from __future__ import annotations

import ipaddress
import logging
import math
import re
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping

from . import config as cfg
from .enforcement import EnforcementBackend, EnforcementError
from .models import Policy, TrafficClass
from .netns import CommandError, CommandResult, run_in_ns

log = logging.getLogger(__name__)

ROOT_HANDLE = "1:"
PARENT_CLASS = "1:1"
# tc parses class ids as hex; these are fixed and never user supplied.
CLASS_IDS: dict[TrafficClass, str] = {
    TrafficClass.REAL_TIME: "1:10", TrafficClass.INTERACTIVE: "1:20",
    TrafficClass.ADAPTIVE_VIDEO: "1:30", TrafficClass.BULK: "1:40",
    TrafficClass.BACKGROUND: "1:50", TrafficClass.UNKNOWN: "1:60",
}
HTB_PRIO: dict[TrafficClass, int] = {c: i for i, c in enumerate(CLASS_IDS)}  # lower borrows first
DEFAULT_CLASS = TrafficClass.UNKNOWN
MIN_RATE_KBIT = 8
MAX_MAPPINGS = 64
_IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
FILTER_PRIO = {4: 10, 6: 11}


class TcPlanError(ValueError):
    """The plan is unsafe or malformed; nothing was applied."""


@dataclass(frozen=True)
class TcClassRate:
    rate_kbit: int       # HTB assured rate (the allocation)
    ceil_kbit: int       # HTB ceiling (how far the class may borrow)


@dataclass(frozen=True)
class TcPlan:
    capacity_kbit: int
    classes: Mapping[TrafficClass, TcClassRate]
    mappings: tuple[tuple[str, TrafficClass], ...] = ()   # (source network, class)
    label: str = ""

    def with_mappings(self, mappings) -> "TcPlan":
        return replace(self, mappings=tuple(mappings))


def _kbit(mbps: float) -> int:
    return int(math.floor(mbps * 1000.0 + 1e-9))


def plan_from_rates(capacity_mbps: float,
                    rates: Mapping[TrafficClass, tuple[float, float]],
                    mappings=(), label: str = "") -> TcPlan:
    """rates: class -> (assured_mbps, ceil_mbps)."""
    return TcPlan(_kbit(capacity_mbps),
                  {c: TcClassRate(max(MIN_RATE_KBIT, _kbit(r)), _kbit(ce)) for c, (r, ce) in rates.items()},
                  tuple(mappings), label)


def plan_from_policy(policy: Policy, mappings=()) -> TcPlan:
    """Translate policy into HTB class rates and ceilings.
    
    Assured rate (rate) incorporates:
      1. Minimum reservation percentage (floor guarantee).
      2. Weighted proportional distribution of contested surplus bandwidth,
         ensuring that classes with higher weights (e.g. boosted by user intent)
         genuinely receive higher assured rates in the kernel shaper.
    Ceiling (ceil) is bounded by maximum percentage of capacity.
    """
    cap = policy.capacity_mbps
    
    # Calculate baseline minimum guarantees
    base_mins = {c: cap * (p.minimum_percent / 100.0) for c, p in policy.classes.items()}
    total_min = sum(base_mins.values())
    surplus = max(0.0, cap - total_min)
    
    # Weight-proportional sharing of surplus
    total_weight = sum(p.weight for p in policy.classes.values()) or 1.0
    
    rates: dict[TrafficClass, tuple[float, float]] = {}
    for c, p in policy.classes.items():
        weight_share = surplus * (p.weight / total_weight) if total_weight > 0 else 0.0
        max_rate = cap * (p.maximum_percent / 100.0)
        # Assured rate is min guarantee plus weight share, capped at max_rate
        assured = min(max_rate, base_mins[c] + weight_share)
        ceil = max_rate
        rates[c] = (assured, ceil)

    # If the sum of assured exceeds capacity due to rounding/caps, scale down proportionally
    sum_assured = sum(r[0] for r in rates.values())
    if sum_assured > cap and sum_assured > 0:
        factor = cap / sum_assured
        rates = {c: (r * factor, ce) for c, (r, ce) in rates.items()}

    return plan_from_rates(cap, rates, mappings, "adaptive-policy")


def _network(text: str):
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise TcPlanError(f"invalid address {text!r}") from exc
    return net


def validate_plan(plan: TcPlan) -> None:
    """Reject anything unsafe BEFORE it reaches the kernel."""
    if plan.capacity_kbit < MIN_RATE_KBIT * len(CLASS_IDS):
        raise TcPlanError("capacity too small")
    missing = [c.value for c in CLASS_IDS if c not in plan.classes]
    if missing:
        raise TcPlanError(f"every class must be present (starvation protection); missing {missing}")
    total = 0
    for c, r in plan.classes.items():
        if c not in CLASS_IDS:
            raise TcPlanError(f"unknown class {c!r}")
        if r.rate_kbit < MIN_RATE_KBIT:
            raise TcPlanError(f"{c.value}: rate below floor of {MIN_RATE_KBIT} kbit")
        if r.ceil_kbit < r.rate_kbit:
            raise TcPlanError(f"{c.value}: ceil below rate")
        if r.ceil_kbit > plan.capacity_kbit:
            raise TcPlanError(f"{c.value}: ceil above capacity")
        total += r.rate_kbit
    if total > plan.capacity_kbit:
        raise TcPlanError(f"assured rates {total} kbit exceed capacity {plan.capacity_kbit} kbit")
    if len(plan.mappings) > MAX_MAPPINGS:
        raise TcPlanError("too many mappings")
    seen = set()
    for src, cls in plan.mappings:
        if cls not in CLASS_IDS:
            raise TcPlanError(f"mapping to unknown class {cls!r}")
        net = _network(src)
        if net in seen:
            raise TcPlanError(f"duplicate mapping for {src}")
        seen.add(net)


# ---------------------------------------------------------------- rendering
def _check_iface(iface: str) -> str:
    if not _IFACE_RE.match(iface):
        raise TcPlanError(f"invalid interface name {iface!r}")
    return iface


def leaf_command(leaf: str, queue_limit: int) -> str:
    if leaf == "fq_codel":
        return "fq_codel"
    if leaf == "pfifo":
        return f"pfifo limit {int(queue_limit)}"
    raise TcPlanError(f"unsupported leaf qdisc {leaf!r}")


def render_class_commands(iface: str, plan: TcPlan, verb: str) -> list[str]:
    """verb 'add' for a fresh tree, 'change' to update rates in place."""
    d = f"dev {_check_iface(iface)}"
    cmds = [f"class {verb} {d} parent {ROOT_HANDLE} classid {PARENT_CLASS} htb "
            f"rate {plan.capacity_kbit}kbit ceil {plan.capacity_kbit}kbit"]
    for cls, cid in CLASS_IDS.items():
        r = plan.classes[cls]
        cmds.append(f"class {verb} {d} parent {PARENT_CLASS} classid {cid} htb "
                    f"rate {r.rate_kbit}kbit ceil {r.ceil_kbit}kbit prio {HTB_PRIO[cls]} quantum 1514")
    return cmds


def render_leaf_commands(iface: str, leaf: str, queue_limit: int) -> list[str]:
    d, lc = f"dev {_check_iface(iface)}", leaf_command(leaf, queue_limit)
    return [f"qdisc add {d} parent {cid} handle {cid.split(':')[1]}: {lc}" for cid in CLASS_IDS.values()]


def render_filter_commands(iface: str, plan: TcPlan) -> list[str]:
    d = f"dev {_check_iface(iface)}"
    cmds = []
    for src, cls in plan.mappings:
        net = _network(src)
        prio = FILTER_PRIO[net.version]
        if net.version == 4:
            cmds.append(f"filter add {d} parent {ROOT_HANDLE} protocol ip prio {prio} u32 "
                        f"match ip src {net} flowid {CLASS_IDS[cls]}")
        else:
            cmds.append(f"filter add {d} parent {ROOT_HANDLE} protocol ipv6 prio {prio} u32 "
                        f"match ip6 src {net} flowid {CLASS_IDS[cls]}")
    return cmds


def render_full_install(iface: str, plan: TcPlan, leaf: str, queue_limit: int) -> list[str]:
    d = f"dev {_check_iface(iface)}"
    return ([f"qdisc add {d} root handle {ROOT_HANDLE} htb default {CLASS_IDS[DEFAULT_CLASS].split(':')[1]}"]
            + render_class_commands(iface, plan, "add")
            + render_leaf_commands(iface, leaf, queue_limit)
            + render_filter_commands(iface, plan))


# ------------------------------------------------------------------ parsing
_UNIT = {"bit": 1.0, "Kbit": 1e3, "Mbit": 1e6, "Gbit": 1e9}
_RATE = re.compile(r"([\d.]+)(Gbit|Mbit|Kbit|bit)")


def _bps(text: str) -> float:
    m = _RATE.fullmatch(text)
    return float(m.group(1)) * _UNIT[m.group(2)] if m else float("nan")


@dataclass(frozen=True)
class ClassCounters:
    rate_bps: float
    ceil_bps: float
    sent_bytes: int = 0
    sent_packets: int = 0
    dropped: int = 0
    overlimits: int = 0
    backlog_bytes: int = 0
    backlog_packets: int = 0


_CLASS_HDR = re.compile(r"^class htb (\d+:[0-9a-f]+)\b.*?\brate (\S+) ceil (\S+)", re.M)
_SENT = re.compile(r"Sent (\d+) bytes (\d+) pkt \(dropped (\d+), overlimits (\d+)")
_BACKLOG = re.compile(r"backlog (\d+)b (\d+)p")


def parse_class_show(text: str) -> dict[str, ClassCounters]:
    out: dict[str, ClassCounters] = {}
    for block in re.split(r"(?m)^(?=class htb )", text):
        h = _CLASS_HDR.search(block)
        if not h:
            continue
        s, b = _SENT.search(block), _BACKLOG.search(block)
        out[h.group(1)] = ClassCounters(
            _bps(h.group(2)), _bps(h.group(3)),
            int(s.group(1)) if s else 0, int(s.group(2)) if s else 0,
            int(s.group(3)) if s else 0, int(s.group(4)) if s else 0,
            int(b.group(1)) if b else 0, int(b.group(2)) if b else 0)
    return out


_QDISC_HDR = re.compile(r"^qdisc (\S+) (\d+:) parent (\d+:[0-9a-f]+)", re.M)


def parse_leaf_drops(text: str) -> dict[str, int]:
    """Leaf-qdisc drops keyed by parent class id (where AQM / tail drops are counted)."""
    out: dict[str, int] = {}
    for block in re.split(r"(?m)^(?=qdisc )", text):
        h = _QDISC_HDR.search(block)
        s = _SENT.search(block)
        if h and s:
            out[h.group(3)] = int(s.group(3))
    return out


def parse_filter_show(text: str) -> dict[str, str]:
    """Return {source address as hex: flowid} for u32 source-address filters."""
    out: dict[str, str] = {}
    for block in re.split(r"(?m)^(?=filter )", text):
        flow = re.search(r"flowid (\d+:[0-9a-f]+)", block)
        if not flow:
            continue
        is6 = "protocol ipv6" in block
        words = {int(off): val for val, off in re.findall(r"match ([0-9a-f]{8})/[0-9a-f]{8} at (\d+)", block)}
        if is6 and all(o in words for o in (8, 12, 16, 20)):
            out["".join(words[o] for o in (8, 12, 16, 20))] = flow.group(1)
        elif not is6 and 12 in words:
            out[words[12]] = flow.group(1)
    return out


def _addr_hex(src: str) -> str:
    return _network(src).network_address.packed.hex()


# ---------------------------------------------------------------- execution
@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    problems: tuple[str, ...] = ()


Runner = Callable[..., CommandResult]


@dataclass
class TcEvent:
    action: str
    ok: bool
    detail: str


class LinuxTCBackend(EnforcementBackend):
    """Enforces a TcPlan on one interface inside one namespace."""

    def __init__(self, namespace: str = cfg.ROUTER_NS, iface: str = cfg.TC_IFACE,
                 leaf: str = cfg.TC_LEAF_QDISC, queue_limit: int = cfg.TC_QUEUE_LIMIT_PACKETS,
                 runner: Runner = run_in_ns, timeout: float = 5.0) -> None:
        self.namespace, self.iface = namespace, _check_iface(iface)
        self.leaf, self.queue_limit = leaf, queue_limit
        self.leaf_fallback = False
        self._run, self._timeout = runner, timeout
        self.active_plan: TcPlan | None = None      # what is installed now
        self.known_good: TcPlan | None = None       # last plan that verified OK
        self.previous_good: TcPlan | None = None
        self.default_mappings: tuple[tuple[str, TrafficClass], ...] = (
            ("10.10.1.10/32", TrafficClass.INTERACTIVE),      # gaming client
            ("10.10.2.10/32", TrafficClass.REAL_TIME),        # call client
            ("10.10.3.10/32", TrafficClass.ADAPTIVE_VIDEO),   # stream client
            ("10.10.4.10/32", TrafficClass.BULK),             # bulk download client
        )
        self.events: list[TcEvent] = []
        self._installed_leaf: str | None = None

    # -- low level -------------------------------------------------------
    def _tc(self, *args: str, check: bool = True) -> CommandResult:
        try:
            res = self._run(self.namespace, ["tc", *args], timeout=self._timeout)
        except CommandError as exc:
            raise EnforcementError(str(exc)) from exc
        if check and res.returncode != 0:
            raise EnforcementError(f"tc {' '.join(args[:3])} failed: {res.stderr.strip() or res.returncode}")
        return res

    def _batch(self, lines: list[str]) -> None:
        try:
            res = self._run(self.namespace, ["tc", "-batch", "-"], timeout=self._timeout,
                            input="\n".join(lines) + "\n")
        except CommandError as exc:
            raise EnforcementError(str(exc)) from exc
        if res.returncode != 0:
            raise EnforcementError(f"tc batch failed: {res.stderr.strip() or res.returncode}")

    def _record(self, action: str, ok: bool, detail: str) -> None:
        self.events.append(TcEvent(action, ok, detail))
        del self.events[:-50]
        log.info("tc %s ok=%s %s", action, ok, detail)

    # -- install / update -----------------------------------------------
    def _full_install(self, plan: TcPlan) -> None:
        self._tc("qdisc", "del", "dev", self.iface, "root", check=False)   # may not exist
        try:
            self._batch(render_full_install(self.iface, plan, self.leaf, self.queue_limit))
        except EnforcementError as exc:
            if self.leaf == "fq_codel" and "kind is unknown" in str(exc):
                log.warning("fq_codel not available in this kernel; falling back to pfifo")
                self.leaf, self.leaf_fallback = "pfifo", True
                self._tc("qdisc", "del", "dev", self.iface, "root", check=False)
                self._batch(render_full_install(self.iface, plan, self.leaf, self.queue_limit))
            else:
                raise
        self._installed_leaf = self.leaf

    def _incremental(self, plan: TcPlan) -> None:
        self._batch(render_class_commands(self.iface, plan, "change"))
        if tuple(plan.mappings) != tuple(self.active_plan.mappings):
            self._tc("filter", "del", "dev", self.iface, "parent", ROOT_HANDLE, check=False)
            filters = render_filter_commands(self.iface, plan)
            if filters:
                self._batch(filters)

    def apply_plan(self, plan: TcPlan) -> VerifyResult:
        """validate -> install/update -> verify -> keep, or restore known-good and raise."""
        validate_plan(plan)                                   # TcPlanError: nothing touched
        try:
            if self.active_plan is None or self._installed_leaf != self.leaf:
                self._full_install(plan)
            else:
                self._incremental(plan)
            result = self.verify(plan)
            if not result.ok:
                raise EnforcementError("verification failed: " + "; ".join(result.problems))
        except EnforcementError as exc:
            self._record("apply", False, str(exc))
            self._restore()
            raise
        self.previous_good, self.known_good, self.active_plan = self.known_good, plan, plan
        self._record("apply", True, plan.label or "plan applied")
        return result

    # -- EnforcementBackend interface (static QoS from percentages) ------
    def apply(self, policy: Policy) -> None:
        self.apply_plan(plan_from_policy(policy, self.default_mappings))

    def rollback(self) -> None:
        self._restore()

    @property
    def active_policy(self):   # compatibility with code that reads backend.active_policy
        return self.active_plan

    # -- verify / stats / clear -----------------------------------------
    def verify(self, plan: TcPlan) -> VerifyResult:
        problems: list[str] = []
        classes = parse_class_show(self._tc("class", "show", "dev", self.iface).stdout)
        for cls, cid in CLASS_IDS.items():
            got, want = classes.get(cid), plan.classes[cls]
            if got is None:
                problems.append(f"class {cid} ({cls.value}) missing")
                continue
            for name, g, w in (("rate", got.rate_bps, want.rate_kbit * 1000), ("ceil", got.ceil_bps, want.ceil_kbit * 1000)):
                if not abs(g - w) <= max(0.02 * w, 2000):
                    problems.append(f"{cid} {name} is {g:.0f} bit/s, expected {w}")
        if plan.mappings:
            filt = parse_filter_show(self._tc("filter", "show", "dev", self.iface, "parent", ROOT_HANDLE).stdout)
            for src, cls in plan.mappings:
                if filt.get(_addr_hex(src)) != CLASS_IDS[cls]:
                    problems.append(f"filter for {src} -> {CLASS_IDS[cls]} not found")
        return VerifyResult(not problems, tuple(problems))

    def stats(self) -> dict[TrafficClass, ClassCounters]:
        """Per-class kernel counters (cumulative; callers compute deltas)."""
        classes = parse_class_show(self._tc("-s", "class", "show", "dev", self.iface).stdout)
        drops = parse_leaf_drops(self._tc("-s", "qdisc", "show", "dev", self.iface).stdout)
        out = {}
        for cls, cid in CLASS_IDS.items():
            c = classes.get(cid)
            if c is not None:
                out[cls] = replace(c, dropped=drops.get(cid, c.dropped))
        return out

    def clear(self) -> None:
        """Safe fallback: remove the shaper entirely (traffic flows unshaped)."""
        self._tc("qdisc", "del", "dev", self.iface, "root", check=False)
        self.active_plan, self._installed_leaf = None, None
        self._record("clear", True, "shaper removed")

    def _restore(self) -> None:
        if self.known_good is not None:
            try:
                self._full_install(self.known_good)
                self.active_plan = self.known_good
                self._record("rollback", True, "restored last known-good plan")
                return
            except EnforcementError as exc:
                self._record("rollback", False, f"restore failed: {exc}")
        self.clear()
        self.known_good = None

    @property
    def rollback_target(self):
        """Read-only view of the last-known-good plan rollback() would restore."""
        return self.known_good

    def status(self) -> dict:
        return {"backend": "linux-tc", "namespace": self.namespace, "iface": self.iface,
                "leaf": self.leaf, "leaf_fallback": self.leaf_fallback,
                "installed": self.active_plan is not None,
                "capacity_kbit": self.active_plan.capacity_kbit if self.active_plan else None,
                "shaping_direction": "egress (client->server)",
                "ingress_note": "Linux tc controls egress only. WAN ingress shaping would require IFB redirect.",
                "events": [vars(e) for e in self.events[-10:]]}

    def tc_status(self) -> dict:
        return self.status()
