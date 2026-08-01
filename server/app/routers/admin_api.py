"""
routers/admin_api.py -- JSON CRUD the dashboard uses to edit every config group.

All routes require an admin session. Every write goes through service.bump_and_audit
so config_version is recomputed and the change is audited. Field updates use an
explicit allow-list per group (never blind setattr) so unknown/immutable columns
can't be poked from the browser.
"""

import secrets
import uuid as uuidlib

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..assembly import REDACTED, assemble_config, redact
from ..auth import require_admin
from ..db import get_db
from ..service import bump_all_devices, bump_and_audit
from . import live

router = APIRouter(prefix="/api/admin", tags=["admin"])


# Fields whose VALUE must never reach cfg_audit_log.diff. Audit rows are kept
# for months and are rendered by the audit endpoint, so a secret written here is
# a secret on a dashboard page long after the edit. rtsp_url is included because
# it embeds the camera password.
SECRET_DIFF_FIELDS = {"telegram_bot_token", "rtsp_url"}

# Fields the dashboard never renders back, so an untouched form control posts an
# empty string. For these, empty means "leave it alone" -- otherwise saving any
# unrelated field on the same page silently wipes the secret. To actually unset
# one, the client posts "<field>__clear": true.
WRITE_ONLY_FIELDS = {"telegram_bot_token"}


def _apply(obj, body: dict, allowed: set):
    """Set only allow-listed keys present in body onto obj.

    Returns the audit diff, with secret values replaced by a placeholder.
    """
    changed = {}
    for k in allowed:
        if k in WRITE_ONLY_FIELDS and body.get(f"{k}__clear"):
            setattr(obj, k, None)
            changed[k] = "<cleared>"
            continue
        if k not in body:
            continue
        v = body[k]
        if k in WRITE_ONLY_FIELDS:
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            v = v.strip() if isinstance(v, str) else v
        setattr(obj, k, v)
        changed[k] = REDACTED if k in SECRET_DIFF_FIELDS else v
    return changed


def _require_device(db: Session, device_id: str) -> models.Device:
    dev = db.get(models.Device, device_id)
    if dev is None:
        raise HTTPException(404, "device not found")
    return dev


