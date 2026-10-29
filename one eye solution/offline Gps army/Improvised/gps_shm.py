"""
Shared memory IPC for GPS data between get_gps_data.py and facil_event_capture.py.
Replaces Redis for real-time GPS values (lat, lon, speed, acc).

Layout (40 bytes):
  - lat:       double (8 bytes)  offset 0
  - lon:       double (8 bytes)  offset 8
  - speed:     double (8 bytes)  offset 16
  - acc:       double (8 bytes)  offset 24
  - timestamp: double (8 bytes)  offset 32
  - Total: 40 bytes (padded to 64)
"""
import struct
import mmap
import os

SHM_PATH = "/dev/shm/blinksmart_gps"  # tmpfs — in RAM, no disk I/O
SHM_SIZE = 64  # Padded for alignment
STRUCT_FMT = 'ddddd'  # lat, lon, speed, acc, timestamp
STRUCT_SIZE = struct.calcsize(STRUCT_FMT)


def _init_shm_file():
    """Create the shared memory file if it doesn't exist."""
    if not os.path.exists(SHM_PATH):
        with open(SHM_PATH, 'wb') as f:
            f.write(b'\x00' * SHM_SIZE)


class GPSWriter:
    """Used by get_gps_data.py to write GPS values."""
    def __init__(self):
        _init_shm_file()
        self._fd = os.open(SHM_PATH, os.O_RDWR)
        self._shm = mmap.mmap(self._fd, SHM_SIZE)

    def write(self, lat, lon, speed, acc, timestamp):
        self._shm.seek(0)
        self._shm.write(struct.pack(STRUCT_FMT,
                                    float(lat), float(lon),
                                    float(speed), float(acc),
                                    float(timestamp)))

    def close(self):
        self._shm.close()
        os.close(self._fd)


class GPSReader:
    """Used by facil_event_capture.py to read GPS values."""
    def __init__(self):
        _init_shm_file()
        self._fd = os.open(SHM_PATH, os.O_RDONLY)
        self._shm = mmap.mmap(self._fd, SHM_SIZE, access=mmap.ACCESS_READ)

    def read(self):
        self._shm.seek(0)
        data = self._shm.read(STRUCT_SIZE)
        if len(data) < STRUCT_SIZE:
            return 0.0, 0.0, 0.0, 0.0, 0.0
        lat, lon, speed, acc, timestamp = struct.unpack(STRUCT_FMT, data)
        return lat, lon, speed, acc, timestamp

    def close(self):
        self._shm.close()
        os.close(self._fd)
