"""
monet/tests/test_control.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Test the control module of monet.

:authors: Heinrich Grabmayr, 2022
:copyright: Copyright (c) 2022 Jungmann Lab, MPI of Biochemistry
"""

import os
import unittest
from datetime import datetime
from unittest import mock

import numpy as np
import pandas as pd

import monet.control as mco
from monet import DATABASE_INDEXLEVELS, normalize_powermeter_type


class _TrackingAttenuator:
    """Minimal attenuator that records the last set position. The stock
    TestAttenuator is a no-op whose curr_pos() is always 0, which is not
    enough to exercise the attenuator-adjusting feedback loop."""

    def __init__(self, start=0.0):
        self._pos = start

    def set(self, val):
        self._pos = val

    def curr_pos(self):
        return self._pos

    def home(self):
        self._pos = 0.0


class _LaserPowerMeter:
    """Fake powermeter whose reading tracks the laser power set-point.
    Models 'fixed_attenuator' mode, where only the laser power changes."""

    unit = "mW"

    def __init__(self, instrument, laser, gain=1.0):
        self._inst = instrument
        self._laser = laser
        self._gain = gain

    def read(self, averaging=10):
        return self._inst.lasers[self._laser].power * self._gain


class _AttenuatorCurvePowerMeter:
    """Fake powermeter whose reading follows the attenuator calibration
    curve. Models 'fixed_laser' mode, where only the attenuator moves.
    `miscal` is a steady multiplicative offset so the PI loop has something
    to correct."""

    unit = "mW"

    def __init__(self, instrument, miscal=1.0):
        self._inst = instrument
        self._miscal = miscal

    def read(self, averaging=10):
        att = self._inst.attenuator.curr_pos()
        return self._inst.analyzer.estimate_power(att) * self._miscal


