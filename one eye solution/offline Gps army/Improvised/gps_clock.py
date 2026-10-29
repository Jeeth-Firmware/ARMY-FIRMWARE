"""Keep the system clock on GPS time.

The Pi Zero has no RTC battery: after every power cut it boots with the last
time fake-hwclock saved, so event folders and image timestamps fall behind by
however long the vehicle was switched off. A valid GPS fix carries exact UTC,
so the clock is corrected from it whenever the two drift apart.
"""
import logging
import os
import subprocess
import time
from datetime import datetime, timezone

GPS_CLOCK_SYNC = os.getenv("GPS_CLOCK_SYNC", "1") == "1"
MAX_DRIFT_SECONDS = float(os.getenv("GPS_CLOCK_MAX_DRIFT_SECONDS", "30"))
CHECK_INTERVAL_SECONDS = 60.0
FAILURE_BACKOFF_SECONDS = 600.0
CONFIRM_TOLERANCE_SECONDS = 5.0
MIN_VALID_YEAR = 2024
MAX_VALID_YEAR = 2099

logger = logging.getLogger("gps_clock")


def utc_from_rmc(msg):
    """UTC datetime from a pynmea2 RMC sentence with a valid fix, else None."""
    try:
        if getattr(msg, "status", None) != "A":
            return None
        date_part = getattr(msg, "datestamp", None)
        time_part = getattr(msg, "timestamp", None)
        if not date_part or time_part is None:
            return None
        return datetime.combine(date_part, time_part.replace(tzinfo=None), tzinfo=timezone.utc)
    except Exception:
        return None


def utc_from_gpsd_tpv(tpv):
    """UTC datetime from a gpsd TPV report with at least a 2D fix, else None."""
    try:
        if int(tpv.get("mode", 0)) < 2:
            return None
        text = tpv.get("time")
        if not text or text == "n/a":
            return None
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


class GpsClockSync:
    def __init__(self, enabled=GPS_CLOCK_SYNC, max_drift=MAX_DRIFT_SECONDS, runner=subprocess.run):
        self.enabled = enabled
        self.max_drift = max_drift
        self._run = runner
        self._next_check = 0.0
        self._pending_offset = None

    def offer(self, gps_utc):
        """Feed UTC time from a valid fix. Returns True when the clock was set."""
        if not self.enabled or gps_utc is None:
            return False
        now_mono = time.monotonic()
        if now_mono < self._next_check:
            return False
        if not MIN_VALID_YEAR <= gps_utc.year <= MAX_VALID_YEAR:
            return False

        gps_epoch = gps_utc.timestamp()
        drift = gps_epoch - time.time()
        if abs(drift) <= self.max_drift:
            self._pending_offset = None
            self._next_check = now_mono + CHECK_INTERVAL_SECONDS
            return False

        # Jump the clock only after a second fix agrees, so one bad sentence
        # (e.g. a receiver still settling after cold start) can't move it.
        offset = gps_epoch - now_mono
        if self._pending_offset is None or abs(offset - self._pending_offset) > CONFIRM_TOLERANCE_SECONDS:
            self._pending_offset = offset
            return False
        self._pending_offset = None

        if self._set_clock(gps_epoch, drift):
            self._next_check = now_mono + CHECK_INTERVAL_SECONDS
            return True
        self._next_check = now_mono + FAILURE_BACKOFF_SECONDS
        return False

    def _set_clock(self, gps_epoch, drift):
        try:
            result = self._run(["sudo", "-n", "date", "-s", f"@{gps_epoch:.3f}"],
                               capture_output=True, text=True, timeout=10)
        except Exception as e:
            logger.warning("GPS clock sync failed: %s", e)
            return False
        if result.returncode != 0:
            logger.warning("GPS clock sync failed: %s", (result.stderr or "").strip() or result.returncode)
            return False
        try:
            self._run(["sudo", "-n", "fake-hwclock", "save"], capture_output=True, timeout=10)
        except Exception:
            pass
        logger.info("System clock set from GPS to %s UTC (was off by %+.0f s)",
                    datetime.fromtimestamp(gps_epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), drift)
        return True
