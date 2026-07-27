"""
assembly.py -- compose the cfg_* rows for one device into the single JSON blob
the Jetson edge consumes, and compute a deterministic config_version hash.

The blob shape is the contract shared with standalone/config_client.py on the
edge: keys mirror the dicts the edge pipeline already uses (cameras keyed by
"CAM n", class settings keyed by class name), so the edge refactor is a thin
"read from blob instead of constant" swap.

The blob carries SECRETS -- the Telegram bot token in flags, and RTSP URLs with
embedded camera passwords. Devices need them; humans and log files must not see
them. Every endpoint that returns a blob to an operator must pass it through
redact() first.

config_version = sha256 over the canonical (sorted-key) JSON of the entire blob
EXCLUDING the config_version field itself. Any change to any cfg_* row for the
device changes the hash, so the edge's cheap /version poll detects it. The same
assemble() runs on every admin write (to bump the stored version) and on the
edge-facing GET, guaranteeing they always agree.
"""

import copy
import hashlib
import json

from sqlalchemy.orm import Session

from . import models


def _motion_block(cam: models.Camera, g: models.GlobalSettings) -> dict:
    """Resolve a camera's motion settings, inheriting the global default block
    for any NULL field (mirrors edge get_motion_settings fallback)."""
    if cam.motion_min_frames is None:
        return {
            "min_frames": g.motion_default_min_frames,
            "threshold": g.motion_default_threshold,
            "kernel": [g.motion_default_kernel_w, g.motion_default_kernel_h],
            "area_min": g.motion_default_area_min,
        }
    return {
        "min_frames": cam.motion_min_frames,
        "threshold": cam.motion_threshold,
        "kernel": [cam.motion_kernel_w, cam.motion_kernel_h],
        "area_min": cam.motion_area_min,
    }


