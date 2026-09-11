#!/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LOG=/var/log/heartbeat.log
IMGDIR=/srv/pollinator/images

TEMP=$(vcgencmd measure_temp | cut -d= -f2)
THR=$(vcgencmd get_throttled | cut -d= -f2)
MEM=$(free -m | awk '/Mem:/{print $7}')
SWAP=$(free -m | awk '/Swap:/{print $3}')
RSSI=$(iw dev wlan0 link | awk '/signal/{print $2}')
LOAD=$(cut -d' ' -f1 /proc/loadavg)
CAM=$(systemctl is-active pollinator-cam.service)
NRE=$(systemctl show -p NRestarts --value pollinator-cam.service)
PID=$(systemctl show -p MainPID --value pollinator-cam.service)
CAMMEM=$(awk '/^VmRSS:/{print int($2/1024)}' "/proc/$PID/status" 2>/dev/null)
[ -z "$CAMMEM" ] && CAMMEM=-1
USED=$(df -k "$IMGDIR" 2>/dev/null | awk 'NR==2{print $3}')

echo "$(date -Is) temp=$TEMP thr=$THR memfree=${MEM}M swap=${SWAP}M cammem=${CAMMEM}M rssi=$RSSI load=$LOAD cam=$CAM nre=$NRE usedKB=$USED" >> "$LOG"
