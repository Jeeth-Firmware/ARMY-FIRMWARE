# Event Image Annotation & Offline Analyzer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add eye/mouth/iris landmark annotations with EAR/MAR values and status labels to saved critical-event images in the live system, and create a standalone offline analyzer that processes folders of images with the same detection and annotation logic.

**Architecture:** Inline approach — annotation drawing code is added directly into `save_image()` for the live system, and a separate self-contained `offline_analyzer.py` duplicates the detection + drawing logic. No shared module.

**Tech Stack:** MediaPipe FaceMesh, OpenCV, NumPy (all existing dependencies)

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `facil_event_capture.py` | Add landmark annotation drawing to `save_image()`, pass landmark data from main loop |
| Create | `offline_analyzer.py` | Standalone CLI tool: walk folders, run MediaPipe, annotate, overwrite images |

---

### Task 1: Expose Landmark Data from FacialTracker to the Main Loop

The main loop in `facil_event_capture.py` needs access to the raw landmark positions, EAR, MAR, and iris positions to pass them to `save_image()`. Currently `FacialTracker` stores `Eye` and `Lips` objects but the main loop only reads `eyes_status` and `yawn_status` strings.

**Files:**
- Modify: `facil_event_capture.py:248-277` (save_image function)
- Modify: `facil_event_capture.py:548-569` (main loop where save_image is called)

- [ ] **Step 1: Update `save_image()` signature to accept annotation data**

In `facil_event_capture.py`, change the `save_image` function to accept optional annotation parameters. When provided, it draws landmarks before the existing speed/footer overlays.

Replace the current `save_image` function (lines 248-277) with:

