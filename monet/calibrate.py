#!/usr/bin/env python
"""
monet/calibrate.py
~~~~~~~~~~~~~~~~~~

Here, the calibration is performed. This orchestrates attenuation,
power measurement and analysis.

:authors: Heinrich Grabmayr, 2022
:copyright: Copyright (c) 2022 Jungmann Lab, MPI of Biochemistry
"""

import logging
import os
import shutil
import time
from datetime import datetime

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from icecream import ic

import monet.io as io
from monet import (
    DEVICE_TAG,
    LASER_TAG,
    POWER_TAG,
    POWERMETER_BFP,
    POWERMETER_SAMPLE,
    normalize_powermeter_type,
)
from monet.control import IlluminationControl, IlluminationLaserControl
from monet.util import load_class, release_hardware

logger = logging.getLogger(__name__)
ic.configureOutput(outputFunction=logger.debug)


class CalibrationProtocol1D:
    """Calibrate the power of an instrument with one laser power input.

    The attenuator is varied while the power is measured.

    Notes
    -----
    Additional config entries compared to IlluminationControl::

        'powermeter': {
            'classpath': 'monet.powermeter.TestPowerMeter',
            'init_kwargs': {
                'address': 'find connection'}},
    """

    def __init__(self, config, load_instrument=True):
        """Initialize the analyzer, powermeter and attenuator from config.

        Parameters
        ----------
        config : dict
            Keys 'analysis', 'attenuation' and 'powermeter', each with
            sub-keys 'classpath' and 'init_kwargs'.
        load_instrument : bool
            Whether to load the instrument hardware. For inheriting classes
            this option should be disabled.
        """
        # load analysis and attenuation
        if load_instrument:
            self.instrument = IlluminationControl(config, do_load_cal=False)

        # Database index of every calibration written since the last
        # reset_saved_calibrations(); lets a caller undo a whole run.
        self.saved_calibrations = []

        pwrconfig = config["powermeter"]
        try:
            self.powermeter = load_class(
                pwrconfig["classpath"], pwrconfig["init_kwargs"]
            )
            self.powermeter_available = True
            self.powermeter_error = None
        except Exception as exc:
            self.powermeter = None
            self.powermeter_available = False
            # Keep the concrete reason so the GUI / caller can surface *why*
            # the meter failed instead of a generic "not available".
            self.powermeter_error = "{:s}: {!s}".format(
                type(exc).__name__, exc
            )
            logger.warning(
                "PowerMeter (%s) not available: %s",
                pwrconfig.get("classpath", "?"),
                self.powermeter_error,
                exc_info=True,
            )

    def reset_saved_calibrations(self):
        """Forget which calibrations were written, starting a fresh run."""
        self.saved_calibrations = []

    def disconnect(self):
        """Release all hardware held by this protocol.

        Closes the instrument (lasers + attenuator) and the power meter so
        the same devices can be re-opened by a fresh connection. Safe to
        call more than once and on partially-constructed objects.
        """
        instrument = getattr(self, "instrument", None)
        if instrument is not None:
            try:
                instrument.disconnect()
            except Exception:
                logger.debug(
                    "disconnect: instrument teardown failed", exc_info=True
                )
        release_hardware(getattr(self, "powermeter", None))

    def calibrate(
        self,
        wait_time=0.1,
        dry_run=False,
        powermeter_type=POWERMETER_SAMPLE,
        save_plot=True,
        point_callback=None,
        comment=None,
        drop_nonfinite=True,
        drift_check=True,
    ):
        """Calibrate power with parameters from the configuration file.

        Parameters
        ----------
        wait_time : float
            Time to wait between attenuator steps [s].
        dry_run : bool
            If True, calibration is performed but not saved to the database.
        powermeter_type : str
            'sample' (sample plane) or 'bfp' (back focal plane) — annotated
            in the database.
        save_plot : bool
            Whether to save the calibration-curve plot (the 2D protocol
            suppresses it and re-renders projected curves once the
            transmission factor for the run is known).
        point_callback : callable or None
            Called after every attenuator step with (index, total, control
            value, measured power), so a caller can follow the curve live.
        drop_nonfinite : bool
            If True (default), non-finite readings (NaN/inf, e.g. from a
            saturated/over-range meter) are dropped and the curve is fit on
            the remaining finite points, with a warning naming the dropped
            control values. If False, any non-finite reading raises instead.
            Either way, if too few finite points remain to fit, it raises.

        Returns
        -------
        control_par_vals : 1D np array
            The control values (e.g. angles).
        powers : 1D np array
            The measured power.
        """
        minval = self.instrument.config["analysis"]["init_kwargs"]["min"]
        if np.isnan(minval):
            minval = 0
        maxval = self.instrument.config["analysis"]["init_kwargs"]["max"]
        step = self.instrument.config["analysis"]["init_kwargs"]["step"]

        # acquire power data
        control_par_vals = np.arange(minval, maxval + step, step)
        powers = np.zeros_like(control_par_vals, dtype=np.float64)
        for i, ctrlval in enumerate(control_par_vals):
            self.instrument.attenuator.set(ctrlval)
            time.sleep(wait_time)
            powers[i] = self.powermeter.read()
            # print('Position: {:.1f}, Power: {:f}'.format(ctrlval, powers[i]))
            if point_callback:
                point_callback(i, len(control_par_vals), ctrlval, powers[i])

        # Non-finite readings (NaN/inf) come from a saturated/over-range or
        # otherwise-bad meter measurement. Feeding them to lmfit aborts the
        # fit with a cryptic "model function generated NaN values" error (or
        # yields a garbage fit), so handle them here — always logging the full
        # arrays to monet.log for diagnosis.
        bad = ~np.isfinite(powers)
        if bad.any():
            dropped = control_par_vals[bad].tolist()
            logger.warning(
                "%d non-finite power reading(s) at control value(s) %s; "
                "control_par_vals=%s, powers=%s",
                int(bad.sum()),
                dropped,
                control_par_vals,
                powers,
            )
            if not drop_nonfinite:
                raise ValueError(
                    "Power meter returned {:d} non-finite reading(s) "
                    "(NaN/inf) at control value(s) {!s}; cannot fit the "
                    "attenuation curve. Check the meter for "
                    "saturation/over-range or a bad measurement, then "
                    "recalibrate.".format(int(bad.sum()), dropped)
                )
            control_par_vals = control_par_vals[~bad]
            powers = powers[~bad]
            logger.warning(
                "Dropped %d non-finite point(s); fitting on the "
                "remaining %d.",
                int(bad.sum()),
                powers.size,
            )

        if powers.size < 3:
            raise ValueError(
                "Only {:d} finite power reading(s) remain after dropping "
                "non-finite ones; too few to fit the attenuation curve. "
                "Check the power meter (saturation/over-range) and "
                "recalibrate.".format(powers.size)
            )

        # analyze
        self.instrument.analyzer.fit(control_par_vals, powers)
        # print(self.instrument.analyzer.fit_result.fit_report())
        self.instrument.is_calibrated = True

        # Fit-quality diagnostic: how well the fitted model reproduces the
        # calibration data. A large residual points at a poor model / noisy
        # data as the cause of a calibrate-vs-measure deviation; a small
        # residual (with a large live deviation) instead points at drift
        # between calibration and use.
        self.last_fit_quality = self._fit_quality(control_par_vals, powers)
        # Keep the raw curve so model comparison can be run on it later.
        self.last_curve = (
            np.asarray(control_par_vals, dtype=float),
            np.asarray(powers, dtype=float),
        )
        if self.last_fit_quality is not None:
            q = self.last_fit_quality
            logger.info(
                "calibration fit quality: RMS %.1f%%, max %.1f%% at "
                "control value %.3f",
                q["rms_pct"],
                q["max_pct"],
                q["max_at"],
            )
            if q["rms_pct"] > 5.0:
                logger.warning(
                    "calibration fit RMS residual is %.1f%% — the model may "
                    "not describe this attenuator well, so a set power can "
                    "deviate from the reading by a similar amount. Consider "
                    "more calibration points or a different analysis model.",
                    q["rms_pct"],
                )

        # Within-sweep drift: re-read the highest-SNR point after the sweep to
        # detect source drift (laser warm-up/instability, thermal) over the
        # sweep duration. A drift growing across a day of repeated runs points
        # at the source rather than the model as the cause of rising residuals.
        self.last_drift_pct = None
        if drift_check:
            self.last_drift_pct = self._measure_drift(
                control_par_vals, powers, wait_time
            )
            if self.last_drift_pct is not None:
                logger.info(
                    "calibration within-sweep drift: %+.1f%% at the "
                    "highest-SNR point",
                    self.last_drift_pct,
                )
        if self.last_fit_quality is not None:
            self.last_fit_quality["drift_pct"] = self.last_drift_pct

        self._log_fit_quality_history(control_par_vals)

        self.save_calibration(
            save_plot=save_plot,
            dry_run=dry_run,
            powermeter_type=powermeter_type,
            ctrl_vals=control_par_vals,
            powers=powers,
            comment=comment,
        )

        return control_par_vals, powers

    def _fit_quality(self, x, y):
        """Relative residual of the fitted model against the calibration data.

        Parameters
        ----------
        x, y : 1d arrays
            The control values and the measured powers that were fit.

        Returns
        -------
        dict or None
            ``{'rms_pct', 'max_pct', 'max_at'}`` — the RMS and maximum relative
            residual (percent) and the control value of the worst point — or
            ``None`` if it cannot be evaluated.
        """
        try:
            pred = np.asarray(
                self.instrument.analyzer.estimate_power(x), dtype=float
            )
            y = np.asarray(y, dtype=float)
            pred = np.broadcast_to(pred, y.shape).astype(float)
            ok = np.isfinite(pred) & np.isfinite(y) & (y > 0)
            if not ok.any():
                return None
            rel = np.abs(y[ok] - pred[ok]) / y[ok]
            imax = int(np.argmax(rel))
            return {
                "rms_pct": float(np.sqrt(np.mean(rel**2)) * 100.0),
                "max_pct": float(np.max(rel) * 100.0),
                "max_at": float(np.asarray(x)[ok][imax]),
            }
        except Exception as exc:
            logger.debug("Could not compute fit quality: %s", exc)
            return None

    def _measure_drift(self, control_par_vals, powers, wait_time):
        """Re-measure the highest-SNR point to gauge source drift over a sweep.

        Returns the relative change (percent) at that control value between the
        sweep reading and a fresh reading taken after the whole sweep, or None.
        """
        try:
            powers = np.asarray(powers, dtype=float)
            finite = np.isfinite(powers) & (powers > 0)
            if not finite.any():
                return None
            idx = np.where(finite)[0]
            i_ref = int(idx[int(np.argmax(powers[idx]))])
            ref = float(np.asarray(control_par_vals, dtype=float)[i_ref])
            p_start = float(powers[i_ref])
            self.instrument.attenuator.set(ref)
            time.sleep(wait_time)
            p_end = float(self.powermeter.read())
            if p_start and np.isfinite(p_end):
                return (p_end - p_start) / p_start * 100.0
        except Exception as exc:
            logger.debug("drift check failed: %s", exc)
        return None

    def _log_fit_quality_history(self, control_par_vals):
        """Append this calibration's fit quality + drift to a durable log.

        Writes one row to ``fit_quality_log.csv`` (in the plot folder, or the
        database's folder for a local DB) so RMS / max / drift can be tracked
        and monitored across runs and days.
        """
        q = self.last_fit_quality or {}
        idx = self.instrument.config.get("index", {}) or {}
        folder = self.instrument.config.get("dest_calibration_plot")
        if not folder:
            db = self.instrument.config.get("database")
            if db and not io._is_server_url(db):
                folder = os.path.dirname(db) or "."
        model = self.instrument.config.get("analysis", {}).get("classpath", "")
        record = {
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "device": idx.get("name"),
            "laser": idx.get(LASER_TAG, idx.get("wavelength [nm]")),
            "laser_power": idx.get(POWER_TAG, idx.get("laser_power [mW]")),
            "model": model.rsplit(".", 1)[-1],
            "rms_pct": q.get("rms_pct"),
            "max_pct": q.get("max_pct"),
            "max_at": q.get("max_at"),
            "drift_pct": getattr(self, "last_drift_pct", None),
            "n_points": int(np.size(control_par_vals)),
        }
        io.append_fit_quality_log(folder, record)

    def _verify_angles(
        self,
        analyzer,
        n_angles,
        wait_time,
        laser,
        level,
        point_callback,
        predictors=None,
        model_devs=None,
    ):
        """Measure n_angles across the calibrated range for one model.

        If ``predictors`` (from :func:`analysis.fit_candidate_models`) and
        ``model_devs`` (a name -> list accumulator) are given, each measured
        point's deviation from every candidate model is also recorded, so all
        models can be compared against the same fresh measurements.
        """
        import time

        params = getattr(analyzer, "analysis_parameters", {}) or {}
        lo = params.get("min", 0)
        hi = params.get("max", 180)
        if not np.isfinite(lo):
            lo = 0
        if not np.isfinite(hi):
            hi = 180
        out = []
        for ang in np.linspace(lo, hi, n_angles):
            ang = float(ang)
            self.instrument.attenuator.set(ang)
            time.sleep(wait_time)
            measured = float(self.powermeter.read())
            predicted = float(analyzer.estimate_power(ang))
            if predicted and np.isfinite(measured):
                dev = (measured - predicted) / predicted * 100.0
            else:
                dev = float("nan")
            rec = {
                "laser": laser,
                "laser_power": level,
                "angle": ang,
                "measured": measured,
                "predicted": predicted,
                "dev_pct": dev,
            }
            out.append(rec)
            if predictors and model_devs is not None:
                for p in predictors:
                    try:
                        pv = float(p["predict"](ang))
                    except Exception:
                        pv = float("nan")
                    if pv and np.isfinite(measured) and np.isfinite(pv):
                        md = (measured - pv) / pv * 100.0
                    else:
                        md = float("nan")
                    model_devs.setdefault(p["model"], []).append(md)
            if point_callback:
                point_callback(rec)
        return out

    def verify_calibration(
        self,
        n_angles=5,
        wait_time=0.5,
        switch_time=5,
        powermeter_type=POWERMETER_BFP,
        manage_laser_state=True,
        point_callback=None,
        compare_degrees=(3, 4, 5, 6),
    ):
        """Re-measure a few attenuator angles and compare to the fitted model.

        A *fresh* cross-check of the stored calibration (unlike
        :meth:`_fit_quality`, which only scores how well the model fit its own
        acquisition): for each calibrated laser and laser-power level, enable
        the laser and route the beam to the meter (exactly as a calibration
        does), then move the attenuator to ``n_angles`` positions across the
        calibrated range, read the meter, and compare to the model prediction
        in the meter's own units (so the objective transmission factor is not
        involved). This catches drift and laser-power-setting effects the fit
        residual cannot — e.g. if the deviation grows only at certain laser
        powers, ``dev_pct`` grouped by ``laser_power`` shows it.

        Parameters
        ----------
        n_angles : int
            Attenuator positions to test per laser-power level.
        wait_time : float
            Seconds to wait after each move / laser-power change before reading.
        switch_time : float
            Seconds to wait after enabling a laser / routing the beam.
        powermeter_type : str
            'bfp' or 'sample' — selects the beam-path routing (turret to the
            powermeter port for BFP).
        manage_laser_state : bool
            If True, switch the verified lasers off again at the end.
        point_callback : callable or None
            Called with each per-point dict as it is measured (for live UI).
        compare_degrees : iterable of int or None
            If set (and the raw calibration curves are available from this
            session), also evaluate the fresh measurements against the
            sinusoidal model and polynomials of these degrees. Comparing the
            per-model *verify* residual to each model's *fit* residual
            separates model accuracy (a better-fitting model verifies better)
            from repeatability/drift (all models verify similarly).

        Returns
        -------
        dict
            ``{'points': [...], 'rms_pct', 'max_pct', 'model_summary'}`` where
            ``model_summary`` maps each model to
            ``{fit_rms_pct, verify_rms_pct, verify_max_pct}`` (empty if the raw
            curves are unavailable).
        """
        import time

        from monet.analysis import fit_candidate_models

        inst = self.instrument
        if not getattr(inst, "is_calibrated", False):
            raise ValueError("Not calibrated. Calibrate before verifying.")
        powermeter_type = normalize_powermeter_type(powermeter_type)

        protocol = getattr(self, "protocol", None)
        is_2d = bool(
            protocol
            and hasattr(inst, "laser")
            and hasattr(inst, "laser_enabled")
        )
        ana_params = inst.config["analysis"]["init_kwargs"]
        model_devs = {}
        model_fit = {}

        def _predictors_for(laser, level):
            if not compare_degrees:
                return None
            if is_2d:
                curve = getattr(self, "last_curves", {}).get((laser, level))
            else:
                curve = getattr(self, "last_curve", None)
            if curve is None:
                return None
            preds = fit_candidate_models(
                curve[0], curve[1], ana_params, compare_degrees
            )
            for p in preds:
                model_fit.setdefault(p["model"], []).append(p["fit_rms_pct"])
            return preds

        points = []
        if not is_2d:
            # 1D: single calibration; the laser is assumed already on.
            points = self._verify_angles(
                inst.analyzer,
                n_angles,
                wait_time,
                None,
                None,
                point_callback,
                predictors=_predictors_for(None, None),
                model_devs=model_devs,
            )
        else:
            lasers = [
                las
                for las in protocol["laser_sequence"]
                if las in getattr(inst, "lasers", {})
            ]
            bp_dict = protocol.get("beampath") or {}
            orig_laser = getattr(inst, "curr_laser", None)
            orig_power = getattr(inst, "curr_laserpower", None)
            # Snapshot each laser's on/off state so a read-only verify restores
            # it (rather than leaving the user's lit laser switched off).
            orig_enabled = {}
            for las in lasers:
                try:
                    orig_enabled[las] = inst.lasers[las].enabled
                except Exception:
                    orig_enabled[las] = False
            try:
                for laser in lasers:
                    # enable the laser and route the beam to the meter
                    inst.laser = laser  # re-populates analyzers/power_ranges
                    inst.laser_enabled = True
                    if getattr(inst, "use_beampath", False):
                        try:
                            if laser in bp_dict:
                                inst.beampath.positions = bp_dict[laser]
                            if powermeter_type == POWERMETER_BFP:
                                scp = bp_dict.get("start_calibrate")
                                if scp:
                                    inst.beampath.positions = scp
                        except Exception as exc:
                            logger.warning(
                                "verify: could not set beam path for %s: %s",
                                laser,
                                exc,
                            )
                    try:
                        inst.attenuator.set_wavelength(laser)
                        self.powermeter.wavelength = int(laser)
                    except Exception:
                        pass
                    time.sleep(switch_time)

                    pr = getattr(inst, "_power_ranges", None)
                    levels = list(pr.index) if pr is not None else []
                    for level in levels:
                        inst.laserpower = (
                            level  # sets power + selects analyzer
                        )
                        if "amp" in getattr(self.powermeter, "config", {}):
                            self.powermeter.config["amp"] = level
                        time.sleep(wait_time)
                        points.extend(
                            self._verify_angles(
                                inst.analyzer,
                                n_angles,
                                wait_time,
                                laser,
                                level,
                                point_callback,
                                predictors=_predictors_for(laser, level),
                                model_devs=model_devs,
                            )
                        )
            finally:
                # Restore the selected laser/power first (selecting a laser may
                # auto-enable it), then restore each laser's prior on/off state.
                if orig_laser is not None:
                    try:
                        inst.laser = orig_laser
                        if orig_power is not None:
                            inst.laserpower = orig_power
                    except Exception:
                        pass
                if manage_laser_state:
                    for las in lasers:
                        try:
                            inst.lasers[las].enabled = orig_enabled.get(
                                las, False
                            )
                        except Exception:
                            pass

        devs = np.array(
            [p["dev_pct"] for p in points if np.isfinite(p["dev_pct"])],
            dtype=float,
        )
        rms = float(np.sqrt(np.mean(devs**2))) if devs.size else float("nan")
        mx = float(np.max(np.abs(devs))) if devs.size else float("nan")

        model_summary = {}
        for name, mdevs in model_devs.items():
            arr = np.array([d for d in mdevs if np.isfinite(d)], dtype=float)
            vrms = (
                float(np.sqrt(np.mean(arr**2))) if arr.size else float("nan")
            )
            vmax = float(np.max(np.abs(arr))) if arr.size else float("nan")
            fits = model_fit.get(name, [])
            frms = float(np.mean(fits)) if fits else float("nan")
            model_summary[name] = {
                "fit_rms_pct": frms,
                "verify_rms_pct": vrms,
                "verify_max_pct": vmax,
            }

        logger.info(
            "calibration verification: %d point(s), RMS %.1f%%, "
            "max |dev| %.1f%%",
            len(points),
            rms,
            mx,
        )
        for name, s in sorted(
            model_summary.items(), key=lambda kv: kv[1]["verify_rms_pct"]
        ):
            logger.info(
                "  verify model %s: fit RMS %.1f%%, verify RMS %.1f%%",
                name,
                s["fit_rms_pct"],
                s["verify_rms_pct"],
            )
        return {
            "points": points,
            "rms_pct": rms,
            "max_pct": mx,
            "model_summary": model_summary,
        }

    def save_calibration(
        self,
        save_plot=True,
        dry_run=False,
        powermeter_type=POWERMETER_SAMPLE,
        ctrl_vals=None,
        powers=None,
        comment=None,
    ):
        """Save the calibration to the database.

        Parameters
        ----------
        save_plot : bool
            Whether to save a plot of the calibration.
        dry_run : bool
            If True, skip writing to the database.
        powermeter_type : str
            'sample' (sample plane) or 'bfp' (back focal plane) — stored as
            a column in the database.
        ctrl_vals, powers : 1D arrays, optional
            The raw control values and measured powers, plotted as the data
            points behind the fitted curve when available.
        comment : str or None
            Free-text note for this run (e.g. 'laser status orange today'),
            stored as a ``comment`` column alongside the calibration.
        """
        powermeter_type = normalize_powermeter_type(powermeter_type)
        cali_pars = self.instrument.analyzer.get_model()
        cali_pars["powermeter_type"] = powermeter_type
        if comment:
            cali_pars["comment"] = comment

        fname = self.instrument.config["database"]
        if not dry_run:
            indexnames, indexvals = io.save_calibration(
                fname, self.instrument.config["index"], cali_pars
            )
            self.saved_calibrations.append(
                {k: v for k, v in zip(indexnames, indexvals)}
            )

        if save_plot:
            laser = self.instrument.config["index"].get(LASER_TAG)
            lpwr = self.instrument.config["index"].get(POWER_TAG)
            if laser is not None:
                self._save_curve_plot(
                    laser,
                    lpwr,
                    ctrl_vals,
                    powers,
                    self.instrument.analyzer.get_model(),
                    powermeter_type,
                )

    def _sample_plane_factor(self, laser, powermeter_type):
        """Transmission factor (P_sample / P_bfp) used to project a saved plot
        to the sample plane.

        Returns 1.0 for sample-plane calibrations (already in sample units) and
        for back-focal-plane calibrations with no transmission factor stored
        yet; otherwise the latest factor recorded for this device / laser.
        """
        if normalize_powermeter_type(powermeter_type) != POWERMETER_BFP:
            return 1.0
        try:
            device = self.instrument.config["index"][DEVICE_TAG]
            factors_df = io.load_factors(
                self.instrument.config["database"], device=device, laser=laser
            )
            if factors_df is not None and not factors_df.empty:
                sub = factors_df.loc[
                    factors_df.index.get_level_values(LASER_TAG) == int(laser)
                ]
                if not sub.empty:
                    return float(sub.iloc[-1]["transmission_objective_mean"])
        except Exception as exc:
            logger.debug(
                "Could not load transmission factor for %s nm: %s", laser, exc
            )
        return 1.0

    def _curve_plot_title(self, laser, lpwr, powermeter_type, projected):
        """Build the plot title.

        Names the power-meter position and whether the values are
        sample-plane projected.
        """
        pm = normalize_powermeter_type(powermeter_type)
        if pm == POWERMETER_BFP:
            plane = (
                "back focal plane → sample plane (projected)"
                if projected
                else "back focal plane (raw, no transmission factor yet)"
            )
        else:
            plane = "sample plane"
        return "power calibration — {:d} nm, {} mW\n{}".format(
            int(laser), lpwr, plane
        )

    def _save_curve_plot(
        self, laser, lpwr, ctrl_vals, powers, model_pars, powermeter_type
    ):
        """Save a single attenuation curve as '<wl>nm_<power>mW.png'.

        Saves into the plot folder, overwriting any previous file for that
        wavelength/power so only the newest (and latest power-meter
        position) is kept. Power values are projected to the sample plane
        when a transmission factor is available.
        """
        folder = self.instrument.config.get("dest_calibration_plot")
        if folder is None:
            fname = self.instrument.config["database"]
            folder = (
                os.getcwd()
                if io._is_server_url(fname)
                else os.path.split(fname)[0]
            )

        factor = self._sample_plane_factor(laser, powermeter_type)
        projected = (
            normalize_powermeter_type(powermeter_type) == POWERMETER_BFP
            and factor != 1.0
        )

        plt.switch_backend("agg")
        fig, ax = plt.subplots()

        # measured data points (projected to the sample plane)
        if ctrl_vals is not None and powers is not None:
            ax.plot(
                np.asarray(ctrl_vals, dtype=float),
                np.asarray(powers, dtype=float) * factor,
                marker="x",
                linestyle="none",
                label="measured",
            )

        # fitted model curve, evaluated over the control range and projected
        try:
            analyzer = load_class(
                self.instrument.config["analysis"]["classpath"],
                self.instrument.config["analysis"]["init_kwargs"],
            )
            analyzer.load_model(model_pars)
            init = self.instrument.config["analysis"]["init_kwargs"]
            grid = np.linspace(init["min"], init["max"], 200)
            ax.plot(
                grid,
                np.array([analyzer.estimate_power(g) for g in grid]) * factor,
                label="fit",
            )
            ax.legend()
        except Exception as exc:
            logger.debug(
                "Could not overlay fitted curve for %s nm: %s", laser, exc
            )

        ax.set_xlabel("attenuator control value")
        ax.set_ylabel("Power [{:s}]".format(self.powermeter.unit))
        ax.grid(True)
        ax.set_title(
            self._curve_plot_title(laser, lpwr, powermeter_type, projected)
        )
        fig.tight_layout()
        fnplot = os.path.join(
            folder, "{:d}nm_{}mW.png".format(int(laser), lpwr)
        )
        fig.savefig(fnplot)
        plt.close(fig)


