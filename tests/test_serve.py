"""
test_serve.py — the server, over a real socket.

Nothing is mocked. `serve_in_thread(port=0)` asks the OS for a free port and the
tests speak HTTP to it, because the interesting failures here — path traversal,
a symlink out of the document root, a wrong content type — all live in the layer
a mocked handler skips over.

`http.client` is used rather than `urllib` deliberately: urllib normalises the
request path before sending it, which would quietly repair the exact attack the
traversal tests are trying to make.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from http.client import HTTPConnection

import _util

import serve


class ServerFixture(unittest.TestCase):
    """A real server over a temp document root, torn down per class."""

    files = {
        "index.html": "<h1>Break Desk</h1>",
        "style.css": "body { color: black; }",
        "app.js": "console.log('desk');",
        os.path.join("data", "breaks.json"): '{"breaks": []}',
    }

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="break-desk-serve-")
        cls.public = os.path.join(cls.dir, "public")
        for name, body in cls.files.items():
            path = os.path.join(cls.public, name)
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                os.makedirs(parent)
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        cls.httpd, _ = serve.serve_in_thread(port=0, public_dir=cls.public)
        cls.host, cls.port = cls.httpd.socket.getsockname()[:2]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def get(self, path):
        """Send `path` verbatim — no client-side normalisation."""
        conn = HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()


class TestRouting(ServerFixture):
    def test_root_redirects_under_the_prefix(self):
        status, headers, _ = self.get("/")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/desk/")

    def test_bare_prefix_redirects_to_the_directory(self):
        status, headers, _ = self.get("/desk")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/desk/")

    def test_the_index_is_served(self):
        status, headers, body = self.get("/desk/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"Break Desk", body)

    def test_anything_outside_the_prefix_is_not_ours(self):
        """The whole app lives under one prefix so it can sit behind a reverse
        proxy beside other tools without owning the root of a host."""
        status, _, body = self.get("/somebody-elses-app")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))


class TestContentTypes(ServerFixture):
    def test_css(self):
        _, headers, _ = self.get("/desk/style.css")
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")

    def test_javascript(self):
        _, headers, _ = self.get("/desk/app.js")
        self.assertEqual(headers["Content-Type"], "application/javascript; charset=utf-8")

    def test_json(self):
        status, headers, body = self.get("/desk/data/breaks.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(json.loads(body), {"breaks": []})

    def test_the_report_is_never_cached(self):
        """It is rebuilt in place, and a cached copy would show yesterday's
        findings on a page whose entire claim is that it shows today's."""
        for path in ("/desk/", "/desk/data/breaks.json"):
            _, headers, _ = self.get(path)
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_content_length_is_accurate(self):
        _, headers, body = self.get("/desk/app.js")
        self.assertEqual(int(headers["Content-Length"]), len(body))


