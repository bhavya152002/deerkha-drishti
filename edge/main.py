# DHEERKHA DRISHTI -- Jetson 24/7 edge deployment build
# Base: your original 8cam_rtsp_clip_ts_gstreamer.py (RF-DETR PyTorch + CLIP PyTorch)
# This build: RF-DETR TensorRT engine + CLIP TensorRT image encoder,
#             hardened GStreamer camera class, per-device Telegram identity,
#             Separated Cooldowns, Async Video Saving, Min/Max Duration logic,
#             and LOCAL Jetson deterrence trigger (GPIO relay + audio),
#             fired in-process the instant an alert confirms -- no DB polling.
#             Supabase is used purely for logging/dashboard history now.
#
# THIS VERSION -- three targeted changes, nothing else touched:
#   1. RF cooldown (cooldown_until_specific) is now PER-SPECIES, same
#      pattern as CLIP's existing _clip_cd dict. Previously it was one
#      shared value across ALL species -- so an RF alert on wild boar
#      would block deer (or any other species) from re-triggering RF for
#      COOLDOWN_TIME_SPECIFIC seconds, and vice versa. This is the most
#      likely real explanation for RF/CLIP mismatches you saw (CLIP
#      confirming a species that RF's own log never separately alerted on,
#      because RF's alert for it got cooldown-blocked by a DIFFERENT
#      species' recent alert).
#   2. Generic fallback (buffalo/goat, tiger<->leopard) no longer sends
#      Telegram by default -- SEND_FALLBACK_TELEGRAM=False. Supabase
#      logging still ALWAYS fires for these regardless, so edge-trigger
#      history/dashboard is never missed even with Telegram muted.
#   3. NEW: deer <-> wild boar cross-species check. At night these two can
#      look visually similar in infrared. If CLIP rejects one, it now
#      immediately tries scoring the crop as the OTHER real species
#      (before falling through to the generic buffalo/goat prompts). If
#      THAT verifies, it's treated as a genuine confirmed species (not a
#      generic distractor bucket) -- Telegram ALWAYS sends for this case,
#      regardless of SEND_FALLBACK_TELEGRAM, exactly as requested.

import os
os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "-8"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"

# ---- LOGGING FIRST -----------------------------------------------------------
# .env then log_setup, BOTH before any pipeline import. Two reasons:
#   1. deterrence.init_hardware() runs a few lines after the config snapshot is
#      taken (below), so its "relay ready" / "relay init failed" lines -- the
#      exact evidence you need when the deterrent doesn't fire -- are emitted
#      before any later logging config would exist. They must land in the file.
#   2. log_setup reads LOG_LEVEL/LOG_DIR from the environment, so .env has to be
#      loaded first for those knobs to work.
from dotenv import load_dotenv
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_env_path):
    load_dotenv(_env_path)
else:
    load_dotenv()

import logging
import log_setup
log_setup.setup(app="deerkha")

import cv2
import numpy as np
import time
import socket
import threading
import requests
import json
import datetime
import queue
from sqlalchemy import create_engine, text

import torch
import open_clip
from PIL import Image

from rfdetr_trt import RFDETR_TRT
from clip_trt import CLIPImageEncoderTRT
from gst_camera_stream_jetson import GstCameraStream
# MUST precede the trigger_jetson_3 import: that module imports Jetson.GPIO at
# ITS import, and Jetson.GPIO resolves the board ONCE, at import, from
# /proc/device-tree/compatible.
#
# This board reports  nvidia,p3768-0000+p3767-0005-super
# Jetson.GPIO 2.1.7 knows nvidia,p3768-0000+p3767-0005      (no "-super")
#
# The lookup is an exact match, so the Orin Nano *Super* raises "Could not
# determine Jetson model" at import -- _GPIO_AVAILABLE goes False and the GPIO
# relay is skipped for the whole process lifetime. Confirmed on jetson-forest-01
# (JetPack R36.4.7) on 2026-08-01, where it had never once fired.
#
# JETSON_MODEL_NAME is Jetson.GPIO's own documented override (gpio_pin_data.py,
# get_model()). JETSON_ORIN_NANO and JETSON_ORIN_NX share one pin table
# (JETSON_ORIN_NX_PIN_DEFS), so this is correct for both. setdefault, not
# assignment: /etc/deerkha/device.env still wins on a board that needs another
# value, and .env is already loaded above.
os.environ.setdefault("JETSON_MODEL_NAME", "JETSON_ORIN_NANO")

import trigger_jetson_3 as deterrence

def _require_env(name):
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"Check that a .env file with this key exists next to this script."
        )
    return val

log = logging.getLogger("DHEERKHA")

# Bounded logging for per-frame hot paths -- see log_setup.Throttle. Without this
# an error inside the inference loop would emit ~10 lines/sec/camera and fill the
# disk this change is meant to protect.
THROTTLE = log_setup.Throttle(log, burst=3, every=60.0)

# Slower throttle for network faults. A server or Tailscale outage lasts hours,
# and the heartbeat/poll loops retry every 30-60s -- one line per 5 minutes is
# enough to establish "still down" without burying everything else.
NET_THROTTLE = log_setup.Throttle(log, burst=3, every=300.0)

# Machine-readable event history (logs/events.jsonl), rotated and bounded.
EVENTS = log_setup.event_logger()

# Last storage-cleanup result, surfaced in the heartbeat notes (set by cleanup_loop).
_CLEANUP_STATUS = "cleanup=never"


def _event(kind, **kw):
    """Append one JSON line of detection/cleanup history. Deliberately NOT called
    per frame -- only on alerts and cleanup passes."""
    try:
        EVENTS.info(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "dev": os.environ.get("JETSON_DEVICE_ID", ""),
            "kind": kind, **kw,
        }, default=str))
    except Exception:
        pass

JETSON_DEVICE_ID = os.environ.get("JETSON_DEVICE_ID", socket.gethostname())
os.environ["JETSON_DEVICE_ID"] = JETSON_DEVICE_ID  # share identity with config_client
log.info(f"Device identity for alerts: {JETSON_DEVICE_ID}")

# ---- DYNAMIC CONFIG ----------------------------------------------------------
# All operational settings now come from the office server (polled over Tailscale)
# via config_client. At boot cfg is the on-disk last-known-good cache, or the
# hardcoded DEFAULTS if there is no cache -- so the device ALWAYS starts. The poll
# daemon (started in __main__) upgrades to live server config and hot-applies the
# live-appliable values through the listener registered below.
from config_client import STORE, start_config_client
from stream_server import start_stream_server, update_stream_map, viewer_active

def _cfg():
    """Current immutable config snapshot (atomic, lock-free read)."""
    return STORE.snapshot

_boot_cfg = _cfg()

# Deterrence hardware, FIRST -- before the TensorRT engines, the cameras or any
# thread that could produce a detection. init_hardware() binds the restart-class
# relay settings (mode, GPIO pins, USB port, on/off commands) from this snapshot
# and only then opens the backends, which is why the module no longer does it at
# import: at import time config_client has not been imported yet, so there is
# nothing to configure from and every one of those settings was hardcoded.
try:
    deterrence.init_hardware(_boot_cfg["deterrence"])
except Exception:
    log.exception("[DETERRENCE] hardware init FAILED -- the deterrent will not fire")

# ---- static (restart-class) binds: fixed for the process lifetime ----
_CAMS_CFG     = _boot_cfg["cameras"]
CAMERAS       = [(c["rtsp_url"], c["codec"]) for c in _CAMS_CFG]
RTSP_URLS     = [c[0] for c in CAMERAS]
CAMERA_CODECS = [c[1] for c in CAMERAS]
CAMERA_NAMES  = [c["cam_name"] for c in _CAMS_CFG]

STORAGE_ROOT  = _boot_cfg["paths"]["storage_root"]
_ROI_POLYGONS_DRAWN_AT_RES = tuple(_boot_cfg["polygons_drawn_at_res"])
DETECTION_RES = tuple(_boot_cfg["detection_res"])   # masks precomputed at this res

def get_motion_settings(cam_name):
    """Live per-camera motion settings, read from the current snapshot."""
    snap = STORE.snapshot
    for c in snap["cameras"]:
        if c["cam_name"] == cam_name:
            m = c["motion"]
            return {"min_frames": m["min_frames"], "threshold": m["threshold"],
                    "kernel": tuple(m["kernel"]), "area_min": m["area_min"]}
    m = snap["motion_default"]
    return {"min_frames": m["min_frames"], "threshold": m["threshold"],
            "kernel": tuple(m["kernel"]), "area_min": m["area_min"]}

# ---- flags (live) ----
SUPABASE_SIGNAL_ENABLED = _boot_cfg["flags"]["supabase_signal_enabled"]
DETAILED_TELEGRAM_MSG   = _boot_cfg["flags"]["detailed_telegram_msg"]


def _resolve_from_config(flags, key, env_name):
    """Server config wins when non-empty, else the local env, else "".

    Both the Telegram bot token and the chat id are now delivered in the config
    blob so they can be rotated from the dashboard instead of by SSH-ing to
    every box. Two rules matter here and both have bitten before:

    1. NEVER hard-require these at boot. A freshly imaged box has no cache and
       no server values yet -- it gets them from its FIRST config poll. Failing
       at import instead means the process dies before it can ever poll, and
       systemd crash-loops a box that would have configured itself in 30 s.
    2. Fall back to the ENV, not to the previous runtime value. Falling back to
       the live global (which this code used to do for the chat id) means
       clearing the field on the dashboard silently keeps alerting to the OLD
       chat -- exactly wrong when a site changes hands.

    Empty is a legitimate outcome: it means Telegram is not configured yet, and
    _send_tg_photo() degrades to a warning instead of posting to a tokenless
    URL.
    """
    v = (flags.get(key) or "").strip()
    if v:
        return v
    return (os.environ.get(env_name) or "").strip()


# Delivered by the server (cfg_fleet_settings, or a per-device override). The
# env values remain as a fallback for a box that has never reached the server.
TELEGRAM_BOT_TOKEN = _resolve_from_config(_boot_cfg["flags"], "telegram_bot_token",
                                          "TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = _resolve_from_config(_boot_cfg["flags"], "telegram_chat_id",
                                          "TELEGRAM_CHAT_ID")
if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
    log.warning("[TELEGRAM] not configured at boot (token=%s chat_id=%s) -- alerts will be "
                "logged but not sent until the server delivers them.",
                "set" if TELEGRAM_BOT_TOKEN else "MISSING",
                "set" if TELEGRAM_CHAT_ID else "MISSING")

DATABASE_URL = _require_env("DATABASE_URL")
# pool_pre_ping is NOT optional here, and this bug is easy to miss because it
# only shows up once per night.
#
# Detections arrive in bursts separated by hours of quiet. SQLAlchemy's default
# pool hands back whatever connection it cached, and after that idle gap the
# server (or any NAT/firewall in between) has long since dropped it. The first
# INSERT of the evening therefore raises on a dead socket -- and
# send_supabase_alert() catches it and routes the message through a throttled
# logger, so the row is silently LOST and only Telegram fires. The detection
# that opens the night is exactly the one you most want in the history.
#
# pre_ping costs one cheap round-trip per checkout and turns that into a
# transparent reconnect. recycle bounds how stale a pooled connection can get.
# The small pool is deliberate: a field box should never be a meaningful
# consumer of the database's connection budget.
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=2,
    max_overflow=3,
)

# engine/model paths + CLIP input size are restart-class (bound once at boot)
RFDETR_ENGINE_PATH  = _boot_cfg["paths"]["rfdetr_engine_path"]
CLIP_ENGINE_PATH    = _boot_cfg["paths"]["clip_engine_path"]
CLIP_IMG_INPUT_SIZE = _boot_cfg["paths"]["clip_img_input_size"]

IGNORE_ZONES_ENABLED = _boot_cfg["flags"]["ignore_zones_enabled"]

