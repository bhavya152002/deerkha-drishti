"""
service.py -- shared write-side helpers.

Every admin config edit must go through bump_and_audit() after committing the
row change so that (1) cfg_devices.config_version is recomputed from the current
rows and (2) an audit row is written. Keeping this in one place guarantees the
edge's /version poll always reflects the latest edit.
"""

from sqlalchemy.orm import Session

from . import models, cache
from .assembly import assemble_config


def bump_and_audit(
    db: Session,
    device_id: str,
    *,
    actor: str,
    table_name: str,
    row_pk: str,
    action: str,
    diff: dict | None = None,
) -> str | None:
    """Recompute + store config_version for the device, and write an audit row.
    Must be called with row changes already flushed/committed in the same or a
    prior transaction. Returns the new config_version (or None if no device)."""
    blob = assemble_config(db, device_id)
    new_version = blob["config_version"] if blob else None

    dev = db.get(models.Device, device_id)
    if dev is not None and new_version is not None:
        dev.config_version = new_version

    # Every config mutation funnels through here, and it has already paid for
    # the assembly -- so seed the cache instead of throwing the blob away. The
    # device that is about to poll for this exact version then gets it without
    # touching the DB at all.
    if new_version is not None:
        cache.put(device_id, new_version, blob)

    db.add(
        models.AuditLog(
            device_id=device_id,
            table_name=table_name,
            row_pk=str(row_pk),
            action=action,
            actor=actor,
            diff=diff or {},
            new_config_version=new_version,
        )
    )
    db.commit()
    return new_version


def bump_all_devices(
    db: Session,
    *,
    actor: str,
    table_name: str,
    row_pk: str,
    action: str,
    diff: dict | None = None,
) -> int:
    """Same as bump_and_audit(), but for a change to a FLEET-WIDE row.

    cfg_fleet_settings feeds every device's blob, so editing it changes every
    device's config_version. Without this, a token rotation would be written to
    the database and then never picked up: the edge polls /version, and each
    device's stored version would still be the old hash.

    Audits per device rather than once, so `which devices did this touch` stays
    answerable from cfg_audit_log alone. Returns the number of devices bumped.
    """
    device_ids = [d.device_id for d in db.query(models.Device.device_id).all()]
    for device_id in device_ids:
        bump_and_audit(
            db, device_id, actor=actor, table_name=table_name,
            row_pk=row_pk, action=action, diff=diff,
        )
    return len(device_ids)
