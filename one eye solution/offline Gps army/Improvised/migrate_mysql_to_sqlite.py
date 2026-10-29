#!/usr/bin/env python3
"""
One-time migration: MySQL -> SQLite

Run this on devices that have existing MySQL data before switching to SQLite.
Requires mysql-connector-python to be installed temporarily:
    pip install mysql-connector-python
    python3 migrate_mysql_to_sqlite.py
    pip uninstall mysql-connector-python
"""
import sqlite3
import os
import sys

# First initialize the SQLite schema
import init_sqlite
init_sqlite.init_db()

try:
    import mysql.connector
except ImportError:
    print("ERROR: mysql-connector-python not installed.")
    print("Run: pip install mysql-connector-python")
    sys.exit(1)

SQLITE_PATH = os.getenv("DB_PATH", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "blinksmart.db"))

MYSQL_HOST = os.getenv("DB_HOST", "127.0.0.1")
MYSQL_DB = os.getenv("DB_NAME", "car")
MYSQL_USER = os.getenv("DB_USER", "root")
MYSQL_PASS = os.getenv("DB_PASSWORD", "raspberry@123")


def migrate():
    print(f"Migrating MySQL ({MYSQL_DB}@{MYSQL_HOST}) -> SQLite ({SQLITE_PATH})")

    try:
        mysql_conn = mysql.connector.connect(
            host=MYSQL_HOST, database=MYSQL_DB,
            user=MYSQL_USER, password=MYSQL_PASS
        )
    except Exception as e:
        print(f"ERROR: Cannot connect to MySQL: {e}")
        sys.exit(1)

    sqlite_conn = sqlite3.connect(SQLITE_PATH)

    tables = ['device', 'user_info', 'gps_data', 'car_data', 'configure', 'count_table']

    for table in tables:
        try:
            mc = mysql_conn.cursor(dictionary=True)
            mc.execute(f"SELECT * FROM {table}")
            rows = mc.fetchall()
            if not rows:
                print(f"  {table}: empty, skipping")
                mc.close()
                continue

            columns = list(rows[0].keys())
            # Skip 'id' column — let SQLite auto-increment
            columns_no_id = [c for c in columns if c != 'id']
            placeholders = ','.join(['?' for _ in columns_no_id])
            col_names = ','.join(columns_no_id)

            for row in rows:
                values = [row[c] for c in columns_no_id]
                sqlite_conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})",
                    values
                )

            sqlite_conn.commit()
            print(f"  {table}: migrated {len(rows)} rows")
            mc.close()
        except Exception as e:
            print(f"  {table}: error - {e}")

    mysql_conn.close()
    sqlite_conn.close()
    print("Migration complete!")


if __name__ == "__main__":
    migrate()
