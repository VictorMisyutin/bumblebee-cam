#!/usr/bin/env python3
"""Pollinator field UI — live view, ROI editor, capture browser, diagnostics.

    python3 pollinator_field.py /srv/pollinator/images \
        --config /etc/pollinator --host 0.0.0.0 --port 8020

Separate from pollinator_gallery_roi_browser.py, which is the annotation tool.
This one is for operating cameras: aim, focus, set motion regions, see what was
captured, delete junk, and work out why a camera has gone quiet.

Thumbnails cache to .thumbs/ so grid tiles are ~15 KB instead of ~500 KB.
ROI edits write only to device.json and SIGHUP the capture service.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

RUN_DIR = "/run/pollinator"
THUMB_DIRNAME = ".thumbs"
THUMB_WIDTH = 360
THUMB_QUALITY = 72
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.jpg$", re.IGNORECASE)
SIDECARS = (".txt", ".capture.json", ".annotations.json")

IMAGE_ROOT = ""
CONFIG_DIR = ""
AUTH_TOKEN = None
_pool = ThreadPoolExecutor(max_workers=1)
_warmed = set()
_lock = threading.Lock()


# ------------------------------------------------------------ files

def safe_parts(date, name):
    if not date or not DATE_RE.match(date):
        return None
    if not name or not NAME_RE.match(name) or "/" in name or "\\" in name:
        return None
    return date, name


def thumb_path(date, name):
    return os.path.join(IMAGE_ROOT, THUMB_DIRNAME, date, name)


def build_thumb(date, name):
    src = os.path.join(IMAGE_ROOT, date, name)
    dst = thumb_path(date, name)
    try:
        if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
    except OSError:
        return None
    try:
        from PIL import Image
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with Image.open(src) as im:
            im.draft("RGB", (THUMB_WIDTH * 2, THUMB_WIDTH * 2))
            im = im.convert("RGB")
            w, h = im.size
            im = im.resize((THUMB_WIDTH, max(1, int(h * THUMB_WIDTH / w))), Image.BILINEAR)
            tmp = dst + ".tmp"
            im.save(tmp, "JPEG", quality=THUMB_QUALITY)
            os.replace(tmp, dst)
        return dst
    except Exception:
        return None


def warm_date(date):
    with _lock:
        if date in _warmed:
            return
        _warmed.add(date)
    _pool.submit(lambda: [build_thumb(date, i["name"]) for i in list_images(date)])


def list_dates():
    try:
        return sorted((d for d in os.listdir(IMAGE_ROOT) if DATE_RE.match(d)), reverse=True)
    except OSError:
        return []


def list_images(date):
    d = os.path.join(IMAGE_ROOT, date)
    out = []
    try:
        for name in os.listdir(d):
            if NAME_RE.match(name):
                st = os.stat(os.path.join(d, name))
                out.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
    except OSError:
        return []
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def read_live_status():
    try:
        with open(os.path.join(RUN_DIR, "live.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        data["age"] = round(time.time() - data.get("ts", 0), 1)
        return data
    except Exception:
        return {"error": "no live frame", "age": None, "counts": {}}


# ------------------------------------------------------------ actions

def signal_capture_reload():
    """SIGHUP the capture service so ROI edits apply at once.

    Scans /proc rather than shelling out, so it works under a sandboxed unit.
    Same UID as the capture service, so the signal is permitted.
    """
    sent = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                if "pollinator_cam.py" in fh.read().decode("utf-8", "replace"):
                    os.kill(int(pid), signal.SIGHUP)
                    sent += 1
        except Exception:
            continue
    return sent


def save_rois(rois, roi_enabled):
    """Write ROIs into device.json only. baseline.json is never touched."""
    path = os.path.join(CONFIG_DIR, "device.json")
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)

    boxes = []
    for r in rois:
        x1, y1, x2, y2 = (int(v) for v in r[:4])
        if x2 > x1 and y2 > y1:
            boxes.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    cfg["motion_rois"] = boxes
    cfg["roi_enabled"] = bool(roi_enabled) and bool(boxes)
    if boxes:
        f = boxes[0]
        cfg["roi_x1"], cfg["roi_y1"] = f["x1"], f["y1"]
        cfg["roi_x2"], cfg["roi_y2"] = f["x2"], f["y2"]

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return signal_capture_reload()


def set_config_value(key, value):
    """Set a single per-camera key in device.json and reload."""
    path = os.path.join(CONFIG_DIR, "device.json")
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg[key] = value
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return signal_capture_reload()


def delete_images(date, names):
    if not DATE_RE.match(date or ""):
        return 0
    n = 0
    for name in names:
        if not safe_parts(date, name):
            continue
        base = os.path.join(IMAGE_ROOT, date, name)
        stem = base[:-4]
        for target in [base, thumb_path(date, name)] + [stem + s for s in SIDECARS]:
            try:
                os.remove(target)
                if target == base:
                    n += 1
            except OSError:
                pass
    return n


def delete_day(date):
    if not DATE_RE.match(date or ""):
        return False
    ok = False
    for d in (os.path.join(IMAGE_ROOT, date), os.path.join(IMAGE_ROOT, THUMB_DIRNAME, date)):
        try:
            shutil.rmtree(d)
            ok = True
        except OSError:
            pass
    with _lock:
        _warmed.discard(date)
    return ok


# ------------------------------------------------------------ diagnostics

def _sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=4).stdout.strip()
    except Exception:
        return ""


def _service(name):
    return {
        "name": name,
        "active": _sh(["systemctl", "is-active", name]) or "unknown",
        "enabled": _sh(["systemctl", "is-enabled", name]) or "unknown",
    }


def _cam_process():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                if "pollinator_cam.py" not in fh.read().decode("utf-8", "replace"):
                    continue
            rss = 0
            with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) // 1024
                        break
            with open("/proc/uptime") as fh:
                up = float(fh.read().split()[0])
            with open(f"/proc/{pid}/stat") as fh:
                start = int(fh.read().rsplit(")", 1)[1].split()[19]) / os.sysconf("SC_CLK_TCK")
            return {"pid": int(pid), "rss_mb": rss, "uptime_s": max(0, round(up - start))}
        except Exception:
            continue
    return None


def _system():
    out = {}
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            out["cpu_temp_c"] = round(int(fh.read().strip()) / 1000, 1)
    except Exception:
        out["cpu_temp_c"] = None
    mem = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                    mem[k] = int(v.split()[0]) // 1024
    except Exception:
        pass
    out["mem_total_mb"] = mem.get("MemTotal")
    out["mem_avail_mb"] = mem.get("MemAvailable")
    out["swap_used_mb"] = (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) or 0
    try:
        du = shutil.disk_usage(IMAGE_ROOT)
        out["disk_free_gb"] = round(du.free / 2 ** 30, 1)
        out["disk_used_pct"] = round(100 * du.used / du.total)
    except Exception:
        out["disk_free_gb"] = out["disk_used_pct"] = None
    try:
        with open("/proc/loadavg") as fh:
            out["load1"] = float(fh.read().split()[0])
    except Exception:
        out["load1"] = None
    out["throttled"] = _sh(["vcgencmd", "get_throttled"])
    return out


def _temperatures():
    """Every temperature source we can find.

    CPU die temp is always available. A DS18B20 on 1-wire or a DS3231 RTC
    (which carries its own sensor) gives actual enclosure air temperature —
    the number that matters for condensation and thermal shutdown in a sealed
    box in July. Neither is required; missing sensors are simply absent.
    """
    temps = []
    try:
        for zone in sorted(os.listdir("/sys/class/thermal")):
            if not zone.startswith("thermal_zone"):
                continue
            base = os.path.join("/sys/class/thermal", zone)
            try:
                with open(os.path.join(base, "temp")) as fh:
                    c = round(int(fh.read().strip()) / 1000, 1)
                label = "cpu"
                try:
                    with open(os.path.join(base, "type")) as fh:
                        label = fh.read().strip()
                except OSError:
                    pass
                temps.append({"label": label, "c": c, "source": "soc"})
            except (OSError, ValueError):
                continue
    except OSError:
        pass

    # DS18B20 one-wire probes, if the overlay is enabled and one is wired.
    try:
        for dev in os.listdir("/sys/bus/w1/devices"):
            if not dev.startswith("28-"):
                continue
            try:
                with open(f"/sys/bus/w1/devices/{dev}/w1_slave") as fh:
                    txt = fh.read()
                if "YES" in txt and "t=" in txt:
                    temps.append({"label": f"enclosure ({dev[-4:]})",
                                  "c": round(int(txt.rsplit("t=", 1)[1]) / 1000, 1),
                                  "source": "ds18b20"})
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return temps


def _network():
    """Wi-Fi link quality, signal strength, SSID and address.

    Signal is the number that decides how far a camera can sit from its
    access point, so it belongs on the same screen as everything else you
    check when standing next to a unit in a field.
    """
    net = {"iface": None, "ssid": None, "signal_dbm": None,
           "quality": None, "quality_max": 70, "ip": None, "rating": None}
    try:
        with open("/proc/net/wireless") as fh:
            for line in fh.readlines()[2:]:
                if ":" not in line:
                    continue
                iface, _, rest = line.partition(":")
                parts = rest.split()
                net["iface"] = iface.strip()
                net["quality"] = float(parts[1].rstrip("."))
                net["signal_dbm"] = float(parts[2].rstrip("."))
                break
    except Exception:
        pass

    if net["signal_dbm"] is not None:
        d = net["signal_dbm"]
        net["rating"] = ("excellent" if d >= -50 else "good" if d >= -60
                         else "fair" if d >= -70 else "weak")

    if net["iface"]:
        out = _sh(["iw", "dev", net["iface"], "link"])
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                net["ssid"] = line.split(":", 1)[1].strip()
        if not net["ssid"]:
            net["ssid"] = _sh(["iwgetid", "-r"]) or None

    ip = _sh(["hostname", "-I"])
    net["ip"] = ip.split()[0] if ip else None
    net["hostname"] = _sh(["hostname"]) or None
    return net


def _config_layers():
    layers = {"baseline": {}, "device": {}}
    for key, fn in (("baseline", "baseline.json"), ("device", "device.json")):
        try:
            with open(os.path.join(CONFIG_DIR, fn), encoding="utf-8") as fh:
                layers[key] = json.load(fh)
        except Exception:
            pass
    layers["overridden"] = sorted(
        k for k in layers["device"]
        if not k.startswith("_") and k in layers["baseline"]
        and layers["baseline"][k] != layers["device"][k]
    )
    return layers


def _last_capture():
    today = time.strftime("%Y-%m-%d")
    newest, count_today = None, 0
    for d in list_dates():
        imgs = list_images(d)
        if d == today:
            count_today = len(imgs)
        if imgs and newest is None:
            newest = {"date": d, "name": imgs[0]["name"], "mtime": imgs[0]["mtime"]}
    return newest, count_today


def collect_debug():
    live = read_live_status()
    counts = live.get("counts") or {}
    newest, today = _last_capture()
    cam = _cam_process()
    sysinfo = _system()

    checks = []

    def chk(state, label, detail):
        checks.append({"state": state, "label": label, "detail": detail})

    cam_svc = _service("pollinator-cam.service")
    chk("ok" if cam_svc["active"] == "active" else "bad",
        "Capture service", f"{cam_svc['active']} / {cam_svc['enabled']}")

    if cam is None:
        chk("bad", "Capture process", "not running")
    else:
        chk("ok", "Capture process", f"pid {cam['pid']} · up {cam['uptime_s']}s · {cam['rss_mb']} MB")

    age = live.get("age")
    if age is None:
        chk("bad", "Live frame", "none published")
    elif age > 5:
        chk("warn", "Live frame", f"{age}s old — loop may be blocked or asleep")
    else:
        chk("ok", "Live frame", f"{age}s old")

    if live.get("active_hours"):
        chk("warn", "Active hours",
            f"{live['active_hours'][0]}–{live['active_hours'][1]} — captures skipped outside this")
    else:
        chk("ok", "Active hours", "always on")

    trig, cap = counts.get("trigger", 0), counts.get("captured", 0)
    if trig == 0:
        chk("warn", "Motion triggers", "none since start — nothing has crossed threshold")
    elif cap == 0:
        chk("bad", "Captures", f"{trig} triggers but 0 images saved — every one was skipped")
    else:
        chk("ok", "Captures", f"{cap} saved from {trig} triggers")

    for key, label, hint in (
        ("skip_duplicate", "Duplicate suppression",
         "preview too similar to last capture; lower duplicate_hash_threshold or disable"),
        ("skip_quality", "Quality gate", "images rejected as blurry/dark"),
        ("skip_disk", "Disk space", "below min_free_mb"),
        ("skip_hours", "Outside active hours", "loop idle by schedule"),
    ):
        n = counts.get(key, 0)
        if n:
            chk("bad" if key in ("skip_duplicate", "skip_disk") else "warn",
                f"{label} skipped {n}", hint)

    if sysinfo.get("disk_used_pct") is not None and sysinfo["disk_used_pct"] > 90:
        chk("bad", "Disk", f"{sysinfo['disk_used_pct']}% used")
    if sysinfo.get("mem_avail_mb") is not None and sysinfo["mem_avail_mb"] < 40:
        chk("warn", "Memory", f"only {sysinfo['mem_avail_mb']} MB available")
    if sysinfo.get("swap_used_mb", 0) > 60:
        chk("warn", "Swap", f"{sysinfo['swap_used_mb']} MB in use — wears the SD card")
    if sysinfo.get("cpu_temp_c") and sysinfo["cpu_temp_c"] > 75:
        chk("warn", "CPU temp", f"{sysinfo['cpu_temp_c']} °C")
    if sysinfo.get("throttled") and sysinfo["throttled"] not in ("", "throttled=0x0"):
        chk("warn", "Power", f"{sysinfo['throttled']} — undervoltage or throttling seen")

    net = _network()
    if net.get("signal_dbm") is None:
        chk("warn", "Wi-Fi", "no wireless interface reporting")
    elif net["rating"] == "weak":
        chk("bad", "Wi-Fi", f"{net['signal_dbm']:.0f} dBm ({net['rating']}) on {net.get('ssid') or '?'}")
    elif net["rating"] == "fair":
        chk("warn", "Wi-Fi", f"{net['signal_dbm']:.0f} dBm ({net['rating']}) on {net.get('ssid') or '?'}")
    else:
        chk("ok", "Wi-Fi", f"{net['signal_dbm']:.0f} dBm ({net['rating']}) on {net.get('ssid') or '?'}")

    for t in _temperatures():
        if t["source"] == "ds18b20" and t["c"] > 50:
            chk("bad", "Enclosure temp", f"{t['c']} °C — above the camera's rated range")
        elif t["source"] == "ds18b20" and t["c"] > 40:
            chk("warn", "Enclosure temp", f"{t['c']} °C")

    return {
        "now": time.time(),
        "device_id": live.get("device_id", "unknown"),
        "checks": checks,
        "services": [cam_svc, _service("pollinator-field.service")],
        "cam": cam,
        "live": live,
        "counts": counts,
        "last_capture": newest,
        "captures_today": today,
        "system": sysinfo,
        "temps": _temperatures(),
        "network": _network(),
        "config": _config_layers(),
    }


# ------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PollinatorField/3.0"

    def log_message(self, *a):
        pass

    def _auth(self, q):
        return (not AUTH_TOKEN
                or self.headers.get("X-Auth-Token") == AUTH_TOKEN
                or q.get("token", [None])[0] == AUTH_TOKEN)

    def _send(self, code, body, ctype, cache=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype, cache=None):
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return self._send(404, "not found", "text/plain")
        self._send(200, data, ctype, cache)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._auth(parse_qs(u.query)):
            return self._send(401, "unauthorized", "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, '{"error":"bad json"}', "application/json")
        try:
            if u.path == "/api/roi":
                sent = save_rois(body.get("rois", []), body.get("roi_enabled", True))
                self._send(200, json.dumps({"ok": True, "signalled": sent}), "application/json")
            elif u.path == "/api/set":
                key, value = body.get("key"), body.get("value")
                allowed = {"lens_position", "sensor_crop", "motion_pixels",
                           "motion_threshold", "motion_confirm_frames",
                           "cooldown_seconds", "duplicate_suppression_enabled",
                           "active_hours_enabled", "quality_gate_enabled"}
                if key not in allowed:
                    return self._send(400, '{"error":"key not settable"}', "application/json")
                sent = set_config_value(key, value)
                self._send(200, json.dumps({"ok": True, "signalled": sent}), "application/json")
            elif u.path == "/api/action":
                action = str(body.get("action", ""))[:32]
                if action not in {"autofocus", "set-reference", "reset-background"}:
                    return self._send(400, '{"error":"unknown action"}', "application/json")
                # One word into a tmpfs file the capture service polls. It owns
                # the sensor exclusively, so it has to do the work, not us.
                # /etc/pollinator, not /run: both services share /run via
                # RuntimeDirectory, and the bind mount is read-only in this
                # namespace. CONFIG_DIR is already writable here (ROI saves).
                req_dir = CONFIG_DIR if CONFIG_DIR and os.path.isdir(CONFIG_DIR) else "/etc/pollinator"
                tmp = os.path.join(req_dir, ".request.tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(action)
                os.replace(tmp, os.path.join(req_dir, ".request"))
                self._send(200, json.dumps({"ok": True, "action": action}), "application/json")
            elif u.path == "/api/zip":
                import io, zipfile
                date = body.get("date")
                names = body.get("names", [])[:60]
                if not DATE_RE.match(date or ""):
                    return self._send(400, '{"error":"bad date"}', "application/json")
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
                    for nm in names:
                        if not safe_parts(date, nm):
                            continue
                        fp = os.path.join(IMAGE_ROOT, date, nm)
                        if os.path.exists(fp):
                            z.write(fp, arcname=f"{date}/{nm}")
                data = buf.getvalue()
                dev = read_live_status().get("device_id", "cam")
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{dev}_{date}.zip"')
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/delete":
                n = delete_images(body.get("date"), body.get("names", []))
                self._send(200, json.dumps({"ok": True, "deleted": n}), "application/json")
            elif u.path == "/api/delete_day":
                self._send(200, json.dumps({"ok": delete_day(body.get("date"))}), "application/json")
            else:
                self._send(404, '{"error":"not found"}', "application/json")
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._auth(q):
            return self._send(401, "unauthorized", "text/plain")
        r = u.path
        if r == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif r == "/live.jpg":
            self._file(os.path.join(RUN_DIR, "live.jpg"), "image/jpeg")
        elif r == "/live.json":
            self._send(200, json.dumps(read_live_status()), "application/json")
        elif r == "/api/debug":
            self._send(200, json.dumps(collect_debug()), "application/json")
        elif r == "/api/dates":
            self._send(200, json.dumps(list_dates()), "application/json")
        elif r == "/api/images":
            date = q.get("date", [None])[0]
            if not date or not DATE_RE.match(date):
                return self._send(400, "[]", "application/json")
            warm_date(date)
            self._send(200, json.dumps(list_images(date)), "application/json")
        elif r == "/thumb":
            p = safe_parts(q.get("date", [None])[0], q.get("name", [None])[0])
            if not p:
                return self._send(400, "bad request", "text/plain")
            t = build_thumb(*p)
            if not t:
                return self._send(404, "not found", "text/plain")
            self._file(t, "image/jpeg", "public, max-age=604800, immutable")
        elif r == "/download":
            p = safe_parts(q.get("date", [None])[0], q.get("name", [None])[0])
            if not p:
                return self._send(400, "bad request", "text/plain")
            try:
                with open(os.path.join(IMAGE_ROOT, p[0], p[1]), "rb") as fh:
                    data = fh.read()
            except OSError:
                return self._send(404, "not found", "text/plain")
            fname = f"{read_live_status().get('device_id','cam')}_{p[0]}_{p[1]}"
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.end_headers()
            self.wfile.write(data)
        elif r == "/full":
            p = safe_parts(q.get("date", [None])[0], q.get("name", [None])[0])
            if not p:
                return self._send(400, "bad request", "text/plain")
            self._file(os.path.join(IMAGE_ROOT, p[0], p[1]), "image/jpeg",
                       "public, max-age=604800, immutable")
        else:
            self._send(404, "not found", "text/plain")


# ------------------------------------------------------------ page

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pollinator field view</title>
<style>
:root{--bg:#f7f6f3;--card:#fff;--ink:#1b1b19;--mute:#6d6c67;--line:#e2e0da;
 --accent:#1a6b52;--accentbg:#e8f2ee;--danger:#a63232;--dangerbg:#fbecec;
 --warn:#8a5a0b;--warnbg:#fdf2e2;--r:10px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:11px 18px;background:var(--card);
 border-bottom:1px solid var(--line);position:sticky;top:0;z-index:30;flex-wrap:wrap}
h1{font-size:15px;font-weight:600;margin:0}
.pill{font-size:12px;padding:3px 10px;border-radius:999px;background:var(--accentbg);color:var(--accent)}
.pill.stale{background:var(--warnbg);color:var(--warn)}
nav{margin-left:auto;display:flex;gap:6px}
button,select,input{font:inherit;font-size:14px;padding:6px 12px;border:1px solid var(--line);
 background:var(--card);color:var(--ink);border-radius:var(--r)}
button,select{cursor:pointer}
button:hover{border-color:#c6c3ba;background:#fafaf8}
button.on{background:var(--ink);color:#fff;border-color:var(--ink)}
button.danger{color:var(--danger);border-color:#e8cccc}
button.danger:hover{background:var(--dangerbg)}
button:disabled{opacity:.4;cursor:default}
main{padding:18px;max-width:1400px;margin:0 auto}
.hide{display:none!important}
.live{display:grid;grid-template-columns:minmax(0,2fr) minmax(230px,1fr);gap:18px}
@media(max-width:860px){.live{grid-template-columns:1fr}}
.vwrap{position:relative;line-height:0;border-radius:var(--r);overflow:hidden;background:#111}
#liveimg{width:100%;display:block;aspect-ratio:4/3;object-fit:contain}
#roisvg{position:absolute;inset:0;width:100%;height:100%;cursor:crosshair;touch-action:none}
.stat{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:11px 13px;margin-bottom:9px}
.stat .k{font-size:12px;color:var(--mute)}
.stat .v{font-size:25px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.25}
.stat .s{font-size:12px;color:var(--mute)}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:7px}
.bar i{display:block;height:100%;background:var(--accent)}
.roibox{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:11px 13px}
.roibox h3{margin:0 0 8px;font-size:13px;font-weight:600}
.roirow{display:flex;justify-content:space-between;align-items:center;font-size:12px;
 color:var(--mute);padding:4px 0;border-top:1px solid var(--line);gap:6px;font-variant-numeric:tabular-nums}
.roirow button{padding:2px 8px;font-size:12px}
.acts{display:flex;gap:6px;margin-top:9px;flex-wrap:wrap}
.ctl{margin:7px 0}
.ctl label{display:flex;justify-content:space-between;font-size:12px;color:var(--mute);margin-bottom:3px}
.ctl label span{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.ctl input[type=range]{width:100%;padding:0;border:none;background:transparent}
.ctl2{font-size:12.5px;color:var(--mute);margin:6px 0}
.ctl2 input{margin-right:6px}
.hint{font-size:11.5px;line-height:1.45;color:var(--mute);margin:4px 0 0}
.hint b{color:var(--ink);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:11px}
.tile{position:relative;background:var(--card);border:1px solid var(--line);
 border-radius:var(--r);overflow:hidden;cursor:pointer}
.tile:hover{border-color:#b7b4aa}
.tile.sel{border-color:var(--accent);box-shadow:0 0 0 2px var(--accentbg)}
.tile img{width:100%;display:block;aspect-ratio:16/9;object-fit:cover;background:#eceae4}
.tile .m{padding:5px 8px;font-size:12px;color:var(--mute);display:flex;justify-content:space-between}
.tile .ck{position:absolute;top:6px;left:6px;width:20px;height:20px;border-radius:5px;
 background:rgba(255,255,255,.92);border:1px solid var(--line);display:none;
 align-items:center;justify-content:center;font-size:13px;color:var(--accent)}
body.selmode .tile .ck{display:flex}
.bartop{display:flex;gap:9px;align-items:center;margin-bottom:15px;flex-wrap:wrap}
.muted{color:var(--mute);font-size:13px}
.chk{display:flex;gap:10px;align-items:flex-start;padding:9px 12px;border-radius:var(--r);
 background:var(--card);border:1px solid var(--line);margin-bottom:7px}
.chk .dot{font-size:11px;line-height:1.7}
.chk.ok .dot{color:var(--accent)}
.chk.warn{background:var(--warnbg);border-color:#f0dcbc}
.chk.warn .dot{color:var(--warn)}
.chk.bad{background:var(--dangerbg);border-color:#eecccc}
.chk.bad .dot{color:var(--danger)}
.chk .lab{font-size:14px;font-weight:600}
.chk .det{font-size:12.5px;color:var(--mute)}
.dgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px;margin-top:14px}
.dcard{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:13px 15px}
.dcard h3{margin:0 0 8px;font-size:13px;font-weight:600}
.drow{display:flex;justify-content:space-between;gap:10px;font-size:13px;padding:4px 0;
 border-top:1px solid var(--line);font-variant-numeric:tabular-nums}
.drow span{color:var(--mute)}
.drow b{font-weight:600}
.drow b.bad{color:var(--danger)}
.dcard details{margin-top:9px;font-size:12px}
.dcard summary{cursor:pointer;color:var(--mute)}
.dcard pre{background:var(--bg);padding:9px;border-radius:8px;overflow:auto;font-size:11px;max-height:260px}
.guide{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
 padding:11px 13px;margin-top:9px}
.guide summary{cursor:pointer;font-size:13px;font-weight:600;list-style:none}
.guide summary::-webkit-details-marker{display:none}
.guide summary::before{content:"▸ ";color:var(--mute)}
.guide[open] summary::before{content:"▾ "}
.guide ol{margin:9px 0 0;padding-left:18px;font-size:12.5px;line-height:1.65;color:var(--mute)}
.guide ol b{color:var(--ink)}
.guide p{font-size:12.5px;line-height:1.55;color:var(--mute);margin:9px 0 0}
.guide p b{color:var(--ink)}
#lb{position:fixed;inset:0;background:rgba(14,14,13,.95);z-index:60;display:none;
 flex-direction:column;align-items:center;justify-content:center;padding:14px}
#lb.open{display:flex}
#lbimg{max-width:100%;max-height:calc(100vh - 104px);object-fit:contain;border-radius:5px}
#lbbar{color:#e9e7e1;font-size:13px;display:flex;gap:13px;align-items:center;
 margin-top:11px;flex-wrap:wrap;justify-content:center}
#lbbar button{background:transparent;color:#e9e7e1;border-color:#4b4a46}
#lbbar button:hover{background:#2b2b28}
#lbbar button.danger{color:#ffb3b3;border-color:#6b3a3a}
#dlg{position:fixed;inset:0;background:rgba(14,14,13,.5);z-index:70;display:none;
 align-items:center;justify-content:center;padding:16px}
#dlg.open{display:flex}
.dbox{background:var(--card);border-radius:12px;padding:20px;max-width:420px;width:100%}
.dbox h3{margin:0 0 8px;font-size:16px}
.dbox p{margin:0 0 14px;font-size:14px;color:var(--mute)}
.dbox input{width:100%;margin-bottom:14px}
.dbox .row{display:flex;gap:8px;justify-content:flex-end}
.btnlink{font-size:14px;padding:6px 12px;border:1px solid #4b4a46;border-radius:var(--r);
 color:#e9e7e1;text-decoration:none;cursor:pointer}
.btnlink:hover{background:#2b2b28}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--ink);
 color:#fff;padding:9px 16px;border-radius:var(--r);font-size:14px;z-index:80;display:none}
</style></head><body>

<header>
  <h1>Pollinator field view</h1>
  <span class="pill" id="dev">connecting</span>
  <span class="pill" id="age"></span>
  <span class="muted" style="font-size:11px">v3.5</span>
  <nav>
    <button id="tabLive" class="on">Live</button>
    <button id="tabGal">Captures</button>
    <button id="tabDbg">Debug</button>
  </nav>
</header>

<main>
  <section id="secLive" class="live">
    <div>
      <div class="vwrap">
        <img id="liveimg" alt="Live preview">
        <svg id="roisvg" viewBox="0 0 640 480" preserveAspectRatio="xMidYMid meet"></svg>
      </div>
      <p class="muted" style="margin:8px 2px 0">Drag on the preview to draw a motion region. Motion is only detected inside these boxes.</p>
    </div>
    <div>
      <div class="stat"><div class="k">Sharpness — peak this to focus</div>
        <div class="v" id="sharp">—</div><div class="s">higher is sharper</div></div>
      <div class="stat"><div class="k">Motion</div><div class="v" id="motion">—</div>
        <div class="s" id="motionSub"></div><div class="bar"><i id="motionBar" style="width:0%"></i></div></div>
      <div class="stat"><div class="k">Lens position</div><div class="v" id="lens">—</div>
        <div class="s" id="lensSub"></div></div>
      <div class="roibox" style="margin-bottom:9px">
        <h3>Camera</h3>
        <div class="ctl"><label>Focus <span id="vLens">—</span></label>
          <input type="range" id="cLens" min="0" max="15" step="0.1">
          <div class="row" style="margin-top:6px">
            <button id="btnAF">Autofocus</button>
            <button id="btnBg">Set reference frame</button>
          </div>
          <p class="hint">Dioptres. Distance = 100 ÷ value. Sweep it and stop where <b>sharpness</b> above peaks.</p></div>
        <div class="ctl"><label>Zoom <span id="vZoom">—</span></label>
          <input type="range" id="cZoom" min="1" max="4" step="0.1">
          <p class="hint">Crops the sensor. Same detail per mm, narrower view, smaller files. <b>Redraw regions after changing.</b></p></div>
      </div>
      <div class="roibox" style="margin-bottom:9px">
        <h3>Trigger</h3>
        <div class="ctl"><label>Motion pixels <span id="vMp">—</span></label>
          <input type="range" id="cMp" min="20" max="8000" step="20">
          <p class="hint">How many changed pixels trigger a capture. Set it above the <b>motion</b> reading on a windy day.</p></div>
        <div class="ctl"><label>Sensitivity <span id="vMt">—</span></label>
          <input type="range" id="cMt" min="5" max="60" step="1">
          <p class="hint">How different one pixel must be to count as changed (0–255). Lower catches more — including sensor noise in dim light.</p></div>
        <div class="ctl"><label>Confirm frames <span id="vCf">—</span></label>
          <input type="range" id="cCf" min="1" max="6" step="1">
          <p class="hint">Consecutive frames above threshold before firing. Each one adds ~0.1 s of delay.</p></div>
        <div class="ctl"><label>Cooldown <span id="vCd">—</span></label>
          <input type="range" id="cCd" min="0" max="30" step="1">
          <p class="hint">Minimum gap between captures. Too short and the camera chases its own exposure changes.</p></div>
        <div class="ctl2"><label><input type="checkbox" id="cDup"> Duplicate suppression</label>
          <p class="hint">Skips a capture when the scene looks like the last one. Can silently discard everything if your subject is small — check <b>Debug → skipped</b>.</p></div>
        <div class="ctl2"><label><input type="checkbox" id="cHours"> Active hours only</label>
          <p class="hint">Sleeps outside the daylight window. Relies on the clock being right — no RTC means an offline unit can sleep through the day.</p></div>
      </div>
      <div class="roibox">
        <h3>Motion regions</h3>
        <div id="roilist"></div>
        <div class="acts">
          <button id="roiSave" class="on">Save regions</button>
          <button id="roiClear">Clear all</button>
        </div>
      </div>
      <details class="guide">
        <summary>Tuning order &amp; troubleshooting</summary>
        <ol>
          <li>Aim the camera and set <b>zoom</b> — this decides what is in frame.</li>
          <li>Set <b>focus</b> by peaking the sharpness number, in daylight.</li>
          <li>Draw <b>motion regions</b> around the flowers.</li>
          <li>Watch <b>motion</b> through a windy spell, then set <b>motion pixels</b> above what the wind produces.</li>
          <li>Leave it an hour, then check <b>Debug</b> — triggers and captures should climb together.</li>
        </ol>
        <p>Each step changes what the next one means, so redo them in order. <b>Changing zoom invalidates your regions.</b></p>
        <p>Nothing being captured? Open <b>Debug</b> and read top to bottom. Triggers climbing while captures stay flat means something downstream is dropping them — usually duplicate suppression.</p>
      </details>
    </div>
  </section>

  <section id="secGal" class="hide">
    <div class="bartop">
      <select id="date"></select>
      <span class="muted" id="count"></span>
      <button id="selBtn" style="margin-left:auto">Select</button>
      <button id="dlSel" class="hide">Download selected</button><button id="delSel" class="danger hide">Delete selected</button>
      <button id="refresh">Refresh</button>
      <button id="delDay" class="danger">Delete day…</button>
    </div>
    <div class="grid" id="grid"></div>
  </section>

  <section id="secDbg" class="hide">
    <div class="bartop"><button id="dbgRefresh">Refresh</button>
      <span class="muted" id="dbgWhen"></span></div>
    <div id="checks"></div>
    <div id="dbgCards"></div>
  </section>

</main>

<div id="lb"><img id="lbimg" alt=""><div id="lbbar">
  <button id="pv">← Prev</button><span id="lbmeta"></span><button id="nx">Next →</button>
  <a id="lbdl" class="btnlink">Download</a><button id="lbdel" class="danger">Delete</button><button id="cl">Close (Esc)</button>
</div></div>

<div id="dlg"><div class="dbox">
  <h3 id="dTitle"></h3><p id="dMsg"></p>
  <input id="dInput" class="hide" autocomplete="off">
  <div class="row"><button id="dCancel">Cancel</button><button id="dOk" class="danger">Delete</button></div>
</div></div>

<div id="toast"></div>

<script>
const $=s=>document.querySelector(s);
let items=[],curDate="",idx=-1,rois=[],selMode=false,sel=new Set(),scrollY=0,dirty=false,drag=null;

function toast(m){const t=$("#toast");t.textContent=m;t.style.display="block";
  clearTimeout(t._t);t._t=setTimeout(()=>t.style.display="none",2800);}
async function post(u,b){return(await fetch(u,{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify(b)})).json();}

function showTab(t){
  ["Live","Gal","Dbg"].forEach(n=>{
    $("#sec"+n).classList.toggle("hide",n!==t);
    $("#tab"+n).classList.toggle("on",n===t);
  });
  if(t==="Gal"&&!items.length)loadDates();
  if(t==="Dbg")loadDebug();
}
$("#tabLive").onclick=()=>showTab("Live");
$("#tabGal").onclick=()=>showTab("Gal");
$("#tabDbg").onclick=()=>showTab("Dbg");

/* ---------- live ---------- */
const svg=$("#roisvg");
async function tick(){
  try{
    const s=await(await fetch("/live.json",{cache:"no-store"})).json();
    if(s.error){$("#dev").textContent="no live frame";$("#dev").className="pill stale";return;}
    $("#dev").textContent=s.device_id;$("#dev").className="pill";
    const stale=s.age>4;
    $("#age").textContent=stale?("stale "+s.age+"s"):(s.age+"s ago");
    $("#age").className="pill"+(stale?" stale":"");
    $("#sharp").textContent=Math.round(s.sharpness);
    $("#motion").textContent=(s.motion_pixels||0).toLocaleString();
    $("#motionSub").textContent="threshold "+(s.motion_threshold||0).toLocaleString()+
      (s.roi_enabled?(" · "+(s.rois||[]).length+" region"):" · full frame");
    $("#motionBar").style.width=Math.min(100,100*(s.motion_pixels||0)/Math.max(1,(s.motion_threshold||1)*3)).toFixed(0)+"%";
    $("#lens").textContent=s.lens_position==null?"auto":s.lens_position;
    $("#lensSub").textContent=s.lens_position==null?(s.autofocus_mode||""):
      ("≈ "+(100/s.lens_position).toFixed(0)+" cm · "+(s.autofocus_mode||""));
    syncControls(s);
    if(s.lowres&&s.lowres[0])svg.setAttribute("viewBox","0 0 "+s.lowres[0]+" "+s.lowres[1]);
    if(!dirty&&s.rois){rois=s.rois.map(r=>r.slice());drawRois();}
    if(!$("#secLive").classList.contains("hide"))$("#liveimg").src="/live.jpg?t="+Date.now();
  }catch(e){}
}

/* ---------- controls ---------- */
let touched={},setTimer={};
function bindCtl(id,key,fmt,transform){
  const el=$("#"+id),out=$("#v"+id.slice(1));
  el.addEventListener("input",()=>{
    touched[id]=Date.now();
    out.textContent=fmt(+el.value);
  });
  el.addEventListener("change",()=>{
    clearTimeout(setTimer[id]);
    setTimer[id]=setTimeout(async()=>{
      const v=transform?transform(+el.value):+el.value;
      const r=await post("/api/set",{key:key,value:v});
      toast(r.ok?(key+" = "+v):("failed: "+(r.error||"?")));
      touched[id]=0;
    },350);
  });
}
function bindChk(id,key){
  const el=$("#"+id);
  el.addEventListener("change",async()=>{
    touched[id]=Date.now();
    const r=await post("/api/set",{key:key,value:el.checked});
    toast(r.ok?(key+" = "+el.checked):"failed");
    touched[id]=0;
  });
}
async function doAction(btn,action,msg){
  const el=$("#"+btn); if(!el) return;
  const old=el.textContent; el.disabled=true; el.textContent="working…";
  try{
    const r=await post("/api/action",{action:action});
    toast(r.ok?msg:("failed: "+(r.error||"?")));
  }catch(e){ toast("failed: "+e); }
  // The sweep steps 25 lens positions at ~0.45s each, so ~13s plus overhead.
  setTimeout(()=>{el.disabled=false;el.textContent=old;},action==="autofocus"?16000:1500);
}
document.addEventListener("click",e=>{
  if(e.target.id==="btnAF")    doAction("btnAF","autofocus","Autofocus running — watch the log");
  if(e.target.id==="btnBg")    doAction("btnBg","set-reference","Reference frame set from the current view");
});

bindCtl("cLens","lens_position",v=>v.toFixed(1)+(v<0.05?" (inf)":" ("+(100/v).toFixed(0)+" cm)"));
bindCtl("cZoom","sensor_crop",v=>v.toFixed(1)+"×");
bindCtl("cMp","motion_pixels",v=>v.toLocaleString()+" px",v=>Math.round(v));
bindCtl("cMt","motion_threshold",v=>String(Math.round(v)),v=>Math.round(v));
bindCtl("cCf","motion_confirm_frames",v=>String(Math.round(v)),v=>Math.round(v));
bindCtl("cCd","cooldown_seconds",v=>Math.round(v)+" s",v=>Math.round(v));
bindChk("cDup","duplicate_suppression_enabled");
bindChk("cHours","active_hours_enabled");

function syncCtl(id,val,fmt){
  if(val==null)return;
  if(touched[id]&&Date.now()-touched[id]<3000)return;
  const el=$("#"+id);
  if(document.activeElement===el)return;
  el.value=val;$("#v"+id.slice(1)).textContent=fmt(+val);
}
function syncControls(s){
  syncCtl("cLens",s.lens_position,v=>v.toFixed(1)+(v<0.05?" (inf)":" ("+(100/v).toFixed(0)+" cm)"));
  syncCtl("cZoom",s.sensor_crop,v=>v.toFixed(1)+"×");
  syncCtl("cMp",s.motion_threshold,v=>v.toLocaleString()+" px");
  syncCtl("cMt",s.pixel_delta,v=>String(Math.round(v)));
  syncCtl("cCf",s.motion_confirm_frames,v=>String(v));
  syncCtl("cCd",s.cooldown_seconds,v=>v+" s");
  if(!touched.cDup)$("#cDup").checked=!!s.duplicate_suppression;
  if(!touched.cHours)$("#cHours").checked=!!s.active_hours_enabled;
}

/* ---------- roi ---------- */
function svgPt(e){const p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;
  return p.matrixTransform(svg.getScreenCTM().inverse());}
function drawRois(){
  let h="";
  rois.forEach((r,i)=>{
    h+='<rect x="'+r[0]+'" y="'+r[1]+'" width="'+(r[2]-r[0])+'" height="'+(r[3]-r[1])+
       '" fill="rgba(26,107,82,.16)" stroke="#2fbf92" stroke-width="2"/>'+
       '<text x="'+(r[0]+5)+'" y="'+(r[1]+17)+'" fill="#2fbf92" font-size="15" font-family="sans-serif">'+(i+1)+'</text>';
  });
  if(drag)h+='<rect x="'+Math.min(drag.x0,drag.x1)+'" y="'+Math.min(drag.y0,drag.y1)+
    '" width="'+Math.abs(drag.x1-drag.x0)+'" height="'+Math.abs(drag.y1-drag.y0)+
    '" fill="rgba(47,191,146,.2)" stroke="#2fbf92" stroke-width="2" stroke-dasharray="5 4"/>';
  svg.innerHTML=h;
  $("#roilist").innerHTML=rois.length?rois.map((r,i)=>
    '<div class="roirow"><span>'+(i+1)+': '+r[0]+','+r[1]+' → '+r[2]+','+r[3]+
    ' ('+(r[2]-r[0])+'×'+(r[3]-r[1])+')</span><button data-i="'+i+'" class="rmroi">Remove</button></div>'
  ).join(""):'<p class="muted" style="margin:0;font-size:12px">None — full frame is watched.</p>';
  document.querySelectorAll(".rmroi").forEach(b=>b.onclick=()=>{
    rois.splice(+b.dataset.i,1);dirty=true;drawRois();});
}
svg.addEventListener("pointerdown",e=>{const p=svgPt(e);
  drag={x0:p.x,y0:p.y,x1:p.x,y1:p.y};svg.setPointerCapture(e.pointerId);});
svg.addEventListener("pointermove",e=>{if(!drag)return;const p=svgPt(e);
  drag.x1=p.x;drag.y1=p.y;drawRois();});
svg.addEventListener("pointerup",()=>{
  if(!drag)return;
  const x1=Math.round(Math.min(drag.x0,drag.x1)),y1=Math.round(Math.min(drag.y0,drag.y1)),
        x2=Math.round(Math.max(drag.x0,drag.x1)),y2=Math.round(Math.max(drag.y0,drag.y1));
  drag=null;
  if(x2-x1>6&&y2-y1>6){rois.push([x1,y1,x2,y2]);dirty=true;}
  drawRois();
});
$("#roiClear").onclick=()=>{rois=[];dirty=true;drawRois();};
$("#roiSave").onclick=async()=>{
  const r=await post("/api/roi",{rois:rois,roi_enabled:rois.length>0});
  dirty=false;toast(r.ok?("Saved "+rois.length+" region(s) — camera reloaded"):"Save failed");
};

/* ---------- gallery ---------- */
async function loadDates(){
  const ds=await(await fetch("/api/dates")).json();
  const s=$("#date");s.innerHTML="";
  ds.forEach(d=>{const o=document.createElement("option");o.value=o.textContent=d;s.appendChild(o)});
  if(ds.length){curDate=ds[0];s.value=curDate;loadImages();}
  else{$("#count").textContent="no captures yet";$("#grid").innerHTML="";}
}
$("#date").onchange=e=>{curDate=e.target.value;sel.clear();loadImages()};
$("#refresh").onclick=()=>loadImages();
$("#selBtn").onclick=()=>{selMode=!selMode;sel.clear();
  document.body.classList.toggle("selmode",selMode);
  $("#selBtn").classList.toggle("on",selMode);
  $("#delSel").classList.toggle("hide",!selMode);
  $("#dlSel").classList.toggle("hide",!selMode);loadImages();};
function updateSel(){$("#delSel").textContent="Delete selected ("+sel.size+")";
  $("#delSel").disabled=sel.size===0;
  $("#dlSel").textContent="Download selected ("+sel.size+")";
  $("#dlSel").disabled=sel.size===0;}
async function loadImages(){
  items=await(await fetch("/api/images?date="+encodeURIComponent(curDate))).json();
  $("#count").textContent=items.length+" image"+(items.length===1?"":"s");
  const g=$("#grid");g.innerHTML="";
  items.forEach((it,i)=>{
    const d=document.createElement("div");d.className="tile"+(sel.has(it.name)?" sel":"");
    const t=new Date(it.mtime*1000).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",second:"2-digit"});
    d.innerHTML='<div class="ck">'+(sel.has(it.name)?"✓":"")+'</div>'+
      '<img loading="lazy" src="/thumb?date='+curDate+'&name='+encodeURIComponent(it.name)+'" alt="">'+
      '<div class="m"><span>'+t+'</span><span>'+Math.round(it.size/1024)+' KB</span></div>';
    d.onclick=()=>{
      if(selMode){
        if(sel.has(it.name))sel.delete(it.name);else sel.add(it.name);
        d.classList.toggle("sel");d.querySelector(".ck").textContent=sel.has(it.name)?"✓":"";
        updateSel();
      } else open_(i);
    };
    g.appendChild(d);
  });
  updateSel();
}

/* ---------- lightbox ---------- */
function open_(i){
  const it=items[i];if(!it)return;
  if(idx<0)scrollY=window.scrollY;
  idx=i;
  $("#lb").classList.add("open");
  document.body.style.overflow="hidden";
  $("#lbimg").src="/full?date="+curDate+"&name="+encodeURIComponent(it.name);
  $("#lbmeta").textContent=it.name+" · "+Math.round(it.size/1024)+" KB · "+(i+1)+"/"+items.length;
  const dl=$("#lbdl");
  dl.href="/download?date="+curDate+"&name="+encodeURIComponent(it.name);
  dl.setAttribute("download",it.name);
  [items[i+1],items[i-1]].forEach(n=>{if(n){const p=new Image();
    p.src="/full?date="+curDate+"&name="+encodeURIComponent(n.name);}});
}
function close_(){
  $("#lb").classList.remove("open");
  document.body.style.overflow="";idx=-1;
  window.scrollTo({top:scrollY});
}
$("#pv").onclick=()=>{if(idx>0)open_(idx-1)};
$("#nx").onclick=()=>{if(idx<items.length-1)open_(idx+1)};
$("#cl").onclick=close_;
$("#lb").onclick=e=>{if(e.target.id==="lb")close_()};
$("#lbdel").onclick=()=>{
  const it=items[idx];if(!it)return;const keep=idx;
  confirmDlg("Delete this image?",it.name+" — this cannot be undone.",null,async()=>{
    await post("/api/delete",{date:curDate,names:[it.name]});
    close_();await loadImages();toast("Deleted");
    if(items.length)open_(Math.min(keep,items.length-1));
  });
};
document.addEventListener("keydown",e=>{
  if($("#dlg").classList.contains("open"))return;
  if(idx<0)return;
  if(e.key==="Escape")close_();
  else if(e.key==="ArrowLeft"&&idx>0)open_(idx-1);
  else if(e.key==="ArrowRight"&&idx<items.length-1)open_(idx+1);
});

/* ---------- confirm ---------- */
let dCb=null;
function confirmDlg(title,msg,requireText,cb){
  $("#dTitle").textContent=title;$("#dMsg").textContent=msg;
  const inp=$("#dInput");
  inp.classList.toggle("hide",!requireText);inp.value="";inp.placeholder=requireText||"";
  $("#dOk").disabled=!!requireText;
  inp.oninput=()=>{$("#dOk").disabled=inp.value.trim()!==requireText;};
  dCb=cb;$("#dlg").classList.add("open");
  if(requireText)setTimeout(()=>inp.focus(),50);
}
$("#dCancel").onclick=()=>{$("#dlg").classList.remove("open");dCb=null;};
$("#dlg").onclick=e=>{if(e.target.id==="dlg")$("#dCancel").click()};
$("#dOk").onclick=async()=>{const cb=dCb;$("#dlg").classList.remove("open");dCb=null;if(cb)await cb();};
$("#delSel").onclick=()=>{
  const names=[...sel];
  confirmDlg("Delete "+names.length+" image(s)?","This cannot be undone.",null,async()=>{
    const r=await post("/api/delete",{date:curDate,names:names});
    sel.clear();await loadImages();toast("Deleted "+r.deleted);});
};
$("#dlSel").onclick=async()=>{
  const names=[...sel];
  if(!names.length)return;
  if(names.length>60){toast("Max 60 at a time");return;}
  toast("Building zip…");
  try{
    const r=await fetch("/api/zip",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({date:curDate,names:names})});
    if(!r.ok){toast("Zip failed");return;}
    const blob=await r.blob();
    const a=document.createElement("a");
    a.href=URL.createObjectURL(blob);
    a.download=curDate+".zip";a.click();
    setTimeout(()=>URL.revokeObjectURL(a.href),8000);
    toast("Downloaded "+names.length+" image(s)");
  }catch(e){toast("Zip failed");}
};
$("#delDay").onclick=()=>{
  if(!curDate)return;
  confirmDlg("Delete an entire day?",
    "All images for "+curDate+" will be permanently removed. Type the date to confirm.",
    curDate,async()=>{await post("/api/delete_day",{date:curDate});
      toast("Day deleted");await loadDates();});
};

/* ---------- debug ---------- */
function ago(ts){if(!ts)return "never";
  const s=Math.round(Date.now()/1000-ts);
  if(s<60)return s+"s ago";if(s<3600)return Math.round(s/60)+"m ago";
  return Math.round(s/3600)+"h ago";}
function dur(s){if(s==null)return "—";
  if(s<60)return s+"s";if(s<3600)return Math.round(s/60)+"m";
  return (s/3600).toFixed(1)+"h";}
function row(k,v,bad){return '<div class="drow"><span>'+k+'</span><b'+(bad?' class="bad"':'')+'>'+v+'</b></div>';}
function card(t,b){return '<div class="dcard"><h3>'+t+'</h3>'+b+'</div>';}

async function loadDebug(){
  let d;
  try{d=await(await fetch("/api/debug",{cache:"no-store"})).json();}
  catch(e){$("#checks").innerHTML='<p class="muted">debug fetch failed</p>';return;}
  $("#dbgWhen").textContent=d.device_id+" · "+new Date().toLocaleTimeString();
  $("#checks").innerHTML=d.checks.map(c=>
    '<div class="chk '+c.state+'"><span class="dot">'+
    (c.state==="ok"?"●":c.state==="warn"?"▲":"■")+'</span><div><div class="lab">'+
    c.label+'</div><div class="det">'+c.detail+'</div></div></div>').join("");

  const L=d.live||{},C=d.counts||{},S=d.system||{},cf=d.config||{},out=[];

  out.push(card("Services",
    d.services.map(s=>row(s.name.replace(".service",""),s.active+" / "+s.enabled,
      s.active!=="active")).join("")+
    (d.cam?row("pid",d.cam.pid)+row("uptime",dur(d.cam.uptime_s))+row("memory",d.cam.rss_mb+" MB")
         :row("capture process","NOT RUNNING",true))));

  out.push(card("Capture loop",
    row("loop uptime",dur(L.loop_uptime))+
    row("live frame",L.age==null?"none":L.age+"s old",L.age>5)+
    row("motion triggers",C.trigger??0)+
    row("images captured",C.captured??0,(C.trigger>0&&!C.captured))+
    row("last trigger",ago(L.last_trigger))+
    row("last capture",ago(L.last_captured))+
    row("captures today",d.captures_today)));

  out.push(card("Skipped captures",
    row("duplicate suppression",C.skip_duplicate??0,(C.skip_duplicate||0)>0)+
    row("quality gate",C.skip_quality??0,(C.skip_quality||0)>0)+
    row("disk full",C.skip_disk??0,(C.skip_disk||0)>0)+
    row("outside active hours",C.skip_hours??0)));

  out.push(card("Detection",
    row("motion",(L.motion_pixels??0).toLocaleString()+" / "+(L.motion_threshold??0).toLocaleString())+
    row("watching",L.roi_enabled?((L.rois||[]).length+" region(s)"):
        "full frame ("+(L.motion_frame_pixels||0).toLocaleString()+" px)")+
    row("duplicate suppression",L.duplicate_suppression?("on, threshold "+L.duplicate_threshold):"off",
        L.duplicate_suppression&&(C.skip_duplicate||0)>0)+
    row("quality gate",L.quality_gate?"on":"off")+
    row("cooldown",L.cooldown_seconds+"s")+
    row("active hours",L.active_hours?L.active_hours.join(" – "):"always on")+
    row("still size",(L.still||[]).join(" × "))+
    row("sharpness",Math.round(L.sharpness??0))));

  out.push(card("System",
    row("disk free",(S.disk_free_gb??"?")+" GB ("+(S.disk_used_pct??"?")+"% used)",S.disk_used_pct>90)+
    row("memory available",(S.mem_avail_mb??"?")+" / "+(S.mem_total_mb??"?")+" MB",S.mem_avail_mb<40)+
    row("swap in use",(S.swap_used_mb??0)+" MB",(S.swap_used_mb??0)>60)+
    row("load (1m)",S.load1==null?"—":S.load1.toFixed(2))+
    row("cpu temp",S.cpu_temp_c==null?"—":S.cpu_temp_c+" °C",S.cpu_temp_c>75)+
    (S.throttled?row("throttling",S.throttled,S.throttled!=="throttled=0x0"):"")));

  const N=d.network||{},T=d.temps||[];
  out.push(card("Network",
    row("hostname",N.hostname||"—")+
    row("interface",N.iface||"—")+
    row("ssid",N.ssid||"—")+
    row("signal",N.signal_dbm==null?"—":(N.signal_dbm.toFixed(0)+" dBm ("+(N.rating||"?")+")"),
        N.rating==="weak")+
    row("link quality",N.quality==null?"—":(N.quality.toFixed(0)+" / "+N.quality_max))+
    row("ip address",N.ip||"—")+
    (N.signal_dbm!=null?'<div class="bar" style="margin-top:8px"><i style="width:'+
      Math.max(0,Math.min(100,(N.signal_dbm+90)*(100/40))).toFixed(0)+
      '%;background:'+(N.rating==="weak"?"#a63232":N.rating==="fair"?"#8a5a0b":"#1a6b52")+
      '"></i></div>':"")));

  out.push(card("Temperature",
    (T.length?T.map(t=>row(t.label,t.c+" °C",t.c>(t.source==="ds18b20"?45:75))).join("")
            :row("sensors","none found"))+
    (T.every(t=>t.source!=="ds18b20")
      ? '<p class="muted" style="font-size:12px;margin:9px 0 0">Only SoC temperature. '+
        'Wire a DS18B20 or DS3231 for enclosure air temperature.</p>' : "")));

  out.push(card("Config layers",
    row("baseline keys",Object.keys(cf.baseline||{}).length)+
    row("device overrides",(cf.overridden||[]).length)+
    '<details><summary>device.json (per-camera)</summary><pre>'+
      JSON.stringify(cf.device||{},null,2)+'</pre></details>'));

  $("#dbgCards").innerHTML='<div class="dgrid">'+out.join("")+'</div>';
}
$("#dbgRefresh").onclick=loadDebug;
setInterval(()=>{if(!$("#secDbg").classList.contains("hide"))loadDebug();},5000);

drawRois();
setInterval(tick,900);tick();
</script></body></html>
"""


def main():
    global IMAGE_ROOT, CONFIG_DIR, AUTH_TOKEN
    ap = argparse.ArgumentParser(description="Pollinator field UI")
    ap.add_argument("image_root")
    ap.add_argument("--config", default="/etc/pollinator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--auth-token", default=os.environ.get("POLLINATOR_FIELD_TOKEN") or None)
    args = ap.parse_args()

    IMAGE_ROOT = os.path.abspath(args.image_root)
    CONFIG_DIR = args.config
    AUTH_TOKEN = args.auth_token
    os.makedirs(os.path.join(IMAGE_ROOT, THUMB_DIRNAME), exist_ok=True)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"Field UI on http://{args.host}:{args.port}  root={IMAGE_ROOT}")
    if not AUTH_TOKEN:
        print("WARNING: no auth token; anyone on the network can view and DELETE")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
