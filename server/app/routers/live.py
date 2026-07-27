"""
routers/live.py -- live camera view proxy (admin-only).

The browser reaches the server via Cloudflare; the server reaches the Jetson only
over Tailscale. The cameras are on the Jetson's field LAN and are not reachable
from here or from the browser -- so this router PROXIES the Jetson's token-gated
stream server (edge/stream_server.py):

    browser --Cloudflare--> server --Tailscale--> Jetson :8090

Handlers authenticate to the Jetson with the device's own token
(cfg_devices.device_token). The browser never sees that token; it only needs an
admin session.

Bandwidth notes (this is a field-site uplink, often metered 4G)
--------------------------------------------------------------
Three things keep this cheap, and all three matter:

  1. The grid asks for small tiles. The dashboard renders ~320px tiles, so
     requesting the Jetson's 1280px default shipped ~16x more pixels than were
     ever displayed. See SNAPSHOT_TILE_W / SNAPSHOT_TILE_Q.
  2. Snapshots are cached and coalesced (see _SNAP_CACHE). N browser tabs on
     the same camera cost ONE fetch from the field, not N. Without this,
     bandwidth scaled with viewers rather than with cameras.
  3. One shared keep-alive client (app.state.http). Building a fresh
     AsyncClient per request meant a new TCP + WireGuard handshake for every
     snapshot -- several times a second, per camera.

Device lookups are cached too (_TARGET_CACHE): they resolve a tailscale_ip and
token that change roughly never, and doing that lookup synchronously inside an
async handler blocked the event loop on a remote-DB round-trip for every frame.
"""

import asyncio
import os
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from .. import models
from ..auth import require_admin
from ..db import session_scope

router = APIRouter(prefix="/api/live", tags=["live"])

# Must match the Jetson's STREAM_SERVER_PORT (default 8090).
JETSON_STREAM_PORT = int(os.environ.get("JETSON_STREAM_PORT", "8090"))

# What the dashboard grid asks the Jetson for. Tiles render at ~320px CSS, so
# 480px covers HiDPI without waste. The ROI editor overrides these because it
# genuinely needs resolution.
SNAPSHOT_TILE_W = int(os.environ.get("LIVE_TILE_W", "480"))
SNAPSHOT_TILE_Q = int(os.environ.get("LIVE_TILE_Q", "55"))

# How long a fetched JPEG is reused for. Must be well under the dashboard's
# refresh interval or it does nothing; must be non-zero or concurrent viewers
# each cost a field fetch.
SNAPSHOT_TTL_SEC = float(os.environ.get("LIVE_SNAPSHOT_TTL", "2.0"))

# Cap concurrent MJPEG streams. Each one holds a socket open across Tailscale
# for the life of the browser tab AND costs the Jetson a JPEG encode per frame
# taken out of the detection pipeline's CPU budget.
MAX_STREAMS = int(os.environ.get("LIVE_MAX_STREAMS", "4"))
_stream_slots = asyncio.Semaphore(MAX_STREAMS)

# (device_id) -> (base_url, token, fetched_at)
_TARGET_CACHE: dict[str, tuple[str, str, float]] = {}
_TARGET_TTL_SEC = 60.0

# (device_id, cam_index, view, w, q) -> (jpeg_bytes, fetched_at)
_SNAP_CACHE: dict[tuple, tuple[bytes, float]] = {}
# One lock per cache key so concurrent requests for the SAME tile collapse into
# a single upstream fetch instead of stampeding the field link.
_SNAP_LOCKS: dict[tuple, asyncio.Lock] = {}


def _normalize_view(view: str) -> str:
    return view if view in ("annotated", "clean") else "annotated"


def _lookup_device(device_id: str) -> tuple[str, str]:
    """Blocking DB read. Callers must not run this on the event loop."""
    with session_scope() as db:
        dev = db.get(models.Device, device_id)
        if dev is None:
            raise HTTPException(404, "unknown device")
        if not dev.tailscale_ip:
            raise HTTPException(502, "device has no tailscale_ip set (set it on the device page)")
        return f"http://{dev.tailscale_ip}:{JETSON_STREAM_PORT}", (dev.device_token or "")


async def _device_target(device_id: str) -> tuple[str, str]:
    """(base_url, device_token), cached. A device's tailnet IP and token change
    at most a few times a year, but this used to be a synchronous DB round-trip
    on the event loop for EVERY snapshot -- at one grid refresh per camera per
    second that alone saturated the loop."""
    hit = _TARGET_CACHE.get(device_id)
    if hit and (time.monotonic() - hit[2]) < _TARGET_TTL_SEC:
        return hit[0], hit[1]
    from starlette.concurrency import run_in_threadpool
    base, token = await run_in_threadpool(_lookup_device, device_id)
    _TARGET_CACHE[device_id] = (base, token, time.monotonic())
    return base, token


