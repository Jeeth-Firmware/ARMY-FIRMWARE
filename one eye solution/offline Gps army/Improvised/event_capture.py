"""
Event-Based Frame Buffer & GIF Generation - Device Side

This module implements:
1. Circular frame buffer (deque) in RAM
2. Event state machine: IDLE → EVENT_ACTIVE → POST_EVENT → SAVING
3. Pre-event (2s) and post-event (1s) frame capture
4. Single save worker thread with queue (prevents SD card I/O storms)
5. Event cooldown to prevent rapid start/stop cycling
"""

import os
import cv2
import time
import uuid
import threading
import numpy as np
from collections import deque
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Deque
from queue import Queue
from log import log_info, log_error
from store_locally import get_last_gps_data


def _is_valid_coord_pair(lat, lon):
    try:
        lat_f = float(lat)
        lon_f = float(lon)
        return lat_f != 0.0 and lon_f != 0.0 and -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0
    except Exception:
        return False


def _get_last_local_gps():
    """Return last locally stored GPS coordinates, or None if no valid row exists."""
    try:
        last_gps = get_last_gps_data()
        if not last_gps:
            return None
        lat, lon = last_gps
        if _is_valid_coord_pair(lat, lon):
            return str(lat), str(lon)
    except Exception as e:
        log_error(f"Failed to read last local GPS for event image: {e}")
    return None


class EventState(Enum):
    """Event capture state machine states"""
    IDLE = "idle"
    EVENT_ACTIVE = "event_active"
    POST_EVENT = "post_event"


@dataclass
class FrameData:
    """Container for frame with metadata.
    Stores raw numpy bytes — JPEG encoding happens in the save worker thread,
    NOT on the main detection thread. At 320x240, each frame is ~225KB raw."""
    frame_raw: bytes       # Raw numpy frame bytes (tobytes)
    frame_shape: tuple     # (h, w, channels) for reconstruction
    timestamp: float
    datetime_str: str
    speed: str
    lat: str
    long: str
    acceleration: str
    driver_status: str


@dataclass
class EventData:
    """Container for a complete event"""
    event_id: str
    event_type: str  # Sleeping, Yawning, NoFace, etc.
    start_time: float
    end_time: Optional[float] = None
    frames: List[FrameData] = field(default_factory=list)
    folder_path: Optional[str] = None

    def get_folder_name(self) -> str:
        """Generate folder name: timestamp_event_id"""
        dt = datetime.fromtimestamp(self.start_time)
        return f"{dt.strftime('%Y%m%d_%H%M%S')}_{self.event_id}"


