"""
monet/tests/test_io_http.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Test monet.io HTTP functions end-to-end using a test server.
"""

import os
import tempfile
import unittest

import pandas as pd
from fastapi.testclient import TestClient

import monet.io as mio
from monet import DATABASE_INDEXLEVELS


class _MockResponse:
    """Adapter to make TestClient responses look like requests.Response."""

    def __init__(self, testclient_response):
        self._resp = testclient_response
        self.status_code = testclient_response.status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}: {self._resp.text}")

    def json(self):
        return self._resp.json()


class TestIOHTTP(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["MONET_DB_PATH"] = self.db_path

        # Redirect the local cache to the temp directory so tests don't write
        # to the real ~/.monet directory.
        import monet.io as _mio
        from monet import cache as _mcache

        _mcache._set_cache_dir(self.tmpdir)
        _mcache._clear_cache_registry()
        _mio._last_flush_failure.clear()

        from monet.server import app

        self.test_client = TestClient(app)
        self.test_client.__enter__()

        # Monkey-patch requests.post to route through TestClient
        import requests as _requests

        self._orig_post = _requests.post

        test_client = self.test_client

        def mock_post(url, **kwargs):
            # Extract path from URL
            from urllib.parse import urlparse

            parsed = urlparse(url)
            path = parsed.path
            json_data = kwargs.get("json")
            resp = test_client.post(path, json=json_data)
            return _MockResponse(resp)

        _requests.post = mock_post

        self.server_url = "http://localhost:8000"

    def tearDown(self):
        import requests as _requests

        _requests.post = self._orig_post
        self.test_client.__exit__(None, None, None)
        from monet import cache as _mcache

        _mcache._clear_cache_registry()

        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load_calibration(self):
        index = {
            "name": "TestScope",
            "wavelength [nm]": 488,
            "laser_power [mW]": 100,
        }
        cali_pars = {"bkg": 0.5, "amp": 45.0, "phi": 32.0}

        indexnames, indexvals = mio.save_calibration(
            self.server_url, index, cali_pars
        )

        self.assertEqual(indexnames, DATABASE_INDEXLEVELS)
        self.assertEqual(indexvals[0], "TestScope")
        self.assertEqual(indexvals[1], 488.0)
        self.assertEqual(indexvals[2], 100.0)

        # Load it back
        loaded = mio.load_calibration(self.server_url, index)
        self.assertAlmostEqual(loaded["bkg"], 0.5)
        self.assertAlmostEqual(loaded["amp"], 45.0)
        self.assertAlmostEqual(loaded["phi"], 32.0)

    def test_load_database_latest_returns_series(self):
        index = {
            "name": "TestScope",
            "wavelength [nm]": 488,
            "laser_power [mW]": 100,
        }
        mio.save_calibration(
            self.server_url, index, {"bkg": 1.0, "amp": 40.0, "phi": 30.0}
        )

        result = mio.load_database(self.server_url, index, time_idx="latest")
        self.assertIsInstance(result, pd.Series)
        self.assertAlmostEqual(result["bkg"], 1.0)

    def test_load_database_all_returns_dataframe(self):
        index = {
            "name": "TestScope",
            "wavelength [nm]": 488,
            "laser_power [mW]": 100,
        }
        mio.save_calibration(
            self.server_url, index, {"bkg": 1.0, "amp": 40.0, "phi": 30.0}
        )
        mio.save_calibration(
            self.server_url, index, {"bkg": 2.0, "amp": 50.0, "phi": 35.0}
        )

        result = mio.load_database(self.server_url, index, time_idx="all")
        self.assertIsInstance(result, pd.DataFrame)
        self.assertEqual(len(result), 2)
        # Check MultiIndex structure
        self.assertEqual(list(result.index.names), DATABASE_INDEXLEVELS)

    def test_load_database_last_combinations(self):
        index_100 = {
            "name": "TestScope",
            "wavelength [nm]": 488,
            "laser_power [mW]": 100,
        }
        index_200 = {
            "name": "TestScope",
            "wavelength [nm]": 488,
            "laser_power [mW]": 200,
        }
        mio.save_calibration(
            self.server_url,
            index_100.copy(),
            {"bkg": 1.0, "amp": 40.0, "phi": 30.0},
        )
        mio.save_calibration(
            self.server_url,
            index_200.copy(),
            {"bkg": 1.5, "amp": 42.0, "phi": 31.0},
        )
        mio.save_calibration(
            self.server_url,
            index_100.copy(),
            {"bkg": 2.0, "amp": 50.0, "phi": 35.0},
        )

        result = mio.load_database(
            self.server_url,
            {"name": "TestScope", "wavelength [nm]": 488},
            time_idx="last combinations",
        )
        self.assertIsInstance(result, pd.DataFrame)
        self.assertEqual(len(result), 2)

    def test_load_database_with_slice_none(self):
        index = {
            "name": "TestScope",
            "wavelength [nm]": 488,
            "laser_power [mW]": 100,
        }
        mio.save_calibration(
            self.server_url, index, {"bkg": 1.0, "amp": 40.0, "phi": 30.0}
        )

        # Use slice(None) as wildcard for wavelength
        result = mio.load_database(
            self.server_url,
            {"name": "TestScope", "wavelength [nm]": slice(None)},
            time_idx="all",
        )
        self.assertIsInstance(result, pd.DataFrame)
        self.assertGreaterEqual(len(result), 1)

    def test_is_server_url(self):
        self.assertTrue(mio._is_server_url("http://localhost:8000"))
        self.assertTrue(mio._is_server_url("https://server.lab.org"))
        self.assertFalse(mio._is_server_url("/path/to/database.xlsx"))
        self.assertFalse(mio._is_server_url("relative/path.xlsx"))

    def test_restart_database(self):
        index = {
            "name": "TestScope",
            "wavelength [nm]": 488,
            "laser_power [mW]": 100,
        }
        mio.save_calibration(
            self.server_url, index, {"bkg": 1.0, "amp": 40.0, "phi": 30.0}
        )
        mio.save_calibration(
            self.server_url, index, {"bkg": 2.0, "amp": 50.0, "phi": 35.0}
        )

        backup_path = mio.restart_database(self.server_url)
        self.assertIsInstance(backup_path, str)
        self.assertTrue(os.path.exists(backup_path))


class TestIOHTTPClientAuth(unittest.TestCase):
    """The io.py HTTP client must present PAINT_MONET_TOKEN so it keeps working
    against an auth-enabled monet server — otherwise turning auth on breaks
    monet's own calibrate/set/GUI DB writes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["MONET_DB_PATH"] = os.path.join(self.tmpdir, "test.db")
        os.environ.pop("PAINT_MONET_TOKEN", None)

        import monet.io as _mio
        from monet import cache as _mcache

        _mcache._set_cache_dir(self.tmpdir)
        _mcache._clear_cache_registry()
        _mio._last_flush_failure.clear()

        from monet.serviceauth import AuthConfig, TokenInfo
        from monet.server import create_app

        # Auth-enabled server: a single write token (write is a superset of
        # read), matching how a real deployment tokens its instrument clients.
        app = create_app(
            auth=AuthConfig(
                {"wtok": TokenInfo(scope="write", label="io-client")}
            )
        )
        self.test_client = TestClient(app)
        self.test_client.__enter__()

        import requests as _requests

        self._orig_post = _requests.post
        tc = self.test_client

        def mock_post(url, **kwargs):
            from urllib.parse import urlparse

            path = urlparse(url).path
            # Forward the Authorization header so the server enforces auth.
            return _MockResponse(
                tc.post(
                    path,
                    json=kwargs.get("json"),
                    headers=kwargs.get("headers"),
                )
            )

        _requests.post = mock_post
        self.server_url = "http://localhost:8000"

    def tearDown(self):
        import requests as _requests

        _requests.post = self._orig_post
        self.test_client.__exit__(None, None, None)
        os.environ.pop("PAINT_MONET_TOKEN", None)
        from monet import cache as _mcache

        _mcache._clear_cache_registry()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _index(self):
        return {
            "name": "S",
            "wavelength [nm]": 488,
            "laser_power [mW]": 100,
        }

    def test_auth_headers_reflect_env(self):
        os.environ.pop("PAINT_MONET_TOKEN", None)
        self.assertEqual(mio._auth_headers(), {})
        os.environ["PAINT_MONET_TOKEN"] = "wtok"
        self.assertEqual(mio._auth_headers(), {"Authorization": "Bearer wtok"})

    def test_write_and_read_with_token_succeeds(self):
        os.environ["PAINT_MONET_TOKEN"] = "wtok"
        mio.save_calibration(
            self.server_url,
            self._index(),
            {"bkg": 0.0, "amp": 1.0, "phi": 0.0},
        )
        loaded = mio.load_calibration(self.server_url, self._index())
        self.assertAlmostEqual(loaded["amp"], 1.0)

    def test_write_without_token_is_rejected(self):
        os.environ.pop("PAINT_MONET_TOKEN", None)
        with self.assertRaises(Exception):
            mio.save_calibration(
                self.server_url,
                self._index(),
                {"bkg": 0.0, "amp": 1.0, "phi": 0.0},
            )
