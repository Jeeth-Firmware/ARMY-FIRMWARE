# Blinksmart DMS — Pi Zero 2W Complete Setup Guide

## Hardware Requirements
- **Board**: Raspberry Pi Zero 2W (BCM2710A1, 512MB RAM)
- **OS**: **64-bit** Raspberry Pi OS Lite (Bookworm) — `aarch64`
- **Cooling**: 5V fan (active cooling — required for sustained overclock)
- **Camera**: CSI camera module (OV5647 or similar)
- **GPS**: USB or UART serial GPS module (no gpsd — direct serial read)
- **Storage**: 32GB+ SD card (A1 or A2 rated recommended)

> **CRITICAL: Must use 64-bit OS.** MediaPipe has NO pre-built wheels for
> 32-bit ARM (armv7l). We confirmed this during deployment — `pip install
> mediapipe` fails on armv7l with "No matching distribution found". Only
> aarch64 is supported. 64-bit also gives 10-30% faster ML inference.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│                Pi Zero 2W (512MB)                │
│                                                  │
│  facial1.service (Nice=-10, CPUWeight=10000)     │
│    └── facil_event_capture.py                    │
│        ├── MediaPipe Face Mesh (TFLite)          │
│        ├── OpenCV VideoCapture (V4L2)            │
│        ├── Eye/Lip tracking → drowsiness detect  │
│        ├── Event capture → images/events/        │
│        └── SQLite (db_helper.py) ← shared DB     │
│                                                  │
│  get_gps_data1.service (Nice=5)                  │
│    └── get_gps_data.py                           │
│        ├── Serial GPS (pyserial + pynmea2)       │
│        ├── Shared memory IPC (gps_shm.py)        │
│        └── SQLite trip log (offline)             │
│                                                  │
│  provisioning_ui.service (Nice=10)               │
│    └── provisioning_ui.py (Flask :5000)          │
│        └── Web UI for device management          │
│                                                  │
│  wifi-ap-fallback.service                        │
│    └── wifi_ap_manager.py                        │
│        └── AP mode if no known WiFi found        │
└─────────────────────────────────────────────────┘
```

---

## RAM Savings Built Into This Deployment

| Change | RAM Saved | Accuracy Risk |
|--------|-----------|---------------|
| MySQL → SQLite | ~50-80MB | None |
| Remove firebase_admin | ~40-60MB | None |
| Redis → shared memory (mmap) | ~10MB | None |
| Lazy imports (base64, requests) | ~10-15MB | None |
| Consolidate threads (non-blocking LED) | ~2-3MB | None |
| systemd MemoryMax tuning | ~30-50MB freed for facial1 | None |
| **Total** | **~140-210MB** | **Zero** |

---

## Step 1: Flash OS & First Boot

**Where**: Your laptop/desktop

1. Open **Raspberry Pi Imager**
2. Choose OS: **Raspberry Pi OS Lite (64-bit, Bookworm)** — `raspios-bookworm-arm64-lite`
   - Must be **64-bit** (mediapipe requires aarch64)
   - Must be **Lite** (no Desktop — saves ~200MB RAM)
3. Choose Storage: Your SD card (32GB+)
4. Click gear icon (⚙️) to pre-configure:
   - Hostname: `blinksmart` (or `pi`)
   - Enable SSH: Yes (password-based)
   - Username: `pi`, Password: `<your-password>`
   - WiFi: Your network SSID + password
   - Locale/timezone as appropriate
5. Flash the SD card
6. Insert into Pi, power on, wait ~60 seconds
7. SSH in:
   ```bash
   ssh pi@blinksmart.local
   # or: ssh pi@<ip-address>
   ```

**Verify**:
```bash
uname -m             # Must show: aarch64
cat /etc/os-release | head -3
free -h              # ~400-440MB total RAM
```

**Expected output**:
```
aarch64
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
VERSION_ID="12"
               total        used        free      shared  buff/cache   available
Mem:           416Mi       140Mi       214Mi       2.6Mi       115Mi       276Mi
Swap:          511Mi          0B       511Mi
```

---

## Step 2: System Update & Essential Packages

```bash
sudo apt update && sudo apt full-upgrade -y

sudo apt install -y git htop iotop vim python3-pip python3-venv \
    libatlas-base-dev libopenblas-dev libjpeg-dev libpng-dev \
    libhdf5-dev libharfbuzz-dev libwebp-dev libtiff-dev \
    libavcodec-dev libavformat-dev libswscale-dev \
    v4l-utils sqlite3 stress-ng hostapd dnsmasq