```python
def save_image(frame, folder_path, speed, lat2, long2, driver_status,
               left_eye_pos=None, right_eye_pos=None, lips_pos=None,
               left_iris_pos=None, right_iris_pos=None,
               ear_left=None, ear_right=None, mar=None):
    """Save image with footer overlay and optional landmark annotations.
    Filename includes metadata for upload_images.py parsing."""
    lat2 = round(float(lat2), 4)
    long2 = round(float(long2), 4)
    timestamp_obj = datetime.now()
    timestamp = timestamp_obj.strftime("%Y-%m-%d %H:%M:%S")
    filename_time = timestamp_obj.strftime("%Y%m%d_%H%M%S%f")
    image_file = os.path.join(folder_path, f"{filename_time}_{driver_status}_{lat2}_{long2}_{speed}.jpg")

    h, w = frame.shape[:2]

    # --- Landmark annotations (only when data is provided) ---
    if left_eye_pos is not None and right_eye_pos is not None:
        is_sleeping = driver_status == 'Sleeping'
        is_yawning = driver_status == 'Yawning'
        eye_color = (0, 0, 255) if is_sleeping else (0, 255, 0)  # Red if closed, green if open
        mouth_color = (0, 0, 255) if is_yawning else (0, 255, 0)

        # Scale landmark positions from detection resolution (320x240) to full frame (640x480)
        scale_x = w / (conf.FRAME_W // 2)
        scale_y = h / (conf.FRAME_H // 2)

        def scale_pos(pos_list):
            return [(int(p[0] * scale_x), int(p[1] * scale_y)) for p in pos_list]

        # Draw eye contour landmarks with connecting lines
        left_scaled = scale_pos(left_eye_pos)
        right_scaled = scale_pos(right_eye_pos)

        for eye_pts in [left_scaled, right_scaled]:
            # Draw lines connecting the 4 contour points (first 4 landmarks)
            contour = eye_pts[:4]
            for i in range(len(contour)):
                cv2.line(frame, contour[i], contour[(i + 1) % len(contour)], eye_color, 1, cv2.LINE_AA)
            # Draw circles at each landmark point
            for pt in eye_pts[:4]:
                cv2.circle(frame, pt, 3, eye_color, -1, cv2.LINE_AA)

        # Draw iris center points
        if left_iris_pos is not None and right_iris_pos is not None:
            left_iris_scaled = scale_pos(left_iris_pos)
            right_iris_scaled = scale_pos(right_iris_pos)
            # First point is iris center
            if left_iris_scaled:
                cv2.circle(frame, left_iris_scaled[0], 4, (255, 255, 0), -1, cv2.LINE_AA)  # Cyan
            if right_iris_scaled:
                cv2.circle(frame, right_iris_scaled[0], 4, (255, 255, 0), -1, cv2.LINE_AA)  # Cyan

        # Draw mouth contour landmarks with connecting lines
        if lips_pos is not None:
            lips_scaled = scale_pos(lips_pos)
            for i in range(len(lips_scaled)):
                cv2.line(frame, lips_scaled[i], lips_scaled[(i + 1) % len(lips_scaled)], mouth_color, 1, cv2.LINE_AA)
            for pt in lips_scaled:
                cv2.circle(frame, pt, 3, mouth_color, -1, cv2.LINE_AA)

        # Draw EAR & MAR text overlay (top-left, white on dark background)
        if ear_left is not None and ear_right is not None and mar is not None:
            ear_avg = (ear_left + ear_right) / 2
            ratio_text = f"EAR: {ear_avg:.2f} | MAR: {mar:.2f}"
            (tw, th), _ = cv2.getTextSize(ratio_text, FONT, 0.5, 1)
            cv2.rectangle(frame, (10, 5), (10 + tw + 10, 5 + th + 10), (0, 0, 0), -1)
            cv2.putText(frame, ratio_text, (15, 5 + th + 5), FONT, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

        # Draw status label (top-center, large text)
        status_text = driver_status.upper()
        (sw, sh), _ = cv2.getTextSize(status_text, FONT, 1.0, 2)
        status_x = (w - sw) // 2
        cv2.rectangle(frame, (status_x - 5, 30), (status_x + sw + 5, 30 + sh + 10), (0, 0, 0), -1)
        cv2.putText(frame, status_text, (status_x, 30 + sh + 5), FONT, 1.0, (0, 0, 255), 2, cv2.LINE_AA)

    # Draw speed on upper right corner (big font)
    speed_text = f"{speed} Km/h"
    (sw, sh), _ = cv2.getTextSize(speed_text, FONT, SPEED_FONT_SCALE, SPEED_FONT_THICKNESS)
    cv2.rectangle(frame, (w - sw - 20, 0), (w, sh + 16), (255, 0, 0), -1)
    cv2.putText(frame, speed_text, (w - sw - 10, sh + 8), FONT, SPEED_FONT_SCALE, (255, 255, 255), SPEED_FONT_THICKNESS, cv2.LINE_AA)

    # Draw footer with metadata (no speed — shown above)
    footer_text = [
        "Sapience Automata 2026",
        f"Time:{timestamp}",
        f"Lat,Long:{lat2},{long2}"
    ]
    cv2.rectangle(frame, (0, h - 24), (w, h), (255, 0, 0), -1)
    x = 20
    for text in footer_text:
        cv2.putText(frame, text, (x, h - 8), FONT, FONT_SCALE, (255, 255, 255), FONT_THICKNESS, cv2.LINE_AA)
        (tw, _), _ = cv2.getTextSize(text, FONT, FONT_SCALE, FONT_THICKNESS)
        x += tw + 10

    cv2.imwrite(image_file, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
```

- [ ] **Step 2: Update `_enqueue_save_image()` to pass through annotation data**

Replace the current `_enqueue_save_image` function (line 338-343) with:

```python
def _enqueue_save_image(frame, folder_path, speed, lat, long2, driver_status,
                        left_eye_pos=None, right_eye_pos=None, lips_pos=None,
                        left_iris_pos=None, right_iris_pos=None,
                        ear_left=None, ear_right=None, mar=None):
    """Queue image for background save. Drops if queue full."""
    try:
        _save_image_queue.put_nowait((frame, folder_path, speed, lat, long2, driver_status,
                                      left_eye_pos, right_eye_pos, lips_pos,
                                      left_iris_pos, right_iris_pos,
                                      ear_left, ear_right, mar))
    except Exception:
        pass  # Drop if worker is busy
```

- [ ] **Step 3: Extract landmark data in the main loop and pass to save_image**

