# =========================================================
# DHEERKHA DRISHTI -- Jetson-local Deterrence Trigger Module
# =========================================================
# Public API unchanged: trigger_deterrence(label) / stop_deterrence().
# =========================================================

import logging
import time
import threading
import os
import pygame

# NOTE: importing this module does NOT touch hardware. The caller must invoke
# init_hardware(cfg["deterrence"]) once, as early as possible, with the boot
# config snapshot -- see main.py, which calls it the moment the snapshot exists.
#
# It used to run init_relay() at import time, which made the relay mode, GPIO
# pins and USB port UNCONFIGURABLE: the import happens before config_client is
# even imported, so the server-managed values in cfg_deterrence_global could
# never be read. The panel's "Relay mode (restart)" was a dead control -- a box
# set to 'gpio' still fired the USB relay, because the module was hardcoded to
# 'both' on every path, restart included.
#
# Logging is still the first thing set up here: these lines used to be print()
# to stdout -- block-buffered when stdout isn't a TTY, so under systemd or a
# redirect they could sit in an 8 KB buffer and be LOST on a crash. That is
# precisely the output you need when the deterrent doesn't fire. The main script
# configures logging before importing this module, so these land in the file.
log = logging.getLogger("DHEERKHA.deterrence")

try:
    import Jetson.GPIO as GPIO
    _GPIO_AVAILABLE = True
except Exception as e:
    log.warning(f"Jetson.GPIO not available ({e}).")
    _GPIO_AVAILABLE = False

try:
    import serial
    _SERIAL_AVAILABLE = True
except Exception as e:
    log.warning(f"pyserial not available ({e}) -- run: pip install pyserial")
    _SERIAL_AVAILABLE = False

# =========================================================
# CONFIG -- FALLBACK DEFAULTS ONLY
# =========================================================
# Everything in this block is overwritten by init_hardware() from the server
# config before any hardware is touched. The literals below only apply if the
# caller never calls init_hardware(), or passes an empty blob.
RELAY_MODE = "both"     # "gpio"  |  "usb"  |  "both"

# ---- Audio Playback Strategy ----
# "mixed" cycles through the class playlist tracks sequentially; "single" plays only the first track.
AUDIO_PLAYBACK_MODE = "mixed"

# =========================================================
# TARGET CONFIG & PER-CLASS AUDIO STRATEGIES
# =========================================================
TARGET_CONFIG = {
    "leopard": {
        "action": "light_and_audio",
        "audio_files": [
            "/opt/deerkha/sounds/lion-snarl.mp3",
            "/opt/deerkha/sounds/ElevenLabs_Fierce_tiger_roaring.mp3"
        ]
    },
    "elephant": {
        "action": "light_and_audio",
        "audio_files": [
            "/opt/deerkha/sounds/lion-snarl.mp3",
            "/opt/deerkha/sounds/tiger.mp3",
            "/opt/deerkha/sounds/ElevenLabs_Fierce_tiger_roaring.mp3"
        ]
    },
    "tiger": {
        "action": "light_and_audio",
        "audio_files": [
            "/opt/deerkha/sounds/lion-snarl.mp3",
            "/opt/deerkha/sounds/gunauto.mp3"
        ]
    },
    "wild boar": {
        "action": "light_and_audio",
        "audio_files": [
            "/opt/deerkha/sounds/gunauto.mp3",
            "/opt/deerkha/sounds/tiger.mp3",
            "/opt/deerkha/sounds/ElevenLabs_Fierce_tiger_roaring.mp3"
        ]
    },
    "deer": {
        "action": "light_only",
        "audio_files": []
    },
    "animal": {
        "action": "light_only",
        "audio_files": []
    }
}

# ---- GPIO settings (used if RELAY_MODE is "gpio" or "both") ----
RELAY_CH1_PIN = 11   # physical pin 11 (BCM 17)
RELAY_CH2_PIN = 13   # physical pin 13 (BCM 27)
RELAY_ACTIVE_LOW = True   # most cheap 2-channel relay boards: LOW = relay ON

# ---- USB settings (used if RELAY_MODE is "usb" or "both") ----
USB_RELAY_PORT = "/dev/ttyUSB0"
USB_RELAY_BAUD = 9600
USB_RELAY_ON_CMD  = bytes([0xA0, 0x01, 0x01, 0xA2])   # LCUS-1: relay ON
USB_RELAY_OFF_CMD = bytes([0xA0, 0x01, 0x00, 0xA1])   # LCUS-1: relay OFF

