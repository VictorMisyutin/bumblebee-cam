#!/usr/bin/env python3
"""Shared utilities for pollinator camera scripts."""

import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
JSON_SIDECAR_SUFFIX = ".annotations.json"
CAPTURE_META_SUFFIX = ".capture.json"
VALID_CLASSES = ["pollinator", "flower"]
CLASS_TO_ID = {name: idx for idx, name in enumerate(VALID_CLASSES)}
MARK_LIST_FILES = {
    "marked_for_deletion.txt",
    "marked_for_review.txt",
    "hard_negatives.txt",
}

DEFAULT_CONFIG = {
    "output_base": "/home/srs57/Pollinator_images",
    "lowres_width": 320,
    "lowres_height": 240,
    "still_width": 1920,
    "still_height": 1080,
    "motion_threshold": 25,
    "motion_pixels": 600,
    "motion_confirm_frames": 2,
    "cooldown_seconds": 5,
    "roi_enabled": False,
    "roi_x1": 100,
    "roi_y1": 60,
    "roi_x2": 220,
    "roi_y2": 180,
    "motion_rois": [],
    "preview_dirname": "setup_previews",
    "warmup_seconds": 2,
    "debug_enabled": True,
    "debug_interval_seconds": 300,
    "debug_dirname": "debug_previews",
    "debug_max_files": 200,
    "background_alpha": 0.05,
    "frame_sleep_seconds": 0.2,
    "post_capture_resume_seconds": 0.2,
    "min_free_mb": 500,
    "config_reload_seconds": 60,
    "detector_enabled": False,
    "detector_model_path": "/home/srs57/Models/pollinator_flower.pt",
    "detector_confidence": 0.45,
    "detector_pollinator_only": True,
    "detector_confirm_frames": 2,
    "detector_infer_every_n_frames": 1,
    "detector_imgsz": 320,
    "detector_save_metadata": True,
    "device_id": "bumblebee001",
    "active_hours_enabled": False,
    "active_hours_start": "07:00",
    "active_hours_end": "19:00",
    "detector_review_enabled": True,
    "detector_review_confidence_low": 0.35,
    "detector_review_confidence_high": 0.55,
    "hard_negative_tracking_enabled": True,
    "hard_negative_save_preview": False,
    "hard_negative_preview_dirname": "hard_negative_previews",
    "hard_negative_preview_max_files": 100,
    "quality_gate_enabled": False,
    "quality_min_blur_variance": 80.0,
    "quality_min_brightness": 25.0,
    "quality_max_brightness": 245.0,
    "quality_gate_action": "skip",
    "duplicate_suppression_enabled": True,
    "duplicate_hash_threshold": 5,
    "detector_backend": "auto",
    "autofocus_enabled": True,
    "autofocus_cycle_before_capture": True,
    "autofocus_mode": "auto",
    "autofocus_range": "normal",
    "autofocus_use_roi_window": True,
    "autofocus_window_shrink": 0.35,
    "autofocus_single_window": True,
    "autofocus_lock_after_cycle": True,
    "autofocus_continuous_preview": False,
    "autofocus_reset_before_cycle": True,
    "autofocus_start_lens_position": None,
    "lens_position": None,
    "still_jpeg_quality": 95,
    "still_settle_seconds": 1.2,
    "still_sharpness": 1.5,
    "still_contrast": 1.1,
    "still_noise_reduction": "off",
    "still_ae_enable": True,
    "still_exposure_time_us": None,
    "still_analogue_gain": None,
    "still_max_exposure_us": None,
    "still_unsharp_amount": 0.0,
    "still_unsharp_radius": 1.0,
}

_INT_KEYS = {
    "lowres_width", "lowres_height", "still_width", "still_height",
    "motion_threshold", "motion_pixels", "motion_confirm_frames",
    "cooldown_seconds", "roi_x1", "roi_y1", "roi_x2", "roi_y2",
    "warmup_seconds", "debug_interval_seconds", "debug_max_files",
    "min_free_mb", "config_reload_seconds",
    "detector_confirm_frames", "detector_infer_every_n_frames", "detector_imgsz",
    "hard_negative_preview_max_files", "duplicate_hash_threshold",
    "still_jpeg_quality",
}
_FLOAT_KEYS = {
    "background_alpha", "frame_sleep_seconds", "post_capture_resume_seconds",
    "detector_confidence", "detector_review_confidence_low", "detector_review_confidence_high",
    "quality_min_blur_variance", "quality_min_brightness", "quality_max_brightness",
    "still_settle_seconds", "still_sharpness", "autofocus_window_shrink",
    "still_contrast", "still_unsharp_amount", "still_unsharp_radius",
}
_BOOL_KEYS = {
    "roi_enabled", "debug_enabled", "detector_enabled", "detector_pollinator_only",
    "detector_save_metadata", "active_hours_enabled", "detector_review_enabled",
    "hard_negative_tracking_enabled", "hard_negative_save_preview",
    "quality_gate_enabled", "duplicate_suppression_enabled",
    "autofocus_enabled", "autofocus_cycle_before_capture",
    "autofocus_use_roi_window", "autofocus_single_window",
    "autofocus_lock_after_cycle", "still_ae_enable",
    "autofocus_continuous_preview", "autofocus_reset_before_cycle",
}
_STR_KEYS = {
    "output_base", "preview_dirname", "debug_dirname", "detector_model_path",
    "device_id", "active_hours_start", "active_hours_end", "hard_negative_preview_dirname",
    "quality_gate_action", "detector_backend",
    "autofocus_mode", "autofocus_range",
    "still_noise_reduction",
}


def setup_logging(name="pollinator", level=logging.INFO):
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(name)


def camera_supports_autofocus(picam2):
    try:
        return "AfMode" in getattr(picam2, "camera_controls", {})
    except Exception:
        return False