In the main loop, where `_enqueue_save_image` is called for Sleeping (around line 558) and Yawning (around line 569), extract the landmark positions and ratios from `facial_tracker` and pass them along.

Replace the Sleeping save block (line 556-558):

```python
                    # Save annotated image (throttled 1/sec, queued to worker)
                    if current_time - last_save_time >= SAVE_IMAGE_INTERVAL:
                        last_save_time = current_time
                        # Extract landmark data for annotation
                        _left_eye_pos = facial_tracker.left_eye.pos if facial_tracker.left_eye else None
                        _right_eye_pos = facial_tracker.right_eye.pos if facial_tracker.right_eye else None
                        _lips_pos = facial_tracker.lips.pos if facial_tracker.lips else None
                        _left_iris_pos = facial_tracker.left_eye.iris.pos if (facial_tracker.left_eye and facial_tracker.left_eye.iris) else None
                        _right_iris_pos = facial_tracker.right_eye.iris.pos if (facial_tracker.right_eye and facial_tracker.right_eye.iris) else None
                        _ear_left = facial_tracker.left_eye.eye_veti_to_hori if facial_tracker.left_eye else None
                        _ear_right = facial_tracker.right_eye.eye_veti_to_hori if facial_tracker.right_eye else None
                        _mar = facial_tracker.lips.mouth_open_ratio if facial_tracker.lips else None
                        _enqueue_save_image(frame.copy(), folder_path, speed, lat, long2, driver_status,
                                            _left_eye_pos, _right_eye_pos, _lips_pos,
                                            _left_iris_pos, _right_iris_pos,
                                            _ear_left, _ear_right, _mar)
```

Replace the Yawning save block (line 567-569) with the identical pattern:

```python
                    if current_time - last_save_time >= SAVE_IMAGE_INTERVAL:
                        last_save_time = current_time
                        _left_eye_pos = facial_tracker.left_eye.pos if facial_tracker.left_eye else None
                        _right_eye_pos = facial_tracker.right_eye.pos if facial_tracker.right_eye else None
                        _lips_pos = facial_tracker.lips.pos if facial_tracker.lips else None
                        _left_iris_pos = facial_tracker.left_eye.iris.pos if (facial_tracker.left_eye and facial_tracker.left_eye.iris) else None
                        _right_iris_pos = facial_tracker.right_eye.iris.pos if (facial_tracker.right_eye and facial_tracker.right_eye.iris) else None
                        _ear_left = facial_tracker.left_eye.eye_veti_to_hori if facial_tracker.left_eye else None
                        _ear_right = facial_tracker.right_eye.eye_veti_to_hori if facial_tracker.right_eye else None
                        _mar = facial_tracker.lips.mouth_open_ratio if facial_tracker.lips else None
                        _enqueue_save_image(frame.copy(), folder_path, speed, lat, long2, driver_status,
                                            _left_eye_pos, _right_eye_pos, _lips_pos,
                                            _left_iris_pos, _right_iris_pos,
                                            _ear_left, _ear_right, _mar)
```

- [ ] **Step 4: Add `import facial_tracking.conf as conf` to facil_event_capture.py**

The `save_image` function now references `conf.FRAME_W` and `conf.FRAME_H` for scale calculations. This import already exists at line 46:

```python
import facial_tracking.conf as conf
```

Verify this import is present. No change needed if it already exists.

- [ ] **Step 5: Commit**

```bash
git add facil_event_capture.py
git commit -m "feat: add eye/mouth/iris landmark annotations to saved critical-event images"
```

---

### Task 2: Create the Standalone Offline Analyzer

A fully self-contained script that walks a root folder of event subfolders, runs MediaPipe FaceMesh on each image, computes EAR/MAR, determines status, draws all annotations, and overwrites the original image.

**Files:**
- Create: `offline_analyzer.py`

- [ ] **Step 1: Create `offline_analyzer.py` with full implementation**

Create the file at the firmware root:

