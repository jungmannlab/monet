#!/usr/bin/env python
"""
docs/diagnostics/analyze_aging.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Turn a ``drift_curve`` run into a **calibration-aging matrix**: how well does a
calibration made at one time set power at a *later* time, as it ages?

This answers the two open questions directly:

  * **Is the 3-18% set-power deviation drift (a stale calibration) or a constant
    systematic bias?** If a *fresh* calibration (tested at its own time, the
    matrix diagonal) is already off, it is systematic and ageing won't explain
    it. If fresh is ~0 and the error grows with calibration age, it is drift.
  * **Would a one-point rescale fix it?** For each aged calibration we also
    compute the residual after a single-point rescale (anchored at the
    highest-SNR angle, measured at *use* time) and after the best global scale
    (the amplitude-only ceiling). If one-point ~ scale ~ small, rescaling works.

No extra hardware pass is needed: a periodic full-curve sweep (``drift_curve``,
ideally on a **cold** laser left on so the warm-up is captured, with a fine
``--drift-step`` so the inverse is accurate) already contains everything. The
old-calibration-vs-later-curve comparison is done here, offline: fit each
cycle's curve, invert the reference calibration to an angle for each target, and
read the *actual* power off the later measured curve by interpolation.

Usage::

    python docs/diagnostics/analyze_aging.py docs/diagnostics/results/run_XXXX \
        [--model "poly deg 5"] [--ref-cycle 0] [--target-fracs 0.25,0.5,0.9]

Writes ``aging_analysis.png`` (+ prints a summary) into the run directory.
"""

import argparse
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# Run from the repo root so ``import monet`` resolves.
sys.path.insert(0, os.getcwd())
from monet import analysis as _an  # noqa: E402
from monet.util import load_class  # noqa: E402


def _fit(angles, power, model, lo, hi, step):
    """Fit ``model`` to one measured curve; return the analyzer or None."""
    cp, extra = _an.model_spec(model)
    kw = {"min": lo, "max": hi, "step": step}
    kw.update(extra)
    try:
        anlz = load_class(cp, kw)
        anlz.fit(np.asarray(angles, float), np.asarray(power, float))
        return anlz
    except Exception as exc:
        print("  fit failed (%s): %s" % (model, exc))
        return None


def _angle_for(anlz, target, lo, hi):
    """Inverse: attenuator angle for a target power, clipped to range."""
    try:
        a = float(np.asarray(anlz.estimate(target)).ravel()[0])
    except Exception:
        return np.nan
    if not np.isfinite(a):
        return np.nan
    return float(min(max(a, lo), hi))


def _fs(resid, scale):
    resid = np.asarray(resid, float)
    ok = np.isfinite(resid)
    if not ok.any() or not scale:
        return np.nan
    return float(np.sqrt(np.mean(resid[ok] ** 2)) / scale * 100.0)


def _curve_corrections(ref_pred, meas, scale):
    """Residual (% full scale) of a reference curve vs a later measured curve
    under none / best-scale / affine corrections -- the amplitude-vs-shape test.
    """
    ref = np.asarray(ref_pred, float)
    cur = np.asarray(meas, float)
    ok = np.isfinite(ref) & np.isfinite(cur)
    ref, cur = ref[ok], cur[ok]
    out = {"none": _fs(cur - ref, scale)}
    denom = float(np.dot(ref, ref))
    k = float(np.dot(cur, ref) / denom) if denom else 1.0
    out["scale"] = _fs(cur - k * ref, scale)
    A = np.vstack([ref, np.ones_like(ref)]).T
    try:
        a, b = np.linalg.lstsq(A, cur, rcond=None)[0]
        out["affine"] = _fs(cur - (a * ref + b), scale)
    except Exception:
        out["affine"] = np.nan
    return out


def _setpower_dev(cal, meas, angles, targets, lo, hi, scale, rescale_s=1.0):
    """Open-loop set-power deviation (% full scale) using ``cal`` against the
    ``meas`` curve. ``rescale_s`` applies a one-point amplitude rescale: to hit
    T the cal is inverted at T / s and the prediction is read s*... implicitly
    via the measured curve at that angle."""
    devs = []
    for t in targets:
        ang = _angle_for(cal, t / rescale_s, lo, hi)
        if not np.isfinite(ang):
            devs.append(np.nan)
            continue
        actual = float(np.interp(ang, angles, meas))
        devs.append((actual - t) / scale * 100.0)
    return float(np.sqrt(np.nanmean(np.square(devs)))) if devs else np.nan