def enable_continuous_autofocus(picam2, log=None, cfg=None):
    """Enable continuous AF, preferably metered on the motion ROI window(s)."""
    logger = log or logging.getLogger("pollinator")
    cfg = cfg or {}
    if not camera_supports_autofocus(picam2):
        logger.debug("Camera does not support AfMode; leaving focus unchanged")
        return False
    if not cfg.get("autofocus_continuous_preview", False):
        # Continuous AF often grabs the closest high-contrast object (leaves, cage).
        # Prefer a single still-mode AF cycle on the flower ROI instead.
        return park_autofocus_for_preview(picam2, cfg, log=logger)
    try:
        from libcamera import controls

        controls_to_set = {"AfMode": controls.AfModeEnum.Continuous}
        _maybe_add_af_range(controls_to_set, controls, cfg)
        _maybe_add_af_windows(controls_to_set, controls, picam2, cfg, log=logger)
        picam2.set_controls(controls_to_set)
        logger.debug("Continuous autofocus enabled (%s)", controls_to_set)
        return True
    except Exception as exc:
        logger.warning("Could not enable continuous autofocus: %s", exc)
        return False


def _lens_position_limits(picam2):
    available = getattr(picam2, "camera_controls", {}) or {}
    limits = available.get("LensPosition")
    if not limits:
        return None
    try:
        if hasattr(limits, "min") and hasattr(limits, "max"):
            return float(limits.min), float(limits.max)
        if isinstance(limits, (list, tuple)) and len(limits) >= 2:
            return float(limits[0]), float(limits[1])
    except Exception:
        return None
    return None


def neutral_lens_position(picam2, cfg):
    """A mid-range start focus so AF does not begin stuck on the closest object."""
    configured = cfg.get("autofocus_start_lens_position", None)
    if configured is not None and configured != "":
        return float(configured)
    # Locked manual value is a good park position when available.
    locked = cfg.get("lens_position", None)
    if locked is not None and locked != "":
        return float(locked)
    limits = _lens_position_limits(picam2)
    if limits is not None:
        lo, hi = limits
        # Bias slightly toward near-mid (flowers), not the close extreme.
        return lo + 0.35 * (hi - lo)
    return 4.0


def park_autofocus_for_preview(picam2, cfg, log=None):
    """Hold a fixed lens position during preview (no continuous hunting)."""
    logger = log or logging.getLogger("pollinator")
    if not camera_supports_autofocus(picam2):
        return False
    try:
        from libcamera import controls

        lens = neutral_lens_position(picam2, cfg)
        picam2.set_controls({
            "AfMode": controls.AfModeEnum.Manual,
            "LensPosition": float(lens),
        })
        logger.info("Preview focus parked at LensPosition=%s (continuous AF off)", lens)
        return True
    except Exception as exc:
        logger.warning("Could not park preview focus: %s", exc)
        return False


def _scaler_crop_maximum(picam2):
    props = getattr(picam2, "camera_properties", {}) or {}
    crop = props.get("ScalerCropMaximum")
    if crop is None:
        # Fallback: full pixel array size as (0, 0, w, h).
        size = props.get("PixelArraySize")
        if size is not None:
            if hasattr(size, "width"):
                return 0, 0, int(size.width), int(size.height)
            if isinstance(size, (list, tuple)) and len(size) >= 2:
                return 0, 0, int(size[0]), int(size[1])
        return None
    if hasattr(crop, "x") and hasattr(crop, "width"):
        return int(crop.x), int(crop.y), int(crop.width), int(crop.height)
    if isinstance(crop, (list, tuple)) and len(crop) >= 4:
        return int(crop[0]), int(crop[1]), int(crop[2]), int(crop[3])
    return None


def _shrink_xyxy(x1, y1, x2, y2, shrink):
    """Shrink a box toward its center. shrink in (0, 1]; 1.0 = unchanged."""
    shrink = max(0.15, min(1.0, float(shrink)))
    if shrink >= 0.999:
        return int(x1), int(y1), int(x2), int(y2)
    cx = 0.5 * (x1 + x2)
    cy = 0.5 * (y1 + y2)
    hw = 0.5 * (x2 - x1) * shrink
    hh = 0.5 * (y2 - y1) * shrink
    return int(round(cx - hw)), int(round(cy - hh)), int(round(cx + hw)), int(round(cy + hh))