def _build_roi_zones_from_cfg(snap):
    """{roi_zone_key -> [np.array(polygon)]} from the config's per-camera ignore
    zones (native camera resolution). Key is cam_name without spaces, matching
    CameraStream.roi_zone_key. Replaces the old roi_zones.json loader -- ignore
    zones are now server-managed."""
    zones = {}
    for c in snap["cameras"]:
        key = c["cam_name"].replace(" ", "")
        polys = c.get("ignore_zones") or []
        if polys:
            zones[key] = [np.array(p, dtype=np.int32) for p in polys]
    total = sum(len(v) for v in zones.values())
    if total:
        log.info(f"[ROI] Loaded {total} ignore polygon(s) across {len(zones)} camera(s) from config.")
    return zones

def _scale_polys(polys, from_w, from_h, to_w, to_h):
    if not polys:
        return []
    sx, sy = to_w / from_w, to_h / from_h
    return [(poly.astype(np.float32) * [sx, sy]).astype(np.int32) for poly in polys]

def _filter_ignore_zone(dets, polys_detres):
    if not polys_detres or len(dets) == 0:
        return dets
    keep_mask = np.ones(len(dets), dtype=bool)
    for i, box in enumerate(dets.xyxy):
        x1, y1, x2, y2 = box
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        for poly in polys_detres:
            if cv2.pointPolygonTest(poly, (float(cx), float(cy)), False) >= 0:
                keep_mask[i] = False
                break
    if keep_mask.all():
        return dets
    DetClass = type(dets)
    return DetClass(dets.xyxy[keep_mask], dets.confidence[keep_mask], dets.class_id[keep_mask])

def _build_rf_input_mask(polys_detres, w, h):
    if not polys_detres:
        return None
    mask = np.full((h, w), 255, dtype=np.uint8)
    cv2.fillPoly(mask, polys_detres, 0)
    return mask

def _apply_rf_input_mask(frame_bgr, mask):
    if mask is None:
        return frame_bgr
    return cv2.bitwise_and(frame_bgr, frame_bgr, mask=mask)

_roi_zones = _build_roi_zones_from_cfg(_boot_cfg) if IGNORE_ZONES_ENABLED else {}

# ---- cooldowns / durations (LIVE: rebound by the config listener) ----
# Per-species RF cooldown pattern is unchanged; only the source of the numbers
# moved to the server config.
COOLDOWN_TIME_SPECIFIC = _boot_cfg["cooldowns"]["specific"]
COOLDOWN_TIME_GENERIC  = _boot_cfg["cooldowns"]["generic"]
MOTION_CLOSE_DELAY     = _boot_cfg["cooldowns"]["motion_close_delay"]

MOTION_MIN_CLIP_DURATION = _boot_cfg["recording"]["motion_min_clip_duration"]
MOTION_MAX_CLIP_DURATION = _boot_cfg["recording"]["motion_max_clip_duration"]
ANIMAL_ROTATE_SEC        = _boot_cfg["recording"]["animal_rotate_sec"]

CLEANUP_DAYS_TO_KEEP = _boot_cfg["cleanup"]["days_to_keep"]
CLEANUP_LOW_SPACE_FREE_PERCENT = _boot_cfg["cleanup"]["low_space_free_percent"]

# Mutable state lives OUTSIDE the code directory. Under the fleet layout the
# code is deployed to an immutable, per-release directory
# (/opt/deerkha/releases/<tag>, symlinked as .../current), so anything written
# next to the script would be lost on the next deploy -- and would make the
# release directory differ between boxes, which defeats the point of shipping
# one identical artifact. Falls back to the script directory for bench runs
# from a checkout.
_STATE_DIR = os.environ.get("DD_STATE_DIR", "/var/lib/deerkha")
try:
    os.makedirs(_STATE_DIR, exist_ok=True)
except OSError:
    _STATE_DIR = os.path.dirname(os.path.abspath(__file__))
_cleanup_cfg_path = os.path.join(_STATE_DIR, "cleanup_config.json")

def _write_cleanup_config():
    """Rewrite cleanup_config.json from current config, so a MANUAL run of
    storage_cleanup.py agrees with what the pipeline is actually doing.

    storage_root is written from the BOUND module global, not from the live
    snapshot: storage_root is restart-class, so between a dashboard path change
    and the next restart the pipeline is still recording to the old root. Writing
    the live value here would point a manual cleanup run at a directory nothing
    is filling, while the one that IS filling goes unmanaged.
    """
    try:
        snap = STORE.snapshot
        with open(_cleanup_cfg_path, "w") as _f:
            json.dump({
                "storage_root": STORAGE_ROOT,
                "days_to_keep": snap["cleanup"]["days_to_keep"],
                "low_space_free_percent": snap["cleanup"]["low_space_free_percent"],
            }, _f, indent=2)
    except Exception as _e:
        log.warning(f"[CLEANUP] Could not write {_cleanup_cfg_path}: {_e}")

_write_cleanup_config()

_mv = _boot_cfg.get("motion_video_res")
MOTION_VIDEO_RES = tuple(_mv) if _mv else None

def _get_motion_fourcc():
    test_path = "/tmp/_fourcc_test.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"avc1")
    test_writer = cv2.VideoWriter(test_path, fourcc, 10.0, (64, 64))
    ok = test_writer.isOpened()
    test_writer.release()
    try:
        os.remove(test_path)
    except OSError:
        pass
    if ok:
        log.info("[VIDEO] Motion video codec: H.264 (avc1) -- phone/browser compatible.")
        return fourcc
    log.warning("[VIDEO] H.264 (avc1) encoder not available on this build -- falling back to mp4v.")
    return cv2.VideoWriter_fourcc(*"mp4v")

MOTION_FOURCC = _get_motion_fourcc()

def is_roi_active():
    nw = STORE.snapshot["night_window"]
    now = datetime.datetime.now()
    t = now.hour * 60 + now.minute
    return t >= nw["start_hour"] * 60 or t < nw["end_hour"] * 60 + nw["end_minute"]

# Date folders the camera threads are CURRENTLY writing into. The cleanup thread
# reads this and refuses to delete them -- so the "don't rmtree a live folder"
# guarantee comes from what the pipeline actually did, not from two processes
# agreeing on night-window date arithmetic. Small and self-pruning: it can only
# ever hold today's and (during the night window) yesterday's date.
_ACTIVE_DATE_DIRS = set()
_active_dates_lock = threading.Lock()

# Memo for get_dynamic_paths, keyed (camera_name, date_str, time_of_day). The
# capture loop calls it on EVERY frame, and it used to issue 5 os.makedirs()
# syscalls plus a lock acquire each time -- ~600 syscalls/sec at 8 cameras,
# fired into the same ext4 journal the video writers are queued on, where tail
# latency is milliseconds rather than microseconds.
# Only two or three keys per camera are ever live (today, and during the night
# window yesterday), so the dict is pruned to the newest few per camera.
_paths_memo = {}
_paths_memo_lock = threading.Lock()


def get_dynamic_paths(camera_name):
    nw = STORE.snapshot["night_window"]
    now = datetime.datetime.now()
    if now.hour < nw["end_hour"] or (now.hour == nw["end_hour"] and now.minute < nw["end_minute"]):
        effective_date = now - datetime.timedelta(days=1)
    else:
        effective_date = now
    date_str = effective_date.strftime("%Y-%m-%d")
    time_of_day = "NIGHT" if is_roi_active() else "DAY"

    # Fast path: same camera, same date, same half of the day -> the folders
    # already exist and the answer cannot have changed.
    memo_key = (camera_name, date_str, time_of_day)
    cached = _paths_memo.get(memo_key)
    if cached is not None:
        return cached

    # Slow path: runs on the first frame of a camera, and again at each date /
    # day-night rollover. _ACTIVE_DATE_DIRS is updated here rather than on
    # every frame because the cleanup thread only needs to know which date
    # folders are being written into, which changes at exactly these moments.
    with _active_dates_lock:
        _ACTIVE_DATE_DIRS.add(effective_date.date())
        if len(_ACTIVE_DATE_DIRS) > 3:
            # Keep only the newest few; older entries are no longer being written.
            for _old in sorted(_ACTIVE_DATE_DIRS)[:-3]:
                _ACTIVE_DATE_DIRS.discard(_old)

    base_root = os.path.join(STORAGE_ROOT, date_str, time_of_day, camera_name.replace(' ', '_'))
    paths = {
        "save": os.path.join(base_root, "detection_frames"),
        "motion": os.path.join(base_root, "motion_video"),
        "animal": os.path.join(base_root, "animal_video"),
        "confirmed": os.path.join(base_root, "animal_detection_confirmation"),
        "clip": os.path.join(base_root, "clip_validation"),
    }
    for p in paths.values():
        os.makedirs(p, exist_ok=True)

    with _paths_memo_lock:
        _paths_memo[memo_key] = paths
        # Bound the memo. Keys are (cam, date, DAY|NIGHT); a long-running box
        # would otherwise accumulate one per camera per half-day forever.
        if len(_paths_memo) > 64:
            for _k in sorted(_paths_memo, key=lambda k: (k[1], k[0], k[2]))[:-32]:
                _paths_memo.pop(_k, None)
    return paths

# Process-wide count of frames dropped from recordings. Surfaced in the heartbeat
# so the dashboard shows encoder backpressure instead of it being invisible.
_TOTAL_FRAME_DROPS = 0
_drop_lock = threading.Lock()


class AsyncVideoWriter:
    def __init__(self, filename, fourcc, fps, frameSize):
        self.filename = filename
        self.writer = cv2.VideoWriter(filename, fourcc, fps, frameSize)
        self.frameSize = frameSize
        self.q = queue.Queue(maxsize=50)
        self.stopped = False
        self.dropped = 0
        self.thread = threading.Thread(
            target=self._write_loop, daemon=True,
            name=f"vw-{os.path.basename(filename)[:24]}")
        self.thread.start()

    def _write_loop(self):
        try:
            while True:
                frame = self.q.get()
                if frame is None:
                    break
                fh, fw = frame.shape[:2]
                if (fw, fh) != self.frameSize:
                    frame = cv2.resize(frame, self.frameSize)
                self.writer.write(frame)
        except Exception:
            log.exception("[VIDEO] writer loop died for %s", self.filename)
        finally:
            try:
                self.writer.release()
            except Exception:
                log.exception("[VIDEO] release failed for %s", self.filename)

    def write(self, frame):
        if not self.stopped:
            try:
                self.q.put(frame, block=False)
            except queue.Full:
                # The encoder can't keep up. Frames are being lost from the
                # recording -- previously this was a bare `pass`, so a camera
                # could silently record half of what it saw.
                global _TOTAL_FRAME_DROPS
                self.dropped += 1
                with _drop_lock:
                    _TOTAL_FRAME_DROPS += 1
                THROTTLE.warning("vw.drop",
                                 "[VIDEO] writer queue full -- FRAME DROPPED from recording")

    def release(self, wait=False, timeout=5.0):
        self.stopped = True
        self.q.put(None)
        if wait:
            self.thread.join(timeout=timeout)
        if self.dropped:
            log.warning("[VIDEO] %d frame(s) dropped from %s", self.dropped, self.filename)
            if self.thread.is_alive():
                log.warning(f"[VIDEO] writer thread did not finish within {timeout}s "
                            f"(file may still be finalizing in the background)")

def _color_tuple(c):
    return tuple(int(x) for x in c)

def _build_class_settings(snap):
    return {name: {"thresh": v["thresh"], "color": _color_tuple(v["color"])}
            for name, v in snap["class_settings"].items()}

CLASS_SETTINGS = _build_class_settings(_boot_cfg)

MIN_CONFIRM_FRAMES        = _boot_cfg["confirm"]["min_confirm_frames"]
BUFFER_WINDOW             = _boot_cfg["confirm"]["buffer_window"]
ANIMAL_MIN_CONFIRM_FRAMES = _boot_cfg["confirm"]["animal_min_confirm_frames"]
ANIMAL_BUFFER_WINDOW      = _boot_cfg["confirm"]["animal_buffer_window"]

