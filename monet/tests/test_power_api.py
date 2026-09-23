"""
monet/tests/test_power_api.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

WP-12a — the target-power API and its auth/safety interlocks.

Covers, against the simulated ``Test*`` hardware:

* setting a target drives the closed-loop PI setter and returns measured power;
* the hard per-laser max-power ceiling (C34) clamps a too-high request *before*
  the instrument is actuated (runtime interlock);
* the shared bearer-token auth (WP-3b / ADR-001): 401 without a token, 403 when a
  ``read`` token attempts a power-write (unauthenticated actuation refused),
  loopback needs no token; and
* the fail-closed host guard refuses a non-loopback bind without tokens.

:authors: Heinrich Grabmayr, 2024
:copyright: Copyright (c) 2024 Jungmann Lab, MPI of Biochemistry
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import pandas as pd
from fastapi.testclient import TestClient

import monet.control as mco
from monet import DATABASE_INDEXLEVELS
from monet.serviceauth import AuthConfig, TokenInfo


class _TrackingAttenuator:
    """Attenuator that records the last set position (the stock TestAttenuator
    is a no-op whose curr_pos() is always 0)."""

    def __init__(self, start=0.0):
        self._pos = start

    def set(self, val):
        self._pos = val

    def curr_pos(self):
        return self._pos

    def home(self):
        self._pos = 0.0


class _AttenuatorCurvePowerMeter:
    """Meter whose reading follows the attenuator calibration curve (models
    'fixed_laser' mode); `miscal` is a steady offset for the PI loop to
    correct."""

    unit = "mW"

    def __init__(self, instrument, miscal=1.0):
        self._inst = instrument
        self._miscal = miscal

    def read(self, averaging=10):
        att = self._inst.attenuator.curr_pos()
        return self._inst.analyzer.estimate_power(att) * self._miscal


def _build_control(safety=None):
    """IlluminationLaserControl with a linear calibration for lasers 488/561 at
    laser powers 50 and 100 mW, and a tracking attenuator so feedback works.
    Optionally attach a ``safety`` config block (max-power ceilings)."""
    os.makedirs("monet/tests/TestData/control", exist_ok=True)
    datim = [
        datetime.now().strftime("%Y-%m-%d"),
        datetime.now().strftime("%H:%M"),
    ]
    db = pd.DataFrame(
        index=pd.MultiIndex.from_product(
            [
                ["DefaultMicroscope"],
                ["488", "561"],
                [50, 100],
                [datim[0]],
                [datim[1]],
            ],
            names=tuple(DATABASE_INDEXLEVELS),
        ),
        data={"bkg": [0, 0, 0, 0], "amp": [1.0, 2.0, 0.8, 1.6]},
    )
    db_path = "monet/tests/TestData/control/power_api_test_db.xlsx"
    db.to_excel(db_path)

    config = {
        "database": db_path,
        "dest_calibration_plot": "monet/tests/TestData/control/",
        "index": {"name": "DefaultMicroscope"},
        "attenuation": {
            "classpath": "monet.attenuation.TestAttenuator",
            "init_kwargs": {
                "bkg": 0,
                "amp": 50,
                "phi": 30,
                "start": 30,
                "step": 5,
            },
        },
        "analysis": {
            "classpath": "monet.analysis.LinearCurveAnalyzer",
            "init_kwargs": {"min": 0, "max": 100, "step": 5},
        },
        "lasers": {
            "488": {
                "classpath": "monet.laser.TestLaser",
                "init_kwargs": {"port": "COM4"},
            },
            "561": {
                "classpath": "monet.laser.TestLaser",
                "init_kwargs": {"port": "COM7"},
            },
        },
    }
    if safety is not None:
        config["safety"] = safety
    ctrl = mco.IlluminationLaserControl(config)
    ctrl.attenuator = _TrackingAttenuator(start=30)
    ctrl.laser = 488
    ctrl.laserpower = 50
    return ctrl, config


class _AppMixin:
    """Build a TestClient over a monet server app, managing a temp DB so
    lifespan can open the SQLite engine."""

    def _make_client(
        self, auth=None, instrument=None, powermeter=None, config=None
    ):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["MONET_DB_PATH"] = os.path.join(self.tmpdir, "test.db")
        os.environ.pop("PAINT_MONET_TOKENS", None)
        from monet.server import create_app

        app = create_app(
            auth=auth,
            instrument=instrument,
            powermeter=powermeter,
            config=config,
        )
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)

        def _rmtmp():
            import shutil

            shutil.rmtree(self.tmpdir, ignore_errors=True)

        self.addCleanup(_rmtmp)
        return client


class TestPowerAPIBehaviour(_AppMixin, unittest.TestCase):
    """The power API drives the hardware and returns measured power."""

    @mock.patch("time.sleep")
    def test_set_power_closed_loop_returns_measured(self, _sleep):
        """A target drives the PI loop and the response reports the measured
        power converged to the target (Tier-3 tolerance check)."""
        ctrl, config = _build_control()
        pm = _AttenuatorCurvePowerMeter(ctrl, miscal=1.1)
        client = self._make_client(
            instrument=ctrl, powermeter=pm, config=config
        )
        resp = client.post(
            "/power/set",
            json={
                "laser": 488,
                "target_power_mw": 30,
                "mode": "fixed_laser",
                "tolerance_pct": 2.0,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["converged"])
        self.assertFalse(body["clamped"])
        self.assertEqual(body["requested_power_mw"], 30)
        self.assertEqual(body["target_power_mw"], 30)
        # measured within tolerance of target across the calibrated range
        self.assertLessEqual(
            abs(body["measured_power_mw"] - 30) / 30 * 100.0, 2.0
        )

    def test_set_power_open_loop_no_meter(self):
        """With no meter attached the server sets open-loop from the
        calibration and reports the predicted power."""
        ctrl, config = _build_control()
        client = self._make_client(
            instrument=ctrl, powermeter=None, config=config
        )
        resp = client.post(
            "/power/set", json={"laser": 488, "target_power_mw": 40}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["mode"], "combined")
        self.assertAlmostEqual(body["measured_power_mw"], 40, delta=1.0)

    def test_read_back_power(self):
        """GET /power reports predicted (and measured, when a meter is
        present) power without enabling a laser."""
        ctrl, config = _build_control()
        pm = _AttenuatorCurvePowerMeter(ctrl, miscal=1.0)
        client = self._make_client(
            instrument=ctrl, powermeter=pm, config=config
        )
        # start from all-off; a read must not switch anything on
        for las in ctrl.lasers.values():
            las.enabled = False
        resp = client.get("/power", params={"laser": 488})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["laser"], 488)
        self.assertTrue(body["has_powermeter"])
        self.assertIsNotNone(body["predicted_power_mw"])
        self.assertFalse(
            any(las.enabled for las in ctrl.lasers.values()),
            "read-back must not enable a laser",
        )

    def test_read_back_rejects_non_active_laser_without_mutating(self):
        """GET /power is read-only: a non-active laser is rejected (409) and the
        instrument's current laser / set-point are left untouched."""
        ctrl, config = _build_control()
        ctrl.laser = 488
        before_laser = ctrl.curr_laser
        before_lp = ctrl.laserpower
        client = self._make_client(instrument=ctrl, config=config)
        resp = client.get("/power", params={"laser": 561})
        self.assertEqual(resp.status_code, 409)
        # no state mutation from a read
        self.assertEqual(ctrl.curr_laser, before_laser)
        self.assertEqual(ctrl.laserpower, before_lp)

    def test_unknown_laser_is_422(self):
        ctrl, config = _build_control()
        client = self._make_client(instrument=ctrl, config=config)
        resp = client.post(
            "/power/set", json={"laser": 999, "target_power_mw": 10}
        )
        self.assertEqual(resp.status_code, 422)

    def test_no_instrument_is_503(self):
        """DB-only deployment (no microscope): the power route is present but
        refuses with 503."""
        client = self._make_client(instrument=None)
        resp = client.post(
            "/power/set", json={"laser": 488, "target_power_mw": 10}
        )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(client.get("/power").status_code, 503)


