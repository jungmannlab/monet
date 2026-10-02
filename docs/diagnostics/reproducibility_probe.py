#!/usr/bin/env python
"""
docs/diagnostics/reproducibility_probe.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Rig diagnostic that isolates the individual sources of calibrate-vs-measure
irreproducibility, so a long unattended run on the microscope can attribute a
deviation to a specific cause instead of guessing.

Each experiment targets ONE error source and appends to its own CSV (one row
per measurement, every row timestamped), so the series can be plotted and
cross-correlated afterwards:

  laser_stability     laser on, fixed attenuator angle, read power over time
                      -> laser warm-up / power instability (+ meter noise)
  meter_dark          laser off, read meter over time
                      -> meter dark/zero drift, ambient light (the noise floor)
  repeatability       set the SAME angle repeatedly, always approached from one
                      direction, read power
                      -> attenuator positioning repeatability (backlash removed)
  hysteresis          re-approach one angle from below vs above (backlash probe)
                      -> rotation-mount backlash
  homing              read at an angle, home the mount, return, read again
                      -> whether homing shifts the angle->power mapping
  calibration         run a full calibration sweep + fit repeatedly; record fit
                      params, RMS/max residual, within-sweep drift, and
                      predicted-vs-measured at reference angles
                      -> run-to-run fit variation and model adequacy
  setpower            set a target power open-loop from the calibration, measure
                      the actual power
                      -> the end-to-end reproducibility you ultimately care about

Run the whole battery on a loop for hours (``--cycles`` / ``--cycle-interval``)
to see how each metric evolves with warm-up and time of day.

Usage
-----
Dry-run (no rig; simulated Test* hardware, just proves the script works)::

    python docs/diagnostics/reproducibility_probe.py --test --cycles 1 \
        --duration 5 --interval 1 --reps 5

On a rig (uses the microscope's monet config, exactly like calibration does)::

    python docs/diagnostics/reproducibility_probe.py MyScope \
        --laser 488 --laser-power 100 --angle 75 \
        --experiments laser_stability,repeatability,hysteresis,homing,\
calibration,setpower,meter_dark \
        --duration 1800 --interval 10 --reps 30 --cycles 8 \
        --cycle-interval 600 --outdir repro_$(date +%Y%m%d_%H%M)

The laser is switched off and the hardware released on exit (including Ctrl-C).
This script is a stand-alone diagnostic; it is not imported by the package.
"""

import argparse
import csv
import os
import signal
import sys
import time
from datetime import datetime

import numpy as np

import monet
import monet.analysis as analysis
import monet.calibrate as mca
from monet import POWERMETER_BFP, normalize_powermeter_type