# How long a published inference result stays usable by the capture loop.
# The inference loop only runs in power mode; the capture loop runs always.
# Without this bound, the last label published before motion ended is read
# forever, so a camera keeps "confirming" a frozen frame and keeps writing
# video, stills, Telegram photos, Supabase rows and deterrence cycles with
# nothing in front of it.
INFER_STALE_SEC = float(os.environ.get("DD_INFER_STALE_SEC", "1.5"))

# Target inference rate per camera. The loop used to run flat out (sleep 0.01)
# and re-infer the same frame at up to ~100 Hz; every camera in power mode was
# competing for the GPU to recompute an answer it already had. Server-settable
# so a box can be tuned down without a redeploy. Clamped: 0 or negative would
# busy-spin, and above ~30 there is no headroom left for anything else.
def _inference_period():
    try:
        fps = float(STORE.get("inference.target_fps", 5.0) or 5.0)
    except (TypeError, ValueError):
        fps = 5.0
    return 1.0 / min(max(fps, 0.5), 30.0)

def _infer_stale_limit():
    """Staleness bound for a published inference result. Derived from the
    pacing period so lowering target_fps can never make a healthy camera look
    stale to the capture loop."""
    return max(INFER_STALE_SEC, _inference_period() * 3.0)

# Local OpenCV preview window. OFF by default: it costs ~1-1.5 CPU cores at 8
# cameras, needs an X server (DISPLAY=:0), and shows a window nobody is sitting
# in front of at a field site. Turn on only for bench work.
DISPLAY_ENABLED = os.environ.get("DD_DISPLAY", "0") == "1"

# id->name map for RF-DETR classes, from class settings that carry an rf_class_id.
# MUST match the engine export order (verified server-side against MY_CLASSES).
MY_CLASSES = {v["rf_class_id"]: name for name, v in _boot_cfg["class_settings"].items()
              if v.get("rf_class_id") is not None}
W, H, LOG_WIDTH = 640, 360, 300
detection_log = []

def _build_roi_structures(snap):
    """Per-camera scaled detection-ROI polygons (at DETECTION_RES) + their masks,
    from the config. Each camera's polygon is scaled from its own roi_drawn_at_res.
    Cameras with no polygon get a permissive full-frame mask."""
    raw_scaled, masks = {}, {}
    dw, dh = DETECTION_RES
    for c in snap["cameras"]:
        name = c["cam_name"]
        pts = c.get("roi_polygon") or []
        fw, fh = c.get("roi_drawn_at_res", [dw, dh])
        if len(pts) >= 3:
            arr = (np.array(pts, dtype=np.float32) * [dw / fw, dh / fh]).astype(np.int32)
            raw_scaled[name] = arr
            masks[name] = cv2.fillPoly(np.zeros((dh, dw), dtype=np.uint8), [arr], 255)
        else:
            raw_scaled[name] = np.zeros((0, 2), dtype=np.int32)
            masks[name] = np.full((dh, dw), 255, dtype=np.uint8)
    return raw_scaled, masks

ROI_POLYGONS_RAW, ROI_MASKS = _build_roi_structures(_boot_cfg)
FRAME_AREA = DETECTION_RES[0] * DETECTION_RES[1]
MIN_BBOX_RATIO = _boot_cfg["min_bbox_ratio"]
ANIMAL_SUPABASE_TRIGGER = _boot_cfg["flags"]["animal_supabase_trigger"]

def classify_detection(label, x1, y1, x2, y2, roi_mask):
    if label == "person":
        return "person"
    if not is_roi_active():
        return label
    x1c, y1c = max(0, x1), max(0, y1)
    x2c, y2c = min(DETECTION_RES[0]-1, x2), min(DETECTION_RES[1]-1, y2)
    if x2c <= x1c or y2c <= y1c:
        return "Animal"
    overlaps = cv2.countNonZero(roi_mask[y1c:y2c, x1c:x2c]) > 0
    big_enough = ((x2-x1)*(y2-y1)) / FRAME_AREA >= MIN_BBOX_RATIO
    return label if (overlaps and big_enough) else "Animal"

def _safe_imwrite(path, img):
    try:
        ok = cv2.imwrite(path, img)
        # DEBUG on success: this fires several times per second per camera during
        # an event and would otherwise dominate the log.
        if ok:
            log.debug(f"[SAVE] OK {os.path.basename(path)}")
        else:
            log.error(f"[SAVE] FAILED (cv2.imwrite returned False, disk full or bad path?) {path}")
    except Exception:
        log.exception(f"[SAVE] {path}")

def _send_tg_photo(caption, img):
    # Read both once: a config apply can rebind them mid-call, and posting a new
    # token with the old chat id is a confusing 400 to debug.
    token, chat_id = TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    if not (token and chat_id):
        # Not configured yet (fresh box that hasn't completed its first poll, or
        # the token was cleared). Degrade instead of POSTing to a tokenless URL,
        # which returns 404 for every alert and buries the real cause.
        log.warning("[TELEGRAM] skipped -- not configured (%s): %s",
                    "no token" if not token else "no chat_id", caption.replace("\n", " ")[:120])
        return
    try:
        _, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"},
            files={"photo": ("alert.jpg", enc.tobytes(), "image/jpeg")},
            timeout=15)
        log.info(f"[TELEGRAM] {'OK' if r.ok else r.status_code}")
    except Exception as e:
        log.warning(f"[TELEGRAM] {e}")

def send_supabase_alert(label, source, conf):
    if not SUPABASE_SIGNAL_ENABLED:
        return
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO detections (timestamp, source, message, type, animals, raw_timestamp) "
                "VALUES (:ts, :src, :msg, :type, :animals, :raw_ts)"),
                {"ts": datetime.datetime.utcnow(), "src": f"SYSTEM / {source}",
                 "msg": f"DETECTION: {label.upper()} at {source} [{JETSON_DEVICE_ID}]",
                 "type": "critical" if label.lower() in ["leopard", "elephant", "tiger", "wild boar", "deer"] else "warning",
                 "animals": json.dumps({label: 1}), "raw_ts": time.time()})
            conn.commit()
    except Exception:
        NET_THROTTLE.exception("supabase.insert", "[SUPABASE ERROR] detection insert failed")

log.info(f"Loading RF-DETR TensorRT engine: {RFDETR_ENGINE_PATH}")
model = RFDETR_TRT(RFDETR_ENGINE_PATH, apply_nms=True, nms_iou=_boot_cfg["paths"]["rf_nms_iou"])
# NOTE: deliberately NO external inference lock here. RFDETR_TRT._infer()
# already serializes the TensorRT context ops internally (rfdetr_trt.py), at
# exactly the right granularity -- it holds its lock across
# set_input_shape/execute_async_v3/stream.synchronize() and allocates its
# output tensors inside. Wrapping predict() in a second, coarser lock also
# serialized the ~10 ms of numpy preprocess (resize/normalize/transpose),
# which is pure CPU work that should run concurrently across cameras.
# Do not re-add one.

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

CLIP2_MODEL_DIR = _boot_cfg["paths"]["clip2_model_dir"]
CLIP2_CHECKPOINT = _boot_cfg["paths"]["clip2_checkpoint"]

# ---- CLIP verification config (LIVE; rebound by the config listener) ----
# Per-(camera,species) CLIP cooldown behavior is unchanged; only the numbers now
# come from the server config. Prompt/fallback/distractor edits additionally
# trigger a text-feature cache rebuild (see rebuild_clip_text_cache below).
CLIP_CLASS_MIN_LOGIT    = dict(_boot_cfg["clip"]["class_min_logit"])
CLIP_FALLBACK_MIN_LOGIT = _boot_cfg["clip"]["fallback_min_logit"]
CLIP_ALERT_CLASSES      = set(_boot_cfg["clip"]["alert_classes"])
CLIP_COOLDOWN           = _boot_cfg["clip"]["cooldown"]
CLIP_BBOX_PAD           = _boot_cfg["clip"]["bbox_pad"]
CLIP_MIN_CROP_W         = _boot_cfg["clip"]["min_crop_w"]
CLIP_MIN_CROP_H         = _boot_cfg["clip"]["min_crop_h"]
CLIP_MIN_RF_CONF        = _boot_cfg["clip"]["min_rf_conf"]

# Fallback Telegram gating: generic fallback (buffalo/goat, tiger<->leopard)
# confirmations only send Telegram when True. Supabase logging for these ALWAYS
# fires regardless (see clip_verify_and_alert).
SEND_FALLBACK_TELEGRAM = _boot_cfg["flags"]["send_fallback_telegram"]

# deer <-> wild boar cross-species check (still a genuine confirmed species when
# it matches -- Telegram always sends for a cross-match, regardless of the gate).
CROSS_SPECIES_FALLBACK = dict(_boot_cfg["clip"]["cross_species"])

CLIP_PROMPTS     = {k: list(v) for k, v in _boot_cfg["clip"]["prompts"].items()}
CLIP_FALLBACKS   = {k: {fk: list(fv) for fk, fv in v.items()} for k, v in _boot_cfg["clip"]["fallbacks"].items()}
CLIP_DISTRACTORS = list(_boot_cfg["clip"]["distractors"])

log.info("Loading MobileCLIP-S2 text tower (PyTorch, one-time cost) ...")
if not os.path.isfile(CLIP2_CHECKPOINT):
    raise FileNotFoundError(f"MobileCLIP-S2 not found: {CLIP2_CHECKPOINT}")

_clip_model, _, _clip_preprocess = open_clip.create_model_and_transforms(
    "MobileCLIP-S2", pretrained=CLIP2_CHECKPOINT, device=DEVICE,
    image_mean=(0.0, 0.0, 0.0), image_std=(1.0, 1.0, 1.0),
    image_interpolation="bilinear", image_resize_mode="shortest",
)
_clip_tokenizer = open_clip.get_tokenizer("MobileCLIP-S2")
_clip_model.eval()
try:
    from open_clip.modules.mobileone import reparameterize_model
    _clip_model = reparameterize_model(_clip_model)
    log.info("CLIP reparameterization applied.")
except ImportError:
    pass

log.info(f"Loading CLIP TensorRT image engine: {CLIP_ENGINE_PATH}")
clip_img_trt = CLIPImageEncoderTRT(CLIP_ENGINE_PATH, input_size=CLIP_IMG_INPUT_SIZE)

# ---- CLIP text-feature cache (rebuildable when prompts/distractors change) ----
# The text tower stays RESIDENT (per config) so prompt edits can rebuild the
# cache live without reloading the model. If keep_text_tower_resident is False we
# drop it after the first build and lazily reload only when a rebuild is needed.
_clip_text_model = _clip_model
_clip_text_tokenizer = _clip_tokenizer
_clip_cache: dict = {}
_clip_fb_cache: dict = {}
_clip_cache_lock = threading.Lock()

def _load_text_tower():
    m, _, _ = open_clip.create_model_and_transforms(
        "MobileCLIP-S2", pretrained=CLIP2_CHECKPOINT, device=DEVICE,
        image_mean=(0.0, 0.0, 0.0), image_std=(1.0, 1.0, 1.0),
        image_interpolation="bilinear", image_resize_mode="shortest")
    tok = open_clip.get_tokenizer("MobileCLIP-S2")
    m.eval()
    try:
        from open_clip.modules.mobileone import reparameterize_model
        m = reparameterize_model(m)
    except ImportError:
        pass
    return m, tok

def _encode_prompts(model_t, tok_t, positive, distractors):
    _all = list(positive) + list(distractors)
    _tok = tok_t(_all).to(DEVICE)
    with torch.no_grad():
        _f = model_t.encode_text(_tok)
        _f /= _f.norm(dim=-1, keepdim=True)
    return {"positive": list(positive), "prompts": _all, "feats_np": _f.detach().cpu().numpy()}