class TestPowerSafetyInterlock(_AppMixin, unittest.TestCase):
    """The hard per-laser max-power ceiling (C34), enforced in code."""

    def test_request_above_ceiling_is_clamped(self):
        """A request above the ceiling is clamped down before actuation; the
        hardware is never driven above the ceiling."""
        ctrl, config = _build_control(safety={"max_power_mw": {488: 40}})
        client = self._make_client(
            instrument=ctrl, powermeter=None, config=config
        )
        resp = client.post(
            "/power/set", json={"laser": 488, "target_power_mw": 80}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["clamped"])
        self.assertEqual(body["requested_power_mw"], 80)
        self.assertEqual(body["target_power_mw"], 40)
        self.assertEqual(body["max_power_mw"], 40)
        # measured power never exceeds the ceiling
        self.assertLessEqual(body["measured_power_mw"], 40 + 1e-6)

    def test_request_below_ceiling_not_clamped(self):
        ctrl, config = _build_control(safety={"max_power_mw": {488: 90}})
        client = self._make_client(
            instrument=ctrl, powermeter=None, config=config
        )
        resp = client.post(
            "/power/set", json={"laser": 488, "target_power_mw": 40}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["clamped"])
        self.assertEqual(body["target_power_mw"], 40)

    def test_ceiling_accepts_string_laser_keys(self):
        """Config from YAML may key the ceiling by a numeric string."""
        ctrl, config = _build_control(safety={"max_power_mw": {"488": 40}})
        client = self._make_client(
            instrument=ctrl, powermeter=None, config=config
        )
        resp = client.post(
            "/power/set", json={"laser": 488, "target_power_mw": 80}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["clamped"])

    def test_malformed_safety_config_refuses_construction(self):
        """Fail-closed: a broken ceiling config refuses to build the instrument
        rather than silently running without the interlock."""
        with self.assertRaises(ValueError):
            _build_control(safety={"max_power_mw": {488: "oops"}})


