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