# STAGED BLINK SETTINGS
BLINK_TOTAL_DURATION = 30
BLINK_ACTIVE_PHASE   = 10
BLINK_REST_PHASE     = 3
BLINK_INTERVAL       = 0.1

# =========================================================
# GLOBAL STATE
# =========================================================
_last_detection_time = 0.0
_audio_active = False
_deterrence_running = False
_current_audio_playlist = []
_animal_sound_indices = {}   # Tracks sequential playback indexes per animal
_hw_lock = threading.Lock()
_gpio_ready = False
_usb_serial = None
_usb_ready = False
# Relay settings actually in force on the hardware, captured by init_hardware().
# apply_config() diffs incoming config against this to detect a restart-class
# change that the running process cannot honor, and says so out loud.
_relay_boot_sig = None
_hw_initialized = False

# =========================================================
# GPIO BACKEND
# =========================================================
def _init_gpio():
    global _gpio_ready
    if not _GPIO_AVAILABLE:
        _gpio_ready = False
        return False
    try:
        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)
        GPIO.setup(RELAY_CH1_PIN, GPIO.OUT)
        GPIO.setup(RELAY_CH2_PIN, GPIO.OUT)
        _off_level = GPIO.HIGH if RELAY_ACTIVE_LOW else GPIO.LOW
        GPIO.output(RELAY_CH1_PIN, _off_level)
        GPIO.output(RELAY_CH2_PIN, _off_level)
        _gpio_ready = True
        log.info(f"GPIO relay ready on pins {RELAY_CH1_PIN}/{RELAY_CH2_PIN} "
                 f"({'active-low' if RELAY_ACTIVE_LOW else 'active-high'}).")
        return True
    except Exception as e:
        log.error(f"GPIO init failed: {e}")
        _gpio_ready = False
        return False

def _gpio_relay_level(state_on: bool):
    if RELAY_ACTIVE_LOW:
        return GPIO.LOW if state_on else GPIO.HIGH
    return GPIO.HIGH if state_on else GPIO.LOW

def _set_relays_gpio(state_on: bool):
    if not _gpio_ready:
        return False
    try:
        lvl = _gpio_relay_level(state_on)
        GPIO.output(RELAY_CH1_PIN, lvl)
        GPIO.output(RELAY_CH2_PIN, lvl)
        return True
    except Exception as e:
        log.error(f"GPIO relay write failed: {e}")
        return False

# =========================================================
# USB (LCUS-1 serial) BACKEND
# =========================================================
def _init_usb():
    global _usb_serial, _usb_ready
    if not _SERIAL_AVAILABLE:
        _usb_ready = False
        return False
    try:
        _usb_serial = serial.Serial(USB_RELAY_PORT, USB_RELAY_BAUD, timeout=1)
        time.sleep(1.0)
        _usb_serial.write(USB_RELAY_OFF_CMD)
        _usb_ready = True
        log.info(f"USB relay ready on {USB_RELAY_PORT} @ {USB_RELAY_BAUD} baud.")
        return True
    except Exception as e:
        log.error(f"USB relay init failed on {USB_RELAY_PORT}: {e}")
        _usb_ready = False
        return False

def _set_relays_usb(state_on: bool):
    if not _usb_ready or _usb_serial is None:
        return False
    try:
        _usb_serial.write(USB_RELAY_ON_CMD if state_on else USB_RELAY_OFF_CMD)
        return True
    except Exception as e:
        log.error(f"USB relay write failed: {e}")
        return False

# =========================================================
# UNIFIED RELAY CONTROL
# =========================================================
def init_relay():
    if RELAY_MODE == "gpio":
        return _init_gpio()
    elif RELAY_MODE == "usb":
        return _init_usb()
    elif RELAY_MODE == "both":
        gpio_ok = _init_gpio()
        usb_ok = _init_usb()
        if not (gpio_ok or usb_ok):
            log.error("RELAY_MODE='both' but NEITHER backend initialized -- "
                      "DETERRENCE HARDWARE IS DEAD.")
        elif not gpio_ok:
            # Half-dead used to be SILENT: set_relays() returns
            # gpio_result or usb_result, so one working backend masks the other
            # completely and the box looks healthy while half the deterrent
            # never fires. Say it once, loudly, at the only point we know.
            log.error("RELAY_MODE='both' but the GPIO backend is DOWN -- only the "
                      "USB relay will fire. Check that the service user is in the "
                      "'gpio' group and that Jetson.GPIO is installed in the venv.")
        elif not usb_ok:
            log.error("RELAY_MODE='both' but the USB backend is DOWN -- only the "
                      f"GPIO relay will fire. Check {USB_RELAY_PORT} and that the "
                      "service user is in the 'dialout' group.")
        return gpio_ok or usb_ok
    else:
        log.error(f"Unknown RELAY_MODE '{RELAY_MODE}'.")
        return False

