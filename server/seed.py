"""
seed.py -- populate the cfg_* tables for one device from the current hardcoded
edge values (seed_data.py), so the assembled config blob reproduces today's
behavior exactly.

Usage (from server/ with .env configured and schema.sql already run):
    python seed.py --device-id jetson-forest-01 --name "Forest Site 01"

Re-running wipes that device's config rows and re-seeds (the device_token is
preserved if the device already exists, so the Jetson keeps working). Prints the
device_token to put in the Jetson's env as CONFIG_API_TOKEN.
"""

import argparse
import secrets

from app.db import session_scope
from app import models
from app.assembly import assemble_config
import seed_data as S


def _wipe_children(db, device_id):
    for M in (
        models.Camera, models.ClassSetting, models.ClipPrompt, models.DeterrenceTarget,
    ):
        db.query(M).filter(M.device_id == device_id).delete()
    for M in (models.ClipDistractors, models.GlobalSettings, models.DeterrenceGlobal):
        db.query(M).filter(M.device_id == device_id).delete()
    db.flush()


def build_device(device_id: str, token: str, display_name: str, tailscale_ip: str | None) -> models.Device:
    """Construct (only) the cfg_devices row from source constants."""
    return models.Device(
        device_id=device_id, device_token=token, display_name=display_name,
        storage_root=S.STORAGE_ROOT, rfdetr_engine_path=S.RFDETR_ENGINE_PATH,
        clip_engine_path=S.CLIP_ENGINE_PATH, clip2_checkpoint=S.CLIP2_CHECKPOINT,
        clip2_model_dir=S.CLIP2_MODEL_DIR, clip_img_input_size=S.CLIP_IMG_INPUT_SIZE,
        rf_nms_iou=S.RF_NMS_IOU, rf_predict_threshold=S.RF_PREDICT_THRESHOLD,
        tailscale_ip=tailscale_ip, enabled=True, restart_required=False,
    )


