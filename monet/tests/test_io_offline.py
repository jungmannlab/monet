"""
monet/tests/test_io_offline.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Integration tests for io.py offline behaviour:
- When the server is unreachable, saves go to the local outbox.
- Reads fall back to the local cache.
- The outbox is flushed when connectivity is restored.
"""

import os
import tempfile
import unittest

import requests

import monet.io as mio
from monet import cache as mcache


class _ConnectionError(requests.exceptions.ConnectionError):
    pass


class TestOfflineSave(unittest.TestCase):
    """Saving while the server is unreachable."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        mcache._set_cache_dir(self.tmpdir)
        mcache._clear_cache_registry()
        # Reset flush-failure timestamps so cooldown does not interfere
        mio._last_flush_failure.clear()
        self.server_url = "http://offline-test-server:8000"

        import requests as _requests

        self._orig_post = _requests.post
        _requests.post = self._raise_connection_error

    def _raise_connection_error(self, *args, **kwargs):
        raise _ConnectionError("Simulated network failure")

    def tearDown(self):
        import requests as _requests

        _requests.post = self._orig_post
        mcache._clear_cache_registry()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_goes_to_outbox_and_cache(self):
        index = {
            "name": "TestScope",
            "wavelength [nm]": 488,
            "laser_power [mW]": 100,
        }
        cali_pars = {"bkg": 0.5, "amp": 45.0}

        indexnames, indexvals = mio.save_calibration(
            self.server_url, index, cali_pars
        )

        # Return values should still be meaningful
        self.assertEqual(indexvals[0], "TestScope")
        self.assertEqual(indexvals[1], 488.0)

        # Entry must be in the local cache
        cache = mcache._get_cache(self.server_url)
        records = cache.query_calibrations(
            {"name": "TestScope"}, time_idx="latest"
        )
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0]["parameters"]["bkg"], 0.5)

        # Entry must be in the outbox
        self.assertEqual(cache.pending_outbox_count(), 1)

    def test_load_falls_back_to_cache(self):
        # Pre-populate the cache directly
        cache = mcache._get_cache(self.server_url)
        cache.upsert_calibration(
            {
                "device_name": "TestScope",
                "wavelength_nm": 488.0,
                "laser_power_mw": 100.0,
                "calibration_date": "2024-01-01",
                "calibration_time": "10:00",
                "parameters": {"bkg": 3.0, "amp": 42.0},
            }
        )

        import pandas as pd

        result = mio.load_database(
            self.server_url,
            {
                "name": "TestScope",
                "wavelength [nm]": 488.0,
                "laser_power [mW]": 100.0,
            },
            time_idx="latest",
        )
        self.assertIsInstance(result, pd.Series)
        self.assertAlmostEqual(result["bkg"], 3.0)

    def test_load_raises_key_error_when_cache_empty(self):
        with self.assertRaises(KeyError):
            mio.load_database(
                self.server_url,
                {"name": "NoScope"},
                time_idx="latest",
            )

    def test_delete_queued_in_outbox(self):
        # Save something to the cache first
        cache = mcache._get_cache(self.server_url)
        cache.upsert_calibration(
            {
                "device_name": "TestScope",
                "wavelength_nm": 488.0,
                "laser_power_mw": 100.0,
                "calibration_date": "2024-01-01",
                "calibration_time": "10:00",
                "parameters": {"bkg": 1.0},
            }
        )

        count = mio.delete_calibration(self.server_url, {"name": "TestScope"})
        self.assertEqual(count, 1)
        # Local cache should be empty
        records = cache.query_calibrations(
            {"name": "TestScope"}, time_idx="all"
        )
        self.assertEqual(len(records), 0)
        # Outbox should have the delete queued
        self.assertEqual(cache.pending_outbox_count(), 1)

    def test_factor_save_goes_to_cache_and_outbox(self):
        mio._save_factor_http(
            self.server_url, "TestScope", 488, "2024-01-01", 0.92, 0.01, 50
        )
        cache = mcache._get_cache(self.server_url)
        records = cache.query_factors("TestScope", 488)
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0]["transmission_objective_mean"], 0.92)
        self.assertEqual(cache.pending_outbox_count(), 1)


class TestOutboxFlush(unittest.TestCase):
    """Outbox is replayed when connectivity is restored."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        mcache._set_cache_dir(self.tmpdir)
        mcache._clear_cache_registry()
        mio._last_flush_failure.clear()

        db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["MONET_DB_PATH"] = db_path

        from fastapi.testclient import TestClient

        from monet.server import app

        self.test_client = TestClient(app)
        self.test_client.__enter__()

        self.server_url = "http://flush-test:8000"

        import requests as _requests

        self._orig_post = _requests.post
        self._fail_next = False
        self._test_client = self.test_client

        def smart_post(url, **kwargs):
            if self._fail_next:
                raise requests.exceptions.ConnectionError("Simulated")
            from urllib.parse import urlparse

            class _Resp:
                def __init__(self, r):
                    self._r = r
                    self.status_code = r.status_code

                def raise_for_status(self):
                    if self.status_code >= 400:
                        raise Exception(f"HTTP {self.status_code}")

                def json(self):
                    return self._r.json()

            parsed = urlparse(url)
            resp = self._test_client.post(parsed.path, json=kwargs.get("json"))
            return _Resp(resp)

        _requests.post = smart_post

    def tearDown(self):
        import requests as _requests

        _requests.post = self._orig_post
        self.test_client.__exit__(None, None, None)
        mcache._clear_cache_registry()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_outbox_flushed_on_reconnect(self):
        index = {
            "name": "TestScope",
            "wavelength [nm]": 488,
            "laser_power [mW]": 100,
        }
        cali_pars = {"bkg": 7.0, "amp": 30.0}

        # Simulate offline save
        self._fail_next = True
        mio.save_calibration(self.server_url, index, cali_pars)
        cache = mcache._get_cache(self.server_url)
        self.assertEqual(cache.pending_outbox_count(), 1)

        # Restore connectivity and trigger a new operation — outbox
        # should flush
        self._fail_next = False
        mio._last_flush_failure.clear()  # bypass cooldown
        mio.save_calibration(self.server_url, index, {"bkg": 8.0, "amp": 31.0})

        # Outbox should now be empty
        self.assertEqual(cache.pending_outbox_count(), 0)

        # Server should have the previously-offline calibration
        import pandas as pd

        result = mio.load_database(self.server_url, index, time_idx="all")
        self.assertIsInstance(result, pd.DataFrame)
        bkgs = [result.iloc[i]["bkg"] for i in range(len(result))]
        self.assertIn(7.0, bkgs)


