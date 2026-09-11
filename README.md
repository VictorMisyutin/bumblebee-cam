# Pollinator Field Camera

Motion-triggered pollinator camera for Raspberry Pi Zero 2 W + Camera Module 3.
Motion detection only, no on-Pi inference — annotation and YOLO training happen
off-Pi on a laptop.

## Layout
    src/       Python. Deploys FLAT to /opt/pollinator (imports are flat).
    config/    baseline.json = fleet defaults. device.json = per-camera.
    systemd/   Unit files and drop-ins.
    ops/       heartbeat.sh (1-min telemetry), netcheck.sh (network watchdog).

## Config layering
    code defaults  ->  baseline.json  ->  device.json     (rightmost wins)
Delete a key from device.json to fall back to the baseline value.

## Install on a new Pi
    sudo ./install.sh
    sudo nano /etc/pollinator/device.json     # set device_id
    sudo systemctl restart pollinator-cam

## Web UI
    http://<pi-ip>:8020     Live / Captures / Debug
    Draw motion ROIs in the UI; they save to device.json.

## Field notes
- Wi-Fi power save is ON by default on the Zero 2 W. install.sh disables it.
  Even so, expect drops on a weak link — netcheck.sh restarts NetworkManager
  after ~6 min unreachable, reboots after ~16.
- No RTC. Timestamps are wrong until NTP syncs, and stay wrong with no network.
  Fit a DS3231 before relying on capture times.
- Images go to the SD card unless an SSD is mounted at /srv/pollinator.
- Verify battery capacity before deploying — a dead battery looks exactly like
  a crashed Pi.
