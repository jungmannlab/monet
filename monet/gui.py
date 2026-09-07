#!/usr/bin/env python
"""
monet/gui.py
~~~~~~~~~~~~

PyQt6 graphical interface for Monet. Provides tabs for calibration,
laser/attenuator adjustment, power setting, and database management.

:authors: Heinrich Grabmayr, 2024
:copyright: Copyright (c) 2024 Jungmann Lab, MPI of Biochemistry
"""

import logging

import numpy as np

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import monet.io as io
from monet import (
    CONFIGS,
    LASER_TAG,
    POWER_TAG,
    POWERMETER_BFP,
    POWERMETER_SAMPLE,
    PROTOCOLS,
)
from monet import __version__ as _monet_version
from monet.beampath import NikonNosepiece
from monet.control import run_power_feedback
from monet.util import update_mm_acquisition_comment, wavelength_to_rgb

# Window-title prefix, e.g. 'Monet v0.3.3'. Falls back to plain 'Monet' when
# the package metadata is unavailable (running from an uninstalled source
# tree).
_TITLE_BASE = (
    "Monet"
    if _monet_version == "unknown"
    else "Monet v{}".format(_monet_version)
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------


class CalibrationWorker(QThread):
    """Runs CalibrationProtocol2D.run_protocol() in a background thread."""

    progress = pyqtSignal(int, int, object, object)  # step, total, laser, lpwr
    # laser, lpwr, control values, measured powers
    curve = pyqtSignal(object, object, object, object)
    # laser, lpwr, index, total, control value, measured power
    point = pyqtSignal(object, object, int, int, object, object)
    log_message = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(
        self,
        pc,
        laser_filter,
        wait_time=0.1,
        switch_time=10,
        powermeter_type=POWERMETER_SAMPLE,
        manage_laser_state=True,
        power_filter=None,
        comment=None,
    ):
        super().__init__()
        self._pc = pc
        self._laser_filter = laser_filter
        self._wait_time = wait_time
        self._switch_time = switch_time
        self._powermeter_type = powermeter_type
        self._manage_laser_state = manage_laser_state
        self._power_filter = power_filter
        self._comment = comment
        self._cancel_requested = False

    def request_cancel(self):
        self._cancel_requested = True

    def run(self):
        def _progress_callback(step, total, laser, lpwr):
            if self._cancel_requested:
                raise InterruptedError("Calibration cancelled by user.")
            self.progress.emit(step, total, laser, lpwr)
            self.log_message.emit(
                f"Step {step}/{total}: laser {laser} nm at {lpwr} mW done."
            )

        def _curve_callback(laser, lpwr, ctrl_vals, powers):
            self.curve.emit(laser, lpwr, ctrl_vals, powers)

        def _point_callback(laser, lpwr, index, total, ctrl_val, power):
            self.point.emit(laser, lpwr, index, total, ctrl_val, power)

        try:
            self._pc.run_protocol(
                wait_time=self._wait_time,
                switch_time=self._switch_time,
                laser_filter=self._laser_filter,
                progress_callback=_progress_callback,
                curve_callback=_curve_callback,
                point_callback=_point_callback,
                manage_laser_state=self._manage_laser_state,
                powermeter_type=self._powermeter_type,
                power_filter=self._power_filter,
                comment=self._comment,
            )
        except InterruptedError:
            self.log_message.emit("Calibration cancelled.")
        except Exception as exc:
            self.error.emit(str(exc))
            return
        self.finished.emit()


class ConnectWorker(QThread):
    """Connects to a microscope configuration in a background thread."""

    connected = pyqtSignal(object)  # calibration protocol object
    warning = pyqtSignal(str)  # non-fatal warning (e.g. no powermeter)
    error = pyqtSignal(str)

    def __init__(self, name, config, protocol, old_pc=None):
        super().__init__()
        self._name = name
        self._config = config
        self._protocol = protocol
        self._old_pc = old_pc

    def run(self):
        import monet.calibrate as mca

        try:
            # Release the previous connection first so its serial ports /
            # SDK sessions are freed; otherwise re-opening the same hardware
            # (e.g. after switching on another laser) fails with the ports
            # still held by the old instance. Done here (worker thread) so
            # the potentially-blocking close does not freeze the UI.
            if self._old_pc is not None:
                try:
                    self._old_pc.disconnect()
                except Exception:
                    logging.getLogger(__name__).debug(
                        "old connection teardown failed", exc_info=True
                    )

            if self._protocol:
                pc = mca.CalibrationProtocol2D(self._config, self._protocol)
            else:
                pc = mca.CalibrationProtocol1D(self._config)
            if not getattr(pc, "powermeter_available", True):
                msg = (
                    "PowerMeter not available — calibration and power "
                    "measurement disabled."
                )
                detail = getattr(pc, "powermeter_error", None)
                if detail:
                    msg += "\n\nReason: " + detail
                self.warning.emit(msg)
            self.connected.emit(pc)
        except Exception as exc:
            self.error.emit(str(exc))


class GenericWorker(QThread):
    """Runs any callable in a background thread.

    Emits result(object) on success, error(str) on failure.
    The function's return value is passed to result; None is emitted if the
    function returns nothing.  progress(object) is available for intermediate
    updates emitted during the run.
    """

    result = pyqtSignal(object)
    progress = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, func):
        super().__init__()
        self._func = func

    def run(self):
        try:
            val = self._func()
            self.result.emit(val)
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Feedback live-plot dialog
# ---------------------------------------------------------------------------


class FeedbackPlotDialog(QDialog):
    """Non-modal dialog showing measured power and setpoint vs. iteration
    during the feedback control loop.  Updated via add_point() which is
    connected to GenericWorker.progress (queued cross-thread connection).
    """

    def __init__(self, target_pwr, max_dev_pct, mode, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Feedback — live progress")
        self.setModal(False)
        self.resize(540, 380)

        self._target = target_pwr
        self._tol = max_dev_pct
        self._mode = mode  # 'fixed_laser' or 'fixed_attenuator'
        self._iters = []
        self._measured_vals = []
        self._setpoint_vals = []

        layout = QVBoxLayout(self)

        try:
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
            from matplotlib.figure import Figure

            self._fig = Figure(figsize=(5.4, 3.5), tight_layout=True)
            self._ax = self._fig.add_subplot(111)
            self._canvas = FigureCanvasQTAgg(self._fig)
            layout.addWidget(self._canvas)
            self._has_mpl = True
            self._init_plot()
        except Exception:
            self._has_mpl = False
            layout.addWidget(
                QLabel("matplotlib not available — cannot show live plot.")
            )

        btn = QPushButton("Close")
        btn.clicked.connect(self.close)
        layout.addWidget(btn)

    def _init_plot(self):
        ax = self._ax
        lo = self._target * (1.0 - self._tol / 100.0)
        hi = self._target * (1.0 + self._tol / 100.0)

        ax.axhline(
            self._target,
            color="green",
            linestyle="--",
            linewidth=1.5,
            label=f"Target ({self._target:.2f} mW)",
            zorder=2,
        )
        ax.axhspan(
            lo, hi, alpha=0.15, color="green", label=f"±{self._tol:.1f}% band"
        )

        (self._line_meas,) = ax.plot(
            [], [], "o-", color="royalblue", label="Measured", zorder=3
        )
        # Setpoint line only meaningful for attenuator mode (PI corrects
        # target)
        (self._line_set,) = ax.plot(
            [],
            [],
            "s--",
            color="darkorange",
            alpha=0.7,
            label="PI setpoint",
            visible=(self._mode == "fixed_laser"),
        )

        ax.set_xlabel("Iteration")
        ax.set_ylabel("Power (mW)")
        ax.set_title("Feedback convergence")
        ax.legend(loc="best", fontsize=8)
        self._canvas.draw()

    def add_point(self, data):
        """Slot connected to GenericWorker.progress (called on main thread)."""
        if not self._has_mpl:
            return
        iteration, setpoint, measured = data
        self._iters.append(iteration)
        self._measured_vals.append(measured)
        self._setpoint_vals.append(setpoint)

        self._line_meas.set_data(self._iters, self._measured_vals)
        self._line_set.set_data(self._iters, self._setpoint_vals)

        # Auto-scale y to data + target band
        all_y = self._measured_vals + self._setpoint_vals + [self._target]
        margin = max(abs(self._target) * 0.05, 0.01)
        self._ax.set_xlim(-0.5, max(self._iters) + 0.5)
        self._ax.set_ylim(min(all_y) - margin, max(all_y) + margin)

        self._canvas.draw_idle()


# ---------------------------------------------------------------------------
# Calibration live plots
# ---------------------------------------------------------------------------


class CalibrationPlots(QWidget):
    """Live plots shown alongside the calibration log.

    Left axes: the attenuation curve (power vs. attenuator control value) of
    the power step just measured. Right axes: the amplitude — the maximum
    measured power — of every curve of the run, against the laser power
    set-point, with one series per wavelength.

    Previous calibration runs (loaded via :meth:`set_history`) are overlaid as
    thin faded lines in *both* plots — the amplitude-vs-power trend and, for the
    conditions currently being measured, the attenuation curve — regenerated
    from the stored fit parameters (the raw points are not kept). A row of
    per-wavelength toggle buttons above the plots selects which wavelengths to
    show, so a wavelength with very low powers can be viewed on its own
    rescaled axes. Live amplitude points that stray from a robust linear fit are
    flagged in red and reported via :attr:`flagged_changed`.

    The curve grows point by point via ``add_point``; ``add_curve`` closes it
    off at the end of a power step and records its amplitude. Both are
    connected to ``CalibrationWorker`` signals and so run on the main thread.
    """

    # Emits the list of currently-flagged points as
    # [{"laser": <orig>, "lpwr": float, "residual_frac": float}, ...].
    flagged_changed = pyqtSignal(object)

    # A point is flagged when it deviates from the robust linear fit by more
    # than this fraction (see io.flag_amplitude_outliers).
    OUTLIER_REL_THRESH = 0.02

    def __init__(self, parent=None):
        super().__init__(parent)
        # {laser (original protocol key): {laser power: max measured power}}
        self._amplitudes = {}
        # {wavelength (float): [{"date": str, "powers": {power: fit params}}]}
        self._history = {}
        # {wavelength (float): [{"date": str, "amplitudes": {power: max}}]}
        self._history_amps = {}
        # {laser: set(lpwr)} flagged as outliers in the current run
        self._flagged = {}
        self._ana_config = None
        self._analyzer = None
        # {wavelength (float): checkable QPushButton}
        self._wl_toggles = {}
        # (laser, lpwr) of the curve currently being acquired, or None
        self._curve_key = None
        self._curve_line = None
        self._curve_x = []
        self._curve_y = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # Per-wavelength show/hide toggles (populated as wavelengths appear).
        self._toggle_bar = QWidget()
        self._toggle_layout = QHBoxLayout(self._toggle_bar)
        self._toggle_layout.setContentsMargins(0, 0, 0, 0)
        self._toggle_layout.addWidget(QLabel("Show:"))
        self._toggle_layout.addStretch()
        self._toggle_bar.setVisible(False)
        root.addWidget(self._toggle_bar)

        try:
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
            from matplotlib.figure import Figure
        except Exception:
            self._has_mpl = False
            root.addWidget(
                QLabel("matplotlib not available — cannot show plots.")
            )
            return

        self._has_mpl = True
        self._curve_fig = Figure(figsize=(3.4, 2.8), tight_layout=True)
        self._curve_ax = self._curve_fig.add_subplot(111)
        self._curve_canvas = FigureCanvasQTAgg(self._curve_fig)

        self._amp_fig = Figure(figsize=(3.4, 2.8), tight_layout=True)
        self._amp_ax = self._amp_fig.add_subplot(111)
        self._amp_canvas = FigureCanvasQTAgg(self._amp_fig)

        canvas_row = QHBoxLayout()
        for canvas in (self._curve_canvas, self._amp_canvas):
            canvas.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            canvas_row.addWidget(canvas)
        root.addLayout(canvas_row)

        self.clear()

    # ── wavelength helpers ────────────────────────────────────────────────
    @staticmethod
    def _canon(wl):
        """Canonical (float) wavelength key; original value if non-numeric."""
        try:
            return float(wl)
        except (TypeError, ValueError):
            return wl

    def _laser_color(self, laser):
        """Approximate visible-spectrum color for this wavelength."""
        return wavelength_to_rgb(self._canon(laser))

    def _all_wavelengths(self):
        """Sorted union of live and history wavelengths (canonical floats)."""
        keys = set()
        for k in self._amplitudes:
            c = self._canon(k)
            if isinstance(c, float):
                keys.add(c)
        keys |= set(self._history_amps) | set(self._history)
        return sorted(keys)

    def _is_visible(self, wl):
        btn = self._wl_toggles.get(self._canon(wl))
        return btn.isChecked() if btn is not None else True

    def _rebuild_toggles(self):
        wls = self._all_wavelengths()
        for wl in wls:
            if wl in self._wl_toggles:
                continue
            btn = QPushButton("{:g} nm".format(wl))
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.setMaximumWidth(90)
            btn.setToolTip("Show/hide this wavelength in the plots")
            # White fill with a coloured outline when shown; greyed out when
            # hidden — never the platform's default filled/blue checked look.
            color = self._laser_color(wl)
            btn.setStyleSheet(
                "QPushButton {{"
                " background-color: white; color: #999;"
                " border: 1px solid #ccc; border-radius: 4px;"
                " padding: 2px 8px;"
                " }}"
                "QPushButton:checked {{"
                " background-color: white; color: {c};"
                " border: 2px solid {c};"
                " }}".format(c=color)
            )
            btn.toggled.connect(self._on_toggle_changed)
            self._toggle_layout.insertWidget(
                self._toggle_layout.count() - 1, btn
            )
            self._wl_toggles[wl] = btn
        for wl in list(self._wl_toggles):
            if wl not in wls:
                btn = self._wl_toggles.pop(wl)
                self._toggle_layout.removeWidget(btn)
                btn.deleteLater()
        self._toggle_bar.setVisible(bool(self._wl_toggles))

    def _on_toggle_changed(self, _checked=False):
        self._redraw_amplitudes()
        # Redraw the current attenuation curve so its overlays follow the
        # toggle as well.
        if self._curve_key is not None:
            laser, lpwr = self._curve_key
            self._draw_curve(laser, lpwr, self._curve_x, self._curve_y)

    # ── history model regeneration ────────────────────────────────────────
    def _get_analyzer(self):
        if self._analyzer is None and self._ana_config is not None:
            try:
                from monet.util import load_class

                self._analyzer = load_class(
                    self._ana_config["classpath"],
                    self._ana_config["init_kwargs"],
                )
            except Exception:
                self._analyzer = None
        return self._analyzer

    def _grid(self):
        init = (self._ana_config or {}).get("init_kwargs", {})
        lo = init.get("min", 0)
        hi = init.get("max", 180)
        try:
            if lo is None or np.isnan(lo):
                lo = 0
        except (TypeError, ValueError):
            lo = 0
        return np.linspace(lo, hi, 200)

    def _compute_history_amps(self):
        """Regenerate {wl: [{date, amplitudes}]} from stored fit parameters."""
        out = {}
        analyzer = self._get_analyzer()
        if analyzer is None:
            return out
        for wl, runs in self._history.items():
            rlist = []
            for run in runs:
                amps = {}
                for power, pars in run.get("powers", {}).items():
                    try:
                        analyzer.load_model(pars)
                        amps[float(power)] = float(
                            np.real(analyzer.output_range()[1])
                        )
                    except Exception:
                        continue
                if amps:
                    rlist.append(
                        {"date": run.get("date", ""), "amplitudes": amps}
                    )
            if rlist:
                out[float(wl)] = rlist
        return out

    def _history_curves(self, laser, lpwr):
        """Regenerated (grid, powers, date) curves for these conditions."""
        analyzer = self._get_analyzer()
        if analyzer is None:
            return []
        wl = self._canon(laser)
        grid = self._grid()
        out = []
        for run in self._history.get(wl, []):
            pars = None
            for p, pr in run.get("powers", {}).items():
                if float(p) == float(lpwr):
                    pars = pr
                    break
            if pars is None:
                continue
            try:
                analyzer.load_model(pars)
                yvals = np.array(
                    [np.real(analyzer.estimate_power(g)) for g in grid]
                )
            except Exception:
                continue
            out.append((grid, yvals, run.get("date", "")))
        return out

    # ── public API ────────────────────────────────────────────────────────
    def clear(self):
        """Drop the current run's data and reset both axes.

        Loaded history (see :meth:`set_history`) is preserved so it can be
        drawn behind the next run; use :meth:`set_history` with an empty dict
        to drop it too.
        """
        self._amplitudes = {}
        self._flagged = {}
        self._curve_key = None
        self._curve_line = None
        self._curve_x = []
        self._curve_y = []
        self.flagged_changed.emit([])
        if not self._has_mpl:
            return
        self._curve_ax.clear()
        self._style_curve_axes("current attenuation curve")
        self._curve_canvas.draw_idle()
        self._redraw_amplitudes()

    def set_history(self, history, ana_config=None):
        """Overlay previous calibration runs, regenerated from fit parameters.

        Parameters
        ----------
        history : dict
            As returned by :func:`monet.io.load_calibration_history`:
            ``{wavelength (float): [{"date": str, "powers": {power: params}}]}``.
        ana_config : dict or None
            Analysis ``classpath`` / ``init_kwargs`` used to rebuild the models
            the overlays are regenerated from. Remembered across calls.
        """
        self._history = history or {}
        if ana_config is not None:
            self._ana_config = ana_config
            self._analyzer = None
        self._history_amps = self._compute_history_amps()
        self._rebuild_toggles()
        self._redraw_amplitudes()
        if self._curve_key is not None:
            laser, lpwr = self._curve_key
            self._draw_curve(laser, lpwr, self._curve_x, self._curve_y)

    def flagged_points(self):
        """Return the currently-flagged points as ``[(laser, lpwr), ...]``."""
        return [
            (laser, lpwr)
            for laser, lpwrs in self._flagged.items()
            for lpwr in lpwrs
        ]

    def _style_curve_axes(self, title):
        ax = self._curve_ax
        ax.set_xlabel("attenuator control value", fontsize=8)
        ax.set_ylabel("power [mW]", fontsize=8)
        ax.set_title(title, fontsize=9)
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.3)

    def _detect_outliers(self, lpwrs, amps):
        """Indices of live points that stray from the linear trend.

        Delegates to :func:`monet.io.flag_amplitude_outliers` (robust
        Theil–Sen line + 2 % relative-deviation test) so the logic is
        unit-tested independently of the GUI. Returns ``{index: residual_frac}``
        for the flagged points.
        """
        return io.flag_amplitude_outliers(
            lpwrs, amps, rel_thresh=self.OUTLIER_REL_THRESH
        )

    def _redraw_amplitudes(self):
        if not self._has_mpl:
            return
        ax = self._amp_ax
        ax.clear()
        self._flagged = {}

        # Previous runs — thin, faded, behind the live series; annotated with
        # their acquisition date. Only for toggled-on wavelengths.
        for wl, runs in self._history_amps.items():
            if not self._is_visible(wl):
                continue
            color = self._laser_color(wl)
            for run in runs:
                amps = run.get("amplitudes", {})
                if not amps:
                    continue
                lpwrs = sorted(amps, key=float)
                ax.plot(
                    lpwrs,
                    [amps[lp] for lp in lpwrs],
                    color=color,
                    linewidth=0.6,
                    alpha=0.4,
                    zorder=1,
                )
                date = run.get("date", "")
                if date:
                    ax.annotate(
                        date,
                        xy=(lpwrs[-1], amps[lpwrs[-1]]),
                        xytext=(2, 0),
                        textcoords="offset points",
                        fontsize=6,
                        color=color,
                        alpha=0.7,
                        va="center",
                        ha="left",
                    )

        # Live run — bold, one series per wavelength, outliers ringed in red.
        # Flags are computed for every wavelength (so the flagged panel is
        # complete) but only drawn for toggled-on ones.
        flagged_report = []
        for laser in sorted(self._amplitudes, key=self._canon):
            points = self._amplitudes[laser]
            lpwrs = sorted(points, key=float)
            amps = [points[lp] for lp in lpwrs]
            outliers = self._detect_outliers(lpwrs, amps)
            if outliers:
                self._flagged[laser] = {lpwrs[i] for i in outliers}
                for i, frac in outliers.items():
                    flagged_report.append(
                        {
                            "laser": laser,
                            "lpwr": float(lpwrs[i]),
                            "residual_frac": frac,
                        }
                    )
            if not self._is_visible(laser):
                continue
            color = self._laser_color(laser)
            ax.plot(
                lpwrs,
                amps,
                marker="o",
                color=color,
                linewidth=1.6,
                zorder=3,
                label="{:g} nm".format(self._canon(laser)),
            )
            if outliers:
                ax.plot(
                    [lpwrs[i] for i in outliers],
                    [amps[i] for i in outliers],
                    marker="o",
                    markersize=11,
                    markerfacecolor="none",
                    markeredgecolor="red",
                    markeredgewidth=1.8,
                    linestyle="none",
                    zorder=4,
                )

        ax.set_xlabel("laser power set-point [mW]", fontsize=8)
        ax.set_ylabel("max measured power [mW]", fontsize=8)
        ax.set_title("measured power amplitudes", fontsize=9)
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.3)
        handles, labels = ax.get_legend_handles_labels()
        if labels:
            ax.legend(fontsize=7)
        self._amp_canvas.draw_idle()
        self.flagged_changed.emit(flagged_report)

    def _draw_curve(self, laser, lpwr, x, y):
        """Draw the attenuation curve with regenerated history overlays."""
        if not self._has_mpl:
            return
        ax = self._curve_ax
        ax.clear()
        color = self._laser_color(laser)

        # Previous calibrations for the same conditions, regenerated from fit
        # parameters (raw points are not stored), drawn thin behind the live
        # curve and annotated with the acquisition date.
        if self._is_visible(laser):
            for grid, yvals, date in self._history_curves(laser, lpwr):
                ax.plot(
                    grid,
                    yvals,
                    color=color,
                    linewidth=0.6,
                    alpha=0.4,
                    zorder=1,
                )
                if date and len(grid):
                    ax.annotate(
                        date,
                        xy=(grid[-1], yvals[-1]),
                        xytext=(2, 0),
                        textcoords="offset points",
                        fontsize=6,
                        color=color,
                        alpha=0.7,
                        va="center",
                        ha="left",
                    )

        (self._curve_line,) = ax.plot(x, y, marker="x", color=color, zorder=3)
        self._style_curve_axes(
            "current curve — {} nm, {} mW".format(laser, lpwr)
        )
        ax.relim()
        ax.autoscale_view()
        self._curve_canvas.draw_idle()

    def add_point(self, laser, lpwr, index, total, ctrl_val, power):
        """Extend the current curve by one freshly measured point.

        Starts a new curve whenever the power step changes.
        """
        if not self._has_mpl:
            return

        if index == 0 or self._curve_key != (laser, lpwr):
            self._curve_key = (laser, lpwr)
            self._curve_x = []
            self._curve_y = []
            # Draw the history overlays once for this new curve; the live line
            # is then extended in place below.
            self._draw_curve(laser, lpwr, [], [])

        self._curve_x.append(ctrl_val)
        self._curve_y.append(power)
        if self._curve_line is not None:
            self._curve_line.set_data(self._curve_x, self._curve_y)
            self._curve_ax.relim()
            self._curve_ax.autoscale_view()
            self._curve_canvas.draw_idle()

    def add_curve(self, laser, lpwr, ctrl_vals, powers):
        """Close off a completed attenuation curve and record its amplitude.

        Redraws the curve from the values the protocol actually kept, so the
        plot is right even if no live points arrived (e.g. the 1D protocol).
        Keeps ``_curve_key`` set so wavelength toggles can refresh this curve
        until the next power step begins.
        """
        if not self._has_mpl:
            return

        self._curve_key = (laser, lpwr)
        self._curve_x = list(ctrl_vals)
        self._curve_y = list(powers)
        self._draw_curve(laser, lpwr, ctrl_vals, powers)

        try:
            amplitude = float(max(powers))
        except (TypeError, ValueError):
            return
        self._amplitudes.setdefault(laser, {})[lpwr] = amplitude
        self._rebuild_toggles()
        self._redraw_amplitudes()


