"""
routers/device.py -- edge-facing API (Jetson polls these over Tailscale).

All routes require a valid device bearer token scoped to the path device_id.
The edge polls /version cheaply and only fetches the full blob when the version
changes (or sends If-None-Match to get a 304).
"""

from fastapi import APIRouter, Depends, Header, Response, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import models, cache
from ..assembly import assemble_config
from ..auth import require_device
from ..db import get_db

router = APIRouter(prefix="/api", tags=["device"])


def _version_for(db: Session, dev: models.Device) -> str | None:
    """Current config_version for a device, cheaply.

    cfg_devices.config_version is maintained by service.bump_and_audit() on
    every admin write, so the stored value is authoritative. This used to
    re-assemble the whole blob (8 queries + SHA-256) on every /version poll to
    guard against drift -- but the only thing that can actually cause drift is
    someone editing cfg_* rows directly in the SQL editor, which is covered by
    the nightly reconcile in app/jobs/retention.py and the Resync button.

    Self-heals lazily: a device that has never been through a write path (fresh
    seed) has a NULL config_version, so assemble once and store it.
    """
    if dev.config_version:
        return dev.config_version
    blob = assemble_config(db, dev.device_id)
    if blob is None:
        return None
    dev.config_version = blob["config_version"]
    db.commit()
    cache.put(dev.device_id, dev.config_version, blob)
    return dev.config_version


@router.get("/config/{device_id}/version")
def get_config_version(
    device_id: str,
    dev: models.Device = Depends(require_device),
    db: Session = Depends(get_db),
):
    """Cheap poll: current config_version + restart flag.

    This is the single hottest endpoint in the system -- every device hits it
    every poll interval, forever. It must stay O(1) queries.
    """
    return {
        "device_id": device_id,
        "config_version": _version_for(db, dev),
        "restart_required": bool(dev.restart_required),
    }


@router.get("/config/{device_id}")
def get_config(
    device_id: str,
    response: Response,
    dev: models.Device = Depends(require_device),
    db: Session = Depends(get_db),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
):
    """Full config blob. Honors If-None-Match: <config_version> -> 304."""
    version = _version_for(db, dev)

    # Compare the ETag BEFORE assembling. Previously the blob was built first
    # and only then compared, so a 304 -- whose entire purpose is to cost
    # nothing -- cost exactly as much as a 200.
    if version and if_none_match and if_none_match.strip('"') == version:
        return Response(status_code=304)

    blob = cache.get(device_id, version)
    if blob is None:
        blob = assemble_config(db, device_id)
        if blob is None:
            return JSONResponse(status_code=404, content={"detail": "device not found"})
        version = blob["config_version"]
        cache.put(device_id, version, blob)
        # Keep the stored version honest if it had drifted.
        if dev.config_version != version:
            dev.config_version = version
            db.commit()

    response.headers["ETag"] = f'"{version}"'
    return blob


@router.post("/heartbeat/{device_id}")
async def post_heartbeat(
    device_id: str,
    request: Request,
    dev: models.Device = Depends(require_device),
    db: Session = Depends(get_db),
):
    """Ingest a device status report. Body is the status JSON the edge builds in
    status_report_loop(). Stored as a new cfg_device_status row (history)."""
    body = await request.json()

    def _write():
        # psycopg2 is fully synchronous, so doing this inline in an async
        # handler blocks the single event loop for a full round-trip to a
        # remote cloud DB -- stalling every other request, including the live
        # view and every other device's config poll.
        row = models.DeviceStatus(
            device_id=device_id,
            cameras_alive=body.get("cameras_alive"),
            cameras_total=body.get("cameras_total"),
            per_camera=body.get("cameras") or body.get("per_camera"),
            config_version=body.get("config_version"),
            pending_restart=body.get("pending_restart"),
            app_uptime_sec=body.get("uptime_sec") or body.get("app_uptime_sec"),
            disk_free_percent=body.get("disk_free_percent"),
            notes=body.get("notes"),
        )
        db.add(row)
        db.commit()

    await run_in_threadpool(_write)
    return {"ok": True}


@router.post("/config/{device_id}/ack")
async def post_ack(
    device_id: str,
    request: Request,
    dev: models.Device = Depends(require_device),
    db: Session = Depends(get_db),
):
    """Edge confirms it applied a version.

    restart_required is cleared only when the edge reports it is running a FRESH
    PROCESS on that version. Previously the ack alone cleared it -- but the edge
    acks immediately after applying the blob, roughly 3s BEFORE it actually calls
    os._exit(). So the flag cleared on "I received it", and a restart that never
    happened (or a device with no supervisor to relaunch it) looked successful on
    the dashboard.
    """
    body = await request.json()
    applied = body.get("config_version")
    uptime = body.get("uptime_sec")

    def _ack() -> bool:
        if not (dev.restart_required and applied):
            return False
        # Compare against the STORED version rather than re-assembling. This
        # ran a full 8-query assembly on the event loop; after any config push
        # all devices ack within one poll interval, so the server used to
        # freeze for the sum of those assemblies at exactly the moment an
        # operator was watching to see their change land.
        if applied != _version_for(db, dev):
            return False
        # uptime_sec distinguishes "I received this config" from "I rebooted
        # into it". None means an older edge build that doesn't report it.
        if uptime is None or uptime < 120:
            dev.restart_required = False
            db.commit()
            return True
        return False

    cleared = await run_in_threadpool(_ack)
    return {"ok": True, "restart_cleared": cleared}
