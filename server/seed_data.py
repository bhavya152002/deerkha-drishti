"""
seed_data.py -- the CURRENT hardcoded values, lifted verbatim from the edge
source (standalone/*.py, roi_zones.json). Kept in one place so both seed.py
(writes them to Supabase) and verify_seed.py (asserts the assembled blob equals
them) read from the same literals -> day-one behavior is provably identical.

If you change a value in the edge source, update it here too (or better: after
the split, edit it in the dashboard and let the edge poll it).
"""

# ---- cameras: (rtsp_url, codec) in order; names are "CAM {i+1}" ----
#
# DELIBERATELY EMPTY -- add cameras per site from the dashboard.
#
# This list used to hold four literal RTSP URLs with a working camera password
# on a 192.168.1.x subnet. Two reasons it cannot stay:
#
#   1. It is a live credential in a file that is committed to git, and git
#      history is permanent. The same reasoning already emptied
#      config_client.DEFAULTS["cameras"] on the edge.
#   2. Private subnets COLLIDE across sites. Seeding site 5 from this list
#      points it at site 1's addresses with site 1's password -- worst case
#      connecting to a camera that is not ours.
#
# seed.py loops over this, so an empty list simply seeds no cameras, which is
# the correct starting state for a new box.
CAMERAS = []

# Fleet layout, matching deploy/provision.sh and deploy/push-models.sh. These
# MUST agree with edge/config_client.py DEFAULTS["paths"] -- a device seeded
# with the old single-machine /home/visionlogix/... paths cannot find its
# TensorRT engines and fails at startup.
STORAGE_ROOT = "/mnt/data/video_storage"
RFDETR_ENGINE_PATH = "/opt/deerkha/models/rfdetr_fp16.engine"
CLIP_ENGINE_PATH = "/opt/deerkha/models/mobileclip2_s2_image_fp16.engine"
CLIP2_MODEL_DIR = "/opt/deerkha/models/pt"
CLIP2_CHECKPOINT = "/opt/deerkha/models/pt/mobileclip2_s2.pt"
CLIP_IMG_INPUT_SIZE = 256
RF_NMS_IOU = 0.7
RF_PREDICT_THRESHOLD = 0.25

ROI_START_HOUR = 18
ROI_END_HOUR = 6
ROI_END_MINUTE = 31
DETECTION_RES = (1280, 720)
ROI_POLYGONS_DRAWN_AT_RES = (1080, 720)

# Resolution motion clips are RECORDED at. Was absent entirely, which left the
# column NULL and meant "native" -- i.e. software-encoding 1080p on a board with
# no hardware encoder, for every camera, continuously.
MOTION_VIDEO_RES = (1280, 720)

MOTION_DEFAULT = {"min_frames": 5, "threshold": 20, "kernel": (7, 7), "area_min": 300}
MOTION_SENSITIVITY_PER_CAM = {
    "CAM 1": {"min_frames": 5, "threshold": 20, "kernel": (7, 7), "area_min": 300},
    "CAM 2": {"min_frames": 5, "threshold": 20, "kernel": (7, 7), "area_min": 300},
    "CAM 3": {"min_frames": 5, "threshold": 25, "kernel": (9, 9), "area_min": 400},
    "CAM 4": {"min_frames": 5, "threshold": 20, "kernel": (7, 7), "area_min": 300},
}

COOLDOWN_TIME_SPECIFIC = 3
COOLDOWN_TIME_GENERIC = 15
MOTION_CLOSE_DELAY = 8
MOTION_MIN_CLIP_DURATION = 30
MOTION_MAX_CLIP_DURATION = 3600
ANIMAL_ROTATE_SEC = 10
FPS = 10.0

MIN_CONFIRM_FRAMES = 18
BUFFER_WINDOW = 25
ANIMAL_MIN_CONFIRM_FRAMES = 30
ANIMAL_BUFFER_WINDOW = 40
MIN_BBOX_RATIO = 0.0026

