"""Device identity — hardware serial (offline) plus legacy cloud ID lookup."""
import db_helper
import logging

logger = logging.getLogger(__name__)


def get_device_serial() -> str:
    """Raspberry Pi CPU serial — stable hardware identity, no network needed."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("serial"):
                    return line.split(":", 1)[1].strip().lstrip("0").upper()
    except OSError:
        pass
    return ""


def get_device_id_from_db():
    """Get device_id from local SQLite database"""
    try:
        row = db_helper.fetchone("SELECT device_id FROM device LIMIT 1")
        if row:
            return row['device_id']
        else:
            logger.warning("No device_id found in database")
            return None
    except Exception as e:
        logger.error(f"Database error: {e}")
        return None


def get_auth_key_from_db():
    """Get auth_key from local SQLite database"""
    try:
        row = db_helper.fetchone("SELECT auth_key FROM device LIMIT 1")
        if row:
            return row['auth_key']
        else:
            logger.warning("No auth_key found in database")
            return None
    except Exception as e:
        logger.error(f"Database error: {e}")
        return None


def get_device_credentials():
    """Get both device_id and auth_key from database

    Returns:
        dict: {"device_id": str, "auth_key": str} or None if not found
    """
    try:
        row = db_helper.fetchone("SELECT device_id, auth_key FROM device LIMIT 1")
        if row and row['device_id'] and row['auth_key']:
            return {"device_id": row['device_id'], "auth_key": row['auth_key']}
        else:
            logger.warning("Device not provisioned (missing device_id or auth_key)")
            return None
    except Exception as e:
        logger.error(f"Database error: {e}")
        return None


def is_device_provisioned():
    """Check if device has been provisioned with device_id and auth_key"""
    creds = get_device_credentials()
    return creds is not None


# For backward compatibility and direct execution
if __name__ == "__main__":
    device_id = get_device_id_from_db()
    if device_id:
        print(f"Device ID: {device_id}")

    auth_key = get_auth_key_from_db()
    if auth_key:
        print(f"Auth Key: {auth_key[:8]}...{auth_key[-8:]}")

    print(f"Device serial: {get_device_serial() or 'unknown'}")
