"""
monet/tests/test_analysis.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Test the analysis module of monet.

:authors: Heinrich Grabmayr, 2022
:copyright: Copyright (c) 2022 Jungmann Lab, MPI of Biochemistry
"""

import os
import tempfile
import unittest

import numpy as np

import monet.analysis as man


class TestAnalysis(unittest.TestCase):

    def setUp(self):
        config = {
            "min": 30,
            "max": 100,
            "step": 5,
        }
        self.att = man.SinusAttenuationCurveAnalyzer(config)

    def tearDown(self):
        pass

    def test_01_SinusAnalyser(self):
        config = {
            "min": 30,
            "max": 100,
            "step": 5,
        }
        man.SinusAttenuationCurveAnalyzer(config)

    def test_02_Sin_model_fun(self):
        model = {
            "bkg": 0,
            "amp": 50,
            "phi": 30,
            "start": 10,
            "step": 5,
            "stop": 100,
        }
        x = np.arange(model["start"], model["stop"], model["step"])
        print("x in ", x)

        pwr = self.att._model_function(
            x, model["bkg"], model["amp"], model["phi"]
        )
        self.att._model_function(90, model["bkg"], model["amp"], model["phi"])

        print("pwr", pwr)

        x_back = self.att._model_function_inv(
            pwr, model["bkg"], model["amp"], model["phi"], mini=0, maxi=100
        )
        self.att._model_function_inv(
            0.5, model["bkg"], model["amp"], model["phi"], mini=0, maxi=100
        )
        with self.assertRaises(ValueError) as context:
            self.att._model_function_inv(
                2 * (model["amp"] + model["bkg"]),
                model["bkg"],
                model["amp"],
                model["phi"],
                mini=0,
                maxi=100,
            )
        self.assertTrue("out of range." in str(context.exception))
        with self.assertRaises(ValueError) as context:
            self.att._model_function_inv(
                2 * pwr,
                model["bkg"],
                model["amp"],
                model["phi"],
                mini=0,
                maxi=100,
            )
        self.assertTrue("out of range." in str(context.exception))

        print("x back", x_back)

        initpars = self.att._model_function_estinit(pwr, x)
        print("estimated init pars")
        print(initpars)
        print("config with init pars", model)
        assert True

    def test_03_Sinus_roundtrip_and_model_io(self):
        # Generate data from a known sinusoidal model, fit it, and confirm
        # the recovered model round-trips position <-> power.
        bkg, amp, phi = 0.0, 50.0, 30.0
        x = np.arange(0, 90, 2.0)
        y = self.att._model_function(x, bkg, amp, phi)

        self.att.fit(x, y)

        model = self.att.get_model()
        for key in ("bkg", "amp", "phi"):
            self.assertIn(key, model)

        # estimate_power(estimate(p)) ~= p for an in-range target. Stay
        # safely inside [bkg, bkg+amp] to avoid the boundary check.
        target = bkg + amp * 0.5
        pos = self.att.estimate(target)
        self.assertAlmostEqual(self.att.estimate_power(pos), target, places=1)

        # load_model restores parameters.
        self.att.load_model(model)
        self.assertEqual(self.att.curr_params, model)

    def test_03_Sinus_plot(self):
        x = np.arange(0, 90, 5.0)
        y = self.att._model_function(x, 0.0, 50.0, 30.0)
        self.att.fit(x, y)
        with tempfile.TemporaryDirectory() as d:
            fname = os.path.join(d, "sinus.png")
            self.att.plot(fname, xlabel="angle", ylabel="power", title="t")
            self.assertTrue(os.path.exists(fname))


