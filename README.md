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

## Day-to-day (Makefile)

    make help       list targets
    make diff       show drift between this repo and what's deployed
    make deploy     push code, config, units, ops; restart services
    make code       just the Python, then restart cam
    make status     are the services up
    make logs       follow the capture log
    make live       last heartbeat + live-frame age
    make freeze     copy deployed files BACK into the repo (after hand-edits)

`make diff` before you start and `make freeze` after you hand-edit on the Pi.
Uncommitted drift between /opt/pollinator and this repo is how work gets lost.

## Services

    pollinator-cam      motion detection and capture
    pollinator-field    web UI on :8020

    sudo systemctl restart pollinator-cam     # after editing device.json
    sudo systemctl stop pollinator-cam        # frees the sensor for rpicam-*
    systemctl is-active pollinator-cam pollinator-field
    journalctl -u pollinator-cam -f

`start` runs it now; `enable` makes it run at boot. They're independent.
Editing a unit file needs `daemon-reload` too; editing device.json needs
only a restart.

## Paths

    /opt/pollinator        code (flat — imports have no package structure)
    /etc/pollinator        baseline.json + device.json
    /srv/pollinator/images captures
    /run/pollinator        live.jpg + live.json (tmpfs, RAM not SD)
    /var/log/heartbeat.log 1-min telemetry

## Monitoring

    tail -f /var/log/heartbeat.log
    journalctl -t netcheck              network drops and recoveries
    vcgencmd get_throttled              0x0 is clean; bit 16 = undervoltage

Empty `rssi=` in the heartbeat means the radio was disassociated — the Pi is
alive but unreachable. netcheck.sh restarts NetworkManager after ~6 min and
reboots after ~16.

Gaps in heartbeat.log mean the Pi was not running at all. Check the battery
before assuming a crash.

## Known gaps

- No RTC. fake-hwclock restores a stale time at boot; captures before NTP
  sync carry the wrong date. DS3231 on I2C fixes it.
- Images land on the SD card. Mount the SSD at /srv/pollinator to move them.
- debug_previews/ grows ~288 files/day with no pruning.
- The web UI writes device.json directly, bypassing the key whitelist in
  pollinator_common.py.
