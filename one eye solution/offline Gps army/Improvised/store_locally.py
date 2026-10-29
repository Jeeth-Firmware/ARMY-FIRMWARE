"""Local storage — SQLite version (no connection pool needed)."""
import math
import os
import time
from datetime import datetime
import db_helper
import gps_trip_manager

GPS_COORD_DECIMALS = int(os.getenv('GPS_COORD_DECIMALS', '7'))
GPS_MIN_STORE_DISTANCE_METERS = float(os.getenv('GPS_MIN_STORE_DISTANCE_METERS', '25.0'))
GPS_IDLE_SPEED_KMH = float(os.getenv('GPS_IDLE_SPEED_KMH', '5.0'))
GPS_MAX_STORE_SPEED_KMH = float(os.getenv('GPS_MAX_STORE_SPEED_KMH', '180.0'))


def _format_coord(value):
    try:
        return f"{float(value):.{GPS_COORD_DECIMALS}f}"
    except Exception:
        return None


def _as_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _distance_meters(lat1, lon1, lat2, lon2):
    radius_m = 6371008.8
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lam = math.radians(float(lon2) - float(lon1))
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2.0) ** 2
    return radius_m * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


# Define a global variable to store the last insertion time
last_insertion_time = float("-inf")


def create_database():
    pass  # Handled by init_sqlite.py


def create_table():
    pass  # Handled by init_sqlite.py


def add_row(driver_status):
    try:
        db_helper.execute_commit(
            "INSERT INTO car_data (driver_status) VALUES (?)",
            (driver_status,))
    except Exception as e:
        print(f'Error adding row: {e}')


def add_row_new(driver_status):
    global last_insertion_time
    try:
        current_time = time.monotonic()
        if (current_time - last_insertion_time) > 5:
            db_helper.execute_commit(
                "INSERT INTO car_data (driver_status) VALUES (?)",
                (driver_status,))
            last_insertion_time = current_time
    except Exception as e:
        print(f'Error adding row: {e}')


def add_gps_data(latitude, longitude, speed, timestamp, driver_status, acceleration):
    try:
        lat = float(latitude) if latitude else 0.0
        lng = float(longitude) if longitude else 0.0
        if lat == 0.0 or lng == 0.0:
            return
        conn = db_helper.get_connection()
        cur = conn.execute(
            "INSERT INTO gps_data (latitude, longitude, speed, timestamp, driver_status, acceleration) "
            "VALUES (?,?,?,?,?,?)",
            (latitude, longitude, speed, timestamp, driver_status, acceleration),
        )
        gps_trip_manager.record_trip_point(cur.lastrowid, latitude, longitude, speed)
        conn.commit()
    except Exception as e:
        print(f'Error adding row: {e}')


def get_last_gps_data():
    try:
        row = db_helper.fetchone(
            "SELECT latitude, longitude FROM gps_data ORDER BY id DESC LIMIT 1")
        if row:
            return (row['latitude'], row['longitude'])
        return None
    except Exception as e:
        print(f'Error getting last GPS data: {e}')
        return None


def _get_last_gps_sample():
    try:
        return db_helper.fetchone(
            "SELECT latitude, longitude, speed, timestamp FROM gps_data ORDER BY id DESC LIMIT 1")
    except Exception as e:
        print(f'Error getting last GPS sample: {e}')
        return None


def _should_store_gps_point(latitude, longitude, speed, last_sample):
    if not last_sample:
        return True

    last_lat = last_sample['latitude']
    last_lng = last_sample['longitude']
    if last_lat is None or last_lng is None:
        return True

    distance_m = _distance_meters(last_lat, last_lng, latitude, longitude)
    if distance_m < GPS_MIN_STORE_DISTANCE_METERS:
        return False

    current_speed = _as_float(speed)
    last_speed = _as_float(last_sample['speed'])

    # Indoor/parked GPS fixes can jump tens or hundreds of meters. Keep the
    # latest live fix flowing elsewhere, but do not turn idle jumps into trips.
    if current_speed < GPS_IDLE_SPEED_KMH and last_speed < GPS_IDLE_SPEED_KMH:
        return False

    if current_speed > GPS_MAX_STORE_SPEED_KMH:
        return False

    return True


def add_gps_data_if_changed(latitude, longitude, speed):
    try:
        lat = float(latitude) if latitude else 0.0
        lng = float(longitude) if longitude else 0.0
        if lat == 0.0 or lng == 0.0:
            return

        formatted_latitude = _format_coord(latitude)
        formatted_longitude = _format_coord(longitude)

        last_sample = _get_last_gps_sample()
        if _should_store_gps_point(formatted_latitude, formatted_longitude, speed, last_sample):
            conn = db_helper.get_connection()
            # Local time like every other gps_data/gps_trips writer; the column's
            # CURRENT_TIMESTAMP default is UTC and would split trips by the TZ offset.
            cur = conn.execute(
                "INSERT INTO gps_data (latitude, longitude, speed, timestamp) VALUES (?,?,?,?)",
                (formatted_latitude, formatted_longitude, speed,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            gps_trip_manager.record_trip_point(cur.lastrowid, formatted_latitude, formatted_longitude, speed)
            conn.commit()
    except Exception as e:
        print(f'Error adding GPS data: {e}')


def update_count(date):
    try:
        row = db_helper.fetchone(
            "SELECT id, count FROM count_table WHERE date = ?", (date,))

        if row is None:
            db_helper.execute_commit(
                "INSERT INTO count_table (date, count) VALUES (?, 1)", (date,))
            return 'added'
        else:
            db_helper.execute_commit(
                "UPDATE count_table SET count = count + 1 WHERE id = ?",
                (row['id'],))
            return 'existed'
    except Exception as e:
        print(f'Error: {e}')
        return 'error'


def fetch_rows():
    try:
        rows = db_helper.fetchall(
            "SELECT * FROM car_data WHERE timestamp >= datetime('now', '-15 minutes')")
        return rows if rows else []
    except Exception as e:
        print(f'Error fetching rows: {e}')
        return []


def analyze_data(rows):
    status_counts = {}
    for row in rows:
        driver_status = row['driver_status']
        if driver_status in status_counts:
            status_counts[driver_status] += 1
        else:
            status_counts[driver_status] = 1
    return status_counts