class TestCompareModels(unittest.TestCase):
    """analysis.compare_models ranks candidate fits by residual."""

    ANA = {"min": 30.0, "max": 130.0, "step": 5.0}

    def test_ranks_and_reports_all_models(self):
        x = np.arange(30.0, 131.0, 5.0)
        # clean squared-sine data -> the sinus model should fit it well
        y = 1.0 + 40.0 * (1 + np.sin(4 * np.pi / 180 * (x + 15))) / 2
        ranking = man.compare_models(x, y, self.ANA, degrees=(3, 4, 5))
        names = {r["model"] for r in ranking}
        self.assertIn("sinus", names)
        self.assertIn("poly deg 4", names)
        # sorted ascending by RMS
        rms = [r["rms_pct"] for r in ranking]
        self.assertEqual(rms, sorted(rms))
        # sinus fits the sinusoid to within a small residual
        sinus = next(r for r in ranking if r["model"] == "sinus")
        self.assertLess(sinus["rms_pct"], 2.0)

    def test_params2coef_rejects_foreign_model_params(self):
        # A sinusoidal-model row must not silently load into the polynomial
        # analyzer (would KeyError on 'p0'); it raises a clear error instead.
        poly = man.PolynomAttenuationCurveAnalyzer(
            {"min": 30, "max": 130, "step": 5, "polydegree": 4}
        )
        with self.assertRaises(ValueError) as ctx:
            poly.load_model({"bkg": 1.0, "amp": 40.0, "phi": 15.0})
        self.assertIn("different analysis model", str(ctx.exception))

    def test_poly_model_roundtrips(self):
        poly = man.PolynomAttenuationCurveAnalyzer(
            {"min": 30, "max": 130, "step": 5, "polydegree": 4}
        )
        x = np.linspace(30, 130, 21)
        y = 0.001 * (x - 20) ** 2 + 2.0
        poly.fit(x, y)
        pars = poly.get_model()
        poly2 = man.PolynomAttenuationCurveAnalyzer(
            {"min": 30, "max": 130, "step": 5, "polydegree": 4}
        )
        poly2.load_model(pars)
        self.assertAlmostEqual(
            float(poly2.estimate_power(80)),
            float(poly.estimate_power(80)),
            places=4,
        )

    def test_model_spec_maps_names(self):
        cp, extra = man.model_spec("sinus")
        self.assertTrue(cp.endswith("SinusAttenuationCurveAnalyzer"))
        self.assertEqual(extra, {})
        cp, extra = man.model_spec("poly deg 5")
        self.assertTrue(cp.endswith("PolynomAttenuationCurveAnalyzer"))
        self.assertEqual(extra, {"polydegree": 5})
        cp, extra = man.model_spec("linear")
        self.assertTrue(cp.endswith("LinearCurveAnalyzer"))
        self.assertEqual(extra, {})

    def test_model_name_from_config_roundtrips(self):
        self.assertEqual(
            man.model_name_from_config(
                "monet.analysis.SinusAttenuationCurveAnalyzer"
            ),
            "sinus",
        )
        self.assertEqual(
            man.model_name_from_config("monet.analysis.LinearCurveAnalyzer"),
            "linear",
        )
        self.assertEqual(
            man.model_name_from_config(
                "monet.analysis.PolynomAttenuationCurveAnalyzer",
                {"polydegree": 5},
            ),
            "poly deg 5",
        )

    def test_polynomial_wins_on_polynomial_data(self):
        x = np.arange(30.0, 131.0, 5.0)
        y = 0.001 * (x - 20) ** 2 + 2.0  # a parabola, not a sinusoid
        ranking = man.compare_models(x, y, self.ANA, degrees=(2, 3, 4))
        self.assertTrue(ranking[0]["model"].startswith("poly"))
        self.assertLess(ranking[0]["rms_pct"], 1.0)

    def test_compare_models_multi_pools_across_curves(self):
        x = np.arange(30.0, 131.0, 5.0)
        y1 = 1.0 + 40.0 * (1 + np.sin(4 * np.pi / 180 * (x + 15))) / 2
        y2 = 0.5 + 30.0 * (1 + np.sin(4 * np.pi / 180 * (x + 15))) / 2
        ranking = man.compare_models_multi(
            [(x, y1), (x, y2)], self.ANA, degrees=(3, 4)
        )
        names = {r["model"] for r in ranking}
        self.assertIn("sinus", names)
        # every model was fit on both curves
        for r in ranking:
            self.assertEqual(r["n_curves"], 2)
        # sorted ascending by pooled RMS; sinus fits the sinusoids well
        rms = [r["rms_pct"] for r in ranking]
        self.assertEqual(rms, sorted(rms))
        sinus = next(r for r in ranking if r["model"] == "sinus")
        self.assertLess(sinus["rms_pct"], 2.0)