def set_relays(state_on: bool):
    with _hw_lock:
        if RELAY_MODE == "gpio":
            return _set_relays_gpio(state_on)
        elif RELAY_MODE == "usb":
            return _set_relays_usb(state_on)
        elif RELAY_MODE == "both":
            gpio_result = _set_relays_gpio(state_on)
            usb_result = _set_relays_usb(state_on)
            return gpio_result or usb_result
        return False

def all_off():
    for _ in range(2):
        set_relays(False)
        time.sleep(0.1)

def relay_health():
    """One-line backend status for the heartbeat, e.g. 'relay=both gpio:ok usb:down'.
    A backend that is not part of the current mode reports '-' rather than 'down',
    so an intentionally unused backend never looks like a fault."""
    if not _hw_initialized:
        return "relay=uninit"
    gpio = "ok" if _gpio_ready else "down"
    usb = "ok" if _usb_ready else "down"
    if RELAY_MODE == "gpio":
        usb = "-"
    elif RELAY_MODE == "usb":
        gpio = "-"
    return f"relay={RELAY_MODE} gpio:{gpio} usb:{usb}"

# =========================================================
# DYNAMIC MULTI-TRACK AUDIO ENGINE
# =========================================================
def init_audio():
    try:
        pygame.mixer.init(frequency=44100, size=-16, channels=2)
        log.info("Audio mixer initialized successfully.")
        return True
    except Exception as e:
        log.error(f"Audio init failed: {e}")
        return False

def resolve_audio_playlist(animal_key):
    config = TARGET_CONFIG.get(animal_key, {})
    raw_files = config.get("audio_files", [])
    playlist = [f.strip() for f in raw_files if f.strip()]

    if not playlist:
        return []

    if AUDIO_PLAYBACK_MODE == "sequential":
        curr_idx = _animal_sound_indices.get(animal_key, 0)
        selected_file = playlist[curr_idx % len(playlist)]
        _animal_sound_indices[animal_key] = curr_idx + 1
        return [selected_file]
    
    return playlist

def _play_audio_loop():
    global _audio_active, _current_audio_playlist
    track_index = 0

    while True:
        if (time.time() - _last_detection_time < BLINK_TOTAL_DURATION) and _audio_active and _current_audio_playlist:
            valid_files = [f for f in _current_audio_playlist if os.path.exists(f)]
            
            if not valid_files:
                log.warning("Target configured for audio, but no valid sound files found on disk.")
                _audio_active = False
                time.sleep(0.5)
                continue

            if AUDIO_PLAYBACK_MODE == "mixed":
                current_file = valid_files[track_index % len(valid_files)]
                track_index += 1
            else:
                current_file = valid_files[0]

            try:
                log.debug(f"Playing acoustic track: {os.path.basename(current_file)}")
                pygame.mixer.music.load(current_file)
                pygame.mixer.music.play()
                
                while pygame.mixer.music.get_busy():
                    if (time.time() - _last_detection_time > BLINK_TOTAL_DURATION) or not _audio_active:
                        pygame.mixer.music.stop()
                        break
                    time.sleep(0.1)
            except Exception as e:
                log.error(f"Audio playback error: {e}")
                time.sleep(0.5)
        else:
            _audio_active = False
            track_index = 0
            time.sleep(0.5)

# =========================================================
# DETERRENCE (BLINK) LOOP
# =========================================================
def _start_deterrence_loop():
    global _deterrence_running
    log.info(f"Starting {BLINK_TOTAL_DURATION}s staged relay cycle (mode={RELAY_MODE})...")
    start_time = time.time()

    try:
        while time.time() - start_time < BLINK_TOTAL_DURATION:
            if not _deterrence_running:
                log.info("Stop received. Aborting loop.")
                break

            phase_start = time.time()
            while time.time() - phase_start < BLINK_ACTIVE_PHASE:
                if not _deterrence_running or (time.time() - start_time >= BLINK_TOTAL_DURATION):
                    break
                set_relays(True)
                time.sleep(BLINK_INTERVAL)
                set_relays(False)
                time.sleep(BLINK_INTERVAL)

            if time.time() - start_time < BLINK_TOTAL_DURATION and _deterrence_running:
                set_relays(False)
                time.sleep(BLINK_REST_PHASE)

    except Exception:
        log.exception("Deterrence loop error")

    all_off()
    _deterrence_running = False
    log.info("Cycle complete.")

