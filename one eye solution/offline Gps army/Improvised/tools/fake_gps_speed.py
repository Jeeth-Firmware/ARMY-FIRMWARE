#!/usr/bin/env python3
"""
Fake GPS speed writer for testing overspeed alerts without driving/OBD.

Examples:
  python3 tools/fake_gps_speed.py
  python3 tools/fake_gps_speed.py --duration 60 --interval 1
  python3 tools/fake_gps_speed.py --pattern "5,12,18,25,35,22,9"
  python3 tools/fake_gps_speed.py --lat 12.9716 --lon 77.5946 --acc 0.2
"""

import argparse
import os
import sys
import time

# Make project-root modules importable when run as:
#   python3 tools/fake_gps_speed.py
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from gps_shm import GPSWriter


def parse_pattern(pattern_text):
    """Parse CSV speed pattern into list of floats."""
    values = []
    for part in pattern_text.split(","):
        s = part.strip()
        if not s:
            continue
        values.append(float(s))
    if not values:
        raise ValueError("Pattern is empty. Provide comma-separated speeds, e.g. 5,15,25")
    return values


def main():
    parser = argparse.ArgumentParser(
        description="Write fake GPS speed values into shared memory."
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="Total run duration in seconds (default: 30)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Seconds between writes (default: 1.0)",
    )
    parser.add_argument(
        "--pattern",
        type=str,
        default="5,12,18,25,35,22,9,0",
        help="Comma-separated speed pattern in km/h",
    )
    parser.add_argument("--lat", type=float, default=12.9716, help="Latitude")
    parser.add_argument("--lon", type=float, default=77.5946, help="Longitude")
    parser.add_argument("--acc", type=float, default=0.2, help="Acceleration value")
    args = parser.parse_args()

    if args.duration <= 0:
        raise ValueError("--duration must be > 0")
    if args.interval <= 0:
        raise ValueError("--interval must be > 0")

    speeds = parse_pattern(args.pattern)
    writer = GPSWriter()
    start = time.time()
    idx = 0

    print("Starting fake GPS speed writer")
    print(f"Duration: {args.duration}s | Interval: {args.interval}s")
    print(f"Pattern: {speeds}")
    print(f"Lat/Lon: {args.lat}, {args.lon} | Acc: {args.acc}")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            now = time.time()
            if now - start >= args.duration:
                break
            speed = speeds[idx % len(speeds)]
            writer.write(args.lat, args.lon, speed, args.acc, now)
            elapsed = now - start
            print(f"[{elapsed:6.1f}s] wrote speed={speed:5.1f} km/h")
            idx += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped by user")
    finally:
        writer.close()
        print("Fake GPS speed writer stopped")


if __name__ == "__main__":
    main()

