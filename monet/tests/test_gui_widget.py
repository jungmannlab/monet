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
    DoctorTab,
    MonetMainWindow,
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

    def test_calibrate_tab_has_model_selector(self):
        tab = CalibrateTab()
        datas = [
            tab._model_combo.itemData(i)
            for i in range(tab._model_combo.count())
        ]
        self.assertIn("sinus", datas)
        self.assertIn("poly deg 5", datas)
        self.assertIn("linear", datas)
        self.assertIsNotNone(tab._btn_set_default)
        self.assertFalse(tab._btn_set_default.isEnabled())

    def test_model_combo_shows_current_and_annotates_default(self):
        import monet.gui as g

        tab = CalibrateTab()

        class _Inst:
            config = {
                "analysis": {
                    "classpath": "monet.analysis."
                    "PolynomAttenuationCurveAnalyzer",
                    "init_kwargs": {"polydegree": 5},
                }
            }

        class _PC:
            instrument = _Inst()
            _microscope_name = "__unittest_scope__"

        g.CONFIGS["__unittest_scope__"] = {
            "analysis": {
                "classpath": "monet.analysis." "SinusAttenuationCurveAnalyzer",
                "init_kwargs": {},
            }
        }
        try:
            tab._pc = _PC()
            tab._sync_model_combo()
            # the dropdown reflects the session model (poly deg 5)
            self.assertEqual(tab._model_combo.currentData(), "poly deg 5")
            # the persisted default (sinus) is annotated "(default)"
            texts = [
                tab._model_combo.itemText(i)
                for i in range(tab._model_combo.count())
            ]
            self.assertTrue(
                any("Sinusoidal (default)" == t for t in texts), texts
            )
        finally:
            g.CONFIGS.pop("__unittest_scope__", None)


class TestExpertView(unittest.TestCase):
    """The expert-view toggle hides advanced Set Power controls."""

    def _hidden(self, tab):
        return (
            tab._btn_backlash.isHidden(),
            tab._btn_hw_refresh.isHidden(),
            tab._hw_att_group.isHidden(),
            tab._hw_pwr_group.isHidden(),
        )

    def test_default_is_normal_view(self):
        tab = SetPowerTab()
        self.assertTrue(all(self._hidden(tab)))

    def test_toggle_shows_and_hides(self):
        tab = SetPowerTab()
        tab.set_expert_view(True)
        self.assertFalse(any(self._hidden(tab)))
        tab.set_expert_view(False)
        self.assertTrue(all(self._hidden(tab)))

    def test_widget_toggle_propagates_to_tabs(self):
        w = MonetWidget(show_toolbar=True, tabs=("set_power",))
        sp = w.tab("set_power")
        self.assertTrue(sp._btn_backlash.isHidden())  # default normal
        w.set_expert_view(True)
        self.assertFalse(sp._btn_backlash.isHidden())
        self.assertTrue(w._expert_cb.isChecked())

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


