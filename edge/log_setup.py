"""log_setup.py -- one durable log sink for every Deerkha Drishti edge process.

WHY THIS EXISTS
    The pipeline already used stdlib `logging`, but it wrote to stderr only, with
    no date in the timestamp, no thread names, no tracebacks and no file. The
    Jetson runs 24/7 in the field, so "the log" was whatever happened to still be
    in the terminal scrollback. This module gives every edge process a rotating
    file sink that survives reboots, plus crash hooks so a dying thread says so.

USAGE
    Call setup() as the FIRST thing in any entrypoint, BEFORE importing any
    pipeline module. trigger_jetson_3 runs init_relay() at import time, and its
    "relay ready" / "relay init failed" lines are exactly the evidence you need
    when the deterrent doesn't fire -- they must land in the file, not on a lost
    stdout.

        import log_setup
        log_setup.setup(app="deerkha")

    Pure stdlib, so it can be exercised on the office PC without a Jetson.

ENV VARS (all optional)
    LOG_LEVEL      DEBUG|INFO|WARNING|ERROR      default INFO
    LOG_DIR        where the files go            default <this dir>/logs
    LOG_MAX_BYTES  bytes per file before rollover default 8388608 (8 MB)
    LOG_BACKUPS    how many rolled files to keep default 5  -> 48 MB ceiling
    LOG_CONSOLE    0 to silence stderr           default 1
"""

import atexit
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

_HERE = os.path.dirname(os.path.abspath(__file__))
_configured = False
_setup_lock = threading.Lock()

# Libraries that become loud the moment the ROOT logger has handlers. Before this
# module we configured the "DHEERKHA" logger only, so none of these were visible;
# root config exposes them all and they will drown the log if left at INFO.
_NOISY = (
    "urllib3", "urllib3.connectionpool", "requests", "PIL", "PIL.Image",
    "matplotlib", "matplotlib.font_manager", "sqlalchemy", "sqlalchemy.engine",
    "open_clip", "asyncio", "gi", "chardet", "charset_normalizer",
)

_FILE_FMT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_CONSOLE_FMT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s"


def log_dir() -> str:
    """Where log files live. Deliberately NOT under storage_root: logs must not
    sit on the filesystem whose exhaustion you are trying to diagnose, and the
    storage cleanup can't reclaim them there anyway."""
    return os.environ.get("LOG_DIR") or os.path.join(_HERE, "logs")


def log_path(app: str = "deerkha") -> str:
    return os.path.join(log_dir(), f"{app}.log")


def setup(app: str = "deerkha", console: bool = True) -> logging.Logger:
    """Configure the ROOT logger. Idempotent -- safe to call from several
    entrypoints. Returns the root logger.

    Root rather than the "DHEERKHA" logger so modules that don't know about our
    naming convention (rfdetr_trt, clip_trt, trigger_jetson_3) can just use
    getLogger(__name__) and propagate for free. Existing getLogger("DHEERKHA")
    callers keep working unchanged -- they now propagate to root's handlers.
    """
    global _configured
    with _setup_lock:
        if _configured:
            return logging.getLogger()

        d = log_dir()
        os.makedirs(d, exist_ok=True)

        level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
        max_bytes = int(os.environ.get("LOG_MAX_BYTES", 8 * 1024 * 1024))
        backups = int(os.environ.get("LOG_BACKUPS", 5))

        root = logging.getLogger()
        root.setLevel(level)
        # Drop anything a stray basicConfig() left behind, so we don't double-log.
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

        # Size-bounded, not time-bounded. This device already has a disk-full
        # problem and the highest-value instrumentation site is a per-frame hot
        # loop -- a nightly-rotating file would happily grow to gigabytes before
        # any retention cap engaged. maxBytes gives a hard ceiling regardless of
        # event rate.
        fh = RotatingFileHandler(log_path(app), maxBytes=max_bytes,
                                 backupCount=backups, encoding="utf-8", delay=False)
        fh.setFormatter(logging.Formatter(_FILE_FMT, datefmt="%Y-%m-%d %H:%M:%S"))
        fh.setLevel(level)
        root.addHandler(fh)

        if console and os.environ.get("LOG_CONSOLE", "1") != "0":
            ch = logging.StreamHandler(sys.stderr)
            ch.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt="%H:%M:%S"))
            ch.setLevel(level)
            root.addHandler(ch)

        for n in _NOISY:
            logging.getLogger(n).setLevel(logging.WARNING)

        # A failed log write must NEVER raise into a camera thread. The one time
        # the disk is full is the one time we most need the pipeline to keep
        # running, and that is exactly when a handler write fails.
        logging.raiseExceptions = False

        _install_crash_hooks()

        # Native crashes (TensorRT / CUDA / GStreamer segfaults) never reach
        # Python's exception machinery -- faulthandler is the only way to get a
        # stack out of them.
        try:
            import faulthandler
            faulthandler.enable(open(os.path.join(d, f"{app}.faulthandler"), "a"))
        except Exception:
            pass

        atexit.register(logging.shutdown)
        _configured = True

        logging.getLogger("startup").info(
            "log sink: %s (level=%s max=%dB backups=%d ceiling=%.0fMB)",
            log_path(app), logging.getLevelName(level), max_bytes, backups,
            max_bytes * (backups + 1) / 1024 / 1024)
        return root


