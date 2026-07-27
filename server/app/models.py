"""
models.py -- SQLAlchemy ORM models for the cfg_* config tables.

These mirror server/schema.sql exactly. The schema is created by running
schema.sql in the Supabase SQL editor (see server/README.md); these models are
used by the app to read/write those tables. The existing `detections` table is
NOT modelled here -- it is queried read-only with lightweight Core SQL in the
admin router.
"""

from sqlalchemy import (
    Column, Text, Integer, Boolean, Float, ForeignKey,
    UniqueConstraint, BigInteger, TIMESTAMP, func,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
import uuid

from .db import Base


class Device(Base):
    __tablename__ = "cfg_devices"
    device_id = Column(Text, primary_key=True)
    display_name = Column(Text, nullable=False, default="")
    device_token = Column(Text, nullable=False)
    # Fleet layout -- must match provision.sh and edge/config_client.py DEFAULTS.
    # Devices created from the dashboard get this default, so a stale value here
    # silently produces a box that records nowhere useful.
    storage_root = Column(Text, nullable=False, default="/mnt/data/video_storage")
    rfdetr_engine_path = Column(Text)
    clip_engine_path = Column(Text)
    clip2_checkpoint = Column(Text)
    clip2_model_dir = Column(Text)
    clip_img_input_size = Column(Integer, nullable=False, default=256)
    rf_nms_iou = Column(Float, nullable=False, default=0.7)
    rf_predict_threshold = Column(Float, nullable=False, default=0.25)
    tailscale_ip = Column(Text)
    restart_required = Column(Boolean, nullable=False, default=False)
    config_version = Column(Text)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Camera(Base):
    __tablename__ = "cfg_cameras"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(Text, ForeignKey("cfg_devices.device_id", ondelete="CASCADE"), nullable=False)
    cam_name = Column(Text, nullable=False)
    cam_index = Column(Integer, nullable=False)
    rtsp_url = Column(Text, nullable=False)
    codec = Column(Text, nullable=False, default="h265")
    enabled = Column(Boolean, nullable=False, default=True)
    motion_min_frames = Column(Integer)
    motion_threshold = Column(Integer)
    motion_kernel_w = Column(Integer)
    motion_kernel_h = Column(Integer)
    motion_area_min = Column(Integer)
    roi_polygon = Column(JSONB, nullable=False, default=list)
    roi_drawn_w = Column(Integer, nullable=False, default=1080)
    roi_drawn_h = Column(Integer, nullable=False, default=720)
    ignore_zones = Column(JSONB, nullable=False, default=list)
    ignore_drawn_w = Column(Integer, nullable=False, default=1920)
    ignore_drawn_h = Column(Integer, nullable=False, default=1080)
    reference_frame_url = Column(Text)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("device_id", "cam_name", name="uq_cfg_cameras_dev_cam"),
        # cam_index is the edge's live-view addressing key (live.py builds
        # /snapshot/{cam_index}), so a duplicate silently makes one camera
        # unreachable and shows another camera's feed under its label.
        UniqueConstraint("device_id", "cam_index", name="uq_cfg_cameras_dev_idx"),
    )


class ClassSetting(Base):
    __tablename__ = "cfg_class_settings"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(Text, ForeignKey("cfg_devices.device_id", ondelete="CASCADE"), nullable=False)
    class_name = Column(Text, nullable=False)
    rf_class_id = Column(Integer)
    rf_thresh = Column(Float, nullable=False)
    color_b = Column(Integer, nullable=False, default=255)
    color_g = Column(Integer, nullable=False, default=255)
    color_r = Column(Integer, nullable=False, default=255)
    clip_min_logit = Column(Float)
    is_clip_alert_class = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=False, default=0)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("device_id", "class_name", name="uq_cfg_class_dev_name"),)


class ClipPrompt(Base):
    __tablename__ = "cfg_clip_prompts"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(Text, ForeignKey("cfg_devices.device_id", ondelete="CASCADE"), nullable=False)
    class_name = Column(Text, nullable=False)
    positive_prompts = Column(JSONB, nullable=False, default=list)
    fallbacks = Column(JSONB, nullable=False, default=dict)
    cross_species = Column(Text)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("device_id", "class_name", name="uq_cfg_prompt_dev_name"),)