class TestSplineAnalyzer(unittest.TestCase):
    """The smoothing-spline attenuation model + its serialization."""

    ANA = {"min": 30.0, "max": 130.0, "step": 5.0}

    def _fit(self):
        a = man.SplineAttenuationCurveAnalyzer(dict(self.ANA))
        x = np.arange(30.0, 131.0, 5.0)
        y = 0.001 * (x - 20) ** 2 + 2.0  # monotonic on [30, 130]
        a.fit(x, y)
        return a, x, y

    def test_forward_follows_data(self):
        a, x, y = self._fit()
        pred = np.asarray(a.estimate_power(x), dtype=float)
        rms = np.sqrt(np.mean(((pred - y) / y) ** 2)) * 100.0
        self.assertLess(rms, 3.0)

    def test_inverse_roundtrips(self):
        a, x, y = self._fit()
        span = float(y.max() - y.min())
        for frac in (0.3, 0.7):
            p = float(y.min() + frac * span)
            ang = float(a.estimate(p))
            self.assertLess(abs(float(a.estimate_power(ang)) - p), 0.03 * span)

    def test_model_roundtrips_through_params(self):
        a, _, _ = self._fit()
        pars = a.get_model()
        self.assertIn("spl_k", pars)
        self.assertTrue(any(str(k).startswith("t") for k in pars))
        b = man.SplineAttenuationCurveAnalyzer(dict(self.ANA))
        b.load_model(pars)
        self.assertAlmostEqual(
            float(b.estimate_power(75.0)),
            float(a.estimate_power(75.0)),
            places=4,
        )

    def test_load_model_rejects_foreign_params(self):
        b = man.SplineAttenuationCurveAnalyzer(dict(self.ANA))
        with self.assertRaises(ValueError):
            b.load_model({"bkg": 1.0, "amp": 40.0, "phi": 15.0})

    def test_model_spec_and_name(self):
        cp, extra = man.model_spec("spline")
        self.assertTrue(cp.endswith("SplineAttenuationCurveAnalyzer"))
        self.assertEqual(extra, {})
        self.assertEqual(
            man.model_name_from_config(
                "monet.analysis.SplineAttenuationCurveAnalyzer"
            ),
            "spline",
        )


class TestLinearAnalyzer(unittest.TestCase):

    def setUp(self):
        self.config = {"min": 0.0, "max": 10.0}
        self.att = man.LinearCurveAnalyzer(self.config)

    def test_fit_and_roundtrip(self):
        bkg, amp = 1.0, 2.0
        x = np.linspace(0.0, 10.0, 21)
        y = bkg + amp * x
        self.att.fit(x, y)

        # estimate(y) inverts the model; estimate_power(x) evaluates it.
        self.assertAlmostEqual(self.att.estimate(11.0), 5.0, places=3)
        self.assertAlmostEqual(self.att.estimate_power(5.0), 11.0, places=3)

    def test_output_range(self):
        x = np.linspace(0.0, 10.0, 21)
        self.att.fit(x, 1.0 + 2.0 * x)
        lo, hi = self.att.output_range()
        self.assertAlmostEqual(lo, 1.0, places=2)
        self.assertAlmostEqual(hi, 21.0, places=2)

    def test_estimate_out_of_range_raises(self):
        x = np.linspace(0.0, 10.0, 21)
        self.att.fit(x, 1.0 + 2.0 * x)
        with self.assertRaises(ValueError):
            self.att.estimate(1000.0)

    def test_plot(self):
        x = np.linspace(0.0, 10.0, 11)
        self.att.fit(x, 1.0 + 2.0 * x)
        with tempfile.TemporaryDirectory() as d:
            fname = os.path.join(d, "linear.png")
            self.att.plot(fname)
            self.assertTrue(os.path.exists(fname))