# =========================================================
# PUBLIC ENTRY POINT -- call this directly from main script
# =========================================================
def trigger_deterrence(label: str):
    global _last_detection_time, _audio_active, _deterrence_running, _current_audio_playlist

    clean_label = (label or "").strip().lower()
    config = TARGET_CONFIG.get(clean_label)

    if not config:
        log.info(f"[Router] IGNORED TARGET ({clean_label}). No hardware response.")
        return

    action = config.get("action", "none")
    if action == "none":
        log.warning(f"[Router] No response configured for '{clean_label}'.")
        return

    _last_detection_time = time.time()

    if action == "light_and_audio":
        log.info(f"[Router] CRITICAL TARGET ({clean_label.upper()}). Arming Light + Audio.")
        _current_audio_playlist = resolve_audio_playlist(clean_label)
        _audio_active = True
    elif action == "light_only":
        log.info(f"[Router] MEDIUM TARGET ({clean_label.upper()}). Arming Light Only.")
        _audio_active = False

    if not _deterrence_running:
        _deterrence_running = True
        threading.Thread(target=_start_deterrence_loop, daemon=True).start()

def stop_deterrence():
    global _deterrence_running, _audio_active
    _deterrence_running = False
    _audio_active = False
    all_off()

def _relay_sig(dcfg):
    """The restart-class slice of the deterrence config: everything bound once, by
    init_hardware(), before the backends are opened. Used both to apply the config
    at boot and to detect -- later, on a live poll -- that the server has moved on
    from what this process is actually running."""
    gpio = (dcfg.get("gpio") or {})
    usb = (dcfg.get("usb") or {})
    # Normalized, so the drift check below compares MEANING, not spelling: 'a00101a2'
    # and 'A0 01 01 A2' are the same command and must not look like a pending change.
    def _hex(v, default):
        return str(v if v is not None else default).strip().replace(" ", "").upper()
    return {
        "relay_mode": str(dcfg.get("relay_mode", RELAY_MODE)).strip().lower(),
        "ch1_pin": int(gpio.get("ch1_pin", RELAY_CH1_PIN)),
        "ch2_pin": int(gpio.get("ch2_pin", RELAY_CH2_PIN)),
        "active_low": bool(gpio.get("active_low", RELAY_ACTIVE_LOW)),
        "port": str(usb.get("port", USB_RELAY_PORT)).strip(),
        "baud": int(usb.get("baud", USB_RELAY_BAUD)),
        "on_cmd_hex": _hex(usb.get("on_cmd_hex"), USB_RELAY_ON_CMD.hex()),
        "off_cmd_hex": _hex(usb.get("off_cmd_hex"), USB_RELAY_OFF_CMD.hex()),
    }


def _parse_hex_cmd(raw, fallback, what):
    """'A00101A2' -> b'\\xa0\\x01\\x01\\xa2'. A typo in this field on the admin page
    must not take the USB relay down, so a bad value keeps the working default."""
    try:
        b = bytes.fromhex(str(raw).strip().replace(" ", ""))
        if not b:
            raise ValueError("empty")
        return b
    except Exception as e:
        log.error(f"USB {what} command {raw!r} is not valid hex ({e}) -- "
                  f"keeping {fallback.hex().upper()}")
        return fallback


