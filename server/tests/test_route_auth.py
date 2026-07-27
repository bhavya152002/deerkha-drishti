"""
test_route_auth.py -- every route must be guarded, or explicitly listed here.

WHY THIS EXISTS
---------------
Dashboard page auth used to be an inline `_page()` call inside each handler.
That is opt-in security: a handler that forgets the call is silently public,
and nothing catches it -- not review, not type checking, not runtime. This
codebase has already shipped that bug once, plus five separate endpoints that
return blanket `{col.name: getattr(...)}` column dumps including plaintext RTSP
passwords. Convention has been tried; this is the mechanical version.

The test walks the live route table and asserts that every route either
    (a) appears in PUBLIC_ROUTES below, with a reason, or
    (b) has one of the known auth dependencies somewhere in its dependency tree.

If you add a route and this fails, that is the test doing its job. Add the
guard. Only add to PUBLIC_ROUTES if the route genuinely must be reachable by an
unauthenticated caller, and say why.

Run:  python -m pytest tests/ -q     (from server/)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SESSION_SECRET", "x" * 48)
os.environ.setdefault("ADMIN_USERNAME", "test")
os.environ.setdefault("ADMIN_PASSWORD", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")
os.environ.setdefault("DD_SERVER_DATA_DIR", os.path.join(os.path.dirname(__file__), "_tmpdata"))

import pytest
from fastapi.routing import APIRoute

from app.main import app

# Names of the dependency callables that constitute "this route is guarded".
AUTH_DEPENDENCIES = {
    "require_admin",       # JSON/API routes -> 401
    "require_admin_page",  # dashboard pages -> 303 to /login
    "require_device",      # edge devices -> bearer token scoped to path device_id
}

# (method, path) -> why it is allowed to be unauthenticated.
PUBLIC_ROUTES = {
    ("GET", "/login"): "the login form itself",
    ("POST", "/login"): "credential submission; rate limited at nginx and per-user",
    ("GET", "/logout"): "clears the session; nothing to protect",
    ("GET", "/healthz"): "liveness/readiness for systemd and the tunnel; leaks only DB reachability",
}


def _auth_names(route: APIRoute) -> set[str]:
    """Every callable name in this route's dependency tree."""
    names = set()
    stack = [route.dependant]
    while stack:
        d = stack.pop()
        if getattr(d, "call", None) is not None:
            names.add(getattr(d.call, "__name__", ""))
        stack.extend(d.dependencies)
    return names


def _api_routes():
    return [r for r in app.routes if isinstance(r, APIRoute)]


def test_every_route_is_guarded_or_explicitly_public():
    unguarded = []
    for route in _api_routes():
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            key = (method, route.path)
            if key in PUBLIC_ROUTES:
                continue
            if _auth_names(route) & AUTH_DEPENDENCIES:
                continue
            unguarded.append(f"{method} {route.path}  (handler: {route.name})")

    assert not unguarded, (
        "These routes have NO auth dependency and are not listed in PUBLIC_ROUTES:\n  "
        + "\n  ".join(unguarded)
        + "\n\nAdd require_admin / require_admin_page / require_device, or add an "
          "entry to PUBLIC_ROUTES with a reason."
    )


def test_no_unauthenticated_static_mount_for_uploads():
    """Reference frames are customer site photography.

    They were once served by `app.mount("/reference_frames", StaticFiles(...))`
    with no session check at all -- guessing a filename was enough. Serving them
    is now an authenticated route. Assert the mount has not come back.
    """
    mounts = [getattr(r, "path", "") for r in app.routes if r.__class__.__name__ == "Mount"]
    assert "/reference_frames" not in mounts, (
        "/reference_frames is mounted as static files again -- that serves every "
        "customer's uploaded site imagery to anyone who can guess a filename. "
        "Use the admin-guarded route in dashboard.py instead."
    )


def test_dashboard_pages_redirect_rather_than_401():
    """A browser hitting a page while logged out should get the login form, not
    a JSON 401. Regression guard for the router split."""
    page_routes = [
        r for r in _api_routes()
        if r.path in ("/", "/detections") or r.path.startswith("/device/")
    ]
    assert page_routes, "no dashboard page routes found -- did the router move?"
    for r in page_routes:
        assert "require_admin_page" in _auth_names(r), (
            f"{r.path} is not using require_admin_page; a logged-out browser would "
            f"get a JSON 401 instead of the login form"
        )


def test_api_docs_disabled_in_production():
    """/docs and /openapi.json enumerate every admin and device route with
    parameters. Fine on a tailnet; not on a public domain."""
    if os.environ.get("DD_ENV", "production").lower() in ("dev", "development", "local"):
        pytest.skip("DD_ENV is a development value; docs are intentionally on")
    assert app.docs_url is None, "/docs is exposed in production"
    assert app.redoc_url is None, "/redoc is exposed in production"
    assert app.openapi_url is None, "/openapi.json is exposed in production"
