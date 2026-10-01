#!/usr/bin/env python
"""
docs/diagnostics/plot_results.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Render PNGs from a reproducibility-probe results directory (the CSVs written by
reproducibility_probe.py), so a rig run can be committed back to the repo with
plots for analysis.

Usage::

    python docs/diagnostics/plot_results.py docs/diagnostics/results/run_XXXX

Writes one ``<experiment>.png`` per CSV into the same directory. Unknown CSVs
are skipped; missing matplotlib/pandas is reported, not fatal.
"""

import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402


def _save(fig, outdir, name):
    path = os.path.join(outdir, name + ".png")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    print("  wrote", path)


def _timeseries(df, outdir, name, ycol, title, ylabel):
    """Scatter ``ycol`` vs elapsed time, one colour per battery cycle."""
    if ycol not in df.columns:
        return
    fig, ax = plt.subplots(figsize=(8, 4))
    x = df["elapsed_s"] if "elapsed_s" in df else range(len(df))
    if "cycle" in df.columns and df["cycle"].nunique() > 1:
        for cyc, sub in df.groupby("cycle"):
            xs = sub["elapsed_s"] if "elapsed_s" in sub else range(len(sub))
            ax.plot(xs, sub[ycol], "o-", ms=3, label="cycle %s" % cyc)
        ax.legend(fontsize=7)
    else:
        ax.plot(x, df[ycol], "o-", ms=3)
    ax.set_xlabel("elapsed [s]")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    _save(fig, outdir, name)


def _plot_laser_stability(df, outdir):
    _timeseries(
        df,
        outdir,
        "laser_stability",
        "power",
        "Laser stability (fixed angle)",
        "power",
    )


def _plot_meter_dark(df, outdir):
    _timeseries(
        df,
        outdir,
        "meter_dark",
        "power",
        "Meter dark reading (laser off)",
        "power",
    )


def _plot_repeatability(df, outdir):
    _timeseries(
        df,
        outdir,
        "repeatability",
        "power",
        "Attenuator repeatability (same angle, one approach)",
        "power",
    )


def _plot_hysteresis(df, outdir):
    if "power_spread_frac" not in df.columns:
        return
    fig, ax = plt.subplots(figsize=(8, 4))
    x = df["elapsed_s"] if "elapsed_s" in df else range(len(df))
    ax.plot(x, df["power_spread_frac"] * 100.0, "o-", ms=3)
    ax.set_xlabel("elapsed [s]")
    ax.set_ylabel("below-vs-above spread [%]")
    ax.set_title("Attenuator backlash (hysteresis probe)")
    ax.grid(True, alpha=0.3)
    _save(fig, outdir, "hysteresis")


def _plot_homing(df, outdir):
    _timeseries(
        df,
        outdir,
        "homing",
        "rel_change_pct",
        "Homing impact (power change after re-home at same angle)",
        "rel. change [%]",
    )


def _plot_setpower(df, outdir):
    if "dev_pct" not in df.columns:
        return
    fig, ax = plt.subplots(figsize=(8, 4))
    group = "target" if "target" in df.columns else None
    if group is not None:
        for tgt, sub in df.groupby(group):
            xs = sub["elapsed_s"] if "elapsed_s" in sub else range(len(sub))
            ax.plot(xs, sub["dev_pct"], "o-", ms=3, label="%s mW" % tgt)
        ax.legend(fontsize=7)
    else:
        ax.plot(df["elapsed_s"], df["dev_pct"], "o-", ms=3)
    ax.axhline(0, color="k", lw=0.8)
    ax.set_xlabel("elapsed [s]")
    ax.set_ylabel("measured - target [%]")
    ax.set_title("Set-power deviation (end to end)")
    ax.grid(True, alpha=0.3)
    _save(fig, outdir, "setpower")


def _plot_calibration(df, outdir):
    if "rms_pct" not in df.columns:
        return
    # one series per (model_variant, step) combination
    gcols = [c for c in ("model_variant", "step") if c in df.columns]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    for ax, ycol, title in (
        (axes[0], "rms_pct", "Calibration fit RMS residual"),
        (axes[1], "dev_pct", "Predicted-vs-measured deviation"),
    ):
        if ycol not in df.columns:
            continue
        if gcols:
            for key, sub in df.groupby(gcols):
                label = ", ".join(
                    "{}={}".format(c, v)
                    for c, v in zip(
                        gcols, key if isinstance(key, tuple) else (key,)
                    )
                )
                ax.plot(sub["run"], sub[ycol], "o-", ms=3, label=label)
            ax.legend(fontsize=7)
        else:
            ax.plot(df["run"], df[ycol], "o-", ms=3)
        ax.set_xlabel("run")
        ax.set_ylabel(ycol)
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
    _save(fig, outdir, "calibration")


_PLOTTERS = {
    "laser_stability": _plot_laser_stability,
    "meter_dark": _plot_meter_dark,
    "repeatability": _plot_repeatability,
    "hysteresis": _plot_hysteresis,
    "homing": _plot_homing,
    "setpower": _plot_setpower,
    "calibration": _plot_calibration,
}


def plot_dir(outdir):
    """Render a PNG for each known CSV in ``outdir``."""
    print("Plotting", outdir)
    for name, fn in _PLOTTERS.items():
        path = os.path.join(outdir, name + ".csv")
        if not os.path.isfile(path):
            continue
        try:
            df = pd.read_csv(path)
            if len(df):
                fn(df, outdir)
        except Exception as exc:
            print("  %s: %s" % (name, exc))


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: plot_results.py <results_dir>")
        return 2
    plot_dir(argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
