"""Unit tests for HeadPoseAttentionDetector (one-eye robust head pose).

  cd ~/facial-tracker-firmware
  ./venv/bin/python -m unittest facial_tracking.test_head_pose -v
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import facial_tracking.conf as conf
from facial_tracking.head_pose import HeadPoseAttentionDetector


class _Lm:
    __slots__ = ("x", "y", "z")

    def __init__(self, x, y, z=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)


def _face(forehead=(0.50, 0.20), nose=(0.50, 0.42), chin=(0.50, 0.80),
          left=(0.30, 0.45), right=(0.70, 0.45)):
    """Build FaceMesh-like landmarks with the 5 head-pose anchors."""
    lms = [_Lm(0.5, 0.5) for _ in range(478)]
    lms[10] = _Lm(*forehead)
    lms[1] = _Lm(*nose)
    lms[152] = _Lm(*chin)
    lms[234] = _Lm(*left)
    lms[454] = _Lm(*right)
    return SimpleNamespace(landmark=lms)


class TestHeadPoseGeometry(unittest.TestCase):
    def setUp(self):
        self.det = HeadPoseAttentionDetector()
        self.aspect = 320 / 240

    def test_forward_face_near_neutral(self):
        face = _face(nose=(0.50, 0.42))
        pitch, yaw, roll = self.det.estimate(face, self.aspect)
        self.assertLess(abs(pitch), conf.LOOKDOWN_PITCH_THRESHOLD)
        self.assertLess(abs(yaw), conf.LOOKAWAY_YAW_THRESHOLD)
        self.assertLess(abs(roll), 20.0)

    def test_looking_down_raises_pitch(self):
        face = _face(nose=(0.50, 0.65))
        pitch, _, _ = self.det.estimate(face, self.aspect)
        self.assertGreaterEqual(pitch, conf.LOOKDOWN_PITCH_THRESHOLD)

    def test_looking_left_raises_yaw(self):
        face = _face(nose=(0.62, 0.42), left=(0.30, 0.45), right=(0.70, 0.45))
        _, yaw, _ = self.det.estimate(face, self.aspect)
        self.assertGreaterEqual(abs(yaw), conf.LOOKAWAY_YAW_THRESHOLD)

    def test_roll_estimate_responds_to_tilt(self):
        """Roll is still estimated for diagnostics; HeadTilt is not an alert."""
        face = _face(left=(0.30, 0.35), right=(0.70, 0.55))
        _, _, roll = self.det.estimate(face, self.aspect)
        self.assertGreaterEqual(abs(roll), 20.0)

    def test_works_without_eye_landmarks(self):
        """Eye indices left at default — detector must not touch them."""
        face = _face(nose=(0.50, 0.68))
        pitch, _, _ = self.det.estimate(face, self.aspect)
        self.assertGreaterEqual(pitch, conf.LOOKDOWN_PITCH_THRESHOLD)


class TestHeadPoseStreak(unittest.TestCase):
    def setUp(self):
        self._prev = {
            "enabled": conf.LOOKDOWN_ENABLED,
            "pitch": conf.LOOKDOWN_PITCH_THRESHOLD,
            "yaw": conf.LOOKAWAY_YAW_THRESHOLD,
            "down_f": conf.LOOKDOWN_FRAMES,
            "away_f": conf.LOOKAWAY_FRAMES,
            "miss": conf.LOOKDOWN_MISS_TOLERANCE,
            "one_eye_s": conf.LOOKAWAY_ONE_EYE_SECONDS,
        }
        conf.LOOKDOWN_ENABLED = True
        conf.LOOKDOWN_PITCH_THRESHOLD = 20.0
        conf.LOOKAWAY_YAW_THRESHOLD = 25.0
        conf.LOOKDOWN_FRAMES = 3
        conf.LOOKAWAY_FRAMES = 3
        conf.LOOKDOWN_MISS_TOLERANCE = 1
        conf.LOOKAWAY_ONE_EYE_SECONDS = 3.0
        self.det = HeadPoseAttentionDetector()
        self.shape = (240, 320, 3)

    def tearDown(self):
        conf.LOOKDOWN_ENABLED = self._prev["enabled"]
        conf.LOOKDOWN_PITCH_THRESHOLD = self._prev["pitch"]
        conf.LOOKAWAY_YAW_THRESHOLD = self._prev["yaw"]
        conf.LOOKDOWN_FRAMES = self._prev["down_f"]
        conf.LOOKAWAY_FRAMES = self._prev["away_f"]
        conf.LOOKDOWN_MISS_TOLERANCE = self._prev["miss"]
        conf.LOOKAWAY_ONE_EYE_SECONDS = self._prev["one_eye_s"]

    def test_looking_down_after_streak(self):
        face = _face(nose=(0.50, 0.68))
        for _ in range(2):
            self.det.update(face, self.shape)
            self.assertEqual(self.det.status(), "Active")
        self.det.update(face, self.shape)
        self.assertEqual(self.det.status(), "LookingDown")

    def test_looking_away_requires_one_eye_and_3s(self):
        face = _face(nose=(0.65, 0.42))
        t0 = 1000.0
        with patch("facial_tracking.head_pose.time.monotonic", return_value=t0):
            self.det.update(face, self.shape, one_eye_closed=False)
            self.assertEqual(self.det.status(), "Active")
            self.det.update(face, self.shape, one_eye_closed=True)
            self.assertEqual(self.det.status(), "Active")
        with patch("facial_tracking.head_pose.time.monotonic", return_value=t0 + 2.9):
            self.det.update(face, self.shape, one_eye_closed=True)
            self.assertEqual(self.det.status(), "Active")
        with patch("facial_tracking.head_pose.time.monotonic", return_value=t0 + 3.0):
            self.det.update(face, self.shape, one_eye_closed=True)
            self.assertEqual(self.det.status(), "LookingAway")

    def test_looking_away_yaw_only_no_alert(self):
        face = _face(nose=(0.65, 0.42))
        t0 = 2000.0
        with patch("facial_tracking.head_pose.time.monotonic", return_value=t0):
            self.det.update(face, self.shape, one_eye_closed=False)
        with patch("facial_tracking.head_pose.time.monotonic", return_value=t0 + 5.0):
            self.det.update(face, self.shape, one_eye_closed=False)
            self.assertEqual(self.det.status(), "Active")

    def test_priority_down_over_away(self):
        face = _face(nose=(0.65, 0.68))
        t0 = 3000.0
        with patch("facial_tracking.head_pose.time.monotonic", return_value=t0):
            for _ in range(3):
                self.det.update(face, self.shape, one_eye_closed=True)
        with patch("facial_tracking.head_pose.time.monotonic", return_value=t0 + 3.0):
            self.det.update(face, self.shape, one_eye_closed=True)
            self.assertEqual(self.det.status(), "LookingDown")

    def test_miss_tolerance_keeps_alert(self):
        down = _face(nose=(0.50, 0.68))
        forward = _face(nose=(0.50, 0.42))
        for _ in range(3):
            self.det.update(down, self.shape)
        self.assertEqual(self.det.status(), "LookingDown")
        self.det.update(forward, self.shape)
        self.assertEqual(self.det.status(), "LookingDown")
        self.det.update(forward, self.shape)
        self.assertEqual(self.det.status(), "Active")

    def test_disabled_resets(self):
        face = _face(nose=(0.50, 0.68))
        for _ in range(3):
            self.det.update(face, self.shape)
        self.assertEqual(self.det.status(), "LookingDown")
        conf.LOOKDOWN_ENABLED = False
        self.det.update(face, self.shape)
        self.assertEqual(self.det.status(), "Active")

    def test_no_face_resets(self):
        face = _face(nose=(0.50, 0.68))
        for _ in range(3):
            self.det.update(face, self.shape)
        self.det.update(None, self.shape)
        self.assertEqual(self.det.status(), "Active")


if __name__ == "__main__":
    unittest.main()
