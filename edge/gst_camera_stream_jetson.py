"""
gst_camera_stream_jetson.py

Hardened version of your GstCameraStream for 24/7 unattended Jetson
deployment. Same drop-in contract as before (.start(), .read() -> (ret,
frame), .stop(), .frame_w/.frame_h), so your CameraStream class and the
rest of the pipeline don't need to change -- just import this instead of
8cam_ts_gst_camera_stream.

What changed vs. your original, and why:

1. protocols=tcp forced on rtspsrc.
   Your CAM1-3 run over Tailscale (WAN-ish path). UDP RTP over a tunnel
   drops packets silently -> corrupt/garbled frames with no error signal.
   TCP is slightly higher latency but self-heals within the transport
   instead of leaving you to detect corruption downstream.

2. GStreamer bus error/EOS watchdog.
   Your original detects failure only indirectly, by "no frame for 8s" in
   the appsink pull. That's fine as a backstop, but a decoder or rtspsrc
   internal error can sit on the bus for a while before that timeout trips.
   This polls the bus non-blockingly (bus.pop_filtered) from inside the
   existing pull loop -- no separate GLib.MainLoop/thread. An earlier
   version of this file used a per-camera GLib.MainLoop() in its own
   thread for this, which triggered
   "g_main_context_push_thread_default: assertion 'acquired_context' failed"
   because the bus watch was attached from one thread's default context
   while the loop ran in another. Polling avoids that entirely and needs
   no extra thread per camera.

3. Jetson decoder tuning with safe fallback.
   nvv4l2decoder supports enable-max-performance / disable-dpb properties
   on JetPack's multimedia API, but exact property names/availability vary
   by L4T/JetPack version. This tries the tuned pipeline first and falls
   back to the plain one if Gst.parse_launch rejects the property, so you
   don't have to hand-edit strings per device image.

4. Leaky queue before appsink.
   Prevents frame backlog from building up (and RAM growing) if the
   consumer momentarily falls behind -- always keeps the newest frame.
"""

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import numpy as np
import threading
import time
import logging

Gst.init(None)
log = logging.getLogger("DHEERKHA")


class GstCameraStream:
    def __init__(self, url, name, codec="h265"):
        self.url = url
        self.name = name
        self.codec = codec.lower()
        self.stopped = False
        self.pipeline_error = False

        self._latest_frame = None
        self._latest_frame_lock = threading.Lock()
        self.frame_w = None
        self.frame_h = None

        depay = "rtph265depay ! h265parse" if self.codec == "h265" else "rtph264depay ! h264parse"

        tuned = (
            f"rtspsrc location={url} latency=200 protocols=tcp buffer-mode=1 "
            f"drop-on-latency=true ! "
            f"{depay} ! nvv4l2decoder enable-max-performance=1 disable-dpb=true ! "
            "nvvidconv ! video/x-raw,format=BGRx ! "
            "videoconvert ! video/x-raw,format=BGR ! "
            "queue max-size-buffers=2 leaky=downstream ! "
            "appsink name=sink emit-signals=false sync=false max-buffers=1 drop=true"
        )
        basic = (
            f"rtspsrc location={url} latency=200 protocols=tcp ! "
            f"{depay} ! nvv4l2decoder ! "
            "nvvidconv ! video/x-raw,format=BGRx ! "
            "videoconvert ! video/x-raw,format=BGR ! "
            "queue max-size-buffers=2 leaky=downstream ! "
            "appsink name=sink emit-signals=false sync=false max-buffers=1 drop=true"
        )

        try:
            self.pipeline = Gst.parse_launch(tuned)
            self.pipeline_str = tuned
        except GLib.Error as e:
            log.warning(f"[{self.name}] tuned decoder props unsupported on this "
                        f"JetPack image, falling back to basic pipeline ({e})")
            self.pipeline = Gst.parse_launch(basic)
            self.pipeline_str = basic

        self.sink = self.pipeline.get_by_name("sink")
        self.bus = self.pipeline.get_bus()

    def _poll_bus(self):
        """Non-blocking check for ERROR/EOS messages. Called from the pull
        loop's own thread every iteration -- cheap (pop_filtered with no
        timeout returns instantly if nothing's queued) and avoids the
        separate-GLib-context problems a dedicated mainloop thread caused."""
        msg = self.bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if msg is None:
            return
        if msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.error(f"[{self.name}] GStreamer ERROR: {err} ({debug})")
            self.pipeline_error = True
        elif msg.type == Gst.MessageType.EOS:
            log.warning(f"[{self.name}] GStreamer EOS (unexpected for a live RTSP source)")
            self.pipeline_error = True

    def start(self):
        self.pipeline.set_state(Gst.State.PLAYING)
        # Keep the handle so stop() can join it -- needed once cameras can be
        # added/removed at runtime, so repeated cycles don't leak threads.
        self._pull_thread = threading.Thread(
            target=self._pull_loop, daemon=True, name=f"gst-{self.name}")
        self._pull_thread.start()
        return self

    def _pull_loop(self):
        while not self.stopped:
            self._poll_bus()
            sample = self.sink.emit("try-pull-sample", Gst.SECOND)
            if sample is None:
                continue
            buf = sample.get_buffer()
            caps = sample.get_caps()
            w = caps.get_structure(0).get_value("width")
            h = caps.get_structure(0).get_value("height")

            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                frame = np.frombuffer(mapinfo.data, np.uint8).reshape((h, w, 3)).copy()
            finally:
                buf.unmap(mapinfo)

            with self._latest_frame_lock:
                self._latest_frame = frame
                self.frame_w, self.frame_h = w, h

    def read(self):
        """Mimics cv2.VideoCapture.read() -> (ret, frame). Returns (False, None)
        both when no frame has arrived yet AND when a bus error/EOS fired --
        the caller's existing reconnect logic handles both identically."""
        if self.pipeline_error:
            return False, None
        with self._latest_frame_lock:
            if self._latest_frame is None:
                return False, None
            # Return the reference, not a copy. _pull_loop publishes by
            # REBINDING _latest_frame to a freshly allocated array every frame
            # (see the .copy() at the np.frombuffer site, which is required
            # because the GstBuffer is unmapped immediately after) and never
            # writes into a published array. Callers must honour that contract:
            # treat frames as read-only and allocate for any transform.
            # This copy was ~6 MB per frame per camera at 1080p.
            return True, self._latest_frame

    def stop(self):
        self.stopped = True
        try:
            self.pipeline.set_state(Gst.State.NULL)
        except Exception as e:
            log.warning(f"[{self.name}] pipeline -> NULL failed: {e}")
        # Join the pull thread so a hot camera remove/re-add can't leak decoder
        # contexts. It exits within one try-pull-sample timeout (1s).
        t = getattr(self, "_pull_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
            if t.is_alive():
                log.warning(f"[{self.name}] pull thread did not exit within 2s")