# ---------------------------------------------------------------------------
# Tab 1 — Calibrate
# ---------------------------------------------------------------------------


class CalibrateTab(QWidget):
    """Tab for running calibration protocols."""

    # Status messages are emitted via the `status` signal so the tab can be
    # embedded anywhere — the host connects it to its own status bar.
    status = pyqtSignal(str, int)
    calibration_started = pyqtSignal()
    calibration_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pc = None
        self._worker = None
        self._discard_worker = None  # keep alive to prevent GC
        self._history_worker = None  # keep alive to prevent GC
        self._checkboxes = {}
        # Latest outlier report from CalibrationPlots (list of dicts).
        self._flagged_report = []
        self._build_ui()

    def _emit_status(self, msg, timeout_ms=0):
        """Emit a status message; ``timeout_ms=0`` means persistent."""
        self.status.emit(msg, timeout_ms)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Wavelength selection group
        self._wl_group = QGroupBox("Wavelengths to calibrate")
        self._wl_layout = QVBoxLayout()
        self._wl_group.setLayout(self._wl_layout)

        btn_row = QHBoxLayout()
        btn_select_all = QPushButton("Select all")
        btn_deselect_all = QPushButton("Deselect all")
        btn_select_all.clicked.connect(self._select_all)
        btn_deselect_all.clicked.connect(self._deselect_all)
        btn_row.addWidget(btn_select_all)
        btn_row.addWidget(btn_deselect_all)
        btn_row.addStretch()
        self._wl_layout.addLayout(btn_row)

        layout.addWidget(self._wl_group)

        # Multi-laser checkbox
        self._multi_cb = QCheckBox(
            "Multi-laser mode (keep other lasers on during calibration)"
        )
        self._multi_cb.setChecked(False)
        self._multi_cb.setToolTip(
            "When unchecked, all lasers are switched off before the run and "
            "each laser is switched off again once its calibration is done, "
            "so only the laser being calibrated is ever on."
        )
        layout.addWidget(self._multi_cb)

        # Power-meter position — mirrors the Set Power tab's dropdown so the
        # two surfaces present the same control (see SetPowerTab._pm_pos_combo).
        pm_row = QHBoxLayout()
        pm_row.addWidget(QLabel("Powermeter position:"))
        self._pm_pos_combo = QComboBox()
        self._pm_pos_combo.addItem("Back focal plane (BFP)", POWERMETER_BFP)
        self._pm_pos_combo.addItem("Sample plane", POWERMETER_SAMPLE)
        self._pm_pos_combo.setToolTip(
            "Where the power meter is physically positioned for this "
            "calibration run. For the back focal plane (BFP) the beampath is "
            "moved to the BFP powermeter position; an objective transmission "
            "factor is computed if both sample-plane and BFP calibrations "
            "exist for the same day."
        )
        pm_row.addWidget(self._pm_pos_combo)
        pm_row.addStretch()
        layout.addLayout(pm_row)

        # Free-text comment stored with every calibration of this run.
        comment_row = QHBoxLayout()
        comment_row.addWidget(QLabel("Comment:"))
        self._comment_edit = QLineEdit()
        self._comment_edit.setPlaceholderText(
            "optional note for this run, e.g. 'laser status orange today'"
        )
        self._comment_edit.setToolTip(
            "Free-text note saved alongside every calibration written in this "
            "run (visible in the Database tab)."
        )
        comment_row.addWidget(self._comment_edit)
        layout.addLayout(comment_row)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setFormat("Waiting…")
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        # Lower half — log on the left, live plots on the right
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("Courier", 9))
        # The two side-by-side canvases have wide size hints; without a floor
        # the splitter would squeeze the log down to an unreadable column.
        self._log.setMinimumWidth(240)
        self._plots = CalibrationPlots()
        self._plots.flagged_changed.connect(self._on_flagged_changed)

        lower = QSplitter(Qt.Orientation.Horizontal)
        lower.addWidget(self._log)
        lower.addWidget(self._plots)
        lower.setStretchFactor(0, 1)
        lower.setStretchFactor(1, 2)
        lower.setSizes([320, 640])
        lower.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout.addWidget(lower, 1)

        # Flagged-points panel — populated when the amplitude vs. laser-power
        # trend has outliers (single failed calibrations). Hidden otherwise.
        self._flagged_group = QGroupBox(
            "Flagged calibrations (off the linear amplitude trend)"
        )
        self._flagged_group.setToolTip(
            "Points whose measured amplitude strays from the expected linear "
            "relationship with laser power — usually a failed single "
            "calibration. Tick the ones to act on, then discard them or "
            "re-measure them in place."
        )
        fl_layout = QVBoxLayout()
        self._flagged_list = QListWidget()
        self._flagged_list.setMaximumHeight(110)
        fl_layout.addWidget(self._flagged_list)
        fl_btn_row = QHBoxLayout()
        self._btn_discard_flagged = QPushButton("Discard selected")
        self._btn_discard_flagged.setToolTip(
            "Delete the ticked calibration records from the database. The "
            "points stay listed so you can then re-measure them."
        )
        self._btn_discard_flagged.clicked.connect(self._on_discard_flagged)
        self._btn_recal_flagged = QPushButton("Re-measure selected")
        self._btn_recal_flagged.setToolTip(
            "Re-measure exactly the ticked wavelength / power points "
            "(replacing any existing records for them)."
        )
        self._btn_recal_flagged.clicked.connect(self._on_recalibrate_flagged)
        fl_btn_row.addWidget(self._btn_discard_flagged)
        fl_btn_row.addWidget(self._btn_recal_flagged)
        fl_btn_row.addStretch()
        fl_layout.addLayout(fl_btn_row)
        self._flagged_group.setLayout(fl_layout)
        self._flagged_group.setVisible(False)
        layout.addWidget(self._flagged_group)

        # Start / Cancel / Discard buttons
        btn_row2 = QHBoxLayout()
        self._btn_start = QPushButton("Start")
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setEnabled(False)
        self._btn_discard = QPushButton("Discard")
        self._btn_discard.setEnabled(False)
        self._btn_discard.setToolTip(
            "Delete the calibration records written by the last run from "
            "the database."
        )
        self._btn_start.clicked.connect(self._on_start)
        self._btn_cancel.clicked.connect(self._on_cancel)
        self._btn_discard.clicked.connect(self._on_discard)
        btn_row2.addWidget(self._btn_start)
        btn_row2.addWidget(self._btn_cancel)
        btn_row2.addWidget(self._btn_discard)
        btn_row2.addStretch()
        layout.addLayout(btn_row2)

    def set_pc(self, pc):
        self._pc = pc
        self._rebuild_checkboxes()
        self._plots.clear()
        # Drop history overlaid from any previous connection.
        self._plots.set_history({})
        self._update_discard_enabled()
        has_beampath = (
            pc is not None
            and hasattr(pc, "instrument")
            and hasattr(pc.instrument, "use_beampath")
            and pc.instrument.use_beampath
        )
        # Without a beam path the BFP position cannot be reached, so pin the
        # selector to the sample plane and disable it.
        self._pm_pos_combo.setEnabled(has_beampath)
        if not has_beampath:
            idx = self._pm_pos_combo.findData(POWERMETER_SAMPLE)
            if idx >= 0:
                self._pm_pos_combo.setCurrentIndex(idx)

    def _rebuild_checkboxes(self):
        # Remove old checkboxes (keep the button row at index 0)
        while self._wl_layout.count() > 1:
            item = self._wl_layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()

        self._checkboxes.clear()

        if self._pc is None:
            return

        if hasattr(self._pc, "protocol") and self._pc.protocol:
            for laser in self._pc.protocol["laser_sequence"]:
                try:
                    enabled = self._pc.instrument.lasers[laser].enabled
                    state_str = "ON" if enabled else "off"
                except Exception:
                    state_str = "?"
                cb = QCheckBox(f"{laser} nm  [{state_str}]")
                cb.setChecked(True)
                self._checkboxes[laser] = cb
                self._wl_layout.addWidget(cb)
        else:
            # 1D microscope — single disabled placeholder
            try:
                wl = self._pc.instrument.config["index"].get(
                    "wavelength [nm]", "?"
                )
            except Exception:
                wl = "?"
            cb = QCheckBox(f"{wl} nm  (single-wavelength mode)")
            cb.setChecked(True)
            cb.setEnabled(False)
            self._wl_layout.addWidget(cb)

    def _select_all(self):
        for cb in self._checkboxes.values():
            cb.setChecked(True)

    def _deselect_all(self):
        for cb in self._checkboxes.values():
            cb.setChecked(False)

    def _selected_lasers(self):
        return [
            laser for laser, cb in self._checkboxes.items() if cb.isChecked()
        ]

    def _on_start(self):
        if self._pc is None:
            QMessageBox.warning(
                self, "Not connected", "Connect to a microscope first."
            )
            return

        comment = self._comment_edit.text().strip() or None

        # 1D mode
        if not (hasattr(self._pc, "protocol") and self._pc.protocol):
            self._log.append("Starting 1D calibration…")
            self._emit_status("Running 1D calibration…")
            self._btn_discard.setEnabled(False)
            self._plots.clear()
            self._pc.reset_saved_calibrations()
            try:
                ctrl_vals, powers = self._pc.calibrate(comment=comment)
                laser = self._pc.instrument.config["index"].get(
                    "wavelength [nm]", ""
                )
                lpwr = self._pc.instrument.config["index"].get(
                    "laser_power [mW]", ""
                )
                self._plots.add_curve(laser, lpwr, ctrl_vals, powers)
                self._log.append("Done.")
                self._emit_status("Calibration complete.", 5000)
            except Exception as exc:
                QMessageBox.critical(self, "Error", str(exc))
                self._emit_status("Calibration failed.", 5000)
            self._btn_discard.setEnabled(bool(self._pc.saved_calibrations))
            return

        selected = self._selected_lasers()
        if not selected:
            QMessageBox.warning(
                self, "No wavelengths", "Select at least one wavelength."
            )
            return

        self._log.clear()
        self._plots.clear()
        self._log.append(f"Starting calibration for lasers: {selected}")
        self._progress.setValue(0)
        self._progress.setFormat("Starting…")
        self._btn_start.setEnabled(False)
        self._btn_cancel.setEnabled(True)
        self._btn_discard.setEnabled(False)
        self._emit_status("Calibration running…")

        self.calibration_started.emit()
        self._launch_calibration(selected)

    def _launch_calibration(self, selected, power_filter=None):
        """Start a calibration worker for ``selected`` lasers.

        ``power_filter`` ({laser: [powers]}) restricts the run to specific
        power points — used when re-measuring flagged calibrations.
        """
        powermeter_type = self._pm_pos_combo.currentData() or POWERMETER_SAMPLE
        comment = self._comment_edit.text().strip() or None
        # Overlay previous runs so the operator can watch for drift.
        self._load_history_async(selected, powermeter_type)
        self._worker = CalibrationWorker(
            self._pc,
            laser_filter=selected,
            powermeter_type=powermeter_type,
            manage_laser_state=not self._multi_cb.isChecked(),
            power_filter=power_filter,
            comment=comment,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.point.connect(self._plots.add_point)
        self._worker.curve.connect(self._plots.add_curve)
        self._worker.log_message.connect(self._log.append)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()
        # Now that a run is in progress, the flag actions are disabled.
        self._update_flagged_buttons()

    def _load_history_async(self, lasers, powermeter_type):
        """Load recent runs' amplitude curves and overlay them (background)."""
        if self._pc is None:
            return
        try:
            cfg = self._pc.instrument.config
            device = cfg["index"].get("name")
            ana_cfg = cfg["analysis"]
            db_fname = cfg["database"]
        except Exception:
            return
        if not device:
            return
        # Match the selected lasers by numeric wavelength — the database may
        # store the wavelength as int or float, so a string compare would miss.
        keep = set()
        for las in lasers:
            try:
                keep.add(float(las))
            except (TypeError, ValueError):
                pass

        def _do():
            all_hist = io.load_calibration_history(
                db_fname, device, powermeter_type=powermeter_type
            )
            if keep:
                return {
                    wl: runs for wl, runs in all_hist.items() if wl in keep
                }
            return all_hist

        def _on_result(history):
            self._plots.set_history(history or {}, ana_cfg)
            self._history_worker = None

        def _on_hist_error(msg):
            logger.debug("amplitude history load failed: %s", msg)
            self._history_worker = None

        worker = GenericWorker(_do)
        worker.result.connect(_on_result)
        worker.error.connect(_on_hist_error)
        self._history_worker = worker
        worker.start()

    def _on_cancel(self):
        if self._worker:
            self._worker.request_cancel()
            self._btn_cancel.setEnabled(False)
            self._emit_status("Cancelling calibration…")

    def _on_progress(self, step, total, laser, lpwr):
        self._progress.setMaximum(total)
        self._progress.setValue(step)
        self._progress.setFormat(f"{laser} nm / {lpwr} mW  ({step}/{total})")
        self._emit_status(
            f"Calibrating: laser {laser} nm at {lpwr} mW  ({step}/{total})"
        )

    def _on_finished(self):
        self._log.append("Calibration complete.")
        self._progress.setFormat("Done")
        self._btn_start.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._update_discard_enabled()
        self._worker = None
        self._update_flagged_buttons()
        self._emit_status("Calibration complete.", 5000)
        self.calibration_finished.emit()

    def _on_error(self, msg):
        self._log.append(f"ERROR: {msg}")
        QMessageBox.critical(self, "Calibration error", msg)
        self._btn_start.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        # A run that failed part-way may still have written records; let the
        # user discard those.
        self._update_discard_enabled()
        self._worker = None
        self._update_flagged_buttons()
        self._emit_status(f"Calibration error: {msg}", 5000)
        self.calibration_finished.emit()

    def _update_discard_enabled(self):
        saved = getattr(self._pc, "saved_calibrations", None)
        self._btn_discard.setEnabled(bool(saved))

    # ── Flagged (outlier) calibration points ──────────────────────────────
    def _on_flagged_changed(self, report):
        """Rebuild the flagged-points list from the plot's outlier report."""
        self._flagged_report = list(report or [])
        self._flagged_list.clear()
        for entry in self._flagged_report:
            laser = entry["laser"]
            lpwr = entry["lpwr"]
            pct = entry.get("residual_frac", 0.0) * 100.0
            item = QListWidgetItem(
                "{} nm @ {:g} mW  ({:.0f}% off trend)".format(laser, lpwr, pct)
            )
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, (laser, lpwr))
            self._flagged_list.addItem(item)
        self._flagged_group.setVisible(bool(self._flagged_report))
        self._update_flagged_buttons()

    def _update_flagged_buttons(self):
        # Only actionable when idle (no run in progress) and something is
        # flagged.
        enabled = self._worker is None and bool(self._flagged_report)
        self._btn_discard_flagged.setEnabled(enabled)
        self._btn_recal_flagged.setEnabled(enabled)

    def _checked_flagged(self):
        """Return the ticked flagged points as ``[(laser, lpwr), ...]``."""
        out = []
        for i in range(self._flagged_list.count()):
            item = self._flagged_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(item.data(Qt.ItemDataRole.UserRole))
        return out

    def _flagged_to_records(self, targets):
        """Map ``(laser, lpwr)`` targets to this run's DB index records."""

        def _fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        want = {(str(las), _fnum(lpwr)) for las, lpwr in targets}
        saved = list(getattr(self._pc, "saved_calibrations", None) or [])
        matched = []
        for rec in saved:
            key = (str(rec.get(LASER_TAG)), _fnum(rec.get(POWER_TAG)))
            if key in want:
                matched.append(rec)
        return matched

    def _on_discard_flagged(self):
        targets = self._checked_flagged()
        if not targets:
            QMessageBox.information(
                self, "Nothing ticked", "Tick the point(s) to discard."
            )
            return
        records = self._flagged_to_records(targets)
        if not records:
            QMessageBox.information(
                self,
                "No records",
                "Could not match the flagged points to records from the "
                "last run. They may already have been removed.",
            )
            return
        reply = QMessageBox.question(
            self,
            "Discard flagged",
            f"Delete {len(records)} flagged calibration record(s) from the "
            "database?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._delete_records_async(records, then_recalibrate=None)

    def _on_recalibrate_flagged(self):
        targets = self._checked_flagged()
        if not targets:
            QMessageBox.information(
                self, "Nothing ticked", "Tick the point(s) to re-measure."
            )
            return
        # Any still-present records for these points are deleted first so the
        # re-measurement does not leave a stale duplicate; if they were already
        # discarded this is simply a no-op.
        records = self._flagged_to_records(targets)
        reply = QMessageBox.question(
            self,
            "Re-measure flagged",
            "Re-measure {} flagged point(s) now?".format(len(targets)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._delete_records_async(records, then_recalibrate=targets)

    def _delete_records_async(self, records, then_recalibrate=None):
        """Delete ``records`` in the background, optionally recalibrating."""
        db_fname = self._pc.instrument.config["database"]

        def _do():
            deleted = 0
            for index in records:
                deleted += io.delete_calibration(db_fname, index) or 0
            return deleted

        def _on_result(deleted):
            self._log.append(f"Discarded {deleted} flagged record(s).")
            self._emit_status(f"Discarded {deleted} flagged record(s).", 5000)
            # Drop the deleted records from this run's undo list.
            remaining = [
                r
                for r in (self._pc.saved_calibrations or [])
                if r not in records
            ]
            self._pc.saved_calibrations = remaining

        def _on_err(msg):
            self._log.append(f"ERROR discarding flagged: {msg}")
            QMessageBox.critical(self, "Discard error", msg)

        def _on_done():
            self._discard_worker = None
            self._update_discard_enabled()
            if then_recalibrate:
                self._recalibrate_points(then_recalibrate)
            else:
                # Keep the discarded points on the plot / in the flagged list
                # so they stay selectable — the user can then click
                # "Re-measure selected" to re-acquire them. (They are only
                # removed from the plot once they are actually re-measured on
                # the linear trend.)
                self._btn_start.setEnabled(True)
                self._update_flagged_buttons()

        self._btn_start.setEnabled(False)
        self._btn_discard_flagged.setEnabled(False)
        self._btn_recal_flagged.setEnabled(False)

        worker = GenericWorker(_do)
        worker.result.connect(_on_result)
        worker.error.connect(_on_err)
        worker.finished.connect(_on_done)
        self._discard_worker = worker
        worker.start()

    def _recalibrate_points(self, targets):
        """Re-measure the given ``(laser, lpwr)`` points in place."""
        power_filter = {}
        for laser, lpwr in targets:
            power_filter.setdefault(laser, []).append(lpwr)
        lasers = list(power_filter.keys())
        self._log.append(
            "Recalibrating: "
            + ", ".join(
                "{} nm @ {} mW".format(las, ", ".join(str(p) for p in ps))
                for las, ps in power_filter.items()
            )
        )
        self._progress.setValue(0)
        self._progress.setFormat("Recalibrating…")
        self._btn_start.setEnabled(False)
        self._btn_cancel.setEnabled(True)
        self._btn_discard.setEnabled(False)
        self._emit_status("Recalibrating flagged points…")
        self.calibration_started.emit()
        self._launch_calibration(lasers, power_filter=power_filter)

    def _on_discard(self):
        """Delete the records written by the last calibration run."""
        records = list(getattr(self._pc, "saved_calibrations", None) or [])
        if not records:
            self._btn_discard.setEnabled(False)
            return

        reply = QMessageBox.question(
            self,
            "Discard calibration",
            f"Delete the {len(records)} calibration record(s) written by the "
            "last run from the database?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        db_fname = self._pc.instrument.config["database"]

        def _do():
            deleted = 0
            for index in records:
                deleted += io.delete_calibration(db_fname, index) or 0
            return deleted

        def _on_result(deleted):
            self._pc.reset_saved_calibrations()
            self._log.append(f"Discarded {deleted} calibration record(s).")
            self._emit_status(
                f"Discarded {deleted} calibration record(s).", 5000
            )

        def _on_error(msg):
            self._log.append(f"ERROR discarding calibration: {msg}")
            QMessageBox.critical(self, "Discard error", msg)
            self._emit_status(f"Discard failed: {msg}", 5000)

        def _on_finished():
            self._btn_start.setEnabled(True)
            self._update_discard_enabled()
            self._discard_worker = None

        self._btn_start.setEnabled(False)
        self._btn_discard.setEnabled(False)
        self._emit_status("Discarding calibration…")

        worker = GenericWorker(_do)
        worker.result.connect(_on_result)
        worker.error.connect(_on_error)
        worker.finished.connect(_on_finished)
        self._discard_worker = worker
        worker.start()

    def set_powermeter_available(self, available):
        """Enable or disable calibration controls per powermeter state."""
        self._btn_start.setEnabled(available)
        if not available:
            self._log.append(
                "WARNING: PowerMeter not available. Calibration is disabled."
            )

    def cancel_worker_and_wait(self):
        if self._worker and self._worker.isRunning():
            self._worker.request_cancel()
            self._worker.wait(5000)


# ---------------------------------------------------------------------------
# Tab 2 — Adjust
# ---------------------------------------------------------------------------


class AdjustTab(QWidget):
    """Tab for direct attenuator and laser power adjustment.

    No laser selector — operations act on whichever laser is currently
    active in the instrument (``instrument.curr_laser``).
    """

    status = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pc = None
        self._active_worker = None  # keep alive to prevent GC
        self._build_ui()

    def _emit_status(self, msg, timeout_ms=0):
        self.status.emit(msg, timeout_ms)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Refresh row
        refresh_row = QHBoxLayout()
        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.setToolTip(
            "Re-read attenuator position and laser power from hardware"
        )
        self._btn_refresh.clicked.connect(self._on_refresh)
        refresh_row.addWidget(self._btn_refresh)
        refresh_row.addStretch()
        layout.addLayout(refresh_row)

        # Attenuator group
        att_group = QGroupBox("Attenuator")
        att_layout = QHBoxLayout()
        att_layout.addWidget(QLabel("Position:"))
        self._att_spin = QDoubleSpinBox()
        self._att_spin.setRange(-1e6, 1e6)
        self._att_spin.setDecimals(3)
        att_layout.addWidget(self._att_spin)
        self._btn_att_set = QPushButton("Set")
        self._btn_att_set.clicked.connect(self._on_att_set)
        self._btn_att_home = QPushButton("Home")
        self._btn_att_home.clicked.connect(self._on_att_home)
        att_layout.addWidget(self._btn_att_set)
        att_layout.addWidget(self._btn_att_home)
        att_layout.addStretch()
        att_group.setLayout(att_layout)
        layout.addWidget(att_group)

        # Laser power output group
        pwr_group = QGroupBox("Laser power output")
        pwr_layout = QHBoxLayout()
        pwr_layout.addWidget(QLabel("Power (mW):"))
        self._pwr_spin = QDoubleSpinBox()
        self._pwr_spin.setRange(0, 10000)
        self._pwr_spin.setDecimals(1)
        pwr_layout.addWidget(self._pwr_spin)
        self._btn_pwr_set = QPushButton("Set")
        self._btn_pwr_set.clicked.connect(self._on_pwr_set)
        pwr_layout.addWidget(self._btn_pwr_set)
        pwr_layout.addStretch()
        pwr_group.setLayout(pwr_layout)
        layout.addWidget(pwr_group)

        # Beampath controls
        bp_row = QHBoxLayout()
        self._btn_bp_open = QPushButton("Open beampath")
        self._btn_bp_close = QPushButton("Close beampath")
        self._btn_bp_open.clicked.connect(self._on_bp_open)
        self._btn_bp_close.clicked.connect(self._on_bp_close)
        bp_row.addWidget(self._btn_bp_open)
        bp_row.addWidget(self._btn_bp_close)
        bp_row.addStretch()
        layout.addLayout(bp_row)

        # Status
        self._status = QLabel("")
        layout.addWidget(self._status)
        layout.addStretch()

    def set_pc(self, pc):
        self._pc = pc
        if pc is None:
            return
        # Populate initial hardware values
        try:
            pos = pc.instrument.attenuator.curr_pos()
            if pos is not None:
                self._att_spin.setValue(float(pos))
        except Exception:
            pass
        try:
            laser = pc.instrument.curr_laser
            pwr = pc.instrument.lasers[laser].power
            if pwr is not None:
                self._pwr_spin.setValue(float(pwr))
        except Exception:
            pass
        # Overlay the last persisted settings for the current laser so the
        # user can restore them after a restart with a single Set. Populate
        # only — nothing is sent to the hardware here.
        try:
            saved = pc.instrument.saved_state(pc.instrument.curr_laser)
        except Exception:
            saved = None
        if saved:
            if saved.get("laser_power") is not None:
                self._pwr_spin.setValue(float(saved["laser_power"]))
            if saved.get("attenuator") is not None:
                self._att_spin.setValue(float(saved["attenuator"]))

    def _on_refresh(self):
        """Read attenuator position and laser power from hardware."""
        if self._pc is None:
            return

        def _do():
            result = {}
            try:
                pos = self._pc.instrument.attenuator.curr_pos()
                if pos is not None:
                    result["att_pos"] = float(pos)
            except Exception:
                pass
            try:
                laser = self._pc.instrument.curr_laser
                pwr = self._pc.instrument.lasers[laser].power
                if pwr is not None:
                    result["laser_pwr"] = float(pwr)
            except Exception:
                pass
            return result

        def _on_result(result):
            if "att_pos" in result:
                self._att_spin.setValue(result["att_pos"])
            if "laser_pwr" in result:
                self._pwr_spin.setValue(result["laser_pwr"])
            self._status.setText("Values refreshed.")
            self._emit_status("Ready", 2000)

        self._run_hw(_do, "Refreshing device values…", on_result=_on_result)

    # --- helpers for async hardware ops ---

    def _hw_buttons(self, enabled):
        for btn in (
            self._btn_att_set,
            self._btn_att_home,
            self._btn_pwr_set,
            self._btn_bp_open,
            self._btn_bp_close,
            self._btn_refresh,
        ):
            btn.setEnabled(enabled)

    def _run_hw(self, func, status_msg, on_done=None, on_result=None):
        """Run a hardware callable in a GenericWorker, updating status bar."""
        self._hw_buttons(False)
        self._emit_status(status_msg)

        worker = GenericWorker(func)

        def _on_success(val):
            if on_result:
                on_result(val)
            if on_done:
                on_done()

        def _on_error(msg):
            self._status.setText(f"Error: {msg}")
            self._emit_status(f"Error: {msg}", 5000)
            QMessageBox.critical(self, "Error", msg)

        def _on_finished():
            self._hw_buttons(True)
            self._active_worker = None

        worker.result.connect(_on_success)
        worker.error.connect(_on_error)
        worker.finished.connect(_on_finished)
        self._active_worker = worker
        worker.start()

    def _on_att_set(self):
        if self._pc is None:
            return
        pos = self._att_spin.value()

        def _do():
            self._pc.instrument.attenuator.set(pos)
            self._pc.instrument.record_state()

        def _done():
            self._status.setText(f"Attenuator set to {pos}.")
            self._emit_status("Ready", 2000)

        self._run_hw(_do, f"Setting attenuator to {pos}…", on_done=_done)

    def _on_att_home(self):
        if self._pc is None:
            return

        def _do():
            self._pc.instrument.attenuator.home()
            self._pc.instrument.record_state()

        def _done():
            self._status.setText("Attenuator homed.")
            self._emit_status("Ready", 2000)
            # Read back new position after homing
            try:
                pos = self._pc.instrument.attenuator.curr_pos()
                if pos is not None:
                    self._att_spin.setValue(float(pos))
            except Exception:
                pass

        self._run_hw(_do, "Homing attenuator…", on_done=_done)

    def _on_pwr_set(self):
        if self._pc is None:
            return
        pwr = self._pwr_spin.value()

        def _do():
            self._pc.instrument.laserpower = pwr
            self._pc.instrument.record_state()

        def _done():
            self._status.setText(f"Laser power set to {pwr} mW.")
            self._emit_status("Ready", 2000)

        self._run_hw(_do, f"Setting laser power to {pwr} mW…", on_done=_done)

    def _on_bp_open(self):
        if self._pc is None:
            return
        try:
            laser = self._pc.instrument.curr_laser
        except Exception:
            laser = None
        protocol = getattr(self._pc, "protocol", None) or {}
        bp_positions = (protocol.get("beampath") or {}).get(laser)
        if bp_positions is None:
            self._status.setText("No beampath config for current laser.")
            return

        def _do():
            self._pc.instrument.beampath.positions = bp_positions

        def _done():
            self._status.setText("Beampath opened.")
            self._emit_status("Ready", 2000)

        self._run_hw(_do, "Opening beampath…", on_done=_done)

    def _on_bp_close(self):
        if self._pc is None:
            return
        protocol = getattr(self._pc, "protocol", None) or {}
        end_pos = (protocol.get("beampath") or {}).get("end")
        if end_pos is None:
            self._status.setText("No beampath end position configured.")
            return

        def _do():
            self._pc.instrument.beampath.positions = end_pos

        def _done():
            self._status.setText("Beampath closed.")
            self._emit_status("Ready", 2000)

        self._run_hw(_do, "Closing beampath…", on_done=_done)


# ---------------------------------------------------------------------------
# Tab 3 — Set Power
# ---------------------------------------------------------------------------


class SetPowerTab(QWidget):
    """Tab for setting output power using calibration data."""

    status = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pc = None
        self._active_worker = None  # keep alive to prevent GC
        self._cancel_feedback = False
        self._laser_state: dict = {}  # {laser: (pwr_value, mode_data)}
        self._build_ui()

    def _emit_status(self, msg, timeout_ms=0):
        self.status.emit(msg, timeout_ms)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Laser selector + on/off
        laser_row = QHBoxLayout()
        laser_row.addWidget(QLabel("Laser:"))
        self._laser_combo = QComboBox()
        self._laser_combo.currentIndexChanged.connect(self._on_laser_changed)
        laser_row.addWidget(self._laser_combo)
        self._btn_onoff = QPushButton("switch ON")
        self._btn_onoff.setCheckable(True)
        self._btn_onoff.clicked.connect(self._on_toggle_laser)
        laser_row.addWidget(self._btn_onoff)
        self._btn_alloff = QPushButton("All lasers OFF")
        self._btn_alloff.clicked.connect(self._on_all_off)
        laser_row.addWidget(self._btn_alloff)
        laser_row.addStretch()
        layout.addLayout(laser_row)

        # Multi-laser checkbox
        self._multi_cb = QCheckBox(
            "Multi-laser mode (keep other lasers on when switching)"
        )
        self._multi_cb.setChecked(True)
        layout.addWidget(self._multi_cb)

        # ── Adjustment group box ──────────────────────────────────────────
        adj_group = QGroupBox("Power adjustment")
        adj_layout = QVBoxLayout()
        adj_group.setLayout(adj_layout)

        # Mode selector
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItem(
            "Combined: laser power + attenuator", "combined"
        )
        self._mode_combo.addItem(
            "Fixed laser power: adjust attenuator only", "fixed_laser"
        )
        self._mode_combo.addItem(
            "Fixed attenuator: adjust laser power only", "fixed_attenuator"
        )
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_combo)
        mode_row.addStretch()
        adj_layout.addLayout(mode_row)

        # Feedback control
        feedback_row = QHBoxLayout()
        self._feedback_cb = QCheckBox("Feedback control")
        feedback_row.addWidget(self._feedback_cb)
        feedback_row.addWidget(QLabel("  Max deviation:"))
        self._feedback_tol_spin = QDoubleSpinBox()
        self._feedback_tol_spin.setRange(0.1, 50.0)
        self._feedback_tol_spin.setDecimals(1)
        self._feedback_tol_spin.setValue(1.0)
        self._feedback_tol_spin.setSuffix(" %")
        feedback_row.addWidget(self._feedback_tol_spin)
        self._btn_pi_params = QPushButton("▶ PI parameters")
        self._btn_pi_params.setFlat(True)
        self._btn_pi_params.clicked.connect(self._toggle_pi_params)
        feedback_row.addWidget(self._btn_pi_params)
        feedback_row.addStretch()
        adj_layout.addLayout(feedback_row)

        # PI parameters panel (hidden by default)
        self._pi_panel = QWidget()
        pi_layout = QHBoxLayout(self._pi_panel)
        pi_layout.setContentsMargins(16, 0, 0, 0)
        pi_layout.addWidget(QLabel("Kp:"))
        self._kp_spin = QDoubleSpinBox()
        self._kp_spin.setRange(0.01, 5.0)
        self._kp_spin.setDecimals(2)
        self._kp_spin.setSingleStep(0.05)
        self._kp_spin.setValue(0.85)
        self._kp_spin.setToolTip(
            "Proportional gain — fraction of current error applied per step.\n"
            "Lower values converge more slowly but without overshoot."
        )
        pi_layout.addWidget(self._kp_spin)
        pi_layout.addWidget(QLabel("  Ki:"))
        self._ki_spin = QDoubleSpinBox()
        self._ki_spin.setRange(0.0, 2.0)
        self._ki_spin.setDecimals(3)
        self._ki_spin.setSingleStep(0.01)
        self._ki_spin.setValue(0.15)
        self._ki_spin.setToolTip(
            "Integral gain — accumulates past error to eliminate "
            "steady-state offset.\n"
            "Set to 0 to disable integral action."
        )
        pi_layout.addWidget(self._ki_spin)
        pi_layout.addStretch()
        self._pi_panel.setVisible(False)
        adj_layout.addWidget(self._pi_panel)

        # Power range label
        self._range_label = QLabel("Range: N/A")
        adj_layout.addWidget(self._range_label)

        # Target power + Set button
        pwr_row = QHBoxLayout()
        pwr_row.addWidget(QLabel("Target power (mW):"))
        self._pwr_spin = QDoubleSpinBox()
        self._pwr_spin.setRange(0, 10000)
        self._pwr_spin.setDecimals(2)
        pwr_row.addWidget(self._pwr_spin)
        self._btn_set = QPushButton("Set")
        self._btn_set.clicked.connect(self._on_set_power)
        pwr_row.addWidget(self._btn_set)
        self._btn_cancel_feedback = QPushButton("Cancel")
        self._btn_cancel_feedback.clicked.connect(self._on_cancel_feedback)
        self._btn_cancel_feedback.setVisible(False)
        pwr_row.addWidget(self._btn_cancel_feedback)
        pwr_row.addStretch()
        adj_layout.addLayout(pwr_row)

        # The "Power adjustment" box is built here but added to the layout
        # further down, below the power-meter / beam-path setup controls, so
        # the physical setup sits on top and power adjustment sits lower.
        # ─────────────────────────────────────────────────────────────────

        # ── Powermeter position ───────────────────────────────────────────
        # Where the meter is physically placed. Drives whether raw readings are
        # projected to the sample plane via the objective transmission factor.
        # A shared label-column width left-aligns the controls of the three
        # setup rows (power-meter position, shutters & filters, turret).
        setup_label_w = 150
        pm_row = QHBoxLayout()
        pm_label = QLabel("Powermeter position:")
        pm_label.setFixedWidth(setup_label_w)
        pm_row.addWidget(pm_label)
        self._pm_pos_combo = QComboBox()
        self._pm_pos_combo.addItem("Back focal plane (BFP)", POWERMETER_BFP)
        self._pm_pos_combo.addItem("Sample plane", POWERMETER_SAMPLE)
        self._pm_pos_combo.setToolTip(
            "Where the power meter is physically positioned. In the back focal "
            "plane (BFP), raw readings are multiplied by the objective "
            "transmission factor (T_obj = P_sample / P_bfp) to report "
            "sample-plane power; in the sample plane the reading is used "
            "as-is. The power setpoint is always a sample-plane power."
        )
        self._pm_pos_combo.currentIndexChanged.connect(
            self._on_pm_position_changed
        )
        pm_row.addWidget(self._pm_pos_combo)
        pm_row.addStretch()
        layout.addLayout(pm_row)

        # ── Beam-path controls ────────────────────────────────────────────
        bp_row = QHBoxLayout()
        bp_label = QLabel("Shutters & Filters:")
        bp_label.setFixedWidth(setup_label_w)
        bp_row.addWidget(bp_label)
        self._btn_open_sf = QPushButton("Open shutters && set filter")
        self._btn_open_sf.setToolTip(
            "Open the shutter and set the dichroic/filter for the selected "
            "laser. Does not move the objective turret."
        )
        self._btn_open_sf.clicked.connect(self._on_open_shutters_filter)
        bp_row.addWidget(self._btn_open_sf)
        self._btn_bp_close = QPushButton("Close beampath (park)")
        self._btn_bp_close.setToolTip(
            'Move the beam path to its configured "end" position (shutter '
            "closed, turret to the objective)."
        )
        self._btn_bp_close.clicked.connect(self._on_bp_close)
        bp_row.addWidget(self._btn_bp_close)
        bp_row.addStretch()
        layout.addLayout(bp_row)

        # ── Objective turret ──────────────────────────────────────────────
        turret_row = QHBoxLayout()
        turret_label = QLabel("Objective turret:")
        turret_label.setFixedWidth(setup_label_w)
        turret_row.addWidget(turret_label)
        self._btn_turret_obj = QPushButton("→ Objective")
        self._btn_turret_obj.setToolTip(
            "Move the objective turret (nosepiece) to the imaging objective "
            'position, taken from the beam-path "end" entry.'
        )
        self._btn_turret_obj.clicked.connect(self._on_turret_objective)
        self._btn_turret_pm = QPushButton("→ Powermeter")
        self._btn_turret_pm.setToolTip(
            "Move the objective turret (nosepiece) to the BFP powermeter "
            'position, taken from the beam-path "start_calibrate" entry.'
        )
        self._btn_turret_pm.clicked.connect(self._on_turret_powermeter)
        turret_row.addWidget(self._btn_turret_obj)
        turret_row.addWidget(self._btn_turret_pm)
        turret_row.addStretch()
        layout.addLayout(turret_row)

        # Beam-path state label
        self._bp_state_label = QLabel("current beampath settings: unknown")
        self._bp_state_label.setToolTip(
            "Current position of each beam-path element (shutter, filter "
            "turret, ...) as read from the hardware."
        )
        layout.addWidget(self._bp_state_label)

        # Measure button — sits just above the power adjustment box.
        measure_row = QHBoxLayout()
        self._btn_measure = QPushButton("Measure")
        self._btn_measure.clicked.connect(self._on_measure)
        measure_row.addWidget(self._btn_measure)
        measure_row.addStretch()
        layout.addLayout(measure_row)

        # Status label sits above the power-adjustment box so action feedback
        # is visible next to the controls, not buried between the boxes.
        self._status = QLabel("")
        layout.addWidget(self._status)

        # ── Power adjustment (built above, placed below the setup controls) ─
        layout.addWidget(adj_group)

        # ── Separator ────────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)

        # ── Hardware state section (formerly Adjust tab) ──────────────────
        hw_refresh_row = QHBoxLayout()
        self._btn_hw_refresh = QPushButton("Refresh hardware state")
        self._btn_hw_refresh.setToolTip(
            "Re-read attenuator position and laser power from hardware"
        )
        self._btn_hw_refresh.clicked.connect(self._on_hw_refresh)
        hw_refresh_row.addWidget(self._btn_hw_refresh)
        hw_refresh_row.addStretch()
        layout.addLayout(hw_refresh_row)

        # Attenuator direct control
        hw_att_group = QGroupBox("Attenuator (direct)")
        hw_att_layout = QHBoxLayout()
        hw_att_layout.addWidget(QLabel("Position:"))
        self._hw_att_spin = QDoubleSpinBox()
        self._hw_att_spin.setRange(-1e6, 1e6)
        self._hw_att_spin.setDecimals(3)
        hw_att_layout.addWidget(self._hw_att_spin)
        self._btn_hw_att_set = QPushButton("Set")
        self._btn_hw_att_set.clicked.connect(self._on_hw_att_set)
        self._btn_hw_att_home = QPushButton("Home")
        self._btn_hw_att_home.clicked.connect(self._on_hw_att_home)
        hw_att_layout.addWidget(self._btn_hw_att_set)
        hw_att_layout.addWidget(self._btn_hw_att_home)
        hw_att_layout.addStretch()
        hw_att_group.setLayout(hw_att_layout)
        layout.addWidget(hw_att_group)

        # Laser power direct control
        hw_pwr_group = QGroupBox("Laser power (direct)")
        hw_pwr_layout = QHBoxLayout()
        hw_pwr_layout.addWidget(QLabel("Power (mW):"))
        self._hw_pwr_spin = QDoubleSpinBox()
        self._hw_pwr_spin.setRange(0, 10000)
        self._hw_pwr_spin.setDecimals(1)
        hw_pwr_layout.addWidget(self._hw_pwr_spin)
        self._btn_hw_pwr_set = QPushButton("Set")
        self._btn_hw_pwr_set.clicked.connect(self._on_hw_pwr_set)
        hw_pwr_layout.addWidget(self._btn_hw_pwr_set)
        hw_pwr_layout.addStretch()
        hw_pwr_group.setLayout(hw_pwr_layout)
        layout.addWidget(hw_pwr_group)

        layout.addStretch()

    def set_pc(self, pc):
        self._pc = pc
        self._laser_combo.blockSignals(True)
        self._laser_combo.clear()
        if pc is not None:
            try:
                pc.instrument.load_calibration_database()
            except Exception:
                pass
            for laser in pc.instrument.lasers:
                self._laser_combo.addItem(str(laser), laser)
        self._laser_combo.blockSignals(False)
        if self._laser_combo.count():
            self._on_laser_changed(0)
        # Populate hardware spinboxes
        if pc is not None:
            try:
                pos = pc.instrument.attenuator.curr_pos()
                if pos is not None:
                    self._hw_att_spin.setValue(float(pos))
            except Exception:
                pass
            try:
                laser = pc.instrument.curr_laser
                pwr = pc.instrument.lasers[laser].power
                if pwr is not None:
                    self._hw_pwr_spin.setValue(float(pwr))
            except Exception:
                pass
            # Overlay the last persisted settings for the current laser so
            # the user can restore them after a restart with a single Set.
            self._apply_saved_state_to_ui(pc.instrument.curr_laser)
            self._set_bp_label(self._read_bp_positions())
            # Sync the instrument's physical powermeter position to the toggle
            # (the toggle default and the instrument default are both BFP).
            self._sync_powermeter_position_to_instrument()
        else:
            self._set_bp_label(None)
        self._update_turret_buttons_enabled()

    def _sync_powermeter_position_to_instrument(self):
        """Reflect the toggle onto the instrument, defaulting from it if set."""
        if self._pc is None:
            return
        try:
            current = getattr(self._pc.instrument, "powermeter_position", None)
            if current is not None:
                idx = self._pm_pos_combo.findData(current)
                if idx >= 0:
                    self._pm_pos_combo.blockSignals(True)
                    self._pm_pos_combo.setCurrentIndex(idx)
                    self._pm_pos_combo.blockSignals(False)
            self._pc.instrument.powermeter_position = (
                self._pm_pos_combo.currentData()
            )
        except Exception:
            pass

    def _apply_saved_state_to_ui(self, laser):
        """Populate the direct hardware spin boxes from persisted settings.

        Loads the laser power set-point and attenuator position last saved for
        ``laser`` (see :mod:`monet.hwstate`) into the direct-control spin boxes.
        This only updates the UI — nothing is sent to the hardware until the
        user clicks Set. Does nothing if no settings were stored.
        """
        if self._pc is None:
            return
        try:
            saved = self._pc.instrument.saved_state(laser)
        except Exception:
            saved = None
        if not saved:
            return
        if saved.get("laser_power") is not None:
            self._hw_pwr_spin.setValue(float(saved["laser_power"]))
        if saved.get("attenuator") is not None:
            self._hw_att_spin.setValue(float(saved["attenuator"]))

    def _on_laser_changed(self, idx):
        if self._pc is None:
            return
        # Save current laser's UI state before switching
        prev_laser = getattr(self, "_current_laser", None)
        if prev_laser is not None:
            self._laser_state[prev_laser] = (
                self._pwr_spin.value(),
                self._mode_combo.currentData(),
            )

        laser = self._laser_combo.currentData()
        if laser is None:
            return
        self._current_laser = laser

        # Restore saved state for the newly selected laser
        if laser in self._laser_state:
            saved_pwr, saved_mode = self._laser_state[laser]
            self._pwr_spin.setValue(saved_pwr)
            mode_idx = self._mode_combo.findData(saved_mode)
            if mode_idx >= 0:
                self._mode_combo.setCurrentIndex(mode_idx)

        try:
            enabled = self._pc.instrument.lasers[laser].enabled
            self._btn_onoff.setChecked(enabled)
            self._btn_onoff.setText("switch OFF" if enabled else "switch ON")
        except Exception as exc:
            self._status.setText(str(exc))
        # Show the persisted hardware settings for the newly selected laser
        # line (populate only — not applied to hardware).
        self._apply_saved_state_to_ui(laser)
        self._update_range_label()

    # --- helpers ---

    def _read_bp_positions(self):
        """Read the current beam-path positions from hardware.

        Safe to call from a worker thread. Returns None if no beam path is
        configured or it cannot be queried.
        """
        try:
            if not getattr(self._pc.instrument, "use_beampath", False):
                return None
            return dict(self._pc.instrument.beampath.positions)
        except Exception:
            return None

    def _set_bp_label(self, positions):
        """Show the beam-path element positions next to the buttons."""
        if not positions:
            self._bp_state_label.setText("current beampath settings: unknown")
            return

        def _fmt(val):
            if isinstance(val, bool):
                return "open" if val else "closed"
            return str(val)

        self._bp_state_label.setText(
            "current beampath settings: "
            + ", ".join(
                "{}: {}".format(obid, _fmt(pos))
                for obid, pos in positions.items()
            )
        )

    def _refresh_bp_label(self):
        """Re-read the beam-path state and update the label."""
        if self._pc is None:
            return
        self._run_hw(
            self._read_bp_positions,
            "Reading beampath state…",
            on_result=self._set_bp_label,
            on_done=lambda: self._emit_status("Ready", 2000),
        )

    def _beampath_matches(self, target_positions):
        """Return True if every key in target_positions already matches the
        current beampath position, so the move can be skipped."""
        try:
            if not getattr(self._pc.instrument, "use_beampath", False):
                return False
            current = self._pc.instrument.beampath.positions
            return all(
                current.get(k) == v for k, v in target_positions.items()
            )
        except Exception:
            return False

    def _on_cancel_feedback(self):
        self._cancel_feedback = True
        self._btn_cancel_feedback.setEnabled(False)

    def _action_buttons(self, enabled):
        for btn in (
            self._btn_onoff,
            self._btn_set,
            self._btn_open_sf,
            self._btn_bp_close,
            self._btn_turret_obj,
            self._btn_turret_pm,
            self._btn_measure,
            self._btn_alloff,
            self._btn_hw_refresh,
            self._btn_hw_att_set,
            self._btn_hw_att_home,
            self._btn_hw_pwr_set,
        ):
            btn.setEnabled(enabled)
        # The turret buttons additionally require a configured nosepiece;
        # re-apply that constraint whenever buttons are being re-enabled.
        if enabled:
            self._update_turret_buttons_enabled()

    def _run_hw(self, func, status_msg, on_done=None, on_result=None):
        """Run a hardware callable in a GenericWorker."""
        self._action_buttons(False)
        self._emit_status(status_msg)

        worker = GenericWorker(func)

        def _on_success(val):
            if on_result:
                on_result(val)
            if on_done:
                on_done()

        def _on_error(msg):
            self._status.setText(f"Error: {msg}")
            self._emit_status(f"Error: {msg}", 5000)
            QMessageBox.critical(self, "Error", msg)

        def _on_finished():
            self._action_buttons(True)
            self._active_worker = None

        worker.result.connect(_on_success)
        worker.error.connect(_on_error)
        worker.finished.connect(_on_finished)
        self._active_worker = worker
        worker.start()

    def _on_toggle_laser(self, checked):
        if self._pc is None:
            return
        laser = self._laser_combo.currentData()

        def _do():
            if not self._multi_cb.isChecked():
                for lsr in self._pc.instrument.lasers:
                    if lsr != laser:
                        self._pc.instrument.lasers[lsr].enabled = False
            self._pc.instrument.laser = laser
            self._pc.instrument.laser_enabled = checked

        def _done():
            self._btn_onoff.setText("switch OFF" if checked else "switch ON")
            self._status.setText(
                f'Laser {laser} nm {"on" if checked else "off"}.'
            )
            self._emit_status("Ready", 2000)

        self._run_hw(
            _do,
            f'{"Enabling" if checked else "Disabling"} laser {laser} nm…',
            on_done=_done,
        )

    def _on_set_power(self):
        if self._pc is None:
            return
        pwr = self._pwr_spin.value()
        laser = self._laser_combo.currentData()
        mode = self._mode_combo.currentData()

        if mode in ("fixed_attenuator", "fixed_laser") and not hasattr(
            self._pc.instrument, "set_power_fixed_attenuator"
        ):
            QMessageBox.warning(
                self,
                "Not supported",
                "This mode requires a multi-laser instrument "
                "with calibrations at multiple laser power levels.",
            )
            return

        if not self._check_power_in_range(pwr, mode, laser):
            return

        # --- Resolve feedback / beampath settings on the main thread ---
        use_feedback = self._feedback_cb.isChecked() and getattr(
            self._pc, "powermeter_available", False
        )
        protocol = getattr(self._pc, "protocol", None) or {}
        bp_dict = protocol.get("beampath") or {}
        # Shutter/filter for the wavelength, with the turret stripped out (it is
        # positioned separately to follow the powermeter toggle).
        bp_for_laser = bp_dict.get(laser)
        nid = self._nosepiece_id()
        if nid is not None and isinstance(bp_for_laser, dict):
            bp_for_laser = {k: v for k, v in bp_for_laser.items() if k != nid}
        bp_end_calibrate = bp_dict.get("end_calibrate")
        bp_end = bp_dict.get("end")

        # Position the objective turret to match where the meter physically is:
        # BFP → powermeter position, sample plane → imaging objective.
        pm_pos = self._pm_pos_combo.currentData()
        pm_is_bfp = pm_pos == POWERMETER_BFP
        turret_kind = "powermeter" if pm_is_bfp else "objective"
        turret_target = self._turret_value(turret_kind)  # (id, val) or None
        try:
            factor_available = self._pc.instrument._has_transmission_factor(
                laser
            )
        except Exception:
            factor_available = False

        max_dev_pct = self._feedback_tol_spin.value()
        MAX_ITER = 20

        # --- Build the background callable ---
        if not use_feedback:
            if mode == "fixed_attenuator":

                def _do():
                    self._pc.instrument.set_power_fixed_attenuator(pwr, laser)
                    self._pc.instrument.record_state(laser)

                done_msg = (
                    f"Laser power adjusted for {pwr} mW output "
                    f"(laser {laser} nm, attenuator fixed)."
                )
                status_msg = f"Adjusting laser power for {pwr} mW…"
            elif mode == "fixed_laser":

                def _do():
                    self._pc.instrument.set_power_fixed_laser(pwr, laser)
                    self._pc.instrument.record_state(laser)

                done_msg = (
                    f"Attenuator set for {pwr} mW "
                    f"(laser {laser} nm, laser power fixed)."
                )
                status_msg = f"Setting attenuator for {pwr} mW…"
            else:

                def _do():
                    self._pc.instrument.laser = laser
                    self._pc.instrument.power = pwr
                    self._pc.instrument.record_state(laser)

                done_msg = f"Power set to {pwr} mW for laser {laser} nm."
                status_msg = f"Setting power to {pwr} mW…"

            def _done():
                self._status.setText(done_msg)
                self._emit_status("Ready", 2000)
                self._refresh_hw_state(laser)
                self._update_range_label()
                # Record the just-set power in the MicroManager acquisition
                # comment, tagged [set]. A later Measure supersedes it with
                # [measured].
                self._write_set_comment(laser, pwr)

            self._run_hw(_do, status_msg, on_done=_done)

        else:
            # Capture PI gains on the main thread before the worker starts.
            kp_att = self._kp_spin.value()
            ki_att = self._ki_spin.value()

            # Mutable relay so _do() can call worker.progress.emit once the
            # worker object exists (set just before worker.start() below).
            progress_relay = [None]

            def _do():
                def _emit(iteration, setpoint, meas):
                    if progress_relay[0] is not None:
                        progress_relay[0]((iteration, setpoint, meas))

                # Open shutter/filter for the wavelength and position the turret
                # to match the powermeter toggle. run_power_feedback performs
                # the initial power set and settles before measuring.
                if bp_for_laser:
                    self._pc.instrument.beampath.positions = bp_for_laser
                if turret_target is not None:
                    tnid, tval = turret_target
                    self._pc.instrument.beampath.positions = {tnid: tval}

                # Closed-loop power setting (initial set, settle, PI feedback).
                result = run_power_feedback(
                    self._pc.instrument,
                    self._pc.powermeter,
                    pwr,
                    laser,
                    mode,
                    kp=kp_att,
                    ki=ki_att,
                    max_dev_pct=max_dev_pct,
                    max_iter=MAX_ITER,
                    progress_callback=_emit,
                    cancel_check=lambda: self._cancel_feedback,
                )

                # Restore beampath, mirroring the calibration routine
                if bp_end_calibrate is not None:
                    self._pc.instrument.beampath.positions = bp_end_calibrate
                if bp_end is not None:
                    self._pc.instrument.beampath.positions = bp_end

                self._pc.instrument.record_state(laser)

                return (
                    result["measured"],
                    result["converged"],
                    result["cali_pred"],
                    result["out_of_range"],
                    result["att_pos"],
                    result["laser_pwr"],
                    result.get("measured_raw"),
                    result.get("transmission"),
                )

            def _on_result(res):
                (
                    measured,
                    converged,
                    cali_pred,
                    out_of_range_warned,
                    att_pos,
                    laser_pwr,
                    raw,
                    factor,
                ) = res
                dev_pct = abs(measured - pwr) / pwr * 100.0 if pwr > 0 else 0.0
                parts = [
                    f"Target {pwr} mW → measured {measured:.3f} mW "
                    f"({dev_pct:.1f}% deviation)"
                ]
                if not converged:
                    parts.append(f"(did not converge within {MAX_ITER} steps)")
                if out_of_range_warned:
                    parts.append("(attenuator range limit reached)")
                try:
                    unit = self._pc.powermeter.unit
                except Exception:
                    unit = "mW"
                # Annotate whether an objective transmission factor was used.
                if factor is not None and factor != 1.0:
                    parts.append(
                        f"(sample plane; measured {raw:.3f} {unit} in BFP"
                        f" × T_obj={factor:.4f})"
                    )
                elif pm_is_bfp and not factor_available:
                    parts.append(
                        f"(⚠ BFP selected but no objective transmission factor "
                        f"for {laser} nm — reporting raw BFP reading as "
                        f"sample-plane power, uncorrected)"
                    )
                else:
                    parts.append("(no objective transmission factor applied)")
                self._status.setText("  ".join(parts))
                mm_err = self._update_mm_comment(
                    laser,
                    measured,
                    unit,
                    att_pos,
                    laser_pwr,
                    raw_power=raw,
                    transmission=factor,
                )
                if mm_err is not None:
                    self._status.setText(
                        "  ".join(parts) + f" — MM comment error: {mm_err}"
                    )
                if cali_pred is not None and cali_pred > 0:
                    cali_dev_pct = (measured - cali_pred) / cali_pred * 100.0
                    self._emit_status(
                        f"Calibration deviation: {cali_dev_pct:+.1f}%"
                        f"  (calibration predicts {cali_pred:.3f} mW,"
                        f" measured {measured:.3f} mW)"
                    )
                else:
                    self._emit_status("Ready", 2000)
                self._refresh_hw_state(laser)
                self._update_range_label()

            # Open the live-plot dialog and wire everything up manually
            # (cannot use _run_hw because we need access to the worker object
            # to hook up the progress signal before the thread starts).
            plot_dialog = FeedbackPlotDialog(pwr, max_dev_pct, mode, self)
            plot_dialog.show()
            self._feedback_dialog = plot_dialog  # keep alive (prevent GC)

            self._action_buttons(False)
            self._cancel_feedback = False
            self._btn_cancel_feedback.setVisible(True)
            self._btn_cancel_feedback.setEnabled(True)
            self._emit_status(f"Setting {pwr} mW with feedback…")

            worker = GenericWorker(_do)
            progress_relay[0] = worker.progress.emit
            worker.progress.connect(plot_dialog.add_point)

            def _on_success(val):
                _on_result(val)

            def _on_error(msg):
                self._status.setText(f"Error: {msg}")
                self._emit_status(f"Error: {msg}", 5000)
                QMessageBox.critical(self, "Error", msg)

            def _on_finished():
                self._action_buttons(True)
                self._btn_cancel_feedback.setVisible(False)
                self._active_worker = None

            worker.result.connect(_on_success)
            worker.error.connect(_on_error)
            worker.finished.connect(_on_finished)
            self._active_worker = worker
            worker.start()

    def _on_pm_position_changed(self, _idx=None):
        """Push the selected physical power-meter position to the instrument.

        This drives whether raw meter readings are projected to the sample
        plane via the objective transmission factor (see
        :meth:`IlluminationLaserControl._measurement_factor`).
        """
        if self._pc is None:
            return
        pos = self._pm_pos_combo.currentData()
        try:
            self._pc.instrument.powermeter_position = pos
        except Exception as exc:
            self._status.setText(str(exc))
            return
        self._update_range_label()

    def _nosepiece_id(self):
        """Return the beam-path object id of the objective turret, or None.

        Detected by scanning the configured beam-path objects for a
        ``NikonNosepiece`` instance.
        """
        try:
            objects = self._pc.instrument.beampath.objects
        except Exception:
            return None
        for obid, obj in objects.items():
            if isinstance(obj, NikonNosepiece):
                return obid
        return None

    def _turret_value(self, kind):
        """Nosepiece target for the turret, read from the beam-path config.

        ``kind='powermeter'`` reads the nosepiece value from the
        ``start_calibrate`` beam-path entry; ``kind='objective'`` reads it from
        the ``end`` entry (falling back to ``end_calibrate``). Returns
        ``(nosepiece_id, value)`` or ``None`` if no nosepiece is configured or
        the relevant entry has no nosepiece position.
        """
        nid = self._nosepiece_id()
        if nid is None:
            return None
        protocol = getattr(self._pc, "protocol", None) or {}
        bp_dict = protocol.get("beampath") or {}
        if kind == "powermeter":
            entries = [bp_dict.get("start_calibrate")]
        else:  # objective
            entries = [bp_dict.get("end"), bp_dict.get("end_calibrate")]
        for entry in entries:
            if isinstance(entry, dict) and nid in entry:
                return nid, entry[nid]
        return None

    def _update_turret_buttons_enabled(self):
        """Enable the turret buttons only when a nosepiece target is defined."""
        has_obj = self._pc is not None and self._turret_value("objective")
        has_pm = self._pc is not None and self._turret_value("powermeter")
        self._btn_turret_obj.setEnabled(bool(has_obj))
        self._btn_turret_pm.setEnabled(bool(has_pm))
        if self._pc is not None and self._nosepiece_id() is None:
            tip = "No objective turret (nosepiece) is configured."
            self._btn_turret_obj.setToolTip(tip)
            self._btn_turret_pm.setToolTip(tip)

    def _move_turret(self, kind, status_verb):
        """Move the objective turret (nosepiece only) to a named position."""
        if self._pc is None:
            return
        target = self._turret_value(kind)
        if target is None:
            self._status.setText(
                "No {} turret position configured.".format(kind)
            )
            return
        nid, value = target
        positions = {nid: value}

        def _do():
            self._pc.instrument.beampath.positions = positions
            return self._read_bp_positions()

        def _done():
            self._status.setText(
                "Objective turret moved to {} position.".format(kind)
            )
            self._emit_status("Ready", 2000)

        self._run_hw(
            _do,
            status_verb,
            on_done=_done,
            on_result=self._set_bp_label,
        )

    def _on_turret_objective(self):
        self._move_turret("objective", "Moving turret to objective…")

    def _on_turret_powermeter(self):
        self._move_turret("powermeter", "Moving turret to powermeter…")

    def _on_open_shutters_filter(self):
        """Open the shutter and set the dichroic/filter for the laser.

        Applies the per-laser beam-path entry but strips any nosepiece key so
        the objective turret is never moved by this action (that is handled by
        the dedicated turret buttons).
        """
        if self._pc is None:
            return
        laser = self._laser_combo.currentData()
        protocol = getattr(self._pc, "protocol", None) or {}
        bp_positions = (protocol.get("beampath") or {}).get(laser)
        if bp_positions is None:
            self._status.setText("No beampath config for this laser.")
            return
        nid = self._nosepiece_id()
        if nid is not None and isinstance(bp_positions, dict):
            bp_positions = {k: v for k, v in bp_positions.items() if k != nid}
        if not bp_positions:
            self._status.setText(
                "No shutter/filter beampath config for this laser."
            )
            return

        def _do():
            self._pc.instrument.beampath.positions = bp_positions
            return self._read_bp_positions()

        def _done():
            self._status.setText("Shutter opened and filter set.")
            self._emit_status("Ready", 2000)

        self._run_hw(
            _do,
            "Opening shutter and setting filter…",
            on_done=_done,
            on_result=self._set_bp_label,
        )

    def _on_bp_close(self):
        if self._pc is None:
            return
        protocol = getattr(self._pc, "protocol", None) or {}
        end_pos = (protocol.get("beampath") or {}).get("end")
        if end_pos is None:
            self._status.setText("No beampath end position configured.")
            return

        def _do():
            self._pc.instrument.beampath.positions = end_pos
            return self._read_bp_positions()

        def _done():
            self._status.setText("Beampath closed.")
            self._emit_status("Ready", 2000)

        self._run_hw(
            _do,
            "Closing beampath…",
            on_done=_done,
            on_result=self._set_bp_label,
        )

    def _update_mm_comment(
        self,
        laser,
        measured,
        unit,
        att_pos=None,
        laser_pwr=None,
        raw_power=None,
        transmission=None,
        kind="measured",
    ):
        """Write a power line into the MicroManager acquisition comment.

        Thin wrapper around util.update_mm_acquisition_comment. ``kind`` is
        ``"measured"`` when the power was measured or ``"set"`` when it was only
        commanded; a later measurement supersedes an earlier set line.
        """
        return update_mm_acquisition_comment(
            laser,
            measured,
            unit,
            att_pos,
            laser_pwr,
            raw_power=raw_power,
            transmission=transmission,
            kind=kind,
        )

    def _write_set_comment(self, laser, pwr):
        """Record a just-set (not measured) power in the MM comment as [set].

        Best-effort: reads the attenuator position and laser power set-point to
        include alongside, and never raises (a missing MicroManager /
        pycromanager is simply a no-op).
        """
        if self._pc is None:
            return
        att_pos = None
        laser_pwr = None
        try:
            att_pos = self._pc.instrument.attenuator.curr_pos()
        except Exception:
            pass
        try:
            laser_pwr = self._pc.instrument.lasers[laser].power
        except Exception:
            pass
        try:
            err = self._update_mm_comment(
                laser,
                pwr,
                "mW",
                att_pos=att_pos,
                laser_pwr=laser_pwr,
                kind="set",
            )
            if err:
                logger.debug("Set-power MM comment update failed: %s", err)
        except Exception as exc:
            logger.debug("Could not write set-power MM comment: %s", exc)

    def _on_measure(self):
        if self._pc is None:
            return
        laser = self._laser_combo.currentData()
        protocol = getattr(self._pc, "protocol", None) or {}
        bp_dict = protocol.get("beampath") or {}

        # Shutter/filter for this laser, with the turret stripped out (the
        # turret is positioned separately, following the powermeter toggle).
        bp_for_laser = bp_dict.get(laser)
        nid = self._nosepiece_id()
        if nid is not None and isinstance(bp_for_laser, dict):
            bp_for_laser = {k: v for k, v in bp_for_laser.items() if k != nid}

        # Position the objective turret to match where the meter physically is:
        # BFP → powermeter position, sample plane → imaging objective.
        pm_pos = self._pm_pos_combo.currentData()
        turret_kind = "powermeter" if pm_pos == POWERMETER_BFP else "objective"
        turret_target = self._turret_value(turret_kind)  # (id, val) or None

        mode = self._mode_combo.currentData()
        pm_is_bfp = pm_pos == POWERMETER_BFP
        try:
            factor_available = self._pc.instrument._has_transmission_factor(
                laser
            )
        except Exception:
            factor_available = False

        def _do():
            import time

            moved = False
            # Open shutter and set filter for this laser
            if bp_for_laser:
                try:
                    self._pc.instrument.beampath.positions = bp_for_laser
                    moved = True
                except Exception:
                    pass
            # Move the turret to match the powermeter position
            if turret_target is not None:
                tnid, tval = turret_target
                try:
                    self._pc.instrument.beampath.positions = {tnid: tval}
                    moved = True
                except Exception:
                    pass
            # Wait for beampath hardware to settle (no polling API available)
            if moved:
                time.sleep(2)
            # Project the raw reading to the sample plane. The factor depends on
            # where the meter physically is (the powermeter toggle), not on the
            # stored calibration.
            raw = self._pc.powermeter.read()
            measured = self._pc.instrument.to_sample_plane(raw, laser)
            try:
                factor = self._pc.instrument._measurement_factor(laser)
            except Exception:
                factor = None
            try:
                if mode == "fixed_attenuator" and hasattr(
                    self._pc.instrument, "predict_power_fixed_attenuator"
                ):
                    curr_lp = self._pc.instrument.lasers[laser].power
                    cali_pred = (
                        self._pc.instrument.predict_power_fixed_attenuator(
                            curr_lp, laser
                        )
                    )
                else:
                    cali_pred = self._pc.instrument.power
            except Exception:
                cali_pred = None
            try:
                att_pos = self._pc.instrument.attenuator.curr_pos()
            except Exception:
                att_pos = None
            try:
                laser_pwr = self._pc.instrument.lasers[laser].power
            except Exception:
                laser_pwr = None
            return (
                measured,
                cali_pred,
                att_pos,
                laser_pwr,
                (self._read_bp_positions()),
                raw,
                factor,
            )

        def _on_val(res):
            (
                measured,
                cali_pred,
                att_pos,
                laser_pwr,
                bp_positions,
                raw,
                factor,
            ) = res
            self._set_bp_label(bp_positions)
            try:
                unit = self._pc.powermeter.unit
            except Exception:
                unit = "a.u."

            # Annotate whether an objective transmission factor was applied.
            if factor is not None and factor != 1.0:
                trans_note = (
                    f" (sample plane; measured {raw:.3f} {unit} in BFP"
                    f" × T_obj={factor:.4f})"
                )
            elif pm_is_bfp and not factor_available:
                trans_note = (
                    f" (⚠ BFP selected but no objective transmission factor "
                    f"for {laser} nm — reporting raw BFP reading as "
                    f"sample-plane power, uncorrected)"
                )
            else:
                trans_note = " (no objective transmission factor applied)"
            self._status.setText(
                f"Measured power: {measured:.3f} {unit}{trans_note}"
            )

            mm_err = self._update_mm_comment(
                laser,
                measured,
                unit,
                att_pos,
                laser_pwr,
                raw_power=raw,
                transmission=factor,
            )

            if cali_pred is not None and cali_pred > 0:
                cali_dev_pct = (measured - cali_pred) / cali_pred * 100.0
                self._emit_status(
                    f"Calibration deviation: {cali_dev_pct:+.1f}%"
                    f"  (calibration predicts {cali_pred:.3f} {unit},"
                    f" measured {measured:.3f} {unit})"
                )
            else:
                self._emit_status("Ready", 2000)

            if mm_err is not None:
                self._status.setText(
                    f"Measured: {measured:.3f} {unit}{trans_note}"
                    f" — MM comment error: {mm_err}"
                )

        self._run_hw(_do, "Measuring power…", on_result=_on_val)

    def _toggle_pi_params(self):
        visible = not self._pi_panel.isVisible()
        self._pi_panel.setVisible(visible)
        self._btn_pi_params.setText(
            "▼ PI parameters" if visible else "▶ PI parameters"
        )

    def _on_mode_changed(self, _idx):
        """Enable feedback only for single-axis modes (not combined)."""
        self._update_feedback_enabled()
        self._update_range_label()

    def _update_feedback_enabled(self):
        mode = self._mode_combo.currentData()
        powermeter_ok = self._btn_measure.isEnabled()
        feedback_ok = powermeter_ok and mode != "combined"
        self._feedback_cb.setEnabled(feedback_ok)
        self._feedback_tol_spin.setEnabled(feedback_ok)
        if not feedback_ok:
            self._feedback_cb.setChecked(False)

    def set_powermeter_available(self, available):
        """Enable or disable powermeter-dependent controls."""
        self._btn_measure.setEnabled(available)
        self._update_feedback_enabled()

    def _on_all_off(self):
        if self._pc is None:
            return

        def _do():
            for laser in self._pc.instrument.lasers:
                self._pc.instrument.lasers[laser].enabled = False

        def _done():
            self._status.setText("All lasers switched off.")
            self._btn_onoff.setChecked(False)
            self._btn_onoff.setText("switch ON")
            self._emit_status("Ready", 2000)

        self._run_hw(_do, "Switching all lasers off…", on_done=_done)

    # ── Hardware state helpers ───────────────────────────────────────────────

    def _refresh_hw_state(self, laser=None):
        """Read back attenuator position and laser power into spinboxes.

        Called on the main thread immediately after a set-power action
        finishes.
        """
        if self._pc is None:
            return
        if laser is None:
            laser = self._laser_combo.currentData()
        try:
            pos = self._pc.instrument.attenuator.curr_pos()
            if pos is not None:
                self._hw_att_spin.setValue(float(pos))
        except Exception:
            pass
        try:
            if laser and hasattr(self._pc.instrument, "lasers"):
                pwr = self._pc.instrument.lasers[laser].power
                if pwr is not None:
                    self._hw_pwr_spin.setValue(float(pwr))
        except Exception:
            pass

    def _check_power_in_range(self, pwr, mode, laser):
        """Return True if pwr is reachable in this mode, else warn and
        return False. Catches the request before it reaches the calibration
        model, which would otherwise raise a bare out-of-range error."""
        try:
            lo, hi = self._pc.instrument.accessible_power_range(mode, laser)
        except Exception as exc:
            # cannot determine the range (e.g. not calibrated) — let the
            # hardware call report the problem
            logger.debug("Could not check power range: %s", exc)
            return True

        tol = 1e-6 * max(abs(hi), 1.0)
        if lo - tol <= pwr <= hi + tol:
            return True

        QMessageBox.warning(
            self,
            "Power out of range",
            f"{pwr:g} mW is not reachable for laser {laser} nm in mode\n"
            f'"{self._mode_combo.currentText()}".\n\n'
            f"Accessible range in this mode: {lo:.2f} – {hi:.2f} mW.\n\n"
            "Choose a power within the range, or switch to a mode that "
            "adjusts the other axis as well.",
        )
        return False

    def refresh_calibration_state(self):
        """Re-sync the calibration-dependent UI after a calibration run.

        A run reloads the instrument's calibration database at the end, but
        this tab's range label is only recomputed on user interaction, so it
        would keep showing the pre-run value. Refresh it here; also reload
        defensively should the instrument-side reload have failed, so the tab
        is never wedged as "not calibrated" when a calibration exists.
        """
        if self._pc is None:
            return
        if not getattr(self._pc.instrument, "is_calibrated", False):
            try:
                self._pc.instrument.load_calibration_database()
            except Exception:
                pass
        self._update_range_label()

    def _update_range_label(self):
        """Compute and display the accessible power range for the mode."""
        if self._pc is None or not getattr(
            self._pc.instrument, "is_calibrated", False
        ):
            self._range_label.setText("Range: N/A (not calibrated)")
            return
        laser = self._laser_combo.currentData()
        mode = self._mode_combo.currentData()
        try:
            lo, hi = self._pc.instrument.accessible_power_range(mode, laser)
            self._range_label.setText(f"Range: {lo:.2f} – {hi:.2f} mW")
        except Exception:
            self._range_label.setText("Range: N/A")

    def _on_hw_refresh(self):
        """Read attenuator position and laser power from hardware."""
        if self._pc is None:
            return

        def _do():
            result = {}
            try:
                pos = self._pc.instrument.attenuator.curr_pos()
                if pos is not None:
                    result["att_pos"] = float(pos)
            except Exception:
                pass
            try:
                laser = self._pc.instrument.curr_laser
                pwr = self._pc.instrument.lasers[laser].power
                if pwr is not None:
                    result["laser_pwr"] = float(pwr)
            except Exception:
                pass
            result["beampath"] = self._read_bp_positions()
            return result

        def _on_result(result):
            if "att_pos" in result:
                self._hw_att_spin.setValue(result["att_pos"])
            if "laser_pwr" in result:
                self._hw_pwr_spin.setValue(result["laser_pwr"])
            self._set_bp_label(result.get("beampath"))
            self._status.setText("Hardware state refreshed.")
            self._emit_status("Ready", 2000)

        self._run_hw(_do, "Refreshing hardware state…", on_result=_on_result)

    def _on_hw_att_set(self):
        if self._pc is None:
            return
        pos = self._hw_att_spin.value()

        def _do():
            self._pc.instrument.attenuator.set(pos)
            self._pc.instrument.record_state()

        def _done():
            self._status.setText(f"Attenuator set to {pos:.3f}.")
            self._emit_status("Ready", 2000)

        self._run_hw(_do, f"Setting attenuator to {pos:.3f}…", on_done=_done)

    def _on_hw_att_home(self):
        if self._pc is None:
            return

        def _do():
            self._pc.instrument.attenuator.home()
            self._pc.instrument.record_state()

        def _done():
            self._status.setText("Attenuator homed.")
            self._emit_status("Ready", 2000)
            try:
                pos = self._pc.instrument.attenuator.curr_pos()
                if pos is not None:
                    self._hw_att_spin.setValue(float(pos))
            except Exception:
                pass

        self._run_hw(_do, "Homing attenuator…", on_done=_done)

    def _on_hw_pwr_set(self):
        if self._pc is None:
            return
        pwr = self._hw_pwr_spin.value()

        def _do():
            self._pc.instrument.laserpower = pwr
            self._pc.instrument.record_state()

        def _done():
            self._status.setText(f"Laser power set to {pwr} mW.")
            self._emit_status("Ready", 2000)

        self._run_hw(_do, f"Setting laser power to {pwr} mW…", on_done=_done)


# ---------------------------------------------------------------------------
# Tab 4 — Database
# ---------------------------------------------------------------------------


def _run_span(run):
    """Short 'HH:MM' or 'HH:MM–HH:MM' time span of a run."""
    span = run["start"][11:]
    if run["end"] != run["start"]:
        span += "–" + run["end"][11:]
    return span


def _plot_run_amplitudes(ax, runs, title=""):
    """Plot amplitude vs laser power for each run.

    Colour approximates each wavelength's visible-spectrum colour (shared
    across runs); each run gets a distinct line style *and* marker so its
    legend entries are told apart even at the same colour. Each ``run`` is a
    dict with an ``amplitudes`` map ``{wavelength: {power: max}}`` (see
    :func:`monet.io.list_calibration_runs` with ``ana_config``).
    """
    ax.clear()
    styles = ["-", "--", ":", "-."]
    markers = ["o", "s", "^", "D"]
    plotted = False
    for ri, run in enumerate(runs):
        style = styles[ri % len(styles)]
        marker = markers[ri % len(markers)]
        amps = run.get("amplitudes") or {}
        tag = "{} {}".format(
            run.get("powermeter_type", ""), run.get("date", "")
        )
        for wl in sorted(amps, key=float):
            pts = amps[wl]
            if not pts:
                continue
            powers = sorted(pts, key=float)
            ax.plot(
                powers,
                [pts[p] for p in powers],
                marker=marker,
                linestyle=style,
                color=wavelength_to_rgb(wl),
                label="{:g} nm · {}".format(float(wl), tag),
            )
            plotted = True
    ax.set_xlabel("laser power set-point [mW]", fontsize=8)
    ax.set_ylabel("max power [mW]", fontsize=8)
    if title:
        ax.set_title(title, fontsize=9)
    ax.tick_params(labelsize=7)
    ax.grid(True, alpha=0.3)
    if plotted:
        # A longer handle makes the dash pattern visible in the legend.
        ax.legend(fontsize=6, handlelength=3)
    else:
        ax.text(
            0.5,
            0.5,
            "select run(s) to plot",
            ha="center",
            va="center",
            transform=ax.transAxes,
            fontsize=8,
        )


class RunPairDialog(QDialog):
    """Curate which calibration runs define the objective transmission factor.

    Runs (from :func:`monet.io.list_calibration_runs`) are grouped by
    power-meter position. Each run has a **checkbox** marking whether it is used
    for the transmission factor; **clicking** a run shows its amplitude-vs-power
    curves in the graph (click/focus = view, checkbox = use). The tick state is
    restored from the stored pairs, so the dialog can be reopened to review and
    change which runs are in use. On accept, every ticked sample run is paired
    with every ticked BFP run.
    """

    def __init__(self, runs, used_sample=None, used_bfp=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Transmission runs — select which to use")
        self._used_sample = used_sample or set()
        self._used_bfp = used_bfp or set()
        self.checked_samples = []
        self.checked_bfps = []

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Tick the sample-plane and BFP runs to use for the objective "
                "transmission factor; click a run to view its amplitude "
                "curves below. Ticked sample and BFP runs are paired "
                "automatically by wavelength and power, and remembered."
            )
        )

        lists_row = QHBoxLayout()
        self._sample_list = QListWidget()
        self._bfp_list = QListWidget()
        for lst, pm, used in (
            (self._sample_list, POWERMETER_SAMPLE, self._used_sample),
            (self._bfp_list, POWERMETER_BFP, self._used_bfp),
        ):
            for r in runs:
                if r["powermeter_type"] != pm:
                    continue
                item = QListWidgetItem(self._fmt_run(r))
                item.setData(Qt.ItemDataRole.UserRole, r)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if self._run_used(r, used)
                    else Qt.CheckState.Unchecked
                )
                lst.addItem(item)
            lst.itemSelectionChanged.connect(self._update)
            lst.itemChanged.connect(self._update)

        for title, lst in (
            ("Sample-plane runs", self._sample_list),
            ("BFP runs", self._bfp_list),
        ):
            col = QVBoxLayout()
            col.addWidget(QLabel(title))
            col.addWidget(lst)
            lists_row.addLayout(col)
        layout.addLayout(lists_row)

        # Amplitude-vs-power graph of the clicked (focused) runs.
        self._canvas = None
        try:
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
            from matplotlib.figure import Figure

            self._fig = Figure(figsize=(5, 2.6), tight_layout=True)
            self._ax = self._fig.add_subplot(111)
            self._canvas = FigureCanvasQTAgg(self._fig)
            self._canvas.setMinimumHeight(220)
            layout.addWidget(self._canvas)
        except Exception:
            self._canvas = None

        self._preview = QLabel("")
        layout.addWidget(self._preview)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        self._btn_ok = QPushButton("Save selection")
        self._btn_ok.clicked.connect(self.accept)
        btn_row.addWidget(self._btn_ok)
        layout.addLayout(btn_row)
        self.resize(720, 580)
        self._update()

    @staticmethod
    def _fmt_run(r):
        wls = "/".join("{:g}".format(w) for w in r["wavelengths"])
        pwrs = "/".join("{:g}".format(p) for p in r["powers"])
        return "{} {}  ·  {} cals  ·  {} nm  ·  {} mW".format(
            r["date"], _run_span(r), r["n_cals"], wls, pwrs
        )

    @staticmethod
    def _run_used(run, used):
        for wl, power, date, time_v in run["members"]:
            key = (float(wl), float(power), str(date)[:10], str(time_v)[:5])
            if key in used:
                return True
        return False

    @staticmethod
    def _focused(lst):
        items = lst.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    @staticmethod
    def _checked(lst):
        out = []
        for i in range(lst.count()):
            it = lst.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def _update(self, *args):
        self.checked_samples = self._checked(self._sample_list)
        self.checked_bfps = self._checked(self._bfp_list)
        self._preview.setText(
            "Using {} sample-plane and {} BFP run(s).".format(
                len(self.checked_samples), len(self.checked_bfps)
            )
        )
        if self._canvas is not None:
            chosen = [
                r
                for r in (
                    self._focused(self._sample_list),
                    self._focused(self._bfp_list),
                )
                if r is not None
            ]
            _plot_run_amplitudes(
                self._ax, chosen, "clicked runs — amplitude vs laser power"
            )
            self._canvas.draw_idle()


class DatabaseTab(QWidget):
    """Tab for exploring calibration runs and objective transmission factors.

    The left table lists 2D calibration *runs* (grouped from the database by
    power-meter position and time); selecting one or more plots their
    amplitude-vs-laser-power curves on the right.
    """

    status = pyqtSignal(str, int)

    RUN_COLUMNS = [
        "Date",
        "Time",
        "Position",
        "Wavelengths (nm)",
        "Powers (mW)",
        "#",
        "Comment",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pc = None
        self._db_fname = None
        self._runs = []
        self._active_worker = None
        self._factor_worker = None  # keep alive to prevent GC
        self._factor_canvas = None
        self._factor_ax = None
        self._runs_canvas = None
        self._runs_ax = None
        self._build_ui()

    def _emit_status(self, msg, timeout_ms=0):
        self.status.emit(msg, timeout_ms)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Top controls. The tab always shows the connected microscope only
        # (other scopes are reachable via the web dashboard link below).
        ctrl_row = QHBoxLayout()
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._on_refresh)
        ctrl_row.addWidget(btn_refresh)
        self._btn_delete_run = QPushButton("Delete run")
        self._btn_delete_run.setEnabled(False)
        self._btn_delete_run.setToolTip(
            "Delete every calibration belonging to the selected run(s) from "
            "the database."
        )
        self._btn_delete_run.clicked.connect(self._on_delete_run)
        ctrl_row.addWidget(self._btn_delete_run)
        ctrl_row.addStretch()
        layout.addLayout(ctrl_row)

        # Database link (shown when database is an HTTP server URL)
        self._db_link_label = QLabel("")
        self._db_link_label.setOpenExternalLinks(True)
        self._db_link_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._db_link_label)

        # Runs table (left) + amplitude-vs-power plot (right).
        split = QSplitter(Qt.Orientation.Horizontal)
        self._table = QTableWidget(0, len(self.RUN_COLUMNS))
        self._table.setHorizontalHeaderLabels(self.RUN_COLUMNS)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.itemSelectionChanged.connect(self._plot_selected_runs)
        self._table.setMinimumWidth(340)
        split.addWidget(self._table)

        try:
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
            from matplotlib.figure import Figure

            self._runs_fig = Figure(figsize=(4.5, 3.0), tight_layout=True)
            self._runs_ax = self._runs_fig.add_subplot(111)
            self._runs_canvas = FigureCanvasQTAgg(self._runs_fig)
            self._runs_canvas.setMinimumWidth(340)
            split.addWidget(self._runs_canvas)
            split.setSizes([420, 520])
        except Exception:
            self._runs_canvas = None
            split.addWidget(QLabel("matplotlib not available — no plot."))
        layout.addWidget(split, 1)

        # Status
        self._status = QLabel("")
        layout.addWidget(self._status)

        # Correction factors section
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)
        factors_hdr = QHBoxLayout()
        factors_hdr.addWidget(QLabel("Objective Transmission (sample / BFP)"))
        factors_hdr.addStretch()
        self._btn_add_pair = QPushButton("Pair runs…")
        self._btn_add_pair.setEnabled(False)
        self._btn_add_pair.setToolTip(
            "Pick a whole sample-plane calibration run and a whole BFP run; "
            "their single calibrations are paired automatically by wavelength "
            "and power to compute the objective transmission factors. Stored "
            "pairs are remembered and drive the plot below."
        )
        self._btn_add_pair.clicked.connect(self._on_add_pairing)
        factors_hdr.addWidget(self._btn_add_pair)
        self._btn_remove_pair = QPushButton("Remove pair")
        self._btn_remove_pair.setEnabled(False)
        self._btn_remove_pair.setToolTip(
            "Remove the selected manually-added transmission pair."
        )
        self._btn_remove_pair.clicked.connect(self._on_remove_pair)
        factors_hdr.addWidget(self._btn_remove_pair)
        self._btn_compute_transmission = QPushButton("Auto-compute")
        self._btn_compute_transmission.setEnabled(False)
        self._btn_compute_transmission.setToolTip(
            "Automatically pair same-day sample-plane and BFP calibrations "
            "and compute the transmission factor for each wavelength."
        )
        self._btn_compute_transmission.clicked.connect(
            self._on_compute_transmission
        )
        factors_hdr.addWidget(self._btn_compute_transmission)
        layout.addLayout(factors_hdr)
        # Shows the manually-selected pairs when any exist, otherwise the
        # auto-computed factors.  ``_showing_manual_pairs`` tracks which.
        self._showing_manual_pairs = False
        self._factors_table = QTableWidget(0, 6)
        self._factors_table.setHorizontalHeaderLabels(
            [
                "Microscope",
                "Wavelength (nm)",
                "Power (mW)",
                "Date",
                "transmission_objective",
                "n / std",
            ]
        )
        self._factors_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._factors_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._factors_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._factors_table.horizontalHeader().setStretchLastSection(True)
        self._factors_table.setMaximumHeight(150)
        self._factors_table.itemSelectionChanged.connect(
            self._update_pair_buttons
        )
        layout.addWidget(self._factors_table)

        # Transmission-factor plot: one point per (date, wavelength, laser
        # power), so drift and per-input outliers are visible at a glance.
        try:
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
            from matplotlib.figure import Figure

            self._factor_fig = Figure(figsize=(5, 2.6), tight_layout=True)
            self._factor_ax = self._factor_fig.add_subplot(111)
            self._factor_canvas = FigureCanvasQTAgg(self._factor_fig)
            self._factor_canvas.setMinimumHeight(200)
            layout.addWidget(self._factor_canvas)
        except Exception:
            self._factor_canvas = None
            layout.addWidget(
                QLabel(
                    "matplotlib not available — no transmission-factor plot."
                )
            )

    def set_pc(self, pc):
        self._pc = pc
        if pc is not None:
            self._db_fname = pc.instrument.config.get("database")
        else:
            self._db_fname = None
        self._btn_compute_transmission.setEnabled(pc is not None)
        self._update_db_link()
        self._on_refresh()

    def _current_scope(self):
        """Name of the connected microscope, or None. The tab shows only it."""
        if self._pc is None:
            return None
        try:
            return self._pc.instrument.config["index"].get("name")
        except Exception:
            return None

    def _update_db_link(self):
        db = self._db_fname or ""
        if db.startswith("http://") or db.startswith("https://"):
            docs_url = db.rstrip("/") + "/dashboard"
            self._db_link_label.setText(
                f'Server: <a href="{docs_url}">{docs_url}</a>'
            )
        else:
            self._db_link_label.setText(f"File: {db}" if db else "")

    def _ana_config(self):
        if self._pc is None:
            return None
        try:
            return self._pc.instrument.config["analysis"]
        except Exception:
            return None

    def _on_refresh(self):
        if self._db_fname is None:
            self._status.setText("No database configured.")
            return
        try:
            self._runs = io.list_calibration_runs(
                self._db_fname, self._current_scope(), self._ana_config()
            )
        except Exception as exc:
            self._status.setText(f"Error loading database: {exc}")
            self._runs = []
        self._table.setRowCount(0)

        _pos_label = {POWERMETER_SAMPLE: "sample plane", POWERMETER_BFP: "BFP"}
        for run in self._runs:
            r = self._table.rowCount()
            self._table.insertRow(r)
            wls = "/".join("{:g}".format(w) for w in run["wavelengths"])
            pwrs = "/".join("{:g}".format(p) for p in run["powers"])
            comment = run.get("comment", "")
            values = [
                run["date"],
                _run_span(run),
                _pos_label.get(run["powermeter_type"], run["powermeter_type"]),
                wls,
                pwrs,
                str(run["n_cals"]),
                comment,
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                if self.RUN_COLUMNS[col] == "Comment" and comment:
                    item.setToolTip(comment)
                self._table.setItem(r, col, item)
            self._table.item(r, 0).setData(Qt.ItemDataRole.UserRole, run)

        self._status.setText("{} run(s)".format(len(self._runs)))
        self._plot_selected_runs()
        self._refresh_factors_table()
        self._refresh_factor_plot()

    def _selected_runs(self):
        """Run dicts for the currently-selected rows."""
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()})
        runs = []
        for r in rows:
            item = self._table.item(r, 0)
            run = item.data(Qt.ItemDataRole.UserRole) if item else None
            if run:
                runs.append(run)
        return runs

    def _plot_selected_runs(self):
        """Plot amplitude vs laser power for the selected runs."""
        runs = self._selected_runs()
        self._btn_delete_run.setEnabled(bool(runs) and self._pc is not None)
        if self._runs_canvas is None:
            return
        _plot_run_amplitudes(self._runs_ax, runs, "amplitude vs laser power")
        self._runs_canvas.draw_idle()

    def _on_delete_run(self):
        runs = self._selected_runs()
        if not runs or self._db_fname is None:
            return
        n_cals = sum(r.get("n_cals", 0) for r in runs)
        reply = QMessageBox.question(
            self,
            "Delete run(s)",
            "Delete {} run(s) — {} calibration record(s) — from the "
            "database?\nThis cannot be undone.".format(len(runs), n_cals),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        db_fname = self._db_fname
        self._btn_delete_run.setEnabled(False)
        self._emit_status("Deleting run(s)…")

        def _do():
            deleted = 0
            for run in runs:
                deleted += io.delete_calibration_run(db_fname, run)
            return deleted

        def _on_result(deleted):
            self._active_worker = None
            self._emit_status(
                "Deleted {} calibration record(s).".format(deleted), 5000
            )
            self._on_refresh()

        def _on_error(msg):
            self._active_worker = None
            QMessageBox.critical(self, "Delete error", msg)
            self._on_refresh()

        worker = GenericWorker(_do)
        worker.result.connect(_on_result)
        worker.error.connect(_on_error)
        self._active_worker = worker
        worker.start()

    def _refresh_factors_table(self):
        """Show manually-selected pairs if any exist, else the auto factors."""
        self._factors_table.setRowCount(0)
        self._showing_manual_pairs = False
        if self._db_fname is None:
            self._update_pair_buttons()
            return
        scope = self._current_scope()
        try:
            pairs_df = io.load_factor_pairs(
                self._db_fname, device=scope if scope else None
            )
        except Exception:
            pairs_df = None

        if pairs_df is not None and not pairs_df.empty:
            self._showing_manual_pairs = True
            for _, row in pairs_df.iterrows():
                r = self._factors_table.rowCount()
                self._factors_table.insertRow(r)
                vals = [
                    str(row.get("device", "")),
                    str(row.get("wavelength", "")),
                    str(row.get("laser_power", "")),
                    str(row.get("date", "")),
                    self._fmt(row.get("factor", "")),
                    "n={}".format(row.get("n_points", "")),
                ]
                for c, v in enumerate(vals):
                    self._factors_table.setItem(r, c, QTableWidgetItem(v))
                # Keep the pair dict so it can be deleted from the store.
                self._factors_table.item(r, 0).setData(
                    Qt.ItemDataRole.UserRole, row.to_dict()
                )
            self._update_pair_buttons()
            return

        # Fallback: auto-computed factors (also used for power projection).
        try:
            factors_df = io.load_factors(
                self._db_fname, device=scope if scope else None
            )
        except Exception:
            factors_df = None
        if factors_df is not None and not factors_df.empty:
            for idx, row in factors_df.iterrows():
                if not isinstance(idx, tuple):
                    idx = (idx,)
                r = self._factors_table.rowCount()
                self._factors_table.insertRow(r)
                std_val = row.get("transmission_objective_std", "")
                vals = [
                    str(idx[0] if len(idx) > 0 else ""),
                    str(idx[1] if len(idx) > 1 else ""),
                    "",  # no per-power breakdown for auto factors
                    str(idx[2] if len(idx) > 2 else ""),
                    self._fmt(row.get("transmission_objective_mean", "")),
                    (
                        "std={:.4f}".format(std_val)
                        if isinstance(std_val, float)
                        else str(std_val)
                    ),
                ]
                for c, v in enumerate(vals):
                    self._factors_table.setItem(r, c, QTableWidgetItem(v))
        self._update_pair_buttons()

    def _update_pair_buttons(self):
        """Enable pair actions per selection / mode."""
        connected = self._pc is not None
        self._btn_add_pair.setEnabled(connected)
        can_remove = self._showing_manual_pairs and bool(
            self._factors_table.selectedIndexes()
        )
        self._btn_remove_pair.setEnabled(can_remove)

    @staticmethod
    def _fmt(v):
        try:
            return "{:.4f}".format(float(v))
        except (TypeError, ValueError):
            return str(v)

    def _on_add_pairing(self):
        """Scan calibration runs, then let the user pair a sample and BFP run."""
        if self._pc is None or self._db_fname is None:
            return
        try:
            ana_config = self._pc.instrument.config["analysis"]
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))
            return
        device = self._current_scope()
        db_fname = self._db_fname
        self._btn_add_pair.setEnabled(False)
        self._emit_status("Scanning calibration runs…")

        def _do():
            return io.list_calibration_runs(db_fname, device, ana_config)

        def _on_result(runs):
            self._active_worker = None
            self._btn_add_pair.setEnabled(True)
            self._emit_status("", 0)
            self._show_run_dialog(runs or [], ana_config)

        def _on_error(msg):
            self._active_worker = None
            self._btn_add_pair.setEnabled(True)
            QMessageBox.critical(self, "Error", msg)

        worker = GenericWorker(_do)
        worker.result.connect(_on_result)
        worker.error.connect(_on_error)
        self._active_worker = worker
        worker.start()

    def _used_run_keys(self):
        """(sample, bfp) sets of (wl, power, date, time) already used in pairs.

        Lets the dialog restore its checkboxes to the runs currently in use.
        """
        us, ub = set(), set()
        try:
            df = io.load_factor_pairs(
                self._db_fname, device=self._current_scope()
            )
        except Exception:
            return us, ub
        if not hasattr(df, "iterrows"):
            return us, ub

        def _v(row, key, fallback):
            v = row.get(key)
            if v is None or (isinstance(v, float) and v != v):
                v = row.get(fallback)
            return v

        for _, row in df.iterrows():
            try:
                wl = float(row["wavelength"])
                p = float(row["laser_power"])
            except (TypeError, ValueError, KeyError):
                continue
            us.add(
                (
                    wl,
                    p,
                    str(_v(row, "sample_date", "date"))[:10],
                    str(row.get("sample_time"))[:5],
                )
            )
            ub.add(
                (
                    wl,
                    p,
                    str(_v(row, "bfp_date", "date"))[:10],
                    str(row.get("bfp_time"))[:5],
                )
            )
        return us, ub

    def _show_run_dialog(self, runs, ana_config):
        samples = [
            r for r in runs if r["powermeter_type"] == POWERMETER_SAMPLE
        ]
        bfps = [r for r in runs if r["powermeter_type"] == POWERMETER_BFP]
        if not samples or not bfps:
            QMessageBox.information(
                self,
                "Not enough runs",
                "Need at least one sample-plane run and one BFP run for this "
                "microscope. Calibrate at both power-meter positions first.",
            )
            return
        used_sample, used_bfp = self._used_run_keys()
        dlg = RunPairDialog(runs, used_sample, used_bfp, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        checked_s = list(dlg.checked_samples)
        checked_b = list(dlg.checked_bfps)
        db_fname = self._db_fname
        device = self._current_scope()
        self._emit_status("Storing transmission selection…")

        def _do():
            # The ticked runs are authoritative: clear the device's pairs and
            # recompute from every ticked sample × ticked BFP run.
            io.clear_factor_pairs(db_fname, device)
            total = 0
            for s in checked_s:
                for b in checked_b:
                    total += len(
                        io.compute_run_pair_factors(db_fname, s, b, ana_config)
                    )
            return total

        def _on_result(total):
            self._active_worker = None
            self._emit_status(
                "Stored {} transmission pair(s).".format(total), 5000
            )
            self._on_refresh()

        def _on_error(msg):
            self._active_worker = None
            QMessageBox.critical(self, "Error", msg)
            self._on_refresh()

        worker = GenericWorker(_do)
        worker.result.connect(_on_result)
        worker.error.connect(_on_error)
        self._active_worker = worker
        worker.start()

    def _on_remove_pair(self):
        if not self._showing_manual_pairs or self._db_fname is None:
            return
        rows = sorted(
            {idx.row() for idx in self._factors_table.selectedIndexes()}
        )
        if not rows:
            return
        removed = 0
        for r in rows:
            item = self._factors_table.item(r, 0)
            pair = item.data(Qt.ItemDataRole.UserRole) if item else None
            if pair:
                try:
                    removed += io.delete_factor_pair(self._db_fname, pair)
                except Exception as exc:
                    logger.debug("delete pair failed: %s", exc)
        self._emit_status("Removed {} pair(s).".format(removed), 4000)
        self._on_refresh()

    def _refresh_factor_plot(self):
        """Redraw the transmission-factor plot.

        Manually-selected pairs take precedence; when none exist the auto
        same-day breakdown is shown instead.
        """
        if self._factor_canvas is None or self._db_fname is None:
            return
        device = self._current_scope()
        db_fname = self._db_fname

        # Prefer the hand-picked pairs — this is the graph the operator curates.
        try:
            pairs_df = io.load_factor_pairs(
                db_fname, device=device if device else None
            )
        except Exception:
            pairs_df = None
        if pairs_df is not None and not pairs_df.empty:
            self._draw_factor_plot(pairs_df, source="manual pairs")
            return

        ana_config = None
        if self._pc is not None:
            try:
                ana_config = self._pc.instrument.config["analysis"]
            except Exception:
                ana_config = None
        # A specific microscope and its analysis config are needed to rebuild
        # the models the auto factor is sampled from.
        if not device or ana_config is None:
            self._draw_factor_plot(None)
            return

        def _do():
            return io.compute_factor_breakdown(db_fname, device, ana_config)

        def _on_result(df):
            self._draw_factor_plot(df, source="auto (same-day pairing)")
            self._factor_worker = None

        def _on_error(msg):
            logger.debug("transmission breakdown failed: %s", msg)
            self._factor_worker = None

        worker = GenericWorker(_do)
        worker.result.connect(_on_result)
        worker.error.connect(_on_error)
        self._factor_worker = worker
        worker.start()

    def _draw_factor_plot(self, df, source=""):
        """Draw factor vs date, colored by wavelength, a point per power."""
        if self._factor_canvas is None:
            return
        ax = self._factor_ax
        ax.clear()
        if df is None or not hasattr(df, "empty") or df.empty:
            ax.text(
                0.5,
                0.5,
                "No transmission pairs to plot\n"
                "(use 'Pair runs…' to select runs)",
                ha="center",
                va="center",
                fontsize=8,
                transform=ax.transAxes,
            )
            ax.set_axis_off()
            self._factor_canvas.draw_idle()
            return
        ax.set_axis_on()
        from datetime import datetime as _dt

        def _parse(d):
            try:
                return _dt.strptime(str(d)[:10], "%Y-%m-%d")
            except Exception:
                return None

        wavelengths = sorted(df["wavelength"].unique())
        for wl in wavelengths:
            sub = df[df["wavelength"] == wl]
            color = wavelength_to_rgb(wl)
            # Individual factors (one per date / power) as faint points.
            xs = [_parse(d) for d in sub["date"]]
            if any(x is None for x in xs):
                xs = list(range(len(sub)))
            ax.scatter(xs, sub["factor"], s=22, color=color, alpha=0.35)
            # Temporal evolution: median factor per date, connected over time.
            med = sub.groupby("date")["factor"].median()
            mxs = [_parse(d) for d in med.index]
            if all(x is not None for x in mxs) and len(med):
                order = sorted(range(len(mxs)), key=lambda k: mxs[k])
                ax.plot(
                    [mxs[k] for k in order],
                    [med.values[k] for k in order],
                    color=color,
                    marker="o",
                    linewidth=1.6,
                    label="{:g} nm (median)".format(wl),
                )
            else:
                # Undated fallback: still label the wavelength via the points.
                ax.scatter([], [], color=color, label="{:g} nm".format(wl))
        ax.set_ylabel("T_obj = P_sample / P_bfp", fontsize=8)
        ax.set_xlabel("date", fontsize=8)
        title = "objective transmission over time (median per wavelength)"
        if source:
            title += "  —  {}".format(source)
        ax.set_title(title, fontsize=9)
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=7)
        for label in ax.get_xticklabels():
            label.set(rotation=30, horizontalalignment="right")
        self._factor_canvas.draw_idle()

    def _on_compute_transmission(self):
        if self._pc is None or self._db_fname is None:
            return

        try:
            device = self._pc.instrument.config["index"]["name"]
            lasers = list(self._pc.instrument.lasers.keys())
            ana_config = self._pc.instrument.config["analysis"]
        except Exception as exc:
            QMessageBox.critical(
                self, "Error", f"Could not read microscope config: {exc}"
            )
            return

        self._btn_compute_transmission.setEnabled(False)
        self._emit_status("Computing objective transmission…")

        def _do():
            for laser in lasers:
                io.compute_and_save_factor(
                    self._db_fname, device, laser, ana_config
                )

        def _on_done():
            self._btn_compute_transmission.setEnabled(True)
            self._active_worker = None
            self._emit_status("Objective transmission computed.", 4000)
            self._on_refresh()

        def _on_error(msg):
            self._btn_compute_transmission.setEnabled(True)
            self._active_worker = None
            self._emit_status("", 0)
            QMessageBox.critical(self, "Error", msg)

        worker = GenericWorker(_do)
        worker.finished.connect(_on_done)
        worker.error.connect(_on_error)
        self._active_worker = worker
        worker.start()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MonetWidget(QWidget):
    """The Monet GUI as a single embeddable ``QWidget``.

    Drop this into any host Qt application — a tab in a host ``QTabWidget``,
    the central widget of a host ``QMainWindow``, anywhere a ``QWidget``
    fits. The host can drive connection programmatically (``set_pc`` or
    ``connect_microscope``) and hide the built-in toolbar.

    Signals
    -------
    status_changed (str, int)
        Bubbled-up status from any tab. ``timeout_ms == 0`` means persistent.
    connected (object)
        Emitted with the calibration-protocol object after a successful
        connection (either via the built-in toolbar or via ``set_pc``).
    connect_error (str)
        Emitted when the built-in ``connect_microscope`` flow fails.
    calibration_started / calibration_finished
        Forwarded from the embedded ``CalibrateTab``.

    Parameters
    ----------
    parent : QWidget, optional
        Standard Qt parent.
    show_toolbar : bool
        If True (default), include the microscope picker + Connect button at
        the top. Pass False to hide them and drive connection from the host.
    tabs : tuple[str, ...]
        Which tabs to include, in display order. Keys: ``'set_power'``,
        ``'calibrate'``, ``'database'``, ``'adjust'``.
    initial_microscope : str, optional
        If given (and ``show_toolbar=True``), select that microscope and
        auto-connect once the widget is shown.
    """

    status_changed = pyqtSignal(str, int)
    connected = pyqtSignal(object)
    connect_error = pyqtSignal(str)
    calibration_started = pyqtSignal()
    calibration_finished = pyqtSignal()

    # Tab key -> (display label, class)
    _TAB_CATALOG = {
        "set_power": ("Set Power", SetPowerTab),
        "calibrate": ("Calibrate", CalibrateTab),
        "database": ("Database", DatabaseTab),
        "adjust": ("Adjust", AdjustTab),
    }

    def __init__(
        self,
        parent=None,
        *,
        show_toolbar=True,
        tabs=("set_power", "calibrate", "database"),
        initial_microscope=None,
    ):
        super().__init__(parent)
        self._pc = None
        self._connect_worker = None
        self._tab_keys = tuple(tabs)
        self._tab_widgets = {}
        self._scope_combo = None
        self._btn_connect = None
        self._build_ui(show_toolbar)
        if initial_microscope and self._scope_combo is not None:
            idx = self._scope_combo.findText(initial_microscope)
            if idx >= 0:
                self._scope_combo.setCurrentIndex(idx)
                # Auto-connect after the event loop starts and widget is shown
                QTimer.singleShot(100, self._on_connect)

    def _build_ui(self, show_toolbar):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if show_toolbar:
            tb_row = QHBoxLayout()
            tb_row.addWidget(QLabel("Microscope: "))
            self._scope_combo = QComboBox()
            for name in sorted(CONFIGS.keys()):
                self._scope_combo.addItem(name)
            tb_row.addWidget(self._scope_combo)
            self._btn_connect = QPushButton("Connect")
            self._btn_connect.clicked.connect(self._on_connect)
            tb_row.addWidget(self._btn_connect)
            tb_row.addStretch()
            layout.addLayout(tb_row)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        # Build tabs in requested order; each tab's status signal is
        # re-emitted as the widget's status_changed.
        for key in self._tab_keys:
            if key not in self._TAB_CATALOG:
                raise ValueError(
                    "Unknown tab key {!r}; choose from {}".format(
                        key, list(self._TAB_CATALOG)
                    )
                )
            label, cls = self._TAB_CATALOG[key]
            w = cls()
            w.status.connect(self.status_changed)
            self._tab_widgets[key] = w
            self._tabs.addTab(w, label)

        # Forward calibration lifecycle if CalibrateTab is included.
        cal = self._tab_widgets.get("calibrate")
        if cal is not None:
            cal.calibration_started.connect(self._on_calibration_started)
            cal.calibration_finished.connect(self._on_calibration_finished)

        # Matplotlib backend safety: must happen before any Qt-backed figure
        # is created. Non-fatal if matplotlib isn't installed.
        try:
            import matplotlib.pyplot as _plt

            _plt.switch_backend("agg")
        except Exception:
            pass

    # ---- public API ----------------------------------------------------

    def tab(self, key):
        """Return the embedded tab widget for ``key``, or ``None``."""
        return self._tab_widgets.get(key)

    @property
    def current_microscope(self):
        """Name of the microscope currently selected in the toolbar, or
        ``None`` if the toolbar is hidden / nothing selected."""
        if self._scope_combo is None:
            return None
        return self._scope_combo.currentText() or None

    @property
    def pc(self):
        """The currently-bound calibration-protocol object, or ``None``."""
        return self._pc

    def set_microscope(self, name):
        """Select ``name`` in the toolbar combo (needs ``show_toolbar``)."""
        if self._scope_combo is None:
            raise RuntimeError(
                "Toolbar is hidden; construct with show_toolbar=True or "
                "use set_pc() to inject a protocol object directly."
            )
        idx = self._scope_combo.findText(name)
        if idx >= 0:
            self._scope_combo.setCurrentIndex(idx)

    def connect_microscope(self, name=None):
        """Start a ``ConnectWorker`` for ``name`` (or the currently selected
        microscope). Emits ``connect_error`` on failure."""
        if name is not None and self._scope_combo is not None:
            self.set_microscope(name)
        if name is None:
            name = self.current_microscope
        if not name:
            self.connect_error.emit("No microscope selected.")
            return

        import copy

        try:
            config = copy.deepcopy(CONFIGS[name])
            protocol = copy.deepcopy(PROTOCOLS.get(name))
        except KeyError as exc:
            self.connect_error.emit(str(exc))
            return

        if self._btn_connect is not None:
            self._btn_connect.setEnabled(False)
        if self._scope_combo is not None:
            self._scope_combo.setEnabled(False)
        self.status_changed.emit("Connecting to {}…".format(name), 0)

        self._connect_worker = ConnectWorker(
            name, config, protocol, old_pc=self._pc
        )
        self._connect_worker.connected.connect(self._on_connected)
        self._connect_worker.warning.connect(self._on_connect_warning)
        self._connect_worker.error.connect(self._on_connect_error)
        self._connect_worker.finished.connect(self._on_connect_finished)
        self._connect_worker.start()

    def set_pc(self, pc):
        """Bind an externally-built calibration-protocol object.

        Use this when the host application manages the hardware connection
        itself and just wants Monet's UI bound to an existing object
        exposing ``.instrument`` (an ``IlluminationLaserControl``),
        ``.powermeter``, and ``.protocol``. Skips the in-widget
        ``ConnectWorker``.
        """
        self._pc = pc
        for w in self._tab_widgets.values():
            w.set_pc(pc)
        powermeter_ok = getattr(pc, "powermeter_available", True)
        self._apply_powermeter_state(powermeter_ok)
        self.connected.emit(pc)

    def shutdown(self):
        """Cancel any running calibration and disable all lasers. Call this
        from the host's close handler when the widget is being torn down."""
        cal = self._tab_widgets.get("calibrate")
        if cal is not None:
            try:
                cal.cancel_worker_and_wait()
            except Exception:
                pass
        if self._pc is not None:
            # disconnect() disables every laser and releases all hardware
            # (serial ports, SDK sessions); fall back to just disabling the
            # lasers if a custom pc object has no disconnect().
            try:
                self._pc.disconnect()
            except AttributeError:
                try:
                    for laser in self._pc.instrument.lasers:
                        self._pc.instrument.lasers[laser].enabled = False
                except Exception:
                    pass
            except Exception:
                pass

    # ---- internal handlers --------------------------------------------

    def _on_connect(self):
        name = self.current_microscope
        if not name:
            QMessageBox.warning(
                self, "No microscope", "Select a microscope first."
            )
            return
        self.connect_microscope(name)

    def _on_connected(self, pc):
        self._pc = pc
        name = self.current_microscope or ""
        self.status_changed.emit("Loading data for {}…".format(name), 0)
        for w in self._tab_widgets.values():
            w.set_pc(pc)
        powermeter_ok = getattr(pc, "powermeter_available", True)
        self._apply_powermeter_state(powermeter_ok)
        status = "Connected to {}.".format(name)
        if not powermeter_ok:
            status += "  [PowerMeter unavailable]"
        self.status_changed.emit(status, 0)
        self.connected.emit(pc)

    def _apply_powermeter_state(self, available):
        """Grey out calibrate tab and feedback controls when no powermeter."""
        cal = self._tab_widgets.get("calibrate")
        if cal is not None:
            self._tabs.setTabEnabled(self._tabs.indexOf(cal), available)
            cal.set_powermeter_available(available)
        sp = self._tab_widgets.get("set_power")
        if sp is not None:
            sp.set_powermeter_available(available)

    def _on_connect_warning(self, msg):
        QMessageBox.warning(self, "PowerMeter unavailable", msg)

    def _on_connect_error(self, msg):
        self.connect_error.emit(msg)
        QMessageBox.critical(self, "Connection error", msg)
        self.status_changed.emit("Connection failed: {}".format(msg), 8000)

    def _on_connect_finished(self):
        if self._btn_connect is not None:
            self._btn_connect.setEnabled(True)
        if self._scope_combo is not None:
            self._scope_combo.setEnabled(True)

    def _on_calibration_started(self):
        sp = self._tab_widgets.get("set_power")
        if sp is not None:
            self._tabs.setTabEnabled(self._tabs.indexOf(sp), False)
        self.calibration_started.emit()

    def _on_calibration_finished(self):
        sp = self._tab_widgets.get("set_power")
        if sp is not None:
            self._tabs.setTabEnabled(self._tabs.indexOf(sp), True)
            # The run reloaded the shared instrument's calibration; refresh the
            # Set Power tab so its range display reflects the new calibration
            # instead of staying stuck on "not calibrated".
            if hasattr(sp, "refresh_calibration_state"):
                sp.refresh_calibration_state()
        self.calibration_finished.emit()

    def closeEvent(self, event):
        # Only fires when this widget is the top-level window; when embedded,
        # the host's close handler is responsible for calling ``shutdown()``.
        self.shutdown()
        super().closeEvent(event)


class MonetMainWindow(QMainWindow):
    """Standalone top-level Monet window — a thin wrapper around
    :class:`MonetWidget`. Used by ``python -m monet gui``. Hosts wanting to
    embed Monet should use :class:`MonetWidget` directly."""

    def __init__(self, initial_microscope=None):
        super().__init__()
        self.setWindowTitle("{} — Laser Power Calibration".format(_TITLE_BASE))
        self.resize(1200, 850)
        self._widget = MonetWidget(self, initial_microscope=initial_microscope)
        self.setCentralWidget(self._widget)
        self._widget.status_changed.connect(self.statusBar().showMessage)
        self._widget.connected.connect(self._on_connected_title)
        self.statusBar().showMessage("Not connected")

    def set_status(self, msg, timeout_ms=0):
        """Back-compat shim: forward to the status bar."""
        self.statusBar().showMessage(msg, timeout_ms)

    def _on_connected_title(self, pc):
        name = self._widget.current_microscope
        if name:
            self.setWindowTitle("{} — {}".format(_TITLE_BASE, name))

    def closeEvent(self, event):
        self._widget.shutdown()
        event.accept()
