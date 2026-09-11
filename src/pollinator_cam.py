#!/usr/bin/env python3
import signal
import sys
import time
import logging
import os
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
            mtime = os.path.getmtime(config_path)
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

            if not is_within_active_hours(cfg):
                pollinator_live.note("skip_hours")  # POLLINATOR_COUNTERS
                time.sleep(cfg["frame_sleep_seconds"])
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
