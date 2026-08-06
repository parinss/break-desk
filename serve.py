#!/usr/bin/env python3
"""
serve.py — the desk, on http://localhost:8110/desk/

Standard library only. No framework, no build step, no node_modules: the whole
point of the demo is that a reviewer can read every line that produced a number,
and a toolchain they have to install first is a toolchain they will not read
through.

Everything lives under the /desk prefix so this can sit behind a reverse proxy
next to other tools without owning the root of a host.

    python3 serve.py            # port 8110
    python3 serve.py --port 0   # ephemeral port, printed on stdout (tests use this)
"""

from __future__ import annotations

import argparse
import io
import json
import os
import posixpath
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
PREFIX = "/desk"
DEFAULT_PORT = 8110

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def _safe_join(root, relative):
    # type: (str, str) -> str
    """
    Resolve a URL path under `root`, or return "" if it escapes.

    Traversal is checked on the resolved real path rather than by inspecting the
    string for "..", because the string check is the one that gets bypassed. A
    symlink inside public/ pointing outside it would pass a textual check and
    fail this one.
    """
    relative = posixpath.normpath("/" + relative).lstrip("/")
    candidate = os.path.realpath(os.path.join(root, relative))
    root_real = os.path.realpath(root)
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        return ""
    return candidate


class DeskHandler(BaseHTTPRequestHandler):
    server_version = "BreakDesk/1.0"
    public_dir = PUBLIC

    def do_GET(self):  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0].split("#", 1)[0]

        if path == "/" or path == PREFIX:
            self._redirect(PREFIX + "/")
            return
        if not path.startswith(PREFIX + "/"):
            self._error(404, "not found")
            return

        relative = path[len(PREFIX) + 1:]
        if relative == "" or relative.endswith("/"):
            relative += "index.html"

        if relative == "api/health":
            self._json({"status": "ok", "report": self._report_exists()})
            return

        target = _safe_join(self.public_dir, relative)
        if not target or not os.path.isfile(target):
            self._error(404, "not found: %s" % relative)
            return

        ext = os.path.splitext(target)[1].lower()
        with io.open(target, "rb") as fh:
            body = fh.read()
        self._respond(200, CONTENT_TYPES.get(ext, "application/octet-stream"), body)

    def _report_exists(self):
        # type: () -> bool
        return os.path.isfile(os.path.join(self.public_dir, "data", "breaks.json"))

    def _respond(self, status, content_type, body):
        # type: (int, str, bytes) -> None
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The report is rebuilt in place; a cached copy would show yesterday's
        # findings on a page whose entire claim is that it shows today's.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status=200):
        # type: (object, int) -> None
        self._respond(status, CONTENT_TYPES[".json"],
                      json.dumps(payload).encode("utf-8"))

    def _redirect(self, location):
        # type: (str) -> None
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _error(self, status, message):
        # type: (int, str) -> None
        self._json({"error": message}, status=status)

    def log_message(self, fmt, *args):
        # Quiet by default; the tests spin up dozens of these.
        if os.environ.get("BREAK_DESK_ACCESS_LOG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def make_server(port=DEFAULT_PORT, public_dir=PUBLIC, host="127.0.0.1"):
    # type: (int, str, str) -> ThreadingHTTPServer
    """
    Build a server without starting it.

    Port 0 asks the OS for a free port, which is what the tests use — a suite
    that binds a fixed port fails when you happen to have the demo running, and
    a test that fails for that reason teaches people to ignore failures.
    """
    handler = type("BoundDeskHandler", (DeskHandler,), {"public_dir": public_dir})
    return ThreadingHTTPServer((host, port), handler)


def serve_in_thread(port=0, public_dir=PUBLIC):
    # type: (int, str) -> tuple
    """Start a server on a background thread. Returns (server, base_url)."""
    httpd = make_server(port=port, public_dir=public_dir)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, bound = httpd.socket.getsockname()[:2]
    return httpd, "http://%s:%d%s" % (host, bound, PREFIX)


def main(argv=None):
    # type: (list) -> int
    parser = argparse.ArgumentParser(description="Serve the break desk.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--public", default=PUBLIC)
    args = parser.parse_args(argv)

    report = os.path.join(args.public, "data", "breaks.json")
    if not os.path.isfile(report):
        sys.stderr.write(
            "no report at %s -- run: python3 scripts/build.py\n"
            % os.path.relpath(report, ROOT)
        )

    httpd = make_server(port=args.port, public_dir=args.public)
    bound = httpd.socket.getsockname()[1]
    sys.stdout.write("break desk on http://localhost:%d%s/\n" % (bound, PREFIX))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
