"""
SQLite database helper — replaces mysql.connector throughout the codebase.
Thread-safe: uses one connection per thread via threading.local().
WAL mode: allows concurrent reads while writing.
"""
import sqlite3
import os
import threading
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "blinksmart.db"))

_local = threading.local()


def get_connection():
    """Get a thread-local SQLite connection with WAL mode enabled."""
    if not hasattr(_local, 'conn') or _local.conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _local.conn = sqlite3.connect(DB_PATH, timeout=10)
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.execute("PRAGMA cache_size=-4000")  # 4MB page cache
        _local.conn.execute("PRAGMA busy_timeout=5000")
        _local.conn.row_factory = sqlite3.Row  # Dict-like rows
    return _local.conn


def execute(query, params=None):
    """Execute a query and return the cursor."""
    conn = get_connection()
    if params:
        return conn.execute(query, params)
    return conn.execute(query)


def execute_commit(query, params=None):
    """Execute a query and commit."""
    conn = get_connection()
    try:
        if params:
            conn.execute(query, params)
        else:
            conn.execute(query)
        conn.commit()
    except Exception as e:
        logger.error(f"DB execute_commit error: {e}")
        raise


def fetchone(query, params=None):
    """Execute and fetch one row."""
    cursor = execute(query, params)
    return cursor.fetchone()


def fetchall(query, params=None):
    """Execute and fetch all rows."""
    cursor = execute(query, params)
    return cursor.fetchall()


def close():
    """Close the thread-local connection."""
    if hasattr(_local, 'conn') and _local.conn is not None:
        _local.conn.close()
        _local.conn = None
