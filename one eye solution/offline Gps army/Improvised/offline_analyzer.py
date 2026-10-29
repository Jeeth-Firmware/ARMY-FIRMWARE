#!/usr/bin/env python3
"""
Offline Analyzer — Process folders of images for drowsiness/yawning detection.

Walks a root folder containing event subfolders, runs MediaPipe FaceMesh on each
image, computes EAR/MAR, determines driver status, draws full mesh annotations
(eyes, iris, lips contours), and overwrites the original image.

Usage:
    python3 offline_analyzer.py /path/to/root/folder

Folder structure expected:
    root_folder/
    ├── event_folder_1/
    │   ├── frame_001.jpg
    │   └── ...
    └── event_folder_2/
        └── ...
"""

import sys
import os
import cv2
import numpy as np
import mediapipe as mp

# --- Constants (same as live system) ---
EYE_CLOSED_THRESHOLD = 0.20
MOUTH_OPEN_THRESHOLD = 0.8

# MediaPipe landmark indices for EAR/MAR calculation (same as facial_tracking/conf.py)
LEFT_EYE_CALC = [263, 362, 386, 374]
RIGHT_EYE_CALC = [133, 33, 159, 145]
LIPS_CALC = [291, 61, 13, 14]

VALID_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp'}

FONT = cv2.FONT_HERSHEY_SIMPLEX

# Full MediaPipe face mesh connection indices for drawing contours (iris + lips only)
_LIPS_CONNECTIONS = frozenset([
    (0, 267), (13, 312), (14, 317), (17, 314), (37, 0),
    (39, 37), (40, 39), (61, 146), (61, 185), (78, 95),
    (78, 191), (80, 81), (81, 82), (82, 13), (84, 17),
    (87, 14), (88, 178), (91, 181), (95, 88), (146, 91),
    (178, 87), (181, 84), (185, 40), (191, 80), (267, 269),
    (269, 270), (270, 409), (310, 415), (311, 310), (312, 311),
    (314, 405), (317, 402), (318, 324), (321, 375), (324, 308),
    (375, 291), (402, 318), (405, 321), (409, 291), (415, 308)
])
_IRISES_CONNECTIONS = frozenset([
    (469, 470), (470, 471), (471, 472), (472, 469),
    (474, 475), (475, 476), (476, 477), (477, 474)
])


def _draw_connections(frame, face_landmarks, connections, color, thickness=1):
    """Draw landmark connections on the frame using cv2."""
    h, w = frame.shape[:2]
    for start_idx, end_idx in connections:
        start = face_landmarks.landmark[start_idx]
        end = face_landmarks.landmark[end_idx]
        pt1 = (int(start.x * w), int(start.y * h))
        pt2 = (int(end.x * w), int(end.y * h))
        cv2.line(frame, pt1, pt2, color, thickness, cv2.LINE_AA)
        cv2.circle(frame, pt1, 1, color, -1, cv2.LINE_AA)
        cv2.circle(frame, pt2, 1, color, -1, cv2.LINE_AA)


def get_landmark_positions(frame, face_landmarks, indices):
    """Extract pixel positions for given landmark indices."""
    h, w = frame.shape[:2]
    positions = []
    for idx in indices:
        lm = face_landmarks.landmark[idx]
        cx = int(lm.x * w)
        cy = int(lm.y * h)
        positions.append((cx, cy))
    return positions


def compute_ear(eye_pos):
    """Compute Eye Aspect Ratio (vertical / horizontal distance)."""
    h_dist = eye_pos[0][0] - eye_pos[1][0]
    if h_dist == 0:
        return 0
    v_dist = eye_pos[3][1] - eye_pos[2][1]
    return v_dist / h_dist


def compute_mar(lips_pos):
    """Compute Mouth Aspect Ratio (vertical / horizontal distance)."""
    h_dist = lips_pos[0][0] - lips_pos[1][0]
    if h_dist == 0:
        return 0
    v_dist = lips_pos[3][1] - lips_pos[2][1]
    return v_dist / h_dist


def annotate_image(frame, face_landmarks, ear, mar, status):
    """Draw iris (only when eyes open) + lips annotations onto the frame."""
    if status != 'SLEEPING':
        _draw_connections(frame, face_landmarks, _IRISES_CONNECTIONS, (0, 255, 255))
    _draw_connections(frame, face_landmarks, _LIPS_CONNECTIONS, (255, 200, 0))


def annotate_noface(frame):
    """No face detected — nothing to draw."""
    pass


