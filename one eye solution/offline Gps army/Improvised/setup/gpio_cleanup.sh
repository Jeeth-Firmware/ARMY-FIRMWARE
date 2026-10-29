#!/bin/bash
# Force all alert GPIO pins LOW — buzzer (12,13) and LED (4).
# Called by systemd ExecStopPost to guarantee cleanup on crash/hang/OOM.
for pin in 4 12 13; do
    echo "$pin" > /sys/class/gpio/export 2>/dev/null
    echo "out" > /sys/class/gpio/gpio${pin}/direction 2>/dev/null
    echo "0"   > /sys/class/gpio/gpio${pin}/value 2>/dev/null
done
