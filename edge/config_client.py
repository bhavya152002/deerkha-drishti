"""
config_client.py -- dynamic config for the Jetson edge.

Polls the office server (over Tailscale) for this device's config blob, hot-swaps
an immutable snapshot behind a lock (lock-free reads for the per-frame pipeline),
caches the last-known-good blob to disk, and falls back to a hardcoded DEFAULTS
snapshot so the device ALWAYS boots -- even with no server and no cache.

Contract with the server: the blob shape here mirrors assemble_config() in
server/app/assembly.py exactly. Both sides must change together.

Env used (all optional except when polling):
    JETSON_DEVICE_ID   -- device identity; also picked up by the main script
    CONFIG_SERVER_URL  -- e.g. http://100.x.y.z:8000  (Tailscale IP of office PC)
    CONFIG_API_TOKEN   -- bearer token from `python seed.py` on the server
    CONFIG_CACHE_PATH  -- override cache file location (default: next to this file)

Public surface used by the main script:
    STORE                       -- the singleton ConfigStore
    STORE.snapshot              -- current blob dict (atomic reference read)
    STORE.generation            -- monotonic int; bumps on every accepted swap
    STORE.version               -- current config_version string
    STORE.get("a.b.c", default) -- dotted-path read off the snapshot
    STORE.register_listener(fn) -- fn(new_blob, old_blob) after each swap
    STORE.pending_restart / STORE.pending_restart_fields
    start_config_client()       -- launches the poll daemon; returns STORE
    load_startup_config()       -- (used internally) cache -> DEFAULTS
"""

import os
import json
import time
import copy
import random
import hashlib
import logging
import threading

try:
    import requests
except Exception:  # requests should exist (main script uses it); degrade gracefully
    requests = None

log = logging.getLogger("DHEERKHA")

# Network faults here repeat every poll interval for as long as the outage lasts,
# so they are throttled to one line per 5 minutes.
try:
    from log_setup import Throttle
    _NET_THROTTLE = Throttle(log, burst=3, every=300.0)
except Exception:  # pragma: no cover -- degrade to plain logging
    class _Fallback:
        def warning(self, key, msg, *a):
            log.warning(msg, *a)
    _NET_THROTTLE = _Fallback()

# Import time ~= process start. Reported in the config ack so the server can tell
# a genuine restart from a plain config apply.
_PROCESS_START = time.time()

_HERE = os.path.dirname(os.path.abspath(__file__))
DEVICE_ID = os.environ.get("JETSON_DEVICE_ID")  # main script sets a socket.gethostname() default
SERVER_URL = os.environ.get("CONFIG_SERVER_URL", "").rstrip("/")
API_TOKEN = os.environ.get("CONFIG_API_TOKEN", "")
# Last-known-good config cache. Must live OUTSIDE the code directory: under the
# fleet layout the code is an immutable per-release tree, so a cache written
# next to the script would be wiped by every deploy -- meaning a box that
# rebooted after an update while the server was down would fall all the way
# back to DEFAULTS (no cameras) instead of its real config.
_STATE_DIR = os.environ.get("DD_STATE_DIR", "/var/lib/deerkha")
if not os.path.isdir(_STATE_DIR):
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
    except OSError:
        _STATE_DIR = _HERE
CACHE_PATH = os.environ.get("CONFIG_CACHE_PATH", os.path.join(_STATE_DIR, "config_cache.json"))

# Structural keys that genuinely cannot be hot-applied:
#   detection_res -- every ROI mask, rf_input_mask and FRAME_AREA is precomputed
#                    at this resolution and read lock-free by every inference
#                    thread.
#   paths         -- TensorRT engine paths and STORAGE_ROOT are bound at import.
#
# "cameras" is deliberately NOT here any more: the edge now reconciles the camera
# set at runtime (see reconcile_streams in the main script), so adding, removing,
# disabling or repointing a camera applies within one poll with no restart.
RESTART_KEYS = ("detection_res", "paths")

