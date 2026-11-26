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


class _BacklashAttenuator(_TrackingAttenuator):
    """Attenuator whose *effective* angle lags the commanded one by
    ``backlash`` in the direction of the last move — a rotation-mount with
    hysteresis. ``curr_pos()`` still reports the commanded angle (as real
    encoders do after a move), so only a power reading reveals the backlash."""

    def __init__(self, start=0.0, backlash=1.0):
        super().__init__(start)
        self._backlash = backlash
        self._last_dir = 0

    def set(self, val):
        self._last_dir = 1 if val >= self._pos else -1
        super().set(val)

    def effective_pos(self):
        return self._pos - self._backlash * self._last_dir


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

    def _minimal_control_config(self):
        """A bare IlluminationControl config (no lasers, no DB access)."""
        return {
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
        }

    def test_auto_home_homes_attenuator_on_startup(self):
        """By default the attenuator is homed once at construction."""
        with mock.patch("monet.attenuation.TestAttenuator.home") as mock_home:
            ctrl = mco.IlluminationControl(
                self._minimal_control_config(), do_load_cal=False
            )
        self.assertTrue(ctrl.auto_home)  # default preserved
        mock_home.assert_called_once_with()

    def test_auto_home_can_be_disabled(self):
        """auto_home=False leaves the attenuator untouched at construction."""
        with mock.patch("monet.attenuation.TestAttenuator.home") as mock_home:
            ctrl = mco.IlluminationControl(
                self._minimal_control_config(),
                do_load_cal=False,
                auto_home=False,
            )
        self.assertFalse(ctrl.auto_home)
        mock_home.assert_not_called()

    def test_auto_home_failure_does_not_block_construction(self):
        """A home() that raises must not prevent the control from building."""
        with mock.patch(
            "monet.attenuation.TestAttenuator.home",
            side_effect=RuntimeError("mount not responding"),
        ):
            ctrl = mco.IlluminationControl(
                self._minimal_control_config(), do_load_cal=False
            )
        self.assertIsNotNone(ctrl.attenuator)

    def test_hysteresis_probe_ideal_attenuator_zero_spread(self):
        """A backlash-free attenuator: both approaches agree, angle restored."""
        ctrl = mco.IlluminationControl(
            self._minimal_control_config(), do_load_cal=False, auto_home=False
        )
        ctrl.attenuator = _TrackingAttenuator(start=50.0)
        # power is a deterministic function of the reported angle
        res = ctrl.attenuator_hysteresis_probe(
            read_power=lambda: 2.0 * ctrl.attenuator.curr_pos(),
            delta=5.0,
            settle=0,
        )
        self.assertAlmostEqual(res["power_spread_frac"], 0.0, places=9)
        self.assertEqual(res["target"], 50.0)
        # left where we found it
        self.assertEqual(ctrl.attenuator.curr_pos(), 50.0)
        # both approaches stayed inside [0, 100]
        self.assertGreaterEqual(res["approach_below"], 0)
        self.assertLessEqual(res["approach_above"], 100)

    def test_hysteresis_probe_detects_backlash(self):
        """A mount with hysteresis yields a non-zero power spread."""
        ctrl = mco.IlluminationControl(
            self._minimal_control_config(), do_load_cal=False, auto_home=False
        )
        att = _BacklashAttenuator(start=50.0, backlash=1.0)
        ctrl.attenuator = att
        res = ctrl.attenuator_hysteresis_probe(
            read_power=lambda: 2.0 * att.effective_pos(),
            delta=5.0,
            settle=0,
        )
        # from below: last move up -> effective = 50-1=49 -> 98
        # from above: last move down -> effective = 50+1=51 -> 102
        self.assertAlmostEqual(res["power_from_below"], 98.0, places=6)
        self.assertAlmostEqual(res["power_from_above"], 102.0, places=6)
        self.assertGreater(res["power_spread_frac"], 0.03)
        self.assertEqual(ctrl.attenuator.curr_pos(), 50.0)

    def test_hysteresis_probe_clamps_to_range(self):
        """Approaches never leave the calibrated angle range."""
        ctrl = mco.IlluminationControl(
            self._minimal_control_config(), do_load_cal=False, auto_home=False
        )
        ctrl.attenuator = _TrackingAttenuator(start=98.0)  # near max=100
        res = ctrl.attenuator_hysteresis_probe(
            read_power=lambda: 1.0, delta=5.0, settle=0
        )
        self.assertLessEqual(res["approach_above"], 100.0)
        self.assertGreaterEqual(res["approach_below"], 0.0)

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

    def test_reload_factors_picks_up_factor_stored_after_load(self):
        """Regression: a transmission factor computed *after* the calibration
        was loaded must become usable without a reconnect.

        This is the "set 40 mW sample, get ~32" bug: the operator calibrates
        (so the control's ``_factors`` cache is empty), then pairs a BFP and a
        sample run to get the factor. Until ``reload_factors`` existed, the
        running control kept the stale empty cache, so ``_bfp_factor`` returned
        1.0 and the sample-plane set-point was served at the BFP power —
        under-delivering by the transmission factor. After ``reload_factors``
        the factor is applied and the BFP target becomes sample / T.
        """
        import monet.io as mio

        ctrl = self._build_laser_control()
        ctrl.laser = 488
        ctrl.laserpower = 50
        # The calibration was taken in the back focal plane.
        ctrl._powermeter_type[488] = "bfp"

        # Fresh calibration → no factor loaded yet: the stale state that made
        # the set-point under-deliver.
        self.assertEqual(ctrl._factors, {})
        self.assertEqual(ctrl._bfp_factor(488), 1.0)

        def _bfp_target(sample_power):
            """Drive to a sample-plane power and read back the BFP power the
            hardware was actually commanded to (raw calibration curve)."""
            ctrl.power = sample_power
            return (
                ctrl.analyzer.estimate_power(ctrl.attenuator.curr_pos())
                * ctrl._pin_factor()
            )

        # Pick a sample target whose BFP-equivalent sits inside the calibrated
        # range for both factor values (T < 1 ⇒ sample/T is the larger one).
        lo, hi = sorted(
            float(v) for v in ctrl._power_ranges.loc[50, ["min", "max"]]
        )
        transmission = 0.8
        sample_target = transmission * (lo + hi) / 2.0

        # Before the factor is loaded, the setter wrongly targets BFP == the
        # requested sample power (no projection).
        raw_before = _bfp_target(sample_target)
        self.assertAlmostEqual(raw_before, sample_target, delta=0.5)

        # Store the factor in the DB *after* the control was built, then reload.
        mio.save_transmission_factor(
            ctrl.config["database"],
            "DefaultMicroscope",
            488,
            datetime.now().strftime("%Y-%m-%d"),
            transmission,
            0.0,
            5,
        )
        ctrl.reload_factors()

        self.assertAlmostEqual(ctrl._factors[488], transmission)
        self.assertAlmostEqual(ctrl._bfp_factor(488), transmission)

        # Now the same sample request drives BFP = sample / T (the larger
        # value), so the sample plane actually receives the requested power.
        raw_after = _bfp_target(sample_target)
        self.assertAlmostEqual(
            raw_after, sample_target / transmission, delta=0.5
        )
        self.assertAlmostEqual(raw_after / raw_before, 1.0 / transmission, 2)

    def test_reload_factors_ignores_nonphysical_stored_factor(self):
        """A non-physical factor persisted by an older monet (before the
        write-side gate) must not be *applied* on load: it would drive
        newpwr / factor to the rails. It is dropped → no correction (1.0)."""
        import monet.io as mio

        ctrl = self._build_laser_control()
        # Simulate a garbage factor already in the DB (e.g. a pre-fix dark-plane
        # value well above the physical cap of 1).
        mio.save_transmission_factor(
            ctrl.config["database"],
            "DefaultMicroscope",
            488,
            datetime.now().strftime("%Y-%m-%d"),
            1500.0,
            0.0,
            5,
        )
        ctrl.reload_factors()
        # Not applied → absent from the cache, and _bfp_factor falls back to 1.0.
        self.assertNotIn(488, ctrl._factors)
        self.assertEqual(ctrl._bfp_factor(488), 1.0)

    def test_all_laser_keys_includes_configured_not_loaded(self):
        """Factors are loaded for every *configured* wavelength, not only the
        lasers whose hardware loaded — so the Doctor tab, the manual-factor
        dialog and auto-compute can address a laser that failed to open its COM
        port (it is popped from ``config['lasers']`` but preserved as
        configured)."""
        # Build the calibration DB that _build_laser_control writes, then
        # construct a control whose 640 driver fails to load.
        self._build_laser_control()
        db_path = "monet/tests/TestData/control/feedback_test_db.xlsx"
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
                # Configured but its driver class does not exist → the load
                # raises, so 640 is popped from config['lasers'] yet must stay
                # in the preserved configured set.
                "640": {
                    "classpath": "monet.laser.NoSuchLaser",
                    "init_kwargs": {},
                },
            },
        }
        ctrl = mco.IlluminationLaserControl(config)
        self.assertNotIn(640, ctrl.lasers)  # driver failed to load
        keys = ctrl._all_laser_keys()
        self.assertIn(488, keys)
        self.assertIn(561, keys)
        self.assertIn(640, keys)

    def test_degraded_mode_constructs_when_no_lasers_load(self):
        """When EVERY laser's hardware fails to open (all COM ports down), the
        control must still construct in degraded mode rather than raising, so
        the Database/Doctor tabs and the transmission-factor tools (all
        DB-based) remain usable with the hardware absent. Power-setting is the
        only thing disabled."""
        self._build_laser_control()  # writes the calibration DB
        db_path = "monet/tests/TestData/control/feedback_test_db.xlsx"
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
                # Every driver class is missing → every laser fails to load.
                "488": {
                    "classpath": "monet.laser.NoSuchLaser",
                    "init_kwargs": {},
                },
                "561": {
                    "classpath": "monet.laser.NoSuchLaser",
                    "init_kwargs": {},
                },
                "640": {
                    "classpath": "monet.laser.NoSuchLaser",
                    "init_kwargs": {},
                },
            },
        }
        # Must NOT raise despite zero lasers loading.
        ctrl = mco.IlluminationLaserControl(config)
        self.assertEqual(ctrl.lasers, {})
        self.assertIsNone(ctrl.curr_laser)
        # All configured wavelengths are still addressable for factor work.
        self.assertEqual(ctrl._all_laser_keys(), {488, 561, 640})
        # Factor application is safe (no hardware needed) and defaults to 1.0.
        self.assertEqual(ctrl._bfp_factor(488), 1.0)

    def test_reload_factors_recovers_from_local_pair_store(self):
        """A factor present only in the local pair store (projection store
        empty) is recovered on reload.

        Mirrors upgrading from a monet that dropped a failed POST /factors: the
        projection store has nothing, but the local pair store still holds the
        operator-computed factor, so the calibration stays usable without a
        recompute.
        """
        import monet.io as mio

        ctrl = self._build_laser_control()
        ctrl.laser = 488
        ctrl.laserpower = 50
        ctrl._powermeter_type[488] = "bfp"

        pair = {
            "device": "DefaultMicroscope",
            "wavelength": 488,
            "laser_power": 50,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "sample_time": "10:00",
            "bfp_time": "10:05",
            "factor": 0.8,
            "n_points": 5,
        }
        db = ctrl.config["database"]
        pair_path = mio._factor_pairs_path(db)
        try:
            # No projection factor stored → only the local pair exists.
            mio.save_factor_pair(db, pair)
            ctrl.reload_factors()
            self.assertAlmostEqual(ctrl._factors[488], 0.8)
            self.assertAlmostEqual(ctrl._bfp_factor(488), 0.8)
        finally:
            if os.path.exists(pair_path):
                os.remove(pair_path)

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

    def test_laserpower_setter_uncalibrated_power_goes_uncalibrated(self):
        """Setting a laser power with no stored calibration must not raise.

        Regression: a calibration run setting a new power level (e.g. 10 mW
        added to the protocol) crashed with a bare ``KeyError: 10`` because
        the setter indexed ``_analyzers`` while the instrument still counted
        as calibrated from the old database. It now degrades to uncalibrated
        so the run can proceed to calibrate that power, and restores as soon
        as a calibrated power is selected again (no laser re-select needed).
        """
        ctrl = self._build_laser_control()
        self.assertTrue(ctrl.is_calibrated)
        ctrl.laserpower = 10  # only 50 and 100 mW are calibrated
        self.assertEqual(ctrl.curr_laserpower, 10)
        self.assertEqual(ctrl.laserpower, 10)
        self.assertFalse(ctrl.is_calibrated)
        # calibrated power setting is locked out while uncalibrated ...
        with self.assertRaises(ValueError):
            ctrl.power = 50.0
        # ... and selecting a calibrated power restores symmetrically.
        ctrl.laserpower = 100
        self.assertTrue(ctrl.is_calibrated)
        self.assertIs(ctrl.analyzer, ctrl._analyzers[100])

    def test_laserpower_setter_fallback_does_not_alias_analyzer(self):
        """The uncalibrated fallback must install a fresh analyzer.

        Regression: the fallback left ``self.analyzer`` aliased to the
        previously selected power's entry in ``_analyzers``; a subsequent 1D
        calibration refit that shared object in place, silently overwriting
        the stored calibration of a different power.
        """
        ctrl = self._build_laser_control()
        ctrl.laserpower = 100
        stored = dict(ctrl._analyzers)
        ctrl.laserpower = 10  # uncalibrated -> fallback
        for analyzer in stored.values():
            self.assertIsNot(ctrl.analyzer, analyzer)
        # fitting the fallback analyzer must leave the stored ones intact
        self.assertEqual(ctrl._analyzers, stored)

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

    def test_power_setter_prefers_lowest_adequate_level(self):
        # 488 levels 50 (max 100) and 100 (max 200). After a high set that put
        # us on level 100, a subsequent low set must drop back to level 50
        # (served near its top), not stay on level 100's trough.
        ctrl = self._build_laser_control()
        ctrl.power = 150.0  # → level 100
        self.assertEqual(ctrl.curr_laserpower, 100)
        ctrl.power = (
            30.0  # reachable on level 50 (max 100) → should switch down
        )
        self.assertEqual(ctrl.curr_laserpower, 50)
        self.assertAlmostEqual(ctrl.power, 30.0, places=6)

    def test_power_setter_leaves_headroom_for_feedback(self):
        # 488: level 50 (max 100), level 100 (max 200). 90 mW is reachable on
        # level 50 but only in its top 10% — too close to the attenuator
        # ceiling for a later fixed-laser feedback step to raise it. Combined
        # mode must prefer level 100, leaving >=15% headroom.
        ctrl = self._build_laser_control()
        ctrl.power = 90.0
        self.assertEqual(ctrl.curr_laserpower, 100)
        self.assertAlmostEqual(ctrl.power, 90.0, places=6)
        # the operating point sits at <=85% of this level's reachable max
        _, hi = ctrl.accessible_power_range("fixed_laser", 488)
        self.assertLessEqual(90.0, 0.85 * hi + 1e-9)

    # ── pin_calibration (one-point rescale) ──────────────────────────────

    def test_pin_calibration_rescales_get_and_set(self):
        ctrl = self._build_laser_control()
        # simulate the laser running 10% hotter than the stored calibration
        gain = 1.1

        def read_power():
            pos = ctrl.attenuator.curr_pos()
            return gain * ctrl.analyzer.estimate_power(pos)

        res = ctrl.pin_calibration(read_power, settle=0.0)
        self.assertAlmostEqual(res["factor"], gain, places=6)
        self.assertTrue(res["shape_ok"])
        self.assertAlmostEqual(ctrl._pin_factor(), gain, places=6)
        # getter now reports the pinned (true) power
        ctrl.attenuator.set(40.0)
        self.assertAlmostEqual(ctrl.power, gain * 40.0, places=6)
        # setter hits the true target: to get 55 mW the model is inverted at
        # 55/1.1 = 50 → angle 50
        ctrl.power = 55.0
        self.assertAlmostEqual(ctrl.attenuator.curr_pos(), 50.0, places=6)
        self.assertAlmostEqual(ctrl.power, 55.0, places=6)

    def test_pin_calibration_flags_shape_change(self):
        ctrl = self._build_laser_control()

        # amplitude up 10% at the anchor but a distorted mid-curve → not a
        # pure rescale; shape_ok must be False.
        def read_power():
            pos = ctrl.attenuator.curr_pos()
            base = ctrl.analyzer.estimate_power(pos)
            return 1.1 * base + (20.0 if pos < 80.0 else 0.0)

        res = ctrl.pin_calibration(read_power, settle=0.0)
        self.assertFalse(res["shape_ok"])

    def test_pin_scales_cleared_on_reload(self):
        ctrl = self._build_laser_control()
        ctrl._pin_scales[(ctrl.curr_laser, ctrl.curr_laserpower)] = 1.2
        ctrl.load_calibration_database()
        self.assertEqual(ctrl._pin_scales, {})

    def _pin_gain(self, ctrl, gain):
        ctrl.pin_calibration(
            lambda: gain
            * ctrl.analyzer.estimate_power(ctrl.attenuator.curr_pos()),
            settle=0.0,
        )

    def test_pin_applies_in_fixed_laser_mode(self):
        ctrl = self._build_laser_control()
        ctrl.laser = 488
        ctrl.laserpower = 50  # linear amp=1.0 → power == angle
        self._pin_gain(ctrl, 1.1)
        # request 55 mW: reality = 1.1*model, so model target = 50 → angle 50
        ctrl.set_power_fixed_laser(55.0)
        self.assertAlmostEqual(ctrl.attenuator.curr_pos(), 50.0, places=4)
        self.assertAlmostEqual(ctrl.power, 55.0, places=4)

    def test_pin_applies_in_fixed_attenuator_mode(self):
        ctrl = self._build_laser_control()
        ctrl.laser = 488
        ctrl.laserpower = 50
        ctrl.attenuator.set(40.0)
        # predicted output before pin, then 10% hot
        before = ctrl.predict_power_fixed_attenuator(50.0)
        ctrl._pin_scales[(488, 50)] = 1.1
        ctrl._pin_scales[(488, 100)] = 1.1
        after = ctrl.predict_power_fixed_attenuator(50.0)
        self.assertAlmostEqual(after, before * 1.1, places=4)

    def test_pin_scales_fixed_laser_range(self):
        ctrl = self._build_laser_control()
        ctrl.laser = 488
        ctrl.laserpower = 50
        _, hi0 = ctrl.accessible_power_range("fixed_laser")
        self._pin_gain(ctrl, 1.1)
        _, hi1 = ctrl.accessible_power_range("fixed_laser")
        self.assertAlmostEqual(hi1, hi0 * 1.1, places=3)

    def test_pin_rejects_implausible_reading_and_restores(self):
        ctrl = self._build_laser_control()
        ctrl.attenuator.set(30.0)
        entry = ctrl.attenuator.curr_pos()
        with self.assertRaises(ValueError):
            ctrl.pin_calibration(lambda: 0.0, settle=0.0)  # dark reading
        self.assertEqual(ctrl._pin_scales, {})  # nothing stored
        self.assertAlmostEqual(
            ctrl.attenuator.curr_pos(), entry, places=4
        )  # attenuator restored

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

    def test_no_lasers_loadable_constructs_degraded(self):
        """When no laser loads, construction no longer raises — it enters
        degraded mode (DB/factor inspection works; power-setting is disabled),
        so the operator can still use the Database/Doctor tabs with the hardware
        down. The numeric-string key ('488') that fails to load must still be
        popped from the cleanup loop without a KeyError, and preserved as a
        configured wavelength."""
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
            "lasers": {
                "488": {
                    "classpath": "monet.laser.NoSuchLaser",
                    "init_kwargs": {},
                },
            },
        }
        ctrl = mco.IlluminationLaserControl(config, do_load_cal=False)
        self.assertEqual(ctrl.lasers, {})
        self.assertIsNone(ctrl.curr_laser)
        # The failed numeric-string key is preserved as configured.
        self.assertEqual(ctrl._all_laser_keys(), {488})

    # ── suggest_pi_gains ──────────────────────────────────────────────────
    def test_suggest_pi_gains_slow_lowers_kp_and_raises_iters(self):
        """A slow, non-converged decay suggests a lower Kp and more steps."""
        target = 10.0
        # error decays ~0.85 per step (slow), same sign -> never reaches 1%.
        e = [0.5 * 0.85**n for n in range(8)]
        measured = [target * (1 - x) for x in e]
        s = mco.suggest_pi_gains(
            target,
            measured,
            kp=0.85,
            ki=0.15,
            mode="fixed_laser",
            converged=False,
            max_dev_pct=1.0,
            max_iter=20,
        )
        self.assertEqual(s["diagnosis"], "slow")
        self.assertTrue(s["changed"])
        self.assertLess(s["kp"], 0.85)  # lower Kp => faster decay
        self.assertGreaterEqual(s["max_iter"], 20)
        self.assertGreaterEqual(s["kp"], 0.05)  # stays in bounds

    def test_suggest_pi_gains_oscillating_lowers_kp(self):
        """A growing, sign-flipping error is diagnosed as oscillating."""
        target = 10.0
        e = [0.2 * (-1.25) ** n for n in range(8)]  # |e| grows, alternates
        measured = [target * (1 - x) for x in e]
        s = mco.suggest_pi_gains(
            target,
            measured,
            kp=1.2,
            ki=0.3,
            mode="fixed_laser",
            converged=False,
            max_dev_pct=1.0,
            max_iter=20,
        )
        self.assertEqual(s["diagnosis"], "oscillating")
        self.assertTrue(s["changed"])
        self.assertLess(s["kp"], 1.2)
        # Ki is tuned independently of Kp: damping oscillation by lowering Kp
        # must not drag Ki down with it (regression for the old ki=0.2·kp tie).
        self.assertAlmostEqual(s["ki"], 0.3, places=6)

    def test_suggest_pi_gains_converged_no_change(self):
        """A quick convergence leaves the gains alone."""
        s = mco.suggest_pi_gains(
            10.0,
            [5.0, 9.8, 9.99],
            kp=0.85,
            ki=0.15,
            mode="fixed_laser",
            converged=True,
            max_dev_pct=1.0,
            max_iter=20,
        )
        self.assertEqual(s["diagnosis"], "converged")
        self.assertFalse(s["changed"])
        self.assertEqual(s["kp"], 0.85)

    def test_suggest_pi_gains_out_of_range_keeps_gains(self):
        """Range-limited runs are not a tuning problem; gains unchanged."""
        s = mco.suggest_pi_gains(
            10.0,
            [6.0, 6.1, 6.0],
            kp=0.85,
            ki=0.15,
            mode="fixed_laser",
            converged=False,
            out_of_range=True,
            max_iter=20,
        )
        self.assertEqual(s["diagnosis"], "range_limited")
        self.assertFalse(s["changed"])
        self.assertEqual((s["kp"], s["ki"]), (0.85, 0.15))

    def test_suggest_pi_gains_fixed_attenuator_mode(self):
        """Fixed-attenuator mode uses no PI gains; say so, change nothing."""
        s = mco.suggest_pi_gains(
            10.0,
            [6.0, 7.0, 7.5],
            kp=0.85,
            ki=0.15,
            mode="fixed_attenuator",
            converged=False,
            max_iter=20,
        )
        self.assertEqual(s["diagnosis"], "range_limited")
        self.assertFalse(s["changed"])

    def test_suggest_pi_gains_stalled_raises_ki_not_kp(self):
        """A persistent same-sign offset is an integral deficiency: raise Ki,
        and do NOT lower Kp (nor collapse Ki, the old bug)."""
        target = 10.0
        # Measured plateaus ~6% below target with no sign change — the classic
        # proportional steady-state offset ("set 18.3, settle at ~17.2").
        measured = [9.50, 9.44, 9.41, 9.40, 9.40, 9.40]
        s = mco.suggest_pi_gains(
            target,
            measured,
            kp=0.9,
            ki=0.0,
            mode="fixed_laser",
            converged=False,
            max_dev_pct=1.0,
            max_iter=20,
        )
        self.assertEqual(s["diagnosis"], "stalled")
        self.assertTrue(s["changed"])
        # Ki is raised from 0 so the offset is integrated away...
        self.assertGreater(s["ki"], 0.0)
        # ...and Kp is left alone (lowering it would deepen the offset).
        self.assertAlmostEqual(s["kp"], 0.9, places=6)

    def test_suggest_pi_gains_stalled_raises_existing_ki(self):
        """A stalled run with Ki already set raises it further, never below."""
        measured = [9.50, 9.44, 9.41, 9.40, 9.40, 9.40]
        s = mco.suggest_pi_gains(
            10.0,
            measured,
            kp=0.5,
            ki=0.15,
            mode="fixed_laser",
            converged=False,
            max_dev_pct=1.0,
            max_iter=20,
        )
        self.assertEqual(s["diagnosis"], "stalled")
        self.assertGreater(s["ki"], 0.15)

    @mock.patch("time.sleep")
    def test_autotune_pi_gains_measures_plant_gain(self, _sleep):
        """The active step-test recovers the plant's multiplicative miscal as
        the plant gain and derives independent, non-zero Kp/Ki from it."""
        ctrl = self._build_laser_control()
        pm = _AttenuatorCurvePowerMeter(ctrl, miscal=1.1)
        res = mco.autotune_pi_gains(
            ctrl,
            pm,
            target_pwr=30,
            laser=488,
            mode="fixed_laser",
            kp=0.85,
            ki=0.15,
            probe_frac=0.15,
            max_dev_pct=2.0,
            max_iter=20,
        )
        self.assertEqual(res["diagnosis"], "tuned")
        self.assertAlmostEqual(res["plant_gain"], 1.1, places=2)
        # Kp = _TARGET_DECAY / g = 0.35 / 1.1 ≈ 0.318
        self.assertAlmostEqual(res["kp"], mco._TARGET_DECAY / 1.1, places=2)
        self.assertGreater(res["ki"], 0.0)
        self.assertEqual(len(res["trajectory"]), 3)
        self.assertTrue(res["confirmed"])
        self.assertTrue(res["converged"])

    def test_autotune_pi_gains_rejects_fixed_attenuator(self):
        """Fixed-attenuator mode has no PI gains; auto-tune is a no-op there."""
        ctrl = self._build_laser_control()
        pm = _AttenuatorCurvePowerMeter(ctrl, miscal=1.1)
        res = mco.autotune_pi_gains(
            ctrl, pm, target_pwr=30, laser=488, mode="fixed_attenuator"
        )
        self.assertEqual(res["diagnosis"], "unsupported")
        self.assertFalse(res["changed"])

    @mock.patch("time.sleep")
    def test_autotune_pi_gains_no_light_keeps_gains(self, _sleep):
        """A dark meter leaves the gains untouched and reports no_light."""
        ctrl = self._build_laser_control()

        class _DarkMeter:
            unit = "mW"

            def read(self, averaging=10):
                return 0.0

        res = mco.autotune_pi_gains(
            ctrl,
            _DarkMeter(),
            target_pwr=30,
            laser=488,
            mode="fixed_laser",
            kp=0.7,
            ki=0.2,
        )
        self.assertEqual(res["diagnosis"], "no_light")
        self.assertFalse(res["changed"])
        self.assertEqual((res["kp"], res["ki"]), (0.7, 0.2))
