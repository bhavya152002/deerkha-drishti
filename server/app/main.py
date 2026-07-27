"""
main.py -- FastAPI application entry point for the Deerkha Drishti office server.

Run (from the server/ directory):
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Serves:
  * device API  (/api/config, /api/heartbeat)      -- Jetson over Tailscale
  * admin API   (/api/admin/*)                      -- dashboard fetch calls
  * dashboard   (/, /login, /device/<id>/...)       -- Jinja/HTMX pages
"""

import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from . import settings
from .db import engine
from .routers import device, admin_api, detections, dashboard, live

log = logging.getLogger("deerkha.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One shared, keep-alive HTTP client for the whole process. The live proxy
    # used to construct a new AsyncClient per snapshot, which meant a fresh TCP
    # + WireGuard handshake several times a second per camera -- pure latency
    # and pure uplink on a field link.
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, read=30.0),
        limits=httpx.Limits(max_keepalive_connections=20,
                            max_connections=50,
                            keepalive_expiry=60.0),
    )
    try:
        yield
    finally:
        await app.state.http.aclose()


# FastAPI publishes /docs, /redoc and /openapi.json with NO authentication by
# default. That schema enumerates every admin and device route with its
# parameters -- a map of the whole control plane, including the device API. It
# was tolerable on a tailnet-only office PC; it is not once this is on a public
# domain. Set DD_ENV=development to get them back locally.
_DEV = os.environ.get("DD_ENV", "production").lower() in ("dev", "development", "local")

app = FastAPI(
    title="Deerkha Drishti Server",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _DEV else None,
    redoc_url="/redoc" if _DEV else None,
    openapi_url="/openapi.json" if _DEV else None,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET,
    session_cookie="dd_admin",
    max_age=60 * 60 * 12,  # 12h
    same_site="lax",
    # Cookies are only ever sent over the Cloudflare tunnel, which terminates
    # TLS. Set DD_INSECURE_COOKIES=1 only for local http:// development.
    https_only=os.environ.get("DD_INSECURE_COOKIES", "0") != "1",
)

_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# NOTE: uploaded camera reference frames are deliberately NOT mounted here.
# They used to be `app.mount("/reference_frames", StaticFiles(...))`, which
# served operator-uploaded photographs of every customer's land to anyone who
# could guess a filename -- no session, no check. They are now served by
# dashboard.get_reference_frame() behind require_admin, with a path-containment
# check. Do not re-add a mount for them.

app.include_router(device.router)
app.include_router(admin_api.router)
app.include_router(detections.router)
app.include_router(live.router)
# Dashboard is three routers with three different auth answers: login/logout are
# public by necessity, pages redirect to the login form, and its JSON endpoints
# return 401 so fetch() callers can handle it.
app.include_router(dashboard.public_router)
app.include_router(dashboard.router)
app.include_router(dashboard.api_router)


@app.get("/healthz")
def healthz():
    """Liveness AND readiness. This used to return {"ok": true} unconditionally,
    so NSSM and Cloudflare would report the service healthy while the database
    was unreachable and the entire fleet was quietly running on cached config."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        log.error("healthz: database unreachable: %s", e)
        return JSONResponse(status_code=503,
                            content={"ok": False, "db": "unreachable"})
    return {"ok": True, "db": "ok"}


@app.exception_handler(500)
async def _500(request: Request, exc: Exception):
    # Log it. This handler previously swallowed the exception entirely, so a
    # 500 left NO trace anywhere -- the single worst property to have when the
    # thing you are debugging is seven remote sites.
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})
