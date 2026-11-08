"""HTTP surface and SSE, exercised against a real server on a real socket."""

import json
import os
import tempfile
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request

from bms_monitor.hub import Hub
from bms_monitor.logging_csv import CsvReader
from bms_monitor.model import CELL_COUNT
from bms_monitor.web.server import MonitorServer

from .packs import pack


def sample(seq, pec_ok=True, stale=False):
    return pack(seq=seq, ics=1, cells_per_ic=CELL_COUNT,
                pec_ok=pec_ok, stale=stale, ccount=seq % 64)


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.hub = Hub(history=500)
        self.server = MonitorServer(self.hub, host="127.0.0.1", port=0)
        self.server.start()
        self.base = self.server.url.rstrip("/")

    def tearDown(self):
        self.server.stop()

    def get(self, path, timeout=5):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)


class TestBinding(WebTestCase):
    def test_binds_loopback_only(self):
        self.assertEqual(self.server.host, "127.0.0.1")

    def test_not_reachable_on_a_non_loopback_address(self):
        # Best-effort: prove we did not bind 0.0.0.0.
        with socket.socket() as probe:
            probe.settimeout(0.5)
            self.assertNotEqual(self.server.httpd.server_address[0], "0.0.0.0")


class TestStaticAssets(WebTestCase):
    def test_index(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"BMS Monitor", body)
        self.assertIn("text/html", headers["Content-Type"])

    def test_css_and_js(self):
        for path, marker in (("/static/style.css", b"--series-1"),
                             ("/static/app.js", b"EventSource"),
                             ("/static/charts.js", b"segmentPaths")):
            status, body, _ = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(marker, body, path)

    def test_nosniff_header(self):
        _, _, headers = self.get("/")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_path_traversal_is_refused(self):
        for path in ("/static/../../../../etc/passwd",
                     "/static/..%2f..%2fetc%2fpasswd",
                     "/static/./../server.py",
                     "/static/subdir/../../server.py"):
            try:
                status, body, _ = self.get(path)
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"root:", body)
            self.assertNotIn(b"ThreadingHTTPServer", body)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_favicon_does_not_error(self):
        status, _, _ = self.get("/favicon.ico")
        self.assertEqual(status, 204)


class TestReadOnlyHttp(WebTestCase):
    def _request(self, method, path="/api/snapshot", data=b"{}"):
        req = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_post_put_delete_patch_are_all_405(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            status, body = self._request(method)
            self.assertEqual(status, 405, method)
            self.assertIn(b"read-only", body)

    def test_no_endpoint_accepts_a_command(self):
        for path in ("/api/command?cmd=4", "/api/send?n=1", "/api/poll?plan=1",
                     "/command", "/api/write"):
            try:
                status, _, _ = self.get(path)
            except urllib.error.HTTPError as exc:
                status = exc.code
            self.assertEqual(status, 404, path)

    def test_query_parameters_on_known_endpoints_change_nothing(self):
        status, body, _ = self.get("/api/status?cmd=1&poll=16")
        self.assertEqual(status, 200)
        json.loads(body)


class TestApi(WebTestCase):
    def test_snapshot_shape(self):
        for i in range(10):
            self.hub._on_sample(sample(i))
            self.hub._on_source_event("line", "IC1:line %d" % i)
        status, body, _ = self.get("/api/snapshot")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["cell_count"], CELL_COUNT)
        self.assertEqual(len(data["samples"]), 10)
        self.assertEqual(len(data["samples"][0]["ics"]), 1)
        self.assertEqual(len(data["samples"][0]["ics"][0]["cells"]), CELL_COUNT)
        self.assertIn("config", data)
        self.assertEqual(len(data["raw_lines"]), 10)
        self.assertIn("stats", data)

    def test_snapshot_on_an_empty_hub(self):
        status, body, _ = self.get("/api/snapshot")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["samples"], [])
        self.assertEqual(data["stats"]["data_state"], "no-data")

    def test_status_endpoint(self):
        status, body, _ = self.get("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ("uptime_s", "data_state", "rate_hz", "pec_recent", "link"):
            self.assertIn(key, data)

    def test_pec_bad_samples_are_present_and_flagged_not_omitted(self):
        self.hub._on_sample(sample(0, pec_ok=True))
        self.hub._on_sample(sample(1, pec_ok=False))
        data = json.loads(self.get("/api/snapshot")[1])
        self.assertEqual(len(data["samples"]), 2)
        self.assertFalse(data["samples"][1]["pec_ok"])


class TestSse(WebTestCase):
    def read_stream(self, seconds=2.0, want=1):
        chunks = []
        request = urllib.request.Request(self.base + "/api/stream")
        with urllib.request.urlopen(request, timeout=seconds + 3) as response:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                line = response.readline()
                if not line:
                    break
                chunks.append(line)
                text = b"".join(chunks)
                if text.count(b"event: ") >= want:
                    break
        return b"".join(chunks)

    def test_stream_delivers_samples(self):
        def produce():
            time.sleep(0.2)
            for i in range(5):
                self.hub._on_sample(sample(i))
                time.sleep(0.05)

        threading.Thread(target=produce, daemon=True).start()
        text = self.read_stream(seconds=3.0, want=3)
        self.assertIn(b"event: sample", text)
        self.assertIn(b"retry: 2000", text)

    def test_stream_sends_heartbeats_when_idle(self):
        text = self.read_stream(seconds=2.5, want=1)
        self.assertTrue(b"keep-alive" in text or b"event: stats" in text, text[:200])

    def test_client_cap_returns_503(self):
        from bms_monitor.hub import MAX_SSE_CLIENTS
        held = [self.hub.add_client() for _ in range(MAX_SSE_CLIENTS)]
        self.assertTrue(all(held))
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get("/api/stream", timeout=5)
            self.assertEqual(ctx.exception.code, 503)
        finally:
            for client in held:
                self.hub.remove_client(client)

    def test_client_disconnect_is_cleaned_up(self):
        request = urllib.request.Request(self.base + "/api/stream")
        response = urllib.request.urlopen(request, timeout=5)
        response.readline()
        deadline = time.monotonic() + 3
        while self.hub.client_count == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.hub.client_count, 1)
        response.close()
        # Force the server-side write to fail so the handler notices.
        deadline = time.monotonic() + 6
        while self.hub.client_count > 0 and time.monotonic() < deadline:
            self.hub._on_sample(sample(0))
            time.sleep(0.05)
        self.assertEqual(self.hub.client_count, 0)

    def test_abrupt_socket_close_does_not_kill_the_server(self):
        raw = socket.create_connection((self.server.host, self.server.port), timeout=3)
        raw.sendall(b"GET /api/stream HTTP/1.1\r\nHost: x\r\n\r\n")
        raw.recv(64)
        raw.close()
        time.sleep(0.3)
        # Server still answers.
        self.assertEqual(self.get("/api/status")[0], 200)


