#!/bin/bash
# Pi Zero 2W monitoring dashboard for Blinksmart DMS
# Usage: bash setup/monitor.sh

while true; do
    TEMP=$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2 || echo "N/A")
    THROTTLE=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2 || echo "N/A")
    MEM=$(free -m | awk 'NR==2{printf "%s/%sMB (%.0f%%)", $3,$2,$3*100/$2}')
    CPU=$(top -bn1 | grep "Cpu(s)" | awk '{print $2}')
    echo "$(date '+%H:%M:%S') | Temp: $TEMP | Throttled: $THROTTLE | RAM: $MEM | CPU: ${CPU}%"
    sleep 5
done
