"""
monet/tests/test_dashboard.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tests for the dashboard API endpoints (mounted under /dashboard) and the
transmission-objective factor endpoints they read from.
"""

import os
import tempfile
import unittest

from fastapi.testclient import TestClient


class TestDashboard(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["MONET_DB_PATH"] = self.db_path
        from monet.server import app

        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _save_record(
        self, name="TestScope", wavelength=488, laser_power=100, params=None
    ):
        if params is None:
            params = {"bkg": 0.5, "amp": 45.0, "phi": 32.0}
        resp = self.client.post(
            "/calibrations",
            json={
                "index": {
                    "name": name,
                    "wavelength [nm]": wavelength,
                    "laser_power [mW]": laser_power,
                },
                "parameters": params,
            },
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def _save_factor(
        self,
        device="TestScope",
        wavelength=488,
        date="2024-01-01",
        mean=0.8,
        std=0.05,
        n=20,
    ):
        resp = self.client.post(
            "/factors",
            json={
                "device_name": device,
                "wavelength_nm": wavelength,
                "calibration_date": date,
                "transmission_objective_mean": mean,
                "transmission_objective_std": std,
                "n_points": n,
            },
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    # ── dashboard HTML page ──────────────────────────────────────────────

    def test_dashboard_html(self):
        resp = self.client.get("/dashboard/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])

    # ── /dashboard/api/filters ───────────────────────────────────────────

    def test_filters_empty(self):
        resp = self.client.get("/dashboard/api/filters")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["devices"], [])
        self.assertIsNone(data["date_min"])
        self.assertIsNone(data["date_max"])

    def test_filters_populated(self):
        self._save_record(wavelength=488, laser_power=100)
        self._save_record(wavelength=561, laser_power=200)
        resp = self.client.get("/dashboard/api/filters")
        data = resp.json()
        self.assertEqual(data["devices"], ["TestScope"])
        self.assertEqual(data["wavelengths"], [488.0, 561.0])
        self.assertEqual(data["laser_powers"], [100.0, 200.0])
        self.assertIsNotNone(data["date_min"])

    # ── /dashboard/api/timeseries ────────────────────────────────────────

    def test_timeseries_no_filter(self):
        self._save_record(wavelength=488)
        self._save_record(wavelength=561)
        resp = self.client.post("/dashboard/api/timeseries", json={})
        self.assertEqual(resp.status_code, 200)
        records = resp.json()["records"]
        self.assertEqual(len(records), 2)
        # Each record carries a combined 'dt' field and decoded parameters.
        self.assertIn("dt", records[0])
        self.assertIn("parameters", records[0])
        self.assertEqual(records[0]["parameters"]["amp"], 45.0)

    def test_timeseries_filtered(self):
        self._save_record(wavelength=488)
        self._save_record(wavelength=561)
        resp = self.client.post(
            "/dashboard/api/timeseries",
            json={
                "wavelengths": [488.0],
            },
        )
        records = resp.json()["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["wavelength"], 488.0)

    def test_timeseries_date_range(self):
        self._save_record()
        # A future date_from should exclude today's record.
        resp = self.client.post(
            "/dashboard/api/timeseries",
            json={
                "date_from": "2999-01-01",
            },
        )
        self.assertEqual(resp.json()["records"], [])

    # ── /dashboard/api/transmission_objectives + /factors ────────────────

    def test_transmission_objectives_empty(self):
        resp = self.client.get("/dashboard/api/transmission_objectives")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_transmission_objectives_with_factor(self):
        self._save_factor(mean=0.8)
        resp = self.client.get("/dashboard/api/transmission_objectives")
        rows = resp.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device"], "TestScope")
        self.assertEqual(rows[0]["transmission_objective_mean"], 0.8)

    def test_transmission_objectives_device_filter(self):
        self._save_factor(device="ScopeA")
        self._save_factor(device="ScopeB")
        resp = self.client.get(
            "/dashboard/api/transmission_objectives",
            params={"device": "ScopeA"},
        )
        rows = resp.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device"], "ScopeA")

    def test_factor_update_replaces_existing(self):
        self._save_factor(mean=0.8)
        # Same (device, wavelength, date) -> update, not a second row.
        updated = self._save_factor(mean=0.9)
        self.assertEqual(updated["transmission_objective_mean"], 0.9)
        resp = self.client.get("/dashboard/api/transmission_objectives")
        self.assertEqual(len(resp.json()), 1)

    def test_factor_query_endpoint(self):
        self._save_factor(device="ScopeA", wavelength=488)
        self._save_factor(device="ScopeB", wavelength=561)
        resp = self.client.post(
            "/factors/query", json={"device_name": "ScopeA"}
        )
        self.assertEqual(resp.status_code, 200)
        records = resp.json()["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["device_name"], "ScopeA")


class TestDashboardAuth(unittest.TestCase):
    """The dashboard data routes require a token when the server enforces auth;
    the HTML shell stays public so the login UI can load."""

    def _client(self, auth):
        import shutil

        self.tmpdir = tempfile.mkdtemp()
        os.environ["MONET_DB_PATH"] = os.path.join(self.tmpdir, "test.db")
        os.environ.pop("PAINT_MONET_TOKENS", None)
        from monet.server import create_app

        client = TestClient(create_app(auth=auth))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.addCleanup(lambda: shutil.rmtree(self.tmpdir, ignore_errors=True))
        return client

    def test_html_shell_is_public(self):
        from monet.serviceauth import AuthConfig, TokenInfo

        client = self._client(
            AuthConfig({"r": TokenInfo(scope="read", label="viewer")})
        )
        # The page must load without a token so the login UI can render.
        self.assertEqual(client.get("/dashboard/").status_code, 200)

    def test_data_routes_require_read_token(self):
        from monet.serviceauth import AuthConfig, TokenInfo

        client = self._client(
            AuthConfig({"r": TokenInfo(scope="read", label="viewer")})
        )
        # no token -> 401 on every data route
        self.assertEqual(client.get("/dashboard/api/filters").status_code, 401)
        self.assertEqual(
            client.post("/dashboard/api/timeseries", json={}).status_code, 401
        )
        self.assertEqual(
            client.get("/dashboard/api/transmission_objectives").status_code,
            401,
        )
        # read token -> 200
        h = {"Authorization": "Bearer r"}
        self.assertEqual(
            client.get("/dashboard/api/filters", headers=h).status_code, 200
        )
        self.assertEqual(
            client.post(
                "/dashboard/api/timeseries", json={}, headers=h
            ).status_code,
            200,
        )
        self.assertEqual(
            client.get(
                "/dashboard/api/transmission_objectives", headers=h
            ).status_code,
            200,
        )

    def test_auth_disabled_needs_no_token(self):
        from monet.serviceauth import AuthConfig

        client = self._client(AuthConfig({}))  # disabled (loopback dev)
        self.assertEqual(client.get("/dashboard/api/filters").status_code, 200)


if __name__ == "__main__":
    unittest.main()
