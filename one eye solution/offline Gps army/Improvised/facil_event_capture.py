"""
Facial Tracking with Event-Based Frame Buffer & GIF Generation

This is an updated version of facil_updated_1.py that uses the new event capture system
for capturing pre-event, during-event, and post-event frames.
"""

import cv2
import os
import sys
import time
import gc
import fcntl
import signal
import socket
from datetime import datetime
from queue import Queue
import threading


# --- systemd watchdog: notify systemd we're alive ---
_sd_notify_sock = None

def _sd_notify(msg):
    """Send sd_notify message to systemd (READY=1, WATCHDOG=1, etc.)."""
    global _sd_notify_sock
    addr = os.getenv('NOTIFY_SOCKET')
    if not addr:
        return
    try:
        if _sd_notify_sock is None:
            _sd_notify_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            if addr[0] == '@':
                addr = '\0' + addr[1:]
            _sd_notify_sock.connect(addr)
        _sd_notify_sock.sendall(msg.encode())
    except Exception:
        _sd_notify_sock = None
import RPi.GPIO as GPIO
from store_locally import add_gps_data, get_last_gps_data
from log import log_info, log_error
from get_configure import get_configure
from facial_tracking.facialTracking import FacialTracker
import facial_tracking.conf as conf
from blnk_led import stop_blinking, start_blinking, refresh_blinking, update_led, force_cleanup as led_force_cleanup
from buzzer_controller import (
    buzz_for, start_continuous_buzz, stop_continuous_buzz,
    speed_limit_buzzer_on, speed_limit_buzzer_off,
    force_cleanup as buzzer_force_cleanup,
)
from event_capture import init_event_capture, get_event_buffer, shutdown_event_capture


FAN_ENABLED = os.getenv("FAN_ENABLED", "true").lower() == "true"
FAN_GPIO_PIN = int(os.getenv("FAN_GPIO_PIN", "18"))
FAN_MODE = os.getenv("FAN_MODE", "auto").lower()  # auto or manual
FAN_MANUAL_DUTY = float(os.getenv("FAN_MANUAL_DUTY", "0"))
FAN_TEMP_ON = float(os.getenv("FAN_TEMP_ON", "50"))
FAN_TEMP_MAX = float(os.getenv("FAN_TEMP_MAX", "65"))
FAN_MIN_DUTY = float(os.getenv("FAN_MIN_DUTY", "30"))
FAN_MAX_DUTY = float(os.getenv("FAN_MAX_DUTY", "100"))
FAN_PWM_FREQUENCY = int(os.getenv("FAN_PWM_FREQUENCY", "250"))
FAN_UPDATE_INTERVAL = float(os.getenv("FAN_UPDATE_INTERVAL", "10"))

_fan_pwm = None
_fan_current_duty = 0.0


def _read_cpu_temp() -> float:
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            return float(int(f.read().strip())) / 1000.0
    except Exception:
        return 0.0


def _calculate_fan_duty(temp_c: float) -> float:
    if not FAN_ENABLED:
        return 0.0
    if FAN_MODE == 'manual':
        return max(0.0, min(100.0, FAN_MANUAL_DUTY))
    if temp_c < FAN_TEMP_ON:
        return 0.0
    if temp_c >= FAN_TEMP_MAX:
        return FAN_MAX_DUTY
    ratio = (temp_c - FAN_TEMP_ON) / max(1.0, FAN_TEMP_MAX - FAN_TEMP_ON)
    return max(0.0, min(100.0, FAN_MIN_DUTY + ratio * (FAN_MAX_DUTY - FAN_MIN_DUTY)))


def _fan_init():
    global _fan_pwm, _fan_current_duty
    _fan_current_duty = 0.0
    if not FAN_ENABLED:
        log_info("Fan control disabled via FAN_ENABLED=false")
        return
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(FAN_GPIO_PIN, GPIO.OUT, initial=GPIO.LOW)
        _fan_pwm = GPIO.PWM(FAN_GPIO_PIN, FAN_PWM_FREQUENCY)
        _fan_pwm.start(0.0)
        log_info(f"Fan control initialized on GPIO {FAN_GPIO_PIN} (BCM) at {FAN_PWM_FREQUENCY}Hz, mode={FAN_MODE}")
    except Exception as e:
        log_error(f"Failed to initialize fan PWM on GPIO {FAN_GPIO_PIN}: {e}")
        _fan_pwm = None