def flush():
    """Flush every handler. Call immediately before os._exit(), which bypasses
    atexit and logging.shutdown()."""
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass


# --------------------------------------------------------------------------
# crash hooks
# --------------------------------------------------------------------------

def _install_crash_hooks():
    """Make an uncaught exception impossible to miss.

    The pipeline runs ~16 daemon threads (2-3 per camera plus config-poll,
    heartbeat, stream server, cleanup, video writers). Without a threading
    excepthook a thread that dies takes its traceback to a raw stderr write and
    then simply stops existing -- the camera goes quiet and nothing says why.
    """
    log = logging.getLogger("crash")

    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return sys.__excepthook__(exc_type, exc, tb)
        log.critical("UNCAUGHT in MainThread", exc_info=(exc_type, exc, tb))
        flush()

    sys.excepthook = _excepthook

    def _threadhook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        name = getattr(args.thread, "name", "?")
        log.critical("UNCAUGHT in thread %r -- THREAD IS NOW DEAD", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        flush()

    try:
        threading.excepthook = _threadhook          # py3.8+
    except Exception:
        pass

    def _sigterm(signum, frame):
        log.warning("SIGTERM received -- shutting down")
        flush()
        os._exit(143)

    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except Exception:
        # Not the main thread, or a platform without SIGTERM. Non-fatal.
        pass


# --------------------------------------------------------------------------
# throttle
# --------------------------------------------------------------------------

class Throttle:
    """Bounded logging for hot paths.

    Emits the first `burst` occurrences of a key, then at most one per `every`
    seconds, reporting how many were suppressed in between. Needed because the
    single highest-value thing to instrument is a per-frame inference loop: at
    ~10fps x 4 cameras an unthrottled log.exception() there would fill the disk
    this whole change is meant to protect.

        THROTTLE.exception("rfdetr.infer", "TensorRT inference failed")
    """

    def __init__(self, logger, burst=3, every=60.0):
        self._log = logger
        self._burst = burst
        self._every = every
        self._state = {}
        self._lk = threading.Lock()

    def _should(self, key, now):
        with self._lk:
            st = self._state.setdefault(key, {"n": 0, "last": 0.0, "sup": 0})
            st["n"] += 1
            if st["n"] <= self._burst or (now - st["last"]) >= self._every:
                sup = st["sup"]
                st["sup"] = 0
                st["last"] = now
                return True, st["n"], sup
            st["sup"] += 1
            return False, st["n"], st["sup"]

    def _emit(self, lvl, key, msg, exc_info, *a):
        ok, n, sup = self._should(key, time.time())
        if not ok:
            return
        suffix = f"  [#{n} for '{key}'" + (f"; {sup} suppressed]" if sup else "]")
        self._log.log(lvl, msg + suffix, *a, exc_info=exc_info)

    def exception(self, key, msg, *a):
        self._emit(logging.ERROR, key, msg, True, *a)

    def error(self, key, msg, *a):
        self._emit(logging.ERROR, key, msg, False, *a)

    def warning(self, key, msg, *a):
        self._emit(logging.WARNING, key, msg, False, *a)

    def info(self, key, msg, *a):
        self._emit(logging.INFO, key, msg, False, *a)

    def counts(self):
        """{key: total occurrences} -- including suppressed ones. Handy for the
        heartbeat, so the dashboard can show that something is wrong even when
        the log line itself is being throttled."""
        with self._lk:
            return {k: v["n"] for k, v in self._state.items() if v["n"]}

    def total(self):
        with self._lk:
            return sum(v["n"] for v in self._state.values())


# --------------------------------------------------------------------------
# event JSONL
# --------------------------------------------------------------------------

def event_logger(app="events", max_bytes=4 * 1024 * 1024, backups=3) -> logging.Logger:
    """A second, isolated logger whose records ARE the JSON line.

    Reuses RotatingFileHandler purely for its bounded retention -- no bespoke
    rotation code. propagate=False keeps machine-readable events out of the
    human-readable main log.
    """
    lg = logging.getLogger("_events")
    if lg.handlers:
        return lg
    os.makedirs(log_dir(), exist_ok=True)
    h = RotatingFileHandler(os.path.join(log_dir(), f"{app}.jsonl"),
                            maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg
