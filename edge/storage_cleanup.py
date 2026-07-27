"""
storage_cleanup.py -- reclaim disk by deleting whole day-folders.

Layout it operates on (built by get_dynamic_paths() in the main pipeline):

    STORAGE_ROOT/
      2026-07-25/                      <- the deletable unit (YYYY-MM-DD)
        DAY/ | NIGHT/
          CAM_1/ CAM_2/ ...
            detection_frames/ motion_video/ animal_video/
            animal_detection_confirmation/ clip_validation/

The date is the TOP level, so deleting one folder removes every camera and both
DAY/NIGHT for that day atomically.

TWO RULES, both run on every pass:
  1. AGE   -- delete date-folders older than `days_to_keep`.
  2. SPACE -- while free% < `low_space_free_percent`, delete the OLDEST folder,
              re-measure, repeat. Stops at the `min_keep` floor.

HOW IT RUNS
    Primarily as an in-process thread inside the main pipeline (cleanup_loop()),
    which is what makes the night-window protection below reliable. This file
    also stays runnable standalone as a manual/rescue tool:

        python storage_cleanup.py --dry-run -v      # report only, delete nothing
        python storage_cleanup.py                   # apply, using cleanup_config.json
        python storage_cleanup.py --storage-root /tmp/test --days 3 --dry-run

    Retention VALUES come from the dashboard (cleanup_days_to_keep /
    cleanup_low_space_free_percent), delivered via config and written to
    cleanup_config.json by the pipeline. Don't edit them here.

THE NIGHT-WINDOW HAZARD (why effective_today() exists)
    Between midnight and night_window.end (default 06:31) the pipeline writes
    into YESTERDAY's date folder -- get_dynamic_paths() back-dates so a night's
    footage stays in one folder. A naive `folder_date < today` guard therefore
    considers a folder that four VideoWriters are actively writing into to be
    fair game. Under low-space pressure at 03:00 that is real data loss, and it
    is close to impossible to reproduce on demand. effective_today() mirrors the
    pipeline's arithmetic exactly, and callers can pass `protect=` with the dates
    the pipeline is genuinely writing to right now.
"""

import argparse
import datetime
import json
import logging
import os
import shutil
import sys
import time

try:
    import fcntl                                     # POSIX only; absent on Windows
except ImportError:                                  # pragma: no cover
    fcntl = None

# =====================================================================
# Fallback defaults -- only used if cleanup_config.json doesn't exist yet.
# The pipeline rewrites that file at boot and on every config change, so in
# normal operation these are never reached.
# =====================================================================
DEFAULT_STORAGE_ROOT = "/mnt/data/video_storage"
DEFAULT_DAYS_TO_KEEP = 3
# Matches the server default and the checked-in cleanup_config.json. This was 30,
# which silently behaved very differently from the dashboard whenever the JSON
# file was missing.
DEFAULT_LOW_SPACE_FREE_PERCENT = 10

# Never leave fewer than this many deletable date-folders, no matter how full the
# disk is. Without a floor, a disk filled by NON-video data would drive the
# low-space loop to delete the entire recording history and still not recover.
DEFAULT_MIN_KEEP_FOLDERS = 2

# Runaway guard: one pass should never delete more than this.
MAX_DELETES_PER_RUN = 30

# Default night-window end, only used when the caller doesn't pass the live
# config values. Mirrors config_client.DEFAULTS["night_window"].
DEFAULT_NIGHT_END_HOUR = 6
DEFAULT_NIGHT_END_MINUTE = 31

_HERE = os.path.dirname(os.path.abspath(__file__))

# Mutable state goes to the state dir, not next to the code: under the fleet
# layout the code directory is an immutable per-release tree that a deploy
# replaces wholesale. Must match main.py's _STATE_DIR.
_STATE_DIR = os.environ.get("DD_STATE_DIR", "/var/lib/deerkha")
if not os.path.isdir(_STATE_DIR):
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
    except OSError:
        _STATE_DIR = _HERE

CONFIG_PATH = os.path.join(_STATE_DIR, "cleanup_config.json")
LOCK_PATH = os.path.join(_STATE_DIR, ".cleanup.lock")

log = logging.getLogger("CLEANUP")