def _fan_update():
    global _fan_pwm, _fan_current_duty
    if not FAN_ENABLED or _fan_pwm is None:
        return
    temp = _read_cpu_temp()
    duty = _calculate_fan_duty(temp)
    if abs(duty - _fan_current_duty) < 1.0:
        return
    _fan_current_duty = duty
    try:
        if duty <= 0.0:
            _fan_pwm.ChangeDutyCycle(0.0)
            GPIO.output(FAN_GPIO_PIN, GPIO.LOW)
        else:
            _fan_pwm.ChangeDutyCycle(duty)
        log_info(f"Fan duty set to {duty:.1f}% for CPU temp {temp:.1f}°C")
    except Exception as e:
        log_error(f"Failed to update fan PWM duty: {e}")


def _fan_shutdown():
    global _fan_pwm
    if _fan_pwm is None:
        return
    try:
        _fan_pwm.ChangeDutyCycle(0.0)
        _fan_pwm.stop()
        GPIO.output(FAN_GPIO_PIN, GPIO.LOW)
    except Exception:
        pass
    _fan_pwm = None


class ThreadedCamera:
    """
    Threaded camera capture to separate I/O from processing.
    Camera reads happen in a background thread so the main loop
    always has a fresh frame ready without blocking on I/O.
    Reference: https://pyimagesearch.com/2015/12/28/increasing-raspberry-pi-fps-with-python-and-opencv/
    """

    def __init__(self, src=0, width=640, height=480, fps=0):
        self.cap = cv2.VideoCapture(src)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if fps > 0:
            self.cap.set(cv2.CAP_PROP_FPS, fps)

        self.grabbed, self.frame = self.cap.read()
        self.stopped = False
        self.lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._update, daemon=True).start()
        return self

    def _update(self):
        while not self.stopped:
            grabbed, frame = self.cap.read()
            with self.lock:
                self.grabbed = grabbed
                self.frame = frame
            if not grabbed:
                time.sleep(0.05)  # Camera error returns instantly — don't spin a core

    def read(self):
        with self.lock:
            return self.grabbed, self.frame

    def isOpened(self):
        return self.cap.isOpened()

    def release(self):
        self.stopped = True
        self.cap.release()

# Default bundle version
bundle_version = "1.0.0"

# Lock file to prevent multiple instances
LOCK_FILE = '/tmp/facil_event_capture.lock'
lock_fd = None

def acquire_lock():
    """Ensure only one instance runs at a time"""
    global lock_fd
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        print(f"Lock acquired. PID: {os.getpid()}")
        return True
    except IOError:
        print("ERROR: Another instance of facil_event_capture.py is already running.")
        print("Kill existing process first: pkill -f facil_event_capture.py")
        sys.exit(1)

# Initialize shared memory GPS reader (replaces Redis)
from gps_shm import GPSReader
gps_reader = GPSReader()

speed_config = int(get_configure('speed')) if get_configure('speed') else 0
speed_limit_config = int(get_configure('speed_limit')) if get_configure('speed_limit') else 0

# Apply head-pose attention settings from DB at startup
_ld = get_configure('lookdown_enabled')
if _ld is not None:
    conf.LOOKDOWN_ENABLED = _ld != '0'
    print(f"DEBUG: LOOKDOWN_ENABLED set to {conf.LOOKDOWN_ENABLED} from DB config")
_lp = get_configure('lookdown_pitch_threshold')
if _lp:
    conf.LOOKDOWN_PITCH_THRESHOLD = float(_lp)
_ly = get_configure('lookaway_yaw_threshold')
if _ly:
    conf.LOOKAWAY_YAW_THRESHOLD = float(_ly)
