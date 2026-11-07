"""Read-only checks done by grepping the package source.

A review can miss a new ``.write(`` in a reconnect branch and a happy-path
unit test won't hit it, but a grep over the whole package will.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "bms_monitor")
GATE = os.path.join(PKG, "sources", "serial_source.py")


def python_sources():
    for base, dirs, files in os.walk(PKG):
        # Skip __pycache__ and any dot-directory. A virtualenv created
        # inside the package (.venv, .monitor) carries pip's vendored
        # code, which is not ours and would fail every rule below.
        dirs[:] = [d for d in dirs
                   if d != "__pycache__" and not d.startswith(".")]
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestNoStrayWrites(unittest.TestCase):
    """Only the serial reader thread may call .write( on a port, and the
    bytes it writes must come from encode_command()."""

    #: Writes that are unrelated to the device: file handles and HTTP
    #: responses. Listed explicitly so a new one has to be justified here.
    ALLOWED = {
        os.path.join(PKG, "logging_csv.py"): ["self._writer.writerow"],
        os.path.join(PKG, "web", "server.py"): ["self.wfile.write"],
        # Both CSV loggers: samples, and the controller's thermistors.
        os.path.join(PKG, "hub.py"): ["self._csv.write", "self._temp_csv.write"],
        # The debug console is the one place allowed to transmit. It goes
        # through encode_console_command(), which the tests below check.
        os.path.join(PKG, "console.py"): ["self.link.write", "self._serial.write",
                                          "handle.write", "self.out.write"],
    }

    def test_only_the_serial_source_writes_to_a_port(self):
        offenders = []
        for path in python_sources():
            src = read(path)
            for match in re.finditer(r"\.write\s*\(|\.writelines\s*\(", src):
                line_no = src.count("\n", 0, match.start()) + 1
                line = src.splitlines()[line_no - 1].strip()
                if path == GATE:
                    continue
                # Prose in a docstring that happens to mention .write(
                if line.startswith("#") or "``" in line:
                    continue
                allowed = self.ALLOWED.get(path, [])
                if any(token in line for token in allowed):
                    continue
                offenders.append("%s:%d  %s" % (os.path.relpath(path, ROOT), line_no, line))
        self.assertEqual(offenders, [], "unexpected .write( outside the gated path:\n" +
                         "\n".join(offenders))

    def test_the_only_serial_write_uses_the_gate(self):
        src = read(GATE)
        writes = [line.strip() for line in src.splitlines()
                  if re.search(r"handle\.write\s*\(", line)]
        self.assertEqual(len(writes), 1, writes)
        self.assertIn("payload", writes[0])
        # ... and `payload` is produced by encode_command on the line above.
        self.assertIn("payload = command_gate.encode_command(command)", src)

    def test_only_the_console_uses_the_wider_allow_list(self):
        """The polling code must not be able to reach the console's wider
        command set."""
        offenders = []
        allowed = {
            os.path.join(PKG, "console.py"),
            os.path.join(PKG, "sources", "command_gate.py"),
        }
        for path in python_sources():
            if path in allowed:
                continue
            if "encode_console_command" in read(path):
                offenders.append(os.path.relpath(path, ROOT))
        self.assertEqual(offenders, [],
                         "encode_console_command() must only be reachable "
                         "from the console")

    def test_no_source_backend_references_the_console_gate(self):
        import os as _os
        sources_dir = os.path.join(PKG, "sources")
        for name in sorted(_os.listdir(sources_dir)):
            if not name.endswith(".py") or name == "command_gate.py":
                continue
            src = read(os.path.join(sources_dir, name))
            self.assertNotIn("encode_console_command", src, name)
            self.assertNotIn("CONSOLE_COMMANDS", src, name)

    def test_the_console_write_goes_through_the_gate(self):
        src = read(os.path.join(PKG, "console.py"))
        self.assertIn("payload = command_gate.encode_console_command(command)", src)
        writes = [line.strip() for line in src.splitlines()
                  if re.search(r"self\.link\.write\s*\(", line)]
        self.assertEqual(len(writes), 1, writes)
        self.assertIn("payload", writes[0])

    def test_no_module_builds_command_bytes_by_hand(self):
        """Nobody may construct a b"...\\r" command payload outside the gate."""
        pattern = re.compile(r"""(?:b["'][0-9]{1,2}\\r["'])""")
        offenders = []
        for path in python_sources():
            if path.endswith(os.path.join("sources", "command_gate.py")):
                continue
            for line_no, line in enumerate(read(path).splitlines(), 1):
                if pattern.search(line):
                    offenders.append("%s:%d  %s" % (os.path.relpath(path, ROOT), line_no, line.strip()))
        self.assertEqual(offenders, [])


class TestSourceInterfaceHasNoTransmitPath(unittest.TestCase):
    def test_abc_exposes_only_read_operations(self):
        from bms_monitor.sources.base import TelemetrySource
        public = {name for name in dir(TelemetrySource) if not name.startswith("_")}
        self.assertEqual(public, {"start", "stop", "subscribe", "status", "name"})

    def test_the_console_does_not_go_through_a_telemetry_source(self):
        """The console opens its own link to transmit instead of widening the
        read-only interface every source shares.

        Checked on the parsed code, since console.py's docstring mentions
        TelemetrySource.
        """
        import ast

        path = os.path.join(PKG, "console.py")
        tree = ast.parse(read(path), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "TelemetrySource",
                                        "the console must not import the "
                                        "read-only source interface")
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    self.assertNotEqual(getattr(base, "id", None),
                                        "TelemetrySource", node.name)
        self.assertIn("class SerialLink", read(path))

    def test_no_concrete_source_exposes_a_send_method(self):
        from bms_monitor.sources.can_source import CanSource
        from bms_monitor.sources.replay import ReplaySource
        from bms_monitor.sources.serial_source import SerialSource
        for cls in (SerialSource, CanSource, ReplaySource):
            public = {name for name in dir(cls) if not name.startswith("_")}
            for banned in ("send", "write", "command", "transmit", "put"):
                self.assertNotIn(banned, public, "%s.%s" % (cls.__name__, banned))


class TestHttpSurfaceIsReadOnly(unittest.TestCase):
    def test_handler_rejects_all_mutating_methods(self):
        src = read(os.path.join(PKG, "web", "server.py"))
        self.assertIn("do_POST = do_PUT = do_DELETE = do_PATCH = _reject", src)

    def test_no_endpoint_mentions_a_command_parameter(self):
        src = read(os.path.join(PKG, "web", "server.py"))
        for banned in ("encode_command", "command_gate", "poll_plan ="):
            self.assertNotIn(banned, src,
                             "the web layer must have no path to the command gate")

    def test_frontend_never_posts(self):
        static = os.path.join(PKG, "web", "static")
        for name in os.listdir(static):
            if not name.endswith(".js"):
                continue
            src = read(os.path.join(static, name))
            self.assertNotIn("method: 'POST'", src)
            self.assertNotIn('method: "POST"', src)
            self.assertNotIn("XMLHttpRequest", src)


class TestFrontendSafety(unittest.TestCase):
    def test_no_innerhtml_outside_comments(self):
        """Firmware output is untrusted input and we display unparsed bytes."""
        static = os.path.join(PKG, "web", "static")
        offenders = []
        for name in sorted(os.listdir(static)):
            if not name.endswith(".js"):
                continue
            for line_no, line in enumerate(read(os.path.join(static, name)).splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("*") or stripped.startswith("//") or stripped.startswith("/*"):
                    continue
                if "innerHTML" in line or "outerHTML" in line or "insertAdjacentHTML" in line:
                    offenders.append("%s:%d  %s" % (name, line_no, stripped))
        self.assertEqual(offenders, [])

    def test_no_eval_or_function_constructor(self):
        static = os.path.join(PKG, "web", "static")
        for name in sorted(os.listdir(static)):
            if not name.endswith(".js"):
                continue
            src = read(os.path.join(static, name))
            self.assertNotIn("eval(", src)
            self.assertNotIn("new Function(", src)


class TestNoExternalResources(unittest.TestCase):
    """There's no internet at the track, so nothing can load from a CDN."""

    def test_no_remote_urls_in_any_static_asset(self):
        static = os.path.join(PKG, "web", "static")
        pattern = re.compile(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""")
        offenders = []
        for name in sorted(os.listdir(static)):
            src = read(os.path.join(static, name))
            for line_no, line in enumerate(src.splitlines(), 1):
                if pattern.search(line):
                    offenders.append("%s:%d  %s" % (name, line_no, line.strip()))
            for match in re.finditer(r"https?://[^\s\"'`)]+", src):
                # Allowed only inside a comment as documentation.
                line_start = src.rfind("\n", 0, match.start()) + 1
                prefix = src[line_start:match.start()].strip()
                if prefix.startswith("*") or prefix.startswith("//") or prefix.startswith("/*"):
                    continue
                offenders.append("%s: %s" % (name, match.group(0)))
        self.assertEqual(offenders, [])

    def test_only_the_hardware_backends_are_third_party(self):
        """Everything except pyserial and python-can is standard library."""
        import ast
        allowed_top_level = {
            "abc", "argparse", "collections", "csv", "dataclasses", "enum",
            "http", "io", "json", "math", "mimetypes", "os", "posixpath",
            "importlib", "queue", "random", "re", "select", "signal", "socket",
            "socketserver", "struct", "sys", "tempfile", "threading", "time",
            "typing", "unittest", "urllib", "webbrowser", "__future__",
        }
        lazy_ok = {"serial", "can"}
        for path in python_sources():
            tree = ast.parse(read(path), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:            # relative import, always fine
                        continue
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    if name in allowed_top_level or name in lazy_ok:
                        continue
                    self.fail("%s imports third-party module %r" %
                              (os.path.relpath(path, ROOT), name))

    def test_third_party_backends_are_only_imported_lazily(self):
        """Importing the package must not require pyserial or python-can."""
        import ast
        for path in python_sources():
            tree = ast.parse(read(path), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = ""
                    if isinstance(node, ast.Import):
                        module = node.names[0].name
                    elif node.module:
                        module = node.module
                    if module.split(".")[0] not in ("serial", "can"):
                        continue
                    # Must be inside a function, never at module scope.
                    self.assertGreater(node.col_offset, 0,
                                       "%s imports %s at module scope" %
                                       (os.path.relpath(path, ROOT), module))


if __name__ == "__main__":
    unittest.main()
