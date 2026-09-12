#!/usr/bin/env python3
import signal
import sys
import time
import logging
import os
import json
from datetime import datetime

import cv2
from picamera2 import Picamera2

import pollinator_live  # POLLINATOR_LIVE
from pollinator_common import (
    apply_quality_gate,
    build_still_configuration,
    capture_high_quality_still,
    count_motion_pixels,
    enable_continuous_autofocus,
    frame_dhash,
    free_mb_for_path,
    get_motion_rois,
    has_min_free_space,
    is_duplicate_frame,
    is_within_active_hours,
    load_config,
    prune_old_files,
    setup_logging,
)

log = setup_logging("pollinator.cam")
_shutdown = False
_reload_requested = False


def _handle_signal(signum, _frame):
    global _shutdown
    log.info("Received signal %s, shutting down", signum)
    _shutdown = True


def _handle_sighup(_signum, _frame):
    global _reload_requested
    log.info("Received SIGHUP, config reload requested")
    _reload_requested = True


REQUEST_PATH = "/run/pollinator/request"
_ALLOWED_REQUESTS = {"autofocus", "set-reference", "reset-background"}


def _take_request():
    """Read and consume a one-word request from the field UI, if any."""
    try:
        with open(REQUEST_PATH, encoding="utf-8") as fh:
            action = fh.read(32).strip().lower()
    except OSError:
        return None
    try:
        os.unlink(REQUEST_PATH)
    except OSError:
        pass
    if action in _ALLOWED_REQUESTS:
        return action
    if action:
        log.warning("Ignoring unknown request: %r", action)
    return None


