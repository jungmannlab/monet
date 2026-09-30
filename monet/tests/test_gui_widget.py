"""
monet/tests/test_gui_widget.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Smoke tests for the embeddable :class:`MonetWidget` and its tabs.
Skipped automatically when PyQt6 is not installed.
"""

import os
import unittest

import pytest

pytest.importorskip("PyQt6")

# Headless backend for environments without a real display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from monet.gui import (  # noqa: E402
    AdjustTab,
    CalibrateTab,
    CalibrationPlots,
    DatabaseTab,
    MonetWidget,
    SetPowerTab,
)

# A single QApplication is required for any QWidget construction.
_app = QApplication.instance() or QApplication([])


class TestMonetWidget(unittest.TestCase):

    def test_construct_without_toolbar(self):
        """show_toolbar=False yields a widget with no microscope combo."""
        w = MonetWidget(show_toolbar=False, tabs=("set_power",))
        self.assertIsNone(w._scope_combo)
        self.assertIsNone(w._btn_connect)
        self.assertIsNotNone(w.tab("set_power"))
        self.assertIsNone(w.tab("calibrate"))
        self.assertIsNone(w.current_microscope)

    def test_construct_with_toolbar(self):
        """show_toolbar=True (default) builds the microscope picker."""
        w = MonetWidget(show_toolbar=True, tabs=("set_power", "database"))
        self.assertIsNotNone(w._scope_combo)
        self.assertIsNotNone(w._btn_connect)
        self.assertIsNotNone(w.tab("database"))

    def test_status_signal_bubbles_up(self):
        """A tab's ``status`` signal re-emits as status_changed."""
        w = MonetWidget(show_toolbar=False, tabs=("set_power",))
        received = []
        w.status_changed.connect(lambda msg, t: received.append((msg, t)))
        w.tab("set_power").status.emit("hello", 1234)
        self.assertEqual(received, [("hello", 1234)])

    def test_unknown_tab_key_raises(self):
        with self.assertRaises(ValueError):
            MonetWidget(show_toolbar=False, tabs=("nonexistent",))

    def test_individual_tabs_are_standalone(self):
        """Each tab can be constructed without a parent / main window."""
        for cls in (SetPowerTab, CalibrateTab, AdjustTab, DatabaseTab):
            tab = cls()
            received = []
            tab.status.connect(lambda msg, t: received.append((msg, t)))
            tab._emit_status("ping", 500)
            self.assertEqual(received, [("ping", 500)])


class TestMeasureReadiness(unittest.TestCase):
    """The Measure-button readiness hint (why a reading may be ~0)."""

    def test_no_laser_selected_is_silent(self):
        ready, short, _ = SetPowerTab._measure_readiness(
            None, False, True, False
        )
        self.assertIsNone(ready)
        self.assertEqual(short, "")

    def test_laser_off_warns(self):
        ready, short, detail = SetPowerTab._measure_readiness(
            561, False, True, True
        )
        self.assertFalse(ready)
        self.assertIn("OFF", short)
        self.assertIn("zero", detail.lower())

    def test_no_beampath_preset_warns(self):
        ready, short, detail = SetPowerTab._measure_readiness(
            561, True, True, False
        )
        self.assertFalse(ready)
        self.assertIn("beam-path", short)
        self.assertIn("561", detail)

    def test_ready_with_preset(self):
        ready, short, detail = SetPowerTab._measure_readiness(
            561, True, True, True
        )
        self.assertTrue(ready)
        self.assertIn("light expected", short)
        self.assertIn("561", detail)

    def test_ready_without_beampath_hardware(self):
        # No beam path configured at all -> laser-on is enough to expect light.
        ready, short, _ = SetPowerTab._measure_readiness(
            488, True, False, False
        )
        self.assertTrue(ready)
        self.assertIn("light expected", short)

    def test_filter_mismatch_warns(self):
        ready, short, detail = SetPowerTab._measure_readiness(
            561, True, True, True, filter_state="mismatch"
        )
        self.assertFalse(ready)
        self.assertIn("filter cube", short)
        self.assertIn("561", detail)

    def test_objective_in_path_for_bfp_warns_sample_needed(self):
        ready, short, detail = SetPowerTab._measure_readiness(
            561, True, True, True, turret_state="objective_but_bfp"
        )
        self.assertFalse(ready)
        self.assertIn("sample position", short)
        self.assertIn("sample", detail.lower())

    def test_filter_warning_takes_priority_over_turret(self):
        # Both wrong -> the filter (excitation blocked) is reported first.
        ready, short, _ = SetPowerTab._measure_readiness(
            561,
            True,
            True,
            True,
            filter_state="mismatch",
            turret_state="objective_but_bfp",
        )
        self.assertFalse(ready)
        self.assertIn("filter cube", short)

    def test_ready_when_filter_and_turret_ok(self):
        ready, short, _ = SetPowerTab._measure_readiness(
            561, True, True, True, filter_state="ok", turret_state="ok"
        )
        self.assertTrue(ready)
        self.assertIn("light expected", short)