```python
#!/usr/bin/env python3
"""
Offline Analyzer — Process folders of images for drowsiness/yawning detection.

Walks a root folder containing event subfolders, runs MediaPipe FaceMesh on each
image, computes EAR/MAR, determines driver status, draws annotations, and
overwrites the original image.

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

# MediaPipe landmark indices (same as facial_tracking/conf.py)
LEFT_EYE = [263, 362, 386, 374, 473, 474, 475, 476, 477]
RIGHT_EYE = [133, 33, 159, 145, 468, 469, 470, 471, 472]
LIPS = [291, 61, 13, 14]

VALID_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp'}

FONT = cv2.FONT_HERSHEY_SIMPLEX


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
    """Compute Eye Aspect Ratio (vertical / horizontal distance).
    eye_pos: first 4 points are [outer, inner, top, bottom] contour landmarks."""
    h_dist = eye_pos[0][0] - eye_pos[1][0]
    if h_dist == 0:
        return 0
    v_dist = eye_pos[3][1] - eye_pos[2][1]
    return v_dist / h_dist


def compute_mar(lips_pos):
    """Compute Mouth Aspect Ratio (vertical / horizontal distance).
    lips_pos: [right, left, top, bottom] lip landmarks."""
    h_dist = lips_pos[0][0] - lips_pos[1][0]
    if h_dist == 0:
        return 0
    v_dist = lips_pos[3][1] - lips_pos[2][1]
    return v_dist / h_dist


def annotate_image(frame, left_eye_pos, right_eye_pos, lips_pos,
                   left_iris_pos, right_iris_pos, ear, mar, status):
    """Draw all annotations onto the frame."""
    h, w = frame.shape[:2]
    is_sleeping = status == 'SLEEPING'
    is_yawning = status == 'YAWNING'
    is_noface = status == 'NO FACE'

    eye_color = (0, 0, 255) if is_sleeping else (0, 255, 0)
    mouth_color = (0, 0, 255) if is_yawning else (0, 255, 0)

    # Draw eye contour landmarks with connecting lines (first 4 points)
    for eye_pts in [left_eye_pos, right_eye_pos]:
        contour = eye_pts[:4]
        for i in range(len(contour)):
            cv2.line(frame, contour[i], contour[(i + 1) % len(contour)], eye_color, 1, cv2.LINE_AA)
        for pt in contour:
            cv2.circle(frame, pt, 3, eye_color, -1, cv2.LINE_AA)

    # Draw iris center points (first point of iris landmarks = center)
    if left_iris_pos:
        cv2.circle(frame, left_iris_pos[0], 4, (255, 255, 0), -1, cv2.LINE_AA)
    if right_iris_pos:
        cv2.circle(frame, right_iris_pos[0], 4, (255, 255, 0), -1, cv2.LINE_AA)

    # Draw mouth contour landmarks with connecting lines
    for i in range(len(lips_pos)):
        cv2.line(frame, lips_pos[i], lips_pos[(i + 1) % len(lips_pos)], mouth_color, 1, cv2.LINE_AA)
    for pt in lips_pos:
        cv2.circle(frame, pt, 3, mouth_color, -1, cv2.LINE_AA)

    # Draw EAR & MAR text (top-left, white on dark background)
    ratio_text = f"EAR: {ear:.2f} | MAR: {mar:.2f}"
    (tw, th), _ = cv2.getTextSize(ratio_text, FONT, 0.5, 1)
    cv2.rectangle(frame, (10, 5), (10 + tw + 10, 5 + th + 10), (0, 0, 0), -1)
    cv2.putText(frame, ratio_text, (15, 5 + th + 5), FONT, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

    # Draw status label (top-center, large text)
    if is_sleeping or is_yawning:
        status_color = (0, 0, 255)  # Red
    elif is_noface:
        status_color = (0, 255, 255)  # Yellow
    else:
        status_color = (0, 255, 0)  # Green

    (sw, sh), _ = cv2.getTextSize(status, FONT, 1.0, 2)
    status_x = (w - sw) // 2
    cv2.rectangle(frame, (status_x - 5, 30), (status_x + sw + 5, 30 + sh + 10), (0, 0, 0), -1)
    cv2.putText(frame, status, (status_x, 30 + sh + 5), FONT, 1.0, status_color, 2, cv2.LINE_AA)


def annotate_noface(frame):
    """Draw NO FACE annotation when no face is detected."""
    h, w = frame.shape[:2]
    status = "NO FACE"
    status_color = (0, 255, 255)  # Yellow

    # Draw EAR/MAR as N/A
    ratio_text = "EAR: N/A | MAR: N/A"
    (tw, th), _ = cv2.getTextSize(ratio_text, FONT, 0.5, 1)
    cv2.rectangle(frame, (10, 5), (10 + tw + 10, 5 + th + 10), (0, 0, 0), -1)
    cv2.putText(frame, ratio_text, (15, 5 + th + 5), FONT, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

    # Draw status label
    (sw, sh), _ = cv2.getTextSize(status, FONT, 1.0, 2)
    status_x = (w - sw) // 2
    cv2.rectangle(frame, (status_x - 5, 30), (status_x + sw + 5, 30 + sh + 10), (0, 0, 0), -1)
    cv2.putText(frame, status, (status_x, 30 + sh + 5), FONT, 1.0, status_color, 2, cv2.LINE_AA)


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

    # Extract landmark positions
    left_eye_pos = get_landmark_positions(frame, face_landmarks, LEFT_EYE[:4])
    right_eye_pos = get_landmark_positions(frame, face_landmarks, RIGHT_EYE[:4])
    lips_pos = get_landmark_positions(frame, face_landmarks, LIPS)

    # Iris positions (last 5 landmarks of each eye index list)
    left_iris_pos = get_landmark_positions(frame, face_landmarks, LEFT_EYE[4:])
    right_iris_pos = get_landmark_positions(frame, face_landmarks, RIGHT_EYE[4:])

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

    annotate_image(frame, left_eye_pos, right_eye_pos, lips_pos,
                   left_iris_pos, right_iris_pos, ear, mar, status)

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
    mp_face_mesh = mp.solutions.face_mesh
    face_mesh = mp_face_mesh.FaceMesh(
        static_image_mode=True,
        max_num_faces=1,
        refine_landmarks=True,
        min_detection_confidence=0.5
    )

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
```

