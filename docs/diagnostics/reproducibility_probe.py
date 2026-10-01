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
import monet.calibrate as mca

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
        self._writers = {}  # name -> (file, csv.writer, fieldnames)
        os.makedirs(outdir, exist_ok=True)

    # ---- infrastructure ---------------------------------------------------

    def _log(self, name, fields, row):
        """Append one row to <outdir>/<name>.csv (header written once)."""
        base = ["iso_time", "elapsed_s", "cycle"]
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

    def _set_laser(self, on=True):
        """Select the laser / power and set its on/off state (2D only)."""
        inst = self.instrument
        if not self._is_2d() or self.args.laser is None:
            return
        inst.laser = self.args.laser
        if self.args.laser_power is not None and hasattr(inst, "laserpower"):
            try:
                inst.laserpower = self.args.laser_power
            except Exception:
                pass
        try:
            inst.attenuator.set_wavelength(self.args.laser)
            self.powermeter.wavelength = int(self.args.laser)
        except Exception:
            pass
        inst.laser_enabled = on

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
        """Fixed everything; read power over time -> laser/meter stability."""
        self._set_laser(on=True)
        angle = self._set_angle(self._angle())
        n = max(1, int(self.args.duration / max(self.args.interval, 1e-3)))
        for i in range(n):
            stop()
            self._log(
                "laser_stability",
                ["i", "angle", "laser_power", "power"],
                {
                    "i": i,
                    "angle": angle,
                    "laser_power": self.args.laser_power,
                    "power": self._read(),
                },
            )
            time.sleep(self.args.interval)

    def meter_dark(self, stop):
        """Laser off; read meter over time -> dark/zero drift, ambient."""
        self._set_laser(on=False)
        n = max(1, int(self.args.duration / max(self.args.interval, 1e-3)))
        for i in range(n):
            stop()
            self._log(
                "meter_dark", ["i", "power"], {"i": i, "power": self._read()}
            )
            time.sleep(self.args.interval)

    def repeatability(self, stop):
        """Set the same angle N times from one direction -> positioning
        repeatability (backlash excluded)."""
        self._set_laser(on=True)
        angle = self._angle()
        for i in range(self.args.reps):
            stop()
            readback = self._set_angle(angle, approach="below")
            self._log(
                "repeatability",
                ["i", "commanded", "readback", "power"],
                {
                    "i": i,
                    "commanded": angle,
                    "readback": readback,
                    "power": self._read(),
                },
            )

    def hysteresis(self, stop):
        """Re-approach one angle from below and above -> backlash."""
        self._set_laser(on=True)
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
                    "i",
                    "target",
                    "power_from_below",
                    "power_from_above",
                    "power_spread_frac",
                ],
                {
                    "i": i,
                    "target": res.get("target"),
                    "power_from_below": res.get("power_from_below"),
                    "power_from_above": res.get("power_from_above"),
                    "power_spread_frac": res.get("power_spread_frac"),
                },
            )

    def homing(self, stop):
        """Measure at an angle, home, return, measure -> home-reference
        dependence of the angle->power mapping."""
        att = self.instrument.attenuator
        if not hasattr(att, "home"):
            print("  (attenuator has no home(); skipping homing experiment)")
            return
        self._set_laser(on=True)
        angle = self._angle()
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
                    "i",
                    "commanded",
                    "readback_before",
                    "readback_after",
                    "power_before",
                    "power_after",
                    "rel_change_pct",
                ],
                {
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

        self._set_laser(on=True)
        refs = self.args.ref_angles or [self._angle()]
        ana = self.instrument.config["analysis"]
        orig_classpath = ana["classpath"]
        orig_kwargs = dict(ana.get("init_kwargs", {}))
        fields = [
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
                        self.instrument.analyzer = load_class(classpath, kw)
                        self.pc.calibrate(
                            wait_time=self.args.settle,
                            dry_run=True,
                            save_plot=False,
                            drift_check=True,
                        )
                    except Exception as exc:
                        print(
                            "  calibration %s/step=%s run %d failed: %s"
                            % (label, step, run, exc)
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

    def setpower(self, stop):
        """Set target powers open-loop from the calibration, measure actual ->
        end-to-end reproducibility."""
        if not getattr(self.instrument, "is_calibrated", False):
            print(
                "  (instrument not calibrated; skipping setpower experiment)"
            )
            return
        self._set_laser(on=True)
        targets = self.args.targets or []
        if not targets:
            print("  (no --targets given; skipping setpower experiment)")
            return
        for rep in range(self.args.reps):
            for target in targets:
                stop()
                try:
                    self.instrument.power = target
                    time.sleep(self.args.settle)
                    measured = self._read()
                    att_pos = float(self.instrument.attenuator.curr_pos())
                except Exception as exc:
                    print("  setpower %s failed: %s" % (target, exc))
                    continue
                dev = (
                    (measured - target) / target * 100.0
                    if target
                    else float("nan")
                )
                self._log(
                    "setpower",
                    ["rep", "target", "measured", "dev_pct", "att_pos"],
                    {
                        "rep": rep,
                        "target": target,
                        "measured": measured,
                        "dev_pct": dev,
                        "att_pos": att_pos,
                    },
                )

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
        help="comma-separated target powers (mW) for the setpower experiment",
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
        probe.run(experiments, stop)
        print("Done.")
    except _Stop:
        print("\nInterrupted; wrote partial results.")
    finally:
        try:
            probe._set_laser(on=False)
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
