"""Single shared MediaPipe Hands runner for PhoneCall.

Pi Zero 2 W — one throttled Hands graph for posture detection.
"""

from __future__ import annotations

import time

import cv2
import mediapipe as mp

import facial_tracking.conf as conf


class SharedHandsRunner:
    """Lazy, throttled Hands — one process() call for phone posture."""

    def __init__(self):
        self._hands = None
        self._last_run = 0.0  # monotonic time of last inference
        self._cached = None  # multi_hand_landmarks from last run (or None)
        self.last_latency_ms = None

    def close(self):
        if self._hands is not None:
            self._hands.close()
            self._hands = None

    @staticmethod
    def needed() -> bool:
        return conf.PHONE_DETECTION_ENABLED

    def _ensure(self):
        if self._hands is not None:
            return
        self._hands = mp.solutions.hands.Hands(
            static_image_mode=False,
            max_num_hands=1,  # driver cabin — one hand near face is enough
            model_complexity=0,
            min_detection_confidence=conf.HANDS_MIN_DETECTION_CONFIDENCE,
            min_tracking_confidence=conf.HANDS_MIN_TRACKING_CONFIDENCE,
        )

    def process(self, frame, face_present: bool, burst: bool = False):
        """Run or reuse Hands.

        Runs at most once per HANDS_SCAN_INTERVAL seconds, or on every call
        when burst=True (phone candidate being confirmed).

        Returns:
            (multi_hand_landmarks_or_None, fresh: bool)
            fresh=True only when inference ran this call (detectors should
            update streaks only then).
        """
        if not self.needed():
            self._cached = None
            return None, False

        if not face_present or frame is None:
            self._cached = None
            return None, False

        now = time.monotonic()
        if not burst and now - self._last_run < conf.HANDS_SCAN_INTERVAL:
            return self._cached, False
        self._last_run = now

        self._ensure()
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False

        t0 = time.perf_counter()
        result = self._hands.process(rgb)
        self.last_latency_ms = (time.perf_counter() - t0) * 1000.0

        self._cached = result.multi_hand_landmarks
        return self._cached, True