def rebuild_clip_text_cache(prompts, fallbacks, distractors):
    """(Re)compute _clip_cache / _clip_fb_cache from prompt lists, swapping them by
    reference under a lock so an in-flight _clip_score finishes on the old cache."""
    global _clip_cache, _clip_fb_cache, _clip_text_model, _clip_text_tokenizer
    resident = bool(STORE.get("clip.keep_text_tower_resident", True))
    model_t, tok_t = _clip_text_model, _clip_text_tokenizer
    reloaded = False
    if model_t is None or tok_t is None:
        log.info("[CLIP] reloading text tower for prompt-cache rebuild ...")
        model_t, tok_t = _load_text_tower()
        reloaded = True

    new_cache = {c: _encode_prompts(model_t, tok_t, pos, distractors) for c, pos in prompts.items()}
    new_fb = {}
    for c, fb_dict in fallbacks.items():
        new_fb[c] = {lbl: _encode_prompts(model_t, tok_t, fp, distractors) for lbl, fp in fb_dict.items()}

    with _clip_cache_lock:
        _clip_cache = new_cache
        _clip_fb_cache = new_fb

    if resident:
        _clip_text_model, _clip_text_tokenizer = model_t, tok_t
    else:
        _clip_text_model, _clip_text_tokenizer = None, None
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass
    log.info(f"[CLIP] text cache rebuilt ({len(new_cache)} classes; tower "
             f"{'resident' if resident else 'released'}{'; reloaded' if reloaded else ''}).")

log.info("Building CLIP text feature cache ...")
rebuild_clip_text_cache(CLIP_PROMPTS, CLIP_FALLBACKS, CLIP_DISTRACTORS)

del _clip_preprocess
if not bool(STORE.get("clip.keep_text_tower_resident", True)):
    _clip_text_model = None
    _clip_text_tokenizer = None
    try:
        del _clip_model, _clip_tokenizer
    except Exception:
        pass
torch.cuda.empty_cache()

_clip_sema = threading.Semaphore(2)
_clip_cd: dict = {}
_clip_cd_lock = threading.Lock()

def _clahe(bgr):
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    cl = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    lab[:, :, 0] = cl.apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

def _make_crop(frame_bgr, x1, y1, x2, y2):
    fh, fw = frame_bgr.shape[:2]
    bw, bh = x2 - x1, y2 - y1
    px, py = int(bw * CLIP_BBOX_PAD), int(bh * CLIP_BBOX_PAD)
    cx1, cy1 = max(0, x1 - px), max(0, y1 - py)
    cx2, cy2 = min(fw, x2 + px), min(fh, y2 + py)
    crop = frame_bgr[cy1:cy2, cx1:cx2]
    if crop.size == 0 or crop.shape[0] < CLIP_MIN_CROP_H or crop.shape[1] < CLIP_MIN_CROP_W:
        return None
    return _clahe(crop)

def _clip_score(crop_bgr, cls):
    cache = _clip_cache.get(cls)
    if cache is None:
        return {"verified": False, "reason": f"no cache for {cls}"}
    try:
        img_f = clip_img_trt.encode_image(crop_bgr)
    except Exception as e:
        THROTTLE.exception("clip.encode", "[CLIP-TRT] encode failed -- verification "
                                          "is effectively OFF while this repeats")
        return {"verified": False, "reason": f"trt encode error: {e}"}

    sim = 100.0 * (img_f @ cache["feats_np"].T)
    sim = sim[0]
    exp = np.exp(sim - np.max(sim))
    probs = exp / exp.sum()
    best_i = int(probs.argmax())
    prompt = cache["prompts"][best_i]
    raw = float(sim[best_i])
    is_pos = prompt in cache["positive"]
    thresh = CLIP_CLASS_MIN_LOGIT.get(cls, 24.0)
    if not np.isfinite(raw):
        return {"verified": False, "prompt": prompt, "raw_logit": raw, "reason": "non-finite logit"}
    if not is_pos:
        return {"verified": False, "prompt": prompt, "raw_logit": raw, "reason": f"distractor won: '{prompt}'"}
    if raw < thresh:
        return {"verified": False, "prompt": prompt, "raw_logit": raw, "reason": f"logit {raw:.1f} < {thresh}"}
    return {"verified": True, "prompt": prompt, "raw_logit": raw, "reason": None}

def _clip_fallback(crop_bgr, primary):
    try:
        img_f = clip_img_trt.encode_image(crop_bgr)
    except Exception:
        THROTTLE.exception("clip.encode.fb", "[CLIP-TRT] fallback encode failed")
        return {"verified": False, "fallback_label": None}

    for fb_lbl, fb_cache in _clip_fb_cache.get(primary, {}).items():
        sim = 100.0 * (img_f @ fb_cache["feats_np"].T)
        sim = sim[0]
        best_i = int(sim.argmax())
        prompt = fb_cache["prompts"][best_i]
        raw = float(sim[best_i])
        is_hit = prompt in fb_cache["positive"]
        if not np.isfinite(raw):
            continue
        if is_hit and raw >= CLIP_FALLBACK_MIN_LOGIT:
            return {"verified": True, "fallback_label": fb_lbl, "prompt": prompt, "raw_logit": raw}
    return {"verified": False, "fallback_label": None}

def clip_verify_and_alert(cam_name, label, crop, rf_frame, rf_conf, paths, animal_count):
    ts = time.strftime("%Y%m%d_%H%M%S")
    with _clip_sema:
        # NB: no embedded "\n" separators here. A newline inside a log record
        # produces a line with no timestamp/level prefix, which breaks grep, tail
        # and any line-oriented parsing of the file. ">>>" / "<<<" bracket the
        # block instead, and two of these can interleave (Semaphore(2)) -- the
        # threadName in the format string tells them apart.
        log.info(f"[CLIP] >>> {label.upper()} | {cam_name} | rf={rf_conf:.2f} | n={animal_count} | dev={JETSON_DEVICE_ID}")
        result = _clip_score(crop, label)

        if result["verified"]:
            log.info(f"[CLIP] CONFIRMED | prompt='{result['prompt']}' | logit={result['raw_logit']:.1f}")
            _event("clip_confirmed", cam=cam_name, label=label, rf_conf=round(rf_conf, 3),
                   logit=round(result["raw_logit"], 2), prompt=result["prompt"], n=animal_count)
            _safe_imwrite(os.path.join(paths["clip"], f"CONFIRMED_{cam_name}_{label}_{ts}.jpg"), crop)
            _safe_imwrite(os.path.join(paths["confirmed"], f"{cam_name}_{label}_CLIP_{ts}.jpg"), rf_frame)
            caption = (
                f"CLIP CONFIRMED\nDevice: {JETSON_DEVICE_ID}\n{cam_name}\n"
                f"{label.upper()}\n"
                f"Prompt: {result['prompt']}\n"
                f"Logit: {result['raw_logit']:.1f} | RF: {rf_conf:.2f}\n"
                f"Count: {animal_count}"
            ) if DETAILED_TELEGRAM_MSG else f"{label.upper()} | {cam_name} | {JETSON_DEVICE_ID} | n={animal_count}"
            threading.Thread(target=_send_tg_photo, args=(caption, rf_frame), daemon=True).start()
            threading.Thread(target=send_supabase_alert, args=(label, cam_name, rf_conf), daemon=True).start()
            deterrence.trigger_deterrence(label)

        else:
            log.info(f"[CLIP] REJECTED {label}: {result.get('reason')} -- trying fallback")

            # ---- FIX (item 3): deer <-> wild boar cross-species check ----
            # Runs BEFORE the generic buffalo/goat fallback. Still a real
            # confirmed species if this matches, so Telegram always sends.
            cross_lbl = CROSS_SPECIES_FALLBACK.get(label)
            cross_result = _clip_score(crop, cross_lbl) if cross_lbl else None

            if cross_result is not None and cross_result["verified"]:
                log.info(f"[CLIP] CROSS-MATCH {label.upper()} -> {cross_lbl.upper()} | "
                         f"prompt='{cross_result['prompt']}' | logit={cross_result['raw_logit']:.1f}")
                _event("clip_crossmatch", cam=cam_name, rf_label=label, label=cross_lbl,
                       rf_conf=round(rf_conf, 3), logit=round(cross_result["raw_logit"], 2))
                _safe_imwrite(os.path.join(paths["clip"], f"CROSSMATCH_{cross_lbl}_{cam_name}_{ts}.jpg"), crop)
                _safe_imwrite(os.path.join(paths["confirmed"], f"{cam_name}_{cross_lbl}_CROSSMATCH_{ts}.jpg"), rf_frame)
                caption = (
                    f"CLIP CROSS-MATCH\nDevice: {JETSON_DEVICE_ID}\n{cam_name}\n"
                    f"RF:{label.upper()} -> CLIP:{cross_lbl.upper()}\n"
                    f"Logit: {cross_result['raw_logit']:.1f} | RF: {rf_conf:.2f}\n"
                    f"Count: {animal_count}"
                ) if DETAILED_TELEGRAM_MSG else f"{cross_lbl.upper()} (cross-match) | {cam_name} | {JETSON_DEVICE_ID}"
                # ALWAYS sends Telegram -- genuine species, not a generic bucket.
                threading.Thread(target=_send_tg_photo, args=(caption, rf_frame), daemon=True).start()
                threading.Thread(target=send_supabase_alert, args=(cross_lbl, cam_name, rf_conf), daemon=True).start()
                deterrence.trigger_deterrence(cross_lbl)

            else:
                # ---- Generic fallback (buffalo/goat, tiger<->leopard) ----
                fb = _clip_fallback(crop, label)
                if fb["verified"]:
                    fb_lbl = fb["fallback_label"]
                    log.info(f"[CLIP] FALLBACK -> {fb_lbl.upper()} | logit={fb['raw_logit']:.1f}")
                    _event("clip_fallback", cam=cam_name, rf_label=label, label=fb_lbl,
                           rf_conf=round(rf_conf, 3), logit=round(fb["raw_logit"], 2))
                    _safe_imwrite(os.path.join(paths["clip"], f"FALLBACK_{fb_lbl}_{cam_name}_{ts}.jpg"), crop)
                    _safe_imwrite(os.path.join(paths["confirmed"], f"{cam_name}_{fb_lbl}_FALLBACK_{ts}.jpg"), rf_frame)
                    # ---- FIX (item 2): Supabase ALWAYS logs here regardless
                    # of Telegram gating -- edge-trigger history is never
                    # missed even while Telegram is muted for these. ----
                    threading.Thread(target=send_supabase_alert, args=(fb_lbl, cam_name, rf_conf), daemon=True).start()
                    if SEND_FALLBACK_TELEGRAM:
                        caption = (
                            f"CLIP FALLBACK\nDevice: {JETSON_DEVICE_ID}\n{cam_name}\n"
                            f"RF:{label.upper()} -> CLIP:{fb_lbl.upper()}\n"
                            f"Logit: {fb['raw_logit']:.1f} | RF: {rf_conf:.2f}\n"
                            f"Count: {animal_count}"
                        ) if DETAILED_TELEGRAM_MSG else f"{fb_lbl.upper()} fallback | {cam_name} | {JETSON_DEVICE_ID}"
                        threading.Thread(target=_send_tg_photo, args=(caption, rf_frame), daemon=True).start()
                    deterrence.trigger_deterrence(fb_lbl)
                else:
                    log.info("[CLIP] FALLBACK REJECTED")
                    _event("clip_rejected", cam=cam_name, label=label, rf_conf=round(rf_conf, 3),
                           reason=result.get("reason"))
                    _safe_imwrite(os.path.join(paths["clip"], f"REJECTED_{cam_name}_{label}_{ts}.jpg"), crop)

        log.info(f"[CLIP] <<< {label.upper()} | {cam_name} done")

