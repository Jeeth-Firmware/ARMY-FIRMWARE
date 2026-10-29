# buzzer_controller.py
import time
import logging
import threading
import atexit
from gpiozero import Buzzer
from time import sleep

logger = logging.getLogger('buzzer')

buzzer1 = Buzzer(12)
buzzer2 = Buzzer(13)

# --- One-shot buzz (for NoFace alerts) ---
_buzz_lock = threading.Lock()
_buzz_active = False

def _do_buzz(duration):
    """Internal: run buzzer for duration, then turn off."""
    global _buzz_active
    try:
        buzzer1.on()
        buzzer2.on()
        sleep(duration)
        # Don't turn off if another buzz mode took over
        if not _continuous_buzz_active and not _speed_limit_buzz_active:
            buzzer1.off()
            buzzer2.off()
    finally:
        _buzz_active = False

def buzz_for(duration):
    """Buzz for a fixed duration (NoFace alerts) - non-blocking, self-limiting."""
    global _buzz_active
    with _buzz_lock:
        if _buzz_active:
            return
        _buzz_active = True
    threading.Thread(target=_do_buzz, args=(duration,), daemon=True).start()

# --- Continuous buzz with WATCHDOG (sleeping/yawning alerts) ---
# Two safety mechanisms to prevent stuck buzzer:
# 1. Watchdog: auto-stops if not refreshed within WATCHDOG_TIMEOUT
# 2. Max duration: force-stops after MAX_CONTINUOUS_DURATION regardless

_continuous_buzz_active = False
_continuous_buzz_thread = None
_speed_limit_buzz_active = False
_continuous_lock = threading.Lock()
_last_refresh_time = 0
_buzz_start_time = 0
WATCHDOG_TIMEOUT = 2.5        # Auto-stop after 2.5s without refresh
MAX_CONTINUOUS_DURATION = 60   # Hard limit: force-stop after 60s no matter what

def _continuous_buzz_loop():
    """Internal: beep on/off until stopped, watchdog timeout, or max duration."""
    global _continuous_buzz_active
    while _continuous_buzz_active:
        now = time.monotonic()
        # Watchdog: auto-stop if main loop stopped refreshing
        if now - _last_refresh_time > WATCHDOG_TIMEOUT:
            _continuous_buzz_active = False
            buzzer1.off()
            buzzer2.off()
            logger.info("Buzzer WATCHDOG TIMEOUT - auto-stopped")
            return
        # Safety valve: force-stop after max duration
        if now - _buzz_start_time > MAX_CONTINUOUS_DURATION:
            _continuous_buzz_active = False
            buzzer1.off()
            buzzer2.off()
            logger.info("Buzzer MAX DURATION reached (%ds) - force-stopped", MAX_CONTINUOUS_DURATION)
            return
        # Beep: short ON, then OFF. Buzzer spends more time OFF than ON
        # so if process freezes, it's more likely to freeze in OFF state
        buzzer1.on()
        buzzer2.on()
        sleep(0.12)
        buzzer1.off()
        buzzer2.off()
        sleep(0.1)

def start_continuous_buzz():
    """Start or refresh continuous beeping. Call every frame to keep alive."""
    global _continuous_buzz_active, _continuous_buzz_thread, _last_refresh_time, _buzz_start_time
    with _continuous_lock:
        if _speed_limit_buzz_active:
            return  # Overspeed solid buzz takes priority over pulsed mode
        _last_refresh_time = time.monotonic()  # Refresh watchdog inside lock
        if _continuous_buzz_active and _continuous_buzz_thread and _continuous_buzz_thread.is_alive():
            return  # Already running, just refreshed timestamp
        _continuous_buzz_active = True
        _buzz_start_time = time.monotonic()
        _continuous_buzz_thread = threading.Thread(target=_continuous_buzz_loop, daemon=True)
        _continuous_buzz_thread.start()
        logger.info("Continuous buzzer STARTED")

def stop_continuous_buzz():
    """Immediately stop continuous beeping."""
    global _continuous_buzz_active
    with _continuous_lock:
        if not _continuous_buzz_active:
            return
        _continuous_buzz_active = False
        if _speed_limit_buzz_active:
            buzzer1.on()
            buzzer2.on()
        else:
            buzzer1.off()
            buzzer2.off()
        logger.info("Continuous buzzer STOPPED")


def speed_limit_buzzer_on():
    """Solid continuous ON for overspeed — stays on until speed_limit_buzzer_off()."""
    global _speed_limit_buzz_active, _continuous_buzz_active
    with _continuous_lock:
        if _speed_limit_buzz_active:
            return
        _continuous_buzz_active = False  # Stop pulsed mode; solid overspeed takes over
        _speed_limit_buzz_active = True
        buzzer1.on()
        buzzer2.on()
        logger.info("Speed limit buzzer ON (solid)")


def speed_limit_buzzer_off():
    """Turn off overspeed solid buzz when speed is back within limit."""
    global _speed_limit_buzz_active
    with _continuous_lock:
        if not _speed_limit_buzz_active:
            return
        _speed_limit_buzz_active = False
        if not _continuous_buzz_active:
            buzzer1.off()
            buzzer2.off()
        logger.info("Speed limit buzzer OFF")


def force_cleanup():
    """Force all buzzers off unconditionally. Called on process exit/signal."""
    global _continuous_buzz_active, _speed_limit_buzz_active, _buzz_active
    _continuous_buzz_active = False
    _speed_limit_buzz_active = False
    _buzz_active = False
    try:
        buzzer1.off()
        buzzer2.off()
    except Exception:
        pass
    logger.info("Buzzer force cleanup done")


# Guarantee buzzers are OFF when process exits for any reason
atexit.register(force_cleanup)