class TestLaserStateControls(unittest.TestCase):
    """All-lasers on/off buttons, live startup-power display, and the
    post-calibration laser-toggle refresh on SetPowerTab."""

    class _Laser:
        def __init__(self, enabled=False, power=0.0):
            self.enabled = enabled
            self.power = power

    class _Att:
        def curr_pos(self):
            return 2.5

    class _Inst:
        def __init__(self, lasers, saved=None):
            self.lasers = lasers
            self.curr_laser = next(iter(lasers))
            self.attenuator = TestLaserStateControls._Att()
            self.is_calibrated = False
            self.powermeter_position = None
            self._saved = saved

        def load_calibration_database(self):
            pass

        def saved_state(self, laser):
            return self._saved

    class _PC:
        def __init__(self, inst):
            self.instrument = inst

    def test_all_on_and_off_buttons_present(self):
        tab = SetPowerTab()
        self.assertEqual(tab._btn_allon.text(), "All lasers ON")
        self.assertEqual(tab._btn_alloff.text(), "All lasers OFF")

    def test_sync_laser_toggle_reflects_enabled_state(self):
        tab = SetPowerTab()
        lasers = {488: self._Laser(enabled=True)}
        tab._pc = self._PC(self._Inst(lasers))
        tab._sync_laser_toggle(488)
        self.assertTrue(tab._btn_onoff.isChecked())
        self.assertEqual(tab._btn_onoff.text(), "switch OFF")
        lasers[488].enabled = False
        tab._sync_laser_toggle(488)
        self.assertFalse(tab._btn_onoff.isChecked())
        self.assertEqual(tab._btn_onoff.text(), "switch ON")

    def test_refresh_laser_toggle_clears_stale_on_after_calibration(self):
        # A calibration run leaves the lasers off; the toggle (left showing
        # "on" for the selected laser) must be re-synced to off.
        tab = SetPowerTab()
        lasers = {488: self._Laser(enabled=False)}
        tab._pc = self._PC(self._Inst(lasers))
        tab._laser_combo.addItem("488", 488)  # currentData() -> 488
        tab._btn_onoff.setChecked(True)
        tab._btn_onoff.setText("switch OFF")
        tab.refresh_laser_toggle()
        self.assertFalse(tab._btn_onoff.isChecked())
        self.assertEqual(tab._btn_onoff.text(), "switch ON")

    def test_set_pc_shows_live_power_not_stale_saved_setpoint(self):
        # On connect the direct "Power (mW)" must reflect the live hardware
        # power (~10 mW idle), not a stale saved set-point of 0.
        tab = SetPowerTab()
        lasers = {488: self._Laser(enabled=True, power=10.0)}
        inst = self._Inst(
            lasers, saved={"laser_power": 0.0, "attenuator": 1.0}
        )
        tab.set_pc(self._PC(inst))
        self.assertEqual(tab._hw_pwr_spin.value(), 10.0)


class TestDoctorTab(unittest.TestCase):
    def test_report_lists_config_locations(self):
        """The diagnostics report names the key config locations."""
        tab = DoctorTab()
        txt = tab.report_text()
        self.assertIn("monet diagnostics", txt)
        self.assertIn("monet version", txt)
        self.assertIn(".env file", txt)
        self.assertIn("configs loaded", txt)
        self.assertIn("Calibration server", txt)

    def test_server_section_renders_status_dict(self):
        """A probed server status dict is rendered into the report."""
        tab = DoctorTab()
        tab._server_url = "https://server.example/monet"
        tab._server_status = {
            "reachable": True,
            "auth_enabled": True,
            "token_present": True,
            "authenticated": True,
            "scope": "write",
            "label": "microscope-mercury",
            "ok": True,
            "detail": "authenticated as microscope-mercury",
        }
        txt = tab.report_text()
        self.assertIn("https://server.example/monet", txt)
        self.assertIn("write / microscope-mercury", txt)
        self.assertIn("OK", txt)

    def test_no_server_configured_message(self):
        tab = DoctorTab()
        tab._server_url = None
        self.assertIn("No server configured", tab.report_text())

    def test_is_standalone_tab(self):
        """DoctorTab is in the catalog and constructs without a parent."""
        self.assertIn("doctor", MonetWidget._TAB_CATALOG)
        w = MonetWidget(show_toolbar=False, tabs=("doctor",))
        self.assertIsNotNone(w.tab("doctor"))


class TestMainWindowChrome(unittest.TestCase):
    def test_main_window_includes_doctor_tab_and_help_menu(self):
        win = MonetMainWindow(initial_microscope=None)
        tabs = [
            win._widget._tabs.tabText(i)
            for i in range(win._widget._tabs.count())
        ]
        self.assertIn("Doctor", tabs)
        menus = [a.text() for a in win.menuBar().actions()]
        self.assertIn("&Help", menus)
        self.assertIn("&Setup", menus)


if __name__ == "__main__":
    unittest.main()