class CameraStream:
    def __init__(self, url, name, codec="h265"):
        self.url, self.name, self.codec = url, name, codec
        self.roi_zone_key = self.name.replace(" ", "")
        self.roi_polygon = ROI_POLYGONS_RAW.get(name, np.zeros((0, 2), dtype=np.int32))
        self.roi_mask = ROI_MASKS.get(name, np.full((DETECTION_RES[1], DETECTION_RES[0]), 255, dtype=np.uint8))
        self.motion_cfg = get_motion_settings(self.name)
        self.cap = GstCameraStream(url, name, codec=codec).start()
        time.sleep(1.0)
        self.orig_w = self.cap.frame_w or 1920
        self.orig_h = self.cap.frame_h or 1080

        self._rebuild_ignore_zones()

        self.fps = float(STORE.get("recording.fps", 10.0))
        self._cfg_generation = STORE.generation
        self._last_frame_ts = 0.0
        self.frame = np.zeros((H, W, 3), np.uint8)
        self.static_back = None
        self.is_power_mode = False
        self.last_motion_time = 0
        self.stopped = False
        self.night_val = 0.0

        # ---- FIX (item 1): RF cooldown now per-species, same pattern as
        # CLIP's _clip_cd. Was previously a single shared float that let
        # one species' alert block a DIFFERENT species' RF alert from
        # firing for COOLDOWN_TIME_SPECIFIC seconds. ----
        self.cooldown_until_specific = {}   # {label: expiry_time}
        self.cooldown_until_generic = 0
        self.motion_counter = 0

        self.detection_history = []
        self.motion_hits = 0
        self.detection_history_animal = []
        self.motion_hits_animal = 0

        self.motion_writer = None
        self.motion_start_time = 0
        self.animal_writer = None
        self.last_a_vid = 0

        self._latest_frame = None
        self._latest_frame_lock = threading.Lock()
        self._last_frame_fingerprint = None
        self._last_inferred_label = ""
        self._last_inferred_conf = 0.0
        self._last_inferred_dets = None
        self._last_ai_input = None
        self._last_annotated = None
        self._last_best_bbox = (0, 0, 0, 0)
        self._last_animal_count = 1
        # Wall-clock of the last published inference result. The capture loop
        # runs regardless of power mode, so without this it would keep reading
        # a label the inference loop stopped refreshing minutes ago -- see the
        # staleness guard in _capture_loop.
        self._last_infer_ts = 0.0
        # Pacing + dedupe state for _inference_loop.
        self._last_infer_started = 0.0
        self._last_infer_src = None

    def get_writer(self, folder, suffix, width, height, fourcc=None):
        ts = time.strftime("%Y%m%d_%H%M%S")
        fn = os.path.join(folder, f"{self.name}_{suffix}_{ts}.mp4")
        if fourcc is None:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        return AsyncVideoWriter(fn, fourcc, self.fps, (width, height))

    def _rebuild_ignore_zones(self):
        _raw_zone_polys = _roi_zones.get(self.roi_zone_key, [])
        self.ignore_zone_polys_detres = (
            _scale_polys(_raw_zone_polys, self.orig_w, self.orig_h, DETECTION_RES[0], DETECTION_RES[1])
            if IGNORE_ZONES_ENABLED else []
        )
        self.rf_input_mask = (
            _build_rf_input_mask(self.ignore_zone_polys_detres, DETECTION_RES[0], DETECTION_RES[1])
            if IGNORE_ZONES_ENABLED else None
        )

    def reload_dynamic(self):
        """Re-point this camera's derived state after a config change. The global
        listener has already rebuilt ROI_POLYGONS_RAW/ROI_MASKS/_roi_zones and the
        CLIP cache; here we just re-read this camera's slice. Cheap; runs once per
        config generation, not per frame."""
        try:
            self.motion_cfg = get_motion_settings(self.name)
            self.roi_polygon = ROI_POLYGONS_RAW.get(self.name, self.roi_polygon)
            self.roi_mask = ROI_MASKS.get(self.name, self.roi_mask)
            self._rebuild_ignore_zones()
        except Exception as e:
            log.warning(f"[{self.name}] reload_dynamic failed: {e}")
        finally:
            self._cfg_generation = STORE.generation

    def start(self):
        threading.Thread(target=self._capture_loop, daemon=True, name=f"cap-{self.name}").start()
        threading.Thread(target=self._inference_loop, daemon=True, name=f"inf-{self.name}").start()
        return self

    def _capture_loop(self):
        no_frame_since = time.time()
        reconnect_backoff = 2
        while not self.stopped:
            if STORE.generation != self._cfg_generation:
                self.reload_dynamic()
            ret, frame = self.cap.read()
            if not ret:
                if time.time() - no_frame_since < 8:
                    time.sleep(0.2)
                    continue
                log.warning(f"[{self.name}] no frames for 8s -- reconnecting")
                try:
                    self.cap.stop()
                except Exception:
                    log.debug(f"[{self.name}] stop() during reconnect failed", exc_info=True)
                time.sleep(reconnect_backoff)
                reconnect_backoff = min(reconnect_backoff * 2, 30)
                self.cap = GstCameraStream(self.url, self.name, codec=self.codec).start()
                self._last_frame_fingerprint = None
                no_frame_since = time.time()
                continue

            fingerprint = frame[::40, ::40, 0]
            if self._last_frame_fingerprint is not None and np.array_equal(fingerprint, self._last_frame_fingerprint):
                time.sleep(0.005)
                continue
            # .copy() matters: the slice is a strided VIEW, so holding it would
            # pin the entire ~6 MB parent frame alive until the next distinct
            # frame arrives. The copy is ~1.3 KB.
            self._last_frame_fingerprint = fingerprint.copy()

            no_frame_since = time.time()
            reconnect_backoff = 2

            now = time.time()
            self._last_frame_ts = now
            paths = get_dynamic_paths(self.name)

            # stop() may have fired while we were reading a frame. Bail before
            # the recording block below, which would otherwise open a NEW video
            # writer on a camera that is being torn down -- leaking its thread
            # and leaving an unfinalized mp4.
            if self.stopped:
                break

            with self._latest_frame_lock:
                # Republish by rebinding, same contract as the GStreamer layer:
                # this array is never written to in place, so consumers (the
                # inference loop, the stream server) can hold the reference.
                self._latest_frame = frame

            res_small = cv2.resize(frame, (W, H))
            gray = cv2.cvtColor(res_small, cv2.COLOR_BGR2GRAY)
            self.night_val = 0.15 if np.mean(gray) < 120 else 0.0

            blurred = cv2.GaussianBlur(gray, self.motion_cfg["kernel"], 0)
            if self.static_back is None:
                self.static_back = blurred.copy().astype("float")
                continue
            cv2.accumulateWeighted(blurred, self.static_back, 0.02)
            diff = cv2.absdiff(cv2.convertScaleAbs(self.static_back), blurred)
            _, tm = cv2.threshold(diff, self.motion_cfg["threshold"], 255, cv2.THRESH_BINARY)
            m_raw = any(
                cv2.contourArea(c) > self.motion_cfg["area_min"]
                for c in cv2.findContours(cv2.dilate(tm, None, iterations=2), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0])

            if m_raw:
                self.motion_counter += 1
            else:
                self.motion_counter = max(0, self.motion_counter - 1)

            if self.motion_counter >= self.motion_cfg["min_frames"]:
                self.last_motion_time = now
                self.is_power_mode = True
            elif (now - self.last_motion_time) > MOTION_CLOSE_DELAY:
                self.is_power_mode = False

            motion_target_size = MOTION_VIDEO_RES if MOTION_VIDEO_RES else (self.orig_w, self.orig_h)

            if self.is_power_mode:
                if not self.motion_writer:
                    self.motion_writer = self.get_writer(
                        paths["motion"], "RAW", motion_target_size[0], motion_target_size[1],
                        fourcc=MOTION_FOURCC)
                    self.motion_start_time = now
                self.motion_writer.write(frame)

                if (now - self.motion_start_time) >= MOTION_MAX_CLIP_DURATION:
                    old_writer = self.motion_writer
                    self.motion_writer = self.get_writer(
                        paths["motion"], "RAW", motion_target_size[0], motion_target_size[1],
                        fourcc=MOTION_FOURCC)
                    self.motion_start_time = now
                    old_writer.release()
                    log.info(f"[{self.name}] motion clip rotated at {MOTION_MAX_CLIP_DURATION}s (still ongoing)")
            else:
                if self.motion_writer:
                    duration_so_far = now - self.motion_start_time
                    if MOTION_MIN_CLIP_DURATION > 0 and duration_so_far < MOTION_MIN_CLIP_DURATION:
                        self.motion_writer.write(frame)
                    else:
                        self.motion_writer.release()
                        self.motion_writer = None

            # Only trust the inference result while it is fresh. The inference
            # loop stops publishing the moment power mode drops (no motion), so
            # a stale result here would keep every downstream confirmation,
            # recording and alert path firing on a frozen frame indefinitely.
            _infer_fresh = (now - self._last_infer_ts) <= _infer_stale_limit()
            if _infer_fresh:
                best_label = self._last_inferred_label
                best_conf = self._last_inferred_conf
                best_bbox = self._last_best_bbox
                animal_count = self._last_animal_count
            else:
                best_label = ""
                best_conf = 0.0
                best_bbox = (0, 0, 0, 0)
                animal_count = 0
            ai_input = (self._last_ai_input if self._last_ai_input is not None else cv2.resize(frame, DETECTION_RES))
            annotated = (self._last_annotated if self._last_annotated is not None else ai_input.copy())

            roi_on = is_roi_active()
            if roi_on:
                cv2.polylines(res_small, [(self.roi_polygon * [W/DETECTION_RES[0], H/DETECTION_RES[1]]).astype(int)], True, (0, 165, 255), 1)

            if self.ignore_zone_polys_detres:
                for poly in self.ignore_zone_polys_detres:
                    disp_poly = (poly * [W / DETECTION_RES[0], H / DETECTION_RES[1]]).astype(int)
                    cv2.polylines(res_small, [disp_poly], True, (0, 0, 255), 1)

            det_this_frame = bool(best_label)
            det_this_frame_animal = (best_label == "Animal")

            self.detection_history.append(det_this_frame)
            if len(self.detection_history) > BUFFER_WINDOW:
                self.detection_history.pop(0)
            self.motion_hits = sum(self.detection_history)

            self.detection_history_animal.append(det_this_frame_animal)
            if len(self.detection_history_animal) > ANIMAL_BUFFER_WINDOW:
                self.detection_history_animal.pop(0)
            self.motion_hits_animal = sum(self.detection_history_animal)

            _clip_confirmed = (best_label in CLIP_ALERT_CLASSES and self.motion_hits >= MIN_CONFIRM_FRAMES)
            _animal_confirmed = (best_label == "Animal" and self.motion_hits_animal >= ANIMAL_MIN_CONFIRM_FRAMES)

            if (_clip_confirmed or _animal_confirmed) and best_label and best_label != "person":
                if not self.animal_writer:
                    self.animal_writer = self.get_writer(paths["animal"], best_label, DETECTION_RES[0], DETECTION_RES[1])
                    self.last_a_vid = now
                self.animal_writer.write(annotated)
                if (now - self.last_a_vid) > ANIMAL_ROTATE_SEC:
                    self.animal_writer.release()
                    self.animal_writer = self.get_writer(paths["animal"], best_label, DETECTION_RES[0], DETECTION_RES[1])
                    self.last_a_vid = now

                if _clip_confirmed and best_conf >= CLIP_MIN_RF_CONF:
                    key = (self.name, best_label)
                    with _clip_cd_lock:
                        if now >= _clip_cd.get(key, 0):
                            _clip_cd[key] = now + CLIP_COOLDOWN
                            crop = _make_crop(ai_input, *best_bbox)
                            if crop is not None:
                                threading.Thread(
                                    target=clip_verify_and_alert,
                                    args=(self.name, best_label, crop, annotated.copy(), best_conf, paths, animal_count),
                                    daemon=True,
                                ).start()

                if best_label == "Animal":
                    if now > self.cooldown_until_generic:
                        self._trigger_alert(annotated, best_label, best_conf, paths)
                else:
                    # ---- FIX (item 1): per-species RF cooldown lookup ----
                    if now > self.cooldown_until_specific.get(best_label, 0):
                        self._trigger_alert(annotated, best_label, best_conf, paths)
            else:
                if self.animal_writer:
                    self.animal_writer.release()
                    self.animal_writer = None
                    self.last_a_vid = now

            # Status overlay is purely cosmetic -- it exists for the local
            # preview window and for the dashboard's annotated live view. Skip
            # the drawing when nobody is looking. res_small itself is still
            # published either way, because stream_server falls back to
            # .frame for the annotated view outside a power-mode event.
            if DISPLAY_ENABLED or viewer_active():
                st_text, st_col = (("PWR", (0, 255, 0)) if self.is_power_mode else ("IDLE", (0, 165, 255)))
                cv2.rectangle(res_small, (0, 0), (230, 78), (0, 0, 0), -1)
                cv2.putText(res_small, f"{self.name} | {st_text}", (6, 22), 0, 0.62, st_col, 2)
                cv2.putText(res_small, f"HITS: {self.motion_hits}/{MIN_CONFIRM_FRAMES}", (6, 46), 0, 0.58, (255, 255, 255), 2)
                cv2.putText(res_small, f"NIGHT: +{self.night_val:.2f} {'ROI:ON' if roi_on else 'ROI:OFF'}", (6, 68), 0, 0.52, (0, 165, 255) if roi_on else (80, 80, 80), 2)
            self.frame = res_small

    def _inference_loop(self):
        while not self.stopped:
            if STORE.generation != self._cfg_generation:
                self.reload_dynamic()
            if not self.is_power_mode:
                # Motion ended -- stop vouching for the last result immediately
                # rather than waiting for it to age out. The staleness guard in
                # the capture loop is the backstop; this is the fast path.
                if self._last_inferred_label:
                    self._last_inferred_label = ""
                    self._last_inferred_conf = 0.0
                    self._last_best_bbox = (0, 0, 0, 0)
                    self._last_animal_count = 0
                    self._last_infer_ts = 0.0
                time.sleep(0.05)
                continue

            # Pace the loop. Without this it re-runs at up to ~100 Hz per
            # camera, bounded only by GPU contention, burning CPU and GPU to
            # produce a result identical to the one it just published.
            period = _inference_period()
            wait = period - (time.time() - self._last_infer_started)
            if wait > 0:
                time.sleep(min(wait, 0.05))
                continue

            # Take a reference, not a copy. The capture loop publishes by
            # rebinding _latest_frame to a fresh array every frame and never
            # mutates one in place, so the reference stays valid for as long
            # as we hold it.
            with self._latest_frame_lock:
                frame = self._latest_frame
            if frame is None:
                time.sleep(0.02)
                continue
            if frame is self._last_infer_src:
                # Capture hasn't produced a new frame yet -- inferring again
                # would re-derive the same answer from the same pixels.
                time.sleep(0.005)
                continue

            self._last_infer_started = time.time()
            self._last_infer_src = frame

            ai_input = cv2.resize(frame, DETECTION_RES)
            annotated = ai_input.copy()
            roi_on = is_roi_active()

            if roi_on:
                roi_ov = annotated.copy()
                cv2.fillPoly(roi_ov, [self.roi_polygon], (0, 165, 255))
                cv2.addWeighted(roi_ov, 0.08, annotated, 0.92, 0, annotated)
                cv2.polylines(annotated, [self.roi_polygon], True, (0, 165, 255), 2)

            if self.ignore_zone_polys_detres:
                for poly in self.ignore_zone_polys_detres:
                    cv2.polylines(annotated, [poly], True, (0, 0, 255), 2)

            rf_input = _apply_rf_input_mask(ai_input, self.rf_input_mask)

            try:
                dets = model.predict(cv2.cvtColor(rf_input, cv2.COLOR_BGR2RGB),
                                     threshold=STORE.get("paths.rf_predict_threshold", 0.25))
            except Exception:
                # Pre/post-process failure (resize, colour convert, NMS). Engine
                # failures are caught one layer down in rfdetr_trt.predict() and
                # never reach here -- both sites are instrumented.
                THROTTLE.exception(f"rf.predict.{self.name}",
                                   f"[{self.name}] RF-DETR predict failed")
                time.sleep(0.05)
                continue

            if IGNORE_ZONES_ENABLED:
                dets = _filter_ignore_zone(dets, self.ignore_zone_polys_detres)

            best_label = ""
            best_conf = 0.0
            best_bbox = (0, 0, 0, 0)
            label_counts: dict = {}

            if dets and dets.class_id is not None:
                for i in range(len(dets.class_id)):
                    lbl = MY_CLASSES.get(int(dets.class_id[i]), "Unknown")
                    conf = float(dets.confidence[i])
                    x1, y1, x2, y2 = dets.xyxy[i].astype(int)

                    disp_lbl = classify_detection(lbl, x1, y1, x2, y2, self.roi_mask)

                    thresh = CLASS_SETTINGS.get(disp_lbl, CLASS_SETTINGS["default"])["thresh"] + self.night_val
                    if conf < thresh:
                        continue

                    col = CLASS_SETTINGS.get(disp_lbl, CLASS_SETTINGS["default"])["color"]
                    cv2.rectangle(annotated, (x1, y1), (x2, y2), col, 2)
                    cv2.putText(annotated, f"{disp_lbl} {conf:.2f}", (x1, y1-10), 0, 0.7, col, 2)
                    label_counts[disp_lbl] = label_counts.get(disp_lbl, 0) + 1
                    if conf > best_conf:
                        best_conf = conf
                        best_label = disp_lbl
                        best_bbox = (x1, y1, x2, y2)

            self._last_inferred_label = best_label
            self._last_inferred_conf = best_conf
            self._last_inferred_dets = dets
            self._last_ai_input = ai_input
            self._last_annotated = annotated
            self._last_best_bbox = best_bbox
            self._last_animal_count = label_counts.get(best_label, 1)
            # Published last: the capture loop gates on this timestamp, so it
            # must never be newer than the values it vouches for.
            self._last_infer_ts = time.time()
            # No sleep here -- the pacing gate at the top of the loop owns the
            # cadence. Sleeping again would stack on top of it.

    def _trigger_alert(self, frame, label, conf, paths):
        ts_f = time.strftime("%Y%m%d_%H%M%S")
        ts_l = time.strftime("%H:%M:%S")
        detection_log.insert(0, f"[{ts_l}] {self.name}: {label.upper()}")
        if len(detection_log) > 10:
            detection_log.pop()

        if label == "Animal":
            self.cooldown_until_generic = time.time() + COOLDOWN_TIME_GENERIC
            _safe_imwrite(os.path.join(paths["save"], f"{self.name}_{label}_{ts_f}.jpg"), frame)
            _safe_imwrite(os.path.join(paths["confirmed"], f"{self.name}_{label}_DIRECT_{ts_f}.jpg"), frame)
            msg = f"ANIMAL DETECTED\nDevice: {JETSON_DEVICE_ID}\n{self.name}\nRF: {conf:.2f}\nUnclassified"
            threading.Thread(target=_send_tg_photo, args=(msg, frame), daemon=True).start()
            if ANIMAL_SUPABASE_TRIGGER:
                threading.Thread(target=send_supabase_alert, args=(label, self.name, conf), daemon=True).start()
            deterrence.trigger_deterrence(label)
        else:
            # ---- FIX (item 1): per-species RF cooldown set ----
            self.cooldown_until_specific[label] = time.time() + COOLDOWN_TIME_SPECIFIC
            _safe_imwrite(os.path.join(paths["save"], f"{self.name}_{label}_RF_{ts_f}.jpg"), frame)

    def stop(self):
        """Shut this camera down cleanly. Blocks up to ~10s draining both video
        writers so the last clip is FINALIZED rather than truncated -- which is
        why reconcile runs on its own thread and not the poll or display thread.

        The ordering matters now that removal is a routine runtime event, not
        just a process-exit path: _capture_loop can be sitting between the
        `stopped = True` write and its next loop check, and would otherwise
        create a BRAND NEW AsyncVideoWriter (thread + queue) on a stream nobody
        will ever release. Detach the writers before releasing them so the rotate
        path has nothing to attach to.
        """
        self.stopped = True
        time.sleep(0.25)                     # let both loops observe the flag
        mw, aw = self.motion_writer, self.animal_writer
        self.motion_writer = self.animal_writer = None
        if mw:
            mw.release(wait=True)
        if aw:
            aw.release(wait=True)
        try:
            self.cap.stop()
        except Exception as e:
            log.warning(f"[{self.name}] capture stop failed: {e}")


