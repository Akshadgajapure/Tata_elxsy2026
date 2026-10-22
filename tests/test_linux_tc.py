"""LinuxTCBackend: validation, rendering, parsing (real captured tc output),
and backend behaviour against a tiny fake kernel.  No root required.
The real-kernel test is in test_linux_tc_integration.py (opt-in)."""

import re
from dataclasses import replace
from pathlib import Path

import pytest

from qos_controller.enforcement import EnforcementError
from qos_controller.linux_tc import (CLASS_IDS, LinuxTCBackend, TcClassRate, TcPlan, TcPlanError,
                                     parse_class_show, parse_filter_show, parse_leaf_drops,
                                     plan_from_policy, plan_from_rates, render_filter_commands,
                                     render_full_install, validate_plan)
from qos_controller.models import TrafficClass as T
from qos_controller.netns import CommandResult
from qos_controller.policy import PolicyEngine

FIX = Path(__file__).parent / "fixtures"
MAPS = (("10.10.4.10", T.BULK), ("10.10.2.10", T.REAL_TIME), ("fd00:4::10", T.BULK))


def good_plan(cap=20.0, label="p") -> TcPlan:
    base = plan_from_policy(PolicyEngine().build(cap))
    return replace(base, mappings=MAPS, label=label)


# ------------------------------------------------------------- validation
def test_valid_plan_passes() -> None:
    validate_plan(good_plan())


