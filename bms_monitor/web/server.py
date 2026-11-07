"""Local HTTP + Server-Sent Events front end.

SSE because the stream is one-way and ``EventSource`` reconnects by itself.
``static/`` is plain HTML/CSS/JS with no CDN links, since there's no wifi in
the paddock (``tests/test_readonly.py`` checks this). GET only: every other
method gets a 405, and no endpoint can send anything to the BMS.
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: How often the server pushes a stats frame / keep-alive.
HEARTBEAT_S = 1.0

#: How long an SSE client waits for data before we send a keep-alive. The
#: keep-alive write is what notices a dead connection.
_SSE_POLL_S = 0.5


def _guess_type(path: str) -> str:
    ctype, _ = mimetypes.guess_type(path)
    if ctype is None:
        return "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        return ctype + "; charset=utf-8"
    return ctype


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bms-monitor"
    sys_version = ""

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):
        """Silence per-request logging so it doesn't bury the console
        messages that matter."""
        return

    def _send(self, code: int, body: bytes, ctype: str, extra=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # nosniff since the page shows device-controlled text. The UI
            # also only uses textContent.
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj, code: int = 200):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    # -- methods -----------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path == "/api/snapshot":
                return self._send_json(self.server.hub.snapshot())
            if path == "/api/status":
                return self._send_json(self.server.hub.stats())
            if path == "/api/export.csv":
                return self._serve_export()
            if path == "/api/stream":
                return self._serve_stream()
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            self._send_json({"error": "not found", "path": path}, 404)
        except (BrokenPipeError, ConnectionResetError):
            # The browser navigated away mid-response.
            pass
        except Exception as exc:                     # pragma: no cover
            try:
                self._send_json({"error": repr(exc)}, 500)
            except Exception:
                pass

    def _reject(self):
        self._send_json(
            {"error": "this tool is read-only; only GET is supported"},
            405,
        )

    do_POST = do_PUT = do_DELETE = do_PATCH = _reject

    def do_HEAD(self):
        # Treat HEAD like GET so a simple health check works.
        self.do_GET()

    # -- handlers ----------------------------------------------------------

    def _serve_export(self):
        """Serve the retained history as a CSV download.

        Returns 409 when nothing has been recorded, so the UI can say why
        instead of downloading an empty file.
        """
        body = self.server.hub.export_csv().encode("utf-8")
        if not body:
            return self._send_json(
                {"error": "no samples recorded yet"}, 409
            )
        self._send(200, body, "text/csv; charset=utf-8", extra={
            "Content-Disposition": 'attachment; filename="bms-monitor.csv"',
        })

    def _serve_static(self, rel: str):
        # Normalise and confine to STATIC_DIR.  posixpath.normpath collapses
        # "..", and the realpath prefix check catches symlink escapes.
        rel = posixpath.normpath("/" + rel).lstrip("/")
        full = os.path.realpath(os.path.join(STATIC_DIR, rel))
        root = os.path.realpath(STATIC_DIR)
        if not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
            return self._send_json({"error": "not found"}, 404)
        with open(full, "rb") as handle:
            body = handle.read()
        self._send(200, body, _guess_type(full))

    def _serve_stream(self):
        hub = self.server.hub
        client = hub.add_client()
        if client is None:
            return self._send_json(
                {"error": "too many stream clients", "max": 8}, 503
            )
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            # EventSource reconnect delay, in ms.
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()

            while not self.server.stopping.is_set():
                if client.dropped:
                    # Client fell behind and its queue overflowed. Drop it;
                    # EventSource reconnects and the page re-snapshots.
                    break
                try:
                    payload = client.q.get(timeout=_SSE_POLL_S)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.remove_client(client)


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that doesn't print a traceback when a client
    hangs up (e.g. a tab closing mid-stream)."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class MonitorServer:
    """Owns the HTTP server thread and the heartbeat thread."""

    def __init__(self, hub, host: str = "127.0.0.1", port: int = 8765):
        # The CLI always binds 127.0.0.1. There's no authentication, so
        # this shouldn't be reachable from the network.
        self.hub = hub
        self.httpd = _QuietThreadingHTTPServer((host, port), _Handler)
        self.httpd.hub = hub
        self.httpd.stopping = threading.Event()
        self.host, self.port = self.httpd.server_address[0], self.httpd.server_address[1]
        self._threads = []
        self._started = False

    @property
    def url(self) -> str:
        return "http://%s:%d/" % (self.host, self.port)

    def start(self) -> None:
        serve = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                 name="http", daemon=True)
        beat = threading.Thread(target=self._heartbeat, name="heartbeat", daemon=True)
        serve.start()
        beat.start()
        self._threads = [serve, beat]
        self._started = True

    def _heartbeat(self) -> None:
        while not self.httpd.stopping.wait(HEARTBEAT_S):
            try:
                self.hub.heartbeat()
            except Exception:                        # pragma: no cover
                pass

    def stop(self) -> None:
        """Release the socket. Safe to call without start(), and safe to
        call twice.

        ``shutdown()`` is only called if started: it waits for
        ``serve_forever()`` and would block forever otherwise.
        """
        self.httpd.stopping.set()
        if self._started:
            try:
                self.httpd.shutdown()
            except Exception:                        # pragma: no cover
                pass
        self._started = False
        try:
            self.httpd.server_close()
        except Exception:                            # pragma: no cover
            pass
        for thread in self._threads:
            thread.join(timeout=2.0)
