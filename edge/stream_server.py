"""
stream_server.py -- tiny live-frame HTTP server for the Jetson edge.

Exposes each camera's already-decoded frame (the pipeline's CameraStream keeps a
clean native-res BGR frame plus an annotated tile) as JPEG over HTTP, so the
office server can proxy it to the remote dashboard. We REUSE those frames -- no
second RTSP/decode -- the Nano can't afford a second decode path.

Reachability: bind 0.0.0.0 but every request is bearer-gated against
CONFIG_API_TOKEN (the same per-device token the office server stores as
cfg_devices.device_token). The Jetson firewall/ufw should further limit the port
to the tailscale0 interface so it is only reachable over Tailscale.

Routes (both require `Authorization: Bearer <CONFIG_API_TOKEN>`):
    GET /snapshot/{idx}?view=annotated|clean  -> one JPEG
    GET /stream/{idx}?view=annotated|clean    -> multipart/x-mixed-replace MJPEG

`idx` is the 0-based camera index (matches cfg_cameras.cam_index); integer keys
avoid the "CAM 1" space/URL-encoding and "CAM 3"/"CAM3" normalization pitfalls.

Public surface:
    start_stream_server(streams, port=8090) -> launches a daemon thread, returns it
"""

import os
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import cv2

log = logging.getLogger("DHEERKHA")

# --- tunables ---
# All env-overridable so a site on a metered 4G link can be tuned down without
# a code change. A metered profile is roughly:
#   DD_STREAM_FPS=2 DD_STREAM_MAX_W=640 DD_SNAPSHOT_MAX_W=640 DD_JPEG_QUALITY=55
# which cuts a single viewer from ~600 KB/s to ~50 KB/s.
def _env_f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)

def _env_i(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)

STALE_AFTER_SEC = _env_f("DD_STREAM_STALE_SEC", 10.0)   # no fresh frame -> 503 "no signal"
JPEG_QUALITY = _env_i("DD_JPEG_QUALITY", 70)            # live view; alerts use 92 elsewhere
STREAM_FPS = min(max(_env_f("DD_STREAM_FPS", 8.0), 0.5), 30.0)   # MJPEG target frame rate
SNAPSHOT_MAX_W = _env_i("DD_SNAPSHOT_MAX_W", 1280)      # downscale wider frames for bandwidth
STREAM_MAX_W = _env_i("DD_STREAM_MAX_W", 960)           # MJPEG is continuous -> downscale harder

# Hard ceiling on concurrent in-flight requests. ThreadingHTTPServer spawns one
# OS thread per connection with no bound: without this, N dashboard tabs x M
# cameras each cost a thread and a JPEG encode taken straight out of the
# detection pipeline's CPU budget.
MAX_CONCURRENT = _env_i("DD_STREAM_MAX_CONCURRENT", 4)
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)

# Wall-clock of the last served request. The pipeline reads this via
# viewer_active() to skip building cosmetic overlays when nobody is watching.
_LAST_REQUEST_TS = 0.0
VIEWER_IDLE_SEC = _env_f("DD_VIEWER_IDLE_SEC", 30.0)


def viewer_active(window=None) -> bool:
    """True if a live-view request was served recently. Deliberately racy --
    a stale read costs one frame of missing overlay, never correctness."""
    w = VIEWER_IDLE_SEC if window is None else window
    ts = _LAST_REQUEST_TS
    return ts > 0.0 and (time.time() - ts) <= w


# cfg_cameras.cam_index -> CameraStream. Rebound atomically by update_stream_map();
# never mutated in place, so request handler threads always see a consistent map.
_STREAMS_BY_IDX = {}


def _token_ok(handler) -> bool:
    """Bearer check against the live CONFIG_API_TOKEN (read per-request, like the
    heartbeat, so a rotated token is honored without a restart)."""
    want = os.environ.get("CONFIG_API_TOKEN", "")
    if not want:
        return False  # no token configured -> deny (fail closed)
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return auth[7:].strip() == want


def _pick_frame(stream, view):
    """Return a BGR frame to encode, or None if unavailable.

    clean     -> raw latest frame under its lock (native res, no overlays)
    annotated -> full detection-box frame during a power-mode event, else the
                 always-live motion/HITS tile (never freezes between events)
    """
    if view == "clean":
        with stream._latest_frame_lock:
            # Reference, not a copy: the capture loop publishes by rebinding
            # and never mutates. _encode() allocates for both the resize and
            # the JPEG buffer, so it never writes here. Copying was a full
            # native-res clone taken under the same lock the capture loop
            # needs on every single frame.
            return stream._latest_frame
    # annotated (default)
    if getattr(stream, "is_power_mode", False) and stream._last_annotated is not None:
        return stream._last_annotated
    return stream.frame


def _encode(frame, max_w, quality=None):
    if frame is None:
        return None
    try:
        if max_w and frame.shape[1] > max_w:
            h = int(frame.shape[0] * max_w / frame.shape[1])
            frame = cv2.resize(frame, (max_w, h))
        q = JPEG_QUALITY if quality is None else quality
        ok, enc = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(q)])
        return enc.tobytes() if ok else None
    except Exception:
        return None


