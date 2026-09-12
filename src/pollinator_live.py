"""Publish live preview frames and loop telemetry to tmpfs.

The capture service owns the camera exclusively, so nothing else can open it.
Rather than run an HTTP server inside the capture loop (which could stall it),
we drop the latest preview frame and a status blob into /run/pollinator. That
is tmpfs — RAM, not the SD card — so it costs no flash wear.

The counters matter as much as the image. Several capture skips in
pollinator_cam.py are silent or log at debug level, so a camera can look
perfectly healthy while quietly discarding every frame. Counting them is the
difference between "it's broken somehow" and "duplicate suppression ate 412
captures".

Every call is wrapped so a failure here can never take capture down.
"""

import json
import os
import socket
import time

RUN_DIR = "/run/pollinator"
LIVE_JPG = os.path.join(RUN_DIR, "live.jpg")
LIVE_JSON = os.path.join(RUN_DIR, "live.json")

_last_write = 0.0
_started = time.time()

EVENTS = (
    "trigger",          # motion crossed threshold
    "captured",         # image written to disk
    "skip_duplicate",   # preview too similar to last capture
    "skip_quality",     # quality gate rejected it
    "skip_disk",        # below min_free_mb
    "skip_hours",       # outside active_hours
    "skip_global",      # whole-frame change: exposure shift, cloud, not an insect
)
_counts = {e: 0 for e in EVENTS}
_last = {"trigger": None, "captured": None}


def note(event):
    """Record that something happened in the capture loop. Never raises."""
    try:
        if event in _counts:
            _counts[event] += 1
        if event in _last:
            _last[event] = time.time()
    except Exception:
        pass


def _atomic_write(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def compute_sharpness(gray, rois=None):
    """Laplacian variance — higher is sharper. Focus by maximising this.

    Measured inside the ROI when one is set, so focus is judged on the flower
    rather than on whatever background happens to be crisp.
    """
    import cv2

    region = gray
    if rois:
        x1, y1, x2, y2 = rois[0]
        if x2 > x1 and y2 > y1:
            region = gray[y1:y2, x1:x2]
    if region.size == 0:
        return 0.0
    return float(cv2.Laplacian(region, cv2.CV_64F).var())


def publish(frame, motion_pixels, cfg, rois=None, threshold=None, min_interval=0.33):
    """Write the current preview frame plus telemetry. Throttled, best-effort.

    `frame` is the raw preview array (RGB888), NOT the blurred grayscale used
    for motion detection — blurring destroys the sharpness signal.
    """
    global _last_write

    now = time.time()
    if now - _last_write < min_interval:
        return
    _last_write = now

    try:
        import cv2

        os.makedirs(RUN_DIR, exist_ok=True)

        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        sharpness = compute_sharpness(gray, rois)

        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if ok:
            _atomic_write(LIVE_JPG, buf.tobytes())

        status = {
            "ts": now,
            "loop_uptime": round(now - _started, 1),
            "device_id": cfg.get("device_id", "unknown"),
            "motion_pixels": int(motion_pixels),
            "motion_threshold": int(threshold if threshold is not None else cfg.get("motion_pixels", 0)),
            "motion_frame_pixels": int(cfg.get("lowres_width", 0)) * int(cfg.get("lowres_height", 0)),
            "sharpness": round(sharpness, 1),
            # cfg's motion_threshold is the per-pixel brightness delta (0-255).
            # Published under a distinct name because "motion_threshold" in this
            # payload already means the configured trigger count.
            "pixel_delta": cfg.get("motion_threshold"),
            "lens_position": cfg.get("lens_position"),
            "autofocus_mode": cfg.get("autofocus_mode"),
            "lowres": [cfg.get("lowres_width"), cfg.get("lowres_height")],
            "still": [cfg.get("still_width"), cfg.get("still_height")],
            "rois": [list(r) for r in (rois or [])],
            "roi_enabled": bool(cfg.get("roi_enabled")),
            "cooldown_seconds": cfg.get("cooldown_seconds"),
            "motion_confirm_frames": cfg.get("motion_confirm_frames"),
            "sensor_crop": cfg.get("sensor_crop", 1.0),
            "active_hours_enabled": bool(cfg.get("active_hours_enabled")),
            "duplicate_suppression": bool(cfg.get("duplicate_suppression_enabled")),
            "duplicate_threshold": cfg.get("duplicate_hash_threshold"),
            "quality_gate": bool(cfg.get("quality_gate_enabled")),
            "active_hours": (
                [cfg.get("active_hours_start"), cfg.get("active_hours_end")]
                if cfg.get("active_hours_enabled") else None
            ),
            "counts": dict(_counts),
            "last_trigger": _last["trigger"],
            "last_captured": _last["captured"],
        }
        _atomic_write(LIVE_JSON, json.dumps(status).encode("utf-8"))
    except Exception:
        # Live view is a convenience. Capture is not. Never propagate.
        pass


def sd_notify(state):
    """Talk to systemd's watchdog without pulling in a dependency.

    A hung capture loop keeps its process alive, so Restart=on-failure never
    fires and the camera reports healthy while doing nothing. Pinging the
    watchdog each iteration turns a hang into a restart.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.connect(addr)
        sock.sendall(state.encode("utf-8"))
        sock.close()
    except Exception:
        pass


def ready():
    sd_notify("READY=1")


def heartbeat():
    sd_notify("WATCHDOG=1")