# =====================================================================
# DEFAULTS -- literal snapshot mirroring the server blob shape. Guarantees the
# device boots with today's behavior even if the server and cache are both gone.
# =====================================================================
# Fleet-neutral sound directory, shipped with the golden image alongside the
# models. Real per-device values come from cfg_deterrence_targets; this only
# applies on a box that can reach neither the server nor its cache, so it must
# not point at one operator's home directory.
_SND = "/opt/deerkha/sounds/"
DEFAULTS = {
    "device_id": DEVICE_ID or "unknown",
    "config_version": "defaults",
    "restart_required": False,
    "poll_interval_sec": 30,
    # Fleet-neutral paths. Every box uses the same on-disk layout so one code
    # artifact runs unmodified everywhere; per-device overrides still come from
    # cfg_devices. Models live outside the release directory because they are
    # large, rarely change, and are shipped separately from the code.
    "paths": {
        "storage_root": "/mnt/data/video_storage",
        "rfdetr_engine_path": "/opt/deerkha/models/rfdetr_fp16.engine",
        "clip_engine_path": "/opt/deerkha/models/mobileclip2_s2_image_fp16.engine",
        "clip2_checkpoint": "/opt/deerkha/models/pt/mobileclip2_s2.pt",
        "clip2_model_dir": "/opt/deerkha/models/pt",
        "clip_img_input_size": 256,
        "rf_nms_iou": 0.7,
        "rf_predict_threshold": 0.25,
    },
    "night_window": {"start_hour": 18, "end_hour": 6, "end_minute": 31},
    "detection_res": [1280, 720],
    "polygons_drawn_at_res": [1080, 720],
    "motion_video_res": None,
    "cooldowns": {"specific": 3, "generic": 15, "motion_close_delay": 8, "clip": 5.0},
    "recording": {"motion_min_clip_duration": 30, "motion_max_clip_duration": 3600, "animal_rotate_sec": 10, "fps": 10.0},
    "confirm": {"min_confirm_frames": 18, "buffer_window": 25, "animal_min_confirm_frames": 30, "animal_buffer_window": 40},
    # Per-camera inference rate. The pipeline paces its inference loop to this;
    # it does NOT affect capture, motion detection or recording frame rates.
    # Raising it costs GPU linearly with the number of cameras in power mode.
    "inference": {"target_fps": 5.0},
    "min_bbox_ratio": 0.0026,
    "cleanup": {"days_to_keep": 3, "low_space_free_percent": 10},
    "heartbeat_interval_sec": 60,
    "motion_default": {"min_frames": 5, "threshold": 20, "kernel": [7, 7], "area_min": 300},
    "flags": {
        "supabase_signal_enabled": True, "detailed_telegram_msg": True,
        "send_fallback_telegram": False, "ignore_zones_enabled": True,
        "animal_supabase_trigger": True, "telegram_chat_id": None,
        # Server-delivered. None here means "fall back to the env value", which
        # is the correct state for a box that has never reached the server --
        # main.py degrades to logging alerts rather than failing to start.
        "telegram_bot_token": None,
    },
    # DELIBERATELY EMPTY. This block is the last-resort fallback used only when
    # a box can reach neither the server nor its own on-disk cache -- i.e. a
    # freshly imaged device at a new site.
    #
    # It used to carry four hardcoded cameras with literal RTSP URLs and an
    # embedded password on a 192.168.1.x subnet. Every box in the fleet runs
    # this same image, and private subnets collide across sites, so a new box
    # at site 5 would try site 1's addresses with site 1's credentials -- worst
    # case connecting to a camera that isn't ours, and shipping a working
    # password to every field device we deploy.
    #
    # Booting with no cameras is the correct failure: validate() accepts an
    # empty list, reconcile_streams() refuses to shut a RUNNING roster down to
    # zero, and the reconciler retries every 60s, so the box comes up healthy,
    # heartbeats, and picks up its real cameras the moment the server answers.
    "cameras": [],
    "class_settings": {
        "wild boar": {"thresh": 0.60, "color": [0, 255, 0], "rf_class_id": 0},
        "elephant": {"thresh": 0.65, "color": [0, 255, 0], "rf_class_id": 1},
        "leopard": {"thresh": 0.65, "color": [0, 0, 255], "rf_class_id": 2},
        "tiger": {"thresh": 0.65, "color": [0, 0, 255], "rf_class_id": 3},
        "person": {"thresh": 0.50, "color": [255, 100, 0], "rf_class_id": 4},
        "deer": {"thresh": 0.60, "color": [0, 215, 255], "rf_class_id": 5},
        "Animal": {"thresh": 0.45, "color": [0, 165, 255], "rf_class_id": None},
        "default": {"thresh": 0.40, "color": [255, 255, 255], "rf_class_id": None},
    },
    "clip": {
        "class_min_logit": {"elephant": 30.0, "wild boar": 32.0, "tiger": 25.5, "leopard": 30.0, "deer": 30.0},
        "fallback_min_logit": 27.0,
        "alert_classes": ["wild boar", "elephant", "leopard", "tiger", "deer"],
        "cooldown": 5.0, "bbox_pad": 0.30, "min_crop_w": 45, "min_crop_h": 45, "min_rf_conf": 0.40,
        "keep_text_tower_resident": True,
        "prompts": {
            "elephant": ["elephant", "asian elephant at night", "elephnat grazing at night", "elephant at night", "elephant night vision footage", "elephant body shape infrared", "elephant back infrared", "elephant in forest at night", "grey animal silhouette", "large trunk animal dark", "elephant night vision", "large grey animal night forest"],
            "wild boar": ["wild boar infrared", "wild boar", "ferral hog", "ferral pig night camera", "dark pig night vision", "wild pig infrared", "pig shaped animal night vision", "wild boar wide body infrared", "wild boar full body night"],
            "tiger": ["tiger full body", "bengal tiger at night", "large striped cat infrared", "tiger night vision footage", "tiger silhouette dark forest", "tiger infrared", "tiger walking at night", "tiger night vision"],
            "leopard": ["leopard", "leopard at night", "feline silhouette night forest", "leopard full body spotted", "leopard night vision"],
            "deer": ["deer", "spotted deer", "chital deer at night", "deer night vision footage", "chital deer", "deer at night", "deer walking night", "spotted deer night vision", "deer with antler", "deer silhouette forest dark", "deer infrared"],
        },
        "fallbacks": {
            "elephant": {"buffalo": ["buffalo", "wild buffalo", "large dark bovine night", "large horned animal dark"]},
            "wild boar": {"buffalo": ["buffalo", "wild buffalo", "large dark bovine night", "large horned animal dark"]},
            "tiger": {"leopard": ["leopard", "leopard at night", "feline silhouette night forest", "leopard full body spotted", "leopard night vision"]},
            "leopard": {"tiger": ["tiger full body", "bengal tiger at night", "large striped cat infrared", "tiger night vision footage", "tiger silhouette dark forest", "tiger infrared", "tiger walking at night", "tiger night vision"]},
            "deer": {"goat": ["goat ", "indian goat ", "small hoofed animal dark"]},
        },
        "cross_species": {"deer": "wild boar", "wild boar": "deer"},
        "distractors": ["tiger tail", "large cat tail", "tiger leg", "large feline tail dark", "animal hindquarters night", "sloth bear", "bear in forest night", "black bear infrared", "gaur bison dark", "domestic cow night", "monkey forest night", "fox infrared", "rabbit night vision", "empty forest", "trees and bushes dark", "blurry night vision background", "empty night vision scene", "no animals present", "leaves and branches infrared", "tree trunk dark"],
    },
    "deterrence": {
        "relay_mode": "both", "audio_playback_mode": "mixed",
        "gpio": {"ch1_pin": 11, "ch2_pin": 13, "active_low": True},
        "usb": {"port": "/dev/ttyUSB0", "baud": 9600, "on_cmd_hex": "A00101A2", "off_cmd_hex": "A00100A1"},
        "blink": {"total_duration": 30, "active_phase": 10, "rest_phase": 3, "interval": 0.1},
        "targets": {
            "leopard": {"action": "light_and_audio", "audio_files": [_SND + "lion-snarl.mp3", _SND + "ElevenLabs_Fierce_tiger_roaring.mp3"]},
            "elephant": {"action": "light_and_audio", "audio_files": [_SND + "lion-snarl.mp3", _SND + "tiger.mp3", _SND + "ElevenLabs_Fierce_tiger_roaring.mp3"]},
            "tiger": {"action": "light_and_audio", "audio_files": [_SND + "lion-snarl.mp3", _SND + "gunauto.mp3"]},
            "wild boar": {"action": "light_and_audio", "audio_files": [_SND + "gunauto.mp3", _SND + "tiger.mp3", _SND + "ElevenLabs_Fierce_tiger_roaring.mp3"]},
            "deer": {"action": "light_only", "audio_files": []},
            "animal": {"action": "light_only", "audio_files": []},
        },
    },
}


