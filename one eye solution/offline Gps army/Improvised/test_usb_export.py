"""Tests for the offline USB export (per-type event folders, batching, partial safety).

  cd ~/facial-tracker-firmware
  ./venv/bin/python -m unittest test_usb_export -v
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import usb_export

TYPES = ["Sleeping", "Yawning", "PhoneCall", "LookingDown", "NoFace"]


def _make_event(root: Path, name: str, event_type: str | None, age: float, frames: int = 3):
    folder = root / name
    folder.mkdir(parents=True)
    for i in range(frames):
        (folder / f"frame_{i:04d}.jpg").write_bytes(os.urandom(2048))
    if event_type is not None:
        start = time.time() - age
        (folder / "event_meta.txt").write_text(
            f"event_id={name}\nevent_type={event_type}\nstart_time={start}\n"
            f"end_time={start + 4}\nframe_count={frames}\nlat=12.97\nlong=77.59\nspeed=42\n"
        )
    past = time.time() - age
    for p in [*folder.iterdir(), folder]:
        os.utime(p, (past, past))
    return folder


class TestUsbExport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.events = base / "events"
        self.mount = base / "usb"
        self.events.mkdir()
        self.mount.mkdir()
        (self.mount / usb_export.MARKER_NAME).write_text("")
        self.db = base / "blinksmart.db"
        sqlite3.connect(self.db).close()
        if not hasattr(os, "sync"):
            patcher = patch.object(os, "sync", lambda: None, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _export(self, **kw):
        with patch.object(usb_export, "_device_serial", return_value="ABC123"):
            return usb_export.export_to_mount(self.mount, self.db, self.events, **kw)

    def test_events_sorted_into_type_folders_and_moved(self):
        for i in range(60):
            _make_event(self.events, f"2026060{i % 9}_1200{i:02d}_evt_{i:04d}", TYPES[i % 5], age=120)
        out = self._export(delete_events=True)

        self.assertTrue((out / "EXPORT_OK").exists())
        self.assertFalse((out / "EXPORT_IN_PROGRESS").exists())
        for t in TYPES:
            self.assertEqual(len(list((out / "events" / t).iterdir())), 12, t)
        self.assertEqual(list(self.events.iterdir()), [])  # all sources moved off the Pi

        with (out / "events_summary.csv").open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 60)
        self.assertTrue(all(r["event_folder"].startswith(f"events/{r['event_type']}/") for r in rows))

        manifest = json.loads((out / "MANIFEST.json").read_text())
        self.assertEqual(manifest["event_transfer"]["event_count"], 60)
        # Frames here are not real JPEGs: the report still builds, just without snapshots
        self.assertTrue((out / "Vehicle_Report.pdf").read_bytes().startswith(b"%PDF-1.4"))
        self.assertIn("Vehicle_Report.pdf", manifest["checksums_sha256"])
        self.assertEqual(manifest["event_transfer"]["deleted_events"], 60)
        self.assertEqual(len([k for k in manifest["checksums_sha256"] if k.startswith("events/")]), 60 * 4)

    def test_in_progress_event_left_on_device(self):
        _make_event(self.events, "done_evt", "Sleeping", age=120)
        fresh = _make_event(self.events, "writing_evt", "Yawning", age=0)
        out = self._export(delete_events=True)
        self.assertTrue((out / "events" / "Sleeping" / "done_evt").is_dir())
        self.assertTrue(fresh.is_dir())
        self.assertFalse((out / "events" / "Yawning").exists())

    def test_meta_less_folder_exported_as_incomplete_when_old(self):
        _make_event(self.events, "crashed_evt", None, age=3600)
        young = _make_event(self.events, "saving_evt", None, age=5)
        out = self._export(delete_events=True)
        self.assertTrue((out / "events" / "Incomplete" / "crashed_evt").is_dir())
        self.assertTrue(young.is_dir())

    def test_future_mtime_after_clock_set_back_is_exported(self):
        folder = _make_event(self.events, "future_evt", "PhoneCall", age=0)
        future = time.time() + 86400
        for p in [*folder.iterdir(), folder]:
            os.utime(p, (future, future))
        out = self._export(delete_events=True)
        self.assertTrue((out / "events" / "PhoneCall" / "future_evt").is_dir())

    def test_keep_events_copies_only(self):
        _make_event(self.events, "keep_evt", "Sleeping", age=120)
        out = self._export(delete_events=False)
        self.assertTrue((out / "events" / "Sleeping" / "keep_evt").is_dir())
        self.assertTrue((self.events / "keep_evt").is_dir())

    def test_full_stick_keeps_verified_progress(self):
        for i in range(30):
            _make_event(self.events, f"evt_{i:03d}", "Sleeping", age=120)
        real_copy = usb_export._copy_file_hashed
        calls = {"n": 0}

        def copy_until_full(src, dst):
            calls["n"] += 1
            if calls["n"] > 20 * 4:  # 20 events fit
                raise OSError(28, "No space left on device")
            return real_copy(src, dst)

        with patch.object(usb_export, "_copy_file_hashed", copy_until_full), \
                self.assertRaisesRegex(RuntimeError, "full after 20 of 30"):
            self._export(delete_events=True)
        self.assertEqual(len(list(self.events.iterdir())), 10)  # 20 verified + moved

    def test_vehicle_report_named_by_vehicle_with_four_snapshots_per_type(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE configure (id INTEGER PRIMARY KEY, config_key TEXT UNIQUE, config_value TEXT)")
        conn.executemany("INSERT INTO configure (config_key, config_value) VALUES (?, ?)",
                         [("vehicle_number", "KA 01 AB 1234"), ("driver_name", "Ravi")])
        conn.commit()
        conn.close()
        for i in range(9):
            folder = _make_event(self.events, f"evt_s{i}", "Sleeping", age=120, frames=0)
            _write_jpeg(folder / "frame_0000.jpg")
            os.utime(folder, (time.time() - 120,) * 2)
        _make_event(self.events, "evt_y0", "Yawning", age=120, frames=0)
        _write_jpeg(self.events / "evt_y0" / "frame_0000.jpg")

        out = self._export(delete_events=True)

        self.assertTrue(out.name.startswith(f"{usb_export.EXPORT_PREFIX}_KA01AB1234_"))
        manifest = json.loads((out / "MANIFEST.json").read_text())
        self.assertEqual(manifest["vehicle_number"], "KA 01 AB 1234")
        pdf = (out / "Vehicle_Report.pdf").read_bytes()
        self.assertEqual(pdf.count(b"/Subtype /Image"), 4 + 1)  # 4 of 9 sleeping + 1 yawning
        self.assertIn(b"KA 01 AB 1234", pdf)
        _assert_valid_xref(self, pdf)


def _write_jpeg(path: Path):
    import cv2
    import numpy as np
    image = np.full((48, 64, 3), 120, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    path.write_bytes(encoded.tobytes())


def _assert_valid_xref(test: unittest.TestCase, pdf: bytes):
    start = int(pdf.rsplit(b"startxref", 1)[1].split()[0])
    lines = pdf[start:].split(b"\n")
    test.assertEqual(lines[0], b"xref")
    count = int(lines[1].split()[1])
    for number in range(1, count):
        offset = int(lines[2 + number][:10])
        test.assertTrue(pdf[offset:].startswith(b"%d 0 obj" % number), number)


if __name__ == "__main__":
    unittest.main()
