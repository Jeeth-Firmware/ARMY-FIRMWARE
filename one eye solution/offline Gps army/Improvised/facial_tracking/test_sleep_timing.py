"""Unit tests for time-based Sleeping detection in FacialTracker.

  cd ~/facial-tracker-firmware
  ./venv/bin/python -m unittest facial_tracking.test_sleep_timing -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import facial_tracking.conf as conf
import facial_tracking.facialTracking as ft


class _FakeEye:
    def __init__(self):
        self.closed = False
        self.iris = None

    def eye_closed(self):
        return self.closed


def _tracker():
    """FacialTracker without MediaPipe graphs — only the eye-state fields."""
    t = ft.FacialTracker.__new__(ft.FacialTracker)
    t.left_eye = _FakeEye()
    t.right_eye = _FakeEye()
    t.left_eye_closed_frames = 0
    t.right_eye_closed_frames = 0
    t.left_eye_gap_frames = 0
    t.right_eye_gap_frames = 0
    t.left_eye_closed_since = None
    t.right_eye_closed_since = None
    t.eyes_status = ''
    return t


class TestSleepTiming(unittest.TestCase):
    def setUp(self):
        patcher = patch.multiple(conf, SLEEP_SECONDS=1.5, SLEEP_MIN_FRAMES=3, HEADLESS=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.t = _tracker()

    def _frame(self, now, left_closed, right_closed=None):
        self.t.left_eye.closed = left_closed
        self.t.right_eye.closed = left_closed if right_closed is None else right_closed
        with patch.object(ft.time, 'monotonic', return_value=now):
            self.t._check_eyes_status()
        return self.t.eyes_status

    def _run(self, fps, seconds, start=0.0):
        """Feed closed-eye frames; return time Sleeping first fired (or None)."""
        dt = 1.0 / fps
        n = int(seconds * fps) + 1
        for i in range(n):
            now = start + i * dt
            if self._frame(now, True) == 'eye closed':
                return now - start
        return None

    def test_fires_at_sleep_seconds_regardless_of_fps(self):
        for fps in (3, 5, 10):
            self.t = _tracker()
            fired = self._run(fps, 3.0)
            self.assertIsNotNone(fired, f"never fired at {fps} FPS")
            self.assertGreaterEqual(fired, 1.5)
            self.assertLess(fired, 1.5 + 1.0 / fps + 1e-9, f"late at {fps} FPS")

    def test_normal_blink_ignored(self):
        self.assertEqual(self._frame(0.0, True), '')
        self.assertEqual(self._frame(0.2, True), '')
        self.assertEqual(self._frame(0.4, False), '')
        self.assertEqual(self._frame(0.6, False), '')
        self.assertIsNone(self.t.left_eye_closed_since)

    def test_single_open_frame_tolerated(self):
        for i in range(5):
            self._frame(i * 0.2, True)
        self._frame(1.0, False)  # one noisy open frame
        self.assertEqual(self._frame(1.6, True), 'eye closed')

    def test_one_eye_closed_is_not_sleeping(self):
        for i in range(20):
            status = self._frame(i * 0.2, True, False)
        self.assertEqual(status, '')

    def test_min_frames_guard_on_stalled_loop(self):
        # Two frames 2s apart (loop stall) must not fire on time alone
        self._frame(0.0, True)
        self.assertEqual(self._frame(2.0, True), '')
        self.assertEqual(self._frame(2.1, True), 'eye closed')

    def test_live_setting_change_applies(self):
        conf.SLEEP_SECONDS = 0.5
        fired = self._run(10, 2.0)
        self.assertAlmostEqual(fired, 0.5, delta=0.11)


if __name__ == "__main__":
    unittest.main()