def _blob_hash(blob: dict) -> str:
    tmp = dict(blob)
    tmp.pop("config_version", None)
    canonical = json.dumps(tmp, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate(blob) -> tuple[bool, str]:
    """Structural + range sanity checks. Returns (ok, reason)."""
    if not isinstance(blob, dict):
        return False, "not a dict"
    for k in ("device_id", "config_version", "cameras", "class_settings", "clip", "deterrence"):
        if k not in blob:
            return False, f"missing key '{k}'"
    if DEVICE_ID and blob.get("device_id") not in (DEVICE_ID, None):
        return False, f"device_id mismatch: blob={blob.get('device_id')} self={DEVICE_ID}"
    if not isinstance(blob["cameras"], list):
        return False, "cameras invalid"
    if not blob["cameras"]:
        # Accept, don't reject. Rejecting the blob here froze EVERY unrelated
        # setting (thresholds, deterrence, cleanup) because one list was empty.
        # The real guard lives in reconcile_streams(), which refuses to drop the
        # running roster to zero.
        log.warning("[CONFIG] blob has zero cameras -- accepting the config; the "
                    "reconciler will refuse to shut the running cameras down")
    nw = blob.get("night_window", {})
    for k in ("start_hour", "end_hour"):
        v = nw.get(k)
        if v is not None and not (0 <= v <= 23):
            return False, f"night_window.{k} out of range: {v}"
    for cname, cs in blob["class_settings"].items():
        t = cs.get("thresh")
        if t is None or not (0.0 <= t <= 1.0):
            return False, f"class_settings[{cname}].thresh out of range: {t}"
    return True, ""


def _restart_fields(new_blob: dict, old_blob: dict) -> list:
    """Which restart-class keys differ between old and new (for pending_restart)."""
    out = []
    for k in RESTART_KEYS:
        if json.dumps(new_blob.get(k), sort_keys=True, default=str) != json.dumps(old_blob.get(k), sort_keys=True, default=str):
            out.append(k)
    return out


class ConfigStore:
    def __init__(self, initial: dict):
        self._snapshot = initial
        self._lock = threading.RLock()
        self._listeners = []
        self.generation = 0
        self.version = initial.get("config_version", "defaults")
        self.pending_restart = bool(initial.get("restart_required"))
        self.pending_restart_fields = []

    @property
    def snapshot(self) -> dict:
        # single atomic attribute read -> lock-free consistent view for readers
        return self._snapshot

    def get(self, dotted: str, default=None):
        cur = self._snapshot
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    def register_listener(self, fn):
        """fn(new_blob, old_blob) is called (under no lock) after each accepted swap."""
        self._listeners.append(fn)

    def apply(self, new_blob: dict) -> bool:
        """Validate + atomically swap in a new blob. Returns True if applied."""
        ok, reason = validate(new_blob)
        if not ok:
            log.warning(f"[CONFIG] rejected update: {reason}")
            return False
        with self._lock:
            old = self._snapshot
            if new_blob.get("config_version") == old.get("config_version") and _blob_hash(new_blob) == _blob_hash(old):
                return False  # no-op
            # STICKY: union, never reset. Previously this was a plain assignment,
            # so any later live-only edit (a cooldown, a threshold) wiped the
            # pending-restart set and the dashboard badge silently went green
            # while the restart-class change was still outstanding. The set only
            # grows within one process lifetime; a fresh process starts empty,
            # which is correct because its boot snapshot is applied by definition.
            fields = _restart_fields(new_blob, old)
            if fields:
                self.pending_restart_fields = sorted(set(self.pending_restart_fields) | set(fields))
            self.pending_restart = bool(new_blob.get("restart_required")) or bool(self.pending_restart_fields)
            self._snapshot = new_blob
            self.generation += 1
            self.version = new_blob.get("config_version", "unknown")
        for fn in self._listeners:
            try:
                fn(new_blob, old)
            except Exception:
                # A listener that throws leaves the config PARTIALLY applied --
                # always worth a full traceback.
                log.exception("[CONFIG] listener failed; config may be partially applied")
        log.info(f"[CONFIG] applied version {self.version} (gen {self.generation})"
                 + (f"; PENDING RESTART: {self.pending_restart_fields}" if self.pending_restart_fields else ""))
        return True


# ---- disk cache (last-known-good) ----
def _write_cache(blob: dict):
    try:
        tmp = CACHE_PATH + ".tmp"
        # 0600, and set AT CREATION rather than by a chmod after the write:
        # the blob carries the Telegram bot token and RTSP URLs with embedded
        # camera passwords, and a create-then-chmod leaves a window where the
        # file is world-readable. os.open honours the mode only for a file it
        # actually creates, hence the O_CREAT|O_TRUNC pair on a fresh temp path.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(blob, f)
        os.replace(tmp, CACHE_PATH)
    except Exception as e:
        log.warning(f"[CONFIG] could not write cache {CACHE_PATH}: {e}")


def _read_cache():
    try:
        if os.path.isfile(CACHE_PATH):
            with open(CACHE_PATH, "r") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"[CONFIG] cache unreadable ({e}); ignoring")
    return None


