# Event Image Annotation & Offline Analyzer

**Date**: 2026-04-10
**Status**: Draft

## Overview

Two features:
1. Annotate saved event images in the live system with eye/mouth landmarks, iris, EAR/MAR values, and detection status
2. A standalone offline analyzer tool that processes folders of images, runs detection, annotates, and overwrites originals

## Part 1: Live System — Annotated Event Images

### What Changes

Modify `save_image()` in `facil_event_capture.py` to draw annotations on the frame before writing to disk during critical events (Sleeping/Yawning).

### Annotations Drawn

1. **Eye contour landmarks** — circles at the 9 left-eye (`[263, 362, 386, 374, 473, 474, 475, 476, 477]`) and 9 right-eye (`[133, 33, 159, 145, 468, 469, 470, 471, 472]`) landmark positions, connected with lines. Green when open, red when closed.
2. **Mouth contour landmarks** — circles at the 4 lip landmark positions (`[291, 61, 13, 14]`), connected with lines. Green when closed, red when yawning.
3. **Iris center** — small filled circle at each iris center point (landmarks 473, 468).
4. **EAR & MAR text** — overlay `EAR: X.XX | MAR: X.XX` near top-left of frame.
5. **Status label** — large bold text (`SLEEPING`, `YAWNING`) in red, top-center of frame.

### What Does NOT Change

- The detection loop is untouched — no drawing during frame processing
- `HEADLESS` mode is unaffected
- Annotation only happens on the frame copy passed to `save_image()`
- Existing speed/timestamp/GPS overlays remain

### Data Flow

The main loop already has access to the `FacialTracker` instance which holds landmark positions, EAR/MAR ratios, and status. These values are passed as additional parameters to `save_image()` when the driver status is Sleeping or Yawning.

```
Detection loop
  └─> Critical event detected (Sleeping/Yawning)
      └─> Copy frame
          └─> save_image(frame, ..., landmarks, ear, mar, status)
              ├─> Draw eye contours (red/green based on status)
              ├─> Draw mouth contours (red/green based on status)
              ├─> Draw iris centers
              ├─> Draw EAR/MAR text
              ├─> Draw status label
              ├─> Draw existing overlays (speed, timestamp, GPS)
              └─> cv2.imwrite()
```

### Performance Impact

None. Annotations are drawn only on the frame being saved to disk, not on every detection frame. The save already happens in the main loop's non-critical path (after detection completes for that frame).

## Part 2: Offline Analyzer Tool

### New File

`offline_analyzer.py` in the firmware root directory. Fully self-contained — duplicates the detection and drawing logic inline rather than importing from the facial_tracking module.

### Input Structure

```
root_folder/
├── event_folder_1/
│   ├── frame_001.jpg
│   ├── frame_002.jpg
│   └── ...
├── event_folder_2/
│   ├── frame_001.jpg
│   ├── some_log.txt      <-- skipped
│   ├── corrupted.jpg      <-- skipped
│   └── ...
```

### Usage

```bash
python3 offline_analyzer.py /path/to/root/folder
```

### Processing Pipeline

1. Accept root folder path as CLI argument (`sys.argv[1]`)
2. List all subfolders in root, sorted alphabetically
3. For each subfolder:
   a. List all files
   b. For each file:
      - Check extension: only process `.jpg`, `.jpeg`, `.png`, `.bmp`
      - Skip non-image extensions (`.txt`, `.csv`, etc.) with a warning log
      - Attempt `cv2.imread()` — if returns None (corrupted), log warning and skip
      - Run MediaPipe FaceMesh on the image
      - If face detected: compute EAR and MAR using same landmark indices and formulas as `eye.py` / `lips.py`
      - Determine per-image status:
        - EAR < 0.20 → `SLEEPING`
        - MAR > 0.8 → `YAWNING`
        - If both → `SLEEPING` (eyes take priority)
        - Otherwise → `ACTIVE`
      - If no face detected → `NO FACE`
      - Draw all annotations on image
      - Overwrite original file with annotated image
   c. Print summary for folder (e.g., "folder_name: 15/20 images processed, 5 skipped")
4. Print overall summary

### Annotations (Same as Live System)

- Eye contour landmarks with connecting lines (red if closed, green if open)
- Mouth contour landmarks with connecting lines (red if yawning, green if closed)
- Iris center points (cyan filled circles)
- `EAR: X.XX | MAR: X.XX` text overlay (top-left, white text on dark background)
- Status label in large text (top-center, red for SLEEPING/YAWNING, green for ACTIVE, yellow for NO FACE)

### Error Handling

- Non-image files: skip with `print(f"Skipping non-image file: {filename}")`
- Corrupted images (`cv2.imread()` returns None): skip with `print(f"Skipping corrupted file: {filename}")`
- Empty subfolders: skip with info message
- MediaPipe initialization: done once, reused across all images
- All errors wrapped in try/except per-file to ensure one bad file doesn't stop the entire run

### Detection Differences from Live System

The live system uses consecutive-frame counting (16 closed frames = Sleeping). The offline tool operates on single images independently — status is determined per-image based on thresholds alone. This is appropriate for reviewing/validating detection quality on captured frames.

### Thresholds

Same as the live system's defaults from `conf.py`:
- `EYE_CLOSED = 0.20`
- `MOUTH_OPEN = 0.8`

Hardcoded in the offline tool (no config file dependency).

### Dependencies

Same as existing project: `mediapipe`, `opencv-python`, `numpy`. No new dependencies.
