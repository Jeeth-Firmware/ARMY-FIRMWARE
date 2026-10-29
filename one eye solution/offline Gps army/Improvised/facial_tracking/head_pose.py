"""Head-pose attention detection from FaceMesh landmarks (no eye dependency).

Uses only stable face anchors so detection continues when one eye is
occluded, off-frame, or hidden by sunglasses:

  nose tip 1, chin 152, left cheek 234, right cheek 454, forehead 10

LookingAway also requires exactly one eye closed for LOOKAWAY_ONE_EYE_SECONDS
before alerting / buzzing (passed in from FacialTracker each frame).
"""

from __future__ import annotations

import math
import time

import facial_tracking.conf as conf

# FaceMesh landmarks that remain reliable with single-eye visibility
_NOSE_TIP = 1
_CHIN = 152
_LEFT_CHEEK = 234
_RIGHT_CHEEK = 454
_FOREHEAD = 10


class HeadPoseAttentionDetector:
    """Estimate pitch/yaw/roll and emit LookingDown / LookingAway."""

    def __init__(self):
        self.attention_status = "Active"
        self.pitch_deg = 0.0
        self.yaw_deg = 0.0
        self.roll_deg = 0.0

        self._down_streak = 0
        self._away_streak = 0
        self._down_misses = 0
        self._away_misses = 0

        self._looking_down = False
        self._looking_away = False

        # LookingAway + one-eye-closed hold timer
        self._away_one_eye_start = None
        self._away_one_eye_misses = 0

    def status(self) -> str:
        return self.attention_status

    def reset(self):
        self.attention_status = "Active"
        self.pitch_deg = 0.0
        self.yaw_deg = 0.0
        self.roll_deg = 0.0
        self._down_streak = 0
        self._away_streak = 0
        self._down_misses = 0
        self._away_misses = 0
        self._looking_down = False
        self._looking_away = False
        self._away_one_eye_start = None
        self._away_one_eye_misses = 0

    @staticmethod
    def _norm_dist(a, b, aspect: float) -> float:
        dx = (a.x - b.x) * aspect
        dy = a.y - b.y
        return math.sqrt(dx * dx + dy * dy)

    def estimate(self, face_landmarks, aspect: float = 1.0):
        """Compute pitch / yaw / roll in approximate degrees from 5 anchors."""
        fl = face_landmarks.landmark
        nose = fl[_NOSE_TIP]
        chin = fl[_CHIN]
        left = fl[_LEFT_CHEEK]
        right = fl[_RIGHT_CHEEK]
        forehead = fl[_FOREHEAD]

        # Roll: tilt of cheek-to-cheek line (kept for diagnostics / estimate API)
        roll = math.degrees(math.atan2(
            (right.y - left.y),
            (right.x - left.x) * aspect + 1e-9,
        ))

        # Yaw: nose nearer one cheek than the other
        d_left = self._norm_dist(nose, left, aspect)
        d_right = self._norm_dist(nose, right, aspect)
        face_w = d_left + d_right
        if face_w < 1e-6:
            yaw = 0.0
        else:
            # Positive = looking toward camera-left (nose closer to right cheek)
            yaw = ((d_right - d_left) / face_w) * 90.0

        # Pitch: nose vs forehead–chin midpoint (down = positive)
        face_h = abs(chin.y - forehead.y)
        if face_h < 1e-6:
            pitch = 0.0
        else:
            mid_y = (forehead.y + chin.y) * 0.5
            pitch = ((nose.y - mid_y) / face_h) * 90.0

        self.pitch_deg = pitch
        self.yaw_deg = yaw
        self.roll_deg = roll
        return pitch, yaw, roll

    def _update_streak(self, active: bool, streak_attr: str, miss_attr: str,
                       flag_attr: str, need_frames: int, miss_tol: int) -> bool:
        streak = getattr(self, streak_attr)
        misses = getattr(self, miss_attr)
        if active:
            streak += 1
            misses = 0
            if streak >= need_frames:
                setattr(self, flag_attr, True)
        else:
            misses += 1
            if misses > miss_tol:
                streak = 0
                misses = 0
                setattr(self, flag_attr, False)
        setattr(self, streak_attr, streak)
        setattr(self, miss_attr, misses)
        return getattr(self, flag_attr)

    def _update_looking_away(self, yaw_away: bool, one_eye_closed: bool) -> bool:
        """LookingAway only after yaw + one eye closed for LOOKAWAY_ONE_EYE_SECONDS."""
        miss_tol = conf.LOOKDOWN_MISS_TOLERANCE
        hold_s = conf.LOOKAWAY_ONE_EYE_SECONDS
        now = time.monotonic()

        if yaw_away and one_eye_closed:
            self._away_one_eye_misses = 0
            if self._away_one_eye_start is None:
                self._away_one_eye_start = now
            elif (now - self._away_one_eye_start) >= hold_s:
                self._looking_away = True
        else:
            self._away_one_eye_misses += 1
            if self._away_one_eye_misses > miss_tol:
                self._away_one_eye_start = None
                self._away_one_eye_misses = 0
                self._looking_away = False

        return self._looking_away

    def update(self, face_landmarks, frame_shape=None, one_eye_closed=False):
        """Update attention state from FaceMesh. No-op when disabled / no face.

        Args:
            one_eye_closed: True when exactly one eye is closed this frame.
                Required (with yaw) for LookingAway / buzzer after 3s.
        """
        if not conf.LOOKDOWN_ENABLED:
            self.reset()
            return

        if face_landmarks is None:
            self.reset()
            return

        aspect = 1.0
        if frame_shape is not None and len(frame_shape) >= 2:
            h, w = frame_shape[0], frame_shape[1]
            if h:
                aspect = w / float(h)

        pitch, yaw, _roll = self.estimate(face_landmarks, aspect)

        miss_tol = conf.LOOKDOWN_MISS_TOLERANCE
        looking_down = self._update_streak(
            pitch >= conf.LOOKDOWN_PITCH_THRESHOLD,
            "_down_streak", "_down_misses", "_looking_down",
            conf.LOOKDOWN_FRAMES, miss_tol,
        )
        yaw_away = abs(yaw) >= conf.LOOKAWAY_YAW_THRESHOLD
        looking_away = self._update_looking_away(yaw_away, bool(one_eye_closed))

        # Priority: LookingDown > LookingAway > Active
        if looking_down:
            self.attention_status = "LookingDown"
        elif looking_away:
            self.attention_status = "LookingAway"
        else:
            self.attention_status = "Active"
