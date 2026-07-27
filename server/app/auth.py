"""
auth.py -- two auth realms.

Device realm: the Jetson sends `Authorization: Bearer <device_token>` on config
and heartbeat calls. The token is matched against cfg_devices.device_token and
must belong to the device_id in the path (a compromised tailnet node still can't
read another device's config).

Admin realm: the dashboard uses a signed session cookie set at /login. This is a
second layer behind Cloudflare Access (which does the real public gating). Kept
deliberately simple for a single operator.
"""

import hmac

from fastapi import Depends, HTTPException, Path, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from . import models, settings
from .db import get_db

_bearer = HTTPBearer(auto_error=True)


def require_device(
    device_id: str = Path(...),
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> models.Device:
    """Authenticate a device call: bearer token must match the path device."""
    dev = db.get(models.Device, device_id)
    if dev is None or not dev.enabled:
        raise HTTPException(status_code=404, detail="unknown or disabled device")
    if not hmac.compare_digest(creds.credentials or "", dev.device_token or ""):
        raise HTTPException(status_code=403, detail="invalid device token")
    return dev


def require_admin(request: Request) -> str:
    """Require an authenticated admin session. Returns the admin username.

    Use for JSON/API routes: fetch() callers want a 401 they can act on, and
    static/app.js already redirects to /login when it sees one.
    """
    user = request.session.get("admin_user")
    if not user:
        raise HTTPException(status_code=401, detail="admin login required")
    return user


def require_admin_page(request: Request) -> str:
    """Same check, browser-shaped: redirect to the login form instead of 401.

    Attached as a ROUTER-level dependency on the dashboard page router rather
    than called inside each handler, so a newly added page cannot accidentally
    ship unauthenticated. A 303 with a Location header is what makes this work
    as a dependency -- a dependency cannot return a RedirectResponse, but
    raising an HTTPException with headers produces an equivalent response and
    the browser follows it.
    """
    user = request.session.get("admin_user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def verify_admin_login(username: str, password: str) -> bool:
    return hmac.compare_digest(username, settings.ADMIN_USERNAME) and hmac.compare_digest(
        password, settings.ADMIN_PASSWORD
    )