class TestServerLifecycle(unittest.TestCase):
    def test_start_stop_is_clean_and_repeatable(self):
        for _ in range(3):
            hub = Hub()
            server = MonitorServer(hub, host="127.0.0.1", port=0)
            server.start()
            with urllib.request.urlopen(server.url + "api/status", timeout=5) as r:
                self.assertEqual(r.status, 200)
            server.stop()
            # Port released: connecting must now fail.
            with self.assertRaises(Exception):
                urllib.request.urlopen(server.url + "api/status", timeout=1)

    def test_no_non_daemon_threads_leak(self):
        before = {t.name for t in threading.enumerate()}
        hub = Hub()
        server = MonitorServer(hub, host="127.0.0.1", port=0)
        server.start()
        server.stop()
        time.sleep(0.3)
        after = {t.name for t in threading.enumerate()}
        leaked = {n for n in after - before if n in ("http", "heartbeat")}
        self.assertEqual(leaked, set())


class TestCsvExport(WebTestCase):
    """GET /api/export.csv, which the header's Export button downloads."""

    def test_export_serves_a_replayable_csv_as_an_attachment(self):
        for i in range(12):
            self.hub._on_sample(sample(i))
        status, body, headers = self.get("/api/export.csv")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        text = body.decode("utf-8")
        # The schema comment is what lets CsvReader refuse a foreign file;
        # without it the download is not replayable.
        self.assertTrue(text.startswith("# bms-monitor-csv/"), text[:60])
        self.assertIn("t_wall,t_mono,seq", text)
        self.assertEqual(len(text.splitlines()), 12 + 2)

    def test_export_round_trips_back_into_pack_samples(self):
        for i in range(8):
            self.hub._on_sample(sample(i))
        _, body, _ = self.get("/api/export.csv")
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "export.csv")
        with open(path, "wb") as handle:
            handle.write(body)
        packs = CsvReader(path).load()
        self.assertEqual([p.seq for p in packs], list(range(8)))

    def test_empty_history_is_409_not_an_empty_download(self):
        try:
            self.get("/api/export.csv")
            self.fail("expected 409 for an empty hub")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 409)
            self.assertIn(b"no samples", exc.read())

    def test_export_is_still_read_only(self):
        req = urllib.request.Request(self.base + "/api/export.csv",
                                     data=b"{}", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("POST to the export endpoint was accepted")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 405)


if __name__ == "__main__":
    unittest.main()
