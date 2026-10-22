#!/usr/bin/env bash
# ==============================================================================
# TataElexy — Automated WAN Ingress IFB Redirection Script
#
# Sets up an Intermediate Functional Block (IFB) device to enable true
# bidirectional Linux tc traffic control (Ingress + Egress).
#
# Linux 'tc' traditionally shapes only outbound (egress) packets.
# By redirecting ingress packets to an 'ifb0' pseudo-device, the kernel
# treats them as egress on ifb0, enabling full HTB + FQ-CoDel ingress shaping!
#
# Usage:
#   sudo ./scripts/setup_ingress_ifb.sh <wan_iface> [ifb_name]
# Example:
#   sudo ./scripts/setup_ingress_ifb.sh eth0 ifb0
# ==============================================================================

set -euo pipefail

WAN_IFACE="${1:-eth0}"
IFB_DEV="${2:-ifb0}"

echo "[*] Setting up bidirectional QoS ingress redirect: $WAN_IFACE -> $IFB_DEV"

# 1. Ensure kernel ifb module is loaded
if ! lsmod | grep -q "^ifb"; then
    echo "[*] Loading Linux 'ifb' kernel module..."
    modprobe ifb numifbs=1 2>/dev/null || true
fi

# 2. Create and bring up the IFB interface if not already present
if ! ip link show "$IFB_DEV" >/dev/null 2>&1; then
    echo "[+] Creating virtual interface $IFB_DEV..."
    ip link add name "$IFB_DEV" type ifb
fi
ip link set dev "$IFB_DEV" up

# 3. Attach ingress qdisc on WAN interface
echo "[+] Attaching ingress qdisc to $WAN_IFACE..."
tc qdisc del dev "$WAN_IFACE" ingress 2>/dev/null || true
tc qdisc add dev "$WAN_IFACE" handle ffff: ingress

# 4. Redirect all incoming packets from WAN interface to IFB device egress
echo "[+] Mirroring ingress packets to $IFB_DEV..."
tc filter add dev "$WAN_IFACE" parent ffff: protocol all prio 10 matchall \
   action mirred egress redirect dev "$IFB_DEV"

echo "[✓] Ingress redirection established successfully!"
echo "    Now traffic control policies applied to '$IFB_DEV' shape incoming WAN traffic."
echo "    To clean up: tc qdisc del dev $WAN_IFACE ingress; ip link set dev $IFB_DEV down"