class TestPointAnalyzer(unittest.TestCase):

    def test_scalar(self):
        att = man.PointCurveAnalyzer({})
        power = 8
        att.fit(0, power)
        self.assertEqual(att.estimate_power(0), power)
        self.assertEqual(att.estimate_power(9), power)
        self.assertEqual(att.estimate(9), 0)
        self.assertEqual(att.output_range(), [power, power])

    def test_array_inputs(self):
        att = man.PointCurveAnalyzer({})
        # fit with an iterable averages the values.
        att.fit([0, 1, 2], np.array([4.0, 6.0, 8.0]))
        self.assertAlmostEqual(att.curr_params["amp"], 6.0)

        x = np.array([0.0, 5.0, 9.0])
        np.testing.assert_allclose(att.estimate(x), np.zeros_like(x))
        np.testing.assert_allclose(
            att.estimate_power(x), 6.0 * np.ones_like(x)
        )

    def test_model_internals(self):
        att = man.PointCurveAnalyzer({})
        # scalar input returns amp; the inverse is the identity.
        self.assertEqual(att._model_function(5, bkg=0, amp=8), 8)
        self.assertEqual(att._model_function_inv(3.3, amp=8), 3.3)
        # init estimate uses the mean of y.
        pars = att._model_function_estinit(np.array([4.0, 8.0]), [0, 1])
        self.assertAlmostEqual(pars["amp"], 6.0)
        # array input path runs and returns an array of the right shape.
        out = att._model_function(np.zeros(3), bkg=0, amp=8)
        self.assertEqual(np.asarray(out).shape, (3,))

    def test_plot_is_noop(self):
        att = man.PointCurveAnalyzer({})
        att.fit(0, 8)
        # PointCurveAnalyzer.plot only logs; just confirm it does not raise.
        att.plot("unused.png")


class TestPolynomAnalyzer(unittest.TestCase):

    def setUp(self):
        self.config = {"min": 0.0, "max": 10.0, "polydegree": 4}
        self.att = man.PolynomAttenuationCurveAnalyzer(self.config)
        self.x = np.linspace(0.0, 10.0, 60)
        self.y = self.x**2  # monotonic increasing over [0, 10]

    def test_fit_estimate_power_roundtrip(self):
        self.att.fit(self.x, self.y)
        # Forward evaluation should track the underlying data closely.
        self.assertAlmostEqual(self.att.estimate_power(5.0), 25.0, delta=0.5)

    def test_estimate_clips_to_range(self):
        self.att.fit(self.x, self.y)
        # A power above the fitted range clips to max control parameter.
        est = self.att.estimate(1e6)
        self.assertLessEqual(est, self.config["max"])
        self.assertGreaterEqual(est, self.config["min"])

    def test_output_range_two_values(self):
        self.att.fit(self.x, self.y)
        rng = self.att.output_range()
        self.assertEqual(len(rng), 2)
        self.assertLessEqual(rng[0], rng[1])

    def test_get_model_and_coef_roundtrip(self):
        self.att.fit(self.x, self.y)
        model = self.att.get_model()
        self.assertIn("p0", model)
        self.assertIn("i0", model)
        coef_fw, coef_bw = self.att.params2coef(model)
        # coef2params(params2coef(model)) is the identity for these keys.
        again = self.att.coef2params(coef_fw, coef_bw)
        self.assertEqual(set(again.keys()), set(model.keys()))

    def test_estimate_power_without_fit_returns_zero(self):
        # poly is None before any fit.
        self.assertEqual(self.att.estimate_power(5.0), 0)

    def test_plot_after_fit(self):
        self.att.fit(self.x, self.y)
        with tempfile.TemporaryDirectory() as d:
            fname = os.path.join(d, "poly.png")
            # fitvals_forward exists -> the data-overlay plotting branch.
            self.att.plot(fname, xlabel="x", ylabel="P", title="poly")
            self.assertTrue(os.path.exists(fname))

    def test_plot_after_load_model(self):
        self.att.fit(self.x, self.y)
        model = self.att.get_model()
        fresh = man.PolynomAttenuationCurveAnalyzer(
            {"min": 0.0, "max": 10.0, "polydegree": 4}
        )
        fresh.load_model(model)
        with tempfile.TemporaryDirectory() as d:
            fname = os.path.join(d, "poly2.png")
            # No fitvals_forward -> the load_model plotting branch.
            fresh.plot(fname)
            self.assertTrue(os.path.exists(fname))