def _is_stale(stream) -> bool:
    ts = getattr(stream, "_last_frame_ts", 0.0) or 0.0
    return ts == 0.0 or (time.time() - ts) > STALE_AFTER_SEC


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # silence default stderr access log

    def _deny(self, code, msg=""):
        body = msg.encode() if msg else b""
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except Exception:
                pass

    def _route(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p != ""]
        q = parse_qs(u.query)
        view = (q.get("view", ["annotated"])[0]).lower()
        if view not in ("annotated", "clean"):
            view = "annotated"
        if len(parts) != 2:
            return None
        kind, idx = parts[0], parts[1]
        try:
            idx = int(idx)
        except ValueError:
            return None

        # Caller-supplied width/quality. The dashboard grid renders ~320px
        # tiles, so serving the full 1280px default at Q70 ships ~16x more
        # pixels than are displayed -- which on a metered 4G site is the single
        # largest consumer of uplink. Clamped so a bad query string can't ask
        # for something expensive.
        def _clamp_int(name, lo, hi):
            raw = q.get(name, [None])[0]
            if raw is None:
                return None
            try:
                return max(lo, min(int(raw), hi))
            except (TypeError, ValueError):
                return None

        return kind, idx, view, _clamp_int("w", 160, 1920), _clamp_int("q", 20, 95)

    def do_GET(self):
        global _LAST_REQUEST_TS
        if not _token_ok(self):
            return self._deny(401, "unauthorized")
        # Bound concurrency BEFORE doing any work. An MJPEG viewer holds its
        # slot for the life of the connection, which is the point: this caps
        # how much of the box's CPU the dashboard can take from detection.
        if not _slots.acquire(blocking=False):
            log.warning("[STREAM] refusing request -- %d concurrent viewers already "
                        "(raise DD_STREAM_MAX_CONCURRENT if this is expected)",
                        MAX_CONCURRENT)
            return self._deny(503, "too many concurrent viewers")
        try:
            _LAST_REQUEST_TS = time.time()
            r = self._route()
            if r is None:
                return self._deny(404, "not found")
            kind, idx, view, want_w, want_q = r
            stream = _STREAMS_BY_IDX.get(idx)
            if stream is None:
                return self._deny(404, "no such camera")
            if _is_stale(stream):
                return self._deny(503, "no signal")
            if kind == "snapshot":
                return self._snapshot(stream, view, want_w, want_q)
            if kind == "stream":
                return self._stream(stream, view, want_w, want_q)
            return self._deny(404, "not found")
        finally:
            _slots.release()

    def _snapshot(self, stream, view, want_w=None, want_q=None):
        data = _encode(_pick_frame(stream, view),
                       want_w or SNAPSHOT_MAX_W, want_q)
        if data is None:
            return self._deny(503, "no frame")
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _stream(self, stream, view, want_w=None, want_q=None):
        boundary = "frame"
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        global _LAST_REQUEST_TS
        period = 1.0 / STREAM_FPS
        try:
            while True:
                # An MJPEG viewer is one long request, so refresh the liveness
                # marker here too -- otherwise viewer_active() goes false 30s
                # into a stream that is very much still being watched.
                _LAST_REQUEST_TS = time.time()
                if getattr(stream, "stopped", False):
                    # Camera was removed/disabled by a reconcile pass. End the
                    # response cleanly instead of spinning forever on stale
                    # frames -- this handler holds its own stream reference for
                    # the life of the connection.
                    log.debug("[STREAM] cam %s removed -- ending stream", stream.name)
                    break
                if _is_stale(stream):
                    time.sleep(period)
                    continue
                data = _encode(_pick_frame(stream, view),
                               want_w or STREAM_MAX_W, want_q)
                if data:
                    self.wfile.write(b"--" + boundary.encode() + b"\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n")
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                time.sleep(period)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # client closed the tab -> normal
        except Exception as e:
            log.debug(f"[STREAM] cam {stream.name} ended: {e}")


def update_stream_map(streams, cam_cfgs=None):
    """(Re)publish the idx -> CameraStream map WITHOUT rebinding :8090.

    Keyed by the camera's DB cam_index, because that is what the dashboard
    actually addresses: live.py builds `/snapshot/{cam_index}` and live.html
    builds its tiles from `c.cam_index`. The old map was keyed by LIST POSITION,
    which silently diverges the moment a camera is deleted -- cfg_cameras rows
    {0,2,3} became positions {0,1,2}, so the tile labelled CAM 4 showed CAM 3's
    feed and index 3 returned 404.

    Sparse indices are fine and are NOT renumbered: compacting them would
    reassign every other camera's live-view URL.

    Falls back to position keying when the config carries no cam_index, so this
    still works against a server that hasn't been updated yet.
    """
    global _STREAMS_BY_IDX
    by_name = {s.name: s for s in streams}
    new_map = {}
    for pos, c in enumerate(cam_cfgs or []):
        s = by_name.get(c.get("cam_name"))
        if s is None:
            continue
        idx = c.get("cam_index")
        idx = pos if idx is None else int(idx)
        if idx in new_map:
            log.warning("[STREAM] duplicate cam_index %s (%s vs %s); keeping the first",
                        idx, new_map[idx].name, s.name)
            continue
        new_map[idx] = s
    if not cam_cfgs:
        new_map = dict(enumerate(streams))
    _STREAMS_BY_IDX = new_map                       # atomic rebind
    log.info("[STREAM] index map -> %s (%d cams)", sorted(new_map), len(new_map))


def start_stream_server(streams, port=8090, cam_cfgs=None):
    """Publish the index->stream map and launch a ThreadingHTTPServer daemon
    thread. Call ONCE; use update_stream_map() for later roster changes (calling
    this again would try to re-bind the port)."""
    update_stream_map(streams, cam_cfgs)

    def _serve():
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
        except Exception:
            log.exception("[STREAM] could not bind :%s -- LIVE VIEW WILL NOT WORK "
                          "(port in use, or a previous instance still running?)", port)
            return
        httpd.daemon_threads = True
        log.info(f"[STREAM] live frame server on :{port} ({len(_STREAMS_BY_IDX)} cams, token-gated)")
        httpd.serve_forever()

    t = threading.Thread(target=_serve, daemon=True, name="stream-server")
    t.start()
    return t