def load_startup_config() -> dict:
    """Cache -> DEFAULTS. Always returns a usable blob so the device boots."""
    cached = _read_cache()
    if cached is not None:
        ok, reason = validate(cached)
        if ok:
            log.info(f"[CONFIG] booting from cache (version {cached.get('config_version')})")
            return cached
        log.warning(f"[CONFIG] cache invalid ({reason}); using DEFAULTS")
    else:
        log.info("[CONFIG] no cache; using DEFAULTS")
    return copy.deepcopy(DEFAULTS)


STORE = ConfigStore(load_startup_config())


# =====================================================================
# Polling
# =====================================================================
def _server_configured() -> bool:
    return bool(SERVER_URL and API_TOKEN and DEVICE_ID and requests is not None)


def _headers():
    return {"Authorization": f"Bearer {API_TOKEN}"}


def _poll_once():
    """One poll cycle: check version cheaply, fetch full blob on change, apply,
    then ack. Never raises. Returns True if the server was reached, False if
    it was not -- the caller uses that to back off instead of hammering a
    server that is down or a link that is saturated."""
    try:
        vr = requests.get(f"{SERVER_URL}/api/config/{DEVICE_ID}/version", headers=_headers(), timeout=10)
        if vr.status_code != 200:
            log.warning(f"[CONFIG] /version -> {vr.status_code}")
            return False
        vj = vr.json()
        server_version = vj.get("config_version")
        if server_version == STORE.version and not vj.get("restart_required"):
            return True  # up to date
        # fetch full blob. Send the version we already hold so an unchanged
        # config can come back as a 304 with no body.
        headers = dict(_headers())
        if STORE.version:
            headers["If-None-Match"] = f'"{STORE.version}"'
        r = requests.get(f"{SERVER_URL}/api/config/{DEVICE_ID}", headers=headers, timeout=15)
        if r.status_code == 304:
            return True
        if r.status_code != 200:
            log.warning(f"[CONFIG] /config -> {r.status_code}")
            return False
        blob = r.json()
        applied = STORE.apply(blob)
        if applied:
            _write_cache(blob)
        # ack so the server can clear restart_required once we're on this version
        try:
            # uptime_sec lets the server distinguish "I received this config"
            # from "I rebooted into it" -- it only clears restart_required for a
            # genuinely fresh process.
            requests.post(f"{SERVER_URL}/api/config/{DEVICE_ID}/ack", headers=_headers(),
                          json={"config_version": STORE.version,
                                "uptime_sec": int(time.time() - _PROCESS_START)}, timeout=10)
        except Exception as e:
            # Was a bare pass. If the ack never lands the server keeps
            # restart_required set and will re-push the same blob forever.
            _NET_THROTTLE.warning("cfg.ack",
                                  "[CONFIG] ack POST failed (server may keep "
                                  "restart_required set): %s", e)
        return True
    except Exception as e:
        # network/parse failure -> keep current in-memory config, never crash.
        # This was log.debug, i.e. invisible at the hardcoded INFO level: total
        # loss of contact with the office server produced NO output at all.
        _NET_THROTTLE.warning("cfg.poll",
                              "[CONFIG] poll FAILED -- keeping current config, "
                              "server may be unreachable: %s", e)
        return False