def build_children(device_id: str) -> list:
    """Construct every child cfg_* row for a device from source constants.
    Pure (no DB), so both seed() and the offline test share one source of truth."""
    rows: list = []

    rows.append(models.GlobalSettings(
            device_id=device_id,
            roi_start_hour=S.ROI_START_HOUR, roi_end_hour=S.ROI_END_HOUR, roi_end_minute=S.ROI_END_MINUTE,
            detection_res_w=S.DETECTION_RES[0], detection_res_h=S.DETECTION_RES[1],
            polygons_drawn_res_w=S.ROI_POLYGONS_DRAWN_AT_RES[0], polygons_drawn_res_h=S.ROI_POLYGONS_DRAWN_AT_RES[1],
            cooldown_specific=S.COOLDOWN_TIME_SPECIFIC, cooldown_generic=S.COOLDOWN_TIME_GENERIC,
            motion_close_delay=S.MOTION_CLOSE_DELAY,
            motion_min_clip_duration=S.MOTION_MIN_CLIP_DURATION, motion_max_clip_duration=S.MOTION_MAX_CLIP_DURATION,
            animal_rotate_sec=S.ANIMAL_ROTATE_SEC, fps=S.FPS,
            min_confirm_frames=S.MIN_CONFIRM_FRAMES, buffer_window=S.BUFFER_WINDOW,
            animal_min_confirm_frames=S.ANIMAL_MIN_CONFIRM_FRAMES, animal_buffer_window=S.ANIMAL_BUFFER_WINDOW,
            min_bbox_ratio=S.MIN_BBOX_RATIO,
            # motion_video_res was NEVER passed here, so every seeded device got
            # NULL -- "record at camera-native resolution" -- and software-encoded
            # 1080p on a board with no hardware encoder.
            motion_video_res_w=S.MOTION_VIDEO_RES[0], motion_video_res_h=S.MOTION_VIDEO_RES[1],
            cleanup_days_to_keep=S.CLEANUP_DAYS_TO_KEEP, cleanup_low_space_free_percent=S.CLEANUP_LOW_SPACE_FREE_PERCENT,
            heartbeat_interval_sec=S.HEARTBEAT_INTERVAL_SEC, poll_interval_sec=S.POLL_INTERVAL_SEC,
            motion_default_min_frames=S.MOTION_DEFAULT["min_frames"], motion_default_threshold=S.MOTION_DEFAULT["threshold"],
            motion_default_kernel_w=S.MOTION_DEFAULT["kernel"][0], motion_default_kernel_h=S.MOTION_DEFAULT["kernel"][1],
            motion_default_area_min=S.MOTION_DEFAULT["area_min"],
            supabase_signal_enabled=S.SUPABASE_SIGNAL_ENABLED, detailed_telegram_msg=S.DETAILED_TELEGRAM_MSG,
            send_fallback_telegram=S.SEND_FALLBACK_TELEGRAM, ignore_zones_enabled=S.IGNORE_ZONES_ENABLED,
            animal_supabase_trigger=S.ANIMAL_SUPABASE_TRIGGER, telegram_chat_id=None,
            # NULL on purpose: the device inherits the fleet-wide bot token from
            # cfg_fleet_settings. Set this only for a site running its own bot,
            # and set it from the dashboard so it never lands in a seed file.
            telegram_bot_token=None,
        ))

    # ---- clip distractors + scalars ----
    rows.append(models.ClipDistractors(
        device_id=device_id, distractors=list(S.CLIP_DISTRACTORS),
        fallback_min_logit=S.CLIP_FALLBACK_MIN_LOGIT, bbox_pad=S.CLIP_BBOX_PAD,
        min_crop_w=S.CLIP_MIN_CROP_W, min_crop_h=S.CLIP_MIN_CROP_H, min_rf_conf=S.CLIP_MIN_RF_CONF,
        clip_cooldown=S.CLIP_COOLDOWN, keep_text_tower_resident=S.KEEP_TEXT_TOWER_RESIDENT,
    ))

    # ---- deterrence global + targets ----
    rows.append(models.DeterrenceGlobal(device_id=device_id, **S.DETERRENCE_GLOBAL))
    for cls, t in S.DETERRENCE_TARGETS.items():
        rows.append(models.DeterrenceTarget(device_id=device_id, class_name=cls, action=t["action"], audio_files=list(t["audio_files"])))

    # ---- cameras ----
    for i, (url, codec) in enumerate(S.CAMERAS):
        cam_name = f"CAM {i+1}"
        m = S.MOTION_SENSITIVITY_PER_CAM.get(cam_name, S.MOTION_DEFAULT)
        roi = [list(p) for p in S.ROI_POLYGONS_RAW_ORIGINAL.get(cam_name, [])]
        ign = S.IGNORE_ZONES_NATIVE.get(cam_name, [])
        rows.append(models.Camera(
            device_id=device_id, cam_name=cam_name, cam_index=i, rtsp_url=url, codec=codec, enabled=True,
            motion_min_frames=m["min_frames"], motion_threshold=m["threshold"],
            motion_kernel_w=m["kernel"][0], motion_kernel_h=m["kernel"][1], motion_area_min=m["area_min"],
            roi_polygon=roi, roi_drawn_w=S.ROI_POLYGONS_DRAWN_AT_RES[0], roi_drawn_h=S.ROI_POLYGONS_DRAWN_AT_RES[1],
            ignore_zones=ign, ignore_drawn_w=S.IGNORE_DRAWN_AT_RES[0], ignore_drawn_h=S.IGNORE_DRAWN_AT_RES[1],
        ))

    # ---- class settings ----
    name_to_id = {v: k for k, v in S.MY_CLASSES.items()}
    for order, (cls, cs) in enumerate(S.CLASS_SETTINGS.items()):
        b, g, r = cs["color"]
        rows.append(models.ClassSetting(
            device_id=device_id, class_name=cls, rf_class_id=name_to_id.get(cls),
            rf_thresh=cs["thresh"], color_b=b, color_g=g, color_r=r,
            clip_min_logit=S.CLIP_CLASS_MIN_LOGIT.get(cls),
            is_clip_alert_class=(cls in S.CLIP_ALERT_CLASSES), sort_order=order,
        ))

    # ---- clip prompts ----
    prompt_classes = set(S.CLIP_PROMPTS) | set(S.CLIP_FALLBACKS) | set(S.CROSS_SPECIES_FALLBACK)
    for cls in sorted(prompt_classes):
        rows.append(models.ClipPrompt(
            device_id=device_id, class_name=cls,
            positive_prompts=list(S.CLIP_PROMPTS.get(cls, [])),
            fallbacks={k: list(v) for k, v in S.CLIP_FALLBACKS.get(cls, {}).items()},
            cross_species=S.CROSS_SPECIES_FALLBACK.get(cls),
        ))

    return rows


def seed(device_id: str, display_name: str, tailscale_ip: str | None):
    with session_scope() as db:
        existing = db.get(models.Device, device_id)
        token = existing.device_token if existing else secrets.token_urlsafe(32)
        if existing is not None:
            _wipe_children(db, device_id)
            db.delete(existing)
            db.flush()
        dev = build_device(device_id, token, display_name, tailscale_ip)
        db.add(dev)
        db.flush()   # insert the parent cfg_devices row FIRST so child FKs resolve
                     # (models have no relationship(), so the UOW won't order this for us)
        for row in build_children(device_id):
            db.add(row)
        db.flush()
        blob = assemble_config(db, device_id)
        dev.config_version = blob["config_version"]
        version = blob["config_version"]

    print(f"Seeded device '{device_id}'.")
    print(f"  config_version: {version}")
    print(f"  device_token  : {token}")
    print("  -> put this token in the Jetson's env as CONFIG_API_TOKEN")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--device-id", default="jetson-forest-01")
    ap.add_argument("--name", default="Forest Site 01")
    ap.add_argument("--tailscale-ip", default=None)
    args = ap.parse_args()
    seed(args.device_id, args.name, args.tailscale_ip)