class TestFactorSaveHttpError(unittest.TestCase):
    """A transient (5xx / unclassified) factor-save failure must NOT be dropped.

    Regression for the "set 40 mW sample, get ~32" divergence: a 5xx while the
    server restarts used to be swallowed silently, so the projection factor the
    power-setting path reads was lost while the pair store the Database tab
    shows still had it. The save must now cache + outbox the factor so the next
    load flush retries it. (A permanent 4xx is handled separately — see
    TestFactorSavePermanentReject.)
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        mcache._set_cache_dir(self.tmpdir)
        mcache._clear_cache_registry()
        mio._last_flush_failure.clear()
        self.server_url = "http://http-error-test-server:8000"

        import requests as _requests

        self._orig_post = _requests.post
        _requests.post = self._raise_http_error

    def _raise_http_error(self, *args, **kwargs):
        class _Resp:
            status_code = 500

            def raise_for_status(self):
                raise requests.exceptions.HTTPError("500 Server Error")

        return _Resp()

    def tearDown(self):
        import requests as _requests

        _requests.post = self._orig_post
        mcache._clear_cache_registry()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_http_error_factor_save_is_cached_and_outboxed(self):
        mio._save_factor_http(
            self.server_url, "TestScope", 488, "2024-01-01", 0.8, 0.0, 50
        )
        cache = mcache._get_cache(self.server_url)
        records = cache.query_factors("TestScope", 488)
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0]["transmission_objective_mean"], 0.8)
        # A 5xx/unclassified failure is transient → queued for retry.
        self.assertEqual(cache.pending_outbox_count(), 1)


class TestFactorSavePermanentReject(unittest.TestCase):
    """A permanent 4xx on a factor save is cached but NOT outboxed, so it does
    not become a poison entry replayed (and re-warned) on every HTTP op."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        mcache._set_cache_dir(self.tmpdir)
        mcache._clear_cache_registry()
        mio._last_flush_failure.clear()
        self.server_url = "http://reject-test-server:8000"

        import requests as _requests

        self._orig_post = _requests.post
        _requests.post = self._raise_4xx

    def _raise_4xx(self, *args, **kwargs):
        class _Resp:
            status_code = 422

            def raise_for_status(self):
                err = requests.exceptions.HTTPError("422 Unprocessable")
                err.response = self
                raise err

        return _Resp()

    def tearDown(self):
        import requests as _requests

        _requests.post = self._orig_post
        mcache._clear_cache_registry()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_4xx_factor_save_is_cached_but_not_outboxed(self):
        mio._save_factor_http(
            self.server_url, "TestScope", 488, "2024-01-01", 0.8, 0.0, 50
        )
        cache = mcache._get_cache(self.server_url)
        records = cache.query_factors("TestScope", 488)
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0]["transmission_objective_mean"], 0.8)
        # Permanent reject → not queued (would otherwise retry forever).
        self.assertEqual(cache.pending_outbox_count(), 0)