_ldf = get_configure('lookdown_frames')
if _ldf:
    conf.LOOKDOWN_FRAMES = int(_ldf)
_laf = get_configure('lookaway_frames')
if _laf:
    conf.LOOKAWAY_FRAMES = int(_laf)
_loes = get_configure('lookaway_one_eye_seconds')
if _loes:
    conf.LOOKAWAY_ONE_EYE_SECONDS = float(_loes)

# Create folders
base_path = "/home/pi/facial-tracker-firmware/images"
events_path = os.path.join(base_path, "events")
os.makedirs(events_path, exist_ok=True)

# Initialize event capture system
event_buffer = init_event_capture(base_events_path=events_path)

# NoFace buzzer settings (defaults — overridden by DB config)
NO_FACE_THRESHOLD = 2.0  # seconds before buzzing
BUZZER_DURATION = 1.7    # seconds

# --- Runtime settings from DB (re-read periodically on a background thread) ---
_settings_read_interval = 30  # re-read every 30 seconds
led_blink_enabled = True
noface_enabled = False
noface_threshold = 2.0


def _apply_float_setting(key, attr, lo, hi):
    """Copy a numeric DB setting into conf, ignoring missing / out-of-range values."""
    val = get_configure(key)
    if val is None:
        return
    try:
        num = float(val)
    except (TypeError, ValueError):
        return
    if lo <= num <= hi:
        setattr(conf, attr, num)


def _reload_settings():
    """Re-read settings from DB so changes take effect without restart."""
    global led_blink_enabled, noface_enabled, noface_threshold, speed_config, speed_limit_config
    # Timing settings are independent of the block below so one bad value can't skip them
    try:
        _apply_float_setting('sleep_seconds', 'SLEEP_SECONDS', 0.5, 5.0)
        _apply_float_setting('phone_confirm_seconds', 'PHONE_CONFIRM_SECONDS', 0.0, 10.0)
        _apply_float_setting('phone_buzz_seconds', 'PHONE_BUZZ_SECONDS', 0.0, 60.0)
        _apply_float_setting('phone_ear_dist_ratio', 'PHONE_EAR_DIST_RATIO', 0.2, 1.0)
    except Exception:
        pass
    try:
        val = get_configure('led_blink_enabled')
        led_blink_enabled = val != '0' if val is not None else True
        val = get_configure('noface_enabled')
        noface_enabled = val == '1' if val is not None else False
        val = get_configure('noface_threshold')
        noface_threshold = float(val) if val else 2.0
        val = get_configure('speed')
        speed_config = int(val) if val else 0
        val = get_configure('speed_limit')
        speed_limit_config = int(val) if val else 0
        # Live phone-detection toggle (detector reads conf each update)
        val = get_configure('phone_detection_enabled')
        if val is not None:
            conf.PHONE_DETECTION_ENABLED = val != '0'
        # Live head-pose attention toggle + thresholds
        val = get_configure('lookdown_enabled')
        if val is not None:
            conf.LOOKDOWN_ENABLED = val != '0'
        val = get_configure('lookdown_pitch_threshold')
        if val is not None:
            conf.LOOKDOWN_PITCH_THRESHOLD = float(val)
        val = get_configure('lookaway_yaw_threshold')
        if val is not None:
            conf.LOOKAWAY_YAW_THRESHOLD = float(val)
        val = get_configure('lookdown_frames')
        if val is not None:
            conf.LOOKDOWN_FRAMES = int(val)
        val = get_configure('lookaway_frames')
        if val is not None:
            conf.LOOKAWAY_FRAMES = int(val)
        val = get_configure('lookaway_one_eye_seconds')
        if val is not None:
            conf.LOOKAWAY_ONE_EYE_SECONDS = float(val)
    except Exception:
        pass


def _settings_reload_loop():
    """Periodic DB reload — keeps ~20 SQLite reads off the detection loop."""
    while True:
        time.sleep(_settings_read_interval)
        _reload_settings()