def config_poll_loop():
    if not _server_configured():
        log.warning("[CONFIG] server not configured (CONFIG_SERVER_URL/CONFIG_API_TOKEN/JETSON_DEVICE_ID); "
                    "running on cache/DEFAULTS only, no polling.")
        return
    log.info(f"[CONFIG] polling {SERVER_URL} for device '{DEVICE_ID}'")
    # try once immediately so a fresh boot upgrades from cache/DEFAULTS quickly
    ok = _poll_once()
    fails = 0
    while True:
        interval = max(5.0, float(STORE.get("poll_interval_sec", 30) or 30))
        if ok:
            fails = 0
            # Randomised jitter, NOT derived from generation. The old
            # `interval + (generation % 5)` was identical on every box running
            # the same config version, so the whole fleet polled in lockstep --
            # and any correlated event (a config push, a site power
            # restoration) re-synchronised them into one burst forever.
            delay = interval * random.uniform(0.85, 1.15)
        else:
            # Exponential backoff while the server is unreachable. Without it,
            # N boxes retry every 30s indefinitely against a server that is
            # down -- and on a metered link that failed traffic is not free.
            fails += 1
            delay = min(interval * (2 ** min(fails, 5)), 900.0) * random.uniform(0.85, 1.15)
            if fails in (1, 5, 20):
                log.warning("[CONFIG] server unreachable x%d -- backing off to ~%ds "
                            "(still running on last-known-good config)", fails, int(delay))
        time.sleep(delay)
        ok = _poll_once()


def start_config_client() -> ConfigStore:
    """Launch the poll daemon and return the store."""
    threading.Thread(target=config_poll_loop, daemon=True, name="config-poll").start()
    return STORE
