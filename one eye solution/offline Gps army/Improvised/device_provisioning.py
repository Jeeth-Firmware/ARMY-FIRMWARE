#!/usr/bin/env python3
"""
Offline vehicle registration — SQLite version.

The device never talks to a cloud backend. It is identified by the vehicle it
is fitted to (entered in the web UI) plus the Raspberry Pi hardware serial.
"""

import logging
import os
import re
import shutil
import sys
import time

import db_helper
from get_configure import get_configure, set_configure
from get_device_id import get_device_serial

logger = logging.getLogger("device_provisioning")

VEHICLE_NUMBER_KEY = "vehicle_number"
DRIVER_NAME_KEY = "driver_name"
VEHICLE_NUMBER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9 -]{1,19}$")
DRIVER_NAME_MAX_LEN = 40
IMAGES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")


def normalize_vehicle_number(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().upper())


def get_vehicle_info() -> dict:
    return {
        "vehicle_number": get_configure(VEHICLE_NUMBER_KEY) or "",
        "driver_name": get_configure(DRIVER_NAME_KEY) or "",
        "device_serial": get_device_serial(),
    }


def is_vehicle_registered() -> bool:
    return bool(get_configure(VEHICLE_NUMBER_KEY))


def save_vehicle_info(vehicle_number, driver_name="") -> tuple:
    """Validate and store vehicle details. Returns (ok, error_message)."""
    number = normalize_vehicle_number(vehicle_number)
    if not VEHICLE_NUMBER_PATTERN.match(number):
        return False, "Vehicle number must be 2-20 letters/digits (spaces and - allowed)"
    driver = re.sub(r"\s+", " ", str(driver_name or "").strip())
    if len(driver) > DRIVER_NAME_MAX_LEN:
        return False, f"Driver name must be at most {DRIVER_NAME_MAX_LEN} characters"
    if not set_configure(VEHICLE_NUMBER_KEY, number) or not set_configure(DRIVER_NAME_KEY, driver):
        return False, "Could not save to the device database"
    logger.info("Vehicle registered: %s (driver=%s)", number, driver or "-")
    return True, ""


def factory_reset() -> bool:
    """Clear vehicle details and all recorded data (GPS, trips, events, images).

    Detection settings (speed, alerts, timings) are kept.
    """
    def _execute_commit_with_retry(query, params=None, retries=5, delay_sec=0.35):
        """Retry writes to handle temporary SQLite lock contention."""
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                db_helper.execute_commit(query, params)
                return True
            except Exception as e:
                last_error = e
                err = str(e).lower()
                if ("locked" in err or "busy" in err) and attempt < retries:
                    time.sleep(delay_sec)
                    continue
                break
        logger.error(f"DB write failed after {retries} attempts: {query} ({last_error})")
        return False

    try:
        if not _execute_commit_with_retry(
            "DELETE FROM configure WHERE config_key IN (?, ?)",
            (VEHICLE_NUMBER_KEY, DRIVER_NAME_KEY),
        ):
            return False
        for table in ("device", "user_info", "gps_data", "gps_trips", "car_data"):
            try:
                _execute_commit_with_retry(f"DELETE FROM {table}")
            except Exception as e:
                logger.warning(f"Could not clear {table}: {e}")

        # Do not manually remove WAL/SHM files while app/services are live.
        # That can trigger SQLite "disk I/O error" under concurrent access.
        try:
            db_helper.close()
        except Exception as e:
            logger.warning(f"Could not reset database connection: {e}")

        if os.path.exists(IMAGES_PATH):
            for item in os.listdir(IMAGES_PATH):
                item_path = os.path.join(IMAGES_PATH, item)
                try:
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    else:
                        os.remove(item_path)
                except OSError as e:
                    logger.warning(f"Could not remove {item_path}: {e}")

        logger.info("Factory reset complete")
        return True
    except Exception as e:
        logger.error(f"Factory reset failed: {e}")
        return False


if __name__ == "__main__":
    info = get_vehicle_info()
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        print(f"VEHICLE: {info['vehicle_number']}" if info["vehicle_number"] else "NOT_REGISTERED")
        sys.exit(0 if info["vehicle_number"] else 1)
    print(info)