# =====================================================================
# Devices
# =====================================================================
@router.get("/devices")
def list_devices(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(models.Device).order_by(models.Device.device_id).all()
    return [
        {
            "device_id": d.device_id,
            "display_name": d.display_name,
            "enabled": d.enabled,
            "config_version": d.config_version,
            "restart_required": d.restart_required,
            "tailscale_ip": d.tailscale_ip,
        }
        for d in rows
    ]


@router.post("/devices")
async def create_device(request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    body = await request.json()
    device_id = body.get("device_id")
    if not device_id:
        raise HTTPException(400, "device_id required")
    if db.get(models.Device, device_id):
        raise HTTPException(409, "device already exists")
    token = secrets.token_urlsafe(32)
    dev = models.Device(
        device_id=device_id,
        display_name=body.get("display_name", device_id),
        device_token=token,
    )
    db.add(dev)
    db.flush()   # insert cfg_devices before its child rows so FKs resolve
    # minimal companion rows so the blob assembles cleanly
    db.add(models.GlobalSettings(device_id=device_id))
    db.add(models.ClipDistractors(device_id=device_id))
    db.add(models.DeterrenceGlobal(device_id=device_id))
    db.commit()
    bump_and_audit(db, device_id, actor=admin, table_name="cfg_devices", row_pk=device_id, action="insert")
    return {"device_id": device_id, "device_token": token}


DEVICE_FIELDS = {
    "display_name", "storage_root", "rfdetr_engine_path", "clip_engine_path",
    "clip2_checkpoint", "clip2_model_dir", "clip_img_input_size", "rf_nms_iou",
    "rf_predict_threshold", "tailscale_ip", "enabled",
}


@router.put("/devices/{device_id}")
async def update_device(device_id: str, request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    dev = _require_device(db, device_id)
    body = await request.json()
    diff = _apply(dev, body, DEVICE_FIELDS)
    db.commit()
    if "tailscale_ip" in diff:
        # A wrong/stale tailscale_ip is the single most common cause of live
        # view returning 502, so don't make the operator wait out a cache TTL
        # after they just corrected it.
        live.invalidate_device(device_id)
    v = bump_and_audit(db, device_id, actor=admin, table_name="cfg_devices", row_pk=device_id, action="update", diff=diff)
    return {"ok": True, "config_version": v}


@router.post("/devices/{device_id}/rotate-token")
def rotate_token(device_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    dev = _require_device(db, device_id)
    token = secrets.token_urlsafe(32)
    dev.device_token = token
    db.commit()
    # The live proxy caches (tailscale_ip, token) for 60s to keep a blocking DB
    # lookup off the event loop on every snapshot. Drop it now so live view
    # doesn't authenticate to the Jetson with the old token until it expires.
    live.invalidate_device(device_id)
    bump_and_audit(db, device_id, actor=admin, table_name="cfg_devices", row_pk=device_id, action="rotate-token")
    return {"device_id": device_id, "device_token": token}


@router.post("/devices/{device_id}/restart")
def force_restart(device_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    dev = _require_device(db, device_id)
    dev.restart_required = True
    db.commit()
    v = bump_and_audit(db, device_id, actor=admin, table_name="cfg_devices", row_pk=device_id, action="force-restart")
    return {"ok": True, "config_version": v, "restart_required": True}


# =====================================================================
# Fleet settings -- one row, shared by every device
# =====================================================================
FLEET_FIELDS = {"telegram_bot_token"}


def _fleet_row(db: Session) -> models.FleetSettings:
    """The singleton row, created on first use so a database that predates the
    table's migration still works after the ALTER is applied."""
    f = db.get(models.FleetSettings, 1)
    if f is None:
        f = models.FleetSettings(id=1)
        db.add(f)
        db.commit()
    return f


@router.get("/fleet")
def get_fleet(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    f = _fleet_row(db)
    # Set/unset only -- the token itself is never sent back to the browser.
    return {"telegram_bot_token_set": bool((f.telegram_bot_token or "").strip())}


@router.put("/fleet")
async def put_fleet(request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    f = _fleet_row(db)
    body = await request.json()
    diff = _apply(f, body, FLEET_FIELDS)
    if not diff:
        # Nothing changed (the usual case: an untouched password field posted
        # empty). Bumping every device for a no-op would make the whole fleet
        # re-fetch its config for nothing.
        return {"ok": True, "devices_bumped": 0}
    db.commit()
    n = bump_all_devices(
        db, actor=admin, table_name="cfg_fleet_settings", row_pk="1",
        action="update", diff=diff,
    )
    return {"ok": True, "devices_bumped": n}


# =====================================================================
# Global settings
# =====================================================================
GLOBAL_FIELDS = {
    "roi_start_hour", "roi_end_hour", "roi_end_minute",
    "detection_res_w", "detection_res_h", "polygons_drawn_res_w", "polygons_drawn_res_h",
    "motion_video_res_w", "motion_video_res_h",
    "cooldown_specific", "cooldown_generic", "motion_close_delay",
    "motion_min_clip_duration", "motion_max_clip_duration", "animal_rotate_sec", "fps",
    "inference_target_fps",
    "min_confirm_frames", "buffer_window", "animal_min_confirm_frames", "animal_buffer_window",
    "min_bbox_ratio", "cleanup_days_to_keep", "cleanup_low_space_free_percent",
    "heartbeat_interval_sec", "poll_interval_sec",
    "motion_default_min_frames", "motion_default_threshold", "motion_default_kernel_w",
    "motion_default_kernel_h", "motion_default_area_min",
    "supabase_signal_enabled", "detailed_telegram_msg", "send_fallback_telegram",
    "ignore_zones_enabled", "animal_supabase_trigger", "telegram_chat_id",
    "telegram_bot_token",
}


@router.get("/devices/{device_id}/global")
def get_global(device_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    g = db.get(models.GlobalSettings, device_id)
    if not g:
        raise HTTPException(404, "no global settings")
    # This is a blanket column dump, so any secret column added to the table is
    # exposed by default. Report whether the per-device token override is set
    # rather than what it is; the dashboard only needs to render "set"/"unset".
    out = {c.name: getattr(g, c.name) for c in g.__table__.columns}
    out.pop("telegram_bot_token", None)
    out["telegram_bot_token_set"] = bool((g.telegram_bot_token or "").strip())
    return out


@router.put("/devices/{device_id}/global")
async def put_global(device_id: str, request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    g = db.get(models.GlobalSettings, device_id)
    if not g:
        g = models.GlobalSettings(device_id=device_id)
        db.add(g)
    body = await request.json()
    diff = _apply(g, body, GLOBAL_FIELDS)
    db.commit()
    v = bump_and_audit(db, device_id, actor=admin, table_name="cfg_global_settings", row_pk=device_id, action="update", diff=diff)
    return {"ok": True, "config_version": v}


# =====================================================================
# Cameras
# =====================================================================
CAMERA_FIELDS = {
    "cam_name", "cam_index", "rtsp_url", "codec", "enabled",
    "motion_min_frames", "motion_threshold", "motion_kernel_w", "motion_kernel_h", "motion_area_min",
    "roi_polygon", "roi_drawn_w", "roi_drawn_h",
    "ignore_zones", "ignore_drawn_w", "ignore_drawn_h", "reference_frame_url",
}


def _cam_dict(c: models.Camera) -> dict:
    return {col.name: (str(getattr(c, col.name)) if col.name == "id" else getattr(c, col.name)) for col in c.__table__.columns}


@router.get("/devices/{device_id}/cameras")
def list_cameras(device_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(models.Camera).filter(models.Camera.device_id == device_id).order_by(models.Camera.cam_index).all()
    return [_cam_dict(c) for c in rows]


def _next_cam_index(db: Session, device_id: str) -> int:
    """One past the highest cam_index on this device. Holes left by deletions are
    NOT reused: cam_index is the live-view addressing key, so reusing an index
    would point an old dashboard URL at a different camera."""
    cur = db.query(func.coalesce(func.max(models.Camera.cam_index), -1)) \
            .filter(models.Camera.device_id == device_id).scalar()
    return int(cur) + 1


def _assert_cam_index_free(db: Session, device_id: str, idx: int, exclude_id=None):
    q = db.query(models.Camera).filter(models.Camera.device_id == device_id,
                                       models.Camera.cam_index == idx)
    if exclude_id is not None:
        q = q.filter(models.Camera.id != exclude_id)
    other = q.first()
    if other is not None:
        raise HTTPException(409, f"cam_index {idx} is already used by '{other.cam_name}' on this device")


@router.post("/devices/{device_id}/cameras")
async def create_camera(device_id: str, request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    _require_device(db, device_id)
    body = await request.json()
    # Previously defaulted to 0, which collided with the first camera on every
    # add and left two rows fighting over one live-view index.
    idx = body.get("cam_index")
    idx = _next_cam_index(db, device_id) if idx in (None, "") else int(idx)
    _assert_cam_index_free(db, device_id, idx)
    body = {**body, "cam_index": idx}
    cam = models.Camera(
        device_id=device_id,
        cam_name=body["cam_name"],
        cam_index=idx,
        rtsp_url=body["rtsp_url"],
        codec=body.get("codec", "h265"),
    )
    _apply(cam, body, CAMERA_FIELDS)
    db.add(cam)
    db.commit()
    # NOTE: deliberately does NOT set restart_required. The edge reconciles the
    # camera set at runtime, so this applies within one poll (~30s).
    v = bump_and_audit(db, device_id, actor=admin, table_name="cfg_cameras", row_pk=str(cam.id), action="insert", diff={"cam_name": cam.cam_name})
    return {"id": str(cam.id), "config_version": v}


def _get_cam(db: Session, cam_id: str) -> models.Camera:
    try:
        cam = db.get(models.Camera, uuidlib.UUID(cam_id))
    except (ValueError, TypeError):
        cam = None
    if cam is None:
        raise HTTPException(404, "camera not found")
    return cam


@router.put("/cameras/{cam_id}")
async def update_camera(cam_id: str, request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    cam = _get_cam(db, cam_id)
    body = await request.json()
    if body.get("cam_index") not in (None, ""):
        _assert_cam_index_free(db, cam.device_id, int(body["cam_index"]), exclude_id=cam.id)
    diff = _apply(cam, body, CAMERA_FIELDS)
    db.commit()
    v = bump_and_audit(db, cam.device_id, actor=admin, table_name="cfg_cameras", row_pk=cam_id, action="update", diff=diff)
    return {"ok": True, "config_version": v}


@router.put("/cameras/{cam_id}/roi")
async def update_camera_roi(cam_id: str, request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Save the detection ROI polygon (drawn at roi_drawn_w x roi_drawn_h)."""
    cam = _get_cam(db, cam_id)
    body = await request.json()
    cam.roi_polygon = body.get("roi_polygon", cam.roi_polygon)
    if "roi_drawn_w" in body:
        cam.roi_drawn_w = body["roi_drawn_w"]
    if "roi_drawn_h" in body:
        cam.roi_drawn_h = body["roi_drawn_h"]
    db.commit()
    v = bump_and_audit(db, cam.device_id, actor=admin, table_name="cfg_cameras", row_pk=cam_id, action="update-roi")
    return {"ok": True, "config_version": v}


@router.put("/cameras/{cam_id}/ignore-zones")
async def update_camera_ignore(cam_id: str, request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Save ignore zones (drawn at native camera res, ignore_drawn_w x _h)."""
    cam = _get_cam(db, cam_id)
    body = await request.json()
    cam.ignore_zones = body.get("ignore_zones", cam.ignore_zones)
    if "ignore_drawn_w" in body:
        cam.ignore_drawn_w = body["ignore_drawn_w"]
    if "ignore_drawn_h" in body:
        cam.ignore_drawn_h = body["ignore_drawn_h"]
    db.commit()
    v = bump_and_audit(db, cam.device_id, actor=admin, table_name="cfg_cameras", row_pk=cam_id, action="update-ignore")
    return {"ok": True, "config_version": v}


@router.delete("/cameras/{cam_id}")
def delete_camera(cam_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Hard delete. The remaining cameras are deliberately NOT renumbered --
    the edge keys its live-view map by cam_index, so sparse indices are correct
    and compacting them would silently reassign every other camera's feed.
    The edge stops this camera within one poll (~30s); no restart needed."""
    cam = _get_cam(db, cam_id)
    device_id = cam.device_id
    db.delete(cam)
    db.commit()
    v = bump_and_audit(db, device_id, actor=admin, table_name="cfg_cameras", row_pk=cam_id, action="delete")
    return {"ok": True, "config_version": v}


# =====================================================================
# Class settings
# =====================================================================
CLASS_FIELDS = {
    "rf_class_id", "rf_thresh", "color_b", "color_g", "color_r",
    "clip_min_logit", "is_clip_alert_class", "sort_order",
}


@router.get("/devices/{device_id}/class-settings")
def list_class_settings(device_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(models.ClassSetting).filter(models.ClassSetting.device_id == device_id).order_by(models.ClassSetting.sort_order).all()
    return [{col.name: (str(getattr(r, col.name)) if col.name == "id" else getattr(r, col.name)) for col in r.__table__.columns} for r in rows]


@router.put("/devices/{device_id}/class-settings/{class_name}")
async def upsert_class_setting(device_id: str, class_name: str, request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    row = (
        db.query(models.ClassSetting)
        .filter(models.ClassSetting.device_id == device_id, models.ClassSetting.class_name == class_name)
        .first()
    )
    body = await request.json()
    if row is None:
        row = models.ClassSetting(device_id=device_id, class_name=class_name, rf_thresh=body.get("rf_thresh", 0.4))
        db.add(row)
    diff = _apply(row, body, CLASS_FIELDS)
    db.commit()
    v = bump_and_audit(db, device_id, actor=admin, table_name="cfg_class_settings", row_pk=class_name, action="upsert", diff=diff)
    return {"ok": True, "config_version": v}


# =====================================================================
# CLIP prompts + distractors
# =====================================================================
@router.get("/devices/{device_id}/clip-prompts")
def list_clip_prompts(device_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(models.ClipPrompt).filter(models.ClipPrompt.device_id == device_id).all()
    return [
        {"class_name": r.class_name, "positive_prompts": r.positive_prompts, "fallbacks": r.fallbacks, "cross_species": r.cross_species}
        for r in rows
    ]


@router.put("/devices/{device_id}/clip-prompts/{class_name}")
async def upsert_clip_prompt(device_id: str, class_name: str, request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    row = (
        db.query(models.ClipPrompt)
        .filter(models.ClipPrompt.device_id == device_id, models.ClipPrompt.class_name == class_name)
        .first()
    )
    body = await request.json()
    if row is None:
        row = models.ClipPrompt(device_id=device_id, class_name=class_name)
        db.add(row)
    diff = _apply(row, body, {"positive_prompts", "fallbacks", "cross_species"})
    db.commit()
    # prompt edits require a text-feature cache rebuild on the edge -> restart flag
    dev = db.get(models.Device, device_id)
    if dev:
        dev.restart_required = True
        db.commit()
    v = bump_and_audit(db, device_id, actor=admin, table_name="cfg_clip_prompts", row_pk=class_name, action="upsert", diff=diff)
    return {"ok": True, "config_version": v, "restart_required": True}


DISTRACTOR_FIELDS = {
    "distractors", "fallback_min_logit", "bbox_pad", "min_crop_w", "min_crop_h",
    "min_rf_conf", "clip_cooldown", "keep_text_tower_resident",
}


@router.get("/devices/{device_id}/clip-distractors")
def get_clip_distractors(device_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    r = db.get(models.ClipDistractors, device_id)
    if not r:
        raise HTTPException(404, "no clip distractor row")
    return {c.name: getattr(r, c.name) for c in r.__table__.columns}


@router.put("/devices/{device_id}/clip-distractors")
async def put_clip_distractors(device_id: str, request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    r = db.get(models.ClipDistractors, device_id)
    if not r:
        r = models.ClipDistractors(device_id=device_id)
        db.add(r)
    body = await request.json()
    restart = "distractors" in body  # distractor list change -> cache rebuild
    diff = _apply(r, body, DISTRACTOR_FIELDS)
    db.commit()
    if restart:
        dev = db.get(models.Device, device_id)
        if dev:
            dev.restart_required = True
            db.commit()
    v = bump_and_audit(db, device_id, actor=admin, table_name="cfg_clip_distractors", row_pk=device_id, action="update", diff=diff)
    return {"ok": True, "config_version": v, "restart_required": restart}


# =====================================================================
# Deterrence
# =====================================================================
DETER_GLOBAL_FIELDS = {
    "relay_mode", "audio_playback_mode", "relay_ch1_pin", "relay_ch2_pin", "relay_active_low",
    "usb_relay_port", "usb_relay_baud", "usb_on_cmd_hex", "usb_off_cmd_hex",
    "blink_total_duration", "blink_active_phase", "blink_rest_phase", "blink_interval",
}

# Bound once by the edge's deterrence.init_hardware(): the GPIO pins are already
# exported and the serial port already open, so these cannot be swapped under a
# running blink loop. Changing one has to bounce the process, or the box keeps
# running the old value -- which is precisely the bug this set exists to close:
# a device set to 'gpio' went on firing the USB relay forever.
#
# Everything else in DETER_GLOBAL_FIELDS (audio_playback_mode, blink_*) is
# genuinely hot -- the audio and blink loops re-read it each iteration -- so it
# must NOT land here, or every timing tweak would reboot the box.
DETER_RESTART_FIELDS = {
    "relay_mode", "relay_ch1_pin", "relay_ch2_pin", "relay_active_low",
    "usb_relay_port", "usb_relay_baud", "usb_on_cmd_hex", "usb_off_cmd_hex",
}


@router.get("/devices/{device_id}/deterrence")
def get_deterrence(device_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    g = db.get(models.DeterrenceGlobal, device_id)
    targets = db.query(models.DeterrenceTarget).filter(models.DeterrenceTarget.device_id == device_id).all()
    return {
        "global": {c.name: getattr(g, c.name) for c in g.__table__.columns} if g else None,
        "targets": [{"class_name": t.class_name, "action": t.action, "audio_files": t.audio_files} for t in targets],
    }


@router.put("/devices/{device_id}/deterrence")
async def put_deterrence_global(device_id: str, request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    g = db.get(models.DeterrenceGlobal, device_id)
    if not g:
        g = models.DeterrenceGlobal(device_id=device_id)
        db.add(g)
    body = await request.json()
    # Compare VALUES, not merely which keys were submitted: the page posts every
    # field on each save, so keying off the diff would turn a blink-timing tweak
    # into a device reboot.
    before = {k: getattr(g, k, None) for k in DETER_RESTART_FIELDS}
    diff = _apply(g, body, DETER_GLOBAL_FIELDS)
    restart = any(getattr(g, k, None) != before[k] for k in DETER_RESTART_FIELDS)
    if restart:
        dev = db.get(models.Device, device_id)
        if dev:
            dev.restart_required = True
    db.commit()
    v = bump_and_audit(db, device_id, actor=admin, table_name="cfg_deterrence_global", row_pk=device_id, action="update", diff=diff)
    return {"ok": True, "config_version": v, "restart_required": restart}


@router.put("/devices/{device_id}/deterrence/targets/{class_name}")
async def upsert_deterrence_target(device_id: str, class_name: str, request: Request, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    row = (
        db.query(models.DeterrenceTarget)
        .filter(models.DeterrenceTarget.device_id == device_id, models.DeterrenceTarget.class_name == class_name)
        .first()
    )
    body = await request.json()
    if row is None:
        row = models.DeterrenceTarget(device_id=device_id, class_name=class_name)
        db.add(row)
    diff = _apply(row, body, {"action", "audio_files"})
    db.commit()
    v = bump_and_audit(db, device_id, actor=admin, table_name="cfg_deterrence_targets", row_pk=class_name, action="upsert", diff=diff)
    return {"ok": True, "config_version": v}


# =====================================================================
# Status / detections / audit / preview
# =====================================================================
@router.get("/devices/{device_id}/status/latest")
def status_latest(device_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    row = (
        db.query(models.DeviceStatus)
        .filter(models.DeviceStatus.device_id == device_id)
        .order_by(models.DeviceStatus.received_at.desc())
        .first()
    )
    if not row:
        return None
    return {
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "cameras_alive": row.cameras_alive,
        "cameras_total": row.cameras_total,
        "per_camera": row.per_camera,
        "config_version": row.config_version,
        "pending_restart": row.pending_restart,
        "app_uptime_sec": row.app_uptime_sec,
        "disk_free_percent": row.disk_free_percent,
    }


@router.get("/devices/{device_id}/audit")
def get_audit(device_id: str, limit: int = 50, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    rows = (
        db.query(models.AuditLog)
        .filter(models.AuditLog.device_id == device_id)
        .order_by(models.AuditLog.created_at.desc())
        .limit(min(limit, 500))
        .all()
    )
    return [
        {
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "table_name": r.table_name, "row_pk": r.row_pk, "action": r.action,
            "actor": r.actor, "diff": r.diff, "new_config_version": r.new_config_version,
        }
        for r in rows
    ]


@router.get("/devices/{device_id}/preview-config")
def preview_config(device_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    blob = assemble_config(db, device_id)
    if blob is None:
        raise HTTPException(404, "device not found")
    # Never return the raw blob to a browser: it carries the Telegram bot token
    # and RTSP URLs with embedded passwords.
    return redact(blob)