class ClipDistractors(Base):
    __tablename__ = "cfg_clip_distractors"
    device_id = Column(Text, ForeignKey("cfg_devices.device_id", ondelete="CASCADE"), primary_key=True)
    distractors = Column(JSONB, nullable=False, default=list)
    fallback_min_logit = Column(Float, nullable=False, default=27.0)
    bbox_pad = Column(Float, nullable=False, default=0.30)
    min_crop_w = Column(Integer, nullable=False, default=45)
    min_crop_h = Column(Integer, nullable=False, default=45)
    min_rf_conf = Column(Float, nullable=False, default=0.40)
    clip_cooldown = Column(Float, nullable=False, default=5.0)
    # False on an 8 GB board: the resident PyTorch text tower costs 2.0-2.5 GB.
    keep_text_tower_resident = Column(Boolean, nullable=False, default=False)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class GlobalSettings(Base):
    __tablename__ = "cfg_global_settings"
    device_id = Column(Text, ForeignKey("cfg_devices.device_id", ondelete="CASCADE"), primary_key=True)
    roi_start_hour = Column(Integer, nullable=False, default=18)
    roi_end_hour = Column(Integer, nullable=False, default=6)
    roi_end_minute = Column(Integer, nullable=False, default=31)
    detection_res_w = Column(Integer, nullable=False, default=1280)
    detection_res_h = Column(Integer, nullable=False, default=720)
    polygons_drawn_res_w = Column(Integer, nullable=False, default=1080)
    polygons_drawn_res_h = Column(Integer, nullable=False, default=720)
    # Record at 720p, not camera-native. Orin Nano has no hardware encoder, so
    # native 1080p means 2.25x the CPU and 2.25x the writer-queue RAM.
    motion_video_res_w = Column(Integer, default=1280)
    motion_video_res_h = Column(Integer, default=720)
    cooldown_specific = Column(Integer, nullable=False, default=3)
    cooldown_generic = Column(Integer, nullable=False, default=15)
    motion_close_delay = Column(Integer, nullable=False, default=8)
    motion_min_clip_duration = Column(Integer, nullable=False, default=30)
    motion_max_clip_duration = Column(Integer, nullable=False, default=3600)
    animal_rotate_sec = Column(Integer, nullable=False, default=10)
    fps = Column(Float, nullable=False, default=10.0)
    inference_target_fps = Column(Float, nullable=False, default=5.0)
    min_confirm_frames = Column(Integer, nullable=False, default=18)
    buffer_window = Column(Integer, nullable=False, default=25)
    animal_min_confirm_frames = Column(Integer, nullable=False, default=30)
    animal_buffer_window = Column(Integer, nullable=False, default=40)
    min_bbox_ratio = Column(Float, nullable=False, default=0.0026)
    cleanup_days_to_keep = Column(Integer, nullable=False, default=2)
    cleanup_low_space_free_percent = Column(Float, nullable=False, default=10)
    heartbeat_interval_sec = Column(Integer, nullable=False, default=60)
    poll_interval_sec = Column(Integer, nullable=False, default=30)
    motion_default_min_frames = Column(Integer, nullable=False, default=5)
    motion_default_threshold = Column(Integer, nullable=False, default=20)
    motion_default_kernel_w = Column(Integer, nullable=False, default=7)
    motion_default_kernel_h = Column(Integer, nullable=False, default=7)
    motion_default_area_min = Column(Integer, nullable=False, default=300)
    supabase_signal_enabled = Column(Boolean, nullable=False, default=True)
    detailed_telegram_msg = Column(Boolean, nullable=False, default=True)
    send_fallback_telegram = Column(Boolean, nullable=False, default=False)
    ignore_zones_enabled = Column(Boolean, nullable=False, default=True)
    animal_supabase_trigger = Column(Boolean, nullable=False, default=True)
    telegram_chat_id = Column(Text)
    # NULL means "inherit the fleet-wide token from cfg_fleet_settings".
    telegram_bot_token = Column(Text)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class FleetSettings(Base):
    """Singleton row (id is always 1) holding fleet-wide values.

    The Telegram bot token lives here so rotation is one edit for the whole
    fleet instead of one per device. GlobalSettings.telegram_bot_token
    overrides it for a device that runs its own bot.
    """

    __tablename__ = "cfg_fleet_settings"
    id = Column(Integer, primary_key=True, default=1)
    telegram_bot_token = Column(Text)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class DeterrenceGlobal(Base):
    __tablename__ = "cfg_deterrence_global"
    device_id = Column(Text, ForeignKey("cfg_devices.device_id", ondelete="CASCADE"), primary_key=True)
    relay_mode = Column(Text, nullable=False, default="both")
    audio_playback_mode = Column(Text, nullable=False, default="mixed")
    relay_ch1_pin = Column(Integer, nullable=False, default=11)
    relay_ch2_pin = Column(Integer, nullable=False, default=13)
    relay_active_low = Column(Boolean, nullable=False, default=True)
    usb_relay_port = Column(Text, nullable=False, default="/dev/ttyUSB0")
    usb_relay_baud = Column(Integer, nullable=False, default=9600)
    usb_on_cmd_hex = Column(Text, nullable=False, default="A00101A2")
    usb_off_cmd_hex = Column(Text, nullable=False, default="A00100A1")
    blink_total_duration = Column(Integer, nullable=False, default=30)
    blink_active_phase = Column(Integer, nullable=False, default=10)
    blink_rest_phase = Column(Integer, nullable=False, default=3)
    blink_interval = Column(Float, nullable=False, default=0.1)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class DeterrenceTarget(Base):
    __tablename__ = "cfg_deterrence_targets"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(Text, ForeignKey("cfg_devices.device_id", ondelete="CASCADE"), nullable=False)
    class_name = Column(Text, nullable=False)
    action = Column(Text, nullable=False, default="none")
    audio_files = Column(JSONB, nullable=False, default=list)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("device_id", "class_name", name="uq_cfg_deter_dev_name"),)


class DeviceStatus(Base):
    __tablename__ = "cfg_device_status"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(Text, ForeignKey("cfg_devices.device_id", ondelete="CASCADE"), nullable=False)
    received_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    cameras_alive = Column(Integer)
    cameras_total = Column(Integer)
    per_camera = Column(JSONB)
    config_version = Column(Text)
    pending_restart = Column(Boolean)
    app_uptime_sec = Column(BigInteger)
    disk_free_percent = Column(Float)
    notes = Column(Text)


class AuditLog(Base):
    __tablename__ = "cfg_audit_log"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(Text)
    table_name = Column(Text)
    row_pk = Column(Text)
    action = Column(Text)
    actor = Column(Text)
    diff = Column(JSONB)
    new_config_version = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
