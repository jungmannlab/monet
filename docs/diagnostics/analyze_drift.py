#!/usr/bin/env python
"""
docs/diagnostics/analyze_drift.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Analyze a ``drift_curve`` run to answer: is the calibration drift a pure
*amplitude* change (so measuring ONE point per laser and rescaling the stored
calibration would fix it), or a *shape/phase* change (it would not)?

For each laser it takes the first cycle's curve as the reference and, for every
later cycle, compares the fresh curve against the reference under four
corrections, reporting the residual as % of full scale:

  none      no correction (the raw drift you'd suffer with a stale calibration)
  1-point   rescale the whole reference by the ratio at the single highest-SNR
            angle (what a quick one-point recal would achieve)
  scale     best single global scale factor (least squares) — the ceiling for
            amplitude-only correction
  affine    best scale + offset (a*ref + b) — separates a drifting background
            from the amplitude

If ``1-point`` ≈ ``scale`` ≈ small (≈ the fresh-fit floor) and ≪ ``none``, the
drift is amplitude and one-point rescaling works. If ``affine`` ≪ ``scale`` the
background drifts separately; if even ``affine`` stays large, the shape/phase
changes and rescaling won't save you.

Usage::

    python docs/diagnostics/analyze_drift.py docs/diagnostics/results/run_XXXX
"""

import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# the script's own dir, so the shared diag_util helper imports regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diag_util import corrections, fs  # noqa: E402


def _corrections(ref, cur, ref_angle_idx):
    """Residuals (% full scale) of cur vs ref under the four corrections:
    none / best-scale / affine (shared with analyze_aging via diag_util) plus
    the single-point rescale anchored at ``ref_angle_idx``."""
    scale = float(np.max(np.abs(ref))) or 1.0
    out = corrections(ref, cur, scale)  # none / scale / affine
    r1 = (
        cur[ref_angle_idx] / ref[ref_angle_idx]
        if ref[ref_angle_idx]
        else np.nan
    )
    out["1-point"] = fs(cur - r1 * ref, scale)
    out["k_1point"] = r1
    return out


def analyze(outdir):
    path = os.path.join(outdir, "drift_curve.csv")
    if not os.path.isfile(path):
        print("no drift_curve.csv in", outdir)
        return
    df = pd.read_csv(path)
    lasers = sorted(df["laser"].unique())
    fig, axes = plt.subplots(
        1, len(lasers), figsize=(5 * len(lasers), 4), squeeze=False
    )
    print("Drift analysis (residual vs reference curve, %% full scale):\n")
    for ax, laser in zip(axes[0], lasers):
        sub = df[df["laser"] == laser]
        piv = sub.pivot_table(
            index="angle", columns="cycle", values="power", aggfunc="mean"
        ).sort_index()
        # elapsed time per cycle (minutes)
        tmin = sub.groupby("cycle")["elapsed_s"].mean() / 60.0
        cycles = [c for c in piv.columns if piv[c].notna().all()]
        if len(cycles) < 2:
            print("  %s: need >=2 complete curves" % laser)
            continue
        ref = piv[cycles[0]].to_numpy(dtype=float)
        ref_angle_idx = int(np.argmax(ref))  # highest-SNR point
        series = {"none": [], "1-point": [], "scale": [], "affine": []}
        ks, times = [], []
        for c in cycles[1:]:
            cur = piv[c].to_numpy(dtype=float)
            r = _corrections(ref, cur, ref_angle_idx)
            for key in series:
                series[key].append(r[key])
            ks.append(r["k_1point"])
            times.append(float(tmin.get(c, np.nan)))
        for key, vals in series.items():
            ax.plot(times, vals, "o-", ms=3, label=key)
        ax.set_xlabel("elapsed [min]")
        ax.set_ylabel("residual vs ref [% full scale]")
        ax.set_title("%s nm" % laser)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=7)
        amp = (max(ks) / min(ks) - 1) * 100 if ks and min(ks) else float("nan")
        print(
            "  %s nm: amplitude drift range %+.1f%% | median residual "
            "none=%.2f  1-point=%.2f  scale=%.2f  affine=%.2f"
            % (
                laser,
                amp,
                float(np.median(series["none"])),
                float(np.median(series["1-point"])),
                float(np.median(series["scale"])),
                float(np.median(series["affine"])),
            )
        )
    fig.tight_layout()
    out = os.path.join(outdir, "drift_analysis.png")
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print("\nwrote", out)
    print(
        "\nReading it: if 1-point ~ scale ~ small and << none, one-point "
        "rescaling fixes the drift (it's amplitude). If affine << scale, the "
        "background drifts too; if affine stays large, the shape/phase "
        "changes and rescaling won't help."
    )


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: analyze_drift.py <results_dir>")
        return 2
    analyze(argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