- [ ] **Step 2: Test the offline analyzer on a small test folder**

Create a test folder with a few sample images and run:

```bash
# Create test structure
mkdir -p /tmp/test_analyzer/session_01

# Copy a few sample images (or use any face images you have)
# Then run:
python3 offline_analyzer.py /tmp/test_analyzer
```

Expected output:
```
[session_01] Processing 3 files...
  frame_001.jpg: ACTIVE (EAR=0.28, MAR=0.35)
  frame_002.jpg: SLEEPING (EAR=0.12, MAR=0.20)
  frame_003.jpg: NO FACE
[session_01] Done: 3 processed, 0 skipped

==================================================
Total: 1 folders, 3 images processed, 0 skipped
```

- [ ] **Step 3: Commit**

```bash
git add offline_analyzer.py
git commit -m "feat: add standalone offline analyzer for batch image detection and annotation"
```

---

### Task 3: Manual Integration Test on the Live System

Verify that the live system changes don't break existing behavior and that annotations appear correctly on saved event images.

**Files:**
- No new files — testing existing changes

- [ ] **Step 1: Verify `save_image` works without annotation data (backward compatible)**

The updated `save_image()` has all annotation parameters defaulting to `None`. When called without them (e.g., if any other code calls `save_image`), it should produce the same output as before — just speed + footer overlays, no landmarks.

Verify by checking that `_save_image_worker` unpacks the tuple correctly. The worker at line 311-321 uses `save_image(*args)` — since we're now passing more arguments in the tuple from `_enqueue_save_image`, this will correctly unpack all parameters positionally.

- [ ] **Step 2: Verify the conf import exists**

```bash
grep -n "import facial_tracking.conf as conf" facil_event_capture.py
```

Expected: Line 46 shows the import already exists.

- [ ] **Step 3: Commit (if any fixes were needed)**

```bash
git add facil_event_capture.py
git commit -m "fix: address any integration issues from landmark annotation changes"
```

Only commit if changes were made. Skip if no fixes needed.
