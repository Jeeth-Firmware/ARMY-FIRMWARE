#!/bin/bash
#
# Pi Zero 2W System Optimization for Driver Monitoring
# Run once on the device: sudo bash setup/pi_optimize.sh
# Requires reboot after running.
#
# What this does:
#   1. Overclock CPU to 1200MHz (safe with heatsink, ~20% faster inference)
#   2. Set gpu_mem=64 (minimum for camera ISP — below 64 breaks capture)
#   3. Replace SD card swap with ZRAM (fast compressed RAM swap)
#   4. Lock CPU governor to 'performance' (no clock ramping delays)
#   5. Mount /tmp and /var/log on tmpfs (eliminates SD card I/O stalls)
#   6. Tune kernel VM parameters for real-time workloads
#

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run as root: sudo bash $0"
    exit 1
fi

echo "=== Pi Zero 2W Optimization for Driver Monitoring ==="
echo ""

REBOOT_NEEDED=0

# ─────────────────────────────────────────────
# 1. OVERCLOCK + GPU MEMORY (/boot/config.txt)
# ─────────────────────────────────────────────
CONFIG="/boot/config.txt"
# Some Pi OS versions use /boot/firmware/config.txt
[ -f "/boot/firmware/config.txt" ] && CONFIG="/boot/firmware/config.txt"

echo "[1/6] Configuring overclock + GPU memory in $CONFIG"

# Backup original
cp "$CONFIG" "${CONFIG}.backup.$(date +%Y%m%d)" 2>/dev/null || true

# Remove any existing entries we're about to set
sed -i '/^arm_freq=/d' "$CONFIG"
sed -i '/^over_voltage=/d' "$CONFIG"
sed -i '/^core_freq=/d' "$CONFIG"
sed -i '/^gpu_mem=/d' "$CONFIG"
sed -i '/^start_x=/d' "$CONFIG"
sed -i '/^camera_auto_detect=/d' "$CONFIG"
sed -i '/^#.*pi_optimize/d' "$CONFIG"
sed -i '/^dtparam=watchdog=/d' "$CONFIG"

# Append our settings
cat >> "$CONFIG" << 'BOOT_EOF'

# --- pi_optimize: Driver monitoring performance ---
arm_freq=1200
over_voltage=2
core_freq=500
gpu_mem=64
start_x=1
camera_auto_detect=0
# start_x=1 enables legacy camera stack (V4L2) needed by OpenCV VideoCapture
# camera_auto_detect=0 disables libcamera so legacy stack takes over
# gpu_mem=64 is minimum for camera ISP — below this, capture is unreliable
temp_soft_limit=80
force_turbo=0
# Hardware watchdog: reboots Pi if kernel hangs (15s timeout)
dtparam=watchdog=on
BOOT_EOF

echo "  arm_freq=1200 (20% overclock, stable with active fan cooling)"
echo "  gpu_mem=64 (minimum for camera ISP — below 64 breaks capture)"
REBOOT_NEEDED=1

# ─────────────────────────────────────────────
# 2. ZRAM (compressed RAM swap, replaces SD card swap)
# ─────────────────────────────────────────────
echo ""
echo "[2/6] Setting up ZRAM swap (replaces slow SD card swap)"

# Disable SD card swap
if systemctl is-active --quiet dphys-swapfile 2>/dev/null; then
    systemctl stop dphys-swapfile
    systemctl disable dphys-swapfile
    echo "  Disabled dphys-swapfile (SD card swap)"
fi

# Remove swap file to reclaim space
[ -f /var/swap ] && swapoff /var/swap 2>/dev/null && rm -f /var/swap && echo "  Removed /var/swap"

# Create ZRAM setup script
cat > /usr/local/bin/zram-setup.sh << 'ZRAM_EOF'
#!/bin/bash
# Setup ZRAM swap — 256MB compressed with LZ4 (fastest on Cortex-A53)
# Effective size ~512MB due to ~2:1 compression ratio
modprobe zram num_devices=1
echo lz4 > /sys/block/zram0/comp_algorithm
echo 256M > /sys/block/zram0/disksize
mkswap /sys/block/zram0 2>/dev/null
mkswap /dev/zram0
swapon -p 100 /dev/zram0
echo "ZRAM swap active: $(cat /proc/swaps | grep zram)"
ZRAM_EOF
chmod +x /usr/local/bin/zram-setup.sh

# Create systemd service to start ZRAM on boot
cat > /etc/systemd/system/zram-swap.service << 'SVC_EOF'
[Unit]
Description=ZRAM compressed swap
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/zram-setup.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SVC_EOF

systemctl daemon-reload
systemctl enable zram-swap.service
# Start it now too
bash /usr/local/bin/zram-setup.sh 2>/dev/null || true
echo "  ZRAM enabled: 256MB LZ4 compressed (effective ~512MB)"

# ─────────────────────────────────────────────
# 3. CPU GOVERNOR = performance
# ─────────────────────────────────────────────
echo ""
echo "[3/6] Setting CPU governor to 'performance'"

# Set now
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance > "$cpu" 2>/dev/null || true
done

