#!/usr/bin/env python3
"""
USB stick auto-export for offline Blinksmart / GPS Army units.

When a USB volume with a BLINKSMART_EXPORT marker is present (or is passed
via --device / --mount), this script:

  1. Safely copies SQLite (online backup — safe while writers run)
  2. Copies completed events one at a time into per-type folders
     (events/Sleeping/, events/Yawning/, ...) and writes events_summary.csv.
     Every SYNC_BATCH_EVENTS events the stick is synced and the verified source
     events are deleted, so an interrupted export still keeps its progress.
  3. Writes trips_summary.json, gps_points.csv, Vehicle_Report.pdf (event counts + sample
     snapshots per event type) and MANIFEST.json (checksums)
  4. sync() then unmounts if this script mounted the volume

Typical paths on-device:
  DB:     /home/pi/facial-tracker-firmware/data/blinksmart.db
  Events: /home/pi/facial-tracker-firmware/images/events

Prepare a stick once: put an empty file named BLINKSMART_EXPORT at the root.
"""
from __future__ import annotations

import argparse
import csv
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_DB = Path(os.getenv(
    "DB_PATH",
    str(SCRIPT_DIR / "data" / "blinksmart.db"),
))
DEFAULT_EVENTS = Path(os.getenv(
    "EVENTS_DIR",
    str(SCRIPT_DIR / "images" / "events"),
))
DEFAULT_MOUNT_POINT = Path(os.getenv(
    "USB_EXPORT_MOUNT_POINT",
    "/mnt/blinksmart-usb",
))
MARKER_NAME = os.getenv("USB_EXPORT_MARKER", "BLINKSMART_EXPORT")
EXPORT_PREFIX = os.getenv("USB_EXPORT_PREFIX", "blinksmart_export")
LOCK_PATH = Path(os.getenv("USB_EXPORT_LOCK", "/tmp/blinksmart-usb-export.lock"))
MOUNT_WAIT_SEC = float(os.getenv("USB_EXPORT_MOUNT_WAIT_SEC", "8"))
LOG_DIR = Path(os.getenv("USB_EXPORT_LOG_DIR", str(SCRIPT_DIR / "logs")))
IDLE_SPEED_KMH = float(os.getenv("GPS_STOP_SPEED_KMH", os.getenv("GPS_IDLE_SPEED_KMH", "2.0")))
LONG_IDLE_MIN_SECONDS = int(os.getenv("GPS_STOP_MIN_SECONDS", "300"))
MAX_POINT_GAP_SECONDS = float(os.getenv("USB_EXPORT_MAX_POINT_GAP_SEC", "3600"))
DELETE_EVENTS_AFTER_EXPORT = os.getenv(
    "USB_EXPORT_DELETE_EVENTS", "1"
).strip().lower() not in {"0", "false", "no", "off"}
# Sync the stick and delete verified sources after this many events
SYNC_BATCH_EVENTS = int(os.getenv("USB_EXPORT_SYNC_BATCH", "25"))
# event_meta.txt is written last by the capture service; skip events touched this recently
EVENT_SETTLE_SECONDS = 10
# Folders without event_meta.txt (power cut mid-save) are exported as "Incomplete" after this
INCOMPLETE_EVENT_AGE_SECONDS = 600
MOUNT_OPTIONS = "rw,noatime,uid=1000,gid=1000,umask=0022"
REPORT_NAME = "Vehicle_Report.pdf"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [usb_export] %(levelname)s %(message)s",
)
logger = logging.getLogger("usb_export")


def _buzz(ok: bool) -> None:
    """Optional hardware feedback; never fails the export."""
    try:
        from buzzer_controller import buzz_for
        if ok:
            buzz_for(0.15)
            time.sleep(0.25)
            buzz_for(0.15)
        else:
            buzz_for(0.8)
    except Exception:
        pass


