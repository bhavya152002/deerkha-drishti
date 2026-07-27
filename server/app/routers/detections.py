"""
routers/detections.py -- read-only access to the EXISTING `detections` table
(the one the Jetson already logs to via send_supabase_alert). Not modelled as an
ORM table; queried with Core SQL so we never risk altering it.

The device id is embedded in the `message` column as "... [<device_id>]" by the
edge (send_supabase_alert), so the optional device filter matches on that.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..db import get_db

router = APIRouter(prefix="/api/admin", tags=["detections"])


@router.get("/detections")
def list_detections(
    device_id: str | None = None,
    type: str | None = None,
    source: str | None = None,
    limit: int = 100,
    offset: int = 0,
    admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    limit = min(limit, 500)
    where = []
    params: dict = {"limit": limit, "offset": offset}
    if device_id:
        where.append("message LIKE :dev")
        params["dev"] = f"%[{device_id}]%"
    if type:
        where.append("type = :type")
        params["type"] = type
    if source:
        where.append("source ILIKE :source")
        params["source"] = f"%{source}%"
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    sql = text(
        f"SELECT timestamp, source, message, type, animals, raw_timestamp "
        f"FROM detections{clause} "
        f"ORDER BY raw_timestamp DESC NULLS LAST, timestamp DESC "
        f"LIMIT :limit OFFSET :offset"
    )
    rows = db.execute(sql, params).mappings().all()
    out = []
    for r in rows:
        msg = r.get("message") or ""
        dev = None
        if "[" in msg and "]" in msg:
            dev = msg[msg.rfind("[") + 1 : msg.rfind("]")]
        out.append(
            {
                "timestamp": r.get("timestamp").isoformat() if r.get("timestamp") else None,
                "source": r.get("source"),
                "message": msg,
                "type": r.get("type"),
                "animals": r.get("animals"),
                "device_id": dev,
            }
        )
    return out