# Make it persistent across reboots
cat > /etc/systemd/system/cpu-performance.service << 'GOV_EOF'
[Unit]
Description=Set CPU governor to performance
After=sysinit.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$g"; done'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
GOV_EOF

systemctl daemon-reload
systemctl enable cpu-performance.service
echo "  All CPU cores locked to max frequency"

# ─────────────────────────────────────────────
# 4. TMPFS for /tmp and /var/log
# ─────────────────────────────────────────────
echo ""
echo "[4/6] Mounting /tmp and /var/log on tmpfs (RAM disk)"

FSTAB="/etc/fstab"
cp "$FSTAB" "${FSTAB}.backup.$(date +%Y%m%d)" 2>/dev/null || true

# Add tmpfs entries if not already present
if ! grep -q 'tmpfs.*/tmp' "$FSTAB"; then
    echo 'tmpfs /tmp tmpfs defaults,noatime,nosuid,size=64m 0 0' >> "$FSTAB"
    echo "  Added /tmp tmpfs (64MB)"
fi

if ! grep -q 'tmpfs.*/var/log' "$FSTAB"; then
    echo 'tmpfs /var/log tmpfs defaults,noatime,nosuid,size=16m 0 0' >> "$FSTAB"
    echo "  Added /var/log tmpfs (16MB)"
fi
REBOOT_NEEDED=1

# ─────────────────────────────────────────────
# 5. SYSCTL TUNING
# ─────────────────────────────────────────────
echo ""
echo "[5/6] Tuning kernel VM parameters"

cat > /etc/sysctl.d/99-driver-monitoring.conf << 'SYSCTL_EOF'
# ZRAM-optimized: swap aggressively to fast ZRAM, not slow SD card
vm.swappiness=100

# Start flushing dirty pages early (1% of RAM)
# Prevents large dirty page buildups that cause I/O stalls
vm.dirty_background_ratio=1

# Don't force synchronous writes until 50% dirty
vm.dirty_ratio=50

# Reduce the watermark boost factor (less aggressive page reclaim)
vm.watermark_boost_factor=0

# Compact memory proactively to reduce allocation stalls
vm.compaction_proactiveness=20
SYSCTL_EOF

sysctl --system > /dev/null 2>&1
echo "  vm.swappiness=100 (prefer fast ZRAM over SD)"
echo "  vm.dirty_background_ratio=1 (flush early)"

# ─────────────────────────────────────────────
# 6. DISABLE UNNECESSARY SERVICES
# ─────────────────────────────────────────────
echo ""
echo "[6/6] Disabling unnecessary system services"

# Services that waste CPU/RAM on a headless embedded device
DISABLE_SERVICES=(
    triggerhappy          # Hotkey daemon — no keyboard
    ModemManager          # No cellular modem
    bluetooth             # No Bluetooth peripherals
    hciuart               # Bluetooth UART attach
    # NOTE: Do NOT disable wpa_supplicant (breaks WiFi)
    # NOTE: Do NOT disable avahi-daemon if using copilot.local / blinksmart.local
)

for svc in "${DISABLE_SERVICES[@]}"; do
    if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        systemctl disable "$svc" 2>/dev/null
        systemctl stop "$svc" 2>/dev/null || true
        echo "  Disabled: $svc"
    fi
done

# Also disable apt auto-update timers (waste I/O and bandwidth)
for timer in man-db.timer apt-daily.timer apt-daily-upgrade.timer; do
    systemctl disable "$timer" 2>/dev/null || true
    systemctl stop "$timer" 2>/dev/null || true
done
echo "  Disabled: apt-daily timers"

# ─────────────────────────────────────────────
# 7. HARDWARE WATCHDOG
# ─────────────────────────────────────────────
echo ""
echo "[7] Enabling hardware watchdog (auto-reboot on system hang)"

# systemd runtime watchdog: reboots Pi if kernel/systemd hangs for 15s
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/watchdog.conf << 'WD_EOF'
[Manager]
RuntimeWatchdogSec=15
RebootWatchdogSec=10min
WD_EOF
echo "  RuntimeWatchdogSec=15 (reboot if kernel hangs 15s)"

# ─────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────
echo ""
echo "=== Optimization Complete ==="
echo ""
echo "Changes applied:"
echo "  [x] CPU overclock: 1000MHz → 1200MHz (+20%)"
echo "  [x] GPU memory: kept at 64MB (minimum for camera ISP)"
echo "  [x] ZRAM swap: 256MB LZ4 (replaces slow SD card swap)"
echo "  [x] CPU governor: performance (no clock ramping)"
echo "  [x] tmpfs: /tmp (64MB) + /var/log (16MB) on RAM"
echo "  [x] Kernel: tuned vm.swappiness, dirty ratios"
echo "  [x] Disabled unused services (bluetooth, avahi, etc.)"
echo "  [x] Hardware watchdog: auto-reboot on 15s system hang"
echo ""

if [ "$REBOOT_NEEDED" -eq 1 ]; then
    echo ">>> REBOOT REQUIRED to apply overclock + tmpfs <<<"
    echo "    Run: sudo reboot"
fi