def af_windows_from_rois(picam2, cfg):
    """Build AfWindows rectangles in ScalerCropMaximum / full-sensor coordinates.

    Low-res motion ROIs are mapped with the same aspect-aware transform used for
    still overlays, so a 4:3 preview ROI lands correctly on a 16:9 sensor.
    """
    crop = _scaler_crop_maximum(picam2)
    if crop is None:
        return None
    sx, sy, sw, sh = crop
    if sw <= 0 or sh <= 0:
        return None

    shrink = float(cfg.get("autofocus_window_shrink", 0.35))
    rois = get_motion_rois(cfg)
    if not rois:
        # Center ~30% window when ROI is disabled.
        cx, cy = sw // 2, sh // 2
        aw, ah = max(32, int(sw * 0.3)), max(32, int(sh * 0.3))
        return [(sx + cx - aw // 2, sy + cy - ah // 2, aw, ah)]

    # Map low-res ROIs onto the sensor crop size, accounting for aspect mismatch.
    mapped = map_rois_to_frame(cfg, rois, sw, sh)
    windows = []
    for mx1, my1, mx2, my2 in mapped:
        mx1, my1, mx2, my2 = _shrink_xyxy(mx1, my1, mx2, my2, shrink)
        aw = max(32, mx2 - mx1)
        ah = max(32, my2 - my1)
        ax = max(0, min(mx1, sw - aw))
        ay = max(0, min(my1, sh - ah))
        aw = min(aw, sw - ax)
        ah = min(ah, sh - ay)
        if aw >= 32 and ah >= 32:
            windows.append((sx + ax, sy + ay, aw, ah))

    if not windows:
        return None

    # Many IPA builds effectively use one AF window; prefer the largest ROI.
    if cfg.get("autofocus_single_window", True) and len(windows) > 1:
        windows = [max(windows, key=lambda w: w[2] * w[3])]
    return windows


def af_window_rect_from_roi(picam2, cfg):
    """Backward-compatible single AF window (largest / only ROI)."""
    windows = af_windows_from_rois(picam2, cfg)
    if not windows:
        return None
    return windows[0]


def _maybe_add_af_range(controls_to_set, controls_mod, cfg):
    range_name = str(cfg.get("autofocus_range", "normal")).strip().lower()
    range_enum = getattr(controls_mod, "AfRangeEnum", None)
    if range_enum is None:
        return
    mapping = {
        "normal": getattr(range_enum, "Normal", None),
        "macro": getattr(range_enum, "Macro", None),
        "full": getattr(range_enum, "Full", None),
    }
    value = mapping.get(range_name) or mapping.get("normal")
    if value is not None:
        controls_to_set["AfRange"] = value


def _maybe_add_af_windows(controls_to_set, controls_mod, picam2, cfg, log=None):
    logger = log or logging.getLogger("pollinator")
    if not cfg.get("autofocus_use_roi_window", True):
        return
    available = getattr(picam2, "camera_controls", {}) or {}
    if "AfWindows" not in available:
        logger.debug("Camera does not expose AfWindows")
        return
    windows = af_windows_from_rois(picam2, cfg)
    if not windows:
        return
    metering = getattr(controls_mod, "AfMeteringEnum", None)
    if metering is not None and hasattr(metering, "Windows"):
        controls_to_set["AfMetering"] = metering.Windows
    controls_to_set["AfWindows"] = windows
    logger.info("AF window(s) set to %s", windows)


def apply_autofocus_for_still(picam2, cfg, log=None):
    """Focus for a still: ROI window + AF cycle, then optionally lock LensPosition."""
    logger = log or logging.getLogger("pollinator")
    if not cfg.get("autofocus_enabled", True):
        return False
    if not camera_supports_autofocus(picam2):
        return False

    try:
        from libcamera import controls
    except Exception as exc:
        logger.warning("libcamera controls unavailable: %s", exc)
        return False

    mode = str(cfg.get("autofocus_mode", "auto")).strip().lower()

    # Manual lock: best for a fixed flower distance once calibrated.
    if mode == "manual":
        lens = cfg.get("lens_position")
        if lens is None:
            logger.warning("autofocus_mode=manual but lens_position is not set")
            return False
        try:
            picam2.set_controls({
                "AfMode": controls.AfModeEnum.Manual,
                "LensPosition": float(lens),
            })
            time.sleep(0.3)
            logger.info("Manual lens position set to %s", lens)
            return True
        except Exception as exc:
            logger.warning("Manual lens position failed: %s", exc)
            return False

    if not cfg.get("autofocus_cycle_before_capture", True):
        return enable_continuous_autofocus(picam2, log=logger, cfg=cfg)

    # Break out of "closest object" continuous/macro hunting before the cycle.
    if cfg.get("autofocus_reset_before_cycle", True):
        try:
            start_lens = neutral_lens_position(picam2, cfg)
            picam2.set_controls({
                "AfMode": controls.AfModeEnum.Manual,
                "LensPosition": float(start_lens),
            })
            time.sleep(0.25)
            logger.info("Reset lens to %s before ROI AF cycle", start_lens)
        except Exception as exc:
            logger.debug("Lens reset before AF cycle skipped: %s", exc)

    controls_to_set = {}
    _maybe_add_af_range(controls_to_set, controls, cfg)
    _maybe_add_af_windows(controls_to_set, controls, picam2, cfg, log=logger)
    # Prefer a full scan from the reset position over continuous micro-adjust.
    speed = getattr(controls, "AfSpeedEnum", None)
    if speed is not None and hasattr(speed, "Fast"):
        controls_to_set["AfSpeed"] = speed.Fast
    controls_to_set["AfMode"] = controls.AfModeEnum.Auto
    try:
        picam2.set_controls(controls_to_set)
    except Exception as exc:
        logger.warning("Could not set AF controls (%s); trying autofocus_cycle alone", exc)

    try:
        ok = picam2.autofocus_cycle()
        meta = {}
        try:
            meta = picam2.capture_metadata() or {}
        except Exception:
            pass
        lens = meta.get("LensPosition")
        logger.info(
            "Still-mode autofocus cycle: %s (LensPosition=%s)",
            "ok" if ok else "failed",
            lens,
        )
        # Lock focus so continuous AF cannot pull away during settle/capture.
        if (
            ok
            and lens is not None
            and cfg.get("autofocus_lock_after_cycle", True)
        ):
            try:
                picam2.set_controls({
                    "AfMode": controls.AfModeEnum.Manual,
                    "LensPosition": float(lens),
                })
                logger.info("Locked focus at LensPosition=%s for still", lens)
            except Exception as lock_exc:
                logger.warning("Could not lock focus after AF cycle: %s", lock_exc)
        return bool(ok)
    except Exception as exc:
        logger.warning("Still-mode autofocus cycle failed (%s)", exc)
        return False

def apply_still_image_controls(picam2, cfg, log=None):
    """Apply JPEG quality and still-mode image/exposure controls (setup + events)."""
    logger = log or logging.getLogger("pollinator")
    quality = int(cfg.get("still_jpeg_quality", 95))
    quality = max(1, min(95, quality))
    try:
        picam2.options["quality"] = quality
    except Exception as exc:
        logger.debug("Could not set JPEG quality: %s", exc)

    controls_to_set = {}
    try:
        available = getattr(picam2, "camera_controls", {}) or {}
        sharpness = float(cfg.get("still_sharpness", 1.5))
        if "Sharpness" in available:
            controls_to_set["Sharpness"] = sharpness

        contrast = float(cfg.get("still_contrast", 1.1))
        if "Contrast" in available:
            controls_to_set["Contrast"] = contrast

        exp = cfg.get("still_exposure_time_us", None)
        gain = cfg.get("still_analogue_gain", None)
        ae_enable = bool(cfg.get("still_ae_enable", True))
        max_exp = cfg.get("still_max_exposure_us", None)
        if exp is not None or gain is not None:
            # Manual exposure path shared by setup capture and event stills.
            if "AeEnable" in available:
                controls_to_set["AeEnable"] = False
            if exp is not None and "ExposureTime" in available:
                controls_to_set["ExposureTime"] = int(exp)
            if gain is not None and "AnalogueGain" in available:
                controls_to_set["AnalogueGain"] = float(gain)
        else:
            if "AeEnable" in available:
                controls_to_set["AeEnable"] = ae_enable
            # Cap shutter while keeping AE (reduces bee/wind motion blur).
            if max_exp is not None and "FrameDurationLimits" in available:
                controls_to_set["FrameDurationLimits"] = (100, int(max_exp))

        try:
            from libcamera import controls

            nr_enum = getattr(controls, "NoiseReductionModeEnum", None) or getattr(
                getattr(controls, "draft", None), "NoiseReductionModeEnum", None
            )
            if nr_enum is not None and "NoiseReductionMode" in available:
                nr_name = str(cfg.get("still_noise_reduction", "off")).strip().lower()
                nr_map = {
                    "off": getattr(nr_enum, "Off", None),
                    "minimal": getattr(nr_enum, "Minimal", None),
                    "fast": getattr(nr_enum, "Fast", None),
                    "high_quality": getattr(nr_enum, "HighQuality", None),
                    "hq": getattr(nr_enum, "HighQuality", None),
                }
                nr_value = nr_map.get(nr_name)
                if nr_value is not None:
                    controls_to_set["NoiseReductionMode"] = nr_value
        except Exception:
            pass
        if controls_to_set:
            picam2.set_controls(controls_to_set)
            logger.info("Still image controls: %s", controls_to_set)
    except Exception as exc:
        logger.debug("Could not apply still image controls: %s", exc)


def apply_still_unsharp_mask(filepath, cfg, log=None):
    """Optional light post-capture sharpening (0 = disabled)."""
    logger = log or logging.getLogger("pollinator")
    amount = float(cfg.get("still_unsharp_amount", 0.0) or 0.0)
    if amount <= 0:
        return False
    radius = max(0.3, float(cfg.get("still_unsharp_radius", 1.0) or 1.0))
    try:
        import cv2

        img = cv2.imread(filepath)
        if img is None:
            return False
        blurred = cv2.GaussianBlur(img, (0, 0), radius)
        sharp = cv2.addWeighted(img, 1.0 + amount, blurred, -amount, 0)
        quality = int(cfg.get("still_jpeg_quality", 95))
        cv2.imwrite(
            filepath,
            sharp,
            [int(cv2.IMWRITE_JPEG_QUALITY), max(1, min(100, quality))],
        )
        logger.info("Applied unsharp mask amount=%.2f radius=%.2f", amount, radius)
        return True
    except Exception as exc:
        logger.warning("Unsharp mask failed: %s", exc)
        return False


def build_still_configuration(picam2, cfg):
    """Create a still configuration sized from config."""
    still_w = int(cfg.get("still_width", 1920))
    still_h = int(cfg.get("still_height", 1080))
    return picam2.create_still_configuration(
        main={"size": (still_w, still_h)},
        sensor={"output_size": (2304, 1296), "bit_depth": 10},
        buffer_count=2,
    )

def capture_high_quality_still(picam2, still_config, preview_config, filepath, cfg, log=None):
    """Switch to still mode, AF on ROI, lock focus, then save a JPEG."""
    logger = log or logging.getLogger("pollinator")
    apply_still_image_controls(picam2, cfg, log=logger)
    settle = max(0.0, float(cfg.get("still_settle_seconds", 1.2)))

    requested = still_config.get("main", {}).get("size") if isinstance(still_config, dict) else None
    logger.info(
        "Still capture start requested_size=%s jpeg_quality=%s",
        requested,
        cfg.get("still_jpeg_quality", 95),
    )

    picam2.switch_mode(still_config)
    # Brief settle so still-mode AE/AF controls are live, then focus on ROI.
    if settle:
        time.sleep(min(0.4, settle))

    apply_autofocus_for_still(picam2, cfg, log=logger)
    # Extra settle after focus lock so exposure can finish before the shutter.
    if settle:
        time.sleep(settle)

    try:
        picam2.capture_file(filepath, wait=True, queue=False)
    except TypeError:
        try:
            picam2.capture_file(filepath, queue=False)
        except TypeError:
            picam2.capture_file(filepath)

    try:
        meta = picam2.capture_metadata() or {}
        logger.info(
            "Still exposure metadata ExposureTime=%s AnalogueGain=%s LensPosition=%s",
            meta.get("ExposureTime"),
            meta.get("AnalogueGain"),
            meta.get("LensPosition"),
        )
    except Exception:
        pass

    apply_still_unsharp_mask(filepath, cfg, log=logger)

    try:
        size_bytes = os.path.getsize(filepath)
        # Prefer Pillow if present; fall back to OpenCV.
        width = height = None
        try:
            from PIL import Image

            with Image.open(filepath) as im:
                width, height = im.size
        except Exception:
            try:
                import cv2

                img = cv2.imread(filepath)
                if img is not None:
                    height, width = img.shape[:2]
            except Exception:
                pass
        logger.info(
            "Still saved %s (%s bytes%s)",
            filepath,
            size_bytes,
            f", {width}x{height}" if width and height else "",
        )
        if width and height:
            cfg_w = int(cfg.get("still_width", 0))
            cfg_h = int(cfg.get("still_height", 0))
            if cfg_w and cfg_h and (width < cfg_w * 0.9 or height < cfg_h * 0.9):
                logger.warning(
                    "Saved still is %sx%s but config asks for %sx%s — sensor may have picked a lower mode",
                    width,
                    height,
                    cfg_w,
                    cfg_h,
                )
    except Exception as exc:
        logger.debug("Could not inspect saved still: %s", exc)

    resume_preview_with_autofocus(picam2, preview_config, cfg, log=logger)


def resume_preview_with_autofocus(picam2, preview_config, cfg, log=None):
    """Return to preview after a still and restore continuous AF if enabled."""
    try:
        picam2.switch_mode(preview_config)
    except Exception:
        picam2.stop()
        picam2.configure(preview_config)
        picam2.start()
    time.sleep(cfg.get("post_capture_resume_seconds", 0.2))
    mode = str(cfg.get("autofocus_mode", "auto")).strip().lower()
    if cfg.get("autofocus_enabled", True) and mode != "manual":
        enable_continuous_autofocus(picam2, log=log, cfg=cfg)
    elif cfg.get("autofocus_enabled", True) and mode == "manual" and cfg.get("lens_position") is not None:
        try:
            from libcamera import controls

            picam2.set_controls({
                "AfMode": controls.AfModeEnum.Manual,
                "LensPosition": float(cfg["lens_position"]),
            })
        except Exception:
            pass


def autofocus_before_still(picam2, cfg, log=None):
    """Deprecated helper — prefer capture_high_quality_still()."""
    return False


def load_config(path):
    # POLLINATOR_LAYERED: a directory merges baseline.json then device.json,
    # so fleet-wide policy and per-camera calibration stay in separate files.
    if os.path.isdir(path):
        cfg = DEFAULT_CONFIG.copy()
        for name in ("baseline.json", "device.json"):
            fp = os.path.join(path, name)
            if os.path.exists(fp):
                with open(fp, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f))
        if "device_id" not in cfg and cfg.get("site_id"):
            cfg["device_id"] = str(cfg["site_id"]).strip()
        return validate_config(cfg)
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            file_cfg = json.load(f)
        if "device_id" not in file_cfg and file_cfg.get("site_id"):
            file_cfg = dict(file_cfg)
            file_cfg["device_id"] = str(file_cfg["site_id"]).strip()
        cfg.update(file_cfg)
    return validate_config(cfg)


def save_config(path, cfg):
    # POLLINATOR_LAYERED_SAVE: when path is a directory, write ONLY the keys
    # that differ from baseline.json into device.json. Fleet policy stays in
    # baseline.json where it can be version-controlled and pushed to all units;
    # device.json keeps just this camera's physical calibration.
    validated = validate_config(cfg)
    if os.path.isdir(path):
        base = DEFAULT_CONFIG.copy()
        base_path = os.path.join(path, "baseline.json")
        if os.path.exists(base_path):
            with open(base_path, "r", encoding="utf-8") as f:
                base.update(json.load(f))

        dev_path = os.path.join(path, "device.json")
        existing = {}
        if os.path.exists(dev_path):
            with open(dev_path, "r", encoding="utf-8") as f:
                existing = json.load(f)

        # Preserve human annotations (_site, _plant, _calibrated, ...).
        overlay = {k: v for k, v in existing.items() if k.startswith("_")}
        for key, value in validated.items():
            if key.startswith("_"):
                continue
            if key not in base or base[key] != value:
                overlay[key] = value
        # device_id is always per-camera, even if it somehow matches baseline.
        if validated.get("device_id"):
            overlay["device_id"] = validated["device_id"]

        tmp = dev_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(overlay, f, indent=2)
            f.write("\n")
        os.replace(tmp, dev_path)
        return

    with open(path, "w", encoding="utf-8") as f:
        json.dump(validated, f, indent=2)
        f.write("\n")


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def validate_config(cfg):
    out = DEFAULT_CONFIG.copy()
    out.update(cfg)

    for key in _INT_KEYS:
        out[key] = int(out[key])
    for key in _FLOAT_KEYS:
        out[key] = float(out[key])
    for key in _BOOL_KEYS:
        out[key] = _coerce_bool(out[key])
    for key in _STR_KEYS:
        out[key] = str(out[key])

    if out["lowres_width"] < 32 or out["lowres_height"] < 32:
        raise ValueError("lowres_width and lowres_height must be >= 32")
    if out["still_width"] < 320 or out["still_height"] < 240:
        raise ValueError("still_width and still_height are too small")
    if out["motion_confirm_frames"] < 1:
        raise ValueError("motion_confirm_frames must be >= 1")
    if out["min_free_mb"] < 0:
        raise ValueError("min_free_mb must be >= 0")
    if out["config_reload_seconds"] < 0:
        raise ValueError("config_reload_seconds must be >= 0")
    if out["detector_confirm_frames"] < 1:
        raise ValueError("detector_confirm_frames must be >= 1")
    if out["detector_infer_every_n_frames"] < 1:
        raise ValueError("detector_infer_every_n_frames must be >= 1")
    if out["detector_imgsz"] < 32:
        raise ValueError("detector_imgsz must be >= 32")
    if not 0.0 <= out["detector_confidence"] <= 1.0:
        raise ValueError("detector_confidence must be between 0 and 1")
    for key in ("detector_review_confidence_low", "detector_review_confidence_high"):
        if not 0.0 <= out[key] <= 1.0:
            raise ValueError(f"{key} must be between 0 and 1")
    _parse_hhmm(out["active_hours_start"])
    _parse_hhmm(out["active_hours_end"])
    if out["quality_gate_action"] not in {"skip", "flag"}:
        raise ValueError("quality_gate_action must be 'skip' or 'flag'")
    if out["detector_backend"] not in {"auto", "pt", "onnx"}:
        raise ValueError("detector_backend must be auto, pt, or onnx")
    if out["duplicate_hash_threshold"] < 0:
        raise ValueError("duplicate_hash_threshold must be >= 0")
    if not 1 <= out["still_jpeg_quality"] <= 95:
        raise ValueError("still_jpeg_quality must be between 1 and 95")
    if out["still_settle_seconds"] < 0:
        raise ValueError("still_settle_seconds must be >= 0")
    if not 0.15 <= out["autofocus_window_shrink"] <= 1.0:
        raise ValueError("autofocus_window_shrink must be between 0.15 and 1.0")
    if out["still_unsharp_amount"] < 0:
        raise ValueError("still_unsharp_amount must be >= 0")
    if out["still_unsharp_radius"] <= 0:
        raise ValueError("still_unsharp_radius must be > 0")

    nr = str(out.get("still_noise_reduction", "off")).strip().lower()
    if nr not in {"off", "minimal", "fast", "high_quality", "hq"}:
        raise ValueError("still_noise_reduction must be off, minimal, fast, or high_quality")
    out["still_noise_reduction"] = "high_quality" if nr == "hq" else nr

    mode = str(out.get("autofocus_mode", "auto")).strip().lower()
    if mode not in {"auto", "continuous", "manual"}:
        raise ValueError("autofocus_mode must be auto, continuous, or manual")
    out["autofocus_mode"] = mode

    af_range = str(out.get("autofocus_range", "macro")).strip().lower()
    if af_range not in {"normal", "macro", "full"}:
        raise ValueError("autofocus_range must be normal, macro, or full")
    out["autofocus_range"] = af_range

    lens = out.get("lens_position", None)
    if lens is None or lens == "" or str(lens).strip().lower() in {"null", "none"}:
        out["lens_position"] = None
    else:
        out["lens_position"] = float(lens)
    if mode == "manual" and out["lens_position"] is None:
        raise ValueError("autofocus_mode=manual requires lens_position")

    exp = out.get("still_exposure_time_us", None)
    if exp is None or exp == "" or str(exp).strip().lower() in {"null", "none"}:
        out["still_exposure_time_us"] = None
    else:
        out["still_exposure_time_us"] = int(exp)
        if out["still_exposure_time_us"] <= 0:
            raise ValueError("still_exposure_time_us must be > 0")

    gain = out.get("still_analogue_gain", None)
    if gain is None or gain == "" or str(gain).strip().lower() in {"null", "none"}:
        out["still_analogue_gain"] = None
    else:
        out["still_analogue_gain"] = float(gain)
        if out["still_analogue_gain"] <= 0:
            raise ValueError("still_analogue_gain must be > 0")

    max_exp = out.get("still_max_exposure_us", None)
    if max_exp is None or max_exp == "" or str(max_exp).strip().lower() in {"null", "none"}:
        out["still_max_exposure_us"] = None
    else:
        out["still_max_exposure_us"] = int(max_exp)
        if out["still_max_exposure_us"] < 100:
            raise ValueError("still_max_exposure_us must be >= 100")

    start_lens = out.get("autofocus_start_lens_position", None)
    if start_lens is None or start_lens == "" or str(start_lens).strip().lower() in {"null", "none"}:
        out["autofocus_start_lens_position"] = None
    else:
        out["autofocus_start_lens_position"] = float(start_lens)

    out["device_id"] = get_device_id(out)

    # Normalize multi-ROI list; seed from legacy roi_* if needed.
    rois = normalize_motion_rois_list(out, out.get("motion_rois"))
    if not rois and out.get("roi_enabled"):
        rois = [clamp_roi_corners(out)]
    out["motion_rois"] = [
        {"x1": x1, "y1": y1, "x2": x2, "y2": y2} for x1, y1, x2, y2 in rois
    ]
    if rois:
        out["roi_x1"], out["roi_y1"], out["roi_x2"], out["roi_y2"] = rois[0]

    return out


def get_device_id(cfg):
    """Return configured camera device id (legacy site_id key still accepted)."""
    device = str(cfg.get("device_id", "")).strip()
    if device:
        return device
    legacy = str(cfg.get("site_id", "")).strip()
    return legacy or "unknown-device"


def _parse_hhmm(value):
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time (expected HH:MM): {value}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time: {value}")
    return hour * 60 + minute


def is_within_active_hours(cfg, now=None):
    if not cfg.get("active_hours_enabled"):
        return True
    now = now or datetime.now()
    current = now.hour * 60 + now.minute
    start = _parse_hhmm(cfg["active_hours_start"])
    end = _parse_hhmm(cfg["active_hours_end"])
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def clamp_roi_corners(cfg):
    x1 = max(0, min(int(cfg["roi_x1"]), cfg["lowres_width"] - 1))
    x2 = max(1, min(int(cfg["roi_x2"]), cfg["lowres_width"]))
    y1 = max(0, min(int(cfg["roi_y1"]), cfg["lowres_height"] - 1))
    y2 = max(1, min(int(cfg["roi_y2"]), cfg["lowres_height"]))
    if x2 <= x1:
        x1, x2 = 0, cfg["lowres_width"]
    if y2 <= y1:
        y1, y2 = 0, cfg["lowres_height"]
    return x1, y1, x2, y2


def _clamp_corner_tuple(cfg, x1, y1, x2, y2):
    x1 = max(0, min(int(x1), cfg["lowres_width"] - 1))
    x2 = max(1, min(int(x2), cfg["lowres_width"]))
    y1 = max(0, min(int(y1), cfg["lowres_height"] - 1))
    y2 = max(1, min(int(y2), cfg["lowres_height"]))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def normalize_motion_rois_list(cfg, raw_rois):
    """Parse a list of ROI dicts into clamped (x1,y1,x2,y2) tuples."""
    rois = []
    for item in raw_rois or []:
        if not isinstance(item, dict):
            continue
        if all(k in item for k in ("x1", "y1", "x2", "y2")):
            corners = _clamp_corner_tuple(cfg, item["x1"], item["y1"], item["x2"], item["y2"])
        elif all(k in item for k in ("x", "y", "w", "h")):
            x, y, w, h = clamp_roi_xywh(cfg, item["x"], item["y"], item["w"], item["h"])
            corners = xywh_to_corners(x, y, w, h)
        else:
            continue
        if corners is not None and corners not in rois:
            rois.append(corners)
    return rois


def get_motion_rois(cfg):
    """Return enabled motion ROIs as a list of (x1, y1, x2, y2) in low-res coords.

    Empty list means full-frame motion (ROI disabled). Motion in any ROI counts.
    """
    if not cfg.get("roi_enabled"):
        return []
    rois = normalize_motion_rois_list(cfg, cfg.get("motion_rois"))
    if rois:
        return rois
    return [clamp_roi_corners(cfg)]


def set_motion_rois(cfg, rois, enabled=True):
    """Write motion ROI list into cfg and mirror the first ROI into legacy roi_* keys."""
    cleaned = []
    for item in rois or []:
        if isinstance(item, (list, tuple)) and len(item) == 4:
            corners = _clamp_corner_tuple(cfg, item[0], item[1], item[2], item[3])
        elif isinstance(item, dict):
            corners = normalize_motion_rois_list(cfg, [item])
            corners = corners[0] if corners else None
        else:
            corners = None
        if corners is not None and corners not in cleaned:
            cleaned.append(corners)

    cfg["motion_rois"] = [
        {"x1": x1, "y1": y1, "x2": x2, "y2": y2} for x1, y1, x2, y2 in cleaned
    ]
    cfg["roi_enabled"] = bool(enabled and cleaned)
    if cleaned:
        x1, y1, x2, y2 = cleaned[0]
        cfg["roi_x1"], cfg["roi_y1"], cfg["roi_x2"], cfg["roi_y2"] = x1, y1, x2, y2
    return cleaned


def union_roi_bounds(rois):
    """Bounding box covering all ROIs, or None if empty."""
    if not rois:
        return None
    return (
        min(r[0] for r in rois),
        min(r[1] for r in rois),
        max(r[2] for r in rois),
        max(r[3] for r in rois),
    )


def map_rois_to_frame(cfg, rois, frame_width, frame_height):
    """Map low-res ROI list onto a still/frame size for overlays."""
    prev_w = cfg["lowres_width"]
    prev_h = cfg["lowres_height"]
    if frame_width == prev_w and frame_height == prev_h:
        return [tuple(r) for r in rois]
    scale, offset_x, offset_y = preview_to_still_transform(cfg, frame_width, frame_height)

    def map_one(x1, y1, x2, y2):
        return (
            max(0, min(int(round(offset_x + x1 * scale)), frame_width - 1)),
            max(0, min(int(round(offset_y + y1 * scale)), frame_height - 1)),
            max(1, min(int(round(offset_x + x2 * scale)), frame_width)),
            max(1, min(int(round(offset_y + y2 * scale)), frame_height)),
        )

    return [map_one(*r) for r in rois]


def count_motion_pixels(gray, background, cfg, rois=None):
    """Update background model and count changed pixels (optionally masked to ROIs).

    Returns (work_frame, background, motion_pixels).
    If rois is empty/None, counts over the full frame.
    """
    import cv2

    work = gray
    if background is None:
        return work, None, 0

    cv2.accumulateWeighted(work, background, cfg["background_alpha"])
    frame_delta = cv2.absdiff(work, cv2.convertScaleAbs(background))
    thresh = cv2.threshold(frame_delta, cfg["motion_threshold"], 255, cv2.THRESH_BINARY)[1]
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=1)

    if rois:
        import numpy as np

        mask = np.zeros(thresh.shape, dtype=thresh.dtype)
        for x1, y1, x2, y2 in rois:
            mask[y1:y2, x1:x2] = 255
        thresh = cv2.bitwise_and(thresh, mask)

    return work, background, cv2.countNonZero(thresh)


