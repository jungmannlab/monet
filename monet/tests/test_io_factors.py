"""
monet/tests/test_io_factors.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tests for the Excel power-meter correction-factor functions and the
device-history plotting helpers in monet.io.

:authors: Heinrich Grabmayr, 2024
:copyright: Copyright (c) 2024 Jungmann Lab, MPI of Biochemistry
"""

import os
import tempfile
import unittest
from datetime import datetime

import numpy as np
import pandas as pd

import monet.analysis as man
import monet.io as mio
from monet import DATABASE_INDEXLEVELS


def _write_calib_db(path, rows):
    """Write the calibration sheet (sheet 0) of an Excel database."""
    index_tuples = [(r[0], r[1], r[2], r[3], r[4]) for r in rows]
    midx = pd.MultiIndex.from_tuples(index_tuples, names=DATABASE_INDEXLEVELS)
    df = pd.DataFrame([r[5] for r in rows], index=midx)
    df.to_excel(path)


class TestFactorsExcel(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "db.xlsx")
        # _save_factor_excel opens the file in append mode, so it must already
        # exist with at least one sheet.
        _write_calib_db(
            self.db,
            [
                (
                    "TestScope",
                    488.0,
                    100.0,
                    "2024-01-01",
                    "10:00",
                    {"amp": 40.0},
                ),
            ],
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load_factor(self):
        mio._save_factor_excel(
            self.db, "TestScope", 488, "2024-06-01", 0.82, 0.04, 30
        )
        df = mio.load_factors(self.db, device="TestScope", laser=488)
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertAlmostEqual(row["transmission_objective_mean"], 0.82)
        self.assertAlmostEqual(row["transmission_objective_std"], 0.04)
        self.assertEqual(int(row["n_points"]), 30)

    def test_save_factor_updates_existing(self):
        mio._save_factor_excel(
            self.db, "TestScope", 488, "2024-06-01", 0.80, 0.04, 30
        )
        mio._save_factor_excel(
            self.db, "TestScope", 488, "2024-06-01", 0.90, 0.02, 50
        )
        df = mio.load_factors(self.db, device="TestScope", laser=488)
        # Same (device, wavelength, date) -> one row, updated value.
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(df.iloc[0]["transmission_objective_mean"], 0.90)

    def test_load_factors_filters(self):
        mio._save_factor_excel(
            self.db, "ScopeA", 488, "2024-06-01", 0.8, 0.0, 10
        )
        mio._save_factor_excel(
            self.db, "ScopeB", 561, "2024-06-01", 0.7, 0.0, 10
        )
        self.assertEqual(len(mio.load_factors(self.db, device="ScopeA")), 1)
        self.assertEqual(len(mio.load_factors(self.db, laser=561)), 1)
        self.assertEqual(len(mio.load_factors(self.db)), 2)

    def test_load_factors_missing_file(self):
        df = mio.load_factors(os.path.join(self.tmpdir, "nope.xlsx"))
        self.assertTrue(df.empty)

    def test_load_factors_no_factor_sheet(self):
        # db exists but has no 'factors' sheet yet -> empty.
        df = mio.load_factors(self.db, device="TestScope")
        self.assertTrue(df.empty)


class TestPlotDeviceHistory(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "db.xlsx")
        self.plot_dir = os.path.join(self.tmpdir, "plots")
        os.mkdir(self.plot_dir)
        _write_calib_db(
            self.db,
            [
                (
                    "TestScope",
                    488.0,
                    100.0,
                    "2024-01-01",
                    "10:00",
                    {"bkg": 1.0, "amp": 40.0},
                ),
                (
                    "TestScope",
                    488.0,
                    100.0,
                    "2024-02-01",
                    "10:00",
                    {"bkg": 1.1, "amp": 41.0},
                ),
                (
                    "TestScope",
                    488.0,
                    200.0,
                    "2024-01-01",
                    "10:00",
                    {"bkg": 2.0, "amp": 60.0},
                ),
            ],
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_plot_device_history_creates_png(self):
        mio.plot_device_history(self.db, "TestScope", self.plot_dir)
        pngs = [f for f in os.listdir(self.plot_dir) if f.endswith(".png")]
        self.assertTrue(any(p.startswith("history_") for p in pngs))

    def test_plot_device_history_no_dir_is_noop(self):
        # Empty plot_dir -> skip without error.
        mio.plot_device_history(self.db, "TestScope", "")

    def test_plot_amplitude_history_creates_png(self):
        analyzer = man.LinearCurveAnalyzer({"min": 0.0, "max": 10.0})
        mio.plot_device_amplitude_history(
            self.db, "TestScope", self.plot_dir, analyzer
        )
        pngs = [f for f in os.listdir(self.plot_dir) if f.endswith(".png")]
        self.assertTrue(any(p.startswith("history_amplitude_") for p in pngs))

    def test_plot_amplitude_history_no_dir_is_noop(self):
        analyzer = man.LinearCurveAnalyzer({"min": 0.0, "max": 10.0})
        mio.plot_device_amplitude_history(self.db, "TestScope", "", analyzer)


class TestOffsetImmuneFactor(unittest.TestCase):
    """The offset-immune (slope) transmission-factor diagnostic."""

    ANA = {
        "classpath": "monet.analysis.LinearCurveAnalyzer",
        "init_kwargs": {"min": 0.0, "max": 180.0},
    }

    def test_slope_recovers_transmission_ignoring_offset(self):
        # P_sample = 0.3 * P_bfp + additive offset -> slope 0.3.
        x = np.linspace(0, 180, 50)
        p_bfp = 1.0 * x + 0.5
        p_sample = 0.3 * p_bfp + 4.0
        slope = mio._offset_immune_factor(p_sample, p_bfp)
        self.assertAlmostEqual(slope, 0.3, places=6)

    def test_slope_none_when_bfp_constant(self):
        self.assertIsNone(mio._offset_immune_factor([1.0, 2.0], [3.0, 3.0]))

    def test_pair_factor_warns_when_offset_biased(self):
        # Large additive offset in the sample plane biases the mean-of-ratios
        # factor away from the true slope (0.3) -> diagnostic warning.
        bfp = {"bkg": 0.5, "amp": 1.0}
        sample = {"bkg": 5.0, "amp": 0.3}
        with self.assertLogs("monet.io", level="WARNING") as cm:
            factor, n = mio.compute_pair_factor(sample, bfp, self.ANA)
        self.assertIsNotNone(factor)
        self.assertTrue(
            any("offset-immune slope" in m for m in cm.output),
            cm.output,
        )

    def test_pair_factor_no_warning_when_proportional(self):
        # Both planes scaled by the same 0.3 (offset scales too) -> no bias.
        bfp = {"bkg": 1.0, "amp": 1.0}
        sample = {"bkg": 0.3, "amp": 0.3}
        with self.assertNoLogs("monet.io", level="WARNING"):
            factor, n = mio.compute_pair_factor(sample, bfp, self.ANA)
        self.assertAlmostEqual(factor, 0.3, places=3)


class TestComputeAndSaveFactor(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "db.xlsx")
        self.ana_config = {
            "classpath": "monet.analysis.LinearCurveAnalyzer",
            "init_kwargs": {"min": 0.0, "max": 180.0},
        }

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_file_is_noop(self):
        # No exception, just an early return.
        mio.compute_and_save_factor(self.db, "TestScope", 488, self.ana_config)
        self.assertFalse(os.path.exists(self.db))

    def test_missing_powermeter_type_column(self):
        _write_calib_db(
            self.db,
            [
                (
                    "TestScope",
                    488.0,
                    100.0,
                    "2024-01-01",
                    "10:00",
                    {"bkg": 0.1, "amp": 40.0},
                ),
            ],
        )
        mio.compute_and_save_factor(self.db, "TestScope", 488, self.ana_config)
        # No 'factors' sheet should have been created.
        self.assertTrue(mio.load_factors(self.db).empty)

    def test_happy_path_writes_factor(self):
        today = datetime.now().strftime("%Y-%m-%d")
        _write_calib_db(
            self.db,
            [
                (
                    "TestScope",
                    488.0,
                    100.0,
                    today,
                    "10:00",
                    {"bkg": 0.1, "amp": 40.0, "powermeter_type": "sample"},
                ),
                (
                    "TestScope",
                    488.0,
                    100.0,
                    today,
                    "10:05",
                    {"bkg": 0.05, "amp": 20.0, "powermeter_type": "bfp"},
                ),
            ],
        )
        mio.compute_and_save_factor(self.db, "TestScope", 488, self.ana_config)
        df = mio.load_factors(self.db, device="TestScope", laser=488)
        self.assertEqual(len(df), 1)
        # sample/bfp amplitude ratio is ~2.0.
        self.assertAlmostEqual(
            df.iloc[0]["transmission_objective_mean"], 2.0, places=1
        )


class TestMadOutlierMask(unittest.TestCase):

    def test_flags_single_outlier(self):
        vals = [1.0, 1.1, 0.9, 1.05, 0.95, 10.0]
        mask = mio.mad_outlier_mask(vals)
        self.assertTrue(mask[-1])
        self.assertFalse(mask[:-1].any())

    def test_too_few_points_no_flags(self):
        self.assertFalse(mio.mad_outlier_mask([1.0, 100.0]).any())

    def test_zero_mad_no_flags(self):
        # Constant data -> MAD 0 -> nothing flagged (never drops everything).
        self.assertFalse(mio.mad_outlier_mask([2.0, 2.0, 2.0, 2.0]).any())

    def test_handles_nan(self):
        mask = mio.mad_outlier_mask([1.0, 1.1, 0.9, 1.0, np.nan, 9.0])
        self.assertFalse(bool(mask[4]))  # NaN is never an outlier
        self.assertTrue(bool(mask[5]))


class TestFlagAmplitudeOutliers(unittest.TestCase):

    def test_healthy_linear_no_flags(self):
        self.assertEqual(
            mio.flag_amplitude_outliers(
                [10, 20, 30, 40, 50], [100, 200, 300, 400, 500]
            ),
            {},
        )

    def test_single_high_outlier_collinear(self):
        # Perfectly collinear but one failed point: the robust Theil-Sen fit is
        # not dragged toward the outlier, so it stands out.
        out = mio.flag_amplitude_outliers(
            [10, 20, 30, 40, 50], [100, 200, 900, 400, 500]
        )
        self.assertEqual(set(out), {2})

    def test_single_low_outlier(self):
        out = mio.flag_amplitude_outliers(
            [10, 20, 30, 40, 50], [100, 200, 300, 400, 50]
        )
        self.assertEqual(set(out), {4})

    def test_noisy_but_ok_no_flags(self):
        # ~1 % scatter is below the 2 % threshold.
        self.assertEqual(
            mio.flag_amplitude_outliers(
                [10, 20, 30, 40, 50], [101, 201, 299, 401, 499]
            ),
            {},
        )

    def test_just_under_two_percent_no_flag(self):
        # 30 mW point 1.5 % high off a 10x line -> not flagged.
        out = mio.flag_amplitude_outliers(
            [10, 20, 30, 40, 50], [100, 200, 300 * 1.015, 400, 500]
        )
        self.assertEqual(out, {})

    def test_just_over_two_percent_flagged(self):
        # 30 mW point 3 % high off a 10x line -> flagged.
        out = mio.flag_amplitude_outliers(
            [10, 20, 30, 40, 50], [100, 200, 300 * 1.03, 400, 500]
        )
        self.assertEqual(set(out), {2})

    def test_noisy_with_outlier(self):
        out = mio.flag_amplitude_outliers(
            [10, 20, 30, 40, 50], [102, 199, 900, 398, 503]
        )
        self.assertEqual(set(out), {2})

    def test_too_few_points(self):
        self.assertEqual(mio.flag_amplitude_outliers([10, 20], [100, 200]), {})


class TestLoadAmplitudeHistory(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "db.xlsx")
        self.analyzer = man.LinearCurveAnalyzer({"min": 0.0, "max": 180.0})

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _rows(self, dates, ptype=None):
        rows = []
        for d in dates:
            for lpwr, amp in ((100.0, 40.0), (200.0, 60.0)):
                params = {"bkg": 0.0, "amp": amp}
                if ptype is not None:
                    params["powermeter_type"] = ptype
                rows.append(("TestScope", 488.0, lpwr, d, "10:00", params))
        return rows

    def test_returns_amplitudes_per_run(self):
        _write_calib_db(self.db, self._rows(["2024-01-01"]))
        hist = mio.load_amplitude_history(self.db, "TestScope", self.analyzer)
        self.assertIn("488", hist)
        runs = hist["488"]
        self.assertEqual(len(runs), 1)
        amps = runs[0]["amplitudes"]
        # output_range max = bkg + amp*max = 40*180 and 60*180.
        self.assertAlmostEqual(amps[100.0], 40.0 * 180.0, places=3)
        self.assertAlmostEqual(amps[200.0], 60.0 * 180.0, places=3)

    def test_max_runs_keeps_latest(self):
        _write_calib_db(
            self.db,
            self._rows(
                ["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"]
            ),
        )
        hist = mio.load_amplitude_history(
            self.db, "TestScope", self.analyzer, max_runs=2
        )
        dates = [run["date"] for run in hist["488"]]
        self.assertEqual(dates, ["2024-03-01", "2024-04-01"])

    def test_powermeter_type_filter(self):
        rows = self._rows(["2024-01-01"], ptype="sample")
        rows += self._rows(["2024-02-01"], ptype="bfp")
        _write_calib_db(self.db, rows)
        hist = mio.load_amplitude_history(
            self.db, "TestScope", self.analyzer, powermeter_type="bfp"
        )
        dates = [run["date"] for run in hist["488"]]
        self.assertEqual(dates, ["2024-02-01"])

    def test_missing_file_empty(self):
        hist = mio.load_amplitude_history(
            os.path.join(self.tmpdir, "nope.xlsx"), "TestScope", self.analyzer
        )
        self.assertEqual(hist, {})


class TestLoadCalibrationHistory(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "db.xlsx")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _rows(self, dates, ptype=None):
        rows = []
        for d in dates:
            for lpwr, amp in ((100.0, 40.0), (200.0, 60.0)):
                params = {"bkg": 0.0, "amp": amp}
                if ptype is not None:
                    params["powermeter_type"] = ptype
                rows.append(("TestScope", 488.0, lpwr, d, "10:00", params))
        return rows

    def test_returns_params_keyed_by_float_wavelength(self):
        _write_calib_db(self.db, self._rows(["2024-01-01"]))
        hist = mio.load_calibration_history(self.db, "TestScope")
        # Key is a numeric wavelength, robust to str/float DB forms.
        self.assertIn(488.0, hist)
        runs = hist[488.0]
        self.assertEqual(len(runs), 1)
        powers = runs[0]["powers"]
        self.assertIn(100.0, powers)
        self.assertAlmostEqual(powers[100.0]["amp"], 40.0)
        # powermeter_type is not a model parameter and must be dropped.
        self.assertNotIn("powermeter_type", powers[100.0])

    def test_max_runs_and_pm_filter(self):
        rows = self._rows(["2024-01-01"], ptype="sample")
        rows += self._rows(["2024-02-01"], ptype="bfp")
        rows += self._rows(["2024-03-01"], ptype="bfp")
        _write_calib_db(self.db, rows)
        hist = mio.load_calibration_history(
            self.db, "TestScope", powermeter_type="bfp", max_runs=1
        )
        self.assertEqual([r["date"] for r in hist[488.0]], ["2024-03-01"])

    def test_missing_file_empty(self):
        hist = mio.load_calibration_history(
            os.path.join(self.tmpdir, "nope.xlsx"), "TestScope"
        )
        self.assertEqual(hist, {})


class TestComputeFactorBreakdown(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "db.xlsx")
        self.ana_config = {
            "classpath": "monet.analysis.LinearCurveAnalyzer",
            "init_kwargs": {"min": 0.0, "max": 180.0},
        }

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _pair_rows(self, date, lpwr, amp_sample, amp_bfp):
        return [
            (
                "TestScope",
                488.0,
                lpwr,
                date,
                "10:00",
                {"bkg": 0.1, "amp": amp_sample, "powermeter_type": "sample"},
            ),
            (
                "TestScope",
                488.0,
                lpwr,
                date,
                "10:05",
                {"bkg": 0.05, "amp": amp_bfp, "powermeter_type": "bfp"},
            ),
        ]

    def test_one_row_per_input(self):
        rows = self._pair_rows("2024-06-01", 100.0, 40.0, 20.0)
        rows += self._pair_rows("2024-06-01", 200.0, 40.0, 20.0)
        rows += self._pair_rows("2024-07-01", 100.0, 40.0, 20.0)
        _write_calib_db(self.db, rows)
        df = mio.compute_factor_breakdown(
            self.db, "TestScope", self.ana_config
        )
        self.assertEqual(len(df), 3)
        self.assertTrue((df["factor"] > 1.9).all())
        self.assertTrue((df["factor"] < 2.1).all())
        self.assertEqual(
            set(zip(df["date"], df["laser_power"])),
            {
                ("2024-06-01", 100.0),
                ("2024-06-01", 200.0),
                ("2024-07-01", 100.0),
            },
        )

    def test_unpaired_date_skipped(self):
        # Only a sample-plane calibration on this date -> nothing to pair.
        rows = [
            (
                "TestScope",
                488.0,
                100.0,
                "2024-06-01",
                "10:00",
                {"bkg": 0.1, "amp": 40.0, "powermeter_type": "sample"},
            )
        ]
        _write_calib_db(self.db, rows)
        df = mio.compute_factor_breakdown(
            self.db, "TestScope", self.ana_config
        )
        self.assertTrue(df.empty)

    def test_missing_file_empty(self):
        df = mio.compute_factor_breakdown(
            os.path.join(self.tmpdir, "nope.xlsx"),
            "TestScope",
            self.ana_config,
        )
        self.assertTrue(df.empty)


class TestComputeAndSaveFactorOutliers(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "db.xlsx")
        self.ana_config = {
            "classpath": "monet.analysis.LinearCurveAnalyzer",
            "init_kwargs": {"min": 0.0, "max": 180.0},
        }

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _pair(self, lpwr, amp_sample, amp_bfp, today):
        return [
            (
                "TestScope",
                488.0,
                lpwr,
                today,
                "10:00",
                {"bkg": 0.2, "amp": amp_sample, "powermeter_type": "sample"},
            ),
            (
                "TestScope",
                488.0,
                lpwr,
                today,
                "10:05",
                {"bkg": 0.1, "amp": amp_bfp, "powermeter_type": "bfp"},
            ),
        ]

    def test_outlier_power_dropped(self):
        today = datetime.now().strftime("%Y-%m-%d")
        # Two consistent powers (ratio ~2.0, 2.05) and one failed run (~5.0).
        rows = self._pair(100.0, 40.0, 20.0, today)
        rows += self._pair(200.0, 41.0, 20.0, today)
        rows += self._pair(300.0, 100.0, 20.0, today)  # failed calibration
        _write_calib_db(self.db, rows)
        mio.compute_and_save_factor(self.db, "TestScope", 488, self.ana_config)
        df = mio.load_factors(self.db, device="TestScope", laser=488)
        self.assertEqual(len(df), 1)
        # Robust mean should stay near the good cluster (~2.0), not be pulled
        # toward 3.0 by the failed run.
        self.assertLess(df.iloc[0]["transmission_objective_mean"], 2.5)


class TestFactorPairs(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "db.xlsx")
        self.ana_config = {
            "classpath": "monet.analysis.LinearCurveAnalyzer",
            "init_kwargs": {"min": 0.0, "max": 180.0},
        }

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _pair(self, factor=2.0, power=100.0, s_time="10:00", b_time="10:05"):
        return {
            "device": "TestScope",
            "wavelength": 488.0,
            "laser_power": power,
            "date": "2024-06-01",
            "sample_time": s_time,
            "bfp_time": b_time,
            "factor": factor,
            "n_points": 50,
        }

    def test_compute_pair_factor(self):
        factor, n = mio.compute_pair_factor(
            {"bkg": 0.1, "amp": 40.0},
            {"bkg": 0.05, "amp": 20.0},
            self.ana_config,
        )
        self.assertAlmostEqual(factor, 2.0, places=1)
        self.assertGreater(n, 0)

    def test_save_and_load(self):
        mio.save_factor_pair(self.db, self._pair())
        df = mio.load_factor_pairs(self.db, device="TestScope")
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(df.iloc[0]["factor"], 2.0)
        self.assertEqual(df.iloc[0]["laser_power"], 100.0)

    def test_upsert_same_key(self):
        mio.save_factor_pair(self.db, self._pair(factor=2.0))
        mio.save_factor_pair(self.db, self._pair(factor=2.5))
        df = mio.load_factor_pairs(self.db)
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(df.iloc[0]["factor"], 2.5)

    def test_distinct_keys_coexist(self):
        mio.save_factor_pair(self.db, self._pair(power=100.0))
        mio.save_factor_pair(self.db, self._pair(power=200.0))
        self.assertEqual(len(mio.load_factor_pairs(self.db)), 2)

    def test_delete(self):
        p = self._pair()
        mio.save_factor_pair(self.db, p)
        removed = mio.delete_factor_pair(self.db, p)
        self.assertEqual(removed, 1)
        self.assertTrue(mio.load_factor_pairs(self.db).empty)

    def test_device_filter(self):
        mio.save_factor_pair(self.db, self._pair())
        other = self._pair()
        other["device"] = "OtherScope"
        mio.save_factor_pair(self.db, other)
        self.assertEqual(
            len(mio.load_factor_pairs(self.db, device="TestScope")), 1
        )
        self.assertEqual(len(mio.load_factor_pairs(self.db)), 2)

    def test_load_missing_is_empty(self):
        self.assertTrue(mio.load_factor_pairs(self.db).empty)


class TestCalibrationRuns(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "db.xlsx")
        self.ana_config = {
            "classpath": "monet.analysis.LinearCurveAnalyzer",
            "init_kwargs": {"min": 0.0, "max": 180.0},
        }

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _cal(self, wl, power, date, time, ptype, amp):
        return (
            "TestScope",
            float(wl),
            float(power),
            date,
            time,
            {"bkg": 0.0, "amp": float(amp), "powermeter_type": ptype},
        )

    def test_runs_split_by_powermeter_and_time_gap(self):
        rows = [
            # Sample-plane run A (10:00-10:05)
            self._cal(488, 100, "2024-06-01", "10:00", "sample", 40),
            self._cal(488, 200, "2024-06-01", "10:05", "sample", 80),
            # BFP run (10:20-10:25)
            self._cal(488, 100, "2024-06-01", "10:20", "bfp", 20),
            self._cal(488, 200, "2024-06-01", "10:25", "bfp", 40),
            # Sample-plane run B, hours later -> separate run
            self._cal(488, 100, "2024-06-01", "16:00", "sample", 41),
        ]
        _write_calib_db(self.db, rows)
        runs = mio.list_calibration_runs(self.db, "TestScope")
        # 2 sample runs + 1 bfp run
        self.assertEqual(len(runs), 3)
        samples = [r for r in runs if r["powermeter_type"] == "sample"]
        bfps = [r for r in runs if r["powermeter_type"] == "bfp"]
        self.assertEqual(len(samples), 2)
        self.assertEqual(len(bfps), 1)
        self.assertEqual(bfps[0]["n_cals"], 2)

    def test_back_to_back_identical_runs_split(self):
        # Three full runs done in a row (same wavelengths/powers, only minutes
        # apart) must be three separate runs, not merged into one.
        rows = []
        t = 0
        for _run in range(3):
            for wl in (488, 561):
                for pwr in (100, 200):
                    hh, mm = divmod(t, 60)
                    rows.append(
                        self._cal(
                            wl,
                            pwr,
                            "2024-06-01",
                            "{:02d}:{:02d}".format(10 + hh, mm),
                            "sample",
                            40,
                        )
                    )
                    t += 2  # 2 minutes between calibrations
        _write_calib_db(self.db, rows)
        runs = mio.list_calibration_runs(self.db, "TestScope")
        self.assertEqual(len(runs), 3)
        for r in runs:
            self.assertEqual(r["n_cals"], 4)
            self.assertEqual(r["wavelengths"], [488.0, 561.0])
            self.assertEqual(r["powers"], [100.0, 200.0])

    def test_amplitudes_regenerated_with_ana_config(self):
        rows = [
            self._cal(488, 100, "2024-06-01", "10:00", "sample", 40),
            self._cal(561, 200, "2024-06-01", "10:05", "sample", 80),
        ]
        _write_calib_db(self.db, rows)
        # Without ana_config, amplitudes are empty.
        runs = mio.list_calibration_runs(self.db, "TestScope")
        self.assertEqual(runs[0]["amplitudes"], {})
        # With ana_config, amplitudes = bkg + amp*max (max=180).
        runs = mio.list_calibration_runs(self.db, "TestScope", self.ana_config)
        amps = runs[0]["amplitudes"]
        self.assertAlmostEqual(amps[488.0][100.0], 40.0 * 180.0, places=2)
        self.assertAlmostEqual(amps[561.0][200.0], 80.0 * 180.0, places=2)

    def test_compute_run_pair_factors(self):
        rows = [
            self._cal(488, 100, "2024-06-01", "10:00", "sample", 40),
            self._cal(488, 200, "2024-06-01", "10:05", "sample", 80),
            self._cal(561, 100, "2024-06-01", "10:10", "sample", 30),
            self._cal(488, 100, "2024-06-01", "10:20", "bfp", 20),
            self._cal(488, 200, "2024-06-01", "10:25", "bfp", 40),
            # 561 only in sample -> not paired
        ]
        _write_calib_db(self.db, rows)
        runs = mio.list_calibration_runs(self.db, "TestScope")
        s_run = [r for r in runs if r["powermeter_type"] == "sample"][0]
        b_run = [r for r in runs if r["powermeter_type"] == "bfp"][0]
        stored = mio.compute_run_pair_factors(
            self.db, s_run, b_run, self.ana_config
        )
        # Only the two 488 nm powers pair (561 nm has no BFP partner).
        self.assertEqual(len(stored), 2)
        for p in stored:
            self.assertEqual(p["wavelength"], 488.0)
            self.assertAlmostEqual(p["factor"], 2.0, places=1)
        # And they now appear as stored pairs.
        self.assertEqual(len(mio.load_factor_pairs(self.db)), 2)

    def test_no_powermeter_type_no_runs(self):
        _write_calib_db(
            self.db,
            [("TestScope", 488.0, 100.0, "2024-06-01", "10:00", {"amp": 40})],
        )
        self.assertEqual(mio.list_calibration_runs(self.db, "TestScope"), [])

    def test_pair_records_carry_dates(self):
        rows = [
            self._cal(488, 100, "2024-06-01", "10:00", "sample", 40),
            self._cal(488, 100, "2024-06-02", "11:00", "bfp", 20),
        ]
        _write_calib_db(self.db, rows)
        runs = mio.list_calibration_runs(self.db, "TestScope")
        s = [r for r in runs if r["powermeter_type"] == "sample"][0]
        b = [r for r in runs if r["powermeter_type"] == "bfp"][0]
        stored = mio.compute_run_pair_factors(self.db, s, b, self.ana_config)
        self.assertEqual(stored[0]["sample_date"], "2024-06-01")
        self.assertEqual(stored[0]["sample_time"], "10:00")
        self.assertEqual(stored[0]["bfp_date"], "2024-06-02")
        self.assertEqual(stored[0]["bfp_time"], "11:00")

    def test_clear_factor_pairs(self):
        rows = [
            self._cal(488, 100, "2024-06-01", "10:00", "sample", 40),
            self._cal(488, 100, "2024-06-01", "10:20", "bfp", 20),
        ]
        _write_calib_db(self.db, rows)
        runs = mio.list_calibration_runs(self.db, "TestScope")
        s = [r for r in runs if r["powermeter_type"] == "sample"][0]
        b = [r for r in runs if r["powermeter_type"] == "bfp"][0]
        mio.compute_run_pair_factors(self.db, s, b, self.ana_config)
        self.assertEqual(len(mio.load_factor_pairs(self.db)), 1)
        self.assertEqual(mio.clear_factor_pairs(self.db, "TestScope"), 1)
        self.assertTrue(mio.load_factor_pairs(self.db).empty)

    def test_delete_calibration_run(self):
        rows = [
            self._cal(488, 100, "2024-06-01", "10:00", "sample", 40),
            self._cal(488, 200, "2024-06-01", "10:05", "sample", 80),
            # a second (bfp) run that must survive
            self._cal(488, 100, "2024-06-01", "10:30", "bfp", 20),
        ]
        _write_calib_db(self.db, rows)
        runs = mio.list_calibration_runs(self.db, "TestScope")
        s = [r for r in runs if r["powermeter_type"] == "sample"][0]
        deleted = mio.delete_calibration_run(self.db, s)
        self.assertEqual(deleted, 2)
        remaining = mio.list_calibration_runs(self.db, "TestScope")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["powermeter_type"], "bfp")


if __name__ == "__main__":
    unittest.main()