# 2, not 3: the fitted NVMe is 465 GB and 3 days at 8 cameras needs ~500 GB.
# Raise once a real week of GB/camera/day has been measured.
CLEANUP_DAYS_TO_KEEP = 2
CLEANUP_LOW_SPACE_FREE_PERCENT = 10

# reporting cadence -- intentionally faster than the old 3600s so the dashboard
# is timely (does not affect detection behavior).
HEARTBEAT_INTERVAL_SEC = 60
POLL_INTERVAL_SEC = 30

# class -> (rf_thresh, (B,G,R)); MY_CLASSES gives rf_class_id for the 6 real ones
MY_CLASSES = {0: "wild boar", 1: "elephant", 2: "leopard", 3: "tiger", 4: "person", 5: "deer"}
CLASS_SETTINGS = {
    "wild boar": {"thresh": 0.60, "color": (0, 255, 0)},
    "elephant": {"thresh": 0.65, "color": (0, 255, 0)},
    "leopard": {"thresh": 0.65, "color": (0, 0, 255)},
    "tiger": {"thresh": 0.65, "color": (0, 0, 255)},
    "person": {"thresh": 0.50, "color": (255, 100, 0)},
    "deer": {"thresh": 0.60, "color": (0, 215, 255)},
    "Animal": {"thresh": 0.45, "color": (0, 165, 255)},
    "default": {"thresh": 0.40, "color": (255, 255, 255)},
}

CLIP_CLASS_MIN_LOGIT = {
    "elephant": 30.0, "wild boar": 32.0, "tiger": 25.5, "leopard": 30.0, "deer": 30.0,
}
CLIP_FALLBACK_MIN_LOGIT = 27.0
CLIP_ALERT_CLASSES = ["wild boar", "elephant", "leopard", "tiger", "deer"]
CLIP_COOLDOWN = 5.0
CLIP_BBOX_PAD = 0.30
CLIP_MIN_CROP_W = 45
CLIP_MIN_CROP_H = 45
CLIP_MIN_RF_CONF = 0.40
SEND_FALLBACK_TELEGRAM = False
CROSS_SPECIES_FALLBACK = {"deer": "wild boar", "wild boar": "deer"}

CLIP_PROMPTS = {
    "elephant": ["elephant", "asian elephant at night", "elephnat grazing at night", "elephant at night", "elephant night vision footage", "elephant body shape infrared", "elephant back infrared", "elephant in forest at night", "grey animal silhouette", "large trunk animal dark", "elephant night vision", "large grey animal night forest"],
    "wild boar": ["wild boar infrared", "wild boar", "ferral hog", "ferral pig night camera", "dark pig night vision", "wild pig infrared", "pig shaped animal night vision", "wild boar wide body infrared", "wild boar full body night"],
    "tiger": ["tiger full body", "bengal tiger at night", "large striped cat infrared", "tiger night vision footage", "tiger silhouette dark forest", "tiger infrared", "tiger walking at night", "tiger night vision"],
    "leopard": ["leopard", "leopard at night", "feline silhouette night forest", "leopard full body spotted", "leopard night vision"],
    "deer": ["deer", "spotted deer", "chital deer at night", "deer night vision footage", "chital deer", "deer at night", "deer walking night", "spotted deer night vision", "deer with antler", "deer silhouette forest dark", "deer infrared"],
}

CLIP_FALLBACKS = {
    "elephant": {"buffalo": ["buffalo", "wild buffalo", "large dark bovine night", "large horned animal dark"]},
    "wild boar": {"buffalo": ["buffalo", "wild buffalo", "large dark bovine night", "large horned animal dark"]},
    "tiger": {"leopard": CLIP_PROMPTS["leopard"]},
    "leopard": {"tiger": CLIP_PROMPTS["tiger"]},
    "deer": {"goat": ["goat ", "indian goat ", "small hoofed animal dark"]},
}

CLIP_DISTRACTORS = [
    "tiger tail", "large cat tail", "tiger leg", "large feline tail dark", "animal hindquarters night", "sloth bear", "bear in forest night", "black bear infrared", "gaur bison dark", "domestic cow night", "monkey forest night", "fox infrared", "rabbit night vision", "empty forest", "trees and bushes dark", "blurry night vision background", "empty night vision scene", "no animals present", "leaves and branches infrared", "tree trunk dark",
]

