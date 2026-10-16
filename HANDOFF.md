# Handoff notes

For whoever takes over the pollinator cameras. Start with [README.md](README.md)
for what the code is. This file covers how to run the cameras and what you
need to know.

| | |
|---|---|
| Cameras | `bumblebee001`, `bumblebee002`, `bumblebee003` |
| Login | `camadmin` (password: **TODO: password-manager entry**) |
| Field UI | `http://bumblebee001.local:8020` |
| Photos | `/srv/pollinator/images/YYYY-MM-DD/` on the SSD |

## Hardware (per camera)

Raspberry Pi Zero 2 W, Camera Module 3 (narrow "Zero" cable), 256 GB microSD
(OS), 256 GB USB SSD (photos only, since the Zero can't boot from USB), Solar
Power Manager (not connected yet), MZ2211 box. Pi Zero 2 W boards are hard to
find. A Pi 3 A+ works with the same software but needs the standard camera
cable and a 2.5 A supply.

## Setting up a camera

1. **Flash the SD card** with Raspberry Pi Imager: Raspberry Pi OS **Lite
   64-bit**, hostname `bumblebee00N`, user `camadmin`, Wi-Fi
   (**TODO: which network**), time zone America/Chicago, and SSH with your
   public key. Flash each card separately. Don't clone cards, because cloned
   cards share SSH keys and cause conflicts.
2. **Log in:** `ssh camadmin@bumblebee00N.local`. After a re-flash you'll get a
   "host identification has changed" warning. That's expected. Run
   `ssh-keygen -R bumblebee00N.local` and connect again.
3. **Mount the SSD at `/srv/pollinator`** before installing, so photos go to the
   SSD and not the SD card. Use the UUID from `lsblk -f` in `/etc/fstab`, with
   the `nofail` option. **TODO: confirm with `findmnt /srv/pollinator` on a
   working camera.**
4. **Install:**
   ```bash
   sudo apt install -y git make
   git clone https://github.com/VictorMisyutin/bumblebee-cam.git && cd bumblebee-cam
   sudo ./install.sh
   ```
   The script installs packages, creates the `pollinator` service user, copies
   everything into place, and starts both services. It's safe to re-run.
5. **Calibrate** in the field UI's **Live** tab. Draw tight motion regions
   around the flowers and press **Save regions**, then press **Autofocus**,
   then tune the trigger sensitivity. These settings are saved to that
   camera's `/etc/pollinator/device.json`.
6. **Check:** `make status`, `make live`, `make logs`.

## Day to day

- **Field UI:** *Live* is for aiming and focus. *Captures* is for browsing,
  downloading and deleting photos. *Debug* shows health and skip counters, so
  look there first when a camera stops taking photos.
- **Get photos off:**
  `scp -r camadmin@bumblebee00N.local:/srv/pollinator/images/2026-10-15 .`
- **Update code:** `git pull && make deploy` on the Pi, then `make diff` to
  confirm it's in sync. Don't edit `/opt/pollinator` directly. If you hot-fix
  something there, run `make freeze` to copy it back into the repo.
- **Config:** change settings for all cameras in `config/baseline.json` and
  run `make config`. Change one camera's settings through the UI, which
  writes that camera's `device.json`.
- **Health log:** `tail /var/log/heartbeat.log`. If `thr=` shows anything other
  than `0x0`, the Pi has hit low voltage or overheating.

## Troubleshooting

| Problem | Look at |
|---|---|
| No new photos | The Debug tab counters. `skip_hours` means it's outside 07:00–19:00, `skip_global` means clouds or exposure changes, `skip_duplicate` means photos too similar to the last one, `skip_disk` means the disk is full. |
| Service restarting | `journalctl -u pollinator-cam -n 100` |
| "Failed to acquire camera" | Something else is using the camera. Run `sudo systemctl stop pollinator-cam` before testing it by hand. |
| UI buttons say "Permission denied" | Re-run `sudo ./install.sh`, which fixes the folder permissions. |
| Camera rebooted itself | `journalctl -t netcheck`. The camera reboots after ~16 min without network. |
| Windows: `bash\r: not found` | The file has Windows line endings. Run `sed -i 's/\r$//' <file>`. |

## Decisions already made

- **No AI on the Pi.** It has too little RAM to run a detector fast enough.
  The Pi does motion detection only, and identification happens on a laptop.
- **Identify to 14 morpho groups, not species:** honey bee, bumble bee,
  eastern carpenter bee, shiny green bee, medium-large dark bee, tiny dark
  bee, other bee, fly, butterfly/moth, skipper, wasp, beetle, hummingbird,
  other.
- **The iNaturalist/Seek computer-vision API was requested and declined**
  (Sept 2026). Use the models in the ID pipeline instead.
- The SSD holds photos, and the SD card holds only the OS. Cameras capture
  only from 07:00 to 19:00. Bench testing is on wall power, and solar comes
  later.

## Known issues

- **Zoom (`sensor_crop`) isn't applied to the photos.** picamera2 resets it on
  the mode switch to stills, and the Live view loses its zoom after the first
  capture.
- **The field UI has no working login.** Setting `POLLINATOR_FIELD_TOKEN`
  breaks the page, because the page doesn't send the token. Keep cameras on a
  trusted network.
- **There's no real-time clock.** If a camera boots without network, its
  timestamps and active hours are wrong. A DS3231 RTC module would fix this.
- **Without Wi-Fi, `netcheck.sh` reboots the camera about every 16 minutes.**
  Remove its cron line (`sudo crontab -e`) if you deploy a camera without
  network.

## Labelling + identification pipeline

Branch `morpho-groups-and-motion-crops` adds three things:

- **Motion crops:** each photo gets crops of the regions that moved, saved
  under `motion_crops/`.
- **Labelling in the Captures viewer:** click a box and press 1–0, W, B, H or
  O to tag its group. "Download labelled crops" exports the crops as a zip.
- **Laptop scripts:** `compare_insect_id.py` and `morphogroups.csv` compare
  Kindwise insect.id, BioCLIP 2 and iNat21 EVA-02.

**TODO: document how to run it once merged.** The genus → group rules in
`morphogroups.csv` are a first guess and still need checking.

## Open questions

- Which Solar Power Manager to use, and what panel and battery size?
- Can the Zero's USB port reliably power the SSD (1–2 W) on solar?
- Enclosure: window reflections and condensation (use desiccant).

## Accounts and secrets

Never commit passwords or keys.

- `camadmin` password: **TODO: password-manager entry**
- SSH: add your key to `~/.ssh/authorized_keys` on each Pi, and remove the
  keys of people who've left.
- Wi-Fi registration: **TODO: network / IT contact**
- Kindwise API key (paid): **TODO: which account.** Read it from an
  environment variable.
- GitHub: currently `VictorMisyutin/bumblebee-cam`. **TODO: transfer to the
  lab org.**