class TestHealth(ServerFixture):
    def test_reports_status_and_whether_a_report_exists(self):
        status, headers, body = self.get("/desk/api/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["report"])

    def test_says_so_when_the_report_has_not_been_built(self):
        empty = tempfile.mkdtemp(prefix="break-desk-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        httpd, _ = serve.serve_in_thread(port=0, public_dir=empty)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        host, port = httpd.socket.getsockname()[:2]
        conn = HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/desk/api/health")
        payload = json.loads(conn.getresponse().read())
        conn.close()
        self.assertFalse(payload["report"])


class TestMissingFiles(ServerFixture):
    def test_404_is_json_not_an_html_error_page(self):
        status, headers, body = self.get("/desk/nope.css")
        self.assertEqual(status, 404)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertIn("nope.css", json.loads(body)["error"])

    def test_a_directory_is_not_a_file(self):
        status, _, _ = self.get("/desk/data")
        self.assertEqual(status, 404)


class TestPathTraversal(ServerFixture):
    def test_dot_dot_cannot_climb_out(self):
        for path in ("/desk/../serve.py",
                     "/desk/../../etc/passwd",
                     "/desk/data/../../serve.py"):
            status, _, _ = self.get(path)
            self.assertEqual(status, 404, path)

    def test_an_absolute_path_is_still_resolved_under_the_root(self):
        status, _, _ = self.get("/desk//etc/passwd")
        self.assertEqual(status, 404)

    def test_a_symlink_out_of_the_document_root_is_refused(self):
        """
        The test that justifies checking the resolved real path instead of
        scanning the string for "..". This request contains no traversal
        sequence at all; a textual check passes it and serves the file.
        """
        secret = os.path.join(self.dir, "outside.txt")
        with io.open(secret, "w", encoding="utf-8") as fh:
            fh.write("not for the web")
        link = os.path.join(self.public, "escape.txt")
        os.symlink(secret, link)
        self.addCleanup(os.unlink, link)

        self.assertTrue(os.path.isfile(link), "the symlink does resolve to a real file")
        status, _, body = self.get("/desk/escape.txt")
        self.assertEqual(status, 404)
        self.assertNotIn(b"not for the web", body)


class TestSafeJoinDirectly(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="break-desk-join-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_a_plain_name_resolves(self):
        got = serve._safe_join(self.root, "index.html")
        self.assertEqual(got, os.path.join(os.path.realpath(self.root), "index.html"))

    def test_a_nested_name_resolves(self):
        got = serve._safe_join(self.root, "data/breaks.json")
        self.assertTrue(got.endswith(os.path.join("data", "breaks.json")))

    def test_dot_dot_is_neutralised_rather_than_rejected(self):
        """
        `..` is collapsed against the URL path before anything touches the disk,
        so a traversal attempt resolves to a harmless name inside the root and
        404s there. The result must still be contained — that is what is being
        asserted, not the 404.
        """
        root_real = os.path.realpath(self.root)
        for relative in ("../secret", "../../etc/passwd", "/etc/passwd", "a/../../b"):
            got = serve._safe_join(self.root, relative)
            self.assertTrue(got.startswith(root_real + os.sep), relative)

    def test_a_symlink_out_of_the_root_returns_empty(self):
        """
        The case a string check cannot see: no `..` anywhere in the request, and
        the file is genuinely outside the document root.
        """
        outside = os.path.join(os.path.dirname(self.root), "elsewhere.txt")
        with io.open(outside, "w", encoding="utf-8") as fh:
            fh.write("x")
        self.addCleanup(os.unlink, outside)
        os.symlink(outside, os.path.join(self.root, "link.txt"))
        self.assertEqual(serve._safe_join(self.root, "link.txt"), "")

    def test_a_sibling_directory_with_a_shared_prefix_is_not_inside(self):
        """`/tmp/public-evil` must not pass a check that `/tmp/public` is a
        prefix of it — the separator is what makes containment containment."""
        sibling = self.root + "-evil"
        os.makedirs(os.path.join(sibling, "sub"))
        self.addCleanup(shutil.rmtree, sibling, True)
        os.symlink(sibling, os.path.join(self.root, "hop"))
        self.assertEqual(serve._safe_join(self.root, "hop/sub"), "")


class TestServerConstruction(unittest.TestCase):
    def test_port_zero_binds_something_free(self):
        """A suite that binds a fixed port fails when the demo happens to be
        running, and a test that fails for that reason teaches people to ignore
        failures."""
        httpd = serve.make_server(port=0, public_dir=tempfile.gettempdir())
        try:
            self.assertGreater(httpd.socket.getsockname()[1], 0)
        finally:
            httpd.server_close()

    def test_two_servers_do_not_share_a_document_root(self):
        a = serve.make_server(port=0, public_dir="/tmp/a")
        b = serve.make_server(port=0, public_dir="/tmp/b")
        try:
            self.assertEqual(a.RequestHandlerClass.public_dir, "/tmp/a")
            self.assertEqual(b.RequestHandlerClass.public_dir, "/tmp/b")
        finally:
            a.server_close()
            b.server_close()

    def test_it_binds_loopback_by_default(self):
        """A reconciliation desk holding client positions does not listen on
        every interface because a default said so."""
        httpd = serve.make_server(port=0, public_dir=tempfile.gettempdir())
        try:
            self.assertEqual(httpd.socket.getsockname()[0], "127.0.0.1")
        finally:
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