# detection ROI polygons, drawn at 1080x720 (ROI_POLYGONS_RAW_ORIGINAL)
ROI_POLYGONS_RAW_ORIGINAL = {
    "CAM 1": [(0, 362), (2, 717), (1077, 718), (1075, 372), (1075, 350), (1028, 337), (961, 326), (860, 312), (741, 302), (667, 289), (608, 289), (505, 286), (434, 286), (358, 297), (307, 307), (200, 317), (85, 340), (1, 356)],
    "CAM 2": [(4, 717), (3, 355), (235, 310), (427, 270), (615, 257), (763, 262), (887, 289), (981, 318), (1078, 356), (1075, 714)],
    "CAM 3": [(1, 714), (1077, 715), (1074, 515), (1077, 367), (972, 342), (878, 344), (795, 340), (685, 337), (592, 339), (482, 346), (391, 354), (291, 362), (218, 366), (98, 366), (27, 366), (5, 367), (5, 718)],
    "CAM 4": [(3, 718), (2, 358), (302, 287), (475, 251), (631, 262), (808, 272), (909, 285), (1023, 301), (1076, 314), (1077, 715)],
}

# ignore zones from roi_zones.json (native camera res ~1920x1080).
# NOTE the key normalization: file uses "CAM3"/"CAM4"; edge derives the key via
# cam_name.replace(" ",""), so "CAM 3" -> "CAM3". We store them under "CAM 3".
IGNORE_ZONES_NATIVE = {
    "CAM 3": [[[1914, 712], [1784, 660], [1788, 543], [1845, 221], [1910, 192], [1910, 710]]],
    "CAM 4": [[[717, 13], [727, 646], [1243, 695], [1216, 26], [718, 14]]],
}
IGNORE_DRAWN_AT_RES = (1920, 1080)

# ---- deterrence (trigger_jetson_3.py) ----
_SND = "/opt/deerkha/sounds/"
DETERRENCE_TARGETS = {
    "leopard": {"action": "light_and_audio", "audio_files": [_SND + "lion-snarl.mp3", _SND + "ElevenLabs_Fierce_tiger_roaring.mp3"]},
    "elephant": {"action": "light_and_audio", "audio_files": [_SND + "lion-snarl.mp3", _SND + "tiger.mp3", _SND + "ElevenLabs_Fierce_tiger_roaring.mp3"]},
    "tiger": {"action": "light_and_audio", "audio_files": [_SND + "lion-snarl.mp3", _SND + "gunauto.mp3"]},
    "wild boar": {"action": "light_and_audio", "audio_files": [_SND + "gunauto.mp3", _SND + "tiger.mp3", _SND + "ElevenLabs_Fierce_tiger_roaring.mp3"]},
    "deer": {"action": "light_only", "audio_files": []},
    "animal": {"action": "light_only", "audio_files": []},
}
DETERRENCE_GLOBAL = {
    "relay_mode": "both", "audio_playback_mode": "mixed",
    "relay_ch1_pin": 11, "relay_ch2_pin": 13, "relay_active_low": True,
    "usb_relay_port": "/dev/ttyUSB0", "usb_relay_baud": 9600,
    "usb_on_cmd_hex": "A00101A2", "usb_off_cmd_hex": "A00100A1",
    "blink_total_duration": 30, "blink_active_phase": 10, "blink_rest_phase": 3, "blink_interval": 0.1,
}

# toggles
SUPABASE_SIGNAL_ENABLED = True
DETAILED_TELEGRAM_MSG = True
IGNORE_ZONES_ENABLED = True
ANIMAL_SUPABASE_TRIGGER = True
# False on 8 GB: the resident PyTorch text tower costs 2.0-2.5 GB, and the
# reload path already exists for the rare prompt change.
KEEP_TEXT_TOWER_RESIDENT = False