def clamp_roi_xywh(cfg, x, y, w, h):
    x = max(0, min(int(x), cfg["lowres_width"] - 1))
    y = max(0, min(int(y), cfg["lowres_height"] - 1))
    w = int(w)
    h = int(h)
    if w <= 0 or h <= 0:
        raise ValueError("ROI width and height must be > 0")
    max_w = cfg["lowres_width"] - x
    max_h = cfg["lowres_height"] - y
    w = min(w, max_w)
    h = min(h, max_h)
    if w <= 0 or h <= 0:
        raise ValueError("ROI must fit within the low-resolution frame")
    return x, y, w, h


def preview_to_still_transform(cfg, frame_width, frame_height):
    """Return (scale, offset_x, offset_y) mapping low-res preview → still frame."""
    prev_w = cfg["lowres_width"]
    prev_h = cfg["lowres_height"]
    prev_aspect = prev_w / prev_h
    frame_aspect = frame_width / frame_height

    if abs(prev_aspect - frame_aspect) < 0.01:
        scale = frame_width / prev_w
        offset_x = 0.0
        offset_y = 0.0
    elif frame_aspect > prev_aspect:
        scale = frame_height / prev_h
        offset_x = (frame_width - prev_w * scale) / 2.0
        offset_y = 0.0
    else:
        scale = frame_width / prev_w
        offset_x = 0.0
        offset_y = (frame_height - prev_h * scale) / 2.0
    return scale, offset_x, offset_y