def map_driver_status(status):
    """Map device driver status to backend expected format"""
    status_map = {
        'Sleeping/Looking Down': 'Sleeping',
        'Yawning/Fatigued': 'Yawning',
        'No Face': 'NoFace',
        'Sleeping': 'Sleeping',
        'Yawning': 'Yawning',
        'PhoneCall': 'PhoneCall',
        'phone call': 'PhoneCall',
        'LookingDown': 'LookingDown',
        'LookingAway': 'LookingAway',
        'NoFace': 'NoFace',
        'Overspeeding': 'Overspeeding',
        'Active': 'Active',
        'eye closed': 'Sleeping',
        'yawning': 'Yawning',
    }
    return status_map.get(status, status)

def decode_or_default(value, default='0'):
    """Legacy helper kept for compatibility."""
    return value.decode() if isinstance(value, bytes) and value else str(value) if value else default

def _is_valid_coord_pair(lat, lon):
    try:
        lat_f = float(lat)
        lon_f = float(lon)
        return lat_f != 0.0 and lon_f != 0.0 and -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0
    except Exception:
        return False

def _get_last_local_gps():
    """Return last locally stored GPS coordinates, or None if no valid row exists."""
    try:
        last_gps = get_last_gps_data()
        if not last_gps:
            return None
        lat, lon = last_gps
        if _is_valid_coord_pair(lat, lon):
            return str(lat), str(lon)
    except Exception as e:
        log_error(f"Failed to read last local GPS: {e}")
    return None

_last_idle_send_time = 0

# Single background worker for GPS sends — prevents unbounded thread spawning
_gps_queue = None

def _gps_worker():
    """Persistent worker for GPS data sends. One at a time, never piles up."""
    while True:
        try:
            args = _gps_queue.get()
            add_gps_data(*args)
        except Exception:
            pass

def _init_workers():
    """Start persistent background worker threads."""
    global _gps_queue
    _gps_queue = Queue(maxsize=2)
    threading.Thread(target=_gps_worker, daemon=True).start()

def _enqueue_gps(lat, long2, speed, timestamp, status, acc):
    """Queue GPS data for background send. Drops if queue full (backpressure)."""
    try:
        _gps_queue.put_nowait((lat, long2, speed, timestamp, status, acc))
    except Exception:
        pass  # Drop if worker is busy — next one will update

def send_status_and_stop_led(lat, long2, speed, status, acc):
    global _last_idle_send_time
    now = time.monotonic()
    # Throttle: only send GPS + stop LED once per 2 seconds when idle
    if now - _last_idle_send_time < 2.0:
        return
    _last_idle_send_time = now

    stop_blinking()  # Direct call, no thread needed (just sets flag + GPIO off)
    stop_continuous_buzz()  # Stop buzzer when vehicle idle/stopped
    speed_limit_buzzer_off()
    api_status = map_driver_status(status)
    _enqueue_gps(lat, long2, speed, str(datetime.now()), api_status, acc)

def _handle_signal(signum, frame):
    """Handle SIGTERM/SIGINT: force buzzer+LED off immediately, then exit cleanly."""
    buzzer_force_cleanup()
    led_force_cleanup()
    sys.exit(0)