def init_hardware(dcfg):
    """Bind the restart-class relay settings from the server config, then open the
    relay backends and the audio engine. Call this ONCE, as early as the boot
    config snapshot exists -- before that snapshot there is nothing to configure
    from, and this module deliberately touches no hardware at import.

    Everything bound here is fixed for the process lifetime: the GPIO pins are
    exported and the serial port is opened against these values. A later change on
    the server is reported by apply_config() and takes effect on restart."""
    global RELAY_MODE, RELAY_CH1_PIN, RELAY_CH2_PIN, RELAY_ACTIVE_LOW
    global USB_RELAY_PORT, USB_RELAY_BAUD, USB_RELAY_ON_CMD, USB_RELAY_OFF_CMD
    global _relay_boot_sig, _hw_initialized

    if _hw_initialized:
        log.warning("init_hardware() called twice -- ignoring the second call.")
        return
    _hw_initialized = True

    # A malformed value here (non-numeric pin or baud) must not leave the box with
    # NO deterrent at all -- fall back to the shipped defaults and say so.
    try:
        sig = _relay_sig(dcfg or {})
    except Exception:
        log.exception("Relay config is malformed -- falling back to built-in defaults")
        sig = _relay_sig({})

    if sig["relay_mode"] not in ("gpio", "usb", "both"):
        log.error(f"Unknown relay_mode {sig['relay_mode']!r} from config -- "
                  f"falling back to {RELAY_MODE!r}.")
        sig["relay_mode"] = RELAY_MODE

    RELAY_MODE = sig["relay_mode"]
    RELAY_CH1_PIN = int(sig["ch1_pin"])
    RELAY_CH2_PIN = int(sig["ch2_pin"])
    RELAY_ACTIVE_LOW = bool(sig["active_low"])
    USB_RELAY_PORT = str(sig["port"])
    USB_RELAY_BAUD = int(sig["baud"])
    USB_RELAY_ON_CMD = _parse_hex_cmd(sig["on_cmd_hex"], USB_RELAY_ON_CMD, "ON")
    USB_RELAY_OFF_CMD = _parse_hex_cmd(sig["off_cmd_hex"], USB_RELAY_OFF_CMD, "OFF")
    _relay_boot_sig = sig

    log.info(f"Deterrence hardware config: mode={RELAY_MODE}, "
             f"gpio pins={RELAY_CH1_PIN}/{RELAY_CH2_PIN} "
             f"({'active-low' if RELAY_ACTIVE_LOW else 'active-high'}), "
             f"usb={USB_RELAY_PORT}@{USB_RELAY_BAUD}")

    init_relay()
    if init_audio():
        threading.Thread(target=_play_audio_loop, daemon=True,
                         name="deterrence-audio").start()


def apply_config(dcfg):
    """Live-apply server-managed deterrence config. TARGET_CONFIG (per-class
    action + audio files), AUDIO_PLAYBACK_MODE and the BLINK_* timings update
    immediately -- the audio and blink loops re-read these globals each iteration.

    Relay mode / GPIO pins / USB port are bound once by init_hardware() and are
    RESTART-class: the pins are already exported and the serial port already open,
    so they cannot be swapped under a running blink loop. They are not applied
    here -- but a change IS reported, because a silently ignored relay-mode change
    is exactly the failure this module used to have."""
    global TARGET_CONFIG, AUDIO_PLAYBACK_MODE
    global BLINK_TOTAL_DURATION, BLINK_ACTIVE_PHASE, BLINK_REST_PHASE, BLINK_INTERVAL
    if not dcfg:
        return

    # Isolated: _relay_sig() coerces types, so a garbage pin or baud in the config
    # raises here. That must not stop the targets and blink timings below from
    # applying -- reporting a restart-class change is strictly less important than
    # keeping the live-appliable config flowing.
    if _relay_boot_sig is not None:
        try:
            drift = {k: (v, _relay_boot_sig[k])
                     for k, v in _relay_sig(dcfg).items() if v != _relay_boot_sig[k]}
            if drift:
                log.warning(
                    "Relay config changed on the server but CANNOT be hot-applied; this "
                    "process is still running the boot values. Restart to pick them up: "
                    + ", ".join(f"{k}: running={old!r} server={new!r}"
                                for k, (new, old) in drift.items()))
        except Exception:
            log.exception("Relay drift check failed -- continuing with the live config")

    with _hw_lock:
        targets = dcfg.get("targets")
        if isinstance(targets, dict):
            TARGET_CONFIG = {
                str(k).strip().lower(): {
                    "action": v.get("action", "none"),
                    "audio_files": list(v.get("audio_files", [])),
                }
                for k, v in targets.items()
            }
        if dcfg.get("audio_playback_mode"):
            AUDIO_PLAYBACK_MODE = dcfg["audio_playback_mode"]
        blink = dcfg.get("blink") or {}
        BLINK_TOTAL_DURATION = blink.get("total_duration", BLINK_TOTAL_DURATION)
        BLINK_ACTIVE_PHASE = blink.get("active_phase", BLINK_ACTIVE_PHASE)
        BLINK_REST_PHASE = blink.get("rest_phase", BLINK_REST_PHASE)
        BLINK_INTERVAL = blink.get("interval", BLINK_INTERVAL)
    log.info(f"Applied config: {len(TARGET_CONFIG)} targets, "
             f"audio={AUDIO_PLAYBACK_MODE}, blink_total={BLINK_TOTAL_DURATION}s")

# =========================================================
# NO MODULE INIT -- the caller owns it
# =========================================================
# Importing this module must not open a serial port, export a GPIO pin or grab
# the audio device. init_hardware(cfg["deterrence"]) does all three, and only the
# caller knows when the config snapshot it needs is available.
