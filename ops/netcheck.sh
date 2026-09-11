#!/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
STATE=/var/lib/netcheck.fails
GW=$(ip route | awk '/^default/{print $3; exit}'); [ -z "$GW" ] && GW=1.1.1.1
if ping -c2 -W3 "$GW" >/dev/null 2>&1; then echo 0 > "$STATE"; exit 0; fi
N=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 )); echo "$N" > "$STATE"
logger -t netcheck "no reply from $GW (failure $N)"
UP=$(cut -d. -f1 /proc/uptime)
if [ "$N" -eq 3 ]; then
  logger -t netcheck "restarting NetworkManager"; systemctl restart NetworkManager
elif [ "$N" -ge 8 ] && [ "$UP" -gt 900 ]; then
  logger -t netcheck "network dead ~16min, rebooting"; echo 0 > "$STATE"; systemctl reboot
fi