def process_image(image_path, face_mesh):
    """Process a single image: detect, annotate, overwrite."""
    frame = cv2.imread(image_path)
    if frame is None:
        print(f"  Skipping corrupted file: {os.path.basename(image_path)}")
        return False

    # Run MediaPipe FaceMesh
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    rgb.flags.writeable = False
    result = face_mesh.process(rgb)

    if not result.multi_face_landmarks:
        annotate_noface(frame)
        cv2.imwrite(image_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        print(f"  {os.path.basename(image_path)}: NO FACE")
        return True

    face_landmarks = result.multi_face_landmarks[0]

    # Extract landmark positions for EAR/MAR calculation
    left_eye_pos = get_landmark_positions(frame, face_landmarks, LEFT_EYE_CALC)
    right_eye_pos = get_landmark_positions(frame, face_landmarks, RIGHT_EYE_CALC)
    lips_pos = get_landmark_positions(frame, face_landmarks, LIPS_CALC)

    # Compute ratios
    ear_left = compute_ear(left_eye_pos)
    ear_right = compute_ear(right_eye_pos)
    ear = (ear_left + ear_right) / 2
    mar = compute_mar(lips_pos)

    # Determine status (per-image, no frame counting)
    if ear < EYE_CLOSED_THRESHOLD:
        status = "SLEEPING"
    elif mar > MOUTH_OPEN_THRESHOLD:
        status = "YAWNING"
    else:
        status = "ACTIVE"

    # Draw full mesh + text overlays
    annotate_image(frame, face_landmarks, ear, mar, status)

    cv2.imwrite(image_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    print(f"  {os.path.basename(image_path)}: {status} (EAR={ear:.2f}, MAR={mar:.2f})")
    return True


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 offline_analyzer.py /path/to/root/folder")
        sys.exit(1)

    root_folder = sys.argv[1]
    if not os.path.isdir(root_folder):
        print(f"Error: '{root_folder}' is not a valid directory")
        sys.exit(1)

    # Initialize MediaPipe FaceMesh once, reuse across all images
    # Use tasks API (works on all mediapipe versions)
    try:
        # Old API (mp.solutions)
        _fm = mp.solutions.face_mesh
        face_mesh = _fm.FaceMesh(
            static_image_mode=True,
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=0.5
        )
    except AttributeError:
        # New API (mp.tasks) — create a wrapper that mimics the old interface
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core import base_options as base_options_module
        import tempfile
        model_path = os.path.join(os.path.dirname(mp.__file__), 'modules', 'face_landmarker', 'face_landmarker.task')
        if not os.path.exists(model_path):
            # Download or find model
            print("Error: MediaPipe face landmarker model not found. Please use mediapipe with solutions API.")
            sys.exit(1)
        base_options = base_options_module.BaseOptions(model_asset_path=model_path)
        options = vision.FaceLandmarkerOptions(
            base_options=base_options,
            num_faces=1,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False
        )
        face_mesh = vision.FaceLandmarker.create_from_options(options)

    # Get sorted list of subfolders
    subfolders = sorted([
        d for d in os.listdir(root_folder)
        if os.path.isdir(os.path.join(root_folder, d))
    ])

    if not subfolders:
        print(f"No subfolders found in '{root_folder}'")
        sys.exit(0)

    total_processed = 0
    total_skipped = 0
    total_folders = 0

    for folder_name in subfolders:
        folder_path = os.path.join(root_folder, folder_name)
        files = sorted(os.listdir(folder_path))

        if not files:
            print(f"[{folder_name}] Empty folder, skipping")
            continue

        total_folders += 1
        folder_processed = 0
        folder_skipped = 0

        print(f"\n[{folder_name}] Processing {len(files)} files...")

        for filename in files:
            filepath = os.path.join(folder_path, filename)

            # Skip directories
            if os.path.isdir(filepath):
                continue

            # Check extension
            ext = os.path.splitext(filename)[1].lower()
            if ext not in VALID_EXTENSIONS:
                print(f"  Skipping non-image file: {filename}")
                folder_skipped += 1
                continue

            # Process image with error resilience
            try:
                if process_image(filepath, face_mesh):
                    folder_processed += 1
                else:
                    folder_skipped += 1
            except Exception as e:
                print(f"  Error processing {filename}: {e}")
                folder_skipped += 1

        print(f"[{folder_name}] Done: {folder_processed} processed, {folder_skipped} skipped")
        total_processed += folder_processed
        total_skipped += folder_skipped

    face_mesh.close()

    print(f"\n{'='*50}")
    print(f"Total: {total_folders} folders, {total_processed} images processed, {total_skipped} skipped")


if __name__ == '__main__':
    main()
