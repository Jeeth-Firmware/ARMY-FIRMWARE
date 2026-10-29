import os

# Performance mode: set HEADLESS=1 in production (no display attached)
# Skips all drawing operations (draw_mesh, draw_iris, putText) to save CPU
HEADLESS = os.getenv('HEADLESS', '1').strip().lower() in {'1', 'true', 'yes', 'on'}

# Camera parameters
CAM_ID  = 0
FRAME_W = int(os.getenv('FRAME_W', '640'))
FRAME_H = int(os.getenv('FRAME_H', '480'))

# MediaPipe settings - iris refinement improves eye/lip landmark accuracy
REFINE_LANDMARKS = os.getenv('REFINE_LANDMARKS', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
MIN_DETECTION_CONFIDENCE = float(os.getenv('MIN_DETECTION_CONFIDENCE', '0.5'))
MIN_TRACKING_CONFIDENCE = float(os.getenv('MIN_TRACKING_CONFIDENCE', '0.5'))

# Target FPS for detection loop (lower = less CPU)
TARGET_DETECTION_FPS = int(os.getenv('TARGET_DETECTION_FPS', '10'))
# Camera capture FPS requested from the driver (0 = driver default, usually 30).
# Detection never exceeds ~10 FPS, so capturing faster only burns CPU on decode.
CAMERA_FPS = int(os.getenv('CAMERA_FPS', '15'))

# Plot configuration
TEXT_COLOR = (102,51,0)
LM_COLOR   = (51,255,51)
CT_COLOR   = (243,166,56)
WARN_COLOR = (76,76,255)

# Target landmarks
LEFT_EYE   = [263, 362, 386, 374, 473, 474, 475, 476, 477]
RIGHT_EYE  = [133,  33, 159, 145, 468, 469, 470, 471, 472]
LIPS       = [291,  61,  13,  14]

# Threshold (all env-configurable for per-device tuning)
GAZE_LEFT  = float(os.getenv('GAZE_LEFT', '0.2'))
GAZE_RIGHT = float(os.getenv('GAZE_RIGHT', '0.8'))
EYE_CLOSED = float(os.getenv('EYE_CLOSED', '0.20'))
MOUTH_OPEN = float(os.getenv('MOUTH_OPEN', '0.8'))
# Sleeping fires once both eyes stay closed this long (time-based, independent of FPS).
# Mutable at runtime via settings reload (DB key: sleep_seconds)
SLEEP_SECONDS = float(os.getenv('SLEEP_SECONDS', '1.5'))
# Minimum closed frames as well, so one slow frame can't satisfy the timer alone
SLEEP_MIN_FRAMES = int(os.getenv('SLEEP_MIN_FRAMES', '3'))

# Phone-call detection — time-based so alert latency does not depend on Pi FPS.
# PHONE_DETECTION_ENABLED / PHONE_CONFIRM_SECONDS / PHONE_BUZZ_SECONDS /
# PHONE_EAR_DIST_RATIO are mutable at runtime via settings reload
PHONE_DETECTION_ENABLED = os.getenv('PHONE_DETECTION_ENABLED', '1').strip().lower() in {
    '1', 'true', 'yes', 'on'
}
# Hand must stay at the ear this long before PhoneCall fires (0 = first hit)
PHONE_CONFIRM_SECONDS = float(os.getenv('PHONE_CONFIRM_SECONDS', '0.3'))
# PhoneCall clears after the hand has been away from the ear this long
PHONE_RELEASE_SECONDS = float(os.getenv('PHONE_RELEASE_SECONDS', '1.0'))
# Buzz for the first N seconds of a call, then report silently (0 = never buzz)
PHONE_BUZZ_SECONDS = float(os.getenv('PHONE_BUZZ_SECONDS', '10'))
# Hand-to-ear distance (x face width) that counts as "at the ear"
PHONE_EAR_DIST_RATIO = float(os.getenv('PHONE_EAR_DIST_RATIO', '0.5'))
# Hands idle scan interval (seconds). While a phone candidate is being
# confirmed, Hands runs every frame so the alert fires immediately.
HANDS_SCAN_INTERVAL = float(os.getenv('HANDS_SCAN_INTERVAL', '0.4'))
PHONE_MIN_DETECTION_CONFIDENCE = float(os.getenv('PHONE_MIN_DETECTION_CONFIDENCE', '0.5'))
PHONE_MIN_TRACKING_CONFIDENCE = float(os.getenv('PHONE_MIN_TRACKING_CONFIDENCE', '0.5'))
HANDS_MIN_DETECTION_CONFIDENCE = float(os.getenv(
    'HANDS_MIN_DETECTION_CONFIDENCE',
    os.getenv('PHONE_MIN_DETECTION_CONFIDENCE', '0.5'),
))
HANDS_MIN_TRACKING_CONFIDENCE = float(os.getenv(
    'HANDS_MIN_TRACKING_CONFIDENCE',
    os.getenv('PHONE_MIN_TRACKING_CONFIDENCE', '0.5'),
))

# Head-pose attention (LookingDown / LookingAway — no eye landmark dependency)
# LOOKDOWN_ENABLED is mutable at runtime via settings reload
LOOKDOWN_ENABLED = os.getenv('LOOKDOWN_ENABLED', '1').strip().lower() in {
    '1', 'true', 'yes', 'on'
}
LOOKDOWN_PITCH_THRESHOLD = float(os.getenv('LOOKDOWN_PITCH_THRESHOLD', '20'))
LOOKAWAY_YAW_THRESHOLD = float(os.getenv('LOOKAWAY_YAW_THRESHOLD', '25'))
LOOKDOWN_FRAMES = int(os.getenv('LOOKDOWN_FRAMES', '8'))
LOOKAWAY_FRAMES = int(os.getenv('LOOKAWAY_FRAMES', '8'))
LOOKDOWN_MISS_TOLERANCE = int(os.getenv('LOOKDOWN_MISS_TOLERANCE', '2'))
# LookingAway buzz: require yaw + exactly one eye closed for this many seconds
LOOKAWAY_ONE_EYE_SECONDS = float(os.getenv('LOOKAWAY_ONE_EYE_SECONDS', '3'))