def still_xywh_to_lowres(cfg, x, y, w, h, frame_width=None, frame_height=None):
    """Convert ROI measured on a full-res still into low-res motion coordinates."""
    frame_width = int(frame_width or cfg.get("still_width", 1920))
    frame_height = int(frame_height or cfg.get("still_height", 1080))
    scale, offset_x, offset_y = preview_to_still_transform(cfg, frame_width, frame_height)
    if scale <= 0:
        raise ValueError("Invalid still/preview scale for ROI conversion")

    def unmap_x(val):
        return (float(val) - offset_x) / scale

    def unmap_y(val):
        return (float(val) - offset_y) / scale

    x1 = unmap_x(x)
    y1 = unmap_y(y)
    x2 = unmap_x(x + w)
    y2 = unmap_y(y + h)
    lx = int(round(x1))
    ly = int(round(y1))
    lw = int(round(x2 - x1))
    lh = int(round(y2 - y1))
    return clamp_roi_xywh(cfg, lx, ly, lw, lh)


def normalize_roi_xywh_input(cfg, x, y, w, h, coord_space="auto"):
    """Accept low-res or still-image ROI coords; always return low-res xywh.

    coord_space:
      - lowres: treat as motion-preview coordinates (320x240 by default)
      - still: treat as full-resolution still / setup-preview coordinates
      - auto: if the box extends past the low-res frame, treat as still coords
    """
    space = str(coord_space or "auto").strip().lower()
    if space not in {"auto", "lowres", "still"}:
        raise ValueError("coord_space must be auto, lowres, or still")

    x, y, w, h = int(x), int(y), int(w), int(h)
    low_w = int(cfg["lowres_width"])
    low_h = int(cfg["lowres_height"])

    looks_like_still = (
        x >= low_w or y >= low_h or (x + w) > low_w or (y + h) > low_h
    )
    if space == "still" or (space == "auto" and looks_like_still):
        return still_xywh_to_lowres(cfg, x, y, w, h), "still"
    return clamp_roi_xywh(cfg, x, y, w, h), "lowres"


