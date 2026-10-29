"""Device configuration management — SQLite version."""
import db_helper

# Default configuration values
DEFAULT_CONFIG = {
    'speed': '20',  # Activation speed threshold (km/h)
    'speed_limit': '0',  # Speed limit threshold (km/h) — buzzer when exceeded (0=disabled)
    'gps_retention_days': '30',  # GPS data retention in active days
    'image_retention_days': '15',  # Image retention in days
    'led_blink_enabled': '1',  # LED blink on sleep/yawn (1=on, 0=off)
    'noface_enabled': '0',  # NoFace buzzer alert (1=on, 0=off)
    'noface_threshold': '2',  # Seconds before NoFace buzzer triggers
    'sleep_seconds': '1.5',  # Both eyes closed this long before Sleeping alert
    'phone_detection_enabled': '1',  # Hand-near-ear phone call detection
    'phone_confirm_seconds': '0.3',  # Hand-at-ear hold before PhoneCall alert (0 = instant)
    'phone_buzz_seconds': '10',  # Buzz for first N seconds of a call (0 = no buzz)
    'phone_ear_dist_ratio': '0.5',  # Hand-to-ear distance (x face width) counted as "at ear"
    'lookdown_enabled': '1',  # Head-pose LookingDown / LookingAway
    'lookdown_pitch_threshold': '20',
    'lookaway_yaw_threshold': '25',
    'headtilt_roll_threshold': '20',
    'lookdown_frames': '8',
    'lookaway_frames': '8',
    'headtilt_frames': '10',
    'lookaway_one_eye_seconds': '3',  # LookingAway + one eye closed hold before buzz
}


def get_configure(config_key):
    try:
        row = db_helper.fetchone(
            "SELECT config_value FROM configure WHERE config_key = ?",
            (config_key,))
        if row:
            return row['config_value']
        return DEFAULT_CONFIG.get(config_key) or None
    except Exception as e:
        print("Error:", e)
        return None


def set_configure(config_key, config_value):
    """Set or update a configuration value in the database"""
    try:
        row = db_helper.fetchone(
            "SELECT config_value FROM configure WHERE config_key = ?",
            (config_key,))

        if row:
            db_helper.execute_commit(
                "UPDATE configure SET config_value = ? WHERE config_key = ?",
                (str(config_value), config_key))
        else:
            db_helper.execute_commit(
                "INSERT INTO configure (config_key, config_value) VALUES (?, ?)",
                (config_key, str(config_value)))

        print(f"Set '{config_key}' = '{config_value}'")
        return True
    except Exception as e:
        print(f"Error setting config: {e}")
        return False


def ensure_configure_table():
    """Ensure configure table exists (handled by init_sqlite.py, kept for compatibility)"""
    try:
        db_helper.execute_commit("""
            CREATE TABLE IF NOT EXISTS configure (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_key TEXT UNIQUE NOT NULL,
                config_value TEXT
            )
        """)
        return True
    except Exception as e:
        print(f"Error ensuring configure table: {e}")
        return False