def compute_config_version(content: dict) -> str:
    """sha256 over canonical JSON of the blob minus its config_version field.

    restart_required is excluded too: it is a transient command, not part of the
    configuration. Hashing it made one restart request produce TWO version bumps
    (set, then clear) and therefore two full apply cycles on the edge.
    """
    tmp = dict(content)
    tmp.pop("config_version", None)
    tmp.pop("restart_required", None)
    canonical = json.dumps(tmp, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_bot_token(g: models.GlobalSettings | None,
                       fleet: models.FleetSettings | None) -> str | None:
    """Per-device override if set to something non-empty, else the fleet token.

    Empty string and NULL mean the same thing here -- "not configured" -- so
    that clearing the field in the dashboard falls back to the fleet value
    instead of pushing an empty token that the edge would have to special-case.
    """
    override = (getattr(g, "telegram_bot_token", None) or "").strip()
    if override:
        return override
    return (getattr(fleet, "telegram_bot_token", None) or "").strip() or None


# Blob paths holding secrets. Any response that renders a config blob to a
# human must go through redact() -- the blob carries the Telegram bot token and
# RTSP URLs with embedded camera passwords.
REDACTED = "***redacted***"


def redact(blob: dict | None) -> dict | None:
    """A display-safe deep copy of a config blob.

    Returns a COPY: the same blob object is held in cache.py and served to
    devices, so redacting in place would push blanked secrets to the fleet.

    config_version is deliberately left intact -- it is a hash, not a secret,
    and operators compare it against what a device reports.
    """
    if blob is None:
        return None
    out = copy.deepcopy(blob)
    flags = out.get("flags")
    if isinstance(flags, dict) and flags.get("telegram_bot_token"):
        flags["telegram_bot_token"] = REDACTED
    for cam in out.get("cameras") or []:
        if isinstance(cam, dict) and cam.get("rtsp_url"):
            cam["rtsp_url"] = REDACTED
    return out


def assemble_config(db: Session, device_id: str) -> dict | None:
    """Build the full config blob for a device. Returns None if the device row
    does not exist. Fills config_version at the end."""
    dev = db.get(models.Device, device_id)
    if dev is None:
        return None

    g = db.get(models.GlobalSettings, device_id)
    cd = db.get(models.ClipDistractors, device_id)
    dg = db.get(models.DeterrenceGlobal, device_id)
    # One extra single-row SELECT, and it only lands on the COLD path: cache.py
    # plus the stored cfg_devices.config_version mean assembly runs on config
    # mutation, not on the edge's /version poll.
    fleet = db.get(models.FleetSettings, 1)

    cams = (
        db.query(models.Camera)
        .filter(models.Camera.device_id == device_id)
        .order_by(models.Camera.cam_index)
        .all()
    )
    class_rows = (
        db.query(models.ClassSetting)
        .filter(models.ClassSetting.device_id == device_id)
        .order_by(models.ClassSetting.sort_order)
        .all()
    )
    prompt_rows = (
        db.query(models.ClipPrompt).filter(models.ClipPrompt.device_id == device_id).all()
    )
    target_rows = (
        db.query(models.DeterrenceTarget).filter(models.DeterrenceTarget.device_id == device_id).all()
    )

    # ---- global-derived sub-blocks (guard against missing global row) ----
    motion_video_res = None
    if g and g.motion_video_res_w and g.motion_video_res_h:
        motion_video_res = [g.motion_video_res_w, g.motion_video_res_h]

    blob: dict = {
        "device_id": device_id,
        "restart_required": bool(dev.restart_required),
        "poll_interval_sec": g.poll_interval_sec if g else 30,
        "paths": {
            "storage_root": dev.storage_root,
            "rfdetr_engine_path": dev.rfdetr_engine_path,
            "clip_engine_path": dev.clip_engine_path,
            "clip2_checkpoint": dev.clip2_checkpoint,
            "clip2_model_dir": dev.clip2_model_dir,
            "clip_img_input_size": dev.clip_img_input_size,
            "rf_nms_iou": dev.rf_nms_iou,
            "rf_predict_threshold": dev.rf_predict_threshold,
        },
        "night_window": {
            "start_hour": g.roi_start_hour if g else 18,
            "end_hour": g.roi_end_hour if g else 6,
            "end_minute": g.roi_end_minute if g else 31,
        },
        "detection_res": [g.detection_res_w, g.detection_res_h] if g else [1280, 720],
        "polygons_drawn_at_res": [g.polygons_drawn_res_w, g.polygons_drawn_res_h] if g else [1080, 720],
        "motion_video_res": motion_video_res,
        "cooldowns": {
            "specific": g.cooldown_specific if g else 3,
            "generic": g.cooldown_generic if g else 15,
            "motion_close_delay": g.motion_close_delay if g else 8,
            "clip": cd.clip_cooldown if cd else 5.0,
        },
        "recording": {
            "motion_min_clip_duration": g.motion_min_clip_duration if g else 30,
            "motion_max_clip_duration": g.motion_max_clip_duration if g else 3600,
            "animal_rotate_sec": g.animal_rotate_sec if g else 10,
            "fps": g.fps if g else 10.0,
        },
        "inference": {
            "target_fps": g.inference_target_fps if g else 5.0,
        },
        "confirm": {
            "min_confirm_frames": g.min_confirm_frames if g else 18,
            "buffer_window": g.buffer_window if g else 25,
            "animal_min_confirm_frames": g.animal_min_confirm_frames if g else 30,
            "animal_buffer_window": g.animal_buffer_window if g else 40,
        },
        "min_bbox_ratio": g.min_bbox_ratio if g else 0.0026,
        "cleanup": {
            "days_to_keep": g.cleanup_days_to_keep if g else 3,
            "low_space_free_percent": g.cleanup_low_space_free_percent if g else 10,
        },
        "heartbeat_interval_sec": g.heartbeat_interval_sec if g else 60,
        "motion_default": {
            "min_frames": g.motion_default_min_frames if g else 5,
            "threshold": g.motion_default_threshold if g else 20,
            "kernel": [g.motion_default_kernel_w, g.motion_default_kernel_h] if g else [7, 7],
            "area_min": g.motion_default_area_min if g else 300,
        },
        "flags": {
            "supabase_signal_enabled": g.supabase_signal_enabled if g else True,
            "detailed_telegram_msg": g.detailed_telegram_msg if g else True,
            "send_fallback_telegram": g.send_fallback_telegram if g else False,
            "ignore_zones_enabled": g.ignore_zones_enabled if g else True,
            "animal_supabase_trigger": g.animal_supabase_trigger if g else True,
            "telegram_chat_id": g.telegram_chat_id if g else None,
            # Device override wins when set to a non-empty value, else the
            # fleet-wide token. A secret in the blob, so every surface that
            # renders a blob must run it through redact() first.
            "telegram_bot_token": _resolve_bot_token(g, fleet),
        },
        "cameras": [
            {
                "cam_name": c.cam_name,
                "cam_index": c.cam_index,
                "enabled": c.enabled,
                "rtsp_url": c.rtsp_url,
                "codec": c.codec,
                "motion": _motion_block(c, g) if g else _motion_block(c, _DEFAULT_G),
                "roi_polygon": c.roi_polygon or [],
                "roi_drawn_at_res": [c.roi_drawn_w, c.roi_drawn_h],
                "ignore_zones": c.ignore_zones or [],
                "ignore_drawn_at_res": [c.ignore_drawn_w, c.ignore_drawn_h],
            }
            for c in cams
        ],
        "class_settings": {
            r.class_name: {
                "thresh": r.rf_thresh,
                "color": [r.color_b, r.color_g, r.color_r],
                "rf_class_id": r.rf_class_id,
            }
            for r in class_rows
        },
        "clip": {
            "class_min_logit": {
                r.class_name: r.clip_min_logit
                for r in class_rows
                if r.clip_min_logit is not None
            },
            "fallback_min_logit": cd.fallback_min_logit if cd else 27.0,
            "alert_classes": [r.class_name for r in class_rows if r.is_clip_alert_class],
            "cooldown": cd.clip_cooldown if cd else 5.0,
            "bbox_pad": cd.bbox_pad if cd else 0.30,
            "min_crop_w": cd.min_crop_w if cd else 45,
            "min_crop_h": cd.min_crop_h if cd else 45,
            "min_rf_conf": cd.min_rf_conf if cd else 0.40,
            "keep_text_tower_resident": cd.keep_text_tower_resident if cd else True,
            "prompts": {r.class_name: (r.positive_prompts or []) for r in prompt_rows},
            "fallbacks": {r.class_name: (r.fallbacks or {}) for r in prompt_rows if r.fallbacks},
            "cross_species": {
                r.class_name: r.cross_species for r in prompt_rows if r.cross_species
            },
            "distractors": (cd.distractors if cd else []),
        },
        "deterrence": {
            "relay_mode": dg.relay_mode if dg else "both",
            "audio_playback_mode": dg.audio_playback_mode if dg else "mixed",
            "gpio": {
                "ch1_pin": dg.relay_ch1_pin if dg else 11,
                "ch2_pin": dg.relay_ch2_pin if dg else 13,
                "active_low": dg.relay_active_low if dg else True,
            },
            "usb": {
                "port": dg.usb_relay_port if dg else "/dev/ttyUSB0",
                "baud": dg.usb_relay_baud if dg else 9600,
                "on_cmd_hex": dg.usb_on_cmd_hex if dg else "A00101A2",
                "off_cmd_hex": dg.usb_off_cmd_hex if dg else "A00100A1",
            },
            "blink": {
                "total_duration": dg.blink_total_duration if dg else 30,
                "active_phase": dg.blink_active_phase if dg else 10,
                "rest_phase": dg.blink_rest_phase if dg else 3,
                "interval": dg.blink_interval if dg else 0.1,
            },
            "targets": {
                r.class_name: {"action": r.action, "audio_files": (r.audio_files or [])}
                for r in target_rows
            },
        },
    }

    blob["config_version"] = compute_config_version(blob)
    return blob


# A stand-in GlobalSettings with defaults, used only if the global row is
# somehow missing when resolving a camera's motion block.
class _DefaultG:
    motion_default_min_frames = 5
    motion_default_threshold = 20
    motion_default_kernel_w = 7
    motion_default_kernel_h = 7
    motion_default_area_min = 300


_DEFAULT_G = _DefaultG()