def xywh_to_corners(x, y, w, h):
    return x, y, x + w, y + h


def corners_to_xywh(x1, y1, x2, y2):
    return x1, y1, x2 - x1, y2 - y1


def roi_bounds_for_frame(cfg, frame_width, frame_height):
    """Union bounding box of all motion ROIs mapped onto a frame, or None."""
    rois = get_motion_rois(cfg)
    if not rois:
        return None
    mapped = map_rois_to_frame(cfg, rois, frame_width, frame_height)
    return union_roi_bounds(mapped)


def motion_rois_for_frame(cfg, frame_width, frame_height):
    """All motion ROIs mapped onto a frame (empty if ROI disabled)."""
    rois = get_motion_rois(cfg)
    if not rois:
        return []
    return map_rois_to_frame(cfg, rois, frame_width, frame_height)


def is_image(path):
    return path.is_file() and path.suffix.lower() in IMAGE_EXTS


def normalize_relpath(path, root_dir):
    return str(path.relative_to(root_dir)).replace("\\", "/")


def safe_join_under_root(root_dir, rel):
    path = (root_dir / rel).resolve()
    path.relative_to(root_dir.resolve())
    return path


def image_label_path(image_path):
    return image_path.with_suffix(".txt")


def image_json_sidecar_path(image_path):
    return image_path.with_suffix(image_path.suffix + JSON_SIDECAR_SUFFIX)