def _acquire_lock() -> int | None:
    """POSIX exclusive lock via O_EXCL lock file. Returns fd or None if busy."""
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, f"{os.getpid()}\n".encode())
        return fd
    except FileExistsError:
        # Stale lock recovery: if holder is dead, take over
        try:
            pid = int(LOCK_PATH.read_text().strip() or "0")
            os.kill(pid, 0)
            logger.warning("Export already running (pid %s)", pid)
            return None
        except (ValueError, OSError, ProcessLookupError):
            LOCK_PATH.unlink(missing_ok=True)
            return _acquire_lock()


def _release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    LOCK_PATH.unlink(missing_ok=True)


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    logger.info("RUN: %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _is_mounted(path: Path) -> bool:
    try:
        return path.is_mount()
    except OSError:
        return False


def _find_marker_mounts(search_roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    for root in search_roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / MARKER_NAME).exists():
                found.append(child)
    return found


def _mount_source(mount_point: Path) -> str | None:
    result = _run(["findmnt", "-n", "-o", "SOURCE", "--mountpoint", str(mount_point)], check=False)
    return result.stdout.strip() or None


def mount_device(device: Path, mount_point: Path) -> bool:
    """Mount device at mount_point. Returns True if the caller should unmount afterwards."""
    if _is_mounted(mount_point):
        source = _mount_source(mount_point)
        if source and os.path.realpath(source) == os.path.realpath(str(device)):
            logger.info("Reusing mount of %s left by an interrupted export", device)
            return True
        # A killed export never unmounted; the old stick is gone and the mount is dead
        logger.warning("Stale mount at %s (source=%s) — lazy unmount", mount_point, source)
        _run(["umount", "-l", str(mount_point)], check=False)

    mount_point.mkdir(parents=True, exist_ok=True)

    # Wait briefly for partition / filesystem settle after udev add
    deadline = time.time() + MOUNT_WAIT_SEC
    while time.time() < deadline:
        if device.exists():
            break
        time.sleep(0.25)
    if not device.exists():
        raise FileNotFoundError(f"Device not found: {device}")

    # No "sync" option: it forces every small write to the stick and made large exports
    # run for hours. Durability comes from explicit os.sync() before any source delete.
    try:
        _run(["mount", "-o", MOUNT_OPTIONS, str(device), str(mount_point)])
    except subprocess.CalledProcessError:
        _run(["mount", "-t", "vfat", "-o", MOUNT_OPTIONS, str(device), str(mount_point)])
    return True


def unmount_device(mount_point: Path) -> None:
    if not _is_mounted(mount_point):
        return
    try:
        _run(["sync"])
        _run(["umount", str(mount_point)])
    except subprocess.CalledProcessError as e:
        logger.error("Unmount failed: %s / %s", e.stdout, e.stderr)
        _run(["umount", "-l", str(mount_point)], check=False)


def consistent_sqlite_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Prefer WAL checkpoint + backup API for a consistent snapshot
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    dst_conn = sqlite3.connect(str(dst))
    try:
        try:
            src_conn.execute("PRAGMA query_only=ON")
        except sqlite3.Error:
            pass
        src_conn.backup(dst_conn)
        dst_conn.commit()
    finally:
        dst_conn.close()
        src_conn.close()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_file_hashed(source: Path, destination: Path) -> str:
    """Copy and hash in one read pass of the source."""
    h = hashlib.sha256()
    with source.open("rb") as fi, destination.open("wb") as fo:
        for chunk in iter(lambda: fi.read(1024 * 1024), b""):
            h.update(chunk)
            fo.write(chunk)
    try:
        shutil.copystat(source, destination)
    except OSError:
        pass  # vfat/exfat can't store all metadata — content is what matters
    return h.hexdigest()


def read_event_meta(folder: Path) -> dict:
    meta: dict = {}
    try:
        text = (folder / "event_meta.txt").read_text(errors="replace")
    except OSError:
        return meta
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            meta[key.strip()] = value.strip()
    return meta


_UNSAFE_TYPE_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def event_type_folder(meta: dict) -> str:
    """Folder name for an event's type (Sleeping, Yawning, PhoneCall, ...)."""
    if not meta:
        return "Incomplete"
    return _UNSAFE_TYPE_CHARS.sub("", meta.get("event_type", "")) or "Other"


def _age_seconds(path: Path, now: float) -> float:
    age = now - path.stat().st_mtime
    # Negative = file is "from the future" because the clock was set back; treat as settled
    return age if age >= 0 else float("inf")


def list_exportable_events(events_dir: Path, now: float | None = None) -> list[Path]:
    """Event folders the capture service has finished writing, oldest first."""
    now = time.time() if now is None else now
    ready: list[Path] = []
    for child in sorted(events_dir.iterdir()):
        if not child.is_dir():
            continue
        try:
            meta_file = child / "event_meta.txt"
            if meta_file.exists():
                if _age_seconds(meta_file, now) >= EVENT_SETTLE_SECONDS:
                    ready.append(child)
            elif _age_seconds(child, now) >= INCOMPLETE_EVENT_AGE_SECONDS:
                ready.append(child)
        except OSError:
            continue
    return ready


def copy_event_verified(source: Path, destination: Path) -> list[dict]:
    """Copy one event folder and verify every destination file's checksum."""
    records: list[dict] = []
    for src_file in sorted(p for p in source.rglob("*") if p.is_file()):
        dst_file = destination / src_file.relative_to(source)
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        before = src_file.stat()
        digest = _copy_file_hashed(src_file, dst_file)
        if sha256_file(dst_file) != digest:
            raise RuntimeError(f"Checksum mismatch after copy: {dst_file}")
        records.append({
            "source": src_file,
            "destination": dst_file,
            "size": before.st_size,
            "mtime_ns": before.st_mtime_ns,
            "sha256": digest,
        })
    return records


def delete_event_source(folder: Path, records: list[dict]) -> int:
    """Delete an exported event's files that are unchanged since the copy.

    Returns the number of files preserved (changed during export or undeletable).
    """
    preserved = 0
    for record in records:
        source: Path = record["source"]
        try:
            current = source.stat()
            if current.st_size != record["size"] or current.st_mtime_ns != record["mtime_ns"]:
                preserved += 1
                continue
            source.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.error("Could not delete exported file %s: %s", source, exc)
            preserved += 1

    for directory in sorted((p for p in folder.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        pass
    return preserved


def _format_unix(value) -> str:
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def event_summary_row(type_folder: str, folder: Path, meta: dict, file_count: int) -> dict:
    start = meta.get("start_time")
    end = meta.get("end_time")
    try:
        duration = round(float(end) - float(start), 1)
    except (TypeError, ValueError):
        duration = ""
    return {
        "event_type": type_folder,
        "event_folder": f"events/{type_folder}/{folder.name}",
        "start_time": _format_unix(start),
        "end_time": _format_unix(end),
        "duration_seconds": duration,
        "frame_count": meta.get("frame_count", ""),
        "file_count": file_count,
        "latitude": meta.get("lat", ""),
        "longitude": meta.get("long", ""),
        "speed_kmh": meta.get("speed", ""),
    }


EVENT_SUMMARY_FIELDS = [
    "event_type", "event_folder", "start_time", "end_time", "duration_seconds",
    "frame_count", "file_count", "latitude", "longitude", "speed_kmh",
]


def export_events(events_dir: Path, out: Path, delete_events: bool) -> dict:
    """Copy completed events into out/events/<Type>/<event>/, batch by batch.

    After every SYNC_BATCH_EVENTS events the stick is synced, then (if enabled)
    the verified sources are deleted — an interrupted run keeps its progress.
    """
    events_out = out / "events"
    events_out.mkdir(parents=True, exist_ok=True)
    folders = list_exportable_events(events_dir) if events_dir.exists() else []
    logger.info("Exporting %d events from %s", len(folders), events_dir)

    checksums: dict[str, str] = {}
    type_counts: dict[str, int] = {}
    report_events: list[dict] = []
    failed: list[str] = []
    stats = {"copied_files": 0, "copied_bytes": 0, "deleted_events": 0, "preserved_files": 0}
    batch: list[tuple[Path, list[dict]]] = []

    def flush_batch():
        if not batch:
            return
        os.sync()
        if delete_events:
            for folder, records in batch:
                preserved = delete_event_source(folder, records)
                stats["preserved_files"] += preserved
                if not preserved:
                    stats["deleted_events"] += 1
        batch.clear()

    with (out / "events_summary.csv").open("w", newline="") as summary_file:
        writer = csv.DictWriter(summary_file, fieldnames=EVENT_SUMMARY_FIELDS)
        writer.writeheader()

        for index, folder in enumerate(folders, 1):
            meta = read_event_meta(folder)
            type_folder = event_type_folder(meta)
            destination = events_out / type_folder / folder.name
            try:
                records = copy_event_verified(folder, destination)
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    flush_batch()
                    raise RuntimeError(
                        f"USB stick is full after {index - 1} of {len(folders)} events"
                    ) from exc
                logger.error("Event %s failed to export: %s", folder.name, exc)
                failed.append(folder.name)
                continue
            except RuntimeError as exc:
                logger.error("Event %s failed to export: %s", folder.name, exc)
                failed.append(folder.name)
                continue

            for record in records:
                rel = str(record["destination"].relative_to(out)).replace("\\", "/")
                checksums[rel] = record["sha256"]
                stats["copied_files"] += 1
                stats["copied_bytes"] += record["size"]
            type_counts[type_folder] = type_counts.get(type_folder, 0) + 1
            writer.writerow(event_summary_row(type_folder, folder, meta, len(records)))
            report_events.append({
                "type": type_folder,
                "start": meta.get("start_time"),
                "end": meta.get("end_time"),
                "speed": meta.get("speed"),
                "lat": meta.get("lat"),
                "long": meta.get("long"),
                "folder": destination,
            })
            batch.append((folder, records))

            if len(batch) >= SYNC_BATCH_EVENTS:
                summary_file.flush()
                flush_batch()
                logger.info("Exported %d/%d events", index, len(folders))

    flush_batch()
    return {
        "event_count": sum(type_counts.values()),
        "events_by_type": type_counts,
        "failed_events": failed,
        "checksums": checksums,
        "report_events": report_events,
        **stats,
    }


def _parse_timestamp(value) -> datetime | None:
    if value is None:
        return None
    text = str(value).replace("T", " ").split(".")[0].strip()
    if not text:
        return None
    try:
        # C fast path; strptime is ~10x slower and runs once per GPS point
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _minutes(seconds: float) -> float:
    return round(max(0.0, float(seconds)) / 60.0, 2)


_TRIP_POINTS_SQL = """
    SELECT speed, timestamp, {status} AS movement_status, latitude, longitude
    FROM gps_data
    WHERE trip_id = ?
      AND timestamp IS NOT NULL
      AND latitude IS NOT NULL
      AND longitude IS NOT NULL
    ORDER BY timestamp, id
"""
GPS_POINTS_CSV_FIELDS = ["trip_id", "timestamp", "latitude", "longitude", "speed_kmh", "movement_status"]


def _point_is_idle(speed, movement_status) -> bool:
    if movement_status:
        return str(movement_status).lower() == "idle"
    try:
        return float(speed or 0.0) <= IDLE_SPEED_KMH
    except (TypeError, ValueError):
        return False


def _iter_trip_points(conn: sqlite3.Connection, trip_id: int):
    """Stream a trip's points; a long trip can be hundreds of thousands of rows."""
    try:
        cursor = conn.execute(_TRIP_POINTS_SQL.format(status="movement_status"), (int(trip_id),))
    except sqlite3.Error:
        cursor = conn.execute(_TRIP_POINTS_SQL.format(status="NULL"), (int(trip_id),))
    yield from cursor


class _IdleStats:
    """Idle/moving durations and long stops, accumulated one point at a time."""

    def __init__(self) -> None:
        self.idle_seconds = 0.0
        self.moving_seconds = 0.0
        self.stops: list[dict] = []
        self.point_count = 0
        self._prev_ts: datetime | None = None
        self._prev_idle = False
        self._segment: dict | None = None

    def add(self, ts: datetime, ts_text: str, idle: bool, lat: float, lon: float) -> None:
        self.point_count += 1
        if self._prev_ts is not None:
            dt = (ts - self._prev_ts).total_seconds()
            if 0 < dt <= MAX_POINT_GAP_SECONDS:
                if self._prev_idle:
                    self.idle_seconds += dt
                else:
                    self.moving_seconds += dt
        if idle:
            seg = self._segment
            if seg is None:
                seg = self._segment = {"start": ts, "start_text": ts_text, "lat": 0.0, "lon": 0.0, "n": 0}
            seg["end"], seg["end_text"] = ts, ts_text
            seg["lat"] += lat
            seg["lon"] += lon
            seg["n"] += 1
        else:
            self._flush_segment()
        self._prev_ts = ts
        self._prev_idle = idle

    def _flush_segment(self) -> None:
        seg, self._segment = self._segment, None
        if not seg or seg["n"] < 2:
            return
        duration = int((seg["end"] - seg["start"]).total_seconds())
        if duration < LONG_IDLE_MIN_SECONDS:
            return
        self.stops.append({
            "start_time": seg["start_text"],
            "end_time": seg["end_text"],
            "duration_minutes": _minutes(duration),
            "latitude": round(seg["lat"] / seg["n"], 7),
            "longitude": round(seg["lon"] / seg["n"], 7),
            "point_count": seg["n"],
        })

    def result(self) -> dict:
        self._flush_segment()
        total = self.idle_seconds + self.moving_seconds
        return {
            "idle_duration_minutes": _minutes(self.idle_seconds),
            "moving_duration_minutes": _minutes(self.moving_seconds),
            "trip_duration_minutes": _minutes(total),
            "idle_percent": round((self.idle_seconds / total) * 100.0, 1) if total > 0 else 0.0,
            "long_idle_stop_count": len(self.stops),
            "long_idle_stop_minutes": round(sum(s["duration_minutes"] for s in self.stops), 2),
            "idle_stops": self.stops,
        }


def _enrich_trip_with_idle(conn: sqlite3.Connection, trip: dict, points_writer=None) -> dict:
    trip_id = trip.get("id")
    if trip_id is None:
        return trip

    stats = _IdleStats()
    for point in _iter_trip_points(conn, int(trip_id)):
        ts_text = str(point["timestamp"])
        ts = _parse_timestamp(ts_text)
        if not ts:
            continue
        try:
            lat = float(point["latitude"])
            lon = float(point["longitude"])
            speed = float(point["speed"] or 0.0)
        except (TypeError, ValueError):
            continue
        status = point["movement_status"]
        idle = _point_is_idle(speed, status)
        stats.add(ts, ts_text, idle, lat, lon)
        if points_writer is not None:
            points_writer.writerow([trip_id, ts_text, round(lat, 7), round(lon, 7), round(speed, 2),
                                    status or ("idle" if idle else "moving")])
    trip.update(stats.result())

    # Fallback duration from trip start/end if point gaps were too sparse
    if stats.point_count and trip.get("trip_duration_minutes", 0) <= 0:
        start_ts = _parse_timestamp(trip.get("start_time"))
        end_ts = _parse_timestamp(trip.get("end_time"))
        if start_ts and end_ts and end_ts > start_ts:
            duration = (end_ts - start_ts).total_seconds()
            trip["trip_duration_minutes"] = _minutes(duration)

    return trip


def write_trips_summary(db_path: Path, out_path: Path, points_csv: Path | None = None) -> dict:
    """Human-readable trip list without requiring SQLite on the laptop.

    Every trip's GPS points are streamed into points_csv (if given) instead of
    being held in memory, so the export stays within its memory limit.
    """
    summary = {
        "trips": [],
        "gps_point_count": 0,
        "event_table_hint": "see images/events",
        "idle_threshold_kmh": IDLE_SPEED_KMH,
        "long_idle_min_minutes": _minutes(LONG_IDLE_MIN_SECONDS),
        "totals": {
            "idle_duration_minutes": 0.0,
            "moving_duration_minutes": 0.0,
            "trip_duration_minutes": 0.0,
            "long_idle_stop_count": 0,
            "long_idle_stop_minutes": 0.0,
        },
    }
    if not db_path.exists():
        out_path.write_text(json.dumps(summary, indent=2))
        return summary

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                """
                SELECT id, session_id, start_time, end_time, status,
                       start_latitude, start_longitude, end_latitude, end_longitude,
                       point_count, total_distance_km
                FROM gps_trips
                ORDER BY id DESC
                LIMIT 500
                """
            ).fetchall()
            summary["trips"] = [dict(r) for r in rows]
        except sqlite3.Error as e:
            # Older DBs may lack columns — fall back to SELECT *
            try:
                rows = conn.execute(
                    "SELECT * FROM gps_trips ORDER BY id DESC LIMIT 500"
                ).fetchall()
                summary["trips"] = [dict(r) for r in rows]
                summary["trips_note"] = f"fallback select *: {e}"
            except sqlite3.Error as e2:
                summary["trips_error"] = str(e2)

        points_file = points_csv.open("w", newline="") if points_csv else None
        try:
            points_writer = csv.writer(points_file) if points_file else None
            if points_writer:
                points_writer.writerow(GPS_POINTS_CSV_FIELDS)
                summary["route_points_file"] = points_csv.name
            summary["trips"] = [_enrich_trip_with_idle(conn, trip, points_writer) for trip in summary["trips"]]
        finally:
            if points_file:
                points_file.close()

        totals = summary["totals"]
        for trip in summary["trips"]:
            totals["idle_duration_minutes"] += float(trip.get("idle_duration_minutes") or 0.0)
            totals["moving_duration_minutes"] += float(trip.get("moving_duration_minutes") or 0.0)
            totals["trip_duration_minutes"] += float(trip.get("trip_duration_minutes") or 0.0)
            totals["long_idle_stop_count"] += int(trip.get("long_idle_stop_count") or 0)
            totals["long_idle_stop_minutes"] += float(trip.get("long_idle_stop_minutes") or 0.0)

        totals["idle_duration_minutes"] = round(totals["idle_duration_minutes"], 2)
        totals["moving_duration_minutes"] = round(totals["moving_duration_minutes"], 2)
        totals["trip_duration_minutes"] = round(totals["trip_duration_minutes"], 2)
        totals["long_idle_stop_minutes"] = round(totals["long_idle_stop_minutes"], 2)

        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM gps_data").fetchone()
            summary["gps_point_count"] = int(row["n"] if row else 0)
        except sqlite3.Error:
            pass
    finally:
        conn.close()

    out_path.write_text(json.dumps(summary, indent=2, default=str))
    return summary


def _device_serial() -> str:
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from get_device_id import get_device_serial
        return get_device_serial()
    except Exception:
        return ""


def read_vehicle_info(db_path: Path) -> dict:
    """Vehicle number / driver name entered in the web UI (configure table)."""
    info = {"vehicle_number": "", "driver_name": ""}
    if not db_path.exists():
        return info
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            rows = conn.execute(
                "SELECT config_key, config_value FROM configure "
                "WHERE config_key IN ('vehicle_number', 'driver_name')"
            ).fetchall()
        finally:
            conn.close()
        for key, value in rows:
            info[key] = (value or "").strip()
    except sqlite3.Error as exc:
        logger.warning("Could not read vehicle info: %s", exc)
    return info


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", value.replace(" ", ""))


def export_to_mount(
    mount: Path,
    db_path: Path,
    events_dir: Path,
    delete_events: bool = DELETE_EVENTS_AFTER_EXPORT,
) -> Path:
    marker = mount / MARKER_NAME
    if not marker.exists():
        raise RuntimeError(
            f"Marker '{MARKER_NAME}' not found on {mount}. "
            f"Create an empty file at the USB root to authorize export."
        )

    stamp = time.strftime("%Y%m%d_%H%M%S")
    hostname = os.uname().nodename if hasattr(os, "uname") else "pi"
    vehicle = read_vehicle_info(db_path)
    vehicle["device_serial"] = _device_serial()
    label = _safe_name(vehicle["vehicle_number"]) or _safe_name(hostname) or "device"
    out = mount / f"{EXPORT_PREFIX}_{label}_{stamp}"
    out.mkdir(parents=True, exist_ok=False)

    logger.info("Export directory: %s", out)
    in_progress = out / "EXPORT_IN_PROGRESS"
    in_progress.write_text("Do not remove the USB stick until EXPORT_OK appears.\n")

    checksums: dict[str, str] = {}
    db_out = out / "blinksmart.db"
    if db_path.exists():
        # Stage via tempfile then move — reduces risk of half-written file on stick
        with tempfile.NamedTemporaryFile(dir=str(out), delete=False, suffix=".db.tmp") as tmp:
            tmp_path = Path(tmp.name)
        try:
            consistent_sqlite_copy(db_path, tmp_path)
            tmp_path.replace(db_out)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        checksums["blinksmart.db"] = sha256_file(db_out)
    else:
        logger.warning("DB missing: %s", db_path)
        (out / "DB_MISSING.txt").write_text(f"Expected DB at {db_path}\n")
    os.sync()

    if not events_dir.exists():
        logger.warning("Events dir missing: %s", events_dir)
    events = export_events(events_dir, out, delete_events)
    checksums.update(events.pop("checksums"))
    report_events = events.pop("report_events")

    # Trip summary is derived from the DB already on the stick — never fail the export on it
    trips = None
    if db_out.exists():
        try:
            trips = write_trips_summary(db_out, out / "trips_summary.json", out / "gps_points.csv")
        except Exception as exc:
            logger.error("Trip summary failed: %s", exc)
            (out / "TRIPS_SUMMARY_ERROR.txt").write_text(f"{exc}\n")

    # PDF is built from the event copies already on the stick (sources may be deleted)
    try:
        from vehicle_report import write_vehicle_report
        write_vehicle_report(out / REPORT_NAME, vehicle, report_events, trips)
    except Exception as exc:
        logger.error("Vehicle report failed: %s", exc)
        (out / "REPORT_ERROR.txt").write_text(f"{exc}\n")
    del report_events, trips

    for name in ("events_summary.csv", "trips_summary.json", "gps_points.csv", REPORT_NAME):
        if (out / name).exists():
            checksums[name] = sha256_file(out / name)

    manifest = {
        "exported_at": stamp,
        "exported_at_unix": int(time.time()),
        "hostname": hostname,
        "vehicle_number": vehicle["vehicle_number"],
        "driver_name": vehicle["driver_name"],
        "device_serial": vehicle["device_serial"],
        "source_db": str(db_path),
        "source_events": str(events_dir),
        "layout": "events/<EventType>/<event_folder>/frame_NNNN.jpg — see events_summary.csv",
        "event_transfer": {
            "mode": "move_after_verified_copy" if delete_events else "copy",
            **events,
        },
        "checksums_sha256": checksums,
        "note": "Offline pull of trips (SQLite) + event images. Insert stick with "
                f"{MARKER_NAME} marker; remove after buzzer double-beep / EXPORT_OK.",
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    in_progress.unlink(missing_ok=True)
    os.sync()

    if events["failed_events"] or events["preserved_files"]:
        (out / "EXPORT_PARTIAL").write_text(
            f"{len(events['failed_events'])} event(s) failed to copy and "
            f"{events['preserved_files']} source file(s) were kept on the device. "
            "Re-insert the stick to retry.\n"
        )
        os.sync()
        raise RuntimeError(
            f"Partial export: {len(events['failed_events'])} failed events, "
            f"{events['preserved_files']} preserved files"
        )

    (out / "EXPORT_OK").write_text(f"ok {stamp}\n")
    os.sync()

    logger.info(
        "Export complete: %d events (%s), %.1f MB -> %s",
        events["event_count"],
        ", ".join(f"{k}={v}" for k, v in sorted(events["events_by_type"].items())) or "none",
        events["copied_bytes"] / (1024 * 1024),
        out,
    )
    return out


def resolve_mount(args: argparse.Namespace) -> tuple[Path, bool]:
    """
    Returns (mount_path, we_mounted).
    we_mounted True means caller should unmount when done.
    """
    if args.mount:
        mount = Path(args.mount)
        if not mount.exists():
            raise FileNotFoundError(f"Mount path does not exist: {mount}")
        return mount, False

    if args.device:
        device = Path(args.device)
        mount_point = Path(args.mount_point)
        we_mounted = mount_device(device, mount_point)
        return mount_point, we_mounted

    # Auto-discover already-mounted volumes with marker
    candidates = _find_marker_mounts([
        Path("/media/pi"),
        Path("/media"),
        Path("/mnt"),
        Path(args.mount_point).parent if args.mount_point else Path("/mnt"),
    ])
    # Deduplicate
    uniq: list[Path] = []
    seen = set()
    for c in candidates:
        key = str(c.resolve()) if c.exists() else str(c)
        if key not in seen and (c / MARKER_NAME).exists():
            seen.add(key)
            uniq.append(c)

    if not uniq:
        raise RuntimeError(
            f"No USB mount with marker '{MARKER_NAME}' found. "
            "Pass --device /dev/sda1 or --mount /media/pi/USBNAME."
        )
    return uniq[0], False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export Blinksmart trips + event images to USB")
    p.add_argument("--device", help="Block device to mount, e.g. /dev/sda1")
    p.add_argument("--mount", help="Already-mounted USB path")
    p.add_argument("--mount-point", default=str(DEFAULT_MOUNT_POINT),
                   help=f"Where to mount --device (default {DEFAULT_MOUNT_POINT})")
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite DB path")
    p.add_argument("--events", default=str(DEFAULT_EVENTS), help="Events directory")
    p.add_argument("--keep-events", action="store_true",
                   help="Copy events without deleting verified source files")
    p.add_argument("--no-unmount", action="store_true",
                   help="Leave device mounted even if we mounted it")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate mount/marker only; do not copy")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # systemd stop / timeout sends SIGTERM — exit through finally so we unmount + unlock
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))

    lock_fd = _acquire_lock()
    if lock_fd is None:
        return 2

    we_mounted = False
    mount: Path | None = None
    try:
        mount, we_mounted = resolve_mount(args)
        logger.info("Using mount: %s (we_mounted=%s)", mount, we_mounted)

        if args.dry_run:
            marker_ok = (mount / MARKER_NAME).exists()
            logger.info("Dry-run: marker_present=%s", marker_ok)
            return 0 if marker_ok else 1

        dest = export_to_mount(
            mount,
            Path(args.db),
            Path(args.events),
            delete_events=DELETE_EVENTS_AFTER_EXPORT and not args.keep_events,
        )
        # Also append a line to a stick-side log for field debugging
        try:
            log_line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} OK {dest.name}\n"
            with (mount / "blinksmart_export.log").open("a") as f:
                f.write(log_line)
            os.sync()
        except OSError as e:
            logger.warning("Could not append stick log: %s", e)

        _buzz(True)
        return 0
    except Exception as e:
        logger.exception("Export failed: %s", e)
        try:
            if mount and mount.exists():
                with (mount / "blinksmart_export.log").open("a") as f:
                    f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} FAIL {e}\n")
                os.sync()
        except OSError:
            pass
        _buzz(False)
        return 1
    finally:
        if we_mounted and not args.no_unmount and mount is not None:
            unmount_device(mount)
        _release_lock(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