class TestPowerAPIAuth(_AppMixin, unittest.TestCase):
    """Bearer-token auth on the actuation route (WP-3b helper reused)."""

    def _enabled_auth(self):
        return AuthConfig(
            {
                "rtok": TokenInfo(scope="read", label="reader"),
                "wtok": TokenInfo(scope="write", label="microscope-x"),
            }
        )

    def test_power_write_without_token_is_401(self):
        ctrl, config = _build_control()
        client = self._make_client(
            auth=self._enabled_auth(), instrument=ctrl, config=config
        )
        resp = client.post(
            "/power/set", json={"laser": 488, "target_power_mw": 10}
        )
        self.assertEqual(resp.status_code, 401)

    def test_read_token_cannot_actuate_is_403(self):
        """A read token attempting a power-write is refused (403) before any
        hardware call — unauthenticated actuation rejected."""
        ctrl, config = _build_control()
        # a tracking attenuator whose position would move if actuated
        start = ctrl.attenuator.curr_pos()
        client = self._make_client(
            auth=self._enabled_auth(), instrument=ctrl, config=config
        )
        resp = client.post(
            "/power/set",
            json={"laser": 488, "target_power_mw": 10},
            headers={"Authorization": "Bearer rtok"},
        )
        self.assertEqual(resp.status_code, 403)
        # hardware untouched: attenuator did not move
        self.assertEqual(ctrl.attenuator.curr_pos(), start)

    def test_write_token_can_actuate(self):
        ctrl, config = _build_control()
        client = self._make_client(
            auth=self._enabled_auth(),
            instrument=ctrl,
            powermeter=None,
            config=config,
        )
        resp = client.post(
            "/power/set",
            json={"laser": 488, "target_power_mw": 40},
            headers={"Authorization": "Bearer wtok"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        # the attributable holder label is echoed back
        self.assertEqual(resp.json()["label"], "microscope-x")

    def test_read_token_can_read_back(self):
        ctrl, config = _build_control()
        client = self._make_client(
            auth=self._enabled_auth(), instrument=ctrl, config=config
        )
        resp = client.get(
            "/power",
            params={"laser": 488},
            headers={"Authorization": "Bearer rtok"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_loopback_needs_no_token(self):
        """With auth disabled (no tokens), the in-process client actuates
        without a token — the zero-config loopback dev path."""
        ctrl, config = _build_control()
        client = self._make_client(
            auth=None, instrument=ctrl, powermeter=None, config=config
        )
        resp = client.post(
            "/power/set", json={"laser": 488, "target_power_mw": 40}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["label"])


class TestDBRouteAuth(_AppMixin, unittest.TestCase):
    """Auth also guards the calibration DB: write on edits, read on queries."""

    def _enabled_auth(self):
        return AuthConfig(
            {
                "rtok": TokenInfo(scope="read", label="reader"),
                "wtok": TokenInfo(scope="write", label="writer"),
            }
        )

    def test_calibration_write_requires_write_token(self):
        client = self._make_client(auth=self._enabled_auth())
        payload = {
            "index": {
                "name": "S",
                "wavelength [nm]": 488,
                "laser_power [mW]": 100,
            },
            "parameters": {"bkg": 0.0, "amp": 1.0, "phi": 0.0},
        }
        # no token -> 401
        self.assertEqual(
            client.post("/calibrations", json=payload).status_code, 401
        )
        # read token -> 403
        self.assertEqual(
            client.post(
                "/calibrations",
                json=payload,
                headers={"Authorization": "Bearer rtok"},
            ).status_code,
            403,
        )
        # write token -> 200
        self.assertEqual(
            client.post(
                "/calibrations",
                json=payload,
                headers={"Authorization": "Bearer wtok"},
            ).status_code,
            200,
        )

    def test_query_allows_read_token(self):
        client = self._make_client(auth=self._enabled_auth())
        resp = client.post(
            "/calibrations/query",
            json={"index": {"name": "none"}, "time_idx": "all"},
            headers={"Authorization": "Bearer rtok"},
        )
        # read token accepted (404 = no data, not an auth failure)
        self.assertEqual(resp.status_code, 404)

    def test_health_is_public(self):
        client = self._make_client(auth=self._enabled_auth())
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})


class TestServeHostGuard(unittest.TestCase):
    """Fail-closed host guard in `monet serve` (ADR-001 / C18)."""

    def tearDown(self):
        os.environ.pop("PAINT_MONET_TOKENS", None)

    def test_non_loopback_without_tokens_refuses(self):
        import monet.__main__ as mmain

        os.environ.pop("PAINT_MONET_TOKENS", None)
        with mock.patch.object(
            sys, "argv", ["monet", "serve", "--host", "0.0.0.0"]
        ):
            with self.assertRaises(SystemExit):
                mmain.main()

    def test_non_loopback_with_tokens_serves(self):
        import monet.__main__ as mmain

        os.environ["PAINT_MONET_TOKENS"] = "wtok:write:microscope-x"
        with mock.patch.object(
            sys, "argv", ["monet", "serve", "--host", "0.0.0.0"]
        ):
            with mock.patch("uvicorn.run") as run:
                mmain.main()
                run.assert_called_once()

    def test_loopback_needs_no_token(self):
        import monet.__main__ as mmain

        os.environ.pop("PAINT_MONET_TOKENS", None)
        with mock.patch.object(
            sys, "argv", ["monet", "serve", "--host", "127.0.0.1"]
        ):
            with mock.patch("uvicorn.run") as run:
                mmain.main()
                run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
