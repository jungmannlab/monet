"""
monet/tests/test_calibration.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Test the calibration module of monet.

:authors: Heinrich Grabmayr, 2022
:copyright: Copyright (c) 2022 Jungmann Lab, MPI of Biochemistry
"""

import os
import shutil
import unittest

import numpy as np

import monet.calibrate as mca


class TestCalibration(unittest.TestCase):

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_01_Calibrator1D(self):
        try:
            shutil.rmtree("monet/tests/TestData/calibrate")
        except Exception:
            pass
        try:
            os.makedirs("monet/tests/TestData/calibrate", exist_ok=True)
        except Exception:
            pass

        config = {
            "database": "monet/tests/TestData/calibrate/power_database.xlsx",
            "index": {
                "name": "DefaultMicroscope",
                "wavelength [nm]": 488,
                "laser_power [mW]": 100,
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
        }
        pc = mca.CalibrationProtocol1D(config)

        # if not calibrated yet, setting power should yield a Value error
        with self.assertRaises(ValueError) as context:
            pc.instrument.power = 5
        self.assertTrue("No calibration present" in str(context.exception))

        # remove the database to test creating a new one
        try:
            os.remove(config["database"])
        except Exception:
            pass
        pc.calibrate(wait_time=0)
        # test saving into an existing database
        pc.save_calibration()

        pc.instrument.load_calibration()

        # assert False

    def _config_1d(self):
        """A minimal 1D config; control values run 30, 35, ... 100."""
        try:
            os.makedirs("monet/tests/TestData/calibrate", exist_ok=True)
        except Exception:
            pass
        return {
            "database": "monet/tests/TestData/calibrate/power_database.xlsx",
            "index": {
                "name": "DefaultMicroscope",
                "wavelength [nm]": 488,
                "laser_power [mW]": 100,
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
        }

    @staticmethod
    def _nan_on_calls(pc, nan_calls):
        """Make ``pc.powermeter.read`` return NaN on the given 1-based calls.

        Simulates a saturated/over-range meter for specific attenuator steps.
        """
        real_read = pc.powermeter.read
        calls = {"n": 0}

        def flaky_read(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] in nan_calls:
                return np.nan
            return real_read(*args, **kwargs)

        pc.powermeter.read = flaky_read

    def test_01b_powermeter_loads_by_default(self):
        """Default construction opens the meter (available True)."""
        pc = mca.CalibrationProtocol1D(self._config_1d())
        self.assertTrue(pc.powermeter_available)
        self.assertIsNotNone(pc.powermeter)

    def test_01c_powermeter_can_be_deferred(self):
        """load_powermeter=False leaves the meter unprobed until asked.

        This is the path the GUI uses so a slow/absent meter can't block the
        connect: the meter is opened later, off the critical path.
        """
        pc = mca.CalibrationProtocol1D(
            self._config_1d(), load_powermeter=False
        )
        self.assertIsNone(pc.powermeter_available)  # None == not yet probed
        self.assertIsNone(pc.powermeter)
        # Now open it explicitly (what the GUI's background worker does).
        available = pc.load_powermeter()
        self.assertTrue(available)
        self.assertTrue(pc.powermeter_available)
        self.assertIsNotNone(pc.powermeter)

    def test_01d_deferred_powermeter_records_failure(self):
        """A failing deferred load sets available False + an error reason."""
        cfg = self._config_1d()
        cfg["powermeter"]["classpath"] = "monet.powermeter.NoSuchMeter"
        pc = mca.CalibrationProtocol1D(cfg, load_powermeter=False)
        self.assertIsNone(pc.powermeter_available)
        available = pc.load_powermeter()
        self.assertFalse(available)
        self.assertFalse(pc.powermeter_available)
        self.assertIsNotNone(pc.powermeter_error)

    def test_02_Calibrator1D_drops_nonfinite(self):
        """By default a non-finite reading is dropped and the fit proceeds.

        Regression for the over-range meter surfacing lmfit's cryptic
        "model function generated NaN values" abort: the bad point (2nd
        step, control value 35) is dropped and the remaining finite points
        are fit, so the calibration still succeeds.
        """
        pc = mca.CalibrationProtocol1D(self._config_1d())
        self._nan_on_calls(pc, {2})

        ctrl_vals, powers = pc.calibrate(wait_time=0)

        self.assertTrue(np.all(np.isfinite(powers)))
        self.assertNotIn(35.0, ctrl_vals.tolist())
        # one of the 15 control values (30..100 step 5) was dropped
        self.assertEqual(len(ctrl_vals), 14)
        self.assertTrue(pc.instrument.is_calibrated)

    def test_02b_Calibrator1D_nonfinite_raises_when_opted_out(self):
        """drop_nonfinite=False raises a clear, control-value-named error."""
        pc = mca.CalibrationProtocol1D(self._config_1d())
        self._nan_on_calls(pc, {2})

        with self.assertRaises(ValueError) as context:
            pc.calibrate(wait_time=0, drop_nonfinite=False)
        msg = str(context.exception)
        self.assertIn("non-finite", msg)
        self.assertIn("35", msg)  # the offending control value

    def test_02c_Calibrator1D_raises_when_too_few_finite(self):
        """Dropping so many points that too few remain still raises."""
        pc = mca.CalibrationProtocol1D(self._config_1d())
        # 15 steps; leave only the first two finite -> below the fit floor.
        self._nan_on_calls(pc, set(range(3, 16)))

        with self.assertRaises(ValueError) as context:
            pc.calibrate(wait_time=0)
        self.assertIn("too few", str(context.exception))

    def test_03_Calibrator1D_reports_fit_quality(self):
        """calibrate() records an RMS/max relative fit residual."""
        pc = mca.CalibrationProtocol1D(self._config_1d())
        pc.calibrate(wait_time=0)
        q = pc.last_fit_quality
        self.assertIsNotNone(q)
        for key in ("rms_pct", "max_pct", "max_at"):
            self.assertIn(key, q)
        self.assertGreaterEqual(q["rms_pct"], 0.0)
        self.assertGreaterEqual(q["max_pct"], q["rms_pct"])

    def test_03c_calibrate_records_drift_and_logs_history(self):
        """calibrate() records within-sweep drift and appends a history row."""
        import tempfile

        cfg = self._config_1d()
        folder = tempfile.mkdtemp()
        cfg["dest_calibration_plot"] = folder
        pc = mca.CalibrationProtocol1D(cfg)
        pc.calibrate(wait_time=0)

        self.assertIn("drift_pct", pc.last_fit_quality)
        # a durable fit-quality history CSV was written with a data row
        log = os.path.join(folder, "fit_quality_log.csv")
        self.assertTrue(os.path.isfile(log))
        with open(log) as f:
            lines = f.read().strip().splitlines()
        self.assertIn("rms_pct", lines[0])  # header
        self.assertIn("drift_pct", lines[0])
        self.assertEqual(len(lines), 2)  # header + one run
        # a second run appends, not overwrites
        pc.calibrate(wait_time=0)
        with open(log) as f:
            self.assertEqual(len(f.read().strip().splitlines()), 3)

    def test_03d_drift_check_can_be_disabled(self):
        pc = mca.CalibrationProtocol1D(self._config_1d())
        pc.calibrate(wait_time=0, drift_check=False)
        self.assertIsNone(pc.last_drift_pct)

    def test_03e_load_calibration_incompatible_model_no_crash(self):
        """load_calibration on a foreign-model row stays uncalibrated,
        rather than crashing (control.load_calibration guard)."""
        import tempfile

        from monet.util import load_class

        cfg = self._config_1d()
        cfg["database"] = os.path.join(tempfile.mkdtemp(), "db.xlsx")
        pc = mca.CalibrationProtocol1D(cfg)
        pc.calibrate(wait_time=0)  # writes a sinusoidal row
        self.assertTrue(pc.instrument.is_calibrated)

        # switch to the polynomial model and reload the (sinusoidal) row
        poly = {
            "classpath": "monet.analysis.PolynomAttenuationCurveAnalyzer",
            "init_kwargs": {"min": 30, "max": 100, "step": 5, "polydegree": 5},
        }
        pc.instrument.config["analysis"] = poly
        pc.instrument.analyzer = load_class(
            poly["classpath"], poly["init_kwargs"]
        )
        pc.instrument.load_calibration()  # must not raise
        self.assertFalse(pc.instrument.is_calibrated)

    def test_03b_fit_quality_flags_model_mismatch(self):
        """A model that mispredicts the data yields a large relative residual.

        The fitted model is replaced with one whose amplitude is 10% low, so
        _fit_quality reports a residual near 10% (the calibrate-vs-measure
        signature of a poor fit rather than backlash/drift).
        """
        import numpy as np

        pc = mca.CalibrationProtocol1D(self._config_1d())
        pc.calibrate(wait_time=0)
        ana = pc.instrument.analyzer
        pars = dict(ana.get_model())
        x = np.arange(30.0, 101.0, 5.0)
        y_true = np.asarray(ana.estimate_power(x), dtype=float)
        pars["amp"] = pars["amp"] * 0.9  # model now under-predicts by ~10%
        ana.load_model(pars)
        q = pc._fit_quality(x, y_true)
        self.assertIsNotNone(q)
        # under-predicting the amplitude by 10% shows up as a sizeable residual
        self.assertGreater(q["max_pct"], 5.0)

    def test_04_verify_calibration_1d_structure(self):
        """verify_calibration re-measures angles and returns per-point data."""
        pc = mca.CalibrationProtocol1D(self._config_1d())
        pc.calibrate(wait_time=0)
        res = pc.verify_calibration(n_angles=4, wait_time=0)
        self.assertEqual(len(res["points"]), 4)
        for p in res["points"]:
            for k in (
                "laser_power",
                "angle",
                "measured",
                "predicted",
                "dev_pct",
            ):
                self.assertIn(k, p)
            self.assertIsNone(p["laser_power"])  # 1D: single calibration
        self.assertIn("rms_pct", res)
        self.assertIn("max_pct", res)

    def test_04b_verify_requires_calibration(self):
        pc = mca.CalibrationProtocol1D(self._config_1d())
        with self.assertRaises(ValueError):
            pc.verify_calibration(wait_time=0)

    def test_01_Calibrator2D(self):
        try:
            shutil.rmtree("monet/tests/TestData/calibrate")
        except Exception:
            pass
        try:
            os.makedirs("monet/tests/TestData/calibrate", exist_ok=True)
        except Exception:
            pass

        config = {
            "database": "monet/tests/TestData/calibrate/power_database.xlsx",
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
                488: {
                    "classpath": "monet.laser.TestLaser",
                    "init_kwargs": {"port": "COM4"},
                },
                561: {
                    "classpath": "monet.laser.TestLaser",
                    "init_kwargs": {"port": "COM7"},
                },
                640: {
                    "classpath": "monet.laser.TestLaser",
                    "init_kwargs": {"port": "COM8"},
                },
            },
            "beampath": {
                "shutter01": {
                    "classpath": "monet.beampath.TestShutter",
                    "init_kwargs": {"SN": 234},
                },
                "shutter02": {
                    "classpath": "monet.beampath.TestShutter",
                    "init_kwargs": {"SN": 456},
                },
            },
        }
        calibration_protocol = {
            "laser_sequence": [488, 561],
            "laser_powers": {
                488: [100, 200, 500],
                561: [200, 500, 1000],
            },
            "beampath": {
                488: {"shutter01": True, "shutter02": True},
                561: {"shutter01": True, "shutter02": False},
            },
        }
        pc = mca.CalibrationProtocol2D(config, calibration_protocol)

        # if not calibrated yet, setting power should yield a Value error
        with self.assertRaises(ValueError) as context:
            pc.instrument.power = 5
        self.assertTrue("Not calibrated" in str(context.exception))

        # remove the database to test creating a new one
        try:
            os.remove(config["database"])
        except Exception:
            pass
        pc.run_protocol(wait_time=0)

        pc.instrument.load_calibration_database()

        pc.instrument.power = 5

        pc.instrument.power = 89

        pc.instrument.power = 300

        pc.instrument.power = 2000

        assert True

    def test_05_verify_and_fit_qualities_2d(self):
        try:
            os.makedirs("monet/tests/TestData/calibrate", exist_ok=True)
        except Exception:
            pass
        db = "monet/tests/TestData/calibrate/verify2d.xlsx"
        try:
            os.remove(db)
        except Exception:
            pass
        config = {
            "database": db,
            "index": {"name": "DefaultMicroscope"},
            "powermeter": {
                "classpath": "monet.powermeter.TestPowerMeter",
                "init_kwargs": {
                    "bkg": 0,
                    "amp": 50,
                    "phi": 30,
                    "start": 10,
                    "step": 5,
                    "noise": 1,
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
                "init_kwargs": {"min": 30, "max": 100, "step": 5},
            },
            "lasers": {
                488: {
                    "classpath": "monet.laser.TestLaser",
                    "init_kwargs": {"port": "COM4"},
                },
            },
            "beampath": {
                "shutter01": {
                    "classpath": "monet.beampath.TestShutter",
                    "init_kwargs": {"SN": 234},
                },
            },
        }
        protocol = {
            "laser_sequence": [488],
            "laser_powers": {488: [100, 200]},
            "beampath": {488: {"shutter01": True}},
        }
        pc = mca.CalibrationProtocol2D(config, protocol)
        pc.run_protocol(wait_time=0)

        # per-curve fit quality was collected for each (laser, power)
        self.assertIn((488, 100), pc.fit_qualities)
        self.assertIn((488, 200), pc.fit_qualities)

        pc.instrument.load_calibration_database()
        res = pc.verify_calibration(n_angles=2, wait_time=0, switch_time=0)

        # 2 angles x 2 calibrated laser powers (one laser)
        self.assertEqual(len(res["points"]), 4)
        levels = {p["laser_power"] for p in res["points"]}
        self.assertEqual(levels, {100, 200})
        # every point records which laser it came from
        self.assertEqual({p["laser"] for p in res["points"]}, {488})
        # the laser is switched off again after verification
        self.assertFalse(pc.instrument.lasers[488].enabled)
        # candidate models were evaluated against the fresh measurements
        ms = res["model_summary"]
        self.assertIn("sinus", ms)
        self.assertTrue(any(k.startswith("poly") for k in ms))
        for s in ms.values():
            for key in ("fit_rms_pct", "verify_rms_pct", "verify_max_pct"):
                self.assertIn(key, s)

        # a laser that was ON before verify is left ON afterwards (its prior
        # on/off state is restored, not force-disabled).
        pc.instrument.lasers[488].enabled = True
        pc.verify_calibration(n_angles=2, wait_time=0, switch_time=0)
        self.assertTrue(pc.instrument.lasers[488].enabled)

    def test_06_switch_model_reuses_mixed_db(self):
        """Switching analysis model reuses a DB with old (foreign) rows.

        Regression: after a sinusoidal calibration, switching to the
        polynomial model and recalibrating on the *same* database must not
        crash loading the older sinusoidal rows (KeyError 'p0'). The latest
        (polynomial) row per power is used; incompatible rows are skipped.
        """
        import tempfile

        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "mixed.xlsx")
        plotdir = os.path.join(tmp, "plots")
        os.makedirs(plotdir, exist_ok=True)

        def make_config(classpath, extra_ana):
            ana_kwargs = {"min": 30, "max": 100, "step": 5}
            ana_kwargs.update(extra_ana)
            return {
                "database": db,
                "dest_calibration_plot": plotdir,
                "index": {"name": "DefaultMicroscope"},
                "powermeter": {
                    "classpath": "monet.powermeter.TestPowerMeter",
                    "init_kwargs": {
                        "bkg": 1,
                        "amp": 50,
                        "phi": 30,
                        "start": 10,
                        "step": 5,
                        "noise": 1,
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
                    "classpath": classpath,
                    "init_kwargs": ana_kwargs,
                },
                "lasers": {
                    488: {
                        "classpath": "monet.laser.TestLaser",
                        "init_kwargs": {"port": "COM4"},
                    },
                },
                "beampath": {
                    "shutter01": {
                        "classpath": "monet.beampath.TestShutter",
                        "init_kwargs": {"SN": 234},
                    },
                },
            }

        protocol = {
            "laser_sequence": [488],
            "laser_powers": {488: [100, 200]},
            "beampath": {488: {"shutter01": True}},
        }

        # 1) sinusoidal calibration writes sinus rows (bkg/amp/phi)
        pc_s = mca.CalibrationProtocol2D(
            make_config("monet.analysis.SinusAttenuationCurveAnalyzer", {}),
            protocol,
        )
        pc_s.run_protocol(wait_time=0)

        # 2) switch to polynomial on the SAME db and recalibrate — must not
        # crash loading/plotting the older sinusoidal rows.
        pc_p = mca.CalibrationProtocol2D(
            make_config(
                "monet.analysis.PolynomAttenuationCurveAnalyzer",
                {"polydegree": 3},
            ),
            protocol,
        )
        pc_p.run_protocol(wait_time=0)

        # loading (latest = polynomial rows) and setting power must work
        pc_p.instrument.load_calibration_database()
        self.assertTrue(pc_p.instrument.is_calibrated)
        pc_p.instrument.power = 5

    def test_07_switch_model_on_calibrated_instrument_no_nan(self):
        """Switching the analysis model on a *calibrated* instrument (as the
        GUI 'Apply best model' does) must not KeyError 'nan'.

        Regression: with the old calibration still loaded, selecting a laser
        rebuilt analyzers from incompatible rows -> empty power ranges ->
        laserpower = NaN -> KeyError 'nan'. It now falls back to uncalibrated.
        """
        import tempfile

        from monet.util import load_class

        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "switch.xlsx")
        plotdir = os.path.join(tmp, "plots")
        os.makedirs(plotdir, exist_ok=True)
        config = {
            "database": db,
            "dest_calibration_plot": plotdir,
            "index": {"name": "DefaultMicroscope"},
            "powermeter": {
                "classpath": "monet.powermeter.TestPowerMeter",
                "init_kwargs": {
                    "bkg": 1,
                    "amp": 50,
                    "phi": 30,
                    "start": 10,
                    "step": 5,
                    "noise": 1,
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
                "init_kwargs": {"min": 30, "max": 100, "step": 5},
            },
            "lasers": {
                488: {
                    "classpath": "monet.laser.TestLaser",
                    "init_kwargs": {"port": "COM4"},
                },
            },
            "beampath": {
                "shutter01": {
                    "classpath": "monet.beampath.TestShutter",
                    "init_kwargs": {"SN": 234},
                },
            },
        }
        protocol = {
            "laser_sequence": [488],
            "laser_powers": {488: [100, 200]},
            "beampath": {488: {"shutter01": True}},
        }
        pc = mca.CalibrationProtocol2D(config, protocol)
        pc.run_protocol(wait_time=0)
        pc.instrument.load_calibration_database()
        self.assertTrue(pc.instrument.is_calibrated)

        # mimic "apply best model": swap in the polynomial analyzer on the
        # already-calibrated instrument, then select the laser.
        poly = {
            "classpath": "monet.analysis.PolynomAttenuationCurveAnalyzer",
            "init_kwargs": {"min": 30, "max": 100, "step": 5, "polydegree": 5},
        }
        pc.instrument.config["analysis"] = poly
        pc.instrument.analyzer = load_class(
            poly["classpath"], poly["init_kwargs"]
        )

        # while still is_calibrated (no reselect yet), an explicit query for a
        # laser with no compatible calibration raises a clear error instead of
        # crashing on min() of an empty analyzers dict.
        self.assertTrue(pc.instrument.is_calibrated)
        with self.assertRaises(ValueError) as ctx:
            pc.instrument._analyzers_for(488)
        self.assertIn("No calibration compatible", str(ctx.exception))

        pc.instrument.laser = 488  # must not raise KeyError 'nan'
        # the stale (sinusoidal) calibration is no longer considered valid
        self.assertFalse(pc.instrument.is_calibrated)

        # recovery: switching back to a compatible model and reselecting the
        # laser re-establishes calibration (no sticky lockout).
        sinus = {
            "classpath": "monet.analysis.SinusAttenuationCurveAnalyzer",
            "init_kwargs": {"min": 30, "max": 100, "step": 5},
        }
        pc.instrument.config["analysis"] = sinus
        pc.instrument.analyzer = load_class(
            sinus["classpath"], sinus["init_kwargs"]
        )
        pc.instrument.laser = 488
        self.assertTrue(pc.instrument.is_calibrated)

    def _config_2d(self, db, plotdir):
        """The shared 2D-protocol config over Test* hardware (same shape as
        the inline dicts in test_06/test_07, which predate this helper)."""
        return {
            "database": db,
            "dest_calibration_plot": plotdir,
            "index": {"name": "DefaultMicroscope"},
            "powermeter": {
                "classpath": "monet.powermeter.TestPowerMeter",
                "init_kwargs": {
                    "bkg": 1,
                    "amp": 50,
                    "phi": 30,
                    "start": 10,
                    "step": 5,
                    "noise": 1,
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
                "init_kwargs": {"min": 30, "max": 100, "step": 5},
            },
            "lasers": {
                488: {
                    "classpath": "monet.laser.TestLaser",
                    "init_kwargs": {"port": "COM4"},
                },
            },
            "beampath": {
                "shutter01": {
                    "classpath": "monet.beampath.TestShutter",
                    "init_kwargs": {"SN": 234},
                },
            },
        }

    def test_08_run_protocol_with_new_power_level(self):
        """Recalibrating with a power level not yet in the database must run.

        Regression: with calibrations for [100, 200] mW loaded, a protocol
        whose ``laser_powers`` adds 50 mW aborted at the first
        ``instrument.laserpower = 50`` with a bare ``KeyError: 50`` (shown as
        "Error: 50" in the GUI) because the instrument still counted as
        calibrated from the old rows.
        """
        import tempfile

        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "newpower.xlsx")
        plotdir = os.path.join(tmp, "plots")
        os.makedirs(plotdir, exist_ok=True)
        protocol = {
            "laser_sequence": [488],
            "laser_powers": {488: [100, 200]},
            "beampath": {488: {"shutter01": True}},
        }
        pc = mca.CalibrationProtocol2D(self._config_2d(db, plotdir), protocol)
        pc.run_protocol(wait_time=0, switch_time=0)
        pc.instrument.load_calibration_database()
        self.assertTrue(pc.instrument.is_calibrated)

        # second run adds an uncalibrated power level; must not KeyError
        pc.protocol["laser_powers"] = {488: [50, 100]}
        pc.run_protocol(wait_time=0, switch_time=0)
        pc.instrument.load_calibration_database()
        self.assertTrue(pc.instrument.is_calibrated)
        self.assertIn(50, pc.instrument._analyzers)

    def test_09_plot_views_raw_and_projected(self):
        """Sample runs get one direct view; BFP runs get raw + projected."""
        import tempfile

        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "views.xlsx")
        plotdir = os.path.join(tmp, "plots")
        os.makedirs(plotdir, exist_ok=True)
        pc = mca.CalibrationProtocol2D(
            self._config_2d(db, plotdir),
            {
                "laser_sequence": [488],
                "laser_powers": {488: [100, 200]},
                "beampath": {488: {"shutter01": True}},
            },
        )

        # sample plane -> single direct view
        views = pc._plot_views(488, "sample")
        self.assertEqual([v[0] for v in views], [mca.SAMPLE_SUBDIR])
        self.assertEqual(views[0][1], 1.0)

        # BFP with a transmission factor -> raw (x1) + projected (xT), the
        # factor shown in the projected label.
        pc._sample_plane_factor = lambda laser, pmt: 0.4
        views = pc._plot_views(488, "bfp")
        self.assertEqual(
            [v[0] for v in views],
            [mca.BFP_RAW_SUBDIR, mca.BFP_PROJECTED_SUBDIR],
        )
        self.assertEqual(views[0][1], 1.0)
        self.assertIn("directly measured", views[0][2])
        self.assertEqual(views[1][1], 0.4)
        self.assertIn("0.4000", views[1][2])

        # BFP without a factor -> projected view still produced, labelled so
        pc._sample_plane_factor = lambda laser, pmt: 1.0
        views = pc._plot_views(488, "bfp")
        self.assertEqual(
            [v[0] for v in views],
            [mca.BFP_RAW_SUBDIR, mca.BFP_PROJECTED_SUBDIR],
        )
        self.assertIn("no transmission factor", views[1][2])

    def test_10_calibration_plot_subfolders(self):
        """A full run files its plots/tables into the per-view subfolders."""
        import tempfile

        import pandas as pd

        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "subfolders.xlsx")
        plotdir = os.path.join(tmp, "plots")
        os.makedirs(plotdir, exist_ok=True)
        protocol = {
            "laser_sequence": [488],
            "laser_powers": {488: [100, 200]},
            "beampath": {488: {"shutter01": True}},
        }

        # sample-plane run -> only the sample subfolder, direct values
        mca.CalibrationProtocol2D(
            self._config_2d(db, plotdir), protocol
        ).run_protocol(wait_time=0, powermeter_type="sample")
        sample_dir = os.path.join(plotdir, mca.SAMPLE_SUBDIR)
        self.assertTrue(
            os.path.exists(os.path.join(sample_dir, "pwrmeasured_488nm.png"))
        )
        self.assertTrue(
            os.path.exists(os.path.join(sample_dir, "488nm_100mW.png"))
        )
        self.assertFalse(
            os.path.isdir(os.path.join(plotdir, mca.BFP_RAW_SUBDIR))
        )

        # BFP run on the same db -> raw + projected subfolders, both populated
        pc = mca.CalibrationProtocol2D(self._config_2d(db, plotdir), protocol)
        pc.run_protocol(wait_time=0, powermeter_type="bfp")
        raw_dir = os.path.join(plotdir, mca.BFP_RAW_SUBDIR)
        proj_dir = os.path.join(plotdir, mca.BFP_PROJECTED_SUBDIR)
        for d in (raw_dir, proj_dir):
            self.assertTrue(
                os.path.exists(os.path.join(d, "pwrmeasured_488nm.xlsx"))
            )
            self.assertTrue(os.path.exists(os.path.join(d, "488nm_200mW.png")))

        # the projected table is the raw table scaled by the stored factor
        raw = pd.read_excel(
            os.path.join(raw_dir, "pwrmeasured_488nm.xlsx"), index_col=0
        )
        proj = pd.read_excel(
            os.path.join(proj_dir, "pwrmeasured_488nm.xlsx"), index_col=0
        )
        factor = pc._sample_plane_factor(488, "bfp")
        np.testing.assert_allclose(
            proj.to_numpy(), raw.to_numpy() * factor, rtol=1e-6
        )

    def test_11_curve_plot_survives_fit_eval_error(self):
        """A fit-evaluation failure mid-build must not crash plot saving.

        Regression: ``grid`` is assigned before ``fit`` in
        ``_save_curve_plot``; if ``estimate_power`` raises, ``grid`` ends up
        set while ``fit`` stays None. Guarding the overlay on ``grid`` then
        crashed on ``fit * factor``. The curve must still be saved (without
        the fit overlay).
        """
        import tempfile

        tmp = tempfile.mkdtemp()
        plotdir = os.path.join(tmp, "plots")
        os.makedirs(plotdir, exist_ok=True)
        cfg = self._config_1d()
        cfg["database"] = os.path.join(tmp, "fiterr.xlsx")
        cfg["dest_calibration_plot"] = plotdir
        pc = mca.CalibrationProtocol1D(cfg)

        class _BoomAnalyzer:
            def load_model(self, pars):
                pass

            def estimate_power(self, x):
                raise RuntimeError("fit eval boom")

        orig = mca.load_class
        mca.load_class = lambda *a, **k: _BoomAnalyzer()
        try:
            pc._save_curve_plot(
                488,
                100,
                np.array([30.0, 40.0]),
                np.array([1.0, 2.0]),
                {"bkg": 0, "amp": 1, "phi": 0},
                "sample",
            )
        finally:
            mca.load_class = orig

        out = os.path.join(plotdir, mca.SAMPLE_SUBDIR, "488nm_100mW.png")
        self.assertTrue(os.path.exists(out))

    def test_12_regenerate_plots_from_db(self):
        """Plots are rebuilt from the stored DB without re-measuring."""
        import shutil as _sh
        import tempfile

        import pandas as pd

        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "regen.xlsx")
        plotdir = os.path.join(tmp, "plots")
        os.makedirs(plotdir, exist_ok=True)
        protocol = {
            "laser_sequence": [488],
            "laser_powers": {488: [100, 200]},
            "beampath": {488: {"shutter01": True}},
        }

        # populate DB + plots with a sample-plane run
        mca.CalibrationProtocol2D(
            self._config_2d(db, plotdir), protocol
        ).run_protocol(wait_time=0, powermeter_type="sample")

        # wipe the plot outputs; the database remains
        sample_dir = os.path.join(plotdir, mca.SAMPLE_SUBDIR)
        _sh.rmtree(sample_dir)
        os.remove(os.path.join(plotdir, "488nm.png"))
        self.assertFalse(os.path.isdir(sample_dir))

        # regenerate purely from the stored calibrations (fresh instance, so
        # nothing from the run lingers in memory)
        pc = mca.CalibrationProtocol2D(self._config_2d(db, plotdir), protocol)
        done = pc.regenerate_plots()
        self.assertEqual(done, [488])
        for name in (
            "pwrmeasured_488nm.xlsx",
            "pwrmeasured_488nm.png",
            "488nm_100mW.png",
            "488nm_200mW.png",
        ):
            self.assertTrue(
                os.path.exists(os.path.join(sample_dir, name)), name
            )
        # model-parameter plot is regenerated at the top level
        self.assertTrue(os.path.exists(os.path.join(plotdir, "488nm.png")))

        # the regenerated table is indexed by the attenuator control grid and
        # holds the stored model evaluated there (sample plane -> unscaled)
        tbl = pd.read_excel(
            os.path.join(sample_dir, "pwrmeasured_488nm.xlsx"), index_col=0
        )
        init = pc.instrument.config["analysis"]["init_kwargs"]
        grid = np.arange(init["min"], init["max"] + init["step"], init["step"])
        np.testing.assert_allclose(
            tbl.index.to_numpy(dtype=float), grid, rtol=1e-6
        )
        self.assertEqual(sorted(int(c) for c in tbl.columns), [100, 200])

    def test_13_regenerate_plots_empty_db(self):
        """Regenerating with no stored calibrations is a no-op, not a crash."""
        import tempfile

        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "empty.xlsx")
        plotdir = os.path.join(tmp, "plots")
        os.makedirs(plotdir, exist_ok=True)
        protocol = {
            "laser_sequence": [488],
            "laser_powers": {488: [100]},
            "beampath": {488: {"shutter01": True}},
        }
        pc = mca.CalibrationProtocol2D(self._config_2d(db, plotdir), protocol)
        self.assertEqual(pc.regenerate_plots(), [])