# Catch SIGTERM (systemctl stop) and SIGINT (Ctrl+C) for clean shutdown
signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def main():
    # Start persistent background workers (GPS)
    _init_workers()
    _fan_init()

    # Automatic GC stays off; collections are run manually on a timer.
    # Startup objects (MediaPipe / OpenCV / numpy / paho) are frozen once init is done,
    # so a pass only scans objects created since. Previously a full gc.collect() over
    # everything ran every 300 frames (~2 min at real Pi FPS) and stalled the loop.
    gc.disable()
    GC_YOUNG_INTERVAL = 10   # seconds — young generations only (~1ms)
    GC_FULL_INTERVAL = 600   # seconds — full pass over non-frozen objects

    # Threaded camera: I/O runs in background thread, main loop never blocks on read
    cap = ThreadedCamera(src=conf.CAM_ID, width=conf.FRAME_W, height=conf.FRAME_H, fps=conf.CAMERA_FPS)
    cap.start()

    facial_tracker = FacialTracker()

    # Frame timing for facial detection (from conf, default 10 FPS for Pi Zero 2W)
    target_detection_fps = conf.TARGET_DETECTION_FPS
    detection_frame_interval = 1.0 / target_detection_fps
    last_detection_time = 0

    # Frame timing for event buffer (2 FPS — raw numpy copy only, JPEG encoding in save worker)
    target_buffer_fps = 2
    buffer_frame_interval = 1.0 / target_buffer_fps
    # During critical events: 4 FPS is plenty for the event GIF and keeps RAM bounded
    event_frame_interval = 1.0 / 4
    last_buffer_frame_time = 0

    # GPS data sending cooldown (for Active status)
    last_gps_send_time = 0
    gps_send_interval = 2  # Send GPS every 2 seconds when Active

    # Track NoFace detection for buzzer trigger
    no_face_start_time = None
    no_face_buzzer_triggered = False
    
    # Track consecutive NoFace frames (5 frames required before triggering alert)
    no_face_consecutive_frames = 0
    NO_FACE_FRAME_THRESHOLD = 5  # Trigger NoFace alert after 5 consecutive frames

    # PhoneCall: buzz for first conf.PHONE_BUZZ_SECONDS only (keep status/MQTT/GPS/images)
    phone_call_buzz_start = None

    # GPS cache — read from shared memory at most 2x/sec
    last_gps_read_time = 0
    gps_read_interval = 0.5
    cached_speed = '0'
    last_local_gps = _get_last_local_gps()
    if last_local_gps:
        cached_lat, cached_long = last_local_gps
        log_info(f"Loaded last local GPS for offline image metadata: {cached_lat},{cached_long}")
    else:
        cached_lat = '0.0'
        cached_long = '0.0'
    last_local_gps_lookup_time = 0
    local_gps_lookup_interval = 10
    cached_acc = '0'

    # Detection resolution: half of capture to reduce preprocessing overhead
    # MediaPipe resizes to fixed model input internally, so savings are in
    # cvtColor + internal resize (~5-10ms). Main benefit: less memory pressure.
    # Eye/lip ratios are scale-invariant, so accuracy is preserved.
    DETECT_W = conf.FRAME_W // 2  # 320
    DETECT_H = conf.FRAME_H // 2  # 240

    log_info(f"Starting facial tracking with event capture (detection FPS: {target_detection_fps}, buffer FPS: {target_buffer_fps})")
    log_info(f"Capture: {conf.FRAME_W}x{conf.FRAME_H}, Detection: {DETECT_W}x{DETECT_H}, HEADLESS: {conf.HEADLESS}, refine_landmarks: {conf.REFINE_LANDMARKS}")

    # Load settings from DB on startup, then keep them fresh in the background
    _reload_settings()
    threading.Thread(target=_settings_reload_loop, daemon=True).start()
    log_info(
        f"Settings: Activation Speed={speed_config}km/h, Speed Limit={speed_limit_config}km/h (0=off), "
        f"LED blink={led_blink_enabled}, NoFace alert={noface_enabled}, NoFace threshold={noface_threshold}s, "
        f"Sleep after={conf.SLEEP_SECONDS}s, CAMERA_FPS={conf.CAMERA_FPS}, "
        f"PhoneDetect={conf.PHONE_DETECTION_ENABLED}, confirm={conf.PHONE_CONFIRM_SECONDS}s, "
        f"buzz={conf.PHONE_BUZZ_SECONDS}s, ear_ratio={conf.PHONE_EAR_DIST_RATIO}, "
        f"hands_scan={conf.HANDS_SCAN_INTERVAL}s, "
        f"LookDown={conf.LOOKDOWN_ENABLED}, pitch>={conf.LOOKDOWN_PITCH_THRESHOLD}, "
        f"yaw>={conf.LOOKAWAY_YAW_THRESHOLD}, "
        f"LookingAway one-eye hold={conf.LOOKAWAY_ONE_EYE_SECONDS}s"
    )

    # Tell systemd we're ready (for Type=notify)
    _sd_notify('READY=1')

    # Watchdog ping interval (ping every 10s, systemd timeout is 30s)
    _last_watchdog_ping = 0
    _watchdog_interval = 10

    # Fan control interval
    _last_fan_update = 0

    # Init done — freeze long-lived startup objects out of future GC passes
    gc.collect()
    if hasattr(gc, 'freeze'):
        gc.freeze()
    last_gc_young = last_gc_full = time.monotonic()

    # Log any detection iteration slower than this (lag diagnostics)
    SLOW_FRAME_LOG_MS = 600

    try:
        while cap.isOpened():
            # Monotonic: setting the date/time (or NTP/GPS sync) must not stall alert timers
            current_time = time.monotonic()

            # Ping systemd watchdog — proves main loop is alive
            if current_time - _last_watchdog_ping >= _watchdog_interval:
                _last_watchdog_ping = current_time
                _sd_notify('WATCHDOG=1')

            # Update fan speed every interval based on CPU temperature
            if current_time - _last_fan_update >= FAN_UPDATE_INTERVAL:
                _last_fan_update = current_time
                _fan_update()

            # Read GPS from shared memory (single read, no network round-trip)
            if current_time - last_gps_read_time >= gps_read_interval:
                last_gps_read_time = current_time
                lat_f, lon_f, speed_f, acc_f, _ts = gps_reader.read()
                cached_speed = f"{float(speed_f):.1f}"
                # Keep last valid GPS coordinates instead of overwriting with 0.0
                # when GPS has a temporary fix drop. This keeps image overlays useful.
                if _is_valid_coord_pair(lat_f, lon_f):
                    cached_lat = str(lat_f)
                    cached_long = str(lon_f)
                elif not _is_valid_coord_pair(cached_lat, cached_long) and current_time - last_local_gps_lookup_time >= local_gps_lookup_interval:
                    last_local_gps_lookup_time = current_time
                    last_local_gps = _get_last_local_gps()
                    if last_local_gps:
                        cached_lat, cached_long = last_local_gps
                        log_info(f"Using last local GPS for offline image metadata: {cached_lat},{cached_long}")
                cached_acc = str(acc_f)

            speed = cached_speed
            lat = cached_lat
            long2 = cached_long
            acc = cached_acc
            overspeeding_active = False

            # Check speed limit violation (independent of activation speed)
            # Speed limit buzzer triggers when speed > limit (even if below activation speed for detection)
            current_speed = float(speed or 0)
            if speed_limit_config > 0 and current_speed > speed_limit_config:
                overspeeding_active = True
                speed_limit_buzzer_on()
            else:
                speed_limit_buzzer_off()

            # Check speed threshold (activation speed for facial detection)
            if current_speed < speed_config and not overspeeding_active:
                # Always ensure LED & buzzer are off when below speed threshold
                stop_blinking()
                stop_continuous_buzz()
                # Send idle status to backend (only when GPS coords available)
                if float(lat) != 0.0 and float(long2) != 0.0:
                    send_status_and_stop_led(lat, long2, speed or "0", "", acc)
                time.sleep(0.1)  # Slow down when not processing
                continue

            # Frame timing: throttle detection to target FPS.
            # Sleep the remaining slot instead of 1ms polling (~1000 wakeups/s on a Pi Zero).
            since_detection = current_time - last_detection_time
            if since_detection < detection_frame_interval:
                time.sleep(detection_frame_interval - since_detection)
                continue

            # Read latest frame from threaded camera (non-blocking, always fresh)
            success, frame = cap.read()
            if not success or frame is None:
                time.sleep(0.01)
                continue

            # Downscale for detection: reduces cvtColor + internal resize overhead
            # Eye/lip ratios are scale-invariant — identical detection accuracy
            detect_frame = cv2.resize(frame, (DETECT_W, DETECT_H), interpolation=cv2.INTER_AREA)

            last_detection_time = current_time
            t_start = time.monotonic()
            facial_tracker.process_frame(detect_frame)
            t_process = time.monotonic() - t_start

            # Log processing time every 30 frames to track actual FPS
            if not hasattr(main, '_frame_count'):
                main._frame_count = 0
                main._total_process_time = 0
            main._frame_count += 1
            main._total_process_time += t_process
            if main._frame_count % 30 == 0:
                avg_ms = (main._total_process_time / 30) * 1000
                actual_fps = 30 / main._total_process_time if main._total_process_time > 0 else 0
                log_info(f"Performance: avg {avg_ms:.0f}ms/frame, max possible FPS: {actual_fps:.1f}, detected: {facial_tracker.detected}")
                main._total_process_time = 0

            # Manual GC at controlled intervals (avoids random GC pauses during inference)
            if current_time - last_gc_young >= GC_YOUNG_INTERVAL:
                full = current_time - last_gc_full >= GC_FULL_INTERVAL
                t_gc = time.monotonic()
                gc.collect() if full else gc.collect(1)
                gc_ms = (time.monotonic() - t_gc) * 1000
                last_gc_young = current_time
                if full:
                    last_gc_full = current_time
                if gc_ms > 50:
                    log_info(f"GC {'full' if full else 'young'} pass took {gc_ms:.0f}ms")

            # Determine driver status
            # Watchdog pattern: buzzer & LED auto-stop if not refreshed.
            # During Sleeping/Yawning/LookingDown/LookingAway:
            #   call start_continuous_buzz() + refresh_blinking() every frame.
            # PhoneCall: buzz only for first PHONE_BUZZ_SECONDS, then silent status (no process restart).
            # When condition clears: explicit stop for instant response, watchdog as safety net.
            # Priority: Sleeping > Yawning > LookingDown > LookingAway > PhoneCall > Overspeeding
            if facial_tracker.detected:
                # Reset NoFace tracking when face is detected
                no_face_start_time = None
                no_face_buzzer_triggered = False
                no_face_consecutive_frames = 0  # Reset consecutive frame counter

                attn = facial_tracker.attention_status

                if facial_tracker.eyes_status == 'eye closed':
                    driver_status = 'Sleeping'
                    phone_call_buzz_start = None
                    if not overspeeding_active:
                        start_continuous_buzz()   # Refreshes watchdog each frame
                    if led_blink_enabled:
                        start_blinking()          # Activate LED (idempotent)
                        refresh_blinking()        # Refreshes LED watchdog each frame
                        update_led()              # Actually toggle the GPIO pin

                elif facial_tracker.yawn_status == 'yawning':
                    driver_status = 'Yawning'
                    phone_call_buzz_start = None
                    if not overspeeding_active:
                        start_continuous_buzz()   # Refreshes watchdog each frame
                    if led_blink_enabled:
                        start_blinking()          # Activate LED (idempotent)
                        refresh_blinking()        # Refreshes LED watchdog each frame
                        update_led()              # Actually toggle the GPIO pin

                elif attn in ('LookingDown', 'LookingAway'):
                    # Head-pose distraction — works with one eye occluded/missing
                    driver_status = attn
                    phone_call_buzz_start = None
                    if not overspeeding_active:
                        start_continuous_buzz()
                    if led_blink_enabled:
                        start_blinking()
                        refresh_blinking()
                        update_led()

                elif facial_tracker.phone_status == 'phone call':
                    driver_status = 'PhoneCall'
                    if phone_call_buzz_start is None:
                        phone_call_buzz_start = current_time
                    # Buzz for the configured window only — then stop buzzer but keep reporting
                    if not overspeeding_active:
                        if current_time - phone_call_buzz_start < conf.PHONE_BUZZ_SECONDS:
                            start_continuous_buzz()
                        else:
                            stop_continuous_buzz()
                    if led_blink_enabled:
                        start_blinking()
                        refresh_blinking()
                        update_led()

                elif overspeeding_active:
                    driver_status = 'Overspeeding'
                    phone_call_buzz_start = None

                else:
                    driver_status = 'Active'
                    phone_call_buzz_start = None
                    # Immediate stop — no throttle. Just flag+GPIO ops, very cheap.
                    stop_blinking()
                    stop_continuous_buzz()
            else:
                # Immediate stop of continuous alerts (driver not visible)
                stop_blinking()
                stop_continuous_buzz()
                phone_call_buzz_start = None

                # Count consecutive NoFace frames
                no_face_consecutive_frames += 1

                # Overspeeding takes priority over NoFace when speed limit is violated.
                # This ensures event type/status is Overspeeding during speed alerts.
                if overspeeding_active:
                    driver_status = 'Overspeeding'
                    no_face_start_time = None
                    no_face_buzzer_triggered = False
                    no_face_consecutive_frames = 0
                # Only trigger NoFace alert when NOT overspeeding
                # Only trigger NoFace alert after 5 consecutive frames
                elif no_face_consecutive_frames >= NO_FACE_FRAME_THRESHOLD:
                    driver_status = 'NoFace'
                    
                    # Start tracking NoFace duration for buzzer (after 5-frame confirmation)
                    if no_face_start_time is None:
                        no_face_start_time = current_time
                        no_face_buzzer_triggered = False

                    # Calculate how long confirmed NoFace has persisted
                    no_face_duration = current_time - no_face_start_time

                    # Buzz when NoFace persists beyond threshold (configurable via Settings)
                    if noface_enabled and no_face_duration >= noface_threshold:
                        buzz_for(BUZZER_DURATION)
                        no_face_buzzer_triggered = True
                        log_info(f"NoFace detected for {no_face_consecutive_frames} frames, {no_face_duration:.1f}s - buzzer activated")
                        no_face_start_time = current_time  # Reset timer for next interval
                else:
                    # Not yet 5 consecutive frames - treat as Active (no face yet)
                    driver_status = 'Overspeeding' if overspeeding_active else 'Active'
                    no_face_start_time = None
                    no_face_buzzer_triggered = False

            # Add full-res frame to event buffer
            # During critical events: 4 FPS for the event GIF
            # During idle/normal: throttle to 2 FPS (pre-event buffer doesn't need more)
            is_critical_event = driver_status in ['Sleeping', 'Yawning', 'PhoneCall',
                                                  'Sleeping/Looking Down', 'Yawning/Fatigued',
                                                  'LookingDown', 'LookingAway',
                                                  'Overspeeding']
            feed_interval = event_frame_interval if is_critical_event else buffer_frame_interval
            if current_time - last_buffer_frame_time >= feed_interval:
                last_buffer_frame_time = current_time
                event_buffer.add_frame(
                    frame=frame,
                    speed=speed,
                    lat=lat,
                    long=long2,
                    acc=acc,
                    driver_status=driver_status
                )

            # Send GPS data + driver status periodically (for all statuses)
            if float(lat) != 0.0 and float(long2) != 0.0:
                if current_time - last_gps_send_time >= gps_send_interval:
                    _enqueue_gps(lat, long2, speed, str(datetime.now()), driver_status, acc)
                    last_gps_send_time = current_time

            loop_ms = (time.monotonic() - current_time) * 1000
            if loop_ms > SLOW_FRAME_LOG_MS:
                log_info(
                    f"Slow frame: {loop_ms:.0f}ms total, detection {t_process * 1000:.0f}ms, "
                    f"status={driver_status}"
                )

    except (KeyboardInterrupt, SystemExit):
        log_info("Shutting down...")
    finally:
        # Graceful shutdown — force buzzer+LED off FIRST, then clean up rest
        buzzer_force_cleanup()
        led_force_cleanup()
        stop_continuous_buzz()
        stop_blinking()
        shutdown_event_capture()
        try:
            facial_tracker.close()
        except Exception:
            pass
        cap.release()
        _fan_shutdown()
        GPIO.cleanup()
        cv2.destroyAllWindows()
        log_info("Facial tracking stopped")

if __name__ == '__main__':
    acquire_lock()
    main()