class TestControl(unittest.TestCase):

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_02_IlluminationLaserControl(self):
        # TestData/ is git-ignored, so create the dirs we write into (a fresh
        # clone won't have them).
        os.makedirs("monet/tests/TestData/calibrate", exist_ok=True)
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
            data={
                "bkg": [0] * 4,
                "amp": [50, 100, 40, 80],
                "phi": [30, 30, 25, 25],
            },
        )
        db_path = "monet/tests/TestData/control/power_database.xlsx"
        db.to_excel(db_path)

        config = {
            "database": db_path,
            "dest_calibration_plot": "monet/tests/TestData/control/",
            "index": {
                "name": "DefaultMicroscope",
            },
            "powermeter": {
                "classpath": "monet.powermeter.TestPowerMeter",
                "init_kwargs": {
                    "bkg": 0,
                    "amp": 50,
                    "phi": 30,
                    "start": 10,
                    "step": 5,
                    "noise": 3,
                },
            },
            "attenuation": {
                "classpath": "monet.attenuation.TestAttenuator",
                "init_kwargs": {
                    "bkg": 0,
                    "amp": 50,
                    "phi": 30,
                    "start": 10,
                    "step": 5,
                },
            },
            "analysis": {
                "classpath": "monet.analysis.SinusAttenuationCurveAnalyzer",
                "init_kwargs": {
                    "min": 30,
                    "max": 100,
                    "step": 5,
                },
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
                "640": {
                    "classpath": "monet.laser.TestLaser",
                    "init_kwargs": {"port": "COM8"},
                },
            },
        }
        ctrl = mco.IlluminationLaserControl(config)

        print(ctrl.laser)

        ctrl.laser = "561"

        ctrl.power = 50
        ctrl.power = 200
        print(ctrl.laserpower)

        assert True

    def _build_laser_control(self):
        """Build an IlluminationLaserControl with a linear calibration for
        lasers 488 and 561 at laser powers 50 and 100 mW. The attenuator is
        swapped for one that tracks its position so the feedback loop can be
        exercised."""
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
        db_path = "monet/tests/TestData/control/feedback_test_db.xlsx"
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
        ctrl = mco.IlluminationLaserControl(config)
        # The stock TestAttenuator is a no-op; swap in one that tracks state.
        ctrl.attenuator = _TrackingAttenuator(start=30)
        ctrl.laser = 488
        ctrl.laserpower = 50
        return ctrl

    def test_load_calibration_database_does_not_enable_lasers(self):
        """Reloading the calibration (e.g. on GUI connect) must not switch a
        laser on, even with auto_enable_lasers left at its default."""
        ctrl = self._build_laser_control()
        # start from all-off, as at a fresh connection
        for las in ctrl.lasers.values():
            las.enabled = False
        self.assertTrue(ctrl.auto_enable_lasers)  # default preserved
        ctrl.load_calibration_database()
        self.assertFalse(
            any(las.enabled for las in ctrl.lasers.values()),
            "load_calibration_database must not enable any laser",
        )

    @mock.patch("time.sleep")
    def test_run_power_feedback_fixed_attenuator(self, _sleep):
        """Feedback in fixed_attenuator mode scales the laser power until the
        measured output power reaches the target."""
        ctrl = self._build_laser_control()
        pm = _LaserPowerMeter(ctrl, laser=488, gain=1.0)
        progress = []
        result = mco.run_power_feedback(
            ctrl,
            pm,
            target_pwr=30,
            laser=488,
            mode="fixed_attenuator",
            max_dev_pct=1.0,
            max_iter=20,
            progress_callback=lambda i, s, m: progress.append((i, s, m)),
        )

        self.assertTrue(result["converged"])
        self.assertLessEqual(abs(result["measured"] - 30) / 30 * 100.0, 1.0)
        self.assertGreaterEqual(len(progress), 2)
        for key in (
            "measured",
            "converged",
            "cali_pred",
            "out_of_range",
            "att_pos",
            "laser_pwr",
            "iterations",
        ):
            self.assertIn(key, result)

    @mock.patch("time.sleep")
    def test_run_power_feedback_fixed_laser(self, _sleep):
        """Feedback in fixed_laser mode runs the PI controller on the
        attenuator until the measured output power reaches the target."""
        ctrl = self._build_laser_control()
        pm = _AttenuatorCurvePowerMeter(ctrl, miscal=1.1)
        progress = []
        result = mco.run_power_feedback(
            ctrl,
            pm,
            target_pwr=30,
            laser=488,
            mode="fixed_laser",
            max_dev_pct=2.0,
            max_iter=20,
            progress_callback=lambda i, s, m: progress.append((i, s, m)),
        )

        self.assertTrue(result["converged"])
        self.assertLessEqual(abs(result["measured"] - 30) / 30 * 100.0, 2.0)
        self.assertGreaterEqual(len(progress), 2)

    @mock.patch("time.sleep")
    def test_run_power_feedback_rejects_combined(self, _sleep):
        """Feedback is not defined for the combined mode."""
        ctrl = self._build_laser_control()
        pm = _LaserPowerMeter(ctrl, laser=488)
        with self.assertRaises(ValueError):
            mco.run_power_feedback(
                ctrl, pm, target_pwr=30, laser=488, mode="combined"
            )

    def test_accessible_power_range(self):
        """accessible_power_range returns a sane (lo, hi) for every mode."""
        ctrl = self._build_laser_control()
        for mode in ("combined", "fixed_laser", "fixed_attenuator"):
            lo, hi = ctrl.accessible_power_range(mode, 488)
            self.assertLessEqual(lo, hi)
            self.assertTrue(np.isfinite(lo) and np.isfinite(hi))

    def test_to_sample_plane(self):
        """to_sample_plane applies the objective transmission factor based on
        where the meter is *physically* positioned (powermeter_position), not
        on the stored calibration's powermeter type."""
        ctrl = self._build_laser_control()
        ctrl._factors[488] = 2.5
        # Meter in the BFP → raw reading projected by the transmission factor.
        ctrl.powermeter_position = "bfp"
        self.assertAlmostEqual(ctrl.to_sample_plane(10.0, 488), 25.0)
        # Meter in the sample plane → reading used as-is, even with a factor.
        ctrl.powermeter_position = "sample"
        self.assertEqual(ctrl.to_sample_plane(10.0, 488), 10.0)
        # BFP but no factor for this laser → unchanged (uncorrected).
        ctrl.powermeter_position = "bfp"
        self.assertEqual(ctrl.to_sample_plane(10.0, 561), 10.0)

    def test_to_sample_plane_legacy_values(self):
        """Legacy powermeter-position values 'beampath'/'manual' are honoured
        by to_sample_plane via normalize_powermeter_type."""
        ctrl = self._build_laser_control()
        ctrl._factors[488] = 2.0
        ctrl.powermeter_position = "beampath"  # legacy → bfp
        self.assertAlmostEqual(ctrl.to_sample_plane(10.0, 488), 20.0)
        ctrl.powermeter_position = "manual"  # legacy → sample
        self.assertEqual(ctrl.to_sample_plane(10.0, 488), 10.0)

    def test_powermeter_position_defaults_to_bfp(self):
        """A fresh control defaults to the BFP powermeter position."""
        ctrl = self._build_laser_control()
        self.assertEqual(
            normalize_powermeter_type(ctrl.powermeter_position), "bfp"
        )

    def test_measurement_factor_decoupled_from_calibration(self):
        """The live-measurement factor follows powermeter_position, decoupled
        from the stored calibration's powermeter type (which _bfp_factor uses).
        """
        ctrl = self._build_laser_control()
        ctrl._factors[488] = 2.0
        # Calibration was sample-plane, but the meter is now in the BFP.
        ctrl._powermeter_type[488] = "sample"
        ctrl.powermeter_position = "bfp"
        self.assertEqual(ctrl._bfp_factor(488), 1.0)  # calibration-keyed
        self.assertEqual(ctrl._measurement_factor(488), 2.0)  # physical-keyed
        # Calibration was BFP, but the meter is now in the sample plane.
        ctrl._powermeter_type[488] = "bfp"
        ctrl.powermeter_position = "sample"
        self.assertEqual(ctrl._bfp_factor(488), 2.0)
        self.assertEqual(ctrl._measurement_factor(488), 1.0)

    def test_has_transmission_factor(self):
        """_has_transmission_factor reflects whether a factor is loaded."""
        ctrl = self._build_laser_control()
        self.assertFalse(ctrl._has_transmission_factor(488))
        ctrl._factors[488] = 1.7
        self.assertTrue(ctrl._has_transmission_factor(488))

    @mock.patch("time.sleep")
    def test_run_power_feedback_projects_to_sample_plane(self, _sleep):
        """With a BFP transmission factor, the feedback loop drives the
        sample-plane power (raw reading × factor) to the target and reports
        the projected value."""
        ctrl = self._build_laser_control()
        ctrl._powermeter_type[488] = "bfp"
        ctrl._factors[488] = 2.0
        # The meter reads in the back focal plane (raw analyzer curve).
        pm = _AttenuatorCurvePowerMeter(ctrl, miscal=1.0)
        progress = []
        result = mco.run_power_feedback(
            ctrl,
            pm,
            target_pwr=30,
            laser=488,
            mode="fixed_laser",
            max_dev_pct=2.0,
            max_iter=20,
            progress_callback=lambda i, s, m: progress.append((i, s, m)),
        )

        self.assertTrue(result["converged"])
        # Returned measured is sample-plane: raw beampath ≈ 15, ×2 ≈ 30.
        self.assertLessEqual(abs(result["measured"] - 30) / 30 * 100.0, 2.0)
        # The raw meter reading itself is ~target/factor = 15.
        self.assertLessEqual(abs(pm.read() - 15) / 15 * 100.0, 3.0)
        # Progress is reported in the sample plane too.
        self.assertLessEqual(abs(progress[-1][2] - 30) / 30 * 100.0, 2.0)

    # ── laser / laserpower / enabled properties ──────────────────────────

    def test_laser_property_lists_and_selects(self):
        ctrl = self._build_laser_control()
        self.assertEqual(sorted(ctrl.laser), [488, 561])
        ctrl.laser = 561
        self.assertEqual(ctrl.curr_laser, 561)
        # Unknown laser is ignored (prints), current laser unchanged.
        ctrl.laser = 999
        self.assertEqual(ctrl.curr_laser, 561)

    def test_laserpower_property(self):
        ctrl = self._build_laser_control()
        ctrl.laserpower = 100
        self.assertEqual(ctrl.curr_laserpower, 100)
        self.assertEqual(ctrl.laserpower, 100)

    def test_laser_enabled_property(self):
        ctrl = self._build_laser_control()
        ctrl.laser_enabled = True
        self.assertTrue(ctrl.laser_enabled)
        ctrl.laser_enabled = False
        self.assertFalse(ctrl.laser_enabled)

    # ── power getter / setter (combined mode) ────────────────────────────

    def test_power_getter(self):
        ctrl = self._build_laser_control()
        # att_pos=30, level-50 linear amp=1.0 → 30 mW.
        self.assertAlmostEqual(ctrl.power, 30.0, places=6)

    def test_power_setter_in_range(self):
        ctrl = self._build_laser_control()
        ctrl.power = 50.0  # within level-50 range [0, 100]
        self.assertEqual(ctrl.curr_laserpower, 50)
        self.assertAlmostEqual(ctrl.attenuator.curr_pos(), 50.0, places=6)
        self.assertAlmostEqual(ctrl.power, 50.0, places=6)

    def test_power_setter_out_of_range_switches_level(self):
        ctrl = self._build_laser_control()
        # 150 mW exceeds level-50 max (100) → switch to level 100 (max 200).
        ctrl.power = 150.0
        self.assertEqual(ctrl.curr_laserpower, 100)
        self.assertAlmostEqual(ctrl.power, 150.0, places=6)

    def test_power_setter_not_calibrated_raises(self):
        ctrl = self._build_laser_control()
        ctrl.is_calibrated = False
        with self.assertRaises(ValueError):
            ctrl.power = 50.0

    # ── set_power_fixed_attenuator / fixed_laser / predict ───────────────

    def test_set_power_fixed_attenuator(self):
        ctrl = self._build_laser_control()
        # att=30: (50→30, 100→60) ⇒ output = 0.6·laser_pwr. Target 45 ⇒ 75 mW.
        ctrl.set_power_fixed_attenuator(45.0, laser=488)
        self.assertAlmostEqual(ctrl.lasers[488].power, 75.0, places=4)

    def test_set_power_fixed_laser(self):
        ctrl = self._build_laser_control()
        ctrl.laserpower = 50
        ctrl.set_power_fixed_laser(40.0, laser=488)
        # level-50 amp=1.0 ⇒ attenuator position = target.
        self.assertAlmostEqual(ctrl.attenuator.curr_pos(), 40.0, places=4)

    def test_predict_power_fixed_attenuator(self):
        ctrl = self._build_laser_control()
        self.assertAlmostEqual(
            ctrl.predict_power_fixed_attenuator(75.0, laser=488),
            45.0,
            places=4,
        )

    def test_fixed_modes_not_calibrated_raise(self):
        ctrl = self._build_laser_control()
        ctrl.is_calibrated = False
        with self.assertRaises(ValueError):
            ctrl.set_power_fixed_attenuator(45.0, laser=488)
        with self.assertRaises(ValueError):
            ctrl.set_power_fixed_laser(40.0, laser=488)
        with self.assertRaises(ValueError):
            ctrl.predict_power_fixed_attenuator(75.0, laser=488)

    # ── C34 per-laser safety ceiling (enforced below the actuation layer) ──

    def test_parse_safety_envelope_valid(self):
        parsed = mco.IlluminationLaserControl._parse_safety_envelope(
            {"safety": {"max_power_mw": {488: 40, "561": 80.5}}}
        )
        self.assertEqual(parsed, {488: 40.0, 561: 80.5})

    def test_parse_safety_envelope_absent_is_empty(self):
        self.assertEqual(
            mco.IlluminationLaserControl._parse_safety_envelope({}), {}
        )
        self.assertEqual(
            mco.IlluminationLaserControl._parse_safety_envelope(
                {"safety": {}}
            ),
            {},
        )

    def test_parse_safety_envelope_malformed_fails_closed(self):
        """A broken ceiling config must refuse rather than silently disable."""
        for bad in (
            {"safety": {"max_power_mw": {488: "abc"}}},  # non-numeric value
            {"safety": {"max_power_mw": {488: 0}}},  # non-positive
            {"safety": {"max_power_mw": {488: -5}}},  # negative
            {"safety": {"max_power_mw": {488: float("inf")}}},  # non-finite
            {"safety": {"max_power_mw": {"x": 40}}},  # non-numeric key
            {"safety": {"max_power_mw": [488, 40]}},  # not a mapping
        ):
            with self.assertRaises(ValueError):
                mco.IlluminationLaserControl._parse_safety_envelope(bad)

    def test_clamp_to_max_power_reports_flag(self):
        ctrl = self._build_laser_control()
        ctrl._max_power = {488: 25.0}
        self.assertEqual(ctrl.clamp_to_max_power(10.0, 488), (10.0, False))
        self.assertEqual(ctrl.clamp_to_max_power(80.0, 488), (25.0, True))
        # a laser with no configured ceiling is never clamped
        self.assertEqual(ctrl.clamp_to_max_power(80.0, 561), (80.0, False))

    def test_power_setter_clamps_to_safety_ceiling(self):
        """The combined-mode setter (used by the GUI and CLI) is bounded by the
        ceiling — proving enforcement is below the actuation layer, not only in
        the HTTP route."""
        ctrl = self._build_laser_control()
        ctrl._max_power = {488: 25.0}
        ctrl.laser = 488
        ctrl.power = 80.0  # request well above the ceiling
        self.assertLessEqual(ctrl.power, 25.0 + 1e-6)

    def test_set_power_fixed_laser_respects_ceiling(self):
        ctrl = self._build_laser_control()
        ctrl._max_power = {488: 20.0}
        ctrl.laserpower = 50
        ctrl.set_power_fixed_laser(40.0, laser=488)  # clamp 40 -> 20
        # level-50 amp=1.0 ⇒ attenuator position == delivered power == 20
        self.assertAlmostEqual(ctrl.attenuator.curr_pos(), 20.0, places=4)

    def test_set_power_fixed_attenuator_respects_ceiling(self):
        ctrl = self._build_laser_control()
        ctrl._max_power = {488: 30.0}
        ctrl.set_power_fixed_attenuator(45.0, laser=488)  # clamp 45 -> 30
        # att=30: output = 0.6·laser_pwr ⇒ 30 mW needs laser power 50 mW
        self.assertAlmostEqual(ctrl.lasers[488].power, 50.0, places=4)

    @mock.patch("time.sleep")
    def test_run_power_feedback_respects_ceiling(self, _sleep):
        ctrl = self._build_laser_control()
        ctrl._max_power = {488: 20.0}
        pm = _AttenuatorCurvePowerMeter(ctrl, miscal=1.0)
        result = mco.run_power_feedback(
            ctrl,
            pm,
            target_pwr=50,  # asks for 50, ceiling is 20
            laser=488,
            mode="fixed_laser",
            max_dev_pct=2.0,
            max_iter=20,
        )
        # the loop drives to the clamped target, never toward 50
        self.assertLessEqual(result["measured"], 20.0 * 1.05)

    # ── BFP-calibrated ranges (sample plane vs power-meter units) ─────────

    def _build_bfp_control(self, factor=0.5):
        """IlluminationLaserControl with a sinusoidal calibration (as on real
        hardware) taken with the BFP powermeter: the calibrated model is in
        BFP units, everything the user sets is in the sample plane.

        Laser 488: max output 50 mW (BFP) at laser power 50, 100 mW at 100.
        Laser 561: 40 / 80 mW. With factor=0.5 the sample-plane maxima are
        half of that.
        """
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
            data={
                "bkg": [0] * 4,
                "amp": [50, 100, 40, 80],
                "phi": [30] * 4,
            },
        )
        db_path = "monet/tests/TestData/control/bfp_range_test_db.xlsx"
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
                "classpath": "monet.analysis.SinusAttenuationCurveAnalyzer",
                "init_kwargs": {"min": 30, "max": 100, "step": 5},
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
        ctrl = mco.IlluminationLaserControl(config)
        ctrl.attenuator = _TrackingAttenuator(start=30)
        ctrl.laser = 488
        ctrl.laserpower = 50
        for laser in (488, 561):
            ctrl._powermeter_type[laser] = "bfp"
            ctrl._factors[laser] = factor
        return ctrl

    def test_accessible_power_range_is_sample_plane(self):
        """The reachable range of a BFP calibration is reported in the sample
        plane, not in (larger) BFP units — otherwise the GUI advertises powers
        that the calibration model cannot deliver."""
        ctrl = self._build_bfp_control(factor=0.5)
        # combined: highest level max 100 mW BFP → 50 mW in the sample.
        _, hi = ctrl.accessible_power_range("combined", 488)
        self.assertAlmostEqual(hi, 50.0, places=4)
        # fixed_laser at level 50: 50 mW BFP → 25 mW in the sample.
        ctrl.laserpower = 50
        _, hi = ctrl.accessible_power_range("fixed_laser", 488)
        self.assertAlmostEqual(hi, 25.0, places=4)

    def test_accessible_power_range_honours_laser_argument(self):
        """The range is reported for the laser asked for, not for whichever
        laser the instrument currently happens to be set to."""
        ctrl = self._build_bfp_control(factor=0.5)
        self.assertEqual(ctrl.curr_laser, 488)
        _, hi_561 = ctrl.accessible_power_range("combined", 561)
        # laser 561 tops out at 80 mW BFP → 40 mW in the sample.
        self.assertAlmostEqual(hi_561, 40.0, places=4)
        # querying another laser must not switch the instrument
        self.assertEqual(ctrl.curr_laser, 488)

    def test_power_setter_bfp_top_of_range(self):
        """Regression: a sample-plane power just below the reported maximum
        must not fail. Previously the target was compared against BFP-unit
        ranges and only then divided by the transmission factor, so the value
        handed to the model exceeded its calibrated maximum and it raised
        'Desired value out of range'."""
        ctrl = self._build_bfp_control(factor=0.5)
        _, hi = ctrl.accessible_power_range("combined", 488)
        ctrl.power = hi * 0.99  # 49.5 mW, comfortably below the reported max
        self.assertAlmostEqual(ctrl.power, hi * 0.99, places=3)
        # the exact boundary must not round out of the model range either
        ctrl.power = hi
        self.assertAlmostEqual(ctrl.power, hi, places=3)

    def test_power_setter_bfp_above_range_clamps(self):
        """Above the reachable maximum, combined mode picks the highest laser
        power level and clamps to its sample-plane maximum."""
        ctrl = self._build_bfp_control(factor=0.5)
        ctrl.power = 60.0  # > sample max 50, but < BFP max 100
        self.assertEqual(ctrl.curr_laserpower, 100)
        self.assertAlmostEqual(ctrl.power, 50.0, places=3)

    def test_set_power_fixed_laser_above_range_raises_clearly(self):
        """fixed_laser mode rejects an unreachable power with a message that
        names the reachable sample-plane range."""
        ctrl = self._build_bfp_control(factor=0.5)
        ctrl.laserpower = 50  # sample-plane range [0, 25] mW
        with self.assertRaises(ValueError) as cm:
            ctrl.set_power_fixed_laser(30.0, laser=488)
        self.assertIn("25.000", str(cm.exception))
        # within range it still works
        ctrl.set_power_fixed_laser(20.0, laser=488)
        self.assertAlmostEqual(ctrl.power, 20.0, places=3)

    # ── accessible_power_range error paths ───────────────────────────────

    def test_accessible_power_range_unknown_mode_raises(self):
        ctrl = self._build_laser_control()
        with self.assertRaises(ValueError):
            ctrl.accessible_power_range("nonsense", 488)

    def test_accessible_power_range_not_calibrated_raises(self):
        ctrl = self._build_laser_control()
        ctrl.is_calibrated = False
        with self.assertRaises(ValueError):
            ctrl.accessible_power_range("combined", 488)

    # ── hardware-state persistence ───────────────────────────────────────

    def test_record_and_restore_state(self):
        """record_state persists the live laser power / attenuator position;
        saved_state reads it back per laser line."""
        import tempfile
        from pathlib import Path

        import monet.hwstate as hwstate

        ctrl = self._build_laser_control()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                hwstate, "STATE_FILE", Path(tmp) / "state.json"
            ):
                ctrl.laser = 488
                ctrl.laserpower = 50
                ctrl.attenuator.set(42.0)
                ctrl.record_state()  # defaults to current laser

                ctrl.laser = 561
                ctrl.lasers[561].power = 80
                ctrl.attenuator.set(17.5)
                ctrl.record_state(561)

                s488 = ctrl.saved_state(488)
                s561 = ctrl.saved_state(561)
                self.assertEqual(s488["laser_power"], 50.0)
                self.assertEqual(s488["attenuator"], 42.0)
                self.assertEqual(s561["laser_power"], 80.0)
                self.assertEqual(s561["attenuator"], 17.5)
                # A laser line never recorded has no saved state.
                self.assertIsNone(ctrl.saved_state(640))

    # ── construction edge case ───────────────────────────────────────────

    def test_no_lasers_loadable_raises_runtimeerror(self):
        config = {
            "database": "unused.xlsx",
            "index": {"name": "DefaultMicroscope"},
            "attenuation": {
                "classpath": "monet.attenuation.TestAttenuator",
                "init_kwargs": {},
            },
            "analysis": {
                "classpath": "monet.analysis.LinearCurveAnalyzer",
                "init_kwargs": {"min": 0, "max": 100},
            },
            # A numeric-string key ('488') that fails to load used to trip a
            # KeyError in the cleanup loop; the missing-laser cleanup must now
            # pop the original key and reach the graceful RuntimeError.
            "lasers": {
                "488": {
                    "classpath": "monet.laser.NoSuchLaser",
                    "init_kwargs": {},
                },
            },
        }
        with self.assertRaises(RuntimeError):
            mco.IlluminationLaserControl(config, do_load_cal=False)
