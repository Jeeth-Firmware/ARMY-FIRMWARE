"""Offline GPS trip sessions and geo-tracing reports."""
import math
import os
import socket
import time
from datetime import datetime

import db_helper


STOP_SPEED_KMH = float(os.getenv("GPS_STOP_SPEED_KMH", "2.0"))
STOP_MIN_SECONDS = int(os.getenv("GPS_STOP_MIN_SECONDS", "300"))

_schema_ready = False
_active_trip_id = None


def ensure_trip_schema():
    """Create/migrate trip tables. Safe to call from multiple services."""
    global _schema_ready
    if _schema_ready:
        return

    db_helper.execute_commit(
        """
        CREATE TABLE IF NOT EXISTS gps_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            latitude REAL,
            longitude REAL,
            speed REAL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            driver_status TEXT,
            acceleration REAL
        )
        """
    )
    db_helper.execute_commit("CREATE INDEX IF NOT EXISTS idx_gps_timestamp ON gps_data(timestamp)")

    for col_def in (
        "trip_id INTEGER",
        "movement_status TEXT",
    ):
        try:
            db_helper.execute_commit(f"ALTER TABLE gps_data ADD COLUMN {col_def}")
        except Exception:
            pass

    db_helper.execute_commit(
        """
        CREATE TABLE IF NOT EXISTS gps_trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP,
            status TEXT DEFAULT 'active',
            start_latitude REAL,
            start_longitude REAL,
            end_latitude REAL,
            end_longitude REAL,
            point_count INTEGER DEFAULT 0,
            total_distance_km REAL DEFAULT 0,
            created_by TEXT
        )
        """
    )
    db_helper.execute_commit("CREATE INDEX IF NOT EXISTS idx_gps_trip_status ON gps_trips(status)")
    db_helper.execute_commit("CREATE INDEX IF NOT EXISTS idx_gps_trip_start ON gps_trips(start_time)")
    db_helper.execute_commit("CREATE INDEX IF NOT EXISTS idx_gps_data_trip ON gps_data(trip_id, timestamp)")
    _schema_ready = True


def start_new_trip():
    """Close stale active trips and create a fresh trip for this process start."""
    global _active_trip_id
    ensure_trip_schema()
    now = _now_text()
    db_helper.execute_commit(
        """
        UPDATE gps_trips
        SET status = 'closed', end_time = COALESCE(end_time, ?)
        WHERE status = 'active'
        """,
        (now,),
    )
    session_id = f"{socket.gethostname()}-{int(time.time())}-{os.getpid()}"
    db_helper.execute_commit(
        """
        INSERT INTO gps_trips (session_id, start_time, status, created_by)
        VALUES (?, ?, 'active', ?)
        """,
        (session_id, now, "get_gps_data"),
    )
    row = db_helper.fetchone("SELECT id FROM gps_trips WHERE session_id = ?", (session_id,))
    _active_trip_id = int(row["id"]) if row else None
    return _active_trip_id


def get_active_trip_id():
    global _active_trip_id
    ensure_trip_schema()
    if _active_trip_id:
        return _active_trip_id
    row = db_helper.fetchone("SELECT id FROM gps_trips WHERE status = 'active' ORDER BY id DESC LIMIT 1")
    if row:
        _active_trip_id = int(row["id"])
        return _active_trip_id
    return start_new_trip()


def close_active_trip():
    ensure_trip_schema()
    row = db_helper.fetchone("SELECT id FROM gps_trips WHERE status = 'active' ORDER BY id DESC LIMIT 1")
    if row:
        finalize_trip(int(row["id"]), status="closed")


