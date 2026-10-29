#!/bin/bash
#
# Install USB stick auto-export for offline trip + event pull.
#
# Run once on the Pi:
#   sudo bash setup/install-usb-export.sh
#
# Stick preparation (one time per USB drive):
#   1. Format FAT32 / exFAT
#   2. Create empty file at root named: BLINKSMART_EXPORT
#   3. Plug into Pi — after export finishes you get:
#        blinksmart_export_<host>_<deviceId>_<timestamp>/
#          blinksmart.db
#          trips_summary.json
#          events_summary.csv        (one row per event: type, time, GPS, speed)
#          events/
#            Sleeping/<event>/frame_0000.jpg ...
#            Yawning/  PhoneCall/  LookingDown/  LookingAway/  Overspeeding/  NoFace/
#          MANIFEST.json
#          EXPORT_OK                 (EXPORT_IN_PROGRESS while copying)
#
# Events are copied in batches of 25. Each batch is checksum-verified and
# flushed to USB before its source files are deleted from the Pi, so an
# interrupted export keeps its progress. Use --keep-events for copy-only mode.
#
# Manual test (with stick already mounted or by device node):
#   sudo /home/pi/facial-tracker-firmware/venv/bin/python \
#     /home/pi/facial-tracker-firmware/usb_export.py --device /dev/sda1
#
# Logs:
#   journalctl -u 'blinksmart-usb-export@*' -f
#

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run as root: sudo bash $0"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIRMWARE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SYSTEMD_SRC="$FIRMWARE_ROOT/systemd"
UDEV_SRC="$FIRMWARE_ROOT/udev"
SYSTEMD_DEST="/etc/systemd/system"
UDEV_DEST="/etc/udev/rules.d"
UNIT="blinksmart-usb-export@.service"
RULE="99-blinksmart-usb-export.rules"
MOUNT_POINT="/mnt/blinksmart-usb"

echo "=== Install Blinksmart USB export ==="
echo "Firmware root: $FIRMWARE_ROOT"
echo ""

if [ ! -f "$FIRMWARE_ROOT/usb_export.py" ]; then
    echo "ERROR: missing $FIRMWARE_ROOT/usb_export.py"
    exit 1
fi

if [ ! -f "$SYSTEMD_SRC/$UNIT" ]; then
    echo "ERROR: missing $SYSTEMD_SRC/$UNIT"
    exit 1
fi

if [ ! -f "$UDEV_SRC/$RULE" ]; then
    echo "ERROR: missing $UDEV_SRC/$RULE"
    exit 1
fi

mkdir -p "$MOUNT_POINT"
chmod 755 "$MOUNT_POINT"

echo "Copying $UNIT -> $SYSTEMD_DEST/"
cp "$SYSTEMD_SRC/$UNIT" "$SYSTEMD_DEST/$UNIT"

echo "Copying $RULE -> $UDEV_DEST/"
cp "$UDEV_SRC/$RULE" "$UDEV_DEST/$RULE"

echo "Reloading systemd + udev..."
systemctl daemon-reload
udevadm control --reload-rules
udevadm trigger --subsystem-match=block || true

# Template units are not enabled in the traditional sense; udev starts instances.
# Ensure unit file is known:
systemctl cat "blinksmart-usb-export@.service" >/dev/null

echo ""
echo "=== Done ==="
echo "Prepare USB stick: create empty file 'BLINKSMART_EXPORT' at the root."
echo "Then plug it in. Watch logs with:"
echo "  journalctl -u 'blinksmart-usb-export@*' -f"
echo ""
echo "Manual run example:"
echo "  lsblk"
echo "  sudo $FIRMWARE_ROOT/venv/bin/python $FIRMWARE_ROOT/usb_export.py --device /dev/sda1"
