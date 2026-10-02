#!/usr/bin/env python
"""
docs/diagnostics/analyze_warmup.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Analyze a ``power_warmup`` run (ideally from ``--plan weekend``) to answer:

  * **How long after enabling / changing laser power does the output settle,
    and by how much?** Per (laser, setpoint) it plots the reading vs seconds
    since the change and reports the settle time (to within ``--tol`` %) and the
    peak excursion.
  * **Is the warm-up in *enabling emission* or in *power being on*?** In the
    staggered weekend plan every line is powered to standby first, then enabled
    one after another, so a later line sat at standby longer before emitting.
    If the *enable* transient (the first setpoint, ``level_index == 0``) shrinks
    for lines enabled later, part of the warm-up is power-on/standby
    thermalisation (it finishes while idle). If it is the same regardless of how
    long the line was powered, the transient is purely the emission/enable step.

Usage::

    python docs/diagnostics/analyze_warmup.py docs/diagnostics/results/run_XXXX \
        [--tol 1.0]

Writes ``warmup_analysis.png`` and prints a summary into the run directory.
"""

import argparse
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


def _steady(p):
    """Steady-state estimate: median of the last fifth of the samples."""
    p = np.asarray(p, float)
    tail = p[max(1, len(p) * 4 // 5) :]
    return float(np.median(tail)) if len(tail) else float("nan")


def _settle_time(t, p, steady, tol):
    """First time after which the reading stays within ``tol`` % of steady."""
    p = np.asarray(p, float)
    t = np.asarray(t, float)
    if not steady:
        return float("nan")
    within = np.abs(p - steady) / abs(steady) <= tol / 100.0
    # last index that is OUTSIDE tolerance -> settle just after it
    bad = np.where(~within)[0]
    if len(bad) == 0:
        return float(t[0])
    k = bad[-1]
    return float(t[k + 1]) if k + 1 < len(t) else float("nan")


def _reenabled_lines(outdir):
    """Lines re-enabled for the soak, read from the plan's manifest.csv."""
    mpath = os.path.join(outdir, "manifest.csv")
    if not os.path.isfile(mpath):
        return set()
    try:
        m = pd.read_csv(mpath)
        vals = m.get("reenabled")
        out = set()
        if vals is not None:
            for v in vals.dropna():
                for x in str(v).split("+"):
                    if x.strip():
                        out.add(int(float(x)))
        return out
    except Exception:
        return set()


def analyze(outdir, tol):
    path = os.path.join(outdir, "power_warmup.csv")
    if not os.path.isfile(path):
        print("no power_warmup.csv in", outdir)
        return 1
    df = pd.read_csv(path)
    if "phase" not in df.columns:
        df["phase"] = ""
    df["phase"] = df["phase"].fillna("")
    reenabled = _reenabled_lines(outdir)
    lasers = sorted(df["laser"].dropna().unique())
    fig, axes = plt.subplots(
        1, len(lasers), figsize=(5.2 * len(lasers), 4), squeeze=False
    )
    # order lasers by when they were first enabled (standby age proxy)
    first_seen = df.groupby("laser")["elapsed_s"].min().to_dict()
    enable_rows, reenable_rows = [], []
    print("Warm-up analysis (tol=%.1f%%):\n" % tol)
    for ax, laser in zip(axes[0], lasers):
        sub = df[df["laser"] == laser]
        for (phase, li), g in sub.groupby(["phase", "level_index"]):
            g = g.sort_values("t_since_change")
            t = g["t_since_change"].to_numpy(float)
            p = g["power"].to_numpy(float)
            steady = _steady(p)
            if not steady or not np.isfinite(steady):
                continue
            lvl = g["laser_power_level"].iloc[0]
            ax.plot(
                t,
                p / steady * 100.0,
                "o-",
                ms=2,
                label="%s l%s (%s)" % (phase or "run", li, lvl),
            )
            st = _settle_time(t, p, steady, tol)
            excursion = (np.nanmax(p) - np.nanmin(p)) / steady * 100.0
            drift0 = (
                (steady - p[0]) / steady * 100.0 if len(p) else float("nan")
            )
            if li == 0 and str(phase).startswith("single"):
                enable_rows.append(
                    (laser, first_seen.get(laser, np.nan), drift0, st)
                )
            if li == 0 and phase == "multi_enable":
                reenable_rows.append((laser, drift0, st))
            print(
                "  %s nm [%s] lvl %s (%s): settle=%.0fs  excursion=%.2f%%  "
                "start->steady=%+.2f%%"
                % (laser, phase or "run", li, lvl, st, excursion, drift0)
            )
        ax.axhline(100, color="k", lw=0.8)
        ax.set_xlabel("time since change [s]")
        ax.set_ylabel("power / steady [%]")
        ax.set_title("%s nm — settle after power change" % laser)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=7)

    if len(enable_rows) >= 2:
        enable_rows.sort(key=lambda r: r[1])  # by enable time (standby age)
        print(
            "\nEnable transient vs standby age (lines enabled later sat at "
            "standby longer):"
        )
        for laser, t_en, drift0, st in enable_rows:
            print(
                "  %s nm: enabled at +%.0f min | enable drift=%+.2f%% "
                "settle=%.0fs" % (laser, t_en / 60.0, drift0, st)
            )
        print(
            "  -> if enable drift shrinks down this list, part of the warm-up "
            "is power-on/standby thermalisation; if it stays constant, the "
            "transient is the emission/enable step itself."
        )

    if reenable_rows:
        print(
            "\nRe-enable transient at soak start (disabled-after-single lines "
            "vs kept-enabled):"
        )
        for laser, drift0, st in sorted(reenable_rows):
            tag = "re-enabled" if laser in reenabled else "kept enabled"
            print(
                "  %s nm (%s): re-enable drift=%+.2f%% settle=%.0fs"
                % (laser, tag, drift0, st)
            )
        print(
            "  -> a large transient on the re-enabled line but ~flat on the "
            "kept one means disabling loses the warm-up (it must re-thermalise)."
        )

    fig.tight_layout()
    out = os.path.join(outdir, "warmup_analysis.png")
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print("\nwrote", out)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("outdir", help="a results directory with power_warmup.csv")
    p.add_argument(
        "--tol",
        type=float,
        default=1.0,
        help="settle tolerance, %% of steady state (default 1.0)",
    )
    a = p.parse_args(argv)
    return analyze(a.outdir, a.tol)


if __name__ == "__main__":
    sys.exit(main())
