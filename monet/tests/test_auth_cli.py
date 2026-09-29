"""
monet/tests/test_auth_cli.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Client side of `monet auth test`: io.check_server_auth (the probe) and the
authcheck CLI helpers. The server /auth/whoami route is covered in
test_power_api.py.
"""

import os
import unittest
from unittest import mock

import monet.io as mio
from monet import authcheck


class _FakeResp:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}

    def json(self):
        return self._json


class TestCheckServerAuth(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.pop("PAINT_MONET_AUTH", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_authenticated_reports_label_and_scope(self):
        os.environ["PAINT_MONET_TOKEN"] = "tok"

        def fake_get(url, **kw):
            if url.endswith("/health"):
                return _FakeResp(200)
            if url.endswith("/auth/whoami"):
                # the client must forward the bearer token
                self.assertEqual(
                    kw["headers"].get("Authorization"), "Bearer tok"
                )
                return _FakeResp(
                    200,
                    {
                        "authenticated": True,
                        "auth_enabled": True,
                        "label": "microscope-skylab",
                        "scope": "write",
                    },
                )
            raise AssertionError("unexpected url " + url)

        with mock.patch.object(mio.requests, "get", side_effect=fake_get):
            r = mio.check_server_auth("http://server:8000/")
        self.assertTrue(r["reachable"])
        self.assertTrue(r["authenticated"])
        self.assertEqual(r["label"], "microscope-skylab")
        self.assertEqual(r["scope"], "write")

    def test_401_reports_failure(self):
        os.environ["PAINT_MONET_TOKEN"] = "bad"

        def fake_get(url, **kw):
            if url.endswith("/health"):
                return _FakeResp(200)
            return _FakeResp(401)

        with mock.patch.object(mio.requests, "get", side_effect=fake_get):
            r = mio.check_server_auth("http://server:8000")
        self.assertFalse(r["authenticated"])
        self.assertTrue(r["auth_enabled"])
        self.assertEqual(r["whoami_status"], 401)

    def test_unreachable(self):
        def fake_get(url, **kw):
            raise mio.requests.exceptions.ConnectionError("nope")

        with mock.patch.object(mio.requests, "get", side_effect=fake_get):
            r = mio.check_server_auth("http://server:8000")
        self.assertFalse(r["reachable"])
        self.assertIn("unreachable", r["detail"])

    def test_fallback_old_server_token_accepted(self):
        os.environ["PAINT_MONET_TOKEN"] = "tok"

        def fake_get(url, **kw):
            if url.endswith("/health"):
                return _FakeResp(200)
            if url.endswith("/auth/whoami"):
                return _FakeResp(404)
            if "/power" in url:
                return _FakeResp(503)  # DB-only server: auth passed
            raise AssertionError("unexpected url " + url)

        with mock.patch.object(mio.requests, "get", side_effect=fake_get):
            r = mio.check_server_auth("http://server:8000")
        self.assertTrue(r["authenticated"])
        self.assertIn("no /auth/whoami", r["detail"])


class TestAuthCli(unittest.TestCase):
    def test_resolve_url_explicit_strips_slash(self):
        self.assertEqual(
            authcheck._resolve_url(None, "http://x:8000/"), "http://x:8000"
        )

    def test_resolve_url_requires_name_or_url(self):
        with self.assertRaises(SystemExit):
            authcheck._resolve_url(None, None)

    def test_auth_test_exit_code_ok(self):
        good = {
            "server_url": "http://x",
            "token_present": True,
            "reachable": True,
            "health_status": 200,
            "whoami_status": 200,
            "authenticated": True,
            "auth_enabled": True,
            "label": "skylab",
            "scope": "write",
            "detail": "ok",
        }
        with mock.patch("monet.io.check_server_auth", return_value=good):
            self.assertEqual(authcheck._auth_test(None, "http://x"), 0)

    def test_auth_test_exit_code_fail(self):
        bad = {
            "server_url": "http://x",
            "token_present": False,
            "reachable": True,
            "health_status": 200,
            "whoami_status": 401,
            "authenticated": False,
            "auth_enabled": True,
            "label": None,
            "scope": None,
            "detail": "401",
        }
        with mock.patch("monet.io.check_server_auth", return_value=bad):
            self.assertEqual(authcheck._auth_test(None, "http://x"), 1)


if __name__ == "__main__":
    unittest.main()
