"""
retention.py -- nightly database housekeeping.

Run from the server/ directory, in the same venv as the app:

    python -m app.jobs.retention

Schedule it with a Windows Scheduled Task (daily, ~03:00). It is deliberately
an external task rather than an in-process thread: an in-process scheduler runs
once per uvicorn worker, so it would either need distributed locking or would
silently do the work N times.

Why this exists
---------------
cfg_device_status gains one row per device per heartbeat interval (default 60s)
and NOTHING ever prunes it. Only the newest row per device is ever read
(admin_api /status/latest). At 7 devices x 7 cameras that is ~10,000 rows and
~12.6 MB per DAY, which exhausts a 500 MB Supabase project in about 40 days.

That failure is a cliff, not a slope: when the project hits its quota it goes
read-only, which simultaneously kills config serving, heartbeat ingest AND the
edge's direct detection inserts. A table nobody reads takes down the fleet.

IMPORTANT -- reclaiming existing bloat
--------------------------------------
DELETE marks rows dead but does not return pages to the OS, so the Supabase
storage gauge will NOT drop after the first run. To reclaim an existing
backlog, run ONCE by hand in the Supabase SQL editor:

    VACUUM FULL cfg_device_status;

That takes a brief exclusive lock (seconds on this table). Afterwards routine
autovacuum keeps the table flat and this job is all you need.
"""

import argparse
import logging
import os
import sys

from sqlalchemy import text

from ..assembly import assemble_config
from ..db import SessionLocal, engine
from .. import cache, models, settings

log = logging.getLogger("deerkha.retention")

# Keep enough status history to debug a bad night, not enough to matter.
STATUS_KEEP_DAYS = 7
AUDIT_KEEP_DAYS = 180

# Detection history. 0 disables pruning entirely.
#
# This used to be somebody else's problem: on a hosted database, unbounded
# growth showed up as a quota warning from the provider. Self-hosted, it is
# just disk -- and the field devices insert into this table directly, so a
# misbehaving box (or the stale-inference storm this project already fixed
# once) can fill it without any operator action. Two years is generous for a
# detection index whose actual evidence -- the video and stills -- lives on the
# Jetsons and is pruned there on a 3-day window.
DETECTIONS_KEEP_DAYS = int(os.environ.get("DD_DETECTIONS_KEEP_DAYS", "730"))

# Delete in batches so a large backlog never takes one long lock or blows up
# the WAL in a single statement.
BATCH = 5000


def _prune(conn, table: str, ts_column: str, keep_days: int, dry_run: bool) -> int:
    total = 0
    while True:
        if dry_run:
            n = conn.execute(text(
                f"SELECT count(*) FROM {table} "
                f"WHERE {ts_column} < now() - interval '{keep_days} days'"
            )).scalar() or 0
            return int(n)
        res = conn.execute(text(
            f"DELETE FROM {table} WHERE ctid IN ("
            f"  SELECT ctid FROM {table}"
            f"  WHERE {ts_column} < now() - interval '{keep_days} days'"
            f"  LIMIT {BATCH}"
            f")"
        ))
        deleted = res.rowcount or 0
        total += deleted
        conn.commit()
        if deleted < BATCH:
            return total


def _prune_orphan_frames(dry_run: bool) -> tuple[int, int]:
    """Delete uploaded reference frames that no camera points at any more.

    dashboard.py names each upload with a fresh random suffix, so re-uploading
    a frame for a camera abandons the previous file rather than replacing it --
    and deleting a camera removes the row but never the file. Neither is
    dramatic on its own; together they are a leak with no upper bound and no
    owner. Returns (files_removed, bytes_freed).
    """
    d = settings.REFERENCE_FRAME_DIR
    if not os.path.isdir(d):
        return 0, 0

    db = SessionLocal()
    try:
        referenced = {
            os.path.basename(u) for (u,) in
            db.query(models.Camera.reference_frame_url)
              .filter(models.Camera.reference_frame_url.isnot(None)).all()
            if u
        }
    finally:
        db.close()

    removed = freed = 0
    for name in os.listdir(d):
        if name in referenced:
            continue
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
            if not dry_run:
                os.remove(path)
            removed += 1
            freed += size
        except OSError as e:
            log.warning("could not remove orphan frame %s: %s", path, e)
    return removed, freed


def _reconcile_versions(dry_run: bool) -> int:
    """Recompute and store config_version for every device.

    The device API now serves the STORED cfg_devices.config_version instead of
    re-assembling on every poll. The one thing that can make that stale is
    someone editing cfg_* rows directly in the Supabase SQL editor, which
    bypasses bump_and_audit(). This nightly pass closes that gap; the dashboard
    Resync button covers the impatient case.
    """
    fixed = 0
    db = SessionLocal()
    try:
        for dev in db.query(models.Device).all():
            blob = assemble_config(db, dev.device_id)
            if blob is None:
                continue
            real = blob["config_version"]
            if dev.config_version != real:
                log.warning("device %s config_version drifted (%s -> %s)",
                            dev.device_id, dev.config_version, real)
                fixed += 1
                if not dry_run:
                    dev.config_version = real
            cache.put(dev.device_id, real, blob)
        if not dry_run:
            db.commit()
    finally:
        db.close()
    return fixed


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Deerkha Drishti DB retention")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be deleted, change nothing")
    p.add_argument("--status-days", type=int, default=STATUS_KEEP_DAYS)
    p.add_argument("--audit-days", type=int, default=AUDIT_KEEP_DAYS)
    p.add_argument("--detections-days", type=int, default=DETECTIONS_KEEP_DAYS,
                   help="0 disables detection pruning")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    verb = "would delete" if args.dry_run else "deleted"

    with engine.connect() as conn:
        n = _prune(conn, "cfg_device_status", "received_at", args.status_days, args.dry_run)
        log.info("cfg_device_status: %s %d rows older than %d days", verb, n, args.status_days)
        n = _prune(conn, "cfg_audit_log", "created_at", args.audit_days, args.dry_run)
        log.info("cfg_audit_log: %s %d rows older than %d days", verb, n, args.audit_days)
        if args.detections_days > 0:
            # raw_timestamp is EPOCH SECONDS (double precision) -- the edge writes
            # time.time() -- while timestamp is timestamptz. Coalescing them
            # directly is a type error and the whole prune throws. Convert first.
            # raw_timestamp is nullable, so keep the fallback rather than silently
            # skipping those rows forever.
            n = _prune(conn, "detections",
                       "COALESCE(to_timestamp(raw_timestamp), timestamp)",
                       args.detections_days, args.dry_run)
            log.info("detections: %s %d rows older than %d days",
                     verb, n, args.detections_days)
        else:
            log.info("detections: pruning disabled (--detections-days 0)")

    removed, freed = _prune_orphan_frames(args.dry_run)
    log.info("reference frames: %s %d orphaned file(s), %.1f MB",
             verb, removed, freed / (1024 * 1024))

    fixed = _reconcile_versions(args.dry_run)
    log.info("config_version reconcile: %d device(s) %s",
             fixed, "would be corrected" if args.dry_run else "corrected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
