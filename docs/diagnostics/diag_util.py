"""
docs/diagnostics/diag_util.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Shared helpers for the diagnostics analysis scripts (analyze_aging.py,
analyze_drift.py): the full-scale residual metric and the amplitude-vs-shape
correction set, defined once so the two scripts cannot drift apart.
"""

import numpy as np


def fs(resid, scale):
    """RMS of ``resid`` as a percentage of full scale (``|scale|``).

    Non-finite residuals are ignored; returns NaN if there is nothing to
    average or ``scale`` is zero/falsy.
    """
    resid = np.asarray(resid, float)
    ok = np.isfinite(resid)
    if not ok.any() or not scale:
        return float("nan")
    return float(np.sqrt(np.mean(resid[ok] ** 2)) / scale * 100.0)


def corrections(ref, cur, scale):
    """Residual (% full scale) of ``cur`` vs ``ref`` under three corrections:

    ``none``   no correction (raw difference),
    ``scale``  best single global scale factor (least squares) — the
               amplitude-only ceiling,
    ``affine`` best scale + offset (``a*ref + b``) — separates a drifting
               background from the amplitude.

    If ``scale`` ``≈`` ``none`` the drift is a pure amplitude change; if
    ``affine`` ``≪`` ``scale`` the background drifts too; if all stay large
    the shape changed.
    """
    ref = np.asarray(ref, float)
    cur = np.asarray(cur, float)
    ok = np.isfinite(ref) & np.isfinite(cur)
    ref, cur = ref[ok], cur[ok]
    out = {"none": fs(cur - ref, scale)}
    denom = float(np.dot(ref, ref))
    k = float(np.dot(cur, ref) / denom) if denom else 1.0
    out["scale"] = fs(cur - k * ref, scale)
    A = np.vstack([ref, np.ones_like(ref)]).T
    try:
        a, b = np.linalg.lstsq(A, cur, rcond=None)[0]
        out["affine"] = fs(cur - (a * ref + b), scale)
    except Exception:
        out["affine"] = float("nan")
    return out