# =====================================================================
# CAMERA ROSTER -- copy-on-write
# =====================================================================
# The list is NEVER mutated in place. Only the 'cam-reconcile' thread rebinds the
# name, and readers take one atomic attribute read and iterate an immutable
# object -- the same discipline ConfigStore.snapshot uses. A lock would instead
# have to be held for the ~1s a CameraStream takes to construct and the ~10s
# stop() takes to drain, stalling the display loop and the heartbeat.
_STREAMS = []
_reconcile_lock = threading.Lock()      # serializes reconcile passes against each other
_reconcile_event = threading.Event()    # poll thread -> reconcile thread doorbell

MAX_CAMERAS = int(os.environ.get("DD_MAX_CAMERAS", "8"))
HOT_RECONCILE = os.environ.get("DD_HOT_RECONCILE", "1") != "0"


def current_streams():
    """Atomic read of the live roster. Safe to iterate without a lock."""
    return _STREAMS


_restart_lock = threading.Lock()
_restarting = False


# Display grid geometry, recomputed by reconcile whenever the roster changes.
_grid = {"cols": 1, "rows": 1}


def _refresh_grid_geometry(n):
    """Recompute display geometry for n cameras. Called only from the reconcile
    thread (the roster's single writer), so the display loop just reads."""
    import math
    n = max(1, n)
    cols = math.ceil(math.sqrt(n))
    _grid["cols"], _grid["rows"] = cols, math.ceil(n / cols)


def _desired_cameras(snap):
    """Ordered camera configs we SHOULD be running.

    This is where the `enabled` flag finally takes effect. It has existed in the
    DB, the config blob and the dashboard all along, but nothing on the edge ever
    read it -- so a camera you 'disabled' was still being decoded, motion
    detected, and recorded to disk.
    """
    out, seen = [], set()
    for c in (snap.get("cameras") or []):
        if not c.get("enabled", True):
            continue
        if not c.get("rtsp_url"):
            log.warning("[RECONCILE] camera %r has no rtsp_url; skipping", c.get("cam_name"))
            continue
        n = c.get("cam_name")
        if not n:
            continue
        if n in seen:
            log.warning("[RECONCILE] duplicate cam_name %r in config; keeping the first", n)
            continue
        seen.add(n)
        out.append(c)
    if len(out) > MAX_CAMERAS:
        log.error("[RECONCILE] config asks for %d cameras; capping at %d (Nano decode/TensorRT "
                  "budget). Dropped: %s", len(out), MAX_CAMERAS,
                  [c["cam_name"] for c in out[MAX_CAMERAS:]])
        out = out[:MAX_CAMERAS]
    return out


def _hot_repoint(s, c):
    """RTSP URL or codec changed on an EXISTING camera: swap just the GStreamer
    capture, exactly like the 8s-no-frames reconnect path already does. Keeps
    motion state, writers, cooldowns and the stream-server identity -- and never
    runs two hardware decoders for one camera at the same time."""
    old = (s.url, s.codec)
    s.url = c["rtsp_url"]
    s.codec = c.get("codec") or s.codec
    try:
        s.cap.stop()
    except Exception as e:
        log.warning("[RECONCILE] %s: stopping old capture failed: %s", s.name, e)
    s.cap = GstCameraStream(s.url, s.name, codec=s.codec).start()
    s._last_frame_fingerprint = None
    s.static_back = None                 # different scene -> motion baseline is meaningless
    time.sleep(1.0)
    s.orig_w = getattr(s.cap, "frame_w", None) or s.orig_w
    s.orig_h = getattr(s.cap, "frame_h", None) or s.orig_h
    try:
        s.reload_dynamic()               # rebuild ROI / ignore zones at the new size
    except Exception:
        log.exception("[RECONCILE] %s: reload_dynamic after repoint failed", s.name)
    log.warning("[RECONCILE] repointed %r: %s -> %s", s.name, old, (s.url, s.codec))