# A fully-simulated config for --test (no rig, no vendor SDKs) so the script
# can be smoke-tested end to end.
_TEST_CONFIG = {
    "database": None,  # set to a temp file in _build_pc
    "dest_calibration_plot": None,
    "index": {"name": "TestScope"},
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
_TEST_PROTOCOL = {
    "laser_sequence": [488],
    "laser_powers": {488: [100, 200]},
    "beampath": {488: {"shutter01": True}},
}

ALL_EXPERIMENTS = [
    "laser_stability",
    "meter_dark",
    "repeatability",
    "hysteresis",
    "homing",
    "calibration",
    "setpower",
    "setpower_breakdown",
    "model_sweep",
    "drift_curve",
    "power_warmup",
]


class _Stop(Exception):
    """Raised to unwind cleanly on Ctrl-C."""


class ReproducibilityProbe:
    """Runs the isolation experiments against a monet instrument + meter."""

    def __init__(self, pc, outdir, args):
        self.pc = pc
        self.instrument = pc.instrument
        self.powermeter = pc.powermeter
        self.outdir = outdir
        self.args = args
        self.t0 = time.time()
        self.cycle = 0
        self.phase = None  # set by the weekend plan to tag CSV rows
        self._writers = {}  # name -> (file, csv.writer, fieldnames)
        os.makedirs(outdir, exist_ok=True)
        # Tell the instrument where the meter physically is, so the
        # transmission-factor / to_sample_plane logic is applied consistently.
        try:
            self.instrument.powermeter_position = args.powermeter_type
        except Exception:
            pass

    # ---- infrastructure ---------------------------------------------------

    def _log(self, name, fields, row):
        """Append one row to <outdir>/<name>.csv (header written once)."""
        base = ["iso_time", "elapsed_s", "cycle", "phase"]
        header = base + fields
        if name not in self._writers:
            f = open(os.path.join(self.outdir, name + ".csv"), "a", newline="")
            w = csv.DictWriter(f, fieldnames=header)
            if f.tell() == 0:
                w.writeheader()
            self._writers[name] = (f, w, header)
        f, w, header = self._writers[name]
        full = {
            "iso_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": round(time.time() - self.t0, 3),
            "cycle": self.cycle,
            "phase": self.phase,
        }
        full.update(row)
        w.writerow({k: full.get(k) for k in header})
        f.flush()

    def close(self):
        for f, _, _ in self._writers.values():
            try:
                f.close()
            except Exception:
                pass

    def _read(self):
        return float(self.powermeter.read(self.args.averaging))

    def _is_2d(self):
        return hasattr(self.instrument, "laser") and hasattr(
            self.instrument, "laser_enabled"
        )

    def _lasers(self):
        """Laser wavelengths to cover (list), for laser-only experiments."""
        proto = getattr(self.pc, "protocol", None) or {}
        if self.args.full_protocol and proto.get("laser_sequence"):
            return list(proto["laser_sequence"])
        if self.args.lasers:
            return self.args.lasers
        return [self.args.laser] if self.args.laser is not None else [None]

    def _operating_points(self):
        """(laser, laser_power) points to run laser+power experiments at.

        ``--full-protocol`` uses the config protocol's full grid; otherwise
        ``--lasers`` x ``--laser-powers`` (or the single ``--laser`` /
        ``--laser-power``, or one ``(None, None)`` point for a 1-D / no-laser
        setup)."""
        proto = getattr(self.pc, "protocol", None) or {}
        if self.args.full_protocol and proto.get("laser_sequence"):
            powers = proto.get("laser_powers", {})
            pts = [
                (laser, pwr)
                for laser in proto["laser_sequence"]
                for pwr in powers.get(laser, [None])
            ]
            if pts:
                return pts
        lasers = self.args.lasers or (
            [self.args.laser] if self.args.laser is not None else [None]
        )
        powers = self.args.laser_powers or (
            [self.args.laser_power]
            if self.args.laser_power is not None
            else [None]
        )
        return [(la, pw) for la in lasers for pw in powers]

    def _select(self, laser, power, on=True):
        """Select laser+power, route the beam to the meter, set on/off (2D)."""
        inst = self.instrument
        if not self._is_2d() or laser is None:
            return
        inst.laser = laser
        if power is not None and hasattr(inst, "laserpower"):
            try:
                inst.laserpower = power
            except Exception:
                pass
        proto = getattr(self.pc, "protocol", None) or {}
        bp = proto.get("beampath") or {}
        if getattr(inst, "use_beampath", False):
            try:
                if laser in bp:
                    inst.beampath.positions = bp[laser]
                if (
                    normalize_powermeter_type(self.args.powermeter_type)
                    == POWERMETER_BFP
                ):
                    scp = bp.get("start_calibrate")
                    if scp:
                        inst.beampath.positions = scp
            except Exception:
                pass
        try:
            inst.attenuator.set_wavelength(laser)
            self.powermeter.wavelength = int(laser)
        except Exception:
            pass
        inst.laser_enabled = on
        time.sleep(self.args.switch_time)

    def _all_lasers_off(self):
        inst = self.instrument
        if not self._is_2d():
            return
        for las in getattr(inst, "lasers", {}):
            try:
                inst.lasers[las].enabled = False
            except Exception:
                pass

    def _set_angle(self, angle, approach="direct"):
        """Move the attenuator to ``angle``; ``approach`` below/above parks
        ``--park`` away first so the final move comes from one direction."""
        att = self.instrument.attenuator
        if approach == "below":
            att.set(angle - self.args.park)
            time.sleep(self.args.settle)
        elif approach == "above":
            att.set(angle + self.args.park)
            time.sleep(self.args.settle)
        att.set(angle)
        time.sleep(self.args.settle)
        try:
            return float(att.curr_pos())
        except Exception:
            return float("nan")

    def _angle(self):
        """The working attenuator angle (``--angle`` or the range midpoint)."""
        if self.args.angle is not None:
            return self.args.angle
        params = (
            getattr(self.instrument.analyzer, "analysis_parameters", {}) or {}
        )
        lo, hi = params.get("min", 0), params.get("max", 180)
        lo = 0 if not np.isfinite(lo) else lo
        hi = 180 if not np.isfinite(hi) else hi
        return (lo + hi) / 2.0

    # ---- experiments ------------------------------------------------------

    def laser_stability(self, stop):
        """Fixed everything; read power over time -> laser/meter stability.
        Repeated for each (laser, power) operating point."""
        for laser, power in self._operating_points():
            self._select(laser, power, on=True)
            angle = self._set_angle(self._angle())
            n = max(1, int(self.args.duration / max(self.args.interval, 1e-3)))
            for i in range(n):
                stop()
                self._log(
                    "laser_stability",
                    ["laser", "laser_power", "i", "angle", "power"],
                    {
                        "laser": laser,
                        "laser_power": power,
                        "i": i,
                        "angle": angle,
                        "power": self._read(),
                    },
                )
                time.sleep(self.args.interval)

    def meter_dark(self, stop):
        """All lasers off; read meter over time -> dark/zero drift, ambient."""
        self._all_lasers_off()
        n = max(1, int(self.args.duration / max(self.args.interval, 1e-3)))
        for i in range(n):
            stop()
            self._log(
                "meter_dark", ["i", "power"], {"i": i, "power": self._read()}
            )
            time.sleep(self.args.interval)

    def repeatability(self, stop):
        """Set the same angle N times from one direction -> positioning
        repeatability (backlash excluded). Per operating point."""
        for laser, power in self._operating_points():
            self._select(laser, power, on=True)
            angle = self._angle()
            for i in range(self.args.reps):
                stop()
                readback = self._set_angle(angle, approach="below")
                self._log(
                    "repeatability",
                    [
                        "laser",
                        "laser_power",
                        "i",
                        "commanded",
                        "readback",
                        "power",
                    ],
                    {
                        "laser": laser,
                        "laser_power": power,
                        "i": i,
                        "commanded": angle,
                        "readback": readback,
                        "power": self._read(),
                    },
                )

    def hysteresis(self, stop):
        """Re-approach one angle from below and above -> backlash. Per point."""
        for laser, power in self._operating_points():
            self._select(laser, power, on=True)
            self._set_angle(self._angle())
            for i in range(self.args.reps):
                stop()
                res = self.instrument.attenuator_hysteresis_probe(
                    read_power=self._read,
                    delta=self.args.park,
                    settle=self.args.settle,
                )
                self._log(
                    "hysteresis",
                    [
                        "laser",
                        "laser_power",
                        "i",
                        "target",
                        "power_from_below",
                        "power_from_above",
                        "power_spread_frac",
                    ],
                    {
                        "laser": laser,
                        "laser_power": power,
                        "i": i,
                        "target": res.get("target"),
                        "power_from_below": res.get("power_from_below"),
                        "power_from_above": res.get("power_from_above"),
                        "power_spread_frac": res.get("power_spread_frac"),
                    },
                )

    def homing(self, stop):
        """Measure at an angle, home, return, measure -> home-reference
        dependence of the angle->power mapping. Per operating point."""
        att = self.instrument.attenuator
        if not hasattr(att, "home"):
            print("  (attenuator has no home(); skipping homing experiment)")
            return
        angle = self._angle()
        for laser, power in self._operating_points():
            self._select(laser, power, on=True)
            for i in range(self.args.reps):
                stop()
                rb_before = self._set_angle(angle, approach="below")
                p_before = self._read()
                try:
                    att.home()
                except Exception as exc:
                    print("  home() failed: %s" % exc)
                    return
                time.sleep(self.args.settle)
                rb_after = self._set_angle(angle, approach="below")
                p_after = self._read()
                rel = (
                    (p_after - p_before) / p_before * 100.0
                    if p_before
                    else float("nan")
                )
                self._log(
                    "homing",
                    [
                        "laser",
                        "laser_power",
                        "i",
                        "commanded",
                        "readback_before",
                        "readback_after",
                        "power_before",
                        "power_after",
                        "rel_change_pct",
                    ],
                    {
                        "laser": laser,
                        "laser_power": power,
                        "i": i,
                        "commanded": angle,
                        "readback_before": rb_before,
                        "readback_after": rb_after,
                        "power_before": p_before,
                        "power_after": p_after,
                        "rel_change_pct": rel,
                    },
                )

    def _cal_variants(self):
        """(model_label, classpath, extra_kwargs) x step-size combinations.

        Defaults to the config's own model/step (one variant) unless
        ``--cal-models`` / ``--cal-steps`` request a sweep over sampling
        density and/or analysis model.
        """
        from monet import analysis as _an

        ana = self.instrument.config["analysis"]
        models = self.args.cal_models or [None]
        steps = self.args.cal_steps or [ana.get("init_kwargs", {}).get("step")]
        out = []
        for name in models:
            if name is None:
                label, classpath, extra = "config", ana["classpath"], {}
            else:
                classpath, extra = _an.model_spec(name)
                label = name
            for step in steps:
                out.append((label, classpath, extra, step))
        return out

    def calibration(self, stop):
        """Repeat full calibration sweeps (as configured) and, optionally,
        variants with different step size / model; record fit params, RMS/max
        residual, within-sweep drift, point count, and predicted-vs-measured at
        reference angles. dry_run, so nothing is written to the database.

        Isolates run-to-run fit variation, model adequacy, and the effect of
        sampling density (step size) on both."""
        from monet.util import load_class

        refs = self.args.ref_angles or [self._angle()]
        ana = self.instrument.config["analysis"]
        orig_classpath = ana["classpath"]
        orig_kwargs = dict(ana.get("init_kwargs", {}))
        fields = [
            "laser",
            "laser_power",
            "model_variant",
            "step",
            "run",
            "n_points",
            "rms_pct",
            "max_pct",
            "drift_pct",
            "ref_angle",
            "predicted",
            "measured",
            "dev_pct",
            "model",
        ]
        try:
            for laser, power in self._operating_points():
                self._select(laser, power, on=True)
                for label, classpath, extra, step in self._cal_variants():
                    kw = dict(orig_kwargs)
                    kw.pop("polydegree", None)
                    kw.update(extra)
                    if step is not None:
                        kw["step"] = step
                    ana["classpath"] = classpath
                    ana["init_kwargs"] = kw
                    for run in range(self.args.reps):
                        stop()
                        try:
                            self.instrument.analyzer = load_class(
                                classpath, kw
                            )
                            self.pc.calibrate(
                                wait_time=self.args.settle,
                                dry_run=True,
                                save_plot=False,
                                drift_check=True,
                            )
                        except Exception as exc:
                            print(
                                "  calibration %s %smW %s/step=%s run %d "
                                "failed: %s"
                                % (laser, power, label, step, run, exc)
                            )
                            continue
                        q = getattr(self.pc, "last_fit_quality", None) or {}
                        curve = getattr(self.pc, "last_curve", None)
                        n_points = len(curve[0]) if curve is not None else None
                        model = self.instrument.analyzer.get_model()
                        model_str = ";".join(
                            "{}={:.5g}".format(k, float(v))
                            for k, v in model.items()
                        )
                        for ref in refs:
                            stop()
                            predicted = float(
                                self.instrument.analyzer.estimate_power(ref)
                            )
                            self._set_angle(ref, approach="below")
                            measured = self._read()
                            dev = (
                                (measured - predicted) / predicted * 100.0
                                if predicted
                                else float("nan")
                            )
                            self._log(
                                "calibration",
                                fields,
                                {
                                    "laser": laser,
                                    "laser_power": power,
                                    "model_variant": label,
                                    "step": step,
                                    "run": run,
                                    "n_points": n_points,
                                    "rms_pct": q.get("rms_pct"),
                                    "max_pct": q.get("max_pct"),
                                    "drift_pct": q.get("drift_pct"),
                                    "ref_angle": ref,
                                    "predicted": predicted,
                                    "measured": measured,
                                    "dev_pct": dev,
                                    "model": model_str,
                                },
                            )
        finally:
            # restore the config's analysis model/step
            ana["classpath"] = orig_classpath
            ana["init_kwargs"] = orig_kwargs
            try:
                self.instrument.analyzer = load_class(
                    orig_classpath, orig_kwargs
                )
            except Exception:
                pass

    def _targets_for(self, laser):
        """Target powers (mW) for ``laser``.

        ``--target-fracs`` picks them as fractions of the laser's *accessible*
        range (so they never clamp); otherwise the absolute ``--targets``.
        """
        if self.args.target_fracs:
            try:
                lo, hi = self.instrument.accessible_power_range(
                    "combined", laser
                )
                return [lo + f * (hi - lo) for f in self.args.target_fracs]
            except Exception as exc:
                print("  (no accessible range for %s: %s)" % (laser, exc))
                return []
        return self.args.targets or []

    def setpower(self, stop):
        """Set target powers open-loop from the calibration, measure actual ->
        end-to-end reproducibility. Targets come from --target-fracs (per-laser
        in-range) or absolute --targets; ``commanded`` records what the set
        actually aimed for (differs from target if the request was clamped)."""
        if not getattr(self.instrument, "is_calibrated", False):
            print(
                "  (instrument not calibrated; skipping setpower experiment)"
            )
            return
        if not self.args.targets and not self.args.target_fracs:
            print("  (no --targets/--target-fracs; skipping setpower)")
            return
        for laser in self._lasers():
            self._select(laser, None, on=True)
            targets = self._targets_for(laser)
            for rep in range(self.args.reps):
                for target in targets:
                    stop()
                    try:
                        self.instrument.power = target
                        time.sleep(self.args.settle)
                        # what the set actually aimed for (clamped prediction)
                        try:
                            commanded = float(self.instrument.power)
                        except Exception:
                            commanded = float("nan")
                        measured = self._read()
                        att_pos = float(self.instrument.attenuator.curr_pos())
                    except Exception as exc:
                        print(
                            "  setpower %s @ %s failed: %s"
                            % (target, laser, exc)
                        )
                        continue
                    dev = (
                        (measured - target) / target * 100.0
                        if target
                        else float("nan")
                    )
                    self._log(
                        "setpower",
                        [
                            "laser",
                            "rep",
                            "target",
                            "commanded",
                            "measured",
                            "dev_pct",
                            "att_pos",
                        ],
                        {
                            "laser": laser,
                            "rep": rep,
                            "target": round(target, 4),
                            "commanded": commanded,
                            "measured": measured,
                            "dev_pct": dev,
                            "att_pos": att_pos,
                        },
                    )

    def setpower_breakdown(self, stop):
        """Decompose the open-loop set-power path to localize the deviation.

        For each (laser, target): set the power, then record the chosen
        laser-power level and actual laser power, the achieved attenuator
        angle, the model's predicted power for that state
        (``instrument.power`` getter, sample plane), the raw meter reading, the
        transmission factor, and the sample-plane reading. The derived errors
        split the end-to-end deviation into:

          inverse_err_pct  commanded(model) vs target -> the inverse step chose
                           an angle whose model power isn't the target
          model_err_pct    sample-measured vs commanded -> model vs reality at
                           the achieved angle (fit residual / drift)
          dev_raw vs dev_sample -> a raw-vs-sample-plane gap is the
                           transmission factor (dev_raw - dev_sample)
        """
        if not getattr(self.instrument, "is_calibrated", False):
            print("  (not calibrated; skipping setpower_breakdown)")
            return
        if not self.args.targets and not self.args.target_fracs:
            print("  (no --targets/--target-fracs; skipping breakdown)")
            return

        def pct(a, b):
            return (a - b) / b * 100.0 if b else float("nan")

        fields = [
            "laser",
            "rep",
            "target",
            "commanded",
            "achieved_angle",
            "laser_power_level",
            "laser_power_actual",
            "raw",
            "factor",
            "sample",
            "dev_raw_pct",
            "dev_sample_pct",
            "inverse_err_pct",
            "model_err_pct",
        ]
        for laser in self._lasers():
            self._select(laser, None, on=True)
            for rep in range(self.args.reps):
                for target in self._targets_for(laser):
                    stop()
                    try:
                        self.instrument.power = target
                        time.sleep(self.args.settle)
                        commanded = float(self.instrument.power)
                        angle = float(self.instrument.attenuator.curr_pos())
                        lp_level = getattr(
                            self.instrument, "curr_laserpower", None
                        )
                        try:
                            lp_actual = float(
                                self.instrument.lasers[laser].power
                            )
                        except Exception:
                            lp_actual = float("nan")
                        raw = self._read()
                        try:
                            factor = float(
                                self.instrument._measurement_factor(laser)
                            )
                        except Exception:
                            factor = float("nan")
                        try:
                            sample = float(
                                self.instrument.to_sample_plane(raw, laser)
                            )
                        except Exception:
                            sample = raw
                    except Exception as exc:
                        print(
                            "  breakdown %s @ %s failed: %s"
                            % (target, laser, exc)
                        )
                        continue
                    self._log(
                        "setpower_breakdown",
                        fields,
                        {
                            "laser": laser,
                            "rep": rep,
                            "target": round(target, 4),
                            "commanded": commanded,
                            "achieved_angle": angle,
                            "laser_power_level": lp_level,
                            "laser_power_actual": lp_actual,
                            "raw": raw,
                            "factor": factor,
                            "sample": sample,
                            "dev_raw_pct": pct(raw, target),
                            "dev_sample_pct": pct(sample, target),
                            "inverse_err_pct": pct(commanded, target),
                            "model_err_pct": pct(sample, commanded),
                        },
                    )

    def model_sweep(self, stop):
        """Find the best analysis model + step size by cross-validation.

        For each operating point and each step size: acquire ONE calibration
        sweep, fit every candidate model to it, then measure a set of FRESH
        angles that are *not* on the calibration grid and score each model on
        those. The fresh-angle ``test_rms_pct`` is the unbiased
        generalization error (fit RMS is biased — a spline overfits it to ~0),
        so the optimum = lowest test_rms at an acceptable calibration cost
        (logged as n_points / acquire_time_s / fit_time_s).
        """
        import time as _t

        from monet.util import load_class

        models = self.args.sweep_models or [
            "sinus",
            "poly deg 3",
            "poly deg 5",
            "spline",
        ]
        ana = self.instrument.config["analysis"]
        orig_cp = ana["classpath"]
        orig_kw = dict(ana.get("init_kwargs", {}))
        lo = orig_kw.get("min", 0)
        hi = orig_kw.get("max", 180)
        if not np.isfinite(lo):
            lo = 0
        if not np.isfinite(hi):
            hi = 180
        steps = self.args.sweep_steps or [orig_kw.get("step") or 5]
        n_test = self.args.n_test
        # fresh test angles, offset so they never coincide with a sweep grid
        test_angles = (np.linspace(lo, hi, n_test + 2)[1:-1]).astype(float)
        test_angles = [float(a) + 0.37 for a in test_angles]

        def errors(y, pred):
            """Return (rel_rms%, rel_max%, fullscale_rms%).

            rel_* are per-point relative errors (inflated by low-power trough
            points); fullscale is RMS error / max|power| — the trough-robust
            metric to rank models on.
            """
            y = np.asarray(y, float)
            pred = np.asarray(pred, float)
            ok = np.isfinite(y) & np.isfinite(pred)
            if not ok.any():
                return float("nan"), float("nan"), float("nan")
            resid = y[ok] - pred[ok]
            scale = np.max(np.abs(y[ok])) or 1.0
            fs = float(np.sqrt(np.mean(resid**2)) / scale * 100.0)
            okr = ok & (y > 0)
            if okr.any():
                r = np.abs(y[okr] - pred[okr]) / y[okr]
                return (
                    float(np.sqrt(np.mean(r**2)) * 100.0),
                    float(np.max(r) * 100.0),
                    fs,
                )
            return float("nan"), float("nan"), fs

        fields = [
            "laser",
            "laser_power",
            "step",
            "model",
            "n_points",
            "acquire_time_s",
            "fit_time_s",
            "fit_rms_pct",
            "test_rms_pct",
            "test_max_pct",
            "test_fullscale_pct",
        ]
        try:
            for laser, power in self._operating_points():
                self._select(laser, power, on=True)
                for step in steps:
                    st = step or 5
                    xs = np.arange(lo, hi + st, st)
                    # one calibration sweep
                    t0 = _t.time()
                    ys = []
                    for a in xs:
                        stop()
                        self.instrument.attenuator.set(float(a))
                        _t.sleep(self.args.settle)
                        ys.append(self._read())
                    acquire_time = _t.time() - t0
                    xs = np.asarray(xs, float)
                    ys = np.asarray(ys, float)
                    # one fresh test set (shared by all models -> fair)
                    tmeas = []
                    for a in test_angles:
                        stop()
                        self.instrument.attenuator.set(a)
                        _t.sleep(self.args.settle)
                        tmeas.append(self._read())
                    tmeas = np.asarray(tmeas, float)
                    for name in models:
                        stop()
                        cp, extra = analysis.model_spec(name)
                        kw = dict(orig_kw)
                        kw.pop("polydegree", None)
                        kw.update(extra)
                        if step is not None:
                            kw["step"] = step
                        if name == "spline" and (
                            self.args.spline_smoothing is not None
                        ):
                            kw["smoothing"] = self.args.spline_smoothing
                        try:
                            anlz = load_class(cp, kw)
                            tf = _t.time()
                            anlz.fit(xs, ys)
                            fit_time = _t.time() - tf
                        except Exception as exc:
                            print(
                                "  model_sweep fit %s step=%s failed: %s"
                                % (name, step, exc)
                            )
                            continue
                        fit_rms, _, _ = errors(ys, anlz.estimate_power(xs))
                        test_rms, test_max, test_fs = errors(
                            tmeas, anlz.estimate_power(np.asarray(test_angles))
                        )
                        self._log(
                            "model_sweep",
                            fields,
                            {
                                "laser": laser,
                                "laser_power": power,
                                "step": step,
                                "model": name,
                                "n_points": len(xs),
                                "acquire_time_s": round(acquire_time, 3),
                                "fit_time_s": round(fit_time, 4),
                                "fit_rms_pct": fit_rms,
                                "test_rms_pct": test_rms,
                                "test_max_pct": test_max,
                                "test_fullscale_pct": test_fs,
                            },
                        )
        finally:
            ana["classpath"] = orig_cp
            ana["init_kwargs"] = orig_kw
            try:
                self.instrument.analyzer = load_class(orig_cp, orig_kw)
            except Exception:
                pass

    def drift_curve(self, stop):
        """Re-acquire each laser's full attenuation curve, timestamped, so a
        later analysis can tell whether the drift between calibration and use
        is a pure *amplitude* rescale (a one-point measurement would correct
        the stored calibration) or a *shape/phase* change (it would not).

        Designed to run on a loop (``--cycles`` / ``--cycle-interval``) for
        hours or a weekend: each cycle writes one full curve per laser.
        """
        ana = self.instrument.config["analysis"]
        kw = ana.get("init_kwargs", {}) or {}
        lo = kw.get("min", 0)
        hi = kw.get("max", 180)
        if not np.isfinite(lo):
            lo = 0
        if not np.isfinite(hi):
            hi = 180
        step = self.args.drift_step or kw.get("step") or 5
        angles = np.arange(lo, hi + step, step)
        for laser, power in self._operating_points():
            self._select(laser, power, on=True)
            for a in angles:
                stop()
                self.instrument.attenuator.set(float(a))
                time.sleep(self.args.settle)
                self._log(
                    "drift_curve",
                    ["laser", "laser_power", "angle", "power"],
                    {
                        "laser": laser,
                        "laser_power": power,
                        "angle": float(a),
                        "power": self._read(),
                    },
                )

    def _warmup_levels(self, laser):
        """Laser-power setpoints to step through in ``power_warmup``.

        ``--warmup-powers`` (absolute setpoints) if given; else the config
        protocol's ``laser_powers`` for this laser; else a single current
        level (so the experiment degrades to a pure enable-settle watch)."""
        if self.args.warmup_powers:
            return list(self.args.warmup_powers)
        proto = getattr(self.pc, "protocol", None) or {}
        levels = (proto.get("laser_powers") or {}).get(laser)
        if levels:
            return list(levels)
        cur = getattr(self.instrument, "curr_laserpower", None)
        return [cur] if cur is not None else [None]

    def power_warmup(self, stop):
        """At a fixed angle, step the laser *output-power* setpoint and watch
        the meter settle after each change -> the thermal transient of
        (a) enabling emission (the first level, read right after the laser is
        enabled) and (b) changing the laser power setpoint (each later level).

        Logs ``t_since_change`` (seconds since the setpoint was applied) and the
        reading, per (laser, level), so the settle time and overshoot are
        visible. Watches each level for ``--warmup-watch`` s at
        ``--warmup-interval`` s."""
        angle = self._angle()
        fields = [
            "laser",
            "level_index",
            "laser_power_level",
            "t_since_change",
            "power",
        ]
        for laser, _power in self._operating_points():
            self._select(laser, None, on=True)
            self._set_angle(angle)
            for li, level in enumerate(self._warmup_levels(laser)):
                stop()
                if level is not None and hasattr(
                    self.instrument, "laserpower"
                ):
                    try:
                        self.instrument.laserpower = level
                    except Exception as exc:
                        print(
                            "  warmup %s level %s failed: %s"
                            % (laser, level, exc)
                        )
                        continue
                # the enable transient (first level) is watched for longer
                # (--enable-watch) than the later power-change transients
                watch = self.args.warmup_watch
                if li == 0 and self.args.enable_watch:
                    watch = self.args.enable_watch
                t_change = time.time()
                watched = 0.0
                while watched < watch:
                    stop()
                    self._log(
                        "power_warmup",
                        fields,
                        {
                            "laser": laser,
                            "level_index": li,
                            "laser_power_level": level,
                            "t_since_change": round(time.time() - t_change, 3),
                            "power": self._read(),
                        },
                    )
                    time.sleep(self.args.warmup_interval)
                    watched = time.time() - t_change

    # ---- driver -----------------------------------------------------------

    def run(self, experiments, stop):
        for cycle in range(self.args.cycles):
            self.cycle = cycle
            for name in experiments:
                stop()
                print("[cycle %d/%d] %s" % (cycle + 1, self.args.cycles, name))
                getattr(self, name)(stop)
            if cycle + 1 < self.args.cycles and self.args.cycle_interval > 0:
                print(
                    "  waiting %ds before next cycle"
                    % self.args.cycle_interval
                )
                waited = 0.0
                while waited < self.args.cycle_interval:
                    stop()
                    time.sleep(min(1.0, self.args.cycle_interval - waited))
                    waited += 1.0

    def _loop_for(self, fn, stop, duration_s, interval):
        """Call ``fn`` repeatedly (bumping ``cycle``) for ~``duration_s``,
        waiting ``interval`` s between calls (interruptibly)."""
        start = time.time()
        while time.time() - start < duration_s:
            stop()
            fn(stop)
            self.cycle += 1
            waited = 0.0
            while waited < interval and (time.time() - start) < duration_s:
                stop()
                time.sleep(min(1.0, interval - waited))
                waited += 1.0

    def run_weekend_plan(self, stop):
        """Staggered weekend protocol. Precondition: all lasers manually
        powered to **standby** (power + interlock on) but **not enabled** to
        lase.

          Phase 1  for each ``--lasers`` line in turn: software-enable it, run
                   ``power_warmup`` (the enable transient + each power-setpoint
                   change) then ``drift_curve`` on a loop for ``--single-hours``.
                   Because every line was already powered at standby, a later
                   line has thermalised its power-on heat longer before being
                   enabled -- so comparing the enable transients across lines
                   separates *power-on/standby* warm-up from *emission* warm-up.
          Phase 2  enable all lines together and loop ``drift_curve`` until the
                   ``--max-hours`` total budget (or ``--soak-hours``) is spent,
                   then shut down -- the multi-line aging soak under realistic
                   all-lasing thermal load.

        Lines named in ``--disable-after-single`` are disabled after their own
        single-line phase (cooling at standby) and re-enabled for the soak;
        lines not named stay continuously enabled. Comparing a disabled line to
        a kept-enabled one shows whether disabling loses the thermalisation (a
        re-enable transient at soak start). ``manifest.csv`` records each phase's
        wall-clock span, the lasing set, whether the single line was disabled
        afterwards, and which lines were re-enabled for the soak -- so the
        analyzers can place every curve on the global timeline (per-phase
        since-enable clock = elapsed_s minus the phase's elapsed_start_s)."""
        import csv as _csv

        lasers = self.args.lasers or (
            [self.args.laser] if self.args.laser is not None else []
        )
        if not lasers:
            print("  weekend plan needs --lasers; aborting")
            return
        disable_set = set(self.args.disable_after_single or [])
        saved = self.args.lasers
        enabled = set()
        manifest = []

        def _begin():
            return (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                round(time.time() - self.t0, 3),
            )

        def _record(phase, lasing, start, disabled_after="", reenabled=""):
            iso0, el0 = start
            manifest.append(
                {
                    "phase": phase,
                    "lasing": "+".join(str(x) for x in lasing),
                    "disabled_after": disabled_after,
                    "reenabled": reenabled,
                    "iso_start": iso0,
                    "elapsed_start_s": el0,
                    "iso_end": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "elapsed_end_s": round(time.time() - self.t0, 3),
                }
            )

        single_s = self.args.single_hours * 3600.0
        try:
            # ---- Phase 1: staggered single-line warm-up + aging ----
            for laser in lasers:
                self.phase = "single_%s" % laser
                self.args.lasers = [laser]
                start = _begin()
                print(
                    "[plan] phase %s: enable + warmup, then %.2f h single-line"
                    % (self.phase, self.args.single_hours)
                )
                t_phase = time.time()
                self.power_warmup(stop)  # enables the line
                enabled.add(laser)
                remaining = single_s - (time.time() - t_phase)
                if remaining > 0:
                    self._loop_for(
                        self.drift_curve,
                        stop,
                        remaining,
                        self.args.cycle_interval,
                    )
                drop = laser in disable_set
                if drop:
                    self._select(laser, None, on=False)
                    enabled.discard(laser)
                _record(self.phase, [laser], start, disabled_after=str(drop))
            # ---- Phase 2: multi-line soak ----
            self.phase = "multi"
            self.args.lasers = list(lasers)
            reenabled = [la for la in lasers if la not in enabled]
            print(
                "[plan] phase multi: enable all lines; aging soak "
                "(re-enabling %s)" % (reenabled or "none")
            )
            for laser in lasers:  # ensure all are lasing together
                self._select(laser, None, on=True)
                enabled.add(laser)
            # if any line was disabled, watch every line settle on re-enable:
            # the re-enabled lines show a transient, the continuously-enabled
            # ones stay flat -- the direct disabled-vs-kept comparison
            if reenabled:
                self.phase = "multi_enable"
                mstart = _begin()
                print("[plan] phase multi_enable: re-enable transient watch")
                self.power_warmup(stop)
                _record(
                    "multi_enable",
                    list(lasers),
                    mstart,
                    reenabled="+".join(str(x) for x in reenabled),
                )
                self.phase = "multi"
            start = _begin()  # the soak proper starts here (after re-enable)
            # respect the total --max-hours budget (auto shut-down) and/or
            # --soak-hours, whichever is smaller
            soak_s = (
                self.args.soak_hours * 3600.0
                if self.args.soak_hours
                else float("inf")
            )
            if self.args.max_hours:
                left = self.args.max_hours * 3600.0 - (time.time() - self.t0)
                soak_s = min(soak_s, max(0.0, left))
            if soak_s == float("inf"):
                print("  (no --max-hours/--soak-hours: soaking until Ctrl-C)")
            self._loop_for(
                self.drift_curve, stop, soak_s, self.args.cycle_interval
            )
            _record(
                "multi",
                list(lasers),
                start,
                reenabled="+".join(str(x) for x in reenabled),
            )
        finally:
            self.args.lasers = saved
            try:
                mpath = os.path.join(self.outdir, "manifest.csv")
                cols = [
                    "phase",
                    "lasing",
                    "disabled_after",
                    "reenabled",
                    "iso_start",
                    "elapsed_start_s",
                    "iso_end",
                    "elapsed_end_s",
                ]
                with open(mpath, "w", newline="") as f:
                    w = _csv.DictWriter(f, fieldnames=cols)
                    w.writeheader()
                    for row in manifest:
                        w.writerow(row)
            except Exception as exc:
                print("  manifest write failed:", exc)


def _build_pc(args):
    if args.test:
        import copy
        import tempfile

        config = copy.deepcopy(_TEST_CONFIG)
        tmp = tempfile.mkdtemp()
        config["database"] = os.path.join(tmp, "repro_test.xlsx")
        config["dest_calibration_plot"] = tmp
        protocol = copy.deepcopy(_TEST_PROTOCOL)
    else:
        name = args.microscope
        if name not in monet.CONFIGS:
            raise SystemExit(
                "Unknown microscope %r. Available: %s"
                % (name, sorted(monet.CONFIGS))
            )
        config = monet.CONFIGS[name]
        protocol = monet.PROTOCOLS.get(name)
    if protocol:
        pc = mca.CalibrationProtocol2D(config, protocol)
    else:
        pc = mca.CalibrationProtocol1D(config)
    if not getattr(pc, "powermeter_available", True):
        raise SystemExit(
            "Power meter not available: %s"
            % getattr(pc, "powermeter_error", "unknown")
        )
    # If the instrument is calibrated, the setpower/calibration refs are usable.
    try:
        if hasattr(pc.instrument, "load_calibration_database"):
            pc.instrument.load_calibration_database()
    except Exception:
        pass
    return pc


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("microscope", nargs="?", help="microscope config name")
    p.add_argument(
        "--test", action="store_true", help="use simulated Test* hw"
    )
    p.add_argument(
        "--experiments",
        default=",".join(ALL_EXPERIMENTS),
        help="comma-separated subset of: " + ",".join(ALL_EXPERIMENTS),
    )
    p.add_argument("--laser", type=int, default=None)
    p.add_argument("--laser-power", type=float, default=None)
    p.add_argument(
        "--lasers",
        type=lambda s: [int(x) for x in s.split(",")],
        default=None,
        help="comma-separated laser wavelengths to cover (overrides --laser)",
    )
    p.add_argument(
        "--laser-powers",
        type=lambda s: [float(x) for x in s.split(",")],
        default=None,
        help="comma-separated laser powers to cover (overrides --laser-power)",
    )
    p.add_argument(
        "--full-protocol",
        action="store_true",
        help="run laser-dependent experiments across the config protocol's "
        "full (laser, power) grid (all lines and powers)",
    )
    p.add_argument(
        "--powermeter-type",
        default="bfp",
        choices=["bfp", "sample"],
        help="beam routing for measurements (BFP uses the start_calibrate "
        "turret position)",
    )
    p.add_argument(
        "--switch-time",
        type=float,
        default=2.0,
        help="seconds to settle after switching laser / beam path",
    )
    p.add_argument(
        "--angle",
        type=float,
        default=None,
        help="working attenuator angle (default: analysis-range midpoint)",
    )
    p.add_argument(
        "--ref-angles",
        type=lambda s: [float(x) for x in s.split(",")],
        default=None,
        help="comma-separated reference angles for the calibration experiment",
    )
    p.add_argument(
        "--targets",
        type=lambda s: [float(x) for x in s.split(",")],
        default=None,
        help="comma-separated absolute target powers (mW) for setpower",
    )
    p.add_argument(
        "--target-fracs",
        type=lambda s: [float(x) for x in s.split(",")],
        default=None,
        help="setpower targets as fractions of each laser's accessible range "
        "(e.g. 0.25,0.5,0.9) — avoids out-of-range clamping",
    )
    p.add_argument(
        "--cal-steps",
        type=lambda s: [float(x) for x in s.split(",")],
        default=None,
        help="comma-separated attenuator step sizes for the calibration "
        "experiment (default: the config's step)",
    )
    p.add_argument(
        "--cal-models",
        type=lambda s: [x.strip() for x in s.split(",")],
        default=None,
        help="comma-separated models for the calibration experiment, e.g. "
        "'sinus,poly deg 5' (default: the config's model)",
    )
    p.add_argument(
        "--sweep-models",
        type=lambda s: [x.strip() for x in s.split(",")],
        default=None,
        help="models for the model_sweep experiment (default: "
        "sinus,poly deg 3,poly deg 5,spline)",
    )
    p.add_argument(
        "--sweep-steps",
        type=lambda s: [float(x) for x in s.split(",")],
        default=None,
        help="attenuator step sizes for model_sweep (default: config step)",
    )
    p.add_argument(
        "--n-test",
        type=int,
        default=12,
        help="number of fresh off-grid test angles for model_sweep",
    )
    p.add_argument(
        "--spline-smoothing",
        type=float,
        default=None,
        help="override the spline smoothing factor s in model_sweep "
        "(default: auto noise-aware)",
    )
    p.add_argument(
        "--drift-step",
        type=float,
        default=None,
        help="attenuator step for the drift_curve experiment "
        "(default: config step)",
    )
    p.add_argument(
        "--warmup-powers",
        type=lambda s: [float(x) for x in s.split(",")],
        default=None,
        help="laser-power setpoints to step through in power_warmup "
        "(default: the config protocol's laser_powers for each line)",
    )
    p.add_argument(
        "--warmup-watch",
        type=float,
        default=120.0,
        help="seconds to watch the meter settle after each power change "
        "[power_warmup]",
    )
    p.add_argument(
        "--enable-watch",
        type=float,
        default=None,
        help="seconds to watch the first level (the enable/warm-up transient) "
        "-- longer than --warmup-watch to catch the slow tail "
        "(default: same as --warmup-watch) [power_warmup]",
    )
    p.add_argument(
        "--warmup-interval",
        type=float,
        default=2.0,
        help="seconds between reads while watching settle [power_warmup]",
    )
    p.add_argument(
        "--plan",
        choices=["weekend"],
        default=None,
        help="run a staggered multi-phase protocol instead of uniform cycles. "
        "'weekend': per-line enable+warmup+aging, then an all-lines soak "
        "(requires all lasers manually powered to standby first)",
    )
    p.add_argument(
        "--single-hours",
        type=float,
        default=2.0,
        help="hours of single-line warmup+aging per line [--plan weekend]",
    )
    p.add_argument(
        "--soak-hours",
        type=float,
        default=None,
        help="hours of the all-lines soak (default: fill up to --max-hours, "
        "else until Ctrl-C) [--plan weekend]",
    )
    p.add_argument(
        "--max-hours",
        type=float,
        default=None,
        help="hard total wall-clock budget: the soak auto-ends and the rig "
        "shuts down so the whole run stays within this [--plan weekend]",
    )
    p.add_argument(
        "--disable-after-single",
        type=lambda s: [int(x) for x in s.split(",") if x.strip()],
        default=None,
        help="comma-separated laser lines to disable after their single-line "
        "phase (cooling at standby, re-enabled for the soak); lines not listed "
        "stay continuously enabled -- so a listed vs unlisted line shows "
        "whether disabling loses the warm-up [--plan weekend]",
    )
    p.add_argument(
        "--duration", type=float, default=60.0, help="stability [s]"
    )
    p.add_argument("--interval", type=float, default=5.0, help="stability [s]")
    p.add_argument("--reps", type=int, default=20)
    p.add_argument("--settle", type=float, default=0.5)
    p.add_argument("--park", type=float, default=5.0, help="approach offset")
    p.add_argument("--averaging", type=int, default=10)
    p.add_argument("--cycles", type=int, default=1, help="battery repeats")
    p.add_argument("--cycle-interval", type=float, default=0.0, help="[s]")
    p.add_argument(
        "--outdir",
        default=None,
        help="output dir (default: docs/diagnostics/results/run_<timestamp> "
        "inside the repo, so results can be committed back for analysis)",
    )
    p.add_argument(
        "--plot",
        action="store_true",
        help="render PNGs from the CSVs when finished (plot_results.py)",
    )
    args = p.parse_args(argv)

    if args.outdir is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.outdir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "results",
            "run_" + stamp,
        )

    if not args.test and not args.microscope:
        p.error("give a microscope config name or --test")

    experiments = [e.strip() for e in args.experiments.split(",") if e.strip()]
    bad = [e for e in experiments if e not in ALL_EXPERIMENTS]
    if bad:
        p.error("unknown experiment(s): %s" % ", ".join(bad))

    stop_flag = {"stop": False}

    def _on_sigint(signum, frame):
        stop_flag["stop"] = True

    signal.signal(signal.SIGINT, _on_sigint)

    def stop():
        if stop_flag["stop"]:
            raise _Stop()

    pc = _build_pc(args)
    probe = ReproducibilityProbe(pc, args.outdir, args)
    print("Writing CSVs to %s/" % os.path.abspath(args.outdir))
    try:
        if args.plan == "weekend":
            probe.run_weekend_plan(stop)
        else:
            probe.run(experiments, stop)
        print("Done.")
    except _Stop:
        print("\nInterrupted; wrote partial results.")
    finally:
        try:
            probe._all_lasers_off()
        except Exception:
            pass
        probe.close()
        try:
            pc.disconnect()
        except Exception:
            pass

    if args.plot:
        try:
            import plot_results

            plot_results.plot_dir(args.outdir)
        except Exception as exc:
            print("Plotting failed: %s" % exc)


if __name__ == "__main__":
    sys.exit(main())
