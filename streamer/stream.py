#!/usr/bin/env python3
"""Six per-cam MJPEG passthrough servers, one port each, one ffmpeg per cam.

Each camera has exactly one long-lived ffmpeg (-c copy) feeding a broadcaster.
HTTP clients on the cam's port subscribe to receive frames; the ffmpeg never
restarts per request, so there's no startup race when multiple clients hit
the server at once.
"""
import http.server
import socketserver
import subprocess
import threading
import time
import os

FPS, W, H = 30, 640, 480

# port -> device
PORTS = {
    8080: "/dev/video0",
    8081: "/dev/video2",
    8082: "/dev/video4",
    8083: "/dev/video6",
    8084: "/dev/video12",
    8085: "/dev/video8",
}
PAGE_PORT = 8080  # also serves the HTML grid at /

HTML_PATH = "/tmp/camtest/live.html"


def ffmpeg_cmd(dev):
    return ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", f"{W}x{H}", "-framerate", str(FPS),
            "-i", dev, "-c", "copy", "-f", "mjpeg", "-"]


class CamBroadcaster:
    """One ffmpeg per camera, many HTTP subscribers."""

    def __init__(self, dev):
        self.dev = dev
        self.lock = threading.Lock()
        self.subs = []  # (queue, condvar, alive)
        self.proc = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        backoff = 0.5
        while True:
            try:
                self.proc = subprocess.Popen(ffmpeg_cmd(self.dev),
                                             stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL,
                                             bufsize=0)
                buf = b""
                while True:
                    chunk = self.proc.stdout.read(8192)
                    if not chunk: break
                    buf += chunk
                    while True:
                        s = buf.find(b"\xff\xd8")
                        if s < 0: buf = b""; break
                        e = buf.find(b"\xff\xd9", s + 2)
                        if e < 0:
                            if s > 0: buf = buf[s:]
                            break
                        jpg = buf[s:e + 2]
                        buf = buf[e + 2:]
                        self._push(jpg)
                # ffmpeg exited; wait and retry (cam may have hiccupped)
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    def _push(self, jpg):
        with self.lock:
            subs = list(self.subs)
        for q, cv, alive in subs:
            with cv:
                if len(q) > 2: q.clear()
                q.append(jpg)
                cv.notify_all()

    def subscribe(self):
        sub = ([], threading.Condition(), [True])
        with self.lock:
            self.subs.append(sub)
        return sub

    def unsubscribe(self, sub):
        with self.lock:
            try: self.subs.remove(sub)
            except ValueError: pass
        sub[2][0] = False
        with sub[1]:
            sub[1].notify_all()


# One broadcaster per cam, staggered start so 6 ffmpegs don't all hit
# VIDIOC_STREAMON at the same instant (avoids USB iso negotiation races).
BCASTS = {}
for _port, _dev in PORTS.items():
    BCASTS[_port] = CamBroadcaster(_dev)
    time.sleep(0.5)
# A bit more grace before HTTP starts.
time.sleep(1)


def make_handler(port, serve_page):
    bcast = BCASTS[port]

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            if serve_page and self.path in ("/", "/index.html"):
                with open(HTML_PATH, "rb") as f: data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if self.path == "/stream":
                sub = bcast.subscribe()
                q, cv, alive = sub
                boundary = "frame"
                self.send_response(200)
                self.send_header("Content-Type",
                                 f"multipart/x-mixed-replace; boundary={boundary}")
                self.send_header("Cache-Control", "no-cache, private")
                self.end_headers()
                try:
                    while alive[0]:
                        with cv:
                            if not q: cv.wait(timeout=5)
                            if not q: continue
                            jpg = q.pop(0)
                        hdr = (f"--{boundary}\r\n"
                               f"Content-Type: image/jpeg\r\n"
                               f"Content-Length: {len(jpg)}\r\n\r\n").encode()
                        try:
                            self.wfile.write(hdr)
                            self.wfile.write(jpg)
                            self.wfile.write(b"\r\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            return
                finally:
                    bcast.unsubscribe(sub)
                return

            self.send_error(404)
    return Handler


class TS(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port):
    TS(("0.0.0.0", port), make_handler(port, port == PAGE_PORT)).serve_forever()


if __name__ == "__main__":
    os.chdir("/tmp/camtest")
    for port in PORTS:
        threading.Thread(target=serve, args=(port,), daemon=True).start()
    threading.Event().wait()