def capture_metadata_path(image_path):
    return image_path.with_suffix(image_path.suffix + CAPTURE_META_SUFFIX)


def rel_under_base(base_dir, filepath):
    return os.path.relpath(filepath, base_dir).replace("\\", "/")


def append_unique_line(path, value):
    path = Path(path)
    existing = set()
    if path.exists():
        existing = {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()}
    if value in existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(value + "\n")
    return True


def append_jsonl(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def flag_for_review(output_base, image_rel, note=None):
    root = Path(output_base)
    append_unique_line(root / "marked_for_review.txt", image_rel)
    if note:
        notes_path = root / "marked_for_review_notes.json"
        notes = {}
        if notes_path.exists():
            try:
                notes = json.loads(notes_path.read_text(encoding="utf-8"))
            except Exception:
                notes = {}
        notes[image_rel] = note
        notes_path.write_text(json.dumps(notes, indent=2) + "\n", encoding="utf-8")


def flag_hard_negative(output_base, image_rel):
    append_unique_line(Path(output_base) / "hard_negatives.txt", image_rel)


def model_version_info(model_path):
    path = Path(model_path)
    if not path.exists():
        return {"model_path": str(path), "model_exists": False}
    stat = path.stat()
    return {
        "model_path": str(path.resolve()),
        "model_exists": True,
        "model_size_bytes": stat.st_size,
        "model_mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def classify_capture_review(cfg, match):
    if not cfg.get("detector_review_enabled") or not match:
        return None
    conf = float(match.get("confidence", 0))
    low = float(cfg["detector_review_confidence_low"])
    high = float(cfg["detector_review_confidence_high"])
    if low <= conf < high:
        return "review"
    return None


def device_backup_tag(cfg, hostname=None):
    """Return a stable backup path segment: device_id/hostname."""
    device = get_device_id(cfg)
    host = (hostname or os.uname().nodename).strip() or "unknown-host"
    return f"{device}/{host}"


def site_backup_tag(cfg, hostname=None):
    """Deprecated alias for device_backup_tag."""
    return device_backup_tag(cfg, hostname=hostname)


def load_capture_metadata(image_path):
    path = capture_metadata_path(Path(image_path))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def frame_dhash(frame_rgb, size=9):
    """Difference hash for duplicate capture detection."""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
    resized = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    diff = resized[:, 1:] > resized[:, :-1]
    return int("".join("1" if bit else "0" for bit in diff.flatten()), 2)


def hamming_distance(hash_a, hash_b):
    return (hash_a ^ hash_b).bit_count()


def is_duplicate_frame(cfg, frame_rgb, last_hash):
    if not cfg.get("duplicate_suppression_enabled") or last_hash is None:
        return False
    current = frame_dhash(frame_rgb)
    return hamming_distance(current, last_hash) <= int(cfg["duplicate_hash_threshold"])


def assess_image_quality(image_bgr, cfg):
    """Return quality metrics and whether the image passes configured gates."""
    import cv2

    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    blur_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    passed = True
    reasons = []

    if cfg.get("quality_gate_enabled"):
        if blur_variance < float(cfg["quality_min_blur_variance"]):
            passed = False
            reasons.append(f"blur_variance={blur_variance:.1f} < {cfg['quality_min_blur_variance']}")
        if brightness < float(cfg["quality_min_brightness"]):
            passed = False
            reasons.append(f"brightness={brightness:.1f} < {cfg['quality_min_brightness']}")
        if brightness > float(cfg["quality_max_brightness"]):
            passed = False
            reasons.append(f"brightness={brightness:.1f} > {cfg['quality_max_brightness']}")

    return {
        "passed": passed,
        "blur_variance": round(blur_variance, 2),
        "brightness": round(brightness, 2),
        "reasons": reasons,
    }


def apply_quality_gate(cfg, image_path, image_rel=None):
    """Assess a saved still; delete or flag if it fails quality checks."""
    import cv2

    path = Path(image_path)
    image_bgr = cv2.imread(str(path))
    if image_bgr is None:
        return {"passed": False, "action": "unreadable", "reasons": ["unreadable image"]}

    quality = assess_image_quality(image_bgr, cfg)
    if quality["passed"]:
        return {"passed": True, "action": None, **quality}

    action = cfg.get("quality_gate_action", "skip")
    meta_path = capture_metadata_path(path)
    if action == "skip":
        path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        append_jsonl(Path(cfg["output_base"]) / "quality_rejects.jsonl", {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "image_rel": image_rel,
            "action": "skipped",
            **quality,
        })
        return {"passed": False, "action": "skipped", **quality}

    if image_rel:
        flag_for_review(cfg["output_base"], image_rel, note="quality_gate: " + "; ".join(quality["reasons"]))
    append_jsonl(Path(cfg["output_base"]) / "quality_rejects.jsonl", {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "image_rel": image_rel,
        "action": "flagged",
        **quality,
    })
    return {"passed": False, "action": "flagged", **quality}


def count_classes_from_txt(label_path):
    counts = {name: 0 for name in VALID_CLASSES}
    if not label_path.exists():
        return counts
    for line in label_path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) != 5:
            continue
        try:
            cls_id = int(parts[0])
        except ValueError:
            continue
        if 0 <= cls_id < len(VALID_CLASSES):
            counts[VALID_CLASSES[cls_id]] += 1
    return counts


def free_mb_for_path(path):
    usage = shutil.disk_usage(path if os.path.exists(path) else os.path.dirname(path) or ".")
    return usage.free / (1024 * 1024)


def has_min_free_space(path, min_free_mb):
    if min_free_mb <= 0:
        return True
    return free_mb_for_path(path) >= min_free_mb


def prune_old_files(directory, max_files, pattern="*"):
    if max_files <= 0 or not os.path.isdir(directory):
        return 0
    files = sorted(
        (Path(directory) / name for name in os.listdir(directory) if (Path(directory) / name).is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    removed = 0
    while len(files) > max_files:
        oldest = files.pop(0)
        oldest.unlink(missing_ok=True)
        removed += 1
    return removed


def read_mark_list(root_dir, filename):
    path = Path(root_dir) / filename
    if not path.exists():
        return set()
    return {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()}


class ImageIndexCache:
    def __init__(self, root_dir, ttl_seconds=30):
        self.root_dir = Path(root_dir).resolve()
        self.ttl_seconds = ttl_seconds
        self._images = []
        self._built_at = 0.0

    def invalidate(self):
        self._built_at = 0.0

    def images(self):
        import time
        now = time.time()
        if not self._images or (now - self._built_at) >= self.ttl_seconds:
            self._images = sorted(
                p for p in self.root_dir.rglob("*") if is_image(p)
            )
            self._built_at = now
        return self._images