class EventFrameBuffer:
    """
    Manages circular frame buffer and event capture logic.

    Event flow: 2 pre-event frames + during-event frames + 2 post-event frames.
    All stored as one event folder on disk (retrieved over USB export or the web UI).

    Key design for Pi Zero 2W performance:
    - add_frame() does ZERO JPEG encoding — just stores raw numpy bytes (~0.1ms)
    - JPEG encoding happens in a single persistent save worker thread
    - Single save worker serializes SD card writes (no concurrent I/O)
    """

    # Configuration - optimized for Pi Zero 2W (512MB RAM)
    PRE_EVENT_FRAMES = 2            # Frames to keep before event starts
    POST_EVENT_FRAMES = 2           # Frames to capture after event ends
    MAX_EVENT_SECONDS = 60.0        # Force complete after 60 seconds (safety cap)
    # Raw 640x480 frames are ~0.9MB each and the service runs under MemoryMax=320M.
    # When an event hits this cap, during-event frames are halved (timeline kept).
    MAX_EVENT_FRAMES = 30
    BUFFER_SIZE = PRE_EVENT_FRAMES + 2  # Circular buffer (slightly larger than needed)
    JPEG_QUALITY = 40               # Used in save worker, not main thread
    EVENT_COOLDOWN = 3.0            # Seconds between events
    POST_EVENT_TIMEOUT = 5.0        # Force complete POST_EVENT if frames stop arriving

    # NoFace: save only 1 frame per minute
    NOFACE_INTERVAL_SECONDS = 60

    # Font settings for footer overlay
    FONT = cv2.FONT_HERSHEY_SIMPLEX
    FONT_SCALE = 0.35
    FONT_THICKNESS = 1
    SPEED_FONT_SCALE = 0.8
    SPEED_FONT_THICKNESS = 2

    def __init__(self, base_events_path: str):
        self.base_events_path = base_events_path

        # Circular buffer for pre-event frames
        self.frame_buffer: Deque[FrameData] = deque(maxlen=self.BUFFER_SIZE)

        # Current state
        self.state = EventState.IDLE
        self.current_event: Optional[EventData] = None

        # Timing / counters
        self.event_end_time: Optional[float] = None
        self.post_event_count: int = 0
        self.last_critical_status: Optional[str] = None
        self.last_event_complete_time: float = float("-inf")  # monotonic, for cooldown
        self._event_start_mono: float = 0.0

        # During-event decimation (see MAX_EVENT_FRAMES)
        self._event_head_len: int = 0
        self._during_seen: int = 0
        self._during_stride: int = 1

        # NoFace tracking
        self.last_noface_save_time: float = float("-inf")  # monotonic

        # Thread safety for state machine
        self.lock = threading.Lock()

        # Single persistent save worker — serializes all SD card I/O
        self._save_queue: Queue = Queue(maxsize=10)
        self._save_worker = threading.Thread(target=self._save_worker_loop, daemon=True)
        self._save_worker.start()

        os.makedirs(base_events_path, exist_ok=True)
        log_info(f"EventFrameBuffer initialized: pre={self.PRE_EVENT_FRAMES}, post={self.POST_EVENT_FRAMES}, cooldown={self.EVENT_COOLDOWN}s")

    def add_frame(self, frame, speed: str, lat: str, long: str, acc: str, driver_status: str):
        """Add a frame to the buffer. Called from main detection loop at ~2 FPS."""
        # Monotonic for cooldowns/durations (immune to clock changes); wall time for event records
        current_time = time.monotonic()

        frame_data = FrameData(
            frame_raw=frame.tobytes(),
            frame_shape=frame.shape,
            timestamp=time.time(),
            datetime_str=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            speed=speed,
            lat=lat,
            long=long,
            acceleration=acc,
            driver_status=driver_status
        )

        with self.lock:
            # Always add to circular buffer (for pre-event frames)
            self.frame_buffer.append(frame_data)

            is_noface = driver_status in ['NoFace', 'No Face']
            is_critical = driver_status in ['Sleeping', 'Yawning', 'PhoneCall',
                                            'Sleeping/Looking Down', 'Yawning/Fatigued',
                                            'LookingDown', 'LookingAway',
                                            'Overspeeding']

            # --- IDLE ---
            if self.state == EventState.IDLE:
                if is_noface:
                    self._handle_noface(frame_data, current_time)
                elif is_critical:
                    if current_time - self.last_event_complete_time < self.EVENT_COOLDOWN:
                        return
                    self._start_event(driver_status, current_time)

            # --- EVENT_ACTIVE ---
            elif self.state == EventState.EVENT_ACTIVE:
                if is_critical:
                    self._append_during_frame(frame_data)
                    self.last_critical_status = driver_status
                    # Force complete if too long
                    if current_time - self._event_start_mono >= self.MAX_EVENT_SECONDS:
                        log_info(f"Event {self.current_event.event_id} max duration reached, completing")
                        self._transition_to_post_event(current_time)
                else:
                    # Not critical anymore — start post-event capture
                    self.current_event.frames.append(frame_data)
                    self._transition_to_post_event(current_time)
                    self.post_event_count += 1
                    if self.post_event_count >= self.POST_EVENT_FRAMES:
                        self._complete_event(current_time)

            # --- POST_EVENT ---
            elif self.state == EventState.POST_EVENT:
                self.current_event.frames.append(frame_data)
                self.post_event_count += 1
                # Complete after collecting enough post-event frames
                if self.post_event_count >= self.POST_EVENT_FRAMES:
                    self._complete_event(current_time)
                # Safety: force complete if post-event takes too long (frames stopped arriving)
                elif current_time - self.event_end_time >= self.POST_EVENT_TIMEOUT:
                    log_info(f"Event {self.current_event.event_id} post-event timeout, completing with {self.post_event_count} post frames")
                    self._complete_event(current_time)

    def _start_event(self, driver_status: str, current_time: float):
        """Start a new event — grab pre-event frames from buffer, current frame is first during-event frame"""
        event_id = f"evt_{uuid.uuid4().hex[:8]}"
        event_type = self._map_status_to_event_type(driver_status)

        self.current_event = EventData(
            event_id=event_id,
            event_type=event_type,
            start_time=time.time(),
            frames=[]
        )
        self._event_start_mono = current_time

        # Copy PRE_EVENT_FRAMES from buffer EXCLUDING the current (triggering) frame
        # The triggering frame (last in buffer) is the first during-event frame, not pre-event
        buffer_list = list(self.frame_buffer)
        if len(buffer_list) > 1:
            # Grab frames before the last one (which is the triggering frame)
            pre_start = max(0, len(buffer_list) - 1 - self.PRE_EVENT_FRAMES)
            pre_frames = buffer_list[pre_start:-1]
            self.current_event.frames.extend(pre_frames)
        else:
            pre_frames = []

        # Add the triggering frame as first during-event frame
        if buffer_list:
            self.current_event.frames.append(buffer_list[-1])

        self._event_head_len = len(self.current_event.frames)
        self._during_seen = 0
        self._during_stride = 1
        self.state = EventState.EVENT_ACTIVE
        self.last_critical_status = driver_status
        log_info(f"Event started: {event_id}, type={event_type}, pre_frames={len(pre_frames)}, first_during=1")

    def _append_during_frame(self, frame_data: FrameData):
        """Store a during-event frame, keeping the event under MAX_EVENT_FRAMES."""
        self._during_seen += 1
        if self._during_seen % self._during_stride:
            return
        frames = self.current_event.frames
        frames.append(frame_data)
        if len(frames) >= self.MAX_EVENT_FRAMES:
            head = self._event_head_len
            self.current_event.frames = frames[:head] + frames[head::2]
            self._during_stride *= 2

    def _handle_noface(self, frame_data: FrameData, current_time: float):
        """Handle NoFace when IDLE — save 1 frame per minute"""
        if current_time - self.last_noface_save_time < self.NOFACE_INTERVAL_SECONDS:
            return

        self.last_noface_save_time = current_time
        event_id = f"noface_{uuid.uuid4().hex[:8]}"
        noface_event = EventData(
            event_id=event_id,
            event_type='NoFace',
            start_time=frame_data.timestamp,
            end_time=frame_data.timestamp,
            frames=[frame_data]
        )

        folder_name = noface_event.get_folder_name()
        noface_event.folder_path = os.path.join(self.base_events_path, folder_name)
        log_info(f"NoFace captured: {event_id} (1 frame, next in {self.NOFACE_INTERVAL_SECONDS}s)")
        self._enqueue_save(noface_event)

    def _transition_to_post_event(self, current_time: float):
        """Driver no longer critical — start collecting post-event frames"""
        self.event_end_time = current_time
        # Elapsed monotonic time keeps the duration right if the clock is
        # corrected (GPS sync / Settings) while the event is running.
        self.current_event.end_time = self.current_event.start_time + (current_time - self._event_start_mono)
        self.post_event_count = 0
        self.state = EventState.POST_EVENT
        log_info(f"Event {self.current_event.event_id} ended, capturing {self.POST_EVENT_FRAMES} post-event frames")

    def _complete_event(self, current_time: float):
        """Event done — queue for save worker"""
        if not self.current_event:
            return

        event = self.current_event
        if event.end_time is None:
            event.end_time = event.start_time + (current_time - self._event_start_mono)
        folder_name = event.get_folder_name()
        event.folder_path = os.path.join(self.base_events_path, folder_name)

        log_info(f"Event {event.event_id} complete: {len(event.frames)} frames (pre={self.PRE_EVENT_FRAMES}+during+post={self.post_event_count}), saving to {event.folder_path}")

        self._enqueue_save(event)

        # Reset state
        self.state = EventState.IDLE
        self.current_event = None
        self.event_end_time = None
        self.post_event_count = 0
        self.last_critical_status = None
        self.last_event_complete_time = current_time

    def _enqueue_save(self, event: EventData):
        """Queue an event for the save worker. Drop if queue full (backpressure)."""
        try:
            self._save_queue.put_nowait(event)
        except Exception:
            log_error(f"Save queue full, dropping event {event.event_id}")
            event.frames.clear()

    def _save_worker_loop(self):
        """Persistent save worker thread. Serializes all SD card I/O.
        Runs at lowest priority to avoid stealing CPU from detection."""
        try:
            os.nice(10)
        except OSError:
            pass

        while True:
            try:
                event = self._save_queue.get()
                self._save_event_frames(event)
            except Exception as e:
                log_error(f"Save worker error: {e}")

    def _save_event_frames(self, event: EventData):
        """Save event frames to disk. JPEG encoding happens HERE, not main thread."""
        try:
            os.makedirs(event.folder_path, exist_ok=True)
            frame_paths = []
            encode_param = [cv2.IMWRITE_JPEG_QUALITY, self.JPEG_QUALITY]

            for i, frame_data in enumerate(event.frames):
                filename = f"frame_{i:04d}.jpg"
                filepath = os.path.join(event.folder_path, filename)

                # Reconstruct numpy array from raw bytes (copy needed for cv2 drawing)
                frame = np.frombuffer(frame_data.frame_raw, dtype=np.uint8).reshape(frame_data.frame_shape).copy()

                # Draw event on upper left and speed on upper right corner (big font)
                h, w = frame.shape[:2]
                if not _is_valid_coord_pair(frame_data.lat, frame_data.long):
                    last_local_gps = _get_last_local_gps()
                    if last_local_gps:
                        frame_data.lat, frame_data.long = last_local_gps
                lat_r = round(float(frame_data.lat), 4) if frame_data.lat else 0.0
                lon_r = round(float(frame_data.long), 4) if frame_data.long else 0.0
                event_text = event.event_type
                (ew, eh), _ = cv2.getTextSize(event_text, self.FONT, self.SPEED_FONT_SCALE, self.SPEED_FONT_THICKNESS)
                cv2.rectangle(frame, (0, 0), (ew + 20, eh + 16), (255, 0, 0), -1)
                cv2.putText(frame, event_text, (10, eh + 8), self.FONT, self.SPEED_FONT_SCALE, (255, 255, 255), self.SPEED_FONT_THICKNESS, cv2.LINE_AA)

                speed_text = f"{frame_data.speed} Km/h"
                (sw, sh), _ = cv2.getTextSize(speed_text, self.FONT, self.SPEED_FONT_SCALE, self.SPEED_FONT_THICKNESS)
                cv2.rectangle(frame, (w - sw - 20, 0), (w, sh + 16), (255, 0, 0), -1)
                cv2.putText(frame, speed_text, (w - sw - 10, sh + 8), self.FONT, self.SPEED_FONT_SCALE, (255, 255, 255), self.SPEED_FONT_THICKNESS, cv2.LINE_AA)

                # Draw footer overlay (no speed — shown above)
                footer_text = [
                    "Sapience Automata 2026",
                    f"Time:{frame_data.datetime_str}",
                    f"Lat,Long:{lat_r},{lon_r}"
                ]
                cv2.rectangle(frame, (0, h - 24), (w, h), (255, 0, 0), -1)
                x = 20
                for text in footer_text:
                    cv2.putText(frame, text, (x, h - 8), self.FONT, self.FONT_SCALE, (255, 255, 255), self.FONT_THICKNESS, cv2.LINE_AA)
                    (tw, _), _ = cv2.getTextSize(text, self.FONT, self.FONT_SCALE, self.FONT_THICKNESS)
                    x += tw + 10

                _, encoded = cv2.imencode('.jpg', frame, encode_param)
                with open(filepath, 'wb') as f:
                    f.write(encoded.tobytes())
                frame_paths.append(filepath)

                # Yield between frames to avoid hogging I/O
                time.sleep(0.01)

            # Save metadata
            metadata_path = os.path.join(event.folder_path, "event_meta.txt")
            with open(metadata_path, 'w') as f:
                f.write(f"event_id={event.event_id}\n")
                f.write(f"event_type={event.event_type}\n")
                f.write(f"start_time={event.start_time}\n")
                f.write(f"end_time={event.end_time}\n")
                f.write(f"frame_count={len(event.frames)}\n")
                if event.frames:
                    f.write(f"lat={event.frames[0].lat}\n")
                    f.write(f"long={event.frames[0].long}\n")
                    f.write(f"speed={event.frames[0].speed}\n")

            log_info(f"Event {event.event_id} saved: {len(frame_paths)} frames")

            # Free RAM
            event.frames.clear()

        except Exception as e:
            log_error(f"Error saving event {event.event_id}: {e}")

    def _map_status_to_event_type(self, status: str) -> str:
        """Map driver status to clean event type"""
        status_map = {
            'Sleeping/Looking Down': 'Sleeping',
            'Yawning/Fatigued': 'Yawning',
            'No Face': 'NoFace',
            'Sleeping': 'Sleeping',
            'Yawning': 'Yawning',
            'PhoneCall': 'PhoneCall',
            'phone call': 'PhoneCall',
            'LookingDown': 'LookingDown',
            'LookingAway': 'LookingAway',
            'NoFace': 'NoFace',
            'Overspeeding': 'Overspeeding',
        }
        return status_map.get(status, status)

    def force_complete_event(self):
        """Force complete current event (e.g., on shutdown)"""
        with self.lock:
            if self.current_event and self.state in [EventState.EVENT_ACTIVE, EventState.POST_EVENT]:
                log_info(f"Force completing event {self.current_event.event_id}")
                self._complete_event(time.monotonic())


# Global instance
_event_buffer: Optional[EventFrameBuffer] = None


def init_event_capture(base_events_path: str):
    """Initialize the event capture system."""
    global _event_buffer

    _event_buffer = EventFrameBuffer(base_events_path=base_events_path)

    log_info("Event capture system initialized")
    return _event_buffer


def get_event_buffer() -> Optional[EventFrameBuffer]:
    """Get the global event buffer instance"""
    return _event_buffer


def shutdown_event_capture():
    """Shutdown the event capture system gracefully"""
    global _event_buffer
    if _event_buffer:
        _event_buffer.force_complete_event()
        log_info("Event capture system shutdown")