def analyze(outdir, model, ref_cycle, fracs):
    path = os.path.join(outdir, "drift_curve.csv")
    if not os.path.isfile(path):
        print("no drift_curve.csv in", outdir)
        return 1
    df = pd.read_csv(path)
    lasers = sorted(df["laser"].dropna().unique())
    fig, axes = plt.subplots(
        2, len(lasers), figsize=(5.2 * len(lasers), 8), squeeze=False
    )
    print(
        "Calibration-aging analysis (model=%s, ref cycle=%d):\n"
        % (model, ref_cycle)
    )
    for col, laser in enumerate(lasers):
        sub = df[df["laser"] == laser]
        piv = sub.pivot_table(
            index="angle", columns="cycle", values="power", aggfunc="mean"
        ).sort_index()
        tmin = sub.groupby("cycle")["elapsed_s"].mean() / 60.0
        angles = piv.index.to_numpy(float)
        cycles = [c for c in piv.columns if piv[c].notna().all()]
        if len(cycles) < 2 or ref_cycle not in cycles:
            print("  %s nm: need >=2 complete curves incl. ref" % laser)
            continue
        lo, hi = float(angles.min()), float(angles.max())
        step = float(np.median(np.diff(angles))) if len(angles) > 1 else 1.0
        meas = {c: piv[c].to_numpy(float) for c in cycles}
        scale = float(np.nanmax(meas[ref_cycle])) or 1.0
        # targets as fractions of the reference curve's accessible power range
        pmin = float(np.nanmin(meas[ref_cycle]))
        targets = [pmin + f * (scale - pmin) for f in fracs]

        cal = {c: _fit(angles, meas[c], model, lo, hi, step) for c in cycles}
        if cal[ref_cycle] is None:
            print("  %s nm: reference fit failed" % laser)
            continue
        a0 = float(
            angles[int(np.argmax(meas[ref_cycle]))]
        )  # anchor (high SNR)
        ref_pred0 = cal[ref_cycle].estimate_power(a0)

        ages, sp_none, sp_1pt, sp_fresh = [], [], [], []
        c_none, c_scale, c_affine = [], [], []
        test = [c for c in cycles if tmin.get(c, 0) >= tmin.get(ref_cycle, 0)]
        for j in test:
            age = float(tmin[j] - tmin[ref_cycle])
            ages.append(age)
            # stale reference cal, no correction
            sp_none.append(
                _setpower_dev(
                    cal[ref_cycle], meas[j], angles, targets, lo, hi, scale
                )
            )
            # one-point rescale: anchor ratio measured now (cycle j)
            s = float(np.interp(a0, angles, meas[j])) / ref_pred0
            s = s if (np.isfinite(s) and s) else 1.0
            sp_1pt.append(
                _setpower_dev(
                    cal[ref_cycle],
                    meas[j],
                    angles,
                    targets,
                    lo,
                    hi,
                    scale,
                    rescale_s=s,
                )
            )
            # always-fresh floor (recalibrate at use time)
            sp_fresh.append(
                _setpower_dev(cal[j], meas[j], angles, targets, lo, hi, scale)
                if cal[j] is not None
                else np.nan
            )
            cc = _curve_corrections(
                cal[ref_cycle].estimate_power(angles), meas[j], scale
            )
            c_none.append(cc["none"])
            c_scale.append(cc["scale"])
            c_affine.append(cc["affine"])

        ax = axes[0][col]
        ax.plot(ages, sp_none, "o-", ms=3, label="stale cal (none)")
        ax.plot(ages, sp_1pt, "o-", ms=3, label="+ one-point rescale")
        ax.plot(
            ages, sp_fresh, "o-", ms=3, color="gray", label="fresh (floor)"
        )
        ax.set_xlabel("calibration age [min]")
        ax.set_ylabel("set-power RMS dev [% full scale]")
        ax.set_title("%s nm — aging of set-power accuracy" % laser)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=7)

        axc = axes[1][col]
        axc.plot(ages, c_none, "o-", ms=3, label="none")
        axc.plot(ages, c_scale, "o-", ms=3, label="best scale (amplitude)")
        axc.plot(ages, c_affine, "o-", ms=3, label="affine (scale+offset)")
        axc.set_xlabel("calibration age [min]")
        axc.set_ylabel("curve residual [% full scale]")
        axc.set_title("%s nm — amplitude vs shape of the drift" % laser)
        axc.grid(True, alpha=0.3)
        axc.legend(fontsize=7)

        fresh0 = sp_fresh[0] if sp_fresh else float("nan")
        last_none = sp_none[-1] if sp_none else float("nan")
        last_1pt = sp_1pt[-1] if sp_1pt else float("nan")
        print(
            "  %s nm: fresh floor=%.2f%%FS | oldest-cal (age %.0f min): "
            "stale=%.2f%%FS  one-point=%.2f%%FS  | drift is %s"
            % (
                laser,
                fresh0,
                ages[-1] if ages else float("nan"),
                last_none,
                last_1pt,
                (
                    "amplitude (rescale helps)"
                    if (c_scale and c_none and c_scale[-1] < 0.5 * c_none[-1])
                    else "shape/constant (rescale won't fully fix)"
                ),
            )
        )

    fig.tight_layout()
    out = os.path.join(outdir, "aging_analysis.png")
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print("\nwrote", out)
    print(
        "\nReading it:\n"
        "  Top row: if 'stale' rises with age while 'fresh' stays flat and low,"
        " the deviation is DRIFT. If even 'fresh' is high, it is a systematic\n"
        "  (model/factor) bias, not aging. '+ one-point rescale' near 'fresh'\n"
        "  means a quick one-point recal recovers the accuracy.\n"
        "  Bottom row: if 'best scale' << 'none', the drift is a pure amplitude\n"
        "  change (rescale works); if 'affine' << 'scale', the background drifts\n"
        "  too; if all stay high, the shape changed and rescale won't save you."
    )
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("outdir", help="a drift_curve results directory")
    p.add_argument(
        "--model",
        default="poly deg 5",
        help="analysis model to fit each curve (default: poly deg 5)",
    )
    p.add_argument(
        "--ref-cycle",
        type=int,
        default=0,
        help="cycle whose calibration is aged (default: 0, the first/cold)",
    )
    p.add_argument(
        "--target-fracs",
        default="0.25,0.5,0.9",
        help="set-power targets as fractions of range (default 0.25,0.5,0.9)",
    )
    a = p.parse_args(argv)
    fracs = [float(x) for x in a.target_fracs.split(",") if x.strip()]
    return analyze(a.outdir, a.model, a.ref_cycle, fracs)


if __name__ == "__main__":
    sys.exit(main())