def invalidate_device(device_id: str) -> None:
    """Call after rotating a token or changing tailscale_ip so the proxy does
    not keep using the stale value for up to _TARGET_TTL_SEC."""
    _TARGET_CACHE.pop(device_id, None)


def _jetson_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _client(request: Request) -> httpx.AsyncClient:
    """The shared keep-alive client created in main.py's lifespan."""
    client = getattr(request.app.state, "http", None)
    if client is None:  # pragma: no cover - lifespan always sets this
        raise HTTPException(500, "http client not initialised")
    return client


@router.get("/{device_id}/{cam_index}/snapshot.jpg")
async def snapshot(
    device_id: str,
    cam_index: int,
    request: Request,
    view: str = Query("annotated"),
    w: int | None = Query(None, ge=160, le=1920),
    q: int | None = Query(None, ge=20, le=95),
    admin: str = Depends(require_admin),
):
    view = _normalize_view(view)
    want_w = w or SNAPSHOT_TILE_W
    want_q = q or SNAPSHOT_TILE_Q
    key = (device_id, cam_index, view, want_w, want_q)

    now = time.monotonic()
    hit = _SNAP_CACHE.get(key)
    if hit and (now - hit[1]) < SNAPSHOT_TTL_SEC:
        return Response(content=hit[0], media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    lock = _SNAP_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        # Re-check: another request may have filled the cache while we waited,
        # which is the whole point of coalescing.
        hit = _SNAP_CACHE.get(key)
        if hit and (time.monotonic() - hit[1]) < SNAPSHOT_TTL_SEC:
            return Response(content=hit[0], media_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

        base, token = await _device_target(device_id)
        url = f"{base}/snapshot/{cam_index}?view={view}&w={want_w}&q={want_q}"
        try:
            r = await _client(request).get(url, headers=_jetson_headers(token))
        except httpx.HTTPError:
            # Jetson unreachable over Tailscale -> let the UI show "no signal"
            raise HTTPException(503, "camera unreachable")
        if r.status_code != 200:
            # propagate the Jetson's status (503 stale, 404 no cam, 401 token)
            raise HTTPException(r.status_code, "camera error")

        _SNAP_CACHE[key] = (r.content, time.monotonic())
        _prune_snap_cache()

    return Response(content=r.content, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


def _prune_snap_cache() -> None:
    """Drop expired entries. Bounded by (devices x cameras x variants), but a
    long-running server with churn would otherwise accumulate dead keys."""
    if len(_SNAP_CACHE) < 256:
        return
    now = time.monotonic()
    for k in [k for k, v in _SNAP_CACHE.items() if (now - v[1]) > SNAPSHOT_TTL_SEC * 10]:
        _SNAP_CACHE.pop(k, None)
        _SNAP_LOCKS.pop(k, None)


@router.get("/{device_id}/{cam_index}/stream")
async def stream(
    device_id: str,
    cam_index: int,
    request: Request,
    view: str = Query("annotated"),
    w: int | None = Query(None, ge=160, le=1920),
    q: int | None = Query(None, ge=20, le=95),
    admin: str = Depends(require_admin),
):
    # Non-blocking acquire: refuse fast rather than queueing a viewer behind
    # streams that may last for hours.
    try:
        await asyncio.wait_for(_stream_slots.acquire(), timeout=0.05)
    except asyncio.TimeoutError:
        raise HTTPException(429, f"too many concurrent streams (max {MAX_STREAMS})")
    released = False

    def _release():
        nonlocal released
        if not released:
            released = True
            _stream_slots.release()

    try:
        base, token = await _device_target(device_id)
        params = f"view={_normalize_view(view)}"
        if w:
            params += f"&w={w}"
        if q:
            params += f"&q={q}"
        url = f"{base}/stream/{cam_index}?{params}"

        # A read timeout is essential here. With read=None a Jetson that accepts
        # the connection and then wedges holds this socket, its client and the
        # WireGuard flow open FOREVER -- one leak per viewer, invisible until
        # the server runs out of sockets weeks later. At the Jetson's stream
        # rate a frame arrives every few hundred ms, so 30s of silence is dead.
        client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=30.0))
        try:
            req = client.build_request("GET", url, headers=_jetson_headers(token))
            resp = await client.send(req, stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(503, "camera unreachable")

        if resp.status_code != 200:
            code = resp.status_code
            await resp.aclose()
            await client.aclose()
            raise HTTPException(code, "camera error")

        media_type = resp.headers.get("content-type", "multipart/x-mixed-replace; boundary=frame")

        async def _gen():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()
                _release()

        # Ownership of the slot passes to _gen()'s finally block; do not
        # release it here or two viewers could share one slot.
        return StreamingResponse(_gen(), media_type=media_type,
                                 headers={"Cache-Control": "no-store"})
    except BaseException:
        _release()
        raise