@pytest.mark.parametrize("mutate,msg", [
    (lambda p: replace(p, classes={k: v for k, v in p.classes.items() if k is not T.BULK}), "missing"),
    (lambda p: replace(p, classes={**p.classes, T.BULK: TcClassRate(5, 100)}), "floor"),
    (lambda p: replace(p, classes={**p.classes, T.BULK: TcClassRate(9000, 8000)}), "ceil below rate"),
    (lambda p: replace(p, classes={**p.classes, T.BULK: TcClassRate(1000, 999999)}), "above capacity"),
    (lambda p: replace(p, classes={k: TcClassRate(p.capacity_kbit // 4, p.capacity_kbit) for k in p.classes}), "exceed capacity"),
    (lambda p: replace(p, mappings=(("10.0.0.1; reboot", T.BULK),)), "invalid address"),
    (lambda p: replace(p, mappings=(("10.0.0.1", T.BULK), ("10.0.0.1/32", T.REAL_TIME))), "duplicate"),
])
def test_unsafe_plans_are_rejected(mutate, msg) -> None:
    with pytest.raises(TcPlanError, match=msg):
        validate_plan(mutate(good_plan()))


def test_interface_name_cannot_inject_commands() -> None:
    with pytest.raises(TcPlanError):
        LinuxTCBackend(iface="r-server; reboot")


# -------------------------------------------------------------- rendering
def test_full_install_renders_expected_tree() -> None:
    cmds = render_full_install("r-server", good_plan(), "pfifo", 200)
    assert cmds[0] == "qdisc add dev r-server root handle 1: htb default 60"
    assert sum(c.startswith("class add") for c in cmds) == 7              # root + 6
    assert sum(" pfifo limit 200" in c for c in cmds) == 6
    assert any("protocol ip prio 10 u32 match ip src 10.10.4.10/32 flowid 1:40" in c for c in cmds)
    assert any("protocol ipv6 prio 11 u32 match ip6 src fd00:4::10/128 flowid 1:40" in c for c in cmds)
    assert all(re.fullmatch(r"[a-z0-9 :./-]+", c) for c in cmds)           # nothing exotic ever rendered


def test_every_class_has_a_fixed_hex_id_and_priority() -> None:
    assert list(CLASS_IDS.values()) == ["1:10", "1:20", "1:30", "1:40", "1:50", "1:60"]


# ---------------------------------------------------------------- parsing
def test_parse_real_kernel_output() -> None:
    classes = parse_class_show((FIX / "tc_class_show_s.txt").read_text())
    assert classes["1:40"].rate_bps == 2e6 and classes["1:40"].ceil_bps == 12e6
    assert classes["1:1"].ceil_bps == 20e6
    assert classes["1:40"].sent_bytes > 1_000_000 and classes["1:10"].sent_packets >= 0
    assert set(classes) >= set(CLASS_IDS.values())
    assert set(parse_leaf_drops((FIX / "tc_qdisc_show_s.txt").read_text())) >= {"1:40"}


def test_parse_real_filter_output_v4_and_v6() -> None:
    f = parse_filter_show((FIX / "tc_filter_show.txt").read_text())
    assert f["0a0a040a"] == "1:40" and f["0a0a020a"] == "1:10"
    assert f["fd000004000000000000000000000010"] == "1:40"


# ------------------------------------------------------ fake kernel backend
class FakeKernel:
    """Interprets just enough tc to exercise backend logic."""
    def __init__(self):
        self.classes, self.filters, self.calls, self.installed = {}, {}, [], False
        self.fail_substr, self.fail_times, self.no_fq_codel, self.corrupt_rate = None, 10**9, False, False

    def __call__(self, ns, argv, timeout=2.0, input=None):
        self.calls.append((tuple(argv), input))
        a = argv[1:]
        if a[:2] == ["-batch", "-"]:
            return self._batch(input)
        if a[:3] == ["qdisc", "del", "dev"]:
            self.classes, self.filters, self.installed = {}, {}, False
            return CommandResult(0, "", "")
        if a[:2] == ["filter", "del"]:
            self.filters = {}
            return CommandResult(0, "", "")
        if a[:3] == ["class", "show", "dev"]:
            return CommandResult(0, "".join(
                f"class htb {c} parent 1:1 prio 0 rate {r}kbit ceil {ce}kbit burst 1600b\n"
                for c, (r, ce) in self.classes.items()).replace("kbit", "Kbit"), "")
        if a[:3] == ["filter", "show", "dev"]:
            out = ""
            for h, fid in self.filters.items():
                if len(h) == 8:
                    out += f"filter parent 1: protocol ip pref 10 u32 *flowid {fid}\n  match {h}/ffffffff at 12\n"
                else:
                    out += f"filter parent 1: protocol ipv6 pref 11 u32 *flowid {fid}\n" + "".join(
                        f"  match {h[i * 8:(i + 1) * 8]}/ffffffff at {8 + 4 * i}\n" for i in range(4))
            return CommandResult(0, out, "")
        return CommandResult(0, "", "")

    def _batch(self, text):
        for line in text.strip().splitlines():
            if self.fail_substr and self.fail_times > 0 and self.fail_substr in line:
                self.fail_times -= 1
                return CommandResult(1, "", "Error: simulated failure")
            if self.no_fq_codel and "fq_codel" in line:
                return CommandResult(1, "", "Error: Specified qdisc kind is unknown.")
            m = re.match(r"class (?:add|change) .* classid (\S+) htb rate (\d+)kbit ceil (\d+)kbit", line)
            if m:
                r = int(m.group(2)) // (2 if self.corrupt_rate and m.group(1) == "1:40" else 1)
                self.classes[m.group(1)] = (r, int(m.group(3)))
            m = re.match(r"filter add .* match ip6? src (\S+)/\d+ flowid (\S+)", line)
            if m:
                import ipaddress
                self.filters[ipaddress.ip_address(m.group(1)).packed.hex()] = m.group(2)
            if line.startswith("qdisc add") and "root" in line:
                self.installed = True
        return CommandResult(0, "", "")


@pytest.fixture()
def kb():
    k = FakeKernel()
    return LinuxTCBackend(leaf="pfifo", runner=k), k


def test_apply_installs_verifies_and_records_known_good(kb) -> None:
    b, k = kb
    r = b.apply_plan(good_plan(label="one"))
    assert r.ok and b.known_good.label == "one" and k.installed


def test_second_plan_updates_in_place_without_rebuilding(kb) -> None:
    b, k = kb
    b.apply_plan(good_plan(20.0, "one"))
    n = len(k.calls)
    b.apply_plan(good_plan(30.0, "two"))
    new = [c for c in k.calls[n:] if c[0][1] == "qdisc"]
    assert not new, "capacity change must not delete/rebuild the qdisc tree"
    assert "class change" in k.calls[n][1]
    assert b.known_good.label == "two" and b.previous_good.label == "one"


def test_unsafe_plan_never_touches_kernel(kb) -> None:
    b, k = kb
    with pytest.raises(TcPlanError):
        b.apply_plan(replace(good_plan(), classes={}))
    assert k.calls == []


def test_transient_failure_restores_last_known_good(kb) -> None:
    b, k = kb
    b.apply_plan(good_plan(20.0, "good"))
    k.fail_substr, k.fail_times = "classid 1:30", 1      # an update fails once, midway
    with pytest.raises(EnforcementError):
        b.apply_plan(good_plan(40.0, "bad"))
    assert b.active_plan.label == "good" and b.known_good.label == "good"
    assert k.classes["1:1"][0] == 20000, "kernel state must be back at the good plan"


def test_persistent_failure_falls_back_to_unshaped(kb) -> None:
    b, k = kb
    b.apply_plan(good_plan(20.0, "good"))
    k.fail_substr = "classid 1:30"                       # restore fails too -> safe fallback
    with pytest.raises(EnforcementError):
        b.apply_plan(good_plan(40.0, "bad"))
    assert b.active_plan is None and b.known_good is None and not k.installed
    assert any(e.action == "rollback" and not e.ok for e in b.events)


def test_verification_failure_triggers_rollback(kb) -> None:
    b, k = kb
    b.apply_plan(good_plan(20.0, "good"))
    k.corrupt_rate = True                    # kernel silently ends up with a wrong rate
    with pytest.raises(EnforcementError, match="verification failed"):
        b.apply_plan(good_plan(30.0, "wrong"))
    k.corrupt_rate = False
    assert b.known_good.label == "good" and k.classes["1:1"][1] == 20000


def test_first_plan_failure_falls_back_to_unshaped(kb) -> None:
    b, k = kb
    k.fail_substr = "classid 1:20"
    with pytest.raises(EnforcementError):
        b.apply_plan(good_plan())
    assert b.active_plan is None and not k.installed and k.classes == {}


def test_fq_codel_missing_falls_back_to_pfifo_visibly() -> None:
    k = FakeKernel(); k.no_fq_codel = True
    b = LinuxTCBackend(leaf="fq_codel", runner=k)
    b.apply_plan(good_plan())
    assert b.leaf == "pfifo" and b.leaf_fallback and b.status()["leaf_fallback"] is True


def test_policy_interface_applies_static_qos(kb) -> None:
    b, k = kb
    b.default_mappings = MAPS
    b.apply(PolicyEngine().build(20.0))      # the existing EnforcementBackend contract
    assert k.classes["1:40"] == (2000, 12000) and "0a0a040a" in k.filters


def test_clear_is_safe_fallback(kb) -> None:
    b, k = kb
    b.apply_plan(good_plan())
    b.clear()
    assert b.active_plan is None and not k.installed