def _shutdown_stream(s, why):
    log.info("[RECONCILE] stopping camera %r: %s", s.name, why)
    try:
        s.stop()
    except Exception:
        log.exception("[RECONCILE] stop(%s) raised", s.name)


def reconcile_streams(snap=None, reason="config"):
    """Bring the running camera roster in line with the config snapshot.

    Runs ONLY on the 'cam-reconcile' thread, plus once on the main thread at boot
    before that thread exists -- single-writer either way. Boot goes through this
    same function deliberately, so the startup path and the hot-apply path can
    never drift apart.
    """
    global _STREAMS
    snap = snap or STORE.snapshot
    with _reconcile_lock:
        desired = _desired_cameras(snap)
        want = {c["cam_name"]: c for c in desired}
        have = {s.name: s for s in _STREAMS}

        # SAFETY: never reconcile ourselves down to zero cameras. A bad blob, a
        # half-finished dashboard edit or an accidental "disable all" must not
        # blind the site. The operator can still stop the process by hand.
        if not want and have:
            log.error("[RECONCILE] config yields ZERO runnable cameras -- REFUSING. "
                      "Keeping the current roster of %d. Fix the dashboard.", len(have))
            return [], [], []

        add_names = [n for n in want if n not in have]
        drop_names = [n for n in have if n not in want]
        repoints = [n for n in want if n in have
                    and (want[n]["rtsp_url"], want[n].get("codec") or have[n].codec)
                    != (have[n].url, have[n].codec)]
        if not (add_names or drop_names or repoints):
            return [], [], []

        # 1. Build the new streams FIRST. If one fails we still hold everything we
        #    already had -- the roster never goes backwards on an error.
        built = {}
        for n in add_names:
            try:
                built[n] = CameraStream(want[n]["rtsp_url"], n,
                                        codec=want[n].get("codec", "h265")).start()
                log.info("[RECONCILE] started camera %r", n)
            except Exception:
                log.exception("[RECONCILE] could not start %r -- will retry next pass", n)

        # 2. Publish the whole new roster in ONE atomic rebind, in config order.
        new_roster = [s for s in (built.get(c["cam_name"]) or have.get(c["cam_name"])
                                  for c in desired) if s is not None]
        _STREAMS = new_roster
        try:
            update_stream_map(new_roster, desired)
        except Exception:
            log.exception("[RECONCILE] updating the live-view index map failed")
        _refresh_grid_geometry(len(new_roster))

        # 3. Only NOW tear down what left the roster. No reader can still reach
        #    these through _STREAMS, so nothing ever observes a half-dead camera.
        for n in drop_names:
            _shutdown_stream(have[n], f"removed/disabled ({reason})")

        # 4. In-place URL/codec swaps, after publication -- identity is unchanged.
        for n in repoints:
            try:
                _hot_repoint(have[n], want[n])
            except Exception:
                log.exception("[RECONCILE] repoint %r failed", n)

        log.warning("[RECONCILE] +%d -%d ~%d => %d running: %s",
                    len(add_names), len(drop_names), len(repoints),
                    len(new_roster), [s.name for s in new_roster])
        return add_names, drop_names, repoints


def reconcile_loop():
    """Sole owner of CameraStream add/remove. Woken by the config listener, and
    also on a 60s timer so a build that failed (camera briefly unreachable, or a
    MAX_CAMERAS / zero-camera refusal) is retried without another dashboard
    edit."""
    while True:
        _reconcile_event.wait(timeout=60)
        _reconcile_event.clear()
        if not HOT_RECONCILE:
            continue
        try:
            reconcile_streams(STORE.snapshot)
        except Exception:
            log.exception("[RECONCILE] pass failed")


def _cameras_changed(new_blob, old_blob):
    """True if the camera SET or a structural per-camera field changed. ROI,
    ignore zones and motion sliders are excluded -- CameraStream.reload_dynamic()
    already picks those up on the generation bump."""
    def key(b):
        return [(c.get("cam_name"), bool(c.get("enabled", True)), c.get("rtsp_url"),
                 c.get("codec"), c.get("cam_index"))
                for c in (b.get("cameras") or [])]
    return key(new_blob) != key(old_blob)


def _request_restart():
    """Exit the process so systemd (Restart=always) relaunches it into the new
    config (already in the on-disk cache). Only the operator's restart signal
    reaches here; a bare value diff never restarts."""
    global _restarting
    with _restart_lock:
        if _restarting:
            return
        _restarting = True
    log.warning("[CONFIG] restart_required set by server -- exiting for systemd relaunch (3s)")

    def _do():
        time.sleep(3)
        for s in current_streams():
            try:
                s.stop()
            except Exception as e:
                log.warning("[RESTART] stream stop failed: %s", e)
        log.warning("[RESTART] exiting now for relaunch")
        # os._exit bypasses atexit and logging.shutdown(), so flush explicitly or
        # the last lines -- the ones explaining why we exited -- are lost.
        log_setup.flush()
        logging.shutdown()
        os._exit(0)

    threading.Thread(target=_do, daemon=True, name="restart").start()


def _on_config_change(new_blob, old_blob):
    """Live-apply everything that can change without a restart. Runs in the poll
    thread after each accepted config swap: rebinds module globals + rebuilds the
    ROI/CLIP/cleanup/deterrence derived state. CameraStream objects pick up their
    per-camera slice via the generation check in their loops."""
    global COOLDOWN_TIME_SPECIFIC, COOLDOWN_TIME_GENERIC, MOTION_CLOSE_DELAY
    global MOTION_MIN_CLIP_DURATION, MOTION_MAX_CLIP_DURATION, ANIMAL_ROTATE_SEC
    global CLEANUP_DAYS_TO_KEEP, CLEANUP_LOW_SPACE_FREE_PERCENT, MOTION_VIDEO_RES
    global CLASS_SETTINGS, MIN_CONFIRM_FRAMES, BUFFER_WINDOW
    global ANIMAL_MIN_CONFIRM_FRAMES, ANIMAL_BUFFER_WINDOW, MIN_BBOX_RATIO
    global ANIMAL_SUPABASE_TRIGGER, SUPABASE_SIGNAL_ENABLED, DETAILED_TELEGRAM_MSG
    global TELEGRAM_CHAT_ID, TELEGRAM_BOT_TOKEN, IGNORE_ZONES_ENABLED
    global CLIP_CLASS_MIN_LOGIT, CLIP_FALLBACK_MIN_LOGIT, CLIP_ALERT_CLASSES
    global CLIP_COOLDOWN, CLIP_BBOX_PAD, CLIP_MIN_CROP_W, CLIP_MIN_CROP_H, CLIP_MIN_RF_CONF
    global SEND_FALLBACK_TELEGRAM, CROSS_SPECIES_FALLBACK
    global CLIP_PROMPTS, CLIP_FALLBACKS, CLIP_DISTRACTORS
    global ROI_POLYGONS_RAW, ROI_MASKS, _roi_zones

    c = new_blob
    COOLDOWN_TIME_SPECIFIC = c["cooldowns"]["specific"]
    COOLDOWN_TIME_GENERIC = c["cooldowns"]["generic"]
    MOTION_CLOSE_DELAY = c["cooldowns"]["motion_close_delay"]
    MOTION_MIN_CLIP_DURATION = c["recording"]["motion_min_clip_duration"]
    MOTION_MAX_CLIP_DURATION = c["recording"]["motion_max_clip_duration"]
    ANIMAL_ROTATE_SEC = c["recording"]["animal_rotate_sec"]
    CLEANUP_DAYS_TO_KEEP = c["cleanup"]["days_to_keep"]
    CLEANUP_LOW_SPACE_FREE_PERCENT = c["cleanup"]["low_space_free_percent"]
    _mv = c.get("motion_video_res")
    MOTION_VIDEO_RES = tuple(_mv) if _mv else None
    CLASS_SETTINGS = _build_class_settings(c)
    MIN_CONFIRM_FRAMES = c["confirm"]["min_confirm_frames"]
    BUFFER_WINDOW = c["confirm"]["buffer_window"]
    ANIMAL_MIN_CONFIRM_FRAMES = c["confirm"]["animal_min_confirm_frames"]
    ANIMAL_BUFFER_WINDOW = c["confirm"]["animal_buffer_window"]
    MIN_BBOX_RATIO = c["min_bbox_ratio"]
    ANIMAL_SUPABASE_TRIGGER = c["flags"]["animal_supabase_trigger"]
    SUPABASE_SIGNAL_ENABLED = c["flags"]["supabase_signal_enabled"]
    DETAILED_TELEGRAM_MSG = c["flags"]["detailed_telegram_msg"]
    # Rotating the bot token or repointing the chat is a dashboard edit that
    # takes effect on the next poll -- no restart, no SSH. Resolve through the
    # same helper used at boot: an empty server value falls back to the ENV, not
    # to the value currently loaded, so clearing the field really does stop
    # alerts going to the old chat.
    _prev_token = TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID = _resolve_from_config(c["flags"], "telegram_chat_id", "TELEGRAM_CHAT_ID")
    TELEGRAM_BOT_TOKEN = _resolve_from_config(c["flags"], "telegram_bot_token", "TELEGRAM_BOT_TOKEN")
    if TELEGRAM_BOT_TOKEN != _prev_token:
        # Never log the token itself -- this line ends up in a rotated log file.
        log.info("[TELEGRAM] bot token %s from server config",
                 "updated" if TELEGRAM_BOT_TOKEN else "CLEARED")
    IGNORE_ZONES_ENABLED = c["flags"]["ignore_zones_enabled"]
    CLIP_CLASS_MIN_LOGIT = dict(c["clip"]["class_min_logit"])
    CLIP_FALLBACK_MIN_LOGIT = c["clip"]["fallback_min_logit"]
    CLIP_ALERT_CLASSES = set(c["clip"]["alert_classes"])
    CLIP_COOLDOWN = c["clip"]["cooldown"]
    CLIP_BBOX_PAD = c["clip"]["bbox_pad"]
    CLIP_MIN_CROP_W = c["clip"]["min_crop_w"]
    CLIP_MIN_CROP_H = c["clip"]["min_crop_h"]
    CLIP_MIN_RF_CONF = c["clip"]["min_rf_conf"]
    SEND_FALLBACK_TELEGRAM = c["flags"]["send_fallback_telegram"]
    CROSS_SPECIES_FALLBACK = dict(c["clip"]["cross_species"])

    # ROI + ignore zones (DETECTION_RES is restart-fixed -> mask shapes stable)
    try:
        ROI_POLYGONS_RAW, ROI_MASKS = _build_roi_structures(c)
        _roi_zones = _build_roi_zones_from_cfg(c) if IGNORE_ZONES_ENABLED else {}
    except Exception:
        log.exception("[CONFIG] ROI rebuild FAILED -- cameras keep their previous zones")

    # CLIP text cache: rebuild only when prompt-related content actually changed
    new_prompts = {k: list(v) for k, v in c["clip"]["prompts"].items()}
    new_fallbacks = {k: {fk: list(fv) for fk, fv in v.items()} for k, v in c["clip"]["fallbacks"].items()}
    new_distractors = list(c["clip"]["distractors"])
    if new_prompts != CLIP_PROMPTS or new_fallbacks != CLIP_FALLBACKS or new_distractors != CLIP_DISTRACTORS:
        CLIP_PROMPTS, CLIP_FALLBACKS, CLIP_DISTRACTORS = new_prompts, new_fallbacks, new_distractors
        try:
            rebuild_clip_text_cache(CLIP_PROMPTS, CLIP_FALLBACKS, CLIP_DISTRACTORS)
        except Exception:
            log.exception("[CONFIG] CLIP cache rebuild FAILED -- keeping previous prompts")

    _write_cleanup_config()

    try:
        deterrence.apply_config(c["deterrence"])
    except Exception as e:
        log.warning(f"[CONFIG] deterrence apply failed: {e}")

    # Camera-set changes are hot-applied by the 'cam-reconcile' thread, NEVER
    # here. This is the poll thread: constructing a CameraStream blocks ~1s and
    # stop() up to ~10s, which would stall every other hot-apply and the config
    # ack behind it.
    try:
        if _cameras_changed(new_blob, old_blob):
            log.info("[CONFIG] camera set changed -- waking the reconciler")
            _reconcile_event.set()
    except Exception:
        log.exception("[CONFIG] camera-change detection failed")

    if c.get("restart_required"):
        _request_restart()