```

**NOT installing** (replaced by lighter alternatives):
- `mariadb-server` — replaced by SQLite (embedded, no daemon)
- `redis-server` — replaced by shared memory IPC (mmap on /dev/shm/)
- `gpsd`, `gpsd-clients` — GPS is read directly via serial (pyserial + pynmea2)

---

## Step 3: Clone Code & Create Directories

```bash
cd /home/pi
mkdir -p facial-tracker-firmware
cd facial-tracker-firmware
git clone https://github.com/Unmesh28/firmware.git .
git checkout main
```

Create required directories:
```bash
mkdir -p data images/pending images/events logs
```

---

## Step 4: Run System Optimization

The `pi_optimize.sh` script configures everything in one shot:

```bash
sudo bash setup/pi_optimize.sh
sudo reboot
```

**What it does**:
- **Overclock**: `arm_freq=1200` (+20%), `over_voltage=2`, `core_freq=500`
- **Camera**: `start_x=1`, `camera_auto_detect=0` (legacy V4L2 stack for OpenCV)
- **GPU memory**: `gpu_mem=64` (minimum for camera ISP — below 64 breaks capture)
- **ZRAM swap**: 256MB LZ4 compressed (~512MB effective, replaces slow SD swap)
- **CPU governor**: `performance` (no clock ramping delays)
- **tmpfs**: `/tmp` (64MB) + `/var/log` (16MB) on RAM
- **Kernel tuning**: `vm.swappiness=100`, `vm.dirty_background_ratio=1`
- **Disabled services**: bluetooth, hciuart, triggerhappy, apt-daily timers

**Verify after reboot**:
```bash
ssh pi@blinksmart.local
vcgencmd measure_clock arm    # Should show: frequency(48)=1200126000
vcgencmd measure_temp         # Should show: ~40-47C with fan
vcgencmd get_throttled        # Must show: throttled=0x0
free -m                       # Check ZRAM in swap
cat /proc/swaps               # Should show /dev/zram0
```

**Verified results from our deployment**:
```
frequency(48)=1200126000
temp=46.7'C
throttled=0x0
Swap:  255  0  255
/dev/zram0  partition  262140  0  100
```

---

## Step 5: Python Virtual Environment & Dependencies

```bash
cd /home/pi/facial-tracker-firmware
python3 -m venv venv --system-site-packages
source venv/bin/activate
pip install --upgrade pip
```

**Important**: `/tmp` is on tmpfs (64MB) which is too small for large packages
like OpenCV (~95MB download). Temporarily unmount it for pip install:

```bash
sudo umount /tmp
TMPDIR=/home/pi/.pip-tmp pip install -r requirements.txt \
    --cache-dir /home/pi/.pip-cache
sudo mount -a   # Remount /tmp tmpfs

# Clean up
rm -rf /home/pi/.pip-tmp /home/pi/.pip-cache
```

**Verify**:
```bash
python3 -c "import mediapipe; print('MediaPipe:', mediapipe.__version__)"
python3 -c "import cv2; print('OpenCV:', cv2.__version__)"
python3 -c "import sqlite3; print('SQLite:', sqlite3.sqlite_version)"
```

**Verified results**:
```
MediaPipe: 0.10.9
OpenCV: 4.13.0
SQLite: 3.40.1
```

> **Why this works on 64-bit but not 32-bit**: mediapipe publishes wheels for
> x86_64 and aarch64 only. On armv7l it fails with "No matching distribution
> found". There are no workarounds — no piwheels build, no mediapipe-rpi4 for
> Python 3.11, building from source is impractical.

---

## Step 6: Initialize SQLite Database

```bash
cd /home/pi/facial-tracker-firmware
python3 init_sqlite.py
```

**Verify**:
```bash
sqlite3 data/blinksmart.db ".tables"
# Should show: car_data  configure  count_table  device  gps_data  user_info
```

**If migrating from an existing device with MySQL data**:
```bash
pip3 install mysql-connector-python
python3 migrate_mysql_to_sqlite.py
pip3 uninstall mysql-connector-python
```

---

## Step 7: Configure UART for Serial GPS

```bash
sudo raspi-config
# → Interface Options → Serial Port
# → Login shell over serial: NO
# → Serial port hardware: YES

sudo reboot
```

**Verify** (after connecting GPS module):
```bash
ls -la /dev/serial0   # Should link to /dev/ttyS0 or /dev/ttyAMA0
```

> **No gpsd needed.** The `get_gps_data.py` script reads NMEA sentences
> directly from the serial port using pyserial + pynmea2. For USB GPS
> modules, the device will appear as `/dev/ttyACM0`.

---

## Step 8: Device Provisioning

```bash
cd /home/pi/facial-tracker-firmware