class TestCalibrateTabVerify(unittest.TestCase):
    """Fit-quality surfacing and the Verify button on the Calibrate tab."""

    def test_verify_button_exists_and_starts_disabled(self):
        tab = CalibrateTab()
        self.assertIsNotNone(tab._btn_verify)
        self.assertFalse(tab._btn_verify.isEnabled())

    def test_compare_button_exists_and_starts_disabled(self):
        tab = CalibrateTab()
        self.assertIsNotNone(tab._btn_compare)
        self.assertFalse(tab._btn_compare.isEnabled())

    def test_apply_model_button_exists_and_starts_disabled(self):
        tab = CalibrateTab()
        self.assertIsNotNone(tab._btn_apply_model)
        self.assertFalse(tab._btn_apply_model.isEnabled())

    def test_fit_quality_text_plain(self):
        q = {"rms_pct": 1.2, "max_pct": 3.4, "max_at": 104.5}
        txt = CalibrateTab._fit_quality_text(q)
        self.assertIn("1.2%", txt)
        self.assertIn("3.4%", txt)
        self.assertNotIn("⚠", txt)

    def test_fit_quality_text_warns_when_large(self):
        q = {"rms_pct": 9.0, "max_pct": 15.0, "max_at": 100.0}
        txt = CalibrateTab._fit_quality_text(q)
        self.assertIn("⚠", txt)
        self.assertIn("deviate", txt)


class TestCalibrationPlots(unittest.TestCase):
    """Regression tests for the live calibration plots / wavelength toggles."""

    _ANA = {
        "classpath": "monet.analysis.LinearCurveAnalyzer",
        "init_kwargs": {"min": 0.0, "max": 180.0},
    }

    def test_toggle_wavelength_during_active_curve(self):
        """Toggling the wavelength being calibrated must not crash.

        Regression: a stale extra argument to ``_draw_curve`` raised a
        TypeError when the wavelength of the in-progress curve was toggled off.
        """
        if not getattr(CalibrationPlots, "_has_mpl", True):
            self.skipTest("matplotlib not available")
        plots = CalibrationPlots()
        if not plots._has_mpl:
            self.skipTest("matplotlib not available")
        plots.set_history({}, self._ANA)
        plots.add_curve(488, 50, [0, 90, 180], [1.0, 20.0, 40.0])
        plots.add_curve(561, 50, [0, 90, 180], [1.0, 15.0, 30.0])
        # an active (in-progress) curve for 488 nm
        for i, (c, p) in enumerate([(0, 0.5), (90, 25.0), (180, 50.0)]):
            plots.add_point(488, 100, i, 3, c, p)
        self.assertEqual(plots._curve_key, (488, 100))
        # toggling the active wavelength off (and back on) must not raise
        plots._wl_toggles[488.0].setChecked(False)
        plots._wl_toggles[488.0].setChecked(True)
        # a history update while a curve is active must not raise either
        plots.set_history(
            {
                488.0: [
                    {
                        "date": "2024-06-01",
                        "powers": {100.0: {"bkg": 0.0, "amp": 45.0}},
                    }
                ]
            },
            self._ANA,
        )


if __name__ == "__main__":
    unittest.main()
