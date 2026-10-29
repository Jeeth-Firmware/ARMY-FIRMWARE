"""Posture-based phone-call detection for Pi Zero 2 W.

Uses shared MediaPipe Hands (via SharedHandsRunner) + FaceMesh ear anchors.
No object detection / no extra neural nets beyond existing MediaPipe Hands.
"""

from __future__ import annotations

import math
import time

import facial_tracking.conf as conf

# FaceMesh ear / width landmarks
_FACE_W_L, _FACE_W_R = 234, 454
_LEFT_EAR = (234, 93)
_RIGHT_EAR = (454, 323)
_UPPER_LIP, _LOWER_LIP = 13, 14

# Sparse hand keypoints (wrist, thumb tip, index MCP/tip, middle tip, pinky MCP/tip)
_HAND_IDS = (0, 4, 5, 8, 12, 17, 20)
# Hand near mouth (not ear) — exclude from PhoneCall to avoid cigarette false positives
_MOUTH_EXCLUDE_RATIO = 0.35
# Fresh Hands misses tolerated while confirming (hand partly hidden by the phone)
_CANDIDATE_MISS_TOLERANCE = 1


class PhoneCallDetector:
    """Detect hand held near an ear (driver on a phone call).

    Time-based: fires once the hand has been at the ear for
    PHONE_CONFIRM_SECONDS, clears after PHONE_RELEASE_SECONDS without a hit.
    """

    def __init__(self):
        self._first_near = None  # monotonic time of first hit in this candidate
        self._last_near = None   # monotonic time of most recent hit
        self._misses = 0         # consecutive fresh misses while confirming
        self._active = False

    def close(self):
        """No owned Hands model — SharedHandsRunner owns inference."""
        return

    def status(self) -> str:
        return "phone call" if self._active else ""

    def wants_burst(self) -> bool:
        """True while a candidate is being confirmed — Hands should run every frame."""
        return self._first_near is not None and not self._active

    def _reset_detection_state(self):
        self._first_near = None
        self._last_near = None
        self._misses = 0
        self._active = False

    @staticmethod
    def _norm_dist(a, b, aspect: float) -> float:
        dx = (a.x - b.x) * aspect
        dy = a.y - b.y
        return math.sqrt(dx * dx + dy * dy)

    @staticmethod
    def _mouth_center(face_landmarks):
        fl = face_landmarks.landmark
        u, lo = fl[_UPPER_LIP], fl[_LOWER_LIP]

        class _Pt:
            __slots__ = ("x", "y")

            def __init__(self, x, y):
                self.x = x
                self.y = y

        return _Pt((u.x + lo.x) * 0.5, (u.y + lo.y) * 0.5)

    def _min_hand_dist(self, hand_landmarks, anchors, aspect: float) -> float:
        hl = hand_landmarks.landmark
        min_d = float("inf")
        for hid in _HAND_IDS:
            hp = hl[hid]
            for ap in anchors:
                d = self._norm_dist(hp, ap, aspect)
                if d < min_d:
                    min_d = d
        return min_d

    def _hand_near_ear(self, face_landmarks, hand_landmarks, aspect: float) -> bool:
        """True only when hand is at an ear — not when it is at the mouth (smoking)."""
        fl = face_landmarks.landmark
        face_w = self._norm_dist(fl[_FACE_W_L], fl[_FACE_W_R], aspect)
        if face_w <= 1e-6:
            return False

        ear_pts = (
            fl[_LEFT_EAR[0]], fl[_LEFT_EAR[1]],
            fl[_RIGHT_EAR[0]], fl[_RIGHT_EAR[1]],
        )
        mouth = self._mouth_center(face_landmarks)
        min_ear = self._min_hand_dist(hand_landmarks, ear_pts, aspect)
        min_mouth = self._min_hand_dist(hand_landmarks, (mouth,), aspect)

        # Hand-to-mouth posture must never count as PhoneCall
        mouth_zone = _MOUTH_EXCLUDE_RATIO * face_w
        if min_mouth <= mouth_zone:
            return False
        if min_mouth <= min_ear:
            return False

        threshold = conf.PHONE_EAR_DIST_RATIO * face_w
        return min_ear < threshold

    def _apply_near_result(self, near: bool, now: float):
        if near:
            if self._first_near is None:
                self._first_near = now
            self._last_near = now
            self._misses = 0
            if now - self._first_near >= conf.PHONE_CONFIRM_SECONDS:
                self._active = True
        elif not self._active and self._first_near is not None:
            self._misses += 1
            if self._misses > _CANDIDATE_MISS_TOLERANCE:
                self._reset_detection_state()
        self._expire(now)

    def _expire(self, now: float):
        if not self._active or self._last_near is None:
            return
        # Release must outlast the idle Hands cadence or an active call would flicker
        release = max(conf.PHONE_RELEASE_SECONDS, 2.5 * conf.HANDS_SCAN_INTERVAL)
        if now - self._last_near > release:
            self._reset_detection_state()

    def update(self, face_landmarks, hands_landmarks=None, aspect: float = 1.0,
               fresh: bool = False, now: float = None):
        """Update from shared Hands result.

        Hits/misses are only counted when fresh=True (Hands just ran);
        release timing is checked on every call.
        """
        if not conf.PHONE_DETECTION_ENABLED:
            self._reset_detection_state()
            return

        if now is None:
            now = time.monotonic()

        if face_landmarks is None:
            if self._active:
                self._expire(now)
            else:
                self._reset_detection_state()
            return

        if not fresh:
            self._expire(now)
            return

        near = False
        if hands_landmarks:
            for hand_lms in hands_landmarks:
                if self._hand_near_ear(face_landmarks, hand_lms, aspect):
                    near = True
                    break

        self._apply_near_result(near, now)