# Create .env with your provisioning token
cp .env.example .env
nano .env   # Set PROVISIONING_TOKEN=<your-token>

# Run provisioning
source venv/bin/activate
python3 device_provisioning.py
```

**Expected output**:
```
Device created successfully: DM00029202602101305
Saved device credentials: device_id=DM00029202602101305
```

---

## Step 9: Setup Sudoers for Web UI Service Control

The provisioning web UI needs to start/stop systemd services and set the clock
(Settings > Date & Time), and the GPS service corrects the clock from GPS time.
Grant passwordless sudo for those commands to the pi user:

```bash
sudo bash -c 'echo "pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl, /usr/bin/date, /usr/bin/timedatectl, /usr/sbin/fake-hwclock, /usr/sbin/hwclock" > /etc/sudoers.d/pi-services && chmod 440 /etc/sudoers.d/pi-services && visudo -c'
```

`visudo -c` should say "parsed OK".

---

## Step 10: Install & Enable All Systemd Services

```bash
# Copy all service files
sudo cp systemd/facial1.service /etc/systemd/system/
sudo cp systemd/get_gps_data1.service /etc/systemd/system/
sudo cp systemd/provisioning_ui.service /etc/systemd/system/
sudo cp systemd/wifi-ap-fallback.service /etc/systemd/system/

sudo systemctl daemon-reload

# Enable all services to start on boot
sudo systemctl enable facial1 get_gps_data1 provisioning_ui wifi-ap-fallback
```

**Start services in priority order**:
```bash
sudo systemctl start provisioning_ui
sudo systemctl start facial1
sleep 3
sudo systemctl start get_gps_data1
sleep 2
sudo systemctl start wifi-ap-fallback
```

---

## Step 11: Verify Everything

### All services running:
```bash
for s in facial1 get_gps_data1 provisioning_ui; do
    echo -n "$s: "; systemctl is-active $s
done
```

**Expected**:
```
facial1: active
get_gps_data1: active
provisioning_ui: active
```

### Facial tracking performance:
```bash
journalctl -u facial1 -f --no-pager -n 20
```

**Expected log lines**:
```
INFO - Starting facial tracking with event capture (detection FPS: 10, buffer FPS: 2)
INFO - Capture: 640x480, Detection: 320x240, HEADLESS: True, refine_landmarks: True
INFO: Created TensorFlow Lite XNNPACK delegate for CPU.
INFO - Performance: avg 87ms/frame, max possible FPS: 11.5, detected: True
```

### Web UI:
Open browser: `http://<pi-ip-address>:5000/`

### System health:
```bash
free -m
vcgencmd measure_temp
vcgencmd get_throttled
```

---

## Step 12: Performance Testing

### Thermal (with 5V fan):
```bash
watch -n 2 'vcgencmd measure_temp && vcgencmd get_throttled'
# Target: 50-65C under load, 0x0 throttle
```

### Fan PWM speed control
This repo now supports PWM fan control on GPIO `18` (BCM 18 / physical pin `12`).
Use the following `.env` settings to control speed:
- `FAN_ENABLED=true`
- `FAN_GPIO_PIN=18`
- `FAN_MODE=auto` or `manual`
- `FAN_MANUAL_DUTY=0` (0-100) when `FAN_MODE=manual`
- `FAN_TEMP_ON=50`
- `FAN_TEMP_MAX=65`
- `FAN_MIN_DUTY=30`
- `FAN_MAX_DUTY=100`
- `FAN_PWM_FREQUENCY=250`
- `FAN_UPDATE_INTERVAL=10`

When `FAN_MODE=auto`, fan speed is scaled from `FAN_MIN_DUTY` at `FAN_TEMP_ON` to `FAN_MAX_DUTY` at `FAN_TEMP_MAX`.
When `FAN_MODE=manual`, the fan runs at the fixed PWM duty specified by `FAN_MANUAL_DUTY`.

Example for a moderate constant speed:
```bash
FAN_MODE=manual
FAN_MANUAL_DUTY=40
```

Example for temperature-based control:
```bash
FAN_MODE=auto
FAN_TEMP_ON=45
FAN_TEMP_MAX=70
FAN_MIN_DUTY=20
FAN_MAX_DUTY=100
```

> Wiring reminder: use a transistor or MOSFET on the fan low side. Fan power should come from 5V, not directly from GPIO.

### RAM Usage:
```bash
free -m
ps aux --sort=-%mem | head -10
```

