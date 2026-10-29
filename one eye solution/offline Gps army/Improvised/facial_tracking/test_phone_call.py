"""Unit tests for PhoneCallDetector (Pi Zero 2 W posture geometry).

Use the project venv (system Python often lacks numpy/opencv/mediapipe):

  cd ~/facial-tracker-firmware
  ./venv/bin/python -m unittest facial_tracking.test_phone_call -v
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# Ensure project root is on path when run directly
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import facial_tracking.conf as conf
from facial_tracking.phone_call import PhoneCallDetector, _HAND_IDS


class _Lm:
    __slots__ = ("x", "y", "z")

    def __init__(self, x, y, z=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)


def _face_landmarks(overrides=None):
    """Build a FaceMesh-like landmark list (478 pts) with ear + mouth anchors."""
    lms = [_Lm(0.5, 0.5) for _ in range(478)]
    # Face width / ears: left 234, right 454; secondary 93 / 323
    lms[234] = _Lm(0.30, 0.45)
    lms[93] = _Lm(0.32, 0.50)
    lms[454] = _Lm(0.70, 0.45)
    lms[323] = _Lm(0.68, 0.50)
    # Mouth (upper/lower lip) — used to reject smoking posture as PhoneCall
    lms[13] = _Lm(0.50, 0.54)
    lms[14] = _Lm(0.50, 0.58)
    if overrides:
        for idx, xy in overrides.items():
            lms[idx] = _Lm(xy[0], xy[1])
    return SimpleNamespace(landmark=lms)


def _hand_landmarks(anchor_xy, offsets=None):
    """Hand landmarks: place sparse keypoints near anchor (or with offsets)."""
    n = max(_HAND_IDS) + 1
    lms = [_Lm(0.5, 0.9) for _ in range(n)]
    ax, ay = anchor_xy
    for i, hid in enumerate(_HAND_IDS):
        if offsets and hid in offsets:
            ox, oy = offsets[hid]
            lms[hid] = _Lm(ax + ox, ay + oy)
        else:
            # Slight spread so all keypoints stay near ear
            lms[hid] = _Lm(ax + 0.01 * (i - 3), ay + 0.01 * ((i % 3) - 1))
    return SimpleNamespace(landmark=lms)


class _ConfPatch(unittest.TestCase):
    """Pin timing-related conf values so tests don't depend on env / DB."""

    def setUp(self):
        patcher = patch.multiple(
            conf,
            PHONE_DETECTION_ENABLED=True,
            PHONE_CONFIRM_SECONDS=0.3,
            PHONE_RELEASE_SECONDS=1.0,
            HANDS_SCAN_INTERVAL=0.2,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.det = PhoneCallDetector()


class TestPhoneCallGeometry(unittest.TestCase):
    def setUp(self):
        self.det = PhoneCallDetector()
        self.aspect = 320 / 240  # DETECT_W / DETECT_H
        self.face = _face_landmarks()

    def test_hand_touching_ear_detected(self):
        hand = _hand_landmarks((0.30, 0.45))
        self.assertTrue(self.det._hand_near_ear(self.face, hand, self.aspect))

    def test_hand_at_mouth_not_phone_call(self):
        """Smoking posture (hand at lips) must not trigger PhoneCall / buzzer."""
        hand = _hand_landmarks((0.50, 0.56))
        self.assertFalse(self.det._hand_near_ear(self.face, hand, self.aspect))

    def test_hand_on_steering_wheel_ignored(self):
        hand = _hand_landmarks((0.50, 0.92))
        self.assertFalse(self.det._hand_near_ear(self.face, hand, self.aspect))

    def test_hand_0_6_face_widths_away_ignored(self):
        # Place hand OUTSIDE the face (left of left ear) at 0.6 * face_w —
        # moving inward would approach the opposite ear and falsely trip.
        fl = self.face.landmark
        face_w = self.det._norm_dist(fl[234], fl[454], self.aspect)
        dx = (0.6 * face_w) / self.aspect
        ax, ay = 0.30 - dx, 0.45
        n = max(_HAND_IDS) + 1
        lms = [_Lm(0.5, 0.9) for _ in range(n)]
        for hid in _HAND_IDS:
            lms[hid] = _Lm(ax, ay)
        hand = SimpleNamespace(landmark=lms)
        self.assertFalse(self.det._hand_near_ear(self.face, hand, self.aspect))


class TestPhoneCallTiming(_ConfPatch):
    def test_below_confirm_time_no_alert(self):
        self.det._apply_near_result(True, 0.0)
        self.det._apply_near_result(True, 0.2)
        self.assertEqual(self.det.status(), "")

    def test_confirm_time_reached_alert(self):
        self.det._apply_near_result(True, 0.0)
        self.det._apply_near_result(True, 0.3)
        self.assertEqual(self.det.status(), "phone call")

    def test_zero_confirm_is_instant(self):
        conf.PHONE_CONFIRM_SECONDS = 0.0
        self.det._apply_near_result(True, 5.0)
        self.assertEqual(self.det.status(), "phone call")

    def test_burst_requested_only_while_confirming(self):
        self.assertFalse(self.det.wants_burst())
        self.det._apply_near_result(True, 0.0)
        self.assertTrue(self.det.wants_burst())
        self.det._apply_near_result(True, 0.3)
        self.assertFalse(self.det.wants_burst())  # active → back to idle cadence

    def test_one_miss_tolerated_while_confirming(self):
        self.det._apply_near_result(True, 0.0)
        self.det._apply_near_result(False, 0.1)
        self.det._apply_near_result(True, 0.3)
        self.assertEqual(self.det.status(), "phone call")

    def test_two_misses_reset_candidate(self):
        self.det._apply_near_result(True, 0.0)
        self.det._apply_near_result(False, 0.1)
        self.det._apply_near_result(False, 0.2)
        self.assertFalse(self.det.wants_burst())
        self.det._apply_near_result(True, 0.3)
        self.assertEqual(self.det.status(), "")  # timer restarted at 0.3

    def test_active_holds_through_short_gap(self):
        self.det._apply_near_result(True, 0.0)
        self.det._apply_near_result(True, 0.3)
        self.det._apply_near_result(False, 0.8)
        self.assertEqual(self.det.status(), "phone call")

    def test_active_releases_after_release_seconds(self):
        self.det._apply_near_result(True, 0.0)
        self.det._apply_near_result(True, 0.3)
        self.det._apply_near_result(False, 1.4)
        self.assertEqual(self.det.status(), "")

    def test_release_never_shorter_than_scan_cadence(self):
        conf.PHONE_RELEASE_SECONDS = 0.1
        conf.HANDS_SCAN_INTERVAL = 0.4  # min release = 1.0s
        self.det._apply_near_result(True, 0.0)
        self.det._apply_near_result(True, 0.3)
        self.det._apply_near_result(False, 0.9)
        self.assertEqual(self.det.status(), "phone call")


class TestPhoneCallUpdate(_ConfPatch):
    def test_stale_hands_do_not_count(self):
        face = _face_landmarks()
        hand = _hand_landmarks((0.30, 0.45))
        self.det.update(face, [hand], aspect=1.0, fresh=False, now=0.0)
        self.assertFalse(self.det.wants_burst())
        self.det.update(face, [hand], aspect=1.0, fresh=True, now=0.0)
        self.assertTrue(self.det.wants_burst())

    def test_stale_frames_still_release(self):
        face = _face_landmarks()
        hand = _hand_landmarks((0.30, 0.45))
        self.det.update(face, [hand], aspect=1.0, fresh=True, now=0.0)
        self.det.update(face, [hand], aspect=1.0, fresh=True, now=0.3)
        self.assertEqual(self.det.status(), "phone call")
        self.det.update(face, None, aspect=1.0, fresh=False, now=2.0)
        self.assertEqual(self.det.status(), "")

    def test_ear_hand_activates(self):
        face = _face_landmarks()
        hand = _hand_landmarks((0.30, 0.45))
        for t in (0.0, 0.15, 0.3):
            self.det.update(face, [hand], aspect=320 / 240, fresh=True, now=t)
        self.assertEqual(self.det.status(), "phone call")

    def test_mouth_hand_ignored(self):
        face = _face_landmarks()
        hand = _hand_landmarks((0.50, 0.56))
        for t in (0.0, 0.2, 0.4, 0.6, 0.8):
            self.det.update(face, [hand], aspect=320 / 240, fresh=True, now=t)
        self.assertEqual(self.det.status(), "")

    def test_no_face_clears_candidate(self):
        face = _face_landmarks()
        hand = _hand_landmarks((0.30, 0.45))
        self.det.update(face, [hand], aspect=1.0, fresh=True, now=0.0)
        self.det.update(None, None, fresh=False, now=0.1)
        self.assertFalse(self.det.wants_burst())
        self.assertEqual(self.det.status(), "")

    def test_disable_clears_state_immediately(self):
        self.det._apply_near_result(True, 0.0)
        self.det._apply_near_result(True, 0.3)
        self.assertEqual(self.det.status(), "phone call")
        conf.PHONE_DETECTION_ENABLED = False
        self.det.update(_face_landmarks(), None, fresh=True, now=0.4)
        self.assertEqual(self.det.status(), "")
        self.assertIsNone(self.det._first_near)


if __name__ == "__main__":
    unittest.main()
