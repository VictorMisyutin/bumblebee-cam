#!/usr/bin/env bash
# Install the pollinator field camera. Run as root on a fresh Pi.
set -euo pipefail
[[ $(id -u) -eq 0 ]] || { echo "Run as root: sudo $0" >&2; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

apt-get update
apt-get install -y --no-install-recommends python3-picamera2 python3-opencv python3-numpy

id -u pollinator &>/dev/null || useradd -r -s /usr/sbin/nologin -G video pollinator

install -d -o root -g root /opt/pollinator
# /etc/pollinator must be group-writable by `pollinator`: the field UI saves
# ROIs/sliders into device.json and drops .request files here, and the capture
# service writes lens_position after an autofocus sweep. Root-only = those
# buttons fail with "Permission denied" on a fresh install.
install -d -o root -g pollinator -m 0775 /etc/pollinator
install -d -o pollinator -g pollinator /srv/pollinator/images

# Python deploys FLAT — imports have no package structure
install -m 0644 "$SRC"/src/*.py /opt/pollinator/
install -m 0644 "$SRC"/config/baseline.json /etc/pollinator/

if [[ ! -f /etc/pollinator/device.json ]]; then
  # device_id defaults to the hostname (bumblebee001, bumblebee002, ...).
  sed "s/\"CHANGEME\"/\"$(hostname)\"/" "$SRC"/config/device.json.example \
    > /etc/pollinator/device.json
  echo "!! Seeded /etc/pollinator/device.json with device_id=$(hostname)."
  echo "!! The ROI/focus values in it are examples — recalibrate in the field UI."
fi
chown root:pollinator /etc/pollinator/device.json
chmod 0664 /etc/pollinator/device.json

install -m 0644 "$SRC"/systemd/*.service /etc/systemd/system/
install -d /etc/systemd/system/pollinator-cam.service.d
install -m 0644 "$SRC"/systemd/pollinator-cam.service.d/*.conf \
  /etc/systemd/system/pollinator-cam.service.d/

if compgen -G "$SRC/systemd/pollinator-field.service.d/*.conf" > /dev/null; then
  install -d /etc/systemd/system/pollinator-field.service.d
  install -m 0644 "$SRC"/systemd/pollinator-field.service.d/*.conf \
    /etc/systemd/system/pollinator-field.service.d/
fi

install -m 0755 "$SRC"/ops/*.sh /usr/local/bin/

# Wi-Fi power save is ON by default on the Zero 2 W and drops inbound connections
install -d /etc/NetworkManager/conf.d
printf '[connection]\nwifi.powersave = 2\n' \
  > /etc/NetworkManager/conf.d/wifi-powersave-off.conf

# Pi OS ships a drop-in forcing volatile journald; 99- outranks it
install -d /etc/systemd/journald.conf.d /var/log/journal
printf '[Journal]\nStorage=persistent\nSystemMaxUse=100M\n' \
  > /etc/systemd/journald.conf.d/99-persistent.conf
systemctl restart systemd-journald && journalctl --flush

# `|| true`: on a fresh Pi root has no crontab, so `crontab -l` fails and,
# with pipefail, used to abort the whole script here without any message.
( { crontab -l 2>/dev/null || true; } | grep -v -e heartbeat.sh -e netcheck.sh || true
  echo '* * * * * /usr/local/bin/heartbeat.sh'
  echo '*/2 * * * * /usr/local/bin/netcheck.sh' ) | crontab -

systemctl daemon-reload
systemctl enable --now pollinator-cam.service pollinator-field.service
systemctl --no-pager --lines=0 status pollinator-cam pollinator-field || true