### FPS:
```bash
journalctl -u facial1 -f | grep "Performance"
# Target: ~87ms/frame, 11+ FPS on Pi 4 (expect ~5-8 FPS on Pi Zero 2W)
```

### Sustained test (10+ minutes):
```bash
dmesg | grep -i oom         # No OOM kills
vcgencmd get_throttled      # Still 0x0
free -h                     # Stable RAM
```

---

## Verified Performance (Pi 4, all services running)

| Metric | Value |
|--------|-------|
| Frame processing | ~87ms/frame |
| Max FPS | 11.4-11.6 |
| Facial1 RAM | ~199-206MB (48-49%) |
| Total RAM used | 290M/416M |
| Swap used | 58M/256M ZRAM |
| Temperature | 53.5C with fan |
| Throttle status | 0x0 (clean) |
| Load average | 1.70 |

> **Pi Zero 2W expected**: ~5-8 FPS (Cortex-A53 @ 1.2GHz is ~2-3x slower
> than Pi 4's Cortex-A72 @ 1.5GHz). RAM will be tighter but fits with ZRAM.

---

## Expected RAM Budget

| Component | RAM Usage |
|-----------|-----------|
| OS (64-bit Lite, Bookworm) | ~55-65MB |
| GPU reserved | 64MB |
| **facial1.service** (MediaPipe + OpenCV) | ~170-220MB |
| get_gps_data1.service | ~15-25MB |
| provisioning_ui.service (Flask) | ~20-30MB |
| SQLite (embedded, no daemon) | ~2MB |
| Shared memory GPS IPC | <1MB |
| **Total Used** | **~360-420MB** |
| **Free RAM** | **~20-80MB** |
| **Swap Available** | **~512MB (ZRAM)** |

---

## WiFi AP Fallback Mode

When the Pi boots without a known WiFi network, it automatically starts AP mode:

- **SSID**: `SapienceDevice-XXXX` (last 4 chars of device ID)
- **Password**: `sapience123`
- **IP**: `192.168.4.1`
- **Web UI**: `http://192.168.4.1:5000`

Connect to the AP from your phone/laptop, open the web UI, and configure WiFi.

---

## Key Configuration Files

| File | Purpose |
|------|---------|
| `facial_tracking/conf.py` | Detection thresholds (SLEEP_SECONDS=1.5, EYE_CLOSED=0.20, etc.) |
| `.env` | Device credentials, provisioning token, delete password |
| `db_helper.py` | SQLite connection manager (WAL mode, thread-local) |
| `gps_shm.py` | Shared memory IPC for GPS data (replaces Redis) |
| `setup/pi_optimize.sh` | System optimization script |
| `systemd/*.service` | All systemd service definitions |

---

## Troubleshooting

### Camera not working
- Ensure `start_x=1` and `camera_auto_detect=0` in `/boot/firmware/config.txt`
- Ensure `gpu_mem=64` (below 64 breaks camera ISP)
- Check: `ls /dev/video0` — should exist

### mediapipe won't install
- Verify `uname -m` shows `aarch64` (not `armv7l`)
- If 32-bit: must reflash with 64-bit Bookworm Lite

### pip install "No space left on device"
- `/tmp` is on tmpfs (64MB) — too small for opencv
- Fix: `sudo umount /tmp` before pip install, `sudo mount -a` after

### Service fails with "ota-startup.service not found"
- Remove `Requires=ota-startup.service` from the service file
- Our updated service files already have this fixed

### Web UI start/stop services not working
- Ensure sudoers rule exists: `cat /etc/sudoers.d/pi-services`
- Must contain: `pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl`
- Must have `chmod 440`

### GPS not reading data
- Check serial: `ls -la /dev/serial0` or `ls /dev/ttyACM0`
- Ensure raspi-config has serial hardware enabled, login shell disabled
- Check `GPS_FORCE_GPSD=0` in service file (we use serial, not gpsd)

---

## Quick Reference

```bash
# SSH in
ssh pi@blinksmart.local

# Service management
sudo systemctl start|stop|restart|status facial1
journalctl -u facial1 -f

# All services status
for s in facial1 get_gps_data1 provisioning_ui; do
    echo -n "$s: "; systemctl is-active $s
done

# Temperature / throttle
vcgencmd measure_temp
vcgencmd get_throttled    # 0x0 = good

# RAM
free -m

# Database
sqlite3 /home/pi/facial-tracker-firmware/data/blinksmart.db ".tables"

# GPS shared memory
ls -la /dev/shm/blinksmart_gps

# Web UI
# http://<pi-ip>:5000/

# Delete device password: Sapience@2128
```
