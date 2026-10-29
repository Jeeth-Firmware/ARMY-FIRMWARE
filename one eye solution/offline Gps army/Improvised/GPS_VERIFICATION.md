# GPS Verification & Troubleshooting Guide

## ✓ GPS is ALREADY in your images!

Your images already include GPS coordinates in the footer. Look at [facil_event_capture.py](facil_event_capture.py#L376):
```
"Lat,Long:{lat2},{long2}"
```

The filename also contains GPS coordinates:
```
{timestamp}_{event}_{lat}_{long}_{speed}.jpg
```

## How to Check If GPS is Working

### Quick Test (Recommended)

Run the GPS diagnostic script:
```bash
python3 test_gps_working.py
```

This will:
- ✓ Verify GPS shared memory is accessible
- ✓ Check if values are being updated
- ✓ Show current GPS coordinates
- ✓ Report data freshness (how old the GPS reading is)

### Expected Output if GPS is Working:
```
✓ GPS has a valid fix!
✓ GPS is WORKING and getting updates!
  → Latest position: (28.453921, 77.045230)
  → Data is FRESH (updated 0.5s ago)
```

### Expected Output if GPS is NOT Working:
```
✗ GPS not working: All coordinates are (0, 0)
  → Check: is get_gps_data.py running?
  → Check: is GPS device connected?
  → Check: does GPS have a valid fix (outdoor, clear sky)?
```

## GPS Data Flow

```
GPS Device (UART)
    ↓
get_gps_data.py (reads GPS via serial/gpsd)
    ↓
Shared Memory: /dev/shm/blinksmart_gps
    ↓
facil_event_capture.py (reads GPS every ~1 second)
    ↓
Image overlay & filename
```

## How GPS Data is Used

### In Code:
1. GPS is read every second from shared memory (line 636 in facil_event_capture.py)
2. Latest coordinates are cached for the next image capture
3. If GPS temporarily loses fix, last known coordinates are used

### In Images:
- **Footer text**: `Lat,Long:28.4539,77.0452`
- **Filename**: `20260620_143022_Active_28.4539_77.0452_45.jpg`
- **Resolution**: 4 decimal places (±11 meters accuracy)

## Verification Checklist

- [ ] Run `python3 test_gps_working.py` and check output
- [ ] Look at saved images in `images/` folder
- [ ] Check if coordinates in footer are non-zero
- [ ] Check if coordinates change between images
- [ ] Verify `get_gps_data.py` is running: `ps aux | grep get_gps_data`
- [ ] Check GPS device connection: `ls -la /dev/ttyUSB0` or `/dev/ttyAMA0`
- [ ] For outdoor GPS: wait 30-60 seconds for initial fix (especially after power-on)

## Troubleshooting

### Problem: All images show (0, 0) or (-1, -1)
**Cause**: GPS has no fix
**Solution**:
1. Go outdoor with clear sky
2. Wait 60 seconds for GPS to acquire fix
3. Check GPS device is receiving power (LED blinking?)
4. Try power cycling the GPS device

### Problem: GPS works but images show old coordinates
**Cause**: GPS fix dropped briefly
**Solution**: This is normal! The code intentionally caches last-known coordinates so images remain useful during brief GPS interruptions.

### Problem: `get_gps_data.py` crashes frequently
**Check**: `journalctl -u get_gps_data1.service -f`

### Problem: GPS readings seem inaccurate
**Note**: GPS accuracy depends on:
- Sky visibility (clear sky = ±5-10m, urban canyon = ±20-50m)
- Number of satellites (wait for 8+ satellites for best accuracy)
- GPS device model