def load_config():
    """(storage_root, days_to_keep, low_space_free_percent) from cleanup_config.json."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            log.info(f"Loaded config from {CONFIG_PATH}: {cfg}")
            return (
                cfg.get("storage_root", DEFAULT_STORAGE_ROOT),
                int(cfg.get("days_to_keep", DEFAULT_DAYS_TO_KEEP)),
                float(cfg.get("low_space_free_percent", DEFAULT_LOW_SPACE_FREE_PERCENT)),
            )
        except Exception as e:
            log.warning(f"Failed to read {CONFIG_PATH} ({e}) -- using built-in defaults.")
    else:
        log.warning(f"{CONFIG_PATH} not found (pipeline hasn't run yet?) -- using built-in defaults.")
    return DEFAULT_STORAGE_ROOT, DEFAULT_DAYS_TO_KEEP, DEFAULT_LOW_SPACE_FREE_PERCENT


# =====================================================================
# date arithmetic -- must stay in lockstep with get_dynamic_paths()
# =====================================================================

def effective_today(end_hour=DEFAULT_NIGHT_END_HOUR,
                    end_minute=DEFAULT_NIGHT_END_MINUTE, now=None):
    """The date the pipeline is CURRENTLY WRITING INTO.

    Mirrors get_dynamic_paths() in the main script: before night_window.end the
    footage still belongs to yesterday's folder. Using date.today() here instead
    would mark a live folder as deletable.
    """
    now = now or datetime.datetime.now()
    if now.hour < end_hour or (now.hour == end_hour and now.minute < end_minute):
        return (now - datetime.timedelta(days=1)).date()
    return now.date()


def protected_dates(end_hour=DEFAULT_NIGHT_END_HOUR,
                    end_minute=DEFAULT_NIGHT_END_MINUTE, now=None, extra=()):
    """Never-touch set.

    Includes BOTH the effective (possibly back-dated) day and the plain calendar
    day: just after the window closes at 06:31 the effective date flips forward
    while writers may still be finalizing yesterday's mp4. `extra` carries the
    dates the running pipeline says it is actually writing to.
    """
    now = now or datetime.datetime.now()
    s = {effective_today(end_hour, end_minute, now), now.date()}
    for d in extra or ():
        if isinstance(d, datetime.datetime):
            d = d.date()
        if isinstance(d, datetime.date):
            s.add(d)
    return s


# =====================================================================
# safety
# =====================================================================

def validate_root(p):
    """Refuse to operate anywhere dangerous. storage_root is server-editable, so
    a bad value can be pushed remotely -- and this function calls shutil.rmtree."""
    if not p or not isinstance(p, str):
        raise ValueError("storage_root is empty or not a string")
    ap = os.path.abspath(p)
    if ap in ("/", os.path.sep) or ap.rstrip(os.sep).count(os.sep) < 2:
        raise ValueError(f"refusing to operate on shallow/root path: {ap!r}")
    if os.path.islink(ap):
        raise ValueError(f"storage_root is a symlink, refusing: {ap!r}")
    if not os.path.isdir(ap):
        raise ValueError(f"storage_root is not a directory: {ap!r}")
    return ap


def _date_folders(storage_root):
    """[(name, date, full_path)] for every YYYY-MM-DD folder directly under the
    root, oldest first. Anything that doesn't parse as a date is skipped, so an
    unrelated folder someone dropped in here can never be deleted."""
    out = []
    if not os.path.isdir(storage_root):
        log.warning(f"Storage root does not exist: {storage_root}")
        return out
    for name in os.listdir(storage_root):
        full = os.path.join(storage_root, name)
        if not os.path.isdir(full):
            continue
        if os.path.islink(full):
            # Never follow a symlink into an rmtree.
            log.warning(f"Skipping symlink: {full}")
            continue
        try:
            d = datetime.datetime.strptime(name, "%Y-%m-%d").date()
        except ValueError:
            continue
        out.append((name, d, full))
    out.sort(key=lambda t: t[1])
    return out


def _dir_size(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def _delete_folder(path, reason, dry_run):
    size = _dir_size(path)
    if dry_run:
        log.warning("DRY-RUN would delete %s (%.2f GB) -- %s", path, size / 1024 ** 3, reason)
        return size
    try:
        shutil.rmtree(path)
        log.warning("DELETED %s (%.2f GB) -- %s", path, size / 1024 ** 3, reason)
        return size
    except Exception:
        log.exception("Failed to delete %s", path)
        return 0


# =====================================================================
# the pass
# =====================================================================

def run_once(storage_root, days_to_keep, low_space_free_percent,
             night_end_hour=DEFAULT_NIGHT_END_HOUR,
             night_end_minute=DEFAULT_NIGHT_END_MINUTE,
             protect=(), dry_run=False, min_keep=DEFAULT_MIN_KEEP_FOLDERS,
             now=None):
    """Run both rules once. Returns a summary dict (also used for the heartbeat).

    `protect` -- extra dates that must never be deleted. The in-process caller
    passes the date folders its camera threads most recently created, which makes
    the night-window guard correct by construction rather than by clock
    arithmetic agreeing across two processes.
    """
    root = validate_root(storage_root)
    prot = protected_dates(night_end_hour, night_end_minute, now, extra=protect)
    eff = effective_today(night_end_hour, night_end_minute, now)

    log.info("cleanup pass: root=%s keep=%dd free_floor=%.0f%% effective_today=%s "
             "protected=%s min_keep=%d dry_run=%s",
             root, days_to_keep, low_space_free_percent, eff,
             sorted(str(d) for d in prot), min_keep, dry_run)

    deleted, freed = [], 0

    # ---- RULE 1: AGE ----
    # keep=3 must leave exactly 3 folders: {eff, eff-1, eff-2}. The old cutoff of
    # (today - days_to_keep) with a strict `<` left days_to_keep + 1.
    cutoff = eff - datetime.timedelta(days=days_to_keep - 1)
    for name, d, full in _date_folders(root):
        if d in prot:
            continue
        if d < cutoff:
            freed += _delete_folder(full, f"age rule: older than cutoff {cutoff}", dry_run)
            deleted.append(name)

    # ---- RULE 2: LOW SPACE ----
    total, _used, free = shutil.disk_usage(root)
    free_pct = free / total * 100.0
    log.info("disk free %.1f%% (%.1f GB of %.1f GB)",
             free_pct, free / 1024 ** 3, total / 1024 ** 3)

    guard = 0
    while free_pct < low_space_free_percent and guard < MAX_DELETES_PER_RUN:
        candidates = [f for f in _date_folders(root) if f[1] not in prot]
        if len(candidates) <= min_keep:
            log.warning("low-space: only %d deletable folder(s) left (floor %d) -- STOPPING "
                        "at %.1f%% free. The disk is full of NON-VIDEO data, or retention "
                        "is already at its minimum.", len(candidates), min_keep, free_pct)
            break
        name, _d, full = candidates[0]                       # oldest first
        size = _delete_folder(
            full, f"low-space rule ({free_pct:.1f}% < {low_space_free_percent}%)", dry_run)
        freed += size
        deleted.append(name)
        guard += 1
        if dry_run:
            free += size                                     # simulate, else infinite loop
        else:
            # Don't monopolise IO -- the video writers share this disk and their
            # queues are only 50 frames deep.
            time.sleep(2.0)
            _t, _u, free = shutil.disk_usage(root)
        free_pct = free / total * 100.0

    if guard >= MAX_DELETES_PER_RUN:
        log.error("low-space: hit MAX_DELETES_PER_RUN=%d -- stopping. Investigate why so "
                  "much needs reclaiming in one pass.", MAX_DELETES_PER_RUN)

    log.info("cleanup done: %d folder(s), %.2f GB reclaimed, %.1f%% free",
             len(deleted), freed / 1024 ** 3, free_pct)
    return {
        "deleted": deleted,
        "freed_bytes": freed,
        "free_pct": round(free_pct, 1),
        "dry_run": bool(dry_run),
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
    }


class Lock:
    """Best-effort single-runner lock, so a manual run can't race the in-process
    thread into the same rmtree. No-op on platforms without fcntl."""

    def __init__(self, path=LOCK_PATH):
        self.path = path
        self.fh = None

    def __enter__(self):
        if fcntl is None:
            return self
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            raise RuntimeError("another cleanup run holds the lock -- skipping this pass")
        return self

    def __exit__(self, *a):
        if self.fh:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            finally:
                self.fh.close()
                self.fh = None


def main():
    ap = argparse.ArgumentParser(
        description="Deerkha Drishti storage cleanup (age + low-space rules).")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what WOULD be deleted; delete nothing")
    ap.add_argument("--storage-root", help="override the configured storage root")
    ap.add_argument("--days", type=int, help="override days_to_keep")
    ap.add_argument("--free-pct", type=float, help="override low_space_free_percent")
    ap.add_argument("--min-keep", type=int, default=DEFAULT_MIN_KEEP_FOLDERS,
                    help=f"never leave fewer than this many date folders (default {DEFAULT_MIN_KEEP_FOLDERS})")
    ap.add_argument("--night-end-hour", type=int, default=DEFAULT_NIGHT_END_HOUR)
    ap.add_argument("--night-end-minute", type=int, default=DEFAULT_NIGHT_END_MINUTE)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if a.verbose:
        os.environ["LOG_LEVEL"] = "DEBUG"
    try:
        import log_setup
        # Own file: RotatingFileHandler rollover is not safe across processes, and
        # the pipeline owns deerkha.log.
        log_setup.setup(app="cleanup")
    except Exception:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    root, days, pct = load_config()
    try:
        with Lock():
            run_once(a.storage_root or root,
                     a.days if a.days is not None else days,
                     a.free_pct if a.free_pct is not None else pct,
                     night_end_hour=a.night_end_hour,
                     night_end_minute=a.night_end_minute,
                     dry_run=a.dry_run,
                     min_keep=a.min_keep)
    except Exception:
        log.exception("cleanup run aborted")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
