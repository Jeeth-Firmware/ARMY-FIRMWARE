#!/bin/bash
# Configure Raspberry Pi mDNS so the device UI is reachable at:
#   http://copilot.local:5000
#
# Requires avahi-daemon (standard on Raspberry Pi OS).
# Run once on the device: sudo bash setup/copilot-local-hostname.sh

set -euo pipefail

HOSTNAME="${DEVICE_LOCAL_HOST:-copilot}"
HOSTNAME="${HOSTNAME,,}"  # lowercase

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash $0"
    exit 1
fi

echo "Setting hostname to: ${HOSTNAME}"
hostnamectl set-hostname "${HOSTNAME}"

# Keep loopback + LAN name in sync (avahi uses hostname for .local)
if grep -q '127.0.1.1' /etc/hosts; then
    sed -i "s/^127.0.1.1.*/127.0.1.1\t${HOSTNAME}/" /etc/hosts
else
    echo -e "127.0.1.1\t${HOSTNAME}" >> /etc/hosts
fi

if systemctl list-unit-files avahi-daemon.service &>/dev/null; then
    systemctl enable avahi-daemon
    systemctl restart avahi-daemon
    echo "avahi-daemon enabled (publishes ${HOSTNAME}.local)"
else
    echo "WARNING: avahi-daemon not found — install: sudo apt install avahi-daemon"
fi

echo ""
echo "Done. After reboot (recommended), open:"
echo "  http://${HOSTNAME}.local:5000/"
echo ""
echo "On Windows, install 'Bonjour' or use IP if .local does not resolve."
echo "On phone/tablet, stay on the same Wi‑Fi as the Pi."
