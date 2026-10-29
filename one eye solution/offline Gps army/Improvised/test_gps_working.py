#!/usr/bin/env python3
"""
Quick diagnostic script to verify GPS is working and writing to shared memory.
Run this to check:
1. If get_gps_data.py is writing GPS values
2. If GPS coordinates are valid (non-zero)
3. If GPS data is fresh (timestamp recent)
"""

import sys
import time
from gps_shm import GPSReader

def check_gps_working():
    """Check if GPS data is being written and is current."""
    print("=" * 60)
    print("GPS DIAGNOSTIC CHECK")
    print("=" * 60)
    
    try:
        reader = GPSReader()
        print("✓ GPS shared memory reader initialized successfully")
    except Exception as e:
        print(f"✗ FAILED to initialize GPS reader: {e}")
        print("  → is get_gps_data.py running?")
        return False
    
    # Read GPS data multiple times to verify it's being updated
    print("\nReading GPS data 5 times (1 second apart)...")
    print("-" * 60)
    
    readings = []
    for i in range(5):
        try:
            lat, lon, speed, acc, ts = reader.read()
            readings.append((lat, lon, speed, acc, ts))
            
            current_time = time.time()
            age_seconds = current_time - ts if ts > 0 else -1
            
            # Format output
            print(f"\nReading {i+1}:")
            print(f"  Latitude:  {lat:.6f}")
            print(f"  Longitude: {lon:.6f}")
            print(f"  Speed:     {speed:.2f} km/h")
            print(f"  Accuracy:  {acc:.2f} m")
            print(f"  Timestamp: {ts:.2f} (age: {age_seconds:.1f}s)")
            
            # Validate GPS fix
            if lat == 0.0 and lon == 0.0:
                print("  ⚠ WARNING: No GPS fix yet (0,0)")
            elif lat == 0.0 or lon == 0.0:
                print("  ⚠ WARNING: Partial fix (one coordinate is zero)")
            else:
                print("  ✓ GPS has a valid fix!")
            
            if i < 4:
                time.sleep(1)
                
        except Exception as e:
            print(f"✗ Error reading GPS: {e}")
            reader.close()
            return False
    
    # Check if values changed (indicates get_gps_data.py is running)
    print("\n" + "=" * 60)
    print("ANALYSIS:")
    print("=" * 60)
    
    lats = [r[0] for r in readings]
    lons = [r[1] for r in readings]
    
    # Check for all zeros
    if all(lat == 0.0 and lon == 0.0 for lat, lon in zip(lats, lons)):
        print("✗ GPS not working: All coordinates are (0, 0)")
        print("  → Check: is get_gps_data.py running?")
        print("  → Check: is GPS device connected?")
        print("  → Check: does GPS have a valid fix (outdoor, clear sky)?")
        reader.close()
        return False
    
    # Check if values changed (indicates get_gps_data.py is writing)
    if len(set(lats)) > 1 or len(set(lons)) > 1:
        print("✓ GPS is WORKING and getting updates!")
        print(f"  → Latest position: ({lats[-1]:.6f}, {lons[-1]:.6f})")
        
        # Show data freshness
        last_ts = readings[-1][4]
        age = time.time() - last_ts
        if age < 5.0:
            print(f"  → Data is FRESH (updated {age:.1f}s ago)")
        else:
            print(f"  ⚠ Data is stale (last update {age:.1f}s ago)")
    else:
        print("⚠ GPS data not changing between reads")
        print("  → get_gps_data.py may not be running")
        print("  → Or GPS still acquiring initial fix")
    
    reader.close()
    print("=" * 60)
    return True

if __name__ == '__main__':
    try:
        success = check_gps_working()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n✗ Interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        sys.exit(1)