def _write_device_key(config_path, key, value):
    """Write one key into device.json atomically. baseline.json is untouched."""
    path = os.path.join(config_path, "device.json") if os.path.isdir(config_path) else config_path
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data[key] = value
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def run_autofocus_sweep(picam2, cfg, rois, log):
    """Step the lens across its range and return the sharpest position.

    Sharpness is measured inside the motion ROIs when set, otherwise the
    centre third. Measuring the whole frame is dominated by background and
    will happily focus on the dirt behind the flower.
    """
    try:
        from libcamera import controls as _ctrls
        lo, hi, _cur = picam2.camera_controls["LensPosition"]
    except Exception as exc:
        log.warning("Autofocus sweep unavailable: %s", exc)
        return None, None, []

    H, W = picam2.capture_array().shape[:2]
    # Measure each ROI separately and average. Hulling several scattered ROIs
    # produces a box covering most of the frame, which just focuses on whatever
    # background happens to have the hardest edges.
    boxes = []
    for r in (rois or []):
        bx1, by1 = max(0, int(r[0])), max(0, int(r[1]))
        bx2, by2 = min(W, int(r[2])), min(H, int(r[3]))
        if bx2 - bx1 >= 16 and by2 - by1 >= 16:
            boxes.append((bx1, by1, bx2, by2))
    if not boxes:
        boxes = [(W // 3, H // 3, 2 * W // 3, 2 * H // 3)]

    steps, settle = 24, 0.45
    best_lens, best_sharp, table = None, -1.0, []
    try:
        picam2.set_controls({"AfMode": _ctrls.AfModeEnum.Manual})
        time.sleep(0.3)
        for i in range(steps + 1):
            lp = lo + (hi - lo) * i / steps
            picam2.set_controls({"LensPosition": float(lp)})
            time.sleep(settle)
            full = cv2.cvtColor(picam2.capture_array(), cv2.COLOR_RGB2GRAY)
            vals = [float(cv2.Laplacian(full[b1:b3, b0:b2], cv2.CV_64F).var())
                    for b0, b1, b2, b3 in boxes]
            sharp = sum(vals) / len(vals)
            table.append((round(lp, 2), round(sharp, 1)))
            if sharp > best_sharp:
                best_lens, best_sharp = lp, sharp
            pollinator_live.heartbeat()   # the sweep takes ~12s; feed the watchdog
    except Exception as exc:
        log.warning("Autofocus sweep failed: %s", exc)
        return None, None, table

    log.info("Autofocus sweep over %d ROI(s): %s", len(boxes), table)
    if best_lens is not None and (best_lens <= lo + 1e-6 or best_lens >= hi - 1e-6):
        log.warning(
            "Autofocus peak at the %s end of the lens range (%.2f). The subject is "
            "probably outside the focus range, or the ROIs are seeing background.",
            "near" if best_lens >= hi - 1e-6 else "far", best_lens,
        )
    if best_lens is not None:
        picam2.set_controls({"LensPosition": float(best_lens)})
    return best_lens, best_sharp, table


def ensure_dirs(cfg):
    os.makedirs(cfg["output_base"], exist_ok=True)
    if cfg["debug_enabled"]:
        os.makedirs(os.path.join(cfg["output_base"], cfg["debug_dirname"]), exist_ok=True)


def apply_sensor_crop(picam2, cfg, log=None):  # POLLINATOR_ZOOM
    """Centre-crop the sensor by `sensor_crop` times. 1.0 means full field.

    ScalerCrop is expressed in the sensor's full pixel-array coordinates, so we
    ask the camera for its maximum rectangle rather than assuming 4608x2592.
    Best-effort: a failure here must never stop capture.
    """
    logger = log or logging.getLogger("pollinator")
    try:
        zoom = float(cfg.get("sensor_crop", 1.0) or 1.0)
    except (TypeError, ValueError):
        return
    zoom = max(1.0, min(6.0, zoom))

    full = None
    try:
        full = picam2.camera_properties.get("ScalerCropMaximum")
    except Exception:
        pass
    if not full or len(full) != 4 or int(full[2]) == 0:
        try:
            full = picam2.camera_controls["ScalerCrop"][1]
        except Exception:
            logger.warning("ScalerCrop unavailable; zoom ignored")
            return

    x, y, w, h = (int(v) for v in full)
    nw, nh = int(w / zoom) & ~1, int(h / zoom) & ~1
    nx, ny = x + (w - nw) // 2, y + (h - nh) // 2
    try:
        picam2.set_controls({"ScalerCrop": (nx, ny, nw, nh)})
        logger.info("Sensor crop %.2fx -> (%d, %d, %d, %d)", zoom, nx, ny, nw, nh)
    except Exception as exc:
        logger.warning("Could not set ScalerCrop: %s", exc)


def main():
    global _shutdown, _reload_requested
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle_sighup)

    config_path = sys.argv[1] if len(sys.argv) > 1 else "pollinator_cam_config.json"
    cfg = load_config(config_path)
    ensure_dirs(cfg)
    log.info("Starting pollinator cam with config %s", config_path)

    motion_rois = get_motion_rois(cfg)
    def _config_mtime(path):
        if os.path.isdir(path):
            return max(
                (os.path.getmtime(os.path.join(path, n))
                 for n in ("baseline.json", "device.json")
                 if os.path.exists(os.path.join(path, n))),
                default=0,
            )
        return os.path.getmtime(path) if os.path.exists(path) else 0

    config_mtime = _config_mtime(config_path)
    last_config_check = time.time()

    picam2 = Picamera2()
    preview_config = picam2.create_preview_configuration(
        main={"size": (cfg["lowres_width"], cfg["lowres_height"]), "format": "RGB888"},
        sensor={"output_size": (2304, 1296), "bit_depth": 10},
        buffer_count=3,
    )
    still_config = build_still_configuration(picam2, cfg)

    picam2.configure(preview_config)
    picam2.start()
    time.sleep(cfg["warmup_seconds"])
    pollinator_live.ready()  # POLLINATOR_STABILITY
    apply_sensor_crop(picam2, cfg, log=log)  # POLLINATOR_ZOOM
    if cfg.get("autofocus_enabled", True):
        enable_continuous_autofocus(picam2, log=log, cfg=cfg)
        time.sleep(0.5)

    background = None
    last_capture = 0
    last_debug = 0
    motion_streak = 0
    last_capture_hash = None

    def maybe_reload_config(force=False):
        nonlocal cfg, motion_rois, config_mtime, background, motion_streak, last_capture_hash
        if cfg["config_reload_seconds"] <= 0 and not force:
            return
        try:
            mtime = _config_mtime(config_path)
        except OSError:
            return
        if not force and mtime == config_mtime:
            return
        new_cfg = load_config(config_path)
        cfg = new_cfg
        config_mtime = mtime
        motion_rois = get_motion_rois(cfg)
        background = None
        motion_streak = 0
        last_capture_hash = None
        log.info("Reloaded config from %s (%s motion ROI(s))", config_path, len(motion_rois))
        apply_sensor_crop(picam2, cfg, log=log)  # POLLINATOR_ZOOM
        if cfg.get("autofocus_enabled", True):
            enable_continuous_autofocus(picam2, log=log, cfg=cfg)

    def capture_still(preview_frame=None):
        nonlocal last_capture, background, motion_streak, last_capture_hash
        if preview_frame is not None and is_duplicate_frame(cfg, preview_frame, last_capture_hash):
            log.info("Skipping duplicate capture (preview hash too similar)")
            pollinator_live.note("skip_duplicate")  # POLLINATOR_COUNTERS
            motion_streak = 0
            return

        if not has_min_free_space(cfg["output_base"], cfg["min_free_mb"]):
            pollinator_live.note("skip_disk")  # POLLINATOR_COUNTERS
            log.warning(
                "Skipping capture: only %.0f MB free (min_free_mb=%s)",
                free_mb_for_path(cfg["output_base"]),
                cfg["min_free_mb"],
            )
            return

        now_dt = datetime.now()
        date_dir = os.path.join(cfg["output_base"], now_dt.strftime("%Y-%m-%d"))
        os.makedirs(date_dir, exist_ok=True)
        filename = now_dt.strftime("%H%M%S_%f") + ".jpg"
        filepath = os.path.join(date_dir, filename)

        capture_high_quality_still(picam2, still_config, preview_config, filepath, cfg, log=log)

        image_rel = os.path.relpath(filepath, cfg["output_base"]).replace("\\", "/")
        quality = apply_quality_gate(cfg, filepath, image_rel=image_rel)
        if not quality.get("passed", True):
            log.info("Quality gate %s for %s (%s)", quality.get("action"), filepath, "; ".join(quality.get("reasons", [])))
            pollinator_live.note("skip_quality")  # POLLINATOR_COUNTERS
            background = None
            motion_streak = 0
            return

        background = None
        motion_streak = 0
        last_capture = time.time()
        if preview_frame is not None:
            last_capture_hash = frame_dhash(preview_frame)
        pollinator_live.note("captured")  # POLLINATOR_COUNTERS
        log.info("Captured %s", filepath)

    def save_debug_frame(frame, motion_pixels):
        debug_dir = os.path.join(cfg["output_base"], cfg["debug_dirname"])
        out = frame.copy()
        for rx1, ry1, rx2, ry2 in motion_rois:
            cv2.rectangle(out, (rx1, ry1), (rx2, ry2), (0, 255, 0), 2)
        now_dt = datetime.now()
        label = now_dt.strftime("%Y-%m-%d %H:%M:%S") + f" motion_pixels={motion_pixels}"
        cv2.putText(out, label, (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1, cv2.LINE_AA)
        path = os.path.join(debug_dir, now_dt.strftime("%Y%m%d_%H%M%S") + ".jpg")
        cv2.imwrite(path, cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
        removed = prune_old_files(debug_dir, cfg["debug_max_files"])
        if removed:
            log.info("Pruned %s old debug preview(s)", removed)
        log.debug("Debug frame saved to %s", path)

    try:
        while not _shutdown:
            now = time.time()
            if _reload_requested:
                _reload_requested = False
                maybe_reload_config(force=True)
            elif cfg["config_reload_seconds"] > 0 and (now - last_config_check) >= cfg["config_reload_seconds"]:
                maybe_reload_config()
                last_config_check = now

            action = _take_request()
            if action == "autofocus":
                lens, sharp, _t = run_autofocus_sweep(picam2, cfg, motion_rois, log)
                if lens is not None:
                    log.info("Autofocus: lens_position=%.2f (~%.0f cm) sharpness=%.0f",
                             lens, 100.0 / lens if lens > 0.05 else float("inf"), sharp)
                    _write_device_key(config_path, "lens_position", round(float(lens), 2))
                    maybe_reload_config(force=True)
                background = None
                motion_streak = 0
                continue
            if action in ("set-reference", "reset-background"):
                log.info("Reference frame reset by request")
                background = None
                motion_streak = 0
                continue

            if not is_within_active_hours(cfg):
                # Keep publishing and keep feeding the watchdog. Going silent
                # for twelve hours makes systemd kill us every WatchdogSec all
                # night, and the Live tab goes dark exactly when you are trying
                # to check the aim.
                pollinator_live.note("skip_hours")  # POLLINATOR_COUNTERS
                try:
                    pollinator_live.publish(
                        picam2.capture_array(), 0, cfg,
                        rois=motion_rois, threshold=cfg["motion_pixels"],
                    )
                except Exception as exc:
                    log.debug("Idle publish failed: %s", exc)
                pollinator_live.heartbeat()  # POLLINATOR_STABILITY
                background = None
                time.sleep(max(1.0, cfg["frame_sleep_seconds"]))
                continue

            frame = picam2.capture_array()
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)

            if background is None:
                background = gray.copy().astype("float")
                time.sleep(0.5)
                continue

            _work, background, motion_pixels = count_motion_pixels(
                gray, background, cfg, rois=motion_rois or None
            )

            pollinator_live.publish(  # POLLINATOR_LIVE
                frame, motion_pixels, cfg,
                rois=motion_rois, threshold=cfg["motion_pixels"],
            )


            pollinator_live.heartbeat()  # POLLINATOR_STABILITY

            # A capture switches sensor modes; AE re-converges on the way back
            # and the whole watched area reads as changed. That self-triggers
            # the next capture. Nothing that fills the frame is an insect.
            if motion_rois:
                watched = sum((x2 - x1) * (y2 - y1) for x1, y1, x2, y2 in motion_rois)
            else:
                watched = cfg["lowres_width"] * cfg["lowres_height"]
            if watched and motion_pixels > 0.35 * watched:
                pollinator_live.note("skip_global")
                background = None
                motion_streak = 0
                time.sleep(cfg["frame_sleep_seconds"])
                continue

            if cfg["debug_enabled"] and (now - last_debug) >= cfg["debug_interval_seconds"]:
                save_debug_frame(frame, motion_pixels)
                last_debug = now

            if motion_pixels > cfg["motion_pixels"]:
                motion_streak += 1
                if motion_streak == 1:
                    log.info(
                        "Motion above threshold: motion_pixels=%s (need %s for %s frame(s))",
                        motion_pixels,
                        cfg["motion_pixels"],
                        cfg["motion_confirm_frames"],
                    )
            else:
                motion_streak = 0

            if (
                motion_streak >= cfg["motion_confirm_frames"]
                and (now - last_capture) > cfg["cooldown_seconds"]
            ):
                log.info("Motion trigger: motion_pixels=%s", motion_pixels)
                pollinator_live.note("trigger")  # POLLINATOR_COUNTERS
                capture_still(preview_frame=frame)

            time.sleep(cfg["frame_sleep_seconds"])

    except KeyboardInterrupt:
        pass
    finally:
        picam2.stop()
        log.info("Camera stopped")


if __name__ == "__main__":
    main()