def _post_heartbeat(payload):
    url = os.environ.get("CONFIG_SERVER_URL", "").rstrip("/")
    token = os.environ.get("CONFIG_API_TOKEN", "")
    if not (url and token):
        return
    try:
        requests.post(f"{url}/api/heartbeat/{JETSON_DEVICE_ID}",
                      headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=10)
    except Exception as e:
        # Throttled: if the office server or Tailscale is down this fires every
        # 60s indefinitely. One line every 5 minutes is enough to notice.
        NET_THROTTLE.warning("hb.post", "[HEARTBEAT] POST failed (dashboard will show "
                                        "this device offline): %s", e)


def status_report_loop():
    """POST device + per-camera liveness to the server so the dashboard shows it.
    Interval is server-controlled (heartbeat_interval_sec). Never fatal.

    Reads the roster fresh each cycle so a hot add/remove is reflected in the
    next heartbeat rather than reporting a stale camera list forever."""
    boot = time.time()
    while True:
        interval = int(STORE.get("heartbeat_interval_sec", 60) or 60)
        time.sleep(max(15, interval))
        try:
            now = time.time()
            cams = []
            for s in current_streams():
                age = (now - s._last_frame_ts) if s._last_frame_ts else None
                cams.append({
                    "cam_name": s.name,
                    "thread_alive": (not s.stopped),
                    "power_mode": bool(s.is_power_mode),
                    "last_frame_age_sec": (round(age, 2) if age is not None else None),
                    "pipeline_error": bool(getattr(s.cap, "pipeline_error", False)),
                    "motion_hits": int(s.motion_hits),
                })
            alive = sum(1 for cc in cams if cc["thread_alive"])
            try:
                import shutil
                _t, _u, _f = shutil.disk_usage(STORAGE_ROOT)
                disk_free = round(_f / _t * 100, 1)
            except Exception as e:
                # Was silent -- the dashboard just showed "-" for disk with no
                # hint that the path itself is wrong.
                THROTTLE.warning("hb.disk", "[HEARTBEAT] disk_usage(%s) failed: %s",
                                 STORAGE_ROOT, e)
                disk_free = None
            log.info(f"[HEARTBEAT] {JETSON_DEVICE_ID}: {alive}/{len(cams)} cams alive; cfg {STORE.version}")
            _post_heartbeat({
                "device_id": JETSON_DEVICE_ID,
                "config_version": STORE.version,
                "pending_restart": bool(STORE.pending_restart),
                "pending_restart_fields": list(STORE.pending_restart_fields),
                "uptime_sec": int(now - boot),
                "cameras_alive": alive, "cameras_total": len(cams),
                "cameras": cams, "disk_free_percent": disk_free,
                "notes": _status_notes(),
            })
        except Exception:
            log.exception("[HEARTBEAT] loop iteration failed")


import storage_cleanup

# Operational kill-switches. The retention VALUES (days_to_keep,
# low_space_free_percent) come from the dashboard, not from here.
CLEANUP_ENABLED = os.environ.get("CLEANUP_ENABLED", "1") != "0"
CLEANUP_DRY_RUN = os.environ.get("CLEANUP_DRY_RUN", "0") == "1"
CLEANUP_INTERVAL_SEC = int(os.environ.get("CLEANUP_INTERVAL_SEC", "1800"))


def cleanup_loop():
    """Reclaim disk by deleting the oldest day-folders.

    Runs IN-PROCESS rather than from cron for two reasons:
      * It knows which date folders the camera threads are writing into
        (_ACTIVE_DATE_DIRS), so it can never rmtree a live folder. A separate
        cron process would have to re-derive that from night-window arithmetic
        and stay in sync with this file forever.
      * It uses the already-live CLEANUP_* globals, and it ships with the code --
        the previous cron-based design was never actually scheduled, which is why
        the disk filled up in the first place.
    """
    global _CLEANUP_STATUS
    if not CLEANUP_ENABLED:
        log.warning("[CLEANUP] disabled by CLEANUP_ENABLED=0 -- disk will NOT be reclaimed")
        _CLEANUP_STATUS = "cleanup=disabled"
        return
    if CLEANUP_DRY_RUN:
        log.warning("[CLEANUP] DRY-RUN mode: will report what it would delete, delete nothing")
    log.info("[CLEANUP] thread started; every %ds, root=%s", CLEANUP_INTERVAL_SEC, STORAGE_ROOT)
    time.sleep(60)                      # let the cameras settle before touching IO
    while True:
        try:
            nw = STORE.snapshot["night_window"]
            with _active_dates_lock:
                protect = tuple(_ACTIVE_DATE_DIRS)
            with storage_cleanup.Lock():
                res = storage_cleanup.run_once(
                    storage_root=STORAGE_ROOT,                       # bound, not snapshot
                    days_to_keep=CLEANUP_DAYS_TO_KEEP,               # live (rebound on config change)
                    low_space_free_percent=CLEANUP_LOW_SPACE_FREE_PERCENT,
                    night_end_hour=nw["end_hour"], night_end_minute=nw["end_minute"],
                    protect=protect,
                    dry_run=CLEANUP_DRY_RUN,
                )
            _CLEANUP_STATUS = (f"cleanup={res['ts']} del={len(res['deleted'])} "
                               f"freed={res['freed_bytes'] / 1024 ** 3:.1f}G "
                               f"free={res['free_pct']}%" + (" DRY" if res["dry_run"] else ""))
            if res["deleted"]:
                _event("cleanup", **res)
        except Exception:
            log.exception("[CLEANUP] pass failed")
            _CLEANUP_STATUS = "cleanup=ERROR"
        time.sleep(CLEANUP_INTERVAL_SEC)


def _read_release() -> str:
    """The git tag this box is running, written into the release tree by
    deploy.sh. Without this you cannot answer "which box is running what" --
    which is the first question asked every time one site behaves differently
    from the other six."""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "RELEASE")
        with open(p, "r") as f:
            return f.read().strip()[:32] or "unknown"
    except Exception:
        return "dev"


RELEASE = _read_release()


def _status_notes():
    """Compact health string for the dashboard. Rides the existing heartbeat into
    cfg_device_status.notes, which the server already persists -- no server code
    change needed to start collecting this."""
    try:
        counts = THROTTLE.counts()
        parts = [f"rel={RELEASE}", f"drops={_TOTAL_FRAME_DROPS}",
                 f"errs={sum(counts.values())}"]
        if counts:
            top = max(counts, key=counts.get)
            parts.append(f"top={top}x{counts[top]}")
        # Absolute free space, not just the percentage the server already gets:
        # "12% free" means very different things on a 256GB and a 2TB disk, and
        # at 8 cameras the difference is hours vs days of recording headroom.
        try:
            st = os.statvfs(STORAGE_ROOT)
            parts.append(f"freeGB={(st.f_bavail * st.f_frsize) // (1024**3)}")
        except Exception:
            pass
        # Relay backend health. In RELAY_MODE='both' a dead backend is otherwise
        # invisible at runtime -- set_relays() returns gpio_result or usb_result,
        # so one working relay masks the other and the box looks fine while half
        # the deterrent never fires.
        try:
            parts.append(deterrence.relay_health())
        except Exception:
            pass
        parts.append(_CLEANUP_STATUS)
        return "; ".join(parts)[:500]
    except Exception:
        return ""


if __name__ == "__main__":
    # apply the boot snapshot's deterrence config before anything can trigger
    try:
        deterrence.apply_config(_boot_cfg["deterrence"])
    except Exception as e:
        log.warning(f"[CONFIG] initial deterrence apply failed: {e}")

    STORE.register_listener(_on_config_change)
    start_config_client()   # begin polling the office server (over Tailscale)

    # Boot the camera roster through the SAME function that hot-applies later
    # changes, so the startup path and the reconcile path can never drift.
    reconcile_streams(STORE.snapshot, reason="boot")
    if not current_streams():
        log.error("No cameras started at boot -- check the camera config and RTSP URLs.")

    threading.Thread(target=status_report_loop, daemon=True, name="status").start()
    threading.Thread(target=reconcile_loop, daemon=True, name="cam-reconcile").start()
    threading.Thread(target=cleanup_loop, daemon=True, name="cleanup").start()
    if not HOT_RECONCILE:
        log.warning("[RECONCILE] hot camera reconcile DISABLED (DD_HOT_RECONCILE=0); "
                    "camera-set changes will need a restart")

    # live-frame HTTP server (reuses decoded frames) for the dashboard proxy.
    # Token-gated; the office server reaches it over Tailscale. The index map is
    # already published by reconcile_streams() above and is kept current there.
    start_stream_server(current_streams(),
                        port=int(os.environ.get("STREAM_SERVER_PORT", "8090")),
                        cam_cfgs=_desired_cameras(STORE.snapshot))

    _blank_tile = np.zeros((H, W, 3), np.uint8)

    try:
        if not DISPLAY_ENABLED:
            # Headless: this is the normal mode for a field box. The grid below
            # composites every camera tile plus a log panel on EVERY iteration
            # with no sleep -- ~1-1.5 CPU cores at 8 cameras, for a window that
            # nobody at a forest site is looking at. It also needs an X server
            # (DISPLAY=:0), which is one more thing to fail on boot.
            # Live view is served by the stream server instead.
            log.info("[DISPLAY] local preview window OFF (set DD_DISPLAY=1 to enable)")
            while True:
                time.sleep(3600)

        log.info("[DISPLAY] local preview window ON -- this costs ~1 CPU core; "
                 "do not leave it enabled on a field box")
        while True:
            # Atomic read: the reconcile thread rebinds the roster, never mutates
            # it, so this iterates a stable list even mid-reconcile.
            streams = current_streams()
            cols, rows = _grid["cols"], _grid["rows"]
            cells = rows * cols
            tiles = [s.frame for s in streams][:cells]        # TRUNCATE: never ragged
            tiles += [_blank_tile] * (cells - len(tiles))     # no .copy(): hstack copies
            grid = np.vstack([np.hstack(tiles[r*cols:(r+1)*cols]) for r in range(rows)])

            panel = np.zeros((grid.shape[0], LOG_WIDTH, 3), dtype=np.uint8)
            cv2.putText(panel, "VISION LOGIX", (10, 30), 1, 1.0, (0, 255, 0), 2)
            cv2.putText(panel, f"Device: {JETSON_DEVICE_ID}", (10, 50), 0, 0.42, (200, 200, 200), 1)
            roi_now = is_roi_active()
            cv2.putText(panel, "ROI: NIGHT MODE" if roi_now else "ROI: DAY MODE", (10, 75), 0, 0.42, (0, 165, 255) if roi_now else (80, 200, 80), 1)
            cv2.putText(panel, f"Cams: {len(streams)}  cfg {str(STORE.version)[:14]}",
                        (10, 100), 0, 0.40, (160, 200, 160), 1)
            for i, msg in enumerate(detection_log):
                cv2.putText(panel, msg, (10, 130 + i*28), 0, 0.38, (255, 255, 255), 1)
            cv2.imshow("DHEERKHA DRISHTI -- TensorRT / Jetson", np.hstack((grid, panel)))
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
            # Cap the preview at ~5 fps. waitKey(1) alone let this spin at
            # hundreds of Hz, re-compositing the whole grid each time.
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        for s in current_streams():
            s.stop()
        if DISPLAY_ENABLED:
            cv2.destroyAllWindows()
        log.info("stopped all streams")
        log_setup.flush()