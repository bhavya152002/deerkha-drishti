"""
verify_seed.py -- assert the assembled config blob for a device equals the
current hardcoded edge values (seed_data.py). Run AFTER seed.py to prove the
server will reproduce today's behavior exactly.

    python verify_seed.py --device-id jetson-forest-01

Exits non-zero (and prints each mismatch) if anything drifted.
"""

import argparse
import sys

from app.db import SessionLocal
from app.assembly import assemble_config
import seed_data as S

_fails: list[str] = []


def check(label, got, want):
    if got != want:
        _fails.append(f"MISMATCH {label}:\n    got : {got!r}\n    want: {want!r}")


def verify(device_id: str):
    db = SessionLocal()
    try:
        blob = assemble_config(db, device_id)
    finally:
        db.close()
    if blob is None:
        print(f"Device '{device_id}' not found -- run seed.py first.")
        sys.exit(2)

    # paths
    check("paths.storage_root", blob["paths"]["storage_root"], S.STORAGE_ROOT)
    check("paths.rfdetr_engine_path", blob["paths"]["rfdetr_engine_path"], S.RFDETR_ENGINE_PATH)
    check("paths.clip_engine_path", blob["paths"]["clip_engine_path"], S.CLIP_ENGINE_PATH)
    check("paths.clip2_checkpoint", blob["paths"]["clip2_checkpoint"], S.CLIP2_CHECKPOINT)
    check("paths.clip_img_input_size", blob["paths"]["clip_img_input_size"], S.CLIP_IMG_INPUT_SIZE)

    # night window / geometry
    check("night.start", blob["night_window"]["start_hour"], S.ROI_START_HOUR)
    check("night.end_hour", blob["night_window"]["end_hour"], S.ROI_END_HOUR)
    check("night.end_minute", blob["night_window"]["end_minute"], S.ROI_END_MINUTE)
    check("detection_res", tuple(blob["detection_res"]), S.DETECTION_RES)
    check("polygons_drawn_at_res", tuple(blob["polygons_drawn_at_res"]), S.ROI_POLYGONS_DRAWN_AT_RES)
    check("min_bbox_ratio", blob["min_bbox_ratio"], S.MIN_BBOX_RATIO)

    # cooldowns / recording / confirm
    check("cd.specific", blob["cooldowns"]["specific"], S.COOLDOWN_TIME_SPECIFIC)
    check("cd.generic", blob["cooldowns"]["generic"], S.COOLDOWN_TIME_GENERIC)
    check("cd.motion_close_delay", blob["cooldowns"]["motion_close_delay"], S.MOTION_CLOSE_DELAY)
    check("cd.clip", blob["cooldowns"]["clip"], S.CLIP_COOLDOWN)
    check("rec.min", blob["recording"]["motion_min_clip_duration"], S.MOTION_MIN_CLIP_DURATION)
    check("rec.max", blob["recording"]["motion_max_clip_duration"], S.MOTION_MAX_CLIP_DURATION)
    check("confirm.min", blob["confirm"]["min_confirm_frames"], S.MIN_CONFIRM_FRAMES)
    check("confirm.buffer", blob["confirm"]["buffer_window"], S.BUFFER_WINDOW)
    check("confirm.animal_min", blob["confirm"]["animal_min_confirm_frames"], S.ANIMAL_MIN_CONFIRM_FRAMES)
    check("confirm.animal_buffer", blob["confirm"]["animal_buffer_window"], S.ANIMAL_BUFFER_WINDOW)

    # cleanup + motion default
    check("cleanup.days", blob["cleanup"]["days_to_keep"], S.CLEANUP_DAYS_TO_KEEP)
    check("cleanup.low_space", blob["cleanup"]["low_space_free_percent"], S.CLEANUP_LOW_SPACE_FREE_PERCENT)
    # NULL here means "record at camera-native resolution", which on a board
    # with no hardware encoder is the single most expensive default we can ship.
    # Assert it is actually set, not merely present.
    check("motion_video_res", blob["motion_video_res"], list(S.MOTION_VIDEO_RES))
    check("motion_default", blob["motion_default"], {
        "min_frames": S.MOTION_DEFAULT["min_frames"], "threshold": S.MOTION_DEFAULT["threshold"],
        "kernel": list(S.MOTION_DEFAULT["kernel"]), "area_min": S.MOTION_DEFAULT["area_min"],
    })

    # flags
    check("flags.supabase", blob["flags"]["supabase_signal_enabled"], S.SUPABASE_SIGNAL_ENABLED)
    check("flags.detailed_tg", blob["flags"]["detailed_telegram_msg"], S.DETAILED_TELEGRAM_MSG)
    check("flags.send_fallback_tg", blob["flags"]["send_fallback_telegram"], S.SEND_FALLBACK_TELEGRAM)
    check("flags.ignore_zones", blob["flags"]["ignore_zones_enabled"], S.IGNORE_ZONES_ENABLED)
    check("flags.animal_supabase", blob["flags"]["animal_supabase_trigger"], S.ANIMAL_SUPABASE_TRIGGER)
    # Contract check only -- the value is a secret set from the dashboard, so
    # assert the key is delivered, not what it contains. Without the key the
    # edge falls back to its env token silently.
    check("flags.telegram_bot_token delivered", "telegram_bot_token" in blob["flags"], True)

    # cameras
    cams = {c["cam_name"]: c for c in blob["cameras"]}
    check("camera count", len(blob["cameras"]), len(S.CAMERAS))
    for i, (url, codec) in enumerate(S.CAMERAS):
        name = f"CAM {i+1}"
        c = cams.get(name)
        if not c:
            _fails.append(f"MISSING camera {name}")
            continue
        check(f"{name}.url", c["rtsp_url"], url)
        check(f"{name}.codec", c["codec"], codec)
        m = S.MOTION_SENSITIVITY_PER_CAM.get(name, S.MOTION_DEFAULT)
        check(f"{name}.motion", c["motion"], {
            "min_frames": m["min_frames"], "threshold": m["threshold"],
            "kernel": list(m["kernel"]), "area_min": m["area_min"],
        })
        check(f"{name}.roi", c["roi_polygon"], [list(p) for p in S.ROI_POLYGONS_RAW_ORIGINAL.get(name, [])])
        check(f"{name}.ignore", c["ignore_zones"], S.IGNORE_ZONES_NATIVE.get(name, []))

    # class settings
    for cls, cs in S.CLASS_SETTINGS.items():
        got = blob["class_settings"].get(cls)
        b, g, r = cs["color"]
        check(f"class[{cls}]", got, {
            "thresh": cs["thresh"], "color": [b, g, r],
            "rf_class_id": {v: k for k, v in S.MY_CLASSES.items()}.get(cls),
        })

    # clip
    check("clip.fallback_min_logit", blob["clip"]["fallback_min_logit"], S.CLIP_FALLBACK_MIN_LOGIT)
    check("clip.bbox_pad", blob["clip"]["bbox_pad"], S.CLIP_BBOX_PAD)
    check("clip.min_crop_w", blob["clip"]["min_crop_w"], S.CLIP_MIN_CROP_W)
    check("clip.min_crop_h", blob["clip"]["min_crop_h"], S.CLIP_MIN_CROP_H)
    check("clip.min_rf_conf", blob["clip"]["min_rf_conf"], S.CLIP_MIN_RF_CONF)
    check("clip.class_min_logit", blob["clip"]["class_min_logit"], S.CLIP_CLASS_MIN_LOGIT)
    check("clip.alert_classes", sorted(blob["clip"]["alert_classes"]), sorted(S.CLIP_ALERT_CLASSES))
    check("clip.cross_species", blob["clip"]["cross_species"], S.CROSS_SPECIES_FALLBACK)
    check("clip.distractors", blob["clip"]["distractors"], list(S.CLIP_DISTRACTORS))
    for cls, prompts in S.CLIP_PROMPTS.items():
        check(f"clip.prompts[{cls}]", blob["clip"]["prompts"].get(cls), list(prompts))
    for cls, fb in S.CLIP_FALLBACKS.items():
        check(f"clip.fallbacks[{cls}]", blob["clip"]["fallbacks"].get(cls), {k: list(v) for k, v in fb.items()})

    # deterrence
    dg = blob["deterrence"]
    check("deter.relay_mode", dg["relay_mode"], S.DETERRENCE_GLOBAL["relay_mode"])
    check("deter.audio_mode", dg["audio_playback_mode"], S.DETERRENCE_GLOBAL["audio_playback_mode"])
    check("deter.gpio.ch1", dg["gpio"]["ch1_pin"], S.DETERRENCE_GLOBAL["relay_ch1_pin"])
    check("deter.gpio.ch2", dg["gpio"]["ch2_pin"], S.DETERRENCE_GLOBAL["relay_ch2_pin"])
    check("deter.usb.on", dg["usb"]["on_cmd_hex"], S.DETERRENCE_GLOBAL["usb_on_cmd_hex"])
    check("deter.blink.total", dg["blink"]["total_duration"], S.DETERRENCE_GLOBAL["blink_total_duration"])
    for cls, t in S.DETERRENCE_TARGETS.items():
        check(f"deter.target[{cls}]", dg["targets"].get(cls), {"action": t["action"], "audio_files": list(t["audio_files"])})

    if _fails:
        print(f"\nFAILED: {len(_fails)} mismatch(es):\n")
        for f in _fails:
            print(f)
        sys.exit(1)
    print(f"OK: assembled config for '{device_id}' matches all {device_id} source constants.")
    print(f"config_version: {blob['config_version']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--device-id", default="jetson-forest-01")
    args = ap.parse_args()
    verify(args.device_id)
