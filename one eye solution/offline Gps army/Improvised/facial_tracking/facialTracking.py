import cv2
import time
import facial_tracking.conf as conf

from facial_tracking.faceMesh import FaceMesh
from facial_tracking.eye import Eye
from facial_tracking.lips import Lips
from facial_tracking.phone_call import PhoneCallDetector
from facial_tracking.hands_runner import SharedHandsRunner
from facial_tracking.head_pose import HeadPoseAttentionDetector


class FacialTracker:
    """
    The object of facial tracking, predicting status of eye, iris, mouth,
    and head-pose attention (LookingDown / LookingAway).
    """

    def __init__(self):
        self.fm = FaceMesh()
        self.left_eye = None
        self.right_eye = None
        self.lips = None
        self.left_eye_closed_frames = 0
        self.right_eye_closed_frames = 0
        self.detected = False

        # Allow 1 noisy "open" frame without resetting closed counter
        self.left_eye_gap_frames = 0
        self.right_eye_gap_frames = 0
        # Monotonic time each eye's current closed streak began (None = open)
        self.left_eye_closed_since = None
        self.right_eye_closed_since = None

        self.hands_runner = SharedHandsRunner()
        self.phone_detector = PhoneCallDetector()
        self.head_pose = HeadPoseAttentionDetector()
        self.phone_status = ''
        self.eyes_status = ''
        self.yawn_status = ''
        self.attention_status = 'Active'

    def process_frame(self, frame):
        """Process the frame to analyze facial status."""
        self.detected = False
        self.fm.process_frame(frame)
        self.fm.draw_mesh_lips()  # no-op in HEADLESS mode

        face_landmarks = None
        if self.fm.mesh_result.multi_face_landmarks:
            self.detected = True
            for face_landmarks in self.fm.mesh_result.multi_face_landmarks:
                self.left_eye = Eye(frame, face_landmarks, conf.LEFT_EYE)
                self.right_eye = Eye(frame, face_landmarks, conf.RIGHT_EYE)
                self.lips = Lips(frame, face_landmarks, conf.LIPS)
                self._check_eyes_status()
                self._check_yawn_status()
                break  # Only process the first face

        one_eye_closed = False
        if self.left_eye is not None and self.right_eye is not None:
            left_c = self.left_eye.eye_closed()
            right_c = self.right_eye.eye_closed()
            one_eye_closed = left_c != right_c
        self.head_pose.update(
            face_landmarks,
            frame.shape if frame is not None else None,
            one_eye_closed=one_eye_closed,
        )
        self.attention_status = self.head_pose.status()

        # Shared Hands for PhoneCall (throttled)
        h, w = frame.shape[:2]
        aspect = (w / h) if h else 1.0
        hands_lms, hands_fresh = self.hands_runner.process(
            frame,
            face_present=(face_landmarks is not None),
            burst=self.phone_detector.wants_burst(),
        )
        self.phone_detector.update(face_landmarks, hands_lms, aspect, hands_fresh)
        self.phone_status = self.phone_detector.status()

    def close(self):
        """Release MediaPipe resources."""
        try:
            self.phone_detector.close()
        except Exception:
            pass
        try:
            self.hands_runner.close()
        except Exception:
            pass
        try:
            if getattr(self.fm, 'face_mesh', None) is not None:
                self.fm.face_mesh.close()
        except Exception:
            pass

    def _check_eyes_status(self):
        self.eyes_status = ''
        left_closed = self.left_eye.eye_closed()
        right_closed = self.right_eye.eye_closed()
        now = time.monotonic()

        if left_closed:
            self.left_eye_closed_frames += 1
            self.left_eye_gap_frames = 0
            if self.left_eye_closed_since is None:
                self.left_eye_closed_since = now
        else:
            self.left_eye_gap_frames += 1
            if self.left_eye_gap_frames > 1:
                self.left_eye_closed_frames = 0
                self.left_eye_closed_since = None
            if not conf.HEADLESS and self.left_eye.iris:
                self.left_eye.iris.draw_iris(True)

        if right_closed:
            self.right_eye_closed_frames += 1
            self.right_eye_gap_frames = 0
            if self.right_eye_closed_since is None:
                self.right_eye_closed_since = now
        else:
            self.right_eye_gap_frames += 1
            if self.right_eye_gap_frames > 1:
                self.right_eye_closed_frames = 0
                self.right_eye_closed_since = None
            if not conf.HEADLESS and self.right_eye.iris:
                self.right_eye.iris.draw_iris(True)

        if self._left_eye_closed(now) and self._right_eye_closed(now):
            self.eyes_status = 'eye closed'
            return

        # Gaze labels unused by the event loop — skip in HEADLESS to save work
        if conf.HEADLESS:
            return

        if not left_closed and not right_closed:
            if self.left_eye.gaze_right() and self.right_eye.gaze_right():
                self.eyes_status = 'gazing right'
            elif self.left_eye.gaze_left() and self.right_eye.gaze_left():
                self.eyes_status = 'gazing left'
            elif self.left_eye.gaze_center() and self.right_eye.gaze_center():
                self.eyes_status = 'gazing center'

    def _check_yawn_status(self):
        self.yawn_status = ''
        if self.lips.mouth_open():
            self.yawn_status = 'yawning'

    # SLEEP_SECONDS is read at call time so DB / live settings changes apply
    @staticmethod
    def _closed_long_enough(since, frames, now):
        return (
            since is not None
            and frames >= conf.SLEEP_MIN_FRAMES
            and now - since >= conf.SLEEP_SECONDS
        )

    def _left_eye_closed(self, now):
        return self._closed_long_enough(self.left_eye_closed_since, self.left_eye_closed_frames, now)

    def _right_eye_closed(self, now):
        return self._closed_long_enough(self.right_eye_closed_since, self.right_eye_closed_frames, now)


def main():
    cap = cv2.VideoCapture(conf.CAM_ID)
    cap.set(3, conf.FRAME_W)
    cap.set(4, conf.FRAME_H)
    facial_tracker = FacialTracker()
    ptime = 0
    ctime = 0

    while cap.isOpened():
        success, frame = cap.read()
        if not success:
            continue

        facial_tracker.process_frame(frame)

        ctime = time.time()
        fps = 1 / (ctime - ptime)
        ptime = ctime

        frame = cv2.flip(frame, 1)
        cv2.putText(frame, f'FPS: {int(fps)}', (30, 30), 0, 0.6, conf.TEXT_COLOR, 1, lineType=cv2.LINE_AA)

        if facial_tracker.detected:
            cv2.putText(frame, f'{facial_tracker.eyes_status}', (30, 70), 0, 0.8, conf.WARN_COLOR, 2, lineType=cv2.LINE_AA)
            cv2.putText(frame, f'{facial_tracker.yawn_status}', (30, 110), 0, 0.8, conf.WARN_COLOR, 2, lineType=cv2.LINE_AA)
            y = 150
            if facial_tracker.phone_status:
                cv2.putText(frame, f'{facial_tracker.phone_status}', (30, y), 0, 0.8, conf.WARN_COLOR, 2, lineType=cv2.LINE_AA)
                y += 40
            if facial_tracker.attention_status and facial_tracker.attention_status != 'Active':
                cv2.putText(frame, f'{facial_tracker.attention_status}', (30, y), 0, 0.8, conf.WARN_COLOR, 2, lineType=cv2.LINE_AA)

        cv2.imshow('Facial tracking', frame)
        key = cv2.waitKey(1)
        if key == ord('q'):
            break

    facial_tracker.close()
    cap.release()
    cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