class CalibrationProtocol2D(CalibrationProtocol1D):
    """Calibrate different lasers at different power settings."""

    def __init__(self, config, protocol):
        """Initialize the 2D calibration protocol.

        Parameters
        ----------
        config : dict
            The configuration, with entries the union of those necessary
            for CalibrationProtocol1D and IlluminationLaserControl.
        protocol : dict
            Required keys ``'laser_sequence'`` (list of lasers matching
            'laser' keys in config) and ``'laser_powers'`` (dict of laser
            keys and lists of respective laser powers); optional key
            ``'beampath'`` (dict of laser keys and dicts of respective
            beampath object settings for object ids as set in the
            'beampath' section of config).
        """
        self.protocol = protocol

        self.instrument = IlluminationLaserControl(config, do_load_cal=False)

        # if not all lasers are present
        lasers_present = list(self.instrument.lasers.keys())
        self.protocol["laser_sequence"] = [
            it
            for it in self.protocol["laser_sequence"]
            if it in lasers_present
        ]
        self.protocol["laser_powers"] = {
            k: v
            for k, v in self.protocol["laser_powers"].items()
            if k in lasers_present
        }
        self.protocol["beampath"] = {
            k: v
            for k, v in self.protocol["beampath"].items()
            if k in lasers_present
            or k == "end"
            or k == "start_calibrate"
            or k == "end_calibrate"
        }

        super().__init__(config, load_instrument=False)

    def run_protocol(
        self,
        wait_time=0,
        switch_time=10,
        laser_filter=None,
        dry_run=False,
        progress_callback=None,
        curve_callback=None,
        point_callback=None,
        manage_laser_state=True,
        powermeter_type="manual",
        power_filter=None,
        comment=None,
        drop_nonfinite=True,
    ):
        """Run a protocol over lasers and power settings.

        Loop through lasers and respective power settings, doing
        calibrations and saving them for every combination.

        Parameters
        ----------
        comment : str or None
            Free-text note stored with every calibration written in this run.
        power_filter : dict or None
            If given, ``{laser: iterable of laser powers}``; only those power
            levels are (re-)measured for the listed lasers. Used to recalibrate
            individual failed points. When active, the run is treated as
            partial: the aggregate per-laser plots (model history, measured
            power table) are left untouched so they are not overwritten with
            an incomplete set of powers.
        wait_time : float
            Time to wait between attenuator steps [s].
        switch_time : float
            Time to wait after switching laser [s].
        laser_filter : list or None
            If not None, only calibrate lasers in this list.
        dry_run : bool
            If True, calibration is performed but not saved to the database.
        progress_callback : callable or None
            Called after each power step with (step, total, laser, lpwr).
        curve_callback : callable or None
            Called after each power step with the raw attenuation curve
            (laser, lpwr, control values, measured powers).
        point_callback : callable or None
            Called after every attenuator step with (laser, lpwr, index,
            total, control value, measured power), so a caller can follow
            each curve as it is acquired.
        manage_laser_state : bool
            If True (CLI mode), switch off all lasers at start and after
            each wavelength. If False (GUI mode), leave laser state as-is.
        powermeter_type : str
            'sample' (sample plane) or 'bfp' (back focal plane) — annotated
            in every saved calibration.
        drop_nonfinite : bool
            Passed through to :meth:`calibrate`; if True (default), non-finite
            readings are dropped and the curve is fit on the remaining points.
        """
        powermeter_type = normalize_powermeter_type(powermeter_type)
        plotfolder = self.instrument.config.get("dest_calibration_plot")
        self.reset_saved_calibrations()
        # Per-curve fit quality and raw curves, keyed (laser, laser_power).
        self.fit_qualities = {}
        self.last_curves = {}

        lasers = [
            las
            for las in self.protocol["laser_sequence"]
            if laser_filter is None or las in laser_filter
        ]

        partial = power_filter is not None

        def _powers_for(las):
            """Laser powers to measure for ``las`` after the power filter."""
            lps = self.protocol["laser_powers"][las]
            if partial and power_filter.get(las) is not None:
                allowed = {float(a) for a in power_filter[las]}
                lps = [lp for lp in lps if float(lp) in allowed]
            return lps

        # delete plots belonging to the lasers being calibrated so stale
        # power levels do not linger. Per-curve files are named
        # '<wl>nm_<power>mW.png' and model/meas plots '<wl>nm.png' /
        # 'pwrmeasured_<wl>nm.*'; all are overwritten on re-run, but
        # pruning removes powers no longer calibrated. Skipped for a partial
        # (single-point recalibration) run so untouched powers keep their
        # plots.
        if not partial:
            laser_ints = {int(las) for las in lasers}
            for fname in os.listdir(plotfolder):
                matched = any(
                    fname
                    in (
                        "{:d}nm.png".format(li),
                        "pwrmeasured_{:d}nm.png".format(li),
                        "pwrmeasured_{:d}nm.xlsx".format(li),
                    )
                    or (
                        fname.startswith("{:d}nm_".format(li))
                        and fname.endswith("mW.png")
                    )
                    # legacy timestamped curve files from older versions
                    or "wavelength (nm)-{:d}_".format(li) in fname
                    for li in laser_ints
                )
                if matched:
                    try:
                        os.remove(os.path.join(plotfolder, fname))
                    except Exception:
                        pass
        total = sum(len(_powers_for(las)) for las in lasers)
        step = 0

        if manage_laser_state:
            # switch off all lasers
            for laser in self.protocol["laser_sequence"]:
                self.instrument.laser = laser
                self.instrument.laser_enabled = False

        # now start calibration
        for laser in lasers:
            print("switching to laser", laser)
            self.instrument.laser = laser
            self.instrument.laser_enabled = True
            laserpowers = _powers_for(laser)
            if not laserpowers:
                continue
            if self.instrument.use_beampath:
                self.instrument.beampath.positions = self.protocol["beampath"][
                    laser
                ]
                if powermeter_type == POWERMETER_BFP:
                    start_cal_pos = self.protocol["beampath"].get(
                        "start_calibrate"
                    )
                    if start_cal_pos:
                        self.instrument.beampath.positions = start_cal_pos
            self.instrument.attenuator.set_wavelength(laser)
            modelpars = pd.DataFrame(index=laserpowers)
            measpwrs = pd.DataFrame(columns=laserpowers)
            # set powermeter setting
            self.powermeter.wavelength = int(laser)
            # self.instrument.config['index'][LASER_TAG] = laser
            time.sleep(switch_time)
            for lpwr in laserpowers:
                print("setting laser power to", lpwr, "mW")
                self.instrument.laserpower = lpwr

                if "amp" in self.powermeter.config.keys():
                    # this is a test powermeter. set amplitude
                    self.powermeter.config["amp"] = lpwr

                if point_callback:

                    def _on_point(i, n, ctrl, pwr, las=laser, lp=lpwr):
                        point_callback(las, lp, i, n, ctrl, pwr)

                else:
                    _on_point = None

                # suppress the per-step plot; projected curves are rendered
                # below once the transmission factor for this run is known
                angles, powers = self.calibrate(
                    wait_time=wait_time,
                    dry_run=dry_run,
                    powermeter_type=powermeter_type,
                    save_plot=False,
                    point_callback=_on_point,
                    comment=comment,
                    drop_nonfinite=drop_nonfinite,
                )
                if getattr(self, "last_fit_quality", None) is not None:
                    self.fit_qualities[(laser, lpwr)] = self.last_fit_quality
                if getattr(self, "last_curve", None) is not None:
                    self.last_curves[(laser, lpwr)] = self.last_curve
                for an, pw in zip(angles, powers):
                    measpwrs.loc[an, lpwr] = pw

                if curve_callback:
                    curve_callback(laser, lpwr, angles, powers)

                # get model parameters for plotting
                model_dict = self.instrument.analyzer.get_model()
                for k, v in model_dict.items():
                    modelpars.loc[lpwr, k] = v
                # calibration state is always set True in each 1D calibration
                self.instrument.is_calibrated = False

                step += 1
                if progress_callback:
                    progress_callback(step, total, laser, lpwr)

            self.instrument.laserpower = min(laserpowers)
            if manage_laser_state:
                self.instrument.laser_enabled = False
            # Compute the transmission factor first so the plots below can be
            # projected to the sample plane when a paired calibration exists.
            if not dry_run:
                io.compute_and_save_factor(
                    self.instrument.config["database"],
                    self.instrument.config["index"][DEVICE_TAG],
                    laser,
                    self.instrument.config["analysis"],
                )
            # For a partial recalibration ``modelpars`` / ``measpwrs`` only
            # hold the re-measured powers, so leave the aggregate plots — which
            # should show every power — as the last full run left them.
            if not partial:
                self.plot_model(modelpars, laser)
                self.save_measvals(measpwrs, laser, powermeter_type)
            # Render one projected attenuation curve per power level, named
            # '<wl>nm_<power>mW.png' so only the newest of each is kept.
            for lpwr in laserpowers:
                try:
                    col = measpwrs[lpwr]
                    self._save_curve_plot(
                        laser,
                        lpwr,
                        col.index.to_numpy(),
                        col.to_numpy(),
                        modelpars.loc[lpwr].to_dict(),
                        powermeter_type,
                    )
                except Exception as exc:
                    logger.debug(
                        "Could not save curve plot %s nm / %s mW: %s",
                        laser,
                        lpwr,
                        exc,
                    )
        self.plot_device_history()
        # post-actions
        # move beampath to end_calibrate position
        if end_pos := self.protocol["beampath"].get("end_calibrate"):
            self.instrument.beampath.positions = end_pos
        # move beampath to general end position (also used for shutdown)
        if (
            self.instrument.use_beampath
            and "end" in self.protocol["beampath"].keys()
        ):
            self.instrument.beampath.positions = self.protocol["beampath"][
                "end"
            ]
        # Belt-and-braces: make sure autoshutter is left on for normal
        # imaging. Closing the shutter already re-enables it (see
        # NikonShutter.position), but the final beam-path move above may not
        # touch the shutter, so re-enable it explicitly here too.
        if self.instrument.use_beampath:
            shutter = self.instrument.beampath.objects.get("shutter")
            if shutter is not None:
                try:
                    shutter.autoshutter = True
                except Exception:
                    logger.debug(
                        "Could not re-enable autoshutter after " "calibration",
                        exc_info=True,
                    )
        # Reload the freshly-written calibrations so the shared instrument is
        # left calibrated: each 1D step above toggles ``is_calibrated`` off, so
        # without this a run would leave the instrument reporting "no
        # calibration available" — breaking other surfaces on the same
        # connection (e.g. the Set Power tab) until reconnect. Guarded so a
        # reload failure never masks an otherwise-successful run.
        try:
            self.instrument.load_calibration_database()
        except Exception:
            logger.debug(
                "Could not reload calibration database after run",
                exc_info=True,
            )

        # copy all plots from local folder into a timestamped archive folder
        # (file-based DB only, and only when a plot folder is configured)
        lfolder = self.instrument.config.get("dest_calibration_plot")
        if lfolder and not io._is_server_url(
            self.instrument.config["database"]
        ):
            device = self.instrument.config["index"][DEVICE_TAG]
            sfolder = os.path.join(
                os.path.split(self.instrument.config["database"])[0],
                "Calibrations",
                datetime.now().strftime("%y%m%d-%H%M") + "_" + device,
            )
            # dirs_exist_ok lets copytree create sfolder itself and tolerates
            # re-runs within the same minute.
            shutil.copytree(lfolder, sfolder, dirs_exist_ok=True)

    def plot_model(self, modeldf, laser):
        plt.switch_backend("agg")
        fig, ax = plt.subplots(
            nrows=len(modeldf.columns), sharex=True, squeeze=False
        )
        for i, col in enumerate(modeldf.columns):
            ax[i, 0].plot(
                modeldf.index.to_numpy(), modeldf[col].to_numpy(), marker="x"
            )
            ax[i, 0].set_ylabel(str(col))
        ax[-1, 0].set_xlabel("laser power [mW]")
        fig.suptitle("laser {:d} nm".format(int(laser)))

        fname = self.instrument.config["database"]
        folder = self.instrument.config.get("dest_calibration_plot")
        if folder is None:
            folder = (
                os.getcwd()
                if io._is_server_url(fname)
                else os.path.split(fname)[0]
            )
        fnplot = os.path.join(folder, "{:d}nm".format(int(laser)) + ".png")
        fig.savefig(fnplot)
        plt.close(fig)

    def save_measvals(self, measdf, laser, powermeter_type=POWERMETER_SAMPLE):
        """Save measured values as an Excel sheet and png.

        Values are projected to the sample plane when a transmission factor
        is available.

        Parameters
        ----------
        measdf : pandas DataFrame
            Measured powers; index = attenuator control values, columns =
            laser power levels.
        laser : int or str
            Laser wavelength.
        powermeter_type : str
            'sample' or 'bfp' — selects whether projection applies.
        """
        fname = self.instrument.config["database"]
        folder = self.instrument.config.get("dest_calibration_plot")
        if folder is None:
            folder = (
                os.getcwd()
                if io._is_server_url(fname)
                else os.path.split(fname)[0]
            )

        factor = self._sample_plane_factor(laser, powermeter_type)
        projected = (
            normalize_powermeter_type(powermeter_type) == POWERMETER_BFP
            and factor != 1.0
        )
        # measpwrs is assembled via .loc and can be object dtype; coerce so the
        # multiplication and rounding below behave numerically.
        measdf = measdf.apply(pd.to_numeric, errors="coerce") * factor

        fnxlsx = os.path.join(
            folder, "pwrmeasured_{:d}nm".format(int(laser)) + ".xlsx"
        )
        measdf.to_excel(fnxlsx)

        plt.switch_backend("agg")
        fig, ax = plt.subplots()
        ax.xaxis.set_visible(False)
        ax.yaxis.set_visible(False)
        ax.axis("off")
        tab = pd.plotting.table(ax, measdf.round(3), loc="center")
        for c in tab.get_celld().values():
            c.visible_edges = "horizontal"
        fig.tight_layout()
        ax.set_title(
            "measured powers in mW\n"
            + self._curve_plot_title(
                laser, "", powermeter_type, projected
            ).split("\n")[-1]
        )
        fnplot = os.path.join(
            folder, "pwrmeasured_{:d}nm".format(int(laser)) + ".png"
        )
        fig.tight_layout()
        plt.savefig(fnplot)
        plt.close(fig)

    def plot_device_history(self):
        """Plot the historic evolution of model parameters."""
        plt.switch_backend("agg")
        device = self.instrument.config["index"][DEVICE_TAG]
        plot_dir = self.instrument.config.get("dest_calibration_plot")
        db_fname = self.instrument.config["database"]
        io.plot_device_history(db_fname, device, plot_dir)
        anaconfig = self.instrument.config["analysis"]
        analyzer = load_class(anaconfig["classpath"], anaconfig["init_kwargs"])
        io.plot_device_amplitude_history(db_fname, device, plot_dir, analyzer)
