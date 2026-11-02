#!/usr/bin/env python
"""
docs/diagnostics/suggest_levels.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Propose a set of laser output-power **levels** to calibrate so that common
set-points land near the *gentle top* of some level's attenuation curve rather
than on the steep, error-amplifying trough of a higher-power level.

Why levels matter: sample power is ``S(theta) = T(theta) * L`` with ``T`` the
attenuator transmission (sin^2) and ``L`` the laser output level. To hit ``S*``
you need ``T = S*/L``; near the trough (``T -> 0``) the *relative* error blows
up (``dT/T ~ 2 dtheta / sqrt(T)``), which is why low set-points on a high level
are inaccurate. A lower ``L`` keeps ``T`` off the trough. The hard limit is the
laser's minimum output — here ``--floor-frac`` of its maximum (default 0.10):
below roughly ``floor_frac * L_max``'s comfortable reach, no level helps and you
need closed-loop feedback.

This reads each laser's **current** calibrated levels and their sample-plane
tops from the calibration database, and proposes a geometric set from the floor
to ``L_max`` (given per laser via ``--laser-max``). It does not move hardware.

Usage::

    python docs/diagnostics/suggest_levels.py Skylab \
        --laser-max 488:50,560:1000,642:1000 \
        --n-levels 4 --floor-frac 0.10 --powermeter-type bfp
"""

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())
import monet  # noqa: E402
import monet.control as mco  # noqa: E402

# transmission at which a set-point is considered "well served" (off-trough);
# below this the relative error from the thermal phase shift grows fast.
_T_MIN = 0.3


def propose_levels(sample_tops, l_max, n_levels=4, floor_frac=0.10):
    """Pure proposal: given ``sample_tops`` {level: sample_top_mW} for the
    currently-calibrated levels and the laser maximum ``l_max``, return a dict
    with the proposed geometric level set (floor = ``floor_frac * l_max``) and
    the predicted sample top of each, by assuming the curve top scales with the
    laser output (``top ~ k * level``, ``k`` from the highest current level).

    Returns ``{proposed: [(level, pred_top_mW), ...], k, floor, lowest_served}``
    where ``lowest_served`` is the smallest set-point still off the trough on
    the floor level (``_T_MIN * pred_top(floor)``).
    """
    if not sample_tops or l_max <= 0:
        return None
    ref_level = max(sample_tops, key=lambda lv: sample_tops[lv])
    k = sample_tops[ref_level] / ref_level if ref_level else 0.0
    floor = floor_frac * l_max
    n = max(2, int(n_levels))
    ratio = (l_max / floor) ** (1.0 / (n - 1)) if floor > 0 else 1.0
    proposed = []
    for i in range(n):
        lv = floor * (ratio**i)
        lv = round(lv / 5.0) * 5.0 if lv >= 20 else round(lv)
        proposed.append((lv, k * lv))
    # dedupe after rounding, keep order
    seen, uniq = set(), []
    for lv, top in proposed:
        if lv not in seen:
            seen.add(lv)
            uniq.append((lv, top))
    lowest_served = _T_MIN * (k * floor)
    return {
        "proposed": uniq,
        "k": k,
        "floor": floor,
        "lowest_served": lowest_served,
    }


def _sample_tops(inst, laser):
    """{level: sample-plane top power} for a laser's calibrated levels."""
    analyzers, pranges = inst._analyzers_for(laser)
    sranges = inst._sample_power_ranges(laser, pranges)
    return {lv: float(sranges.loc[lv, "max"]) for lv in sranges.index}


def _parse_laser_max(s):
    out = {}
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        k, v = part.split(":")
        out[int(k)] = float(v)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("microscope", help="microscope config name")
    p.add_argument(
        "--laser-max",
        default="",
        help="per-laser maximum output power, e.g. 488:50,560:1000,642:1000; "
        "lasers omitted fall back to the highest calibrated level (a warning "
        "is printed)",
    )
    p.add_argument("--n-levels", type=int, default=4)
    p.add_argument("--floor-frac", type=float, default=0.10)
    p.add_argument(
        "--powermeter-type", choices=["bfp", "sample"], default="bfp"
    )
    a = p.parse_args(argv)

    if a.microscope not in monet.CONFIGS:
        raise SystemExit(
            "Unknown microscope %r. Available: %s"
            % (a.microscope, sorted(monet.CONFIGS))
        )
    config = monet.CONFIGS[a.microscope]
    # read-only: no homing, no lasing — we only read the calibration DB
    inst = mco.IlluminationLaserControl(
        config, do_load_cal=True, auto_enable_lasers=False, auto_home=False
    )
    try:
        inst.powermeter_position = a.powermeter_type
    except Exception:
        pass
    lasers_max = _parse_laser_max(a.laser_max)

    print(
        "Level suggestions for %s (floor = %.0f%% of max, %d levels):\n"
        % (a.microscope, a.floor_frac * 100, a.n_levels)
    )
    for laser in inst.lasers:
        try:
            tops = _sample_tops(inst, laser)
        except Exception as exc:
            print("  %s nm: no usable calibration (%s)\n" % (laser, exc))
            continue
        l_max = lasers_max.get(int(laser))
        note = ""
        if l_max is None:
            l_max = max(tops) if tops else 0.0
            note = " (no --laser-max given; using highest calibrated level!)"
        print("== %s nm ==%s" % (laser, note))
        print(
            "  current levels → sample top: "
            + ", ".join("%g→%.1fmW" % (lv, tops[lv]) for lv in sorted(tops))
        )
        res = propose_levels(tops, l_max, a.n_levels, a.floor_frac)
        if res is None:
            print("  (cannot propose: no calibration or L_max)\n")
            continue
        print(
            "  proposed levels → predicted sample top: "
            + ", ".join(
                "%g→~%.1fmW" % (lv, top) for lv, top in res["proposed"]
            )
        )
        print(
            "  → serves set-points down to ~%.1f mW near a curve's gentle top; "
            "below that use closed-loop feedback.\n" % res["lowest_served"]
        )


if __name__ == "__main__":
    sys.exit(main())
