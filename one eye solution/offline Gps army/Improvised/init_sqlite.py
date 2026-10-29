#!/usr/bin/env python3
"""Initialize SQLite database — replaces MySQL init_database.sql"""
import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "blinksmart.db"))


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Device table (provisioning)
    c.execute("""
        CREATE TABLE IF NOT EXISTS device (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE NOT NULL,
            auth_key TEXT,
            device_type TEXT DEFAULT 'DM',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # User info (legacy compatibility)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT,
            access_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # GPS data
    c.execute("""
        CREATE TABLE IF NOT EXISTS gps_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            latitude REAL,
            longitude REAL,
            speed REAL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            driver_status TEXT,
            acceleration REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_gps_timestamp ON gps_data(timestamp)")

    # Offline GPS trip sessions (one fresh session per GPS service/device restart)
    c.execute("""
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
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_gps_trip_status ON gps_trips(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gps_trip_start ON gps_trips(start_time)")

    # Car data (driver status log)
    c.execute("""
        CREATE TABLE IF NOT EXISTS car_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            driver_status TEXT
        )
    """)

    # Configure (key-value settings)
    c.execute("""
        CREATE TABLE IF NOT EXISTS configure (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_key TEXT UNIQUE NOT NULL,
            config_value TEXT
        )
    """)

    # Count table (daily event counts)
    c.execute("""
        CREATE TABLE IF NOT EXISTS count_table (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            count INTEGER DEFAULT 0
        )
    """)

    # Driver usability (facial DMS + OBD fused score)
    c.execute("""
        CREATE TABLE IF NOT EXISTS driver_usability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            usability_score REAL,
            usability_rating TEXT,
            usability_status TEXT,
            attention_score REAL,
            driving_score REAL,
            efficiency_score REAL,
            driver_status TEXT,
            facial_detected INTEGER DEFAULT 0,
            speed REAL,
            acceleration REAL,
            distance_km REAL,
            fuel_km_per_l REAL,
            harsh_accel_count INTEGER DEFAULT 0,
            harsh_brake_count INTEGER DEFAULT 0,
            factors_json TEXT
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_usability_timestamp ON driver_usability(timestamp)"
    )

    # Extend gps_data with usability columns (safe migration for existing DBs)
    for col_def in (
        "usability_score REAL",
        "usability_rating TEXT",
        "usability_status TEXT",
        "trip_id INTEGER",
        "movement_status TEXT",
    ):
        try:
            c.execute(f"ALTER TABLE gps_data ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_gps_data_trip ON gps_data(trip_id, timestamp)")

    # Insert defaults
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('speed', '0')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('speed_limit', '0')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('alert_enabled', '1')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('upload_interval', '30')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('gps_retention_days', '30')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('image_retention_days', '15')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('led_blink_enabled', '1')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('noface_enabled', '0')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('noface_threshold', '2')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('sleep_seconds', '1.5')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('phone_detection_enabled', '1')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('phone_confirm_seconds', '0.3')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('phone_buzz_seconds', '10')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('phone_ear_dist_ratio', '0.5')")

    conn.commit()
    conn.close()
    print(f"SQLite database initialized: {DB_PATH}")


if __name__ == "__main__":
    init_db()