def record_trip_point(gps_row_id, latitude, longitude, speed):
    """Attach a gps_data row to the active trip and update trip summary fields.

    The summary is updated incrementally (one haversine step from the trip's
    current end point) so the cost stays constant however long the trip gets.
    Commits any pending insert on this thread's connection in the same
    transaction.
    """
    ensure_trip_schema()
    trip_id = get_active_trip_id()
    if not trip_id or not gps_row_id:
        return None

    lat = _as_float(latitude)
    lon = _as_float(longitude)
    movement_status = "idle" if _as_float(speed) <= STOP_SPEED_KMH else "moving"
    conn = db_helper.get_connection()
    try:
        trip = conn.execute(
            "SELECT point_count, end_latitude, end_longitude FROM gps_trips WHERE id = ?",
            (trip_id,),
        ).fetchone()
        step_km = 0.0
        if trip and trip["point_count"] and trip["end_latitude"] is not None and trip["end_longitude"] is not None:
            step_km = _haversine_km(trip["end_latitude"], trip["end_longitude"], lat, lon)

        conn.execute(
            "UPDATE gps_data SET trip_id = ?, movement_status = ? WHERE id = ?",
            (trip_id, movement_status, gps_row_id),
        )
        conn.execute(
            """
            UPDATE gps_trips
            SET start_latitude = COALESCE(start_latitude, ?),
                start_longitude = COALESCE(start_longitude, ?),
                end_latitude = ?, end_longitude = ?,
                point_count = COALESCE(point_count, 0) + 1,
                total_distance_km = COALESCE(total_distance_km, 0) + ?,
                end_time = COALESCE((SELECT timestamp FROM gps_data WHERE id = ?), end_time)
            WHERE id = ?
            """,
            (lat, lon, lat, lon, step_km, gps_row_id, trip_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return trip_id


def finalize_trip(trip_id, status="closed"):
    ensure_trip_schema()
    _refresh_trip_summary(trip_id)
    db_helper.execute_commit(
        """
        UPDATE gps_trips
        SET status = ?, end_time = COALESCE(
            (SELECT MAX(timestamp) FROM gps_data WHERE trip_id = ?),
            end_time,
            ?
        )
        WHERE id = ?
        """,
        (status, trip_id, _now_text(), trip_id),
    )


def list_trips(limit=50):
    ensure_trip_schema()
    rows = db_helper.fetchall(
        """
        SELECT *
        FROM gps_trips
        WHERE status = 'active' OR COALESCE(point_count, 0) > 0
        ORDER BY datetime(start_time) DESC, id DESC
        LIMIT ?
        """,
        (int(limit),),
    )
    return [_trip_to_dict(row) for row in rows]


def get_trip_report(trip_id):
    ensure_trip_schema()
    trip = db_helper.fetchone("SELECT * FROM gps_trips WHERE id = ?", (int(trip_id),))
    if not trip:
        return None

    rows = db_helper.fetchall(
        """
        SELECT id, latitude, longitude, speed, timestamp, movement_status
        FROM gps_data
        WHERE trip_id = ? AND latitude IS NOT NULL AND longitude IS NOT NULL
        ORDER BY datetime(timestamp), id
        """,
        (int(trip_id),),
    )
    points = [_point_to_dict(row) for row in rows]
    distance_km = _route_distance_km(points)
    stops = _detect_stops(points)

    out = _trip_to_dict(trip)
    if points:
        out.update(
            {
                "start_location": _location(points[0]),
                "end_location": _location(points[-1]),
                "start_time": points[0]["timestamp"] or out.get("start_time"),
                "end_time": points[-1]["timestamp"] or out.get("end_time"),
            }
        )
    out.update(
        {
            "points": points,
            "stops": stops,
            "point_count": len(points),
            "total_distance_km": round(distance_km, 3),
            "bounds": _bounds(points),
        }
    )
    return out


def _refresh_trip_summary(trip_id):
    """Full recompute, streamed row by row so a long trip can't exhaust memory."""
    first = last = None
    count = 0
    distance_km = 0.0
    cursor = db_helper.execute(
        """
        SELECT latitude, longitude, timestamp
        FROM gps_data
        WHERE trip_id = ? AND latitude IS NOT NULL AND longitude IS NOT NULL
        ORDER BY id
        """,
        (int(trip_id),),
    )
    for row in cursor:
        point = (_as_float(row["latitude"]), _as_float(row["longitude"]), row["timestamp"])
        if last:
            distance_km += _haversine_km(last[0], last[1], point[0], point[1])
        else:
            first = point
        last = point
        count += 1
    if not count:
        return
    db_helper.execute_commit(
        """
        UPDATE gps_trips
        SET start_latitude = ?, start_longitude = ?,
            end_latitude = ?, end_longitude = ?,
            point_count = ?, total_distance_km = ?,
            end_time = ?
        WHERE id = ?
        """,
        (
            first[0],
            first[1],
            last[0],
            last[1],
            count,
            round(distance_km, 3),
            str(last[2]) if last[2] is not None else None,
            int(trip_id),
        ),
    )


def _detect_stops(points):
    stops = []
    current = []
    for point in points:
        if (point.get("speed") or 0.0) <= STOP_SPEED_KMH:
            current.append(point)
        else:
            _append_stop(stops, current)
            current = []
    _append_stop(stops, current)
    return stops


def _append_stop(stops, points):
    if len(points) < 2:
        return
    start_ts = _parse_time(points[0].get("timestamp"))
    end_ts = _parse_time(points[-1].get("timestamp"))
    if not start_ts or not end_ts:
        return
    duration = int((end_ts - start_ts).total_seconds())
    if duration < STOP_MIN_SECONDS:
        return
    lat = sum(p["latitude"] for p in points) / len(points)
    lon = sum(p["longitude"] for p in points) / len(points)
    stops.append(
        {
            "latitude": round(lat, 7),
            "longitude": round(lon, 7),
            "start_time": points[0]["timestamp"],
            "end_time": points[-1]["timestamp"],
            "duration_seconds": duration,
            "point_count": len(points),
        }
    )


def _route_distance_km(points):
    total = 0.0
    prev = None
    for point in points:
        if prev:
            total += _haversine_km(prev["latitude"], prev["longitude"], point["latitude"], point["longitude"])
        prev = point
    return total


def _haversine_km(lat1, lon1, lat2, lon2):
    radius_km = 6371.0088
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lam = math.radians(float(lon2) - float(lon1))
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2.0) ** 2
    return radius_km * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _bounds(points):
    if not points:
        return None
    lats = [p["latitude"] for p in points]
    lons = [p["longitude"] for p in points]
    return {
        "min_latitude": min(lats),
        "max_latitude": max(lats),
        "min_longitude": min(lons),
        "max_longitude": max(lons),
    }


def _point_to_dict(row):
    return {
        "id": row["id"],
        "latitude": _as_float(row["latitude"]),
        "longitude": _as_float(row["longitude"]),
        "speed": _as_float(row["speed"]),
        "timestamp": str(row["timestamp"]) if row["timestamp"] is not None else None,
        "movement_status": row["movement_status"] or ("idle" if _as_float(row["speed"]) <= STOP_SPEED_KMH else "moving"),
    }


def _trip_to_dict(row):
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "start_time": str(row["start_time"]) if row["start_time"] is not None else None,
        "end_time": str(row["end_time"]) if row["end_time"] is not None else None,
        "status": row["status"],
        "start_location": {"latitude": row["start_latitude"], "longitude": row["start_longitude"]},
        "end_location": {"latitude": row["end_latitude"], "longitude": row["end_longitude"]},
        "point_count": row["point_count"] or 0,
        "total_distance_km": row["total_distance_km"] or 0.0,
        "created_by": row["created_by"],
    }


def _location(point):
    return {"latitude": point["latitude"], "longitude": point["longitude"]}


def _as_float(value):
    try:
        return float(value)
    except Exception:
        return 0.0


def _parse_time(value):
    if not value:
        return None
    text = str(value).replace("T", " ").split(".")[0]
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